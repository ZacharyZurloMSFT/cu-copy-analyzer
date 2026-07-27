"""CLI harness for reproducing the project-scoped Content Understanding copy bug.

Given a portal-created (project-scoped) analyzer that already exists on the
SOURCE resource, the harness:

  1. Verifies the analyzer is ready on SOURCE (no PUT).
  2. Grants copy authorization on SOURCE.
  3. Copies SOURCE -> TARGET (QA) and waits for the LRO.
  4. Runs a diagnostic matrix on TARGET: list, GET by-id under both api-versions
     (API_VERSION + PREVIEW_API_VERSION), and analyze-by-id under the primary
     api-version. Captures the copied analyzer's projectId tag as seen on target.
  5. Writes report.md + http_log.ndjson under runs/<run_id>/.

Subcommands:
  verify-source   GET the existing analyzer on SOURCE, confirm status=ready.
  grant           Grant copy authorization on SOURCE.
  copy            Copy SOURCE -> TARGET and wait for the LRO.
  verify          Diagnostic matrix on TARGET.
  run-all         Do all of the above end-to-end, then write report.md.
  cleanup         Delete the copied analyzer on TARGET. Source is never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from cu_client import CUClient, CUHttpError, HttpLogger, ResourceConfig

try:
    from azure.identity import ClientSecretCredential
except ImportError:  # pragma: no cover
    ClientSecretCredential = None  # type: ignore[assignment]

ANALYZE_SAMPLE_URL = (
    "https://github.com/Azure-Samples/azure-ai-content-understanding-python/raw/refs/heads/main/data/invoice.pdf"
)


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------


@dataclass
class SPNConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    roles_note: str = ""


@dataclass
class Settings:
    api_version: str
    preview_api_version: str
    source: ResourceConfig
    target: ResourceConfig
    target_key: Optional[str]
    source_analyzer_id: str
    source_project_id: Optional[str]
    spn: Optional[SPNConfig] = None

    @staticmethod
    def load() -> "Settings":
        load_dotenv()
        api_version = os.getenv("API_VERSION", "2025-11-01")
        preview_api_version = os.getenv("PREVIEW_API_VERSION", "2026-06-01-preview")

        def build(name: str) -> ResourceConfig:
            prefix = name.upper()
            endpoint = _require_env(f"{prefix}_ENDPOINT")
            resource_id = _require_env(f"{prefix}_RESOURCE_ID")
            region = _require_env(f"{prefix}_REGION")
            auth_mode = os.getenv(f"{prefix}_AUTH_MODE", "entra").strip() or "entra"
            key = os.getenv(f"{prefix}_KEY") or None
            return ResourceConfig(
                name=name,
                endpoint=endpoint,
                resource_id=resource_id,
                region=region,
                auth_mode=auth_mode,
                key=key,
            )

        source = build("source")
        target = build("target")
        target_key = os.getenv("TARGET_KEY") or None
        source_analyzer_id = _require_env("SOURCE_ANALYZER_ID")
        source_project_id = os.getenv("SOURCE_PROJECT_ID") or None

        spn: Optional[SPNConfig] = None
        spn_tenant = os.getenv("SPN_TENANT_ID")
        spn_client = os.getenv("SPN_CLIENT_ID")
        spn_secret = os.getenv("SPN_CLIENT_SECRET")
        if spn_tenant and spn_client and spn_secret and not any(
            v.startswith("<") for v in (spn_tenant, spn_client, spn_secret)
        ):
            spn = SPNConfig(
                tenant_id=spn_tenant,
                client_id=spn_client,
                client_secret=spn_secret,
                roles_note=os.getenv("SPN_ROLES_NOTE", "").strip(),
            )

        return Settings(
            api_version=api_version,
            preview_api_version=preview_api_version,
            source=source,
            target=target,
            target_key=target_key,
            source_analyzer_id=source_analyzer_id,
            source_project_id=source_project_id,
            spn=spn,
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or value.startswith("<"):
        raise SystemExit(f"Missing or placeholder env var: {name}")
    return value


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run_dir(run_id: str) -> Path:
    return Path("runs") / run_id


def _state_path(run_id: str) -> Path:
    return _run_dir(run_id) / "state.json"


def _log_path(run_id: str) -> Path:
    return _run_dir(run_id) / "http_log.ndjson"


def _report_path(run_id: str) -> Path:
    return _run_dir(run_id) / "report.md"


def _spn_probe_report_path(run_id: str) -> Path:
    return _run_dir(run_id) / "spn_probe.md"


def _load_state(run_id: str) -> dict:
    p = _state_path(run_id)
    if not p.exists():
        return {"run_id": run_id, "analyzers": {}, "events": []}
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_state(run_id: str, state: dict) -> None:
    p = _state_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _analyzer_ids(settings: Settings, run_id: str) -> dict[str, str]:
    src_id = settings.source_analyzer_id
    return {
        "src": src_id,
        "copied": f"{src_id}_copied_{run_id}",
    }


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _make_clients(settings: Settings, run_id: str) -> tuple[CUClient, CUClient, HttpLogger]:
    logger = HttpLogger(_log_path(run_id))
    src = CUClient(settings.source, settings.api_version, logger)
    tgt = CUClient(settings.target, settings.api_version, logger)
    return src, tgt, logger


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_verify_source(settings: Settings, run_id: str) -> dict:
    src, _, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings, run_id)
    state["analyzers"].update(ids)

    print(f"[source] verifying existing analyzer {ids['src']}")
    resp = src.get_analyzer(ids["src"], tolerate_404=True, label=f"verify-existing:{ids['src']}")
    if resp.status == 404:
        raise SystemExit(
            f"Source analyzer {ids['src']!r} not found on source resource "
            f"(GET returned 404). Check SOURCE_ANALYZER_ID and SOURCE_ENDPOINT."
        )
    body = resp.body if isinstance(resp.body, dict) else {}
    status = str(body.get("status", "")).lower()
    tags = body.get("tags") or {}
    source_project_id = tags.get("projectId") if isinstance(tags, dict) else None
    state["source_analyzer_details"] = {
        "analyzer_id": ids["src"],
        "status": body.get("status"),
        "project_id_from_tags": source_project_id,
        "project_id_from_env": settings.source_project_id,
    }
    if status != "ready":
        raise SystemExit(
            f"Source analyzer {ids['src']} is not ready (status={body.get('status')}). "
            f"Fix it in the portal before running the copy repro."
        )
    print(f"[source] {ids['src']} -> {body.get('status')} (projectId={source_project_id})")
    state["events"].append({
        "step": "verify_source",
        "analyzer_id": ids["src"],
        "status": body.get("status"),
        "project_id": source_project_id,
    })
    _save_state(run_id, state)
    return state


def cmd_grant(settings: Settings, run_id: str) -> dict:
    src, _, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings, run_id)
    print(f"[source] granting copy authorization for {ids['src']} -> {settings.target.resource_id}")
    resp = src.grant_copy_authorization(
        source_analyzer_id=ids["src"],
        target_resource_id=settings.target.resource_id,
        target_region=settings.target.region,
    )
    state["copy_authorization"] = resp.body
    state["events"].append({"step": "grant", "status": resp.status})
    _save_state(run_id, state)
    print(f"[source] grant returned {resp.status}")
    return state


def cmd_copy(settings: Settings, run_id: str) -> dict:
    _, tgt, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings, run_id)
    print(f"[target] copying {ids['src']} -> {ids['copied']}")
    resp = tgt.copy_analyzer(
        target_analyzer_id=ids["copied"],
        source_azure_resource_id=settings.source.resource_id,
        source_analyzer_id=ids["src"],
        source_region=settings.source.region,
        copy_authorization=state.get("copy_authorization"),
    )
    op_location = resp.headers.get("Operation-Location")
    copy_request_id = resp.headers.get("x-ms-request-id") or resp.headers.get("apim-request-id")
    state["copy"] = {
        "target_analyzer_id": ids["copied"],
        "initial_status": resp.status,
        "initial_headers": resp.headers,
        "operation_location": op_location,
        "x_ms_request_id": copy_request_id,
    }
    print(f"[target] copy accepted status={resp.status} op-location={op_location}")

    if not op_location:
        raise SystemExit("Copy response did not include an Operation-Location header; cannot poll LRO.")
    final = tgt.poll_operation(op_location)
    state["copy"]["final_operation"] = final.body
    print(f"[target] copy operation -> {(final.body or {}).get('status')}")

    state["events"].append({
        "step": "copy",
        "target_analyzer_id": ids["copied"],
        "x_ms_request_id": copy_request_id,
    })
    _save_state(run_id, state)
    return state


def _extract_analyzer_ids(body: Any) -> set[str]:
    if isinstance(body, dict):
        items = body.get("value") or body.get("analyzers") or []
    elif isinstance(body, list):
        items = body
    else:
        return set()
    out: set[str] = set()
    for it in items:
        if isinstance(it, dict):
            aid = it.get("analyzerId") or it.get("id") or it.get("name")
            if aid:
                out.add(str(aid))
    return out


def _list_shows_status(body: Any, analyzer_id: str) -> Optional[str]:
    items: list = []
    if isinstance(body, dict):
        items = body.get("value") or body.get("analyzers") or []
    elif isinstance(body, list):
        items = body
    for it in items:
        if isinstance(it, dict) and (it.get("analyzerId") == analyzer_id or it.get("id") == analyzer_id):
            return it.get("status")
    return None


def _list_shows_project_id(body: Any, analyzer_id: str) -> Optional[str]:
    items: list = []
    if isinstance(body, dict):
        items = body.get("value") or body.get("analyzers") or []
    elif isinstance(body, list):
        items = body
    for it in items:
        if isinstance(it, dict) and (it.get("analyzerId") == analyzer_id or it.get("id") == analyzer_id):
            tags = it.get("tags")
            if isinstance(tags, dict):
                return tags.get("projectId")
            return None
    return None


def cmd_verify(settings: Settings, run_id: str) -> dict:
    _, tgt, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings, run_id)
    analyzer_id = ids["copied"]

    listed = tgt.list_analyzers(label=f"verify-list:{analyzer_id}")
    ids_in_list = _extract_analyzer_ids(listed.body)
    list_status_for_id = _list_shows_status(listed.body, analyzer_id)
    list_project_id = _list_shows_project_id(listed.body, analyzer_id)

    api_versions: list[tuple[str, str]] = [("primary", settings.api_version)]
    if settings.preview_api_version and settings.preview_api_version != settings.api_version:
        api_versions.append(("preview", settings.preview_api_version))

    def one(auth_mode: Optional[str], auth_label: str, api_version_label: str, api_version: str, run_analyze: bool) -> dict:
        get_resp = tgt.get_analyzer(
            analyzer_id,
            tolerate_404=True,
            auth_override=auth_mode,
            label=f"verify-get[{auth_label}][{api_version_label}]:{analyzer_id}",
            api_version_override=api_version,
        )
        analyze_status: Optional[int] = None
        analyze_request_id: Optional[str] = None
        if run_analyze:
            analyze_resp = tgt.analyze(analyzer_id, ANALYZE_SAMPLE_URL, tolerate_404=True)
            analyze_status = analyze_resp.status
            analyze_request_id = analyze_resp.headers.get("x-ms-request-id")
        return {
            "analyzer_id": analyzer_id,
            "auth": auth_label,
            "api_version": api_version,
            "api_version_label": api_version_label,
            "list_present": analyzer_id in ids_in_list,
            "list_status": list_status_for_id,
            "list_project_id": list_project_id,
            "get_status": get_resp.status,
            "get_x_ms_request_id": get_resp.headers.get("x-ms-request-id"),
            "get_apim_request_id": get_resp.headers.get("apim-request-id"),
            "get_x_ms_region": get_resp.headers.get("x-ms-region"),
            "analyze_status": analyze_status,
            "analyze_x_ms_request_id": analyze_request_id,
        }

    matrix: list[dict] = []
    primary_label, primary_version = api_versions[0]
    matrix.append(one(None, tgt.config.auth_mode, primary_label, primary_version, run_analyze=True))
    for extra_label, extra_version in api_versions[1:]:
        matrix.append(one(None, tgt.config.auth_mode, extra_label, extra_version, run_analyze=False))

    if settings.target_key and tgt.config.auth_mode == "entra":
        original_key = tgt.config.key
        tgt.config.key = settings.target_key
        try:
            matrix.append(one("key", "key", primary_label, primary_version, run_analyze=False))
        finally:
            tgt.config.key = original_key

    state["diagnostic_matrix"] = matrix
    state["target_list_project_id"] = list_project_id
    state["events"].append({"step": "verify", "row_count": len(matrix)})
    _save_state(run_id, state)

    for row in matrix:
        print(
            f"[verify] {row['auth']}/{row['api_version_label']}({row['api_version']}): "
            f"list_present={row['list_present']} list_status={row['list_status']} "
            f"list_projectId={row['list_project_id']} get={row['get_status']} "
            f"analyze={row['analyze_status']}"
        )
    return state


def cmd_cleanup(settings: Settings, run_id: str) -> None:
    _, tgt, _ = _make_clients(settings, run_id)
    ids = _analyzer_ids(settings, run_id)
    print(f"[cleanup] run {run_id}: source {ids['src']} is customer-owned — NOT deleted")
    aid = ids["copied"]
    try:
        r = tgt.delete_analyzer(aid)
        print(f"[cleanup] TARGET delete {aid} -> {r.status}")
    except CUHttpError as exc:
        print(f"[cleanup] TARGET delete {aid} failed: {exc}")


# ---------------------------------------------------------------------------
# SPN role-scope probe
# ---------------------------------------------------------------------------


def _resolve_probe_analyzer_id(settings: Settings, run_id: str, override: Optional[str]) -> str:
    if override:
        return override
    state = _load_state(run_id)
    copied = (state.get("copy") or {}).get("target_analyzer_id")
    if copied:
        return copied
    ids = state.get("analyzers") or {}
    if ids.get("copied"):
        return ids["copied"]
    env_id = os.getenv("TARGET_ANALYZER_ID")
    if env_id and not env_id.startswith("<"):
        return env_id
    raise SystemExit(
        "spn-probe: could not resolve target analyzer id. Pass --analyzer-id, "
        "use --run-id from a run that included copy, or set TARGET_ANALYZER_ID in .env."
    )


def _make_spn_target_client(settings: Settings, run_id: str) -> CUClient:
    if settings.spn is None:
        raise SystemExit(
            "spn-probe: SPN_TENANT_ID / SPN_CLIENT_ID / SPN_CLIENT_SECRET must all be set in .env."
        )
    if ClientSecretCredential is None:  # pragma: no cover
        raise SystemExit("azure-identity is not installed; run `pip install -r requirements.txt`.")
    credential = ClientSecretCredential(
        tenant_id=settings.spn.tenant_id,
        client_id=settings.spn.client_id,
        client_secret=settings.spn.client_secret,
    )
    logger = HttpLogger(_log_path(run_id))
    # Force entra auth for SPN probe regardless of TARGET_AUTH_MODE — the whole
    # point is to test bearer-token behavior, not the Ocp-Apim key path.
    target_cfg = ResourceConfig(
        name="target",
        endpoint=settings.target.endpoint,
        resource_id=settings.target.resource_id,
        region=settings.target.region,
        auth_mode="entra",
        key=None,
    )
    return CUClient(target_cfg, settings.api_version, logger, credential=credential)


def cmd_spn_probe(settings: Settings, run_id: str, analyzer_id_override: Optional[str] = None) -> dict:
    analyzer_id = _resolve_probe_analyzer_id(settings, run_id, analyzer_id_override)
    tgt = _make_spn_target_client(settings, run_id)
    roles_note = (settings.spn.roles_note if settings.spn else "") or "(unset)"

    print(f"[spn-probe] target={settings.target.endpoint} analyzer={analyzer_id}")
    print(f"[spn-probe] roles_note={roles_note!r} api_version={settings.api_version}")

    # 1) list — does the id show up at all for this principal?
    list_resp = tgt.list_analyzers(label=f"spn:list:{analyzer_id}")
    list_ids = _extract_analyzer_ids(list_resp.body)
    list_status_for_id = _list_shows_status(list_resp.body, analyzer_id)
    list_project_id = _list_shows_project_id(list_resp.body, analyzer_id)

    # 2) get-by-id — the direct resolve
    get_resp = tgt.get_analyzer(
        analyzer_id,
        tolerate_404=True,
        label=f"spn:get-by-id:{analyzer_id}",
    )
    get_project_id: Optional[str] = None
    if isinstance(get_resp.body, dict):
        tags = get_resp.body.get("tags")
        if isinstance(tags, dict):
            get_project_id = tags.get("projectId")

    # 3) analyze — the real workload call
    try:
        analyze_resp = tgt.analyze(analyzer_id, ANALYZE_SAMPLE_URL, tolerate_404=True)
        analyze_status = analyze_resp.status
        analyze_headers = analyze_resp.headers
        analyze_body_snippet = _snippet(analyze_resp.raw_text)
    except CUHttpError as exc:
        # Capture 403 without raising — that IS the answer for some role combos.
        analyze_status = exc.status
        analyze_headers = {}
        analyze_body_snippet = _snippet(exc.body)

    def _summarise_probe(label: str, status: int, headers: dict, body_snippet: str) -> dict:
        return {
            "probe": label,
            "status": status,
            "x_ms_request_id": headers.get("x-ms-request-id"),
            "apim_request_id": headers.get("apim-request-id"),
            "x_ms_region": headers.get("x-ms-region"),
            "body_snippet": body_snippet,
        }

    probes = [
        {
            **_summarise_probe(
                "spn:list",
                list_resp.status,
                list_resp.headers,
                _snippet(list_resp.raw_text),
            ),
            "list_present": analyzer_id in list_ids,
            "list_status": list_status_for_id,
            "list_project_id": list_project_id,
        },
        {
            **_summarise_probe(
                "spn:get-by-id",
                get_resp.status,
                get_resp.headers,
                _snippet(get_resp.raw_text),
            ),
            "get_project_id": get_project_id,
        },
        {
            **_summarise_probe("spn:analyze", analyze_status, analyze_headers, analyze_body_snippet),
        },
    ]

    verdict = _interpret_spn_probes(probes)
    state = _load_state(run_id)
    state["spn_probe"] = {
        "analyzer_id": analyzer_id,
        "api_version": settings.api_version,
        "roles_note": roles_note,
        "probes": probes,
        "verdict": verdict,
    }
    state["events"].append({"step": "spn-probe", "verdict": verdict, "roles_note": roles_note})
    _save_state(run_id, state)

    for p in probes:
        print(
            f"[spn-probe] {p['probe']:<16} status={p['status']} "
            f"x-ms-request-id={p.get('x_ms_request_id')}"
        )
    print(f"[spn-probe] verdict: {verdict}")

    report_path = _write_spn_probe_report(settings, run_id)
    print(f"[spn-probe] report: {report_path}")
    return state


def _snippet(text: Optional[str], limit: int = 240) -> str:
    if not text:
        return ""
    single_line = " ".join(text.split())
    return single_line if len(single_line) <= limit else single_line[: limit - 1] + "…"


def _interpret_spn_probes(probes: list[dict]) -> str:
    by_label = {p["probe"]: p for p in probes}
    get_status = by_label.get("spn:get-by-id", {}).get("status")
    analyze_status = by_label.get("spn:analyze", {}).get("status")
    if get_status == 200 and analyze_status == 202:
        return "SPN can resolve and analyze the project-scoped analyzer under the current role assignment"
    if get_status == 403 or analyze_status == 403:
        return "SPN rejected at RBAC (403) — role/scope insufficient for this data-plane call"
    if get_status == 404 or analyze_status == 404:
        return "SPN authenticated but analyzer id was not resolvable (404) — token accepted, project scope not visible"
    return f"SPN probe inconclusive (get={get_status}, analyze={analyze_status})"


def _write_spn_probe_report(settings: Settings, run_id: str) -> Path:
    state = _load_state(run_id)
    probe_state = state.get("spn_probe") or {}
    probes: list[dict] = probe_state.get("probes") or []

    lines: list[str] = []
    lines.append(f"# CU SPN Role-Scope Probe — {run_id}")
    lines.append("")
    lines.append(f"**Verdict:** {probe_state.get('verdict')}")
    lines.append("")
    lines.append("## Test setup")
    lines.append("")
    lines.append(f"- Target endpoint: `{settings.target.endpoint}`")
    lines.append(f"- Target resource ID: `{settings.target.resource_id}`")
    lines.append(f"- Target region: {settings.target.region}")
    lines.append(f"- api-version: `{probe_state.get('api_version')}`")
    lines.append(f"- Analyzer id probed: `{probe_state.get('analyzer_id')}`")
    lines.append(f"- SPN tenant: `{settings.spn.tenant_id if settings.spn else '(none)'}`")
    lines.append(f"- SPN client id: `{settings.spn.client_id if settings.spn else '(none)'}`")
    lines.append(f"- `SPN_ROLES_NOTE` (current role assignment being tested): `{probe_state.get('roles_note')}`")
    lines.append("")
    lines.append("## Probe results")
    lines.append("")
    lines.append("| Probe | HTTP status | x-ms-request-id | x-ms-region | Notes |")
    lines.append("|---|---|---|---|---|")
    for p in probes:
        note_bits: list[str] = []
        if p["probe"] == "spn:list":
            note_bits.append(f"list_present={p.get('list_present')}")
            if p.get("list_status") is not None:
                note_bits.append(f"list_status={p.get('list_status')}")
            if p.get("list_project_id"):
                note_bits.append(f"projectId={p.get('list_project_id')}")
        elif p["probe"] == "spn:get-by-id" and p.get("get_project_id"):
            note_bits.append(f"projectId={p.get('get_project_id')}")
        notes = "; ".join(note_bits) or "—"
        lines.append(
            f"| `{p['probe']}` | **{p['status']}** | `{p.get('x_ms_request_id')}` | "
            f"`{p.get('x_ms_region')}` | {notes} |"
        )
    lines.append("")

    lines.append("## Response body snippets")
    lines.append("")
    for p in probes:
        lines.append(f"### `{p['probe']}` → {p['status']}")
        lines.append("")
        lines.append("```")
        lines.append(p.get("body_snippet") or "(empty)")
        lines.append("```")
        lines.append("")

    lines.append("## How to read this")
    lines.append("")
    lines.append(
        "- **200 on get-by-id + 202 on analyze** — the current role assignment is sufficient for the SPN.\n"
        "- **403 on either** — the token was rejected at RBAC. The role/scope does not grant that data-plane call.\n"
        "- **404 on get-by-id** — the token was accepted (authentication passed) but the analyzer id is not resolvable "
        "for this principal. This is the fingerprint of a project-scoped analyzer that the SPN has no visibility into "
        "under its current role/scope.\n"
        "- Cross-reference the `x-ms-request-id` values above with `http_log.ndjson` and share both files with Azure "
        "support so they can pull backend logs."
    )
    lines.append("")

    p = _spn_probe_report_path(run_id)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# run-all + report
# ---------------------------------------------------------------------------


def cmd_run_all(settings: Settings, run_id: str) -> Path:
    _run_dir(run_id).mkdir(parents=True, exist_ok=True)
    print(f"=== run-all RUN_ID={run_id} ===")
    cmd_verify_source(settings, run_id)
    cmd_grant(settings, run_id)
    cmd_copy(settings, run_id)
    cmd_verify(settings, run_id)
    report = write_report(settings, run_id)
    verdict = _determine_verdict(_load_state(run_id))
    print(f"\n=== VERDICT: {verdict} ===")
    print(f"Report: {report}")
    return report


def _determine_verdict(state: dict) -> str:
    """Pass iff get-by-id returns 200 on BOTH api-versions AND analyze returns 202 on primary."""
    matrix = state.get("diagnostic_matrix") or []
    non_key_rows = [r for r in matrix if r["auth"] != "key"]
    primary = next((r for r in non_key_rows if r["api_version_label"] == "primary"), None)
    preview = next((r for r in non_key_rows if r["api_version_label"] == "preview"), None)
    if primary is None:
        return "Copy Pipeline Failed"
    primary_ok = primary["get_status"] == 200 and primary["analyze_status"] == 202
    preview_ok = preview is None or preview["get_status"] == 200
    return "Copy Pipeline Succeeded" if (primary_ok and preview_ok) else "Copy Pipeline Failed"


def write_report(settings: Settings, run_id: str) -> Path:
    state = _load_state(run_id)
    ids = _analyzer_ids(settings, run_id)
    matrix = state.get("diagnostic_matrix") or []
    verdict = _determine_verdict(state)

    copy_info = state.get("copy") or {}
    copy_headers = copy_info.get("initial_headers") or {}
    non_key_rows = [r for r in matrix if r["auth"] != "key"]
    primary = next((r for r in non_key_rows if r["api_version_label"] == "primary"), None)
    preview = next((r for r in non_key_rows if r["api_version_label"] == "preview"), None)

    lines: list[str] = []
    lines.append(f"# CU Copy Analyzer Repro Report — {run_id}")
    lines.append("")
    lines.append(f"**Verdict:** {verdict}")
    lines.append("")
    lines.append("## Resources")
    lines.append("")
    lines.append(f"- **Source endpoint:** {settings.source.endpoint}")
    lines.append(f"- **Source resource ID:** `{settings.source.resource_id}`")
    lines.append(f"- **Source region:** {settings.source.region}")
    lines.append(f"- **Target endpoint:** {settings.target.endpoint}")
    lines.append(f"- **Target resource ID:** `{settings.target.resource_id}`")
    lines.append(f"- **Target region:** {settings.target.region}")
    lines.append(f"- **API version (primary):** {settings.api_version}")
    lines.append(f"- **API version (preview, verify-only):** {settings.preview_api_version}")
    lines.append("")
    lines.append("## Analyzer IDs")
    lines.append("")
    lines.append(f"- Source (portal-created, not deleted on cleanup): `{ids['src']}`")
    lines.append(f"- Copied (on target): `{ids['copied']}`")
    lines.append("")

    details = state.get("source_analyzer_details") or {}
    list_project_id = state.get("target_list_project_id")
    lines.append("## Project scoping")
    lines.append("")
    lines.append(f"- **Source analyzer status (GET on source):** `{details.get('status')}`")
    lines.append(f"- **Source `projectId` (from analyzer tags):** `{details.get('project_id_from_tags')}`")
    lines.append(f"- **`SOURCE_PROJECT_ID` (from .env, informational):** `{details.get('project_id_from_env')}`")
    lines.append(f"- **Copied analyzer `projectId` (from target list tags):** `{list_project_id}`")
    lines.append("")
    lines.append(
        "If the copied analyzer's `projectId` is set but get-by-id returns 404, "
        "this reproduces the reported behavior where list surfaces project-scoped "
        "analyzers that get-by-id cannot resolve."
    )
    lines.append("")

    lines.append("## Copy call")
    lines.append("")
    lines.append(f"- Initial HTTP status: **{copy_info.get('initial_status')}**")
    lines.append(f"- `x-ms-request-id`: `{copy_headers.get('x-ms-request-id')}`")
    lines.append(f"- `apim-request-id`: `{copy_headers.get('apim-request-id')}`")
    lines.append(f"- `x-ms-region`: `{copy_headers.get('x-ms-region')}`")
    lines.append(f"- `Operation-Location`: `{copy_headers.get('Operation-Location')}`")
    final_op = copy_info.get("final_operation") or {}
    lines.append(f"- Terminal status: **{final_op.get('status')}**")
    lines.append("")

    lines.append("## Get-by-id on copied analyzer (entra auth)")
    lines.append("")
    for label, row in (("primary", primary), ("preview", preview)):
        if row is None:
            continue
        lines.append(f"### api-version = `{row['api_version']}` ({label})")
        lines.append("")
        lines.append(f"- HTTP status: **{row['get_status']}**")
        lines.append(f"- `x-ms-request-id`: `{row['get_x_ms_request_id']}`")
        lines.append(f"- `apim-request-id`: `{row['get_apim_request_id']}`")
        lines.append(f"- `x-ms-region`: `{row['get_x_ms_region']}`")
        lines.append("")

    lines.append("## Diagnostic matrix (target resource)")
    lines.append("")
    lines.append("| Auth | api-version | List present? | List status | List projectId | GET by-id | analyze-by-id |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in matrix:
        analyze_cell = r['analyze_status'] if r['analyze_status'] is not None else "—"
        lines.append(
            f"| {r['auth']} | `{r['api_version']}` | "
            f"{'yes' if r['list_present'] else 'no'} | {r['list_status']} | "
            f"{r.get('list_project_id')} | {r['get_status']} | {analyze_cell} |"
        )
    lines.append("")

    lines.append("## Timestamps and request IDs")
    lines.append("")
    lines.append(f"All HTTP calls (start/end UTC, headers, bodies, api-version) are in `{_log_path(run_id).as_posix()}`.")
    lines.append("")
    lines.append(
        "Share this report and the NDJSON log with your Azure support contact — "
        "the resource URIs plus the `x-ms-request-id` values let the service team "
        "pull backend logs for the copy operation and the get-by-id calls."
    )
    lines.append("")

    p = _report_path(run_id)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproduce the project-scoped Content Understanding copy-analyzer bug (SOURCE -> QA target)."
    )
    parser.add_argument("--run-id", help="Reuse an existing run id; defaults to a UTC timestamp.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in [
        ("verify-source", "GET the existing SOURCE_ANALYZER_ID on SOURCE and confirm it is ready."),
        ("grant", "Grant copy authorization on SOURCE for the source analyzer."),
        ("copy", "Copy source analyzer to TARGET and wait for the LRO."),
        ("verify", "Run the diagnostic matrix on TARGET (list, get-by-id on both api-versions, analyze)."),
        ("run-all", "Execute the full repro end-to-end and write report.md."),
        ("cleanup", "Delete the copied analyzer on TARGET. Source is never touched."),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(command=name)

    spn = sub.add_parser(
        "spn-probe",
        help="Probe TARGET (list, get-by-id, analyze) as a service principal to test role/scope requirements.",
    )
    spn.add_argument(
        "--analyzer-id",
        default=None,
        help="Analyzer id to probe on the target. Defaults to the copied analyzer from state.json, "
             "then falls back to TARGET_ANALYZER_ID from .env.",
    )
    spn.set_defaults(command="spn-probe")
    return parser


COMMANDS = {
    "verify-source": cmd_verify_source,
    "grant": cmd_grant,
    "copy": cmd_copy,
    "verify": cmd_verify,
    "run-all": cmd_run_all,
    "cleanup": cmd_cleanup,
    "spn-probe": cmd_spn_probe,
}


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = args.run_id or _default_run_id()
    _run_dir(run_id).mkdir(parents=True, exist_ok=True)

    settings = Settings.load()
    fn = COMMANDS[args.command]
    try:
        if args.command == "spn-probe":
            result = fn(settings, run_id, getattr(args, "analyzer_id", None))
        else:
            result = fn(settings, run_id)
    except CUHttpError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 2
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # pragma: no cover
        print(f"Unhandled error: {exc}", file=sys.stderr)
        return 1

    if args.command != "run-all":
        print(f"[ok] {args.command} finished. Run id: {run_id}")
        if isinstance(result, Path):
            print(f"     Report: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

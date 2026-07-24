"""CLI harness for testing the Azure AI Content Understanding copy-analyzer flow.

Subcommands:
  create-source   Create the source analyzer on SOURCE and wait until ready.
  create-native   Create a native control analyzer on TARGET and wait until ready.
  grant           Grant copy authorization on SOURCE.
  copy            Copy source -> target and wait for the LRO to succeed.
  verify          Run the diagnostic matrix (list, get-by-id, analyze) on TARGET.
  run-all         Do everything above end-to-end, then write report.md.
  cleanup         Delete the three analyzers on both resources for a given run.
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

ANALYZE_SAMPLE_URL = (
    "https://github.com/Azure-Samples/azure-ai-content-understanding-python/raw/refs/heads/main/data/invoice.pdf"
)


def _analyzer_body() -> dict:
    completion = os.getenv("COMPLETION_MODEL") or "gpt-4o-mini"
    embedding = os.getenv("EMBEDDING_MODEL") or "text-embedding-3-large"
    return {
        "description": "Content Understanding copy-analyzer repro harness",
        "baseAnalyzerId": "prebuilt-document",
        "models": {
            "completion": completion,
            "embedding": embedding,
        },
        "fieldSchema": {
            "fields": {
                "Summary": {
                    "type": "string",
                    "method": "generate",
                    "description": "One-paragraph summary.",
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    api_version: str
    base_name: str
    source: ResourceConfig
    target: ResourceConfig
    target_key: Optional[str]  # separate from target.key if primary auth mode is entra

    @staticmethod
    def load() -> "Settings":
        load_dotenv()
        api_version = os.getenv("API_VERSION", "2025-11-01")
        base_name = os.getenv("ANALYZER_BASE_NAME", "cu_copy_repro")

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
        # Keep an independent target key for the entra-vs-key comparison, even when auth_mode=entra.
        target_key = os.getenv("TARGET_KEY") or None
        return Settings(
            api_version=api_version,
            base_name=base_name,
            source=source,
            target=target,
            target_key=target_key,
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


def _analyzer_ids(base_name: str, run_id: str) -> dict[str, str]:
    return {
        "src": f"{base_name}_{run_id}_src",
        "native": f"{base_name}_{run_id}_native",
        "copied": f"{base_name}_{run_id}_copied",
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


def cmd_create_source(settings: Settings, run_id: str) -> dict:
    src, _, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings.base_name, run_id)
    state["analyzers"].update(ids)
    print(f"[source] creating {ids['src']}")
    src.create_or_replace_analyzer(ids["src"], _analyzer_body())
    print(f"[source] polling {ids['src']} for ready…")
    final = src.poll_analyzer_ready(ids["src"])
    status = (final.body or {}).get("status")
    print(f"[source] {ids['src']} -> {status}")
    if str(status).lower() != "ready":
        raise SystemExit(f"Source analyzer did not become ready (status={status})")
    state["events"].append({"step": "create_source", "analyzer_id": ids["src"], "status": status})
    _save_state(run_id, state)
    return state


def cmd_create_native(settings: Settings, run_id: str) -> dict:
    _, tgt, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings.base_name, run_id)
    state["analyzers"].update(ids)
    print(f"[target] creating native control {ids['native']}")
    tgt.create_or_replace_analyzer(ids["native"], _analyzer_body())
    print(f"[target] polling {ids['native']} for ready…")
    final = tgt.poll_analyzer_ready(ids["native"])
    status = (final.body or {}).get("status")
    print(f"[target] {ids['native']} -> {status}")
    if str(status).lower() != "ready":
        raise SystemExit(f"Native target analyzer did not become ready (status={status})")
    state["events"].append({"step": "create_native", "analyzer_id": ids["native"], "status": status})
    _save_state(run_id, state)
    return state


def cmd_grant(settings: Settings, run_id: str) -> dict:
    src, _, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings.base_name, run_id)
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
    ids = _analyzer_ids(settings.base_name, run_id)
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

    if op_location:
        final = tgt.poll_operation(op_location)
        state["copy"]["final_operation"] = final.body
        print(f"[target] copy operation -> {(final.body or {}).get('status')}")
    else:
        final = tgt.poll_analyzer_ready(ids["copied"])
        state["copy"]["final_analyzer"] = final.body
        print(f"[target] copied analyzer -> {(final.body or {}).get('status')}")

    state["events"].append({"step": "copy", "target_analyzer_id": ids["copied"], "x_ms_request_id": copy_request_id})
    _save_state(run_id, state)
    return state


def _verify_pair(tgt: CUClient, analyzer_id: str, *, kind: str, target_key: Optional[str]) -> list[dict]:
    """Run list-shows / get-by-id / analyze-by-id for one analyzer, returning matrix rows."""
    rows: list[dict] = []

    listed = tgt.list_analyzers(label=f"verify-list:{analyzer_id}")
    ids_in_list = _extract_analyzer_ids(listed.body)
    list_status_for_id = _list_shows_status(listed.body, analyzer_id)

    def one(auth_mode: Optional[str], auth_label: str) -> dict:
        get_resp = tgt.get_analyzer(
            analyzer_id,
            tolerate_404=True,
            auth_override=auth_mode,
            label=f"verify-get[{auth_label}]:{analyzer_id}",
        )
        analyze_resp = tgt.analyze(analyzer_id, ANALYZE_SAMPLE_URL, tolerate_404=True)
        return {
            "kind": kind,
            "analyzer_id": analyzer_id,
            "auth": auth_label,
            "list_present": analyzer_id in ids_in_list,
            "list_status": list_status_for_id,
            "get_status": get_resp.status,
            "get_x_ms_request_id": get_resp.headers.get("x-ms-request-id"),
            "get_apim_request_id": get_resp.headers.get("apim-request-id"),
            "get_x_ms_region": get_resp.headers.get("x-ms-region"),
            "get_start_utc": None,  # timestamps live in http_log.ndjson; kept here for optional enrichment
            "analyze_status": analyze_resp.status,
            "analyze_x_ms_request_id": analyze_resp.headers.get("x-ms-request-id"),
        }

    rows.append(one(None, tgt.config.auth_mode))
    if kind == "copied" and target_key and tgt.config.auth_mode == "entra":
        # Extra apples-to-apples comparison under key auth.
        # Temporarily attach the key so auth_override="key" works.
        original_key = tgt.config.key
        tgt.config.key = target_key
        try:
            rows.append(one("key", "key"))
        finally:
            tgt.config.key = original_key
    return rows


def _extract_analyzer_ids(body: Any) -> set[str]:
    """List responses come as either {'value': [...]} or a bare list; be forgiving."""
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


def cmd_verify(settings: Settings, run_id: str) -> dict:
    _, tgt, _ = _make_clients(settings, run_id)
    state = _load_state(run_id)
    ids = _analyzer_ids(settings.base_name, run_id)

    matrix: list[dict] = []
    matrix.extend(_verify_pair(tgt, ids["copied"], kind="copied", target_key=settings.target_key))
    matrix.extend(_verify_pair(tgt, ids["native"], kind="native", target_key=settings.target_key))
    state["diagnostic_matrix"] = matrix
    state["events"].append({"step": "verify", "row_count": len(matrix)})
    _save_state(run_id, state)

    for row in matrix:
        print(
            f"[verify] {row['kind']}/{row['auth']}: list_present={row['list_present']} "
            f"list_status={row['list_status']} get={row['get_status']} analyze={row['analyze_status']}"
        )
    return state


def cmd_cleanup(settings: Settings, run_id: str) -> None:
    src, tgt, _ = _make_clients(settings, run_id)
    ids = _analyzer_ids(settings.base_name, run_id)
    print(f"[cleanup] deleting analyzers for run {run_id}")
    for aid in (ids["src"],):
        try:
            r = src.delete_analyzer(aid)
            print(f"[cleanup] SOURCE delete {aid} -> {r.status}")
        except CUHttpError as exc:
            print(f"[cleanup] SOURCE delete {aid} failed: {exc}")
    for aid in (ids["native"], ids["copied"]):
        try:
            r = tgt.delete_analyzer(aid)
            print(f"[cleanup] TARGET delete {aid} -> {r.status}")
        except CUHttpError as exc:
            print(f"[cleanup] TARGET delete {aid} failed: {exc}")


# ---------------------------------------------------------------------------
# run-all + report
# ---------------------------------------------------------------------------


def cmd_run_all(settings: Settings, run_id: str) -> Path:
    _run_dir(run_id).mkdir(parents=True, exist_ok=True)
    print(f"=== run-all RUN_ID={run_id} ===")
    cmd_create_source(settings, run_id)
    cmd_create_native(settings, run_id)
    cmd_grant(settings, run_id)
    cmd_copy(settings, run_id)
    cmd_verify(settings, run_id)
    report = write_report(settings, run_id)
    verdict = _determine_verdict(_load_state(run_id))
    print(f"\n=== VERDICT: {verdict} ===")
    print(f"Report: {report}")
    return report


def _determine_verdict(state: dict) -> str:
    """Success only when the copied analyzer is retrievable end-to-end.

    Any failure — 404 on get-by-id, missing matrix, native control failing,
    etc. — reports as "Copy Pipeline Failed" since there are multiple
    reasons a copy pipeline can fall over.
    """
    matrix = state.get("diagnostic_matrix") or []
    copied_entra = next(
        (r for r in matrix if r["kind"] == "copied" and r["auth"] in ("entra",)),
        None,
    )
    native_row = next((r for r in matrix if r["kind"] == "native"), None)
    if copied_entra is None or native_row is None:
        return "Copy Pipeline Failed"
    copied_ok = copied_entra["get_status"] == 200 and copied_entra["analyze_status"] == 202
    native_ok = native_row["get_status"] == 200
    if copied_ok and native_ok:
        return "Copy Pipeline Succeeded"
    return "Copy Pipeline Failed"


def write_report(settings: Settings, run_id: str) -> Path:
    state = _load_state(run_id)
    ids = _analyzer_ids(settings.base_name, run_id)
    matrix = state.get("diagnostic_matrix") or []
    verdict = _determine_verdict(state)

    copy_info = state.get("copy") or {}
    copy_headers = copy_info.get("initial_headers") or {}

    copied_get = next(
        (r for r in matrix if r["kind"] == "copied" and r["auth"] in ("entra",)),
        None,
    )

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
    lines.append(f"- **API version:** {settings.api_version}")
    lines.append("")
    lines.append("## Analyzer IDs")
    lines.append("")
    lines.append(f"- Source: `{ids['src']}`")
    lines.append(f"- Native (control on target): `{ids['native']}`")
    lines.append(f"- Copied (on target): `{ids['copied']}`")
    lines.append("")
    lines.append("## Copy call")
    lines.append("")
    lines.append(f"- Initial HTTP status: **{copy_info.get('initial_status')}**")
    lines.append(f"- `x-ms-request-id`: `{copy_headers.get('x-ms-request-id')}`")
    lines.append(f"- `apim-request-id`: `{copy_headers.get('apim-request-id')}`")
    lines.append(f"- `x-ms-region`: `{copy_headers.get('x-ms-region')}`")
    lines.append(f"- `Operation-Location`: `{copy_headers.get('Operation-Location')}`")
    final_op = copy_info.get("final_operation") or copy_info.get("final_analyzer") or {}
    lines.append(f"- Terminal status: **{final_op.get('status')}**")
    lines.append("")
    lines.append("## Get-by-id on copied analyzer (entra auth)")
    lines.append("")
    if copied_get:
        lines.append(f"- HTTP status: **{copied_get['get_status']}**")
        lines.append(f"- `x-ms-request-id`: `{copied_get['get_x_ms_request_id']}`")
        lines.append(f"- `apim-request-id`: `{copied_get['get_apim_request_id']}`")
        lines.append(f"- `x-ms-region`: `{copied_get['get_x_ms_region']}`")
    else:
        lines.append("_Copied analyzer was not verified — see events in state.json._")
    lines.append("")
    lines.append("## Diagnostic matrix (target resource)")
    lines.append("")
    lines.append("| Analyzer | Auth | List present? | List status | GET by-id | analyze-by-id |")
    lines.append("|---|---|---|---|---|---|")
    for r in matrix:
        lines.append(
            f"| `{r['analyzer_id']}` ({r['kind']}) | {r['auth']} | "
            f"{'yes' if r['list_present'] else 'no'} | {r['list_status']} | "
            f"{r['get_status']} | {r['analyze_status']} |"
        )
    lines.append("")
    lines.append("## Timestamps and request IDs")
    lines.append("")
    lines.append(f"All HTTP calls (start/end UTC, headers, bodies) are in `{_log_path(run_id).as_posix()}`.")
    lines.append("")
    lines.append(
        "Share this report and the NDJSON log with your Azure support contact — "
        "the resource URIs plus the `x-ms-request-id` values let the service team "
        "pull backend logs for the copy operation and the get-by-id call."
    )
    lines.append("")

    p = _report_path(run_id)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Azure AI Content Understanding copy-analyzer test harness.")
    parser.add_argument("--run-id", help="Reuse an existing run id; defaults to a UTC timestamp.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in [
        ("create-source", "Create source analyzer on SOURCE and wait for ready."),
        ("create-native", "Create native control analyzer on TARGET and wait for ready."),
        ("grant", "Grant copy authorization on SOURCE for the source analyzer."),
        ("copy", "Copy source analyzer to TARGET and wait for the LRO."),
        ("verify", "Run the diagnostic matrix on TARGET (list, get-by-id, analyze)."),
        ("run-all", "Execute the full repro end-to-end and write report.md."),
        ("cleanup", "Delete the three analyzers on both resources for the given run."),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(command=name)
    return parser


COMMANDS = {
    "create-source": cmd_create_source,
    "create-native": cmd_create_native,
    "grant": cmd_grant,
    "copy": cmd_copy,
    "verify": cmd_verify,
    "run-all": cmd_run_all,
    "cleanup": cmd_cleanup,
}


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = args.run_id or _default_run_id()
    _run_dir(run_id).mkdir(parents=True, exist_ok=True)

    settings = Settings.load()
    fn = COMMANDS[args.command]
    try:
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

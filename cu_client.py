"""Azure AI Content Understanding raw-REST client for the copy-analyzer repro.

Deliberately avoids the CU SDK so we observe authentic HTTP status codes and
response headers. Every request is timed, its interesting headers captured,
and appended as one line to an NDJSON log file.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from azure.identity import DefaultAzureCredential

CAPTURED_HEADERS = (
    "x-ms-request-id",
    "apim-request-id",
    "request-id",
    "Operation-Location",
    "x-ms-region",
    "api-supported-versions",
    "Retry-After",
)

ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _pick_headers(headers) -> dict[str, str]:
    picked: dict[str, str] = {}
    for name in CAPTURED_HEADERS:
        value = headers.get(name)
        if value is not None:
            picked[name] = value
    return picked


@dataclass
class ResourceConfig:
    name: str  # "source" or "target" — used in log entries
    endpoint: str
    resource_id: str
    region: str
    auth_mode: str  # "entra" | "key"
    key: Optional[str] = None

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        if self.auth_mode not in ("entra", "key"):
            raise ValueError(f"{self.name}: auth_mode must be 'entra' or 'key', got {self.auth_mode!r}")
        if self.auth_mode == "key" and not self.key:
            raise ValueError(f"{self.name}: auth_mode=key but no key was provided")


class HttpLogger:
    """Appends one NDJSON line per HTTP call."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


class CUResponse:
    """Lightweight wrapper so callers get status, headers, body without keeping requests.Response."""

    def __init__(self, status: int, headers: dict[str, str], body: Any, raw_text: str, url: str):
        self.status = status
        self.headers = headers  # captured subset
        self.body = body        # parsed json (dict/list) or None
        self.raw_text = raw_text
        self.url = url

    def __repr__(self) -> str:  # pragma: no cover
        return f"CUResponse(status={self.status}, url={self.url})"


class CUHttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> {status}\n{body}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class CUClient:
    """One instance per Foundry resource. Auth-aware, logs every call."""

    def __init__(
        self,
        config: ResourceConfig,
        api_version: str,
        logger: HttpLogger,
        credential: Optional[DefaultAzureCredential] = None,
    ) -> None:
        self.config = config
        self.api_version = api_version
        self.logger = logger
        self._credential = credential
        self._token_cache: Optional[tuple[str, float]] = None  # (token, expires_on)

    # ---- auth ---------------------------------------------------------------

    def _bearer_token(self) -> str:
        now = time.time()
        if self._token_cache and self._token_cache[1] - 60 > now:
            return self._token_cache[0]
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        access = self._credential.get_token(ENTRA_SCOPE)
        self._token_cache = (access.token, float(access.expires_on))
        return access.token

    def _auth_headers(self, auth_override: Optional[str] = None) -> dict[str, str]:
        mode = auth_override or self.config.auth_mode
        if mode == "entra":
            return {"Authorization": f"Bearer {self._bearer_token()}"}
        if mode == "key":
            if not self.config.key:
                raise RuntimeError(f"{self.config.name}: key auth requested but no key set")
            return {"Ocp-Apim-Subscription-Key": self.config.key}
        raise ValueError(f"unknown auth mode {mode!r}")

    # ---- core request -------------------------------------------------------

    def _url(self, path: str, api_version: Optional[str] = None) -> str:
        sep = "&" if "?" in path else "?"
        ver = api_version or self.api_version
        return f"{self.config.endpoint}/contentunderstanding{path}{sep}api-version={ver}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        tolerate_statuses: Iterable[int] = (),
        auth_override: Optional[str] = None,
        label: Optional[str] = None,
        api_version_override: Optional[str] = None,
    ) -> CUResponse:
        effective_api_version = api_version_override or self.api_version
        url = self._url(path, api_version=api_version_override)
        headers = {"Accept": "application/json"}
        headers.update(self._auth_headers(auth_override))
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        start = _utc_now_iso()
        t0 = time.perf_counter()
        try:
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=60)
        except requests.RequestException as exc:
            end = _utc_now_iso()
            self.logger.log({
                "resource": self.config.name,
                "label": label,
                "method": method,
                "url": url,
                "api_version": effective_api_version,
                "request_body": json_body,
                "auth_mode": auth_override or self.config.auth_mode,
                "start_utc": start,
                "end_utc": end,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise

        end = _utc_now_iso()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        captured = _pick_headers(resp.headers)

        try:
            parsed_body: Any = resp.json() if resp.content else None
        except ValueError:
            parsed_body = None

        entry = {
            "resource": self.config.name,
            "label": label,
            "method": method,
            "url": url,
            "api_version": effective_api_version,
            "request_body": json_body,
            "auth_mode": auth_override or self.config.auth_mode,
            "start_utc": start,
            "end_utc": end,
            "elapsed_ms": elapsed_ms,
            "status": resp.status_code,
            "response_headers": captured,
            "response_body": parsed_body if parsed_body is not None else resp.text,
        }
        self.logger.log(entry)

        tolerated = set(tolerate_statuses)
        if not (200 <= resp.status_code < 300) and resp.status_code not in tolerated:
            raise CUHttpError(method, url, resp.status_code, resp.text)

        return CUResponse(
            status=resp.status_code,
            headers=captured,
            body=parsed_body,
            raw_text=resp.text,
            url=url,
        )

    # ---- analyzer operations ------------------------------------------------

    def create_or_replace_analyzer(self, analyzer_id: str, body: dict) -> CUResponse:
        return self._request(
            "PUT",
            f"/analyzers/{analyzer_id}",
            json_body=body,
            tolerate_statuses=(201, 202),
            label=f"create:{analyzer_id}",
        )

    def get_analyzer(
        self,
        analyzer_id: str,
        *,
        tolerate_404: bool = False,
        auth_override: Optional[str] = None,
        label: Optional[str] = None,
        api_version_override: Optional[str] = None,
    ) -> CUResponse:
        tolerated: tuple[int, ...] = (404,) if tolerate_404 else ()
        return self._request(
            "GET",
            f"/analyzers/{analyzer_id}",
            tolerate_statuses=tolerated,
            auth_override=auth_override,
            label=label or f"get:{analyzer_id}",
            api_version_override=api_version_override,
        )

    def list_analyzers(self, *, label: Optional[str] = None) -> CUResponse:
        return self._request("GET", "/analyzers", label=label or "list")

    def delete_analyzer(self, analyzer_id: str) -> CUResponse:
        return self._request(
            "DELETE",
            f"/analyzers/{analyzer_id}",
            tolerate_statuses=(204, 404),
            label=f"delete:{analyzer_id}",
        )

    def grant_copy_authorization(
        self, source_analyzer_id: str, target_resource_id: str, target_region: str
    ) -> CUResponse:
        body = {
            "targetAzureResourceId": target_resource_id,
            "targetRegion": target_region,
        }
        return self._request(
            "POST",
            f"/analyzers/{source_analyzer_id}:grantCopyAuthorization",
            json_body=body,
            label=f"grantCopy:{source_analyzer_id}",
        )

    def copy_analyzer(
        self,
        target_analyzer_id: str,
        source_azure_resource_id: str,
        source_analyzer_id: str,
        source_region: str,
        copy_authorization: Optional[dict] = None,
    ) -> CUResponse:
        body: dict[str, Any] = {
            "sourceAzureResourceId": source_azure_resource_id,
            "sourceAnalyzerId": source_analyzer_id,
            "sourceRegion": source_region,
        }
        if copy_authorization:
            # Some API shapes require the authorization payload be forwarded verbatim; include when present.
            body["copyAuthorization"] = copy_authorization
        return self._request(
            "POST",
            f"/analyzers/{target_analyzer_id}:copy",
            json_body=body,
            tolerate_statuses=(202,),
            label=f"copy:{target_analyzer_id}",
        )

    def analyze(
        self,
        analyzer_id: str,
        input_url: str,
        *,
        tolerate_404: bool = False,
    ) -> CUResponse:
        body = {"inputs": [{"url": input_url}]}
        tolerated: tuple[int, ...] = (202,) + ((404,) if tolerate_404 else ())
        return self._request(
            "POST",
            f"/analyzers/{analyzer_id}:analyze",
            json_body=body,
            tolerate_statuses=tolerated,
            label=f"analyze:{analyzer_id}",
        )

    # ---- polling helpers ----------------------------------------------------

    def poll_analyzer_ready(self, analyzer_id: str, *, timeout_s: int = 300) -> CUResponse:
        """Poll GET /analyzers/{id} until status is ready/succeeded/failed or timeout."""
        return self._poll(
            fn=lambda: self.get_analyzer(analyzer_id, label=f"poll-get:{analyzer_id}"),
            done=_analyzer_terminal,
            timeout_s=timeout_s,
            what=f"analyzer {analyzer_id}",
        )

    def poll_operation(self, operation_location: str, *, timeout_s: int = 600) -> CUResponse:
        """Poll a service-returned Operation-Location URL until terminal."""
        if not operation_location:
            raise ValueError("operation_location is empty")

        def do_get() -> CUResponse:
            start = _utc_now_iso()
            t0 = time.perf_counter()
            headers = {"Accept": "application/json"}
            headers.update(self._auth_headers())
            resp = requests.get(operation_location, headers=headers, timeout=60)
            end = _utc_now_iso()
            captured = _pick_headers(resp.headers)
            try:
                parsed = resp.json() if resp.content else None
            except ValueError:
                parsed = None
            self.logger.log({
                "resource": self.config.name,
                "label": "poll-op",
                "method": "GET",
                "url": operation_location,
                "auth_mode": self.config.auth_mode,
                "start_utc": start,
                "end_utc": end,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "status": resp.status_code,
                "response_headers": captured,
                "response_body": parsed if parsed is not None else resp.text,
            })
            if not (200 <= resp.status_code < 300):
                raise CUHttpError("GET", operation_location, resp.status_code, resp.text)
            return CUResponse(resp.status_code, captured, parsed, resp.text, operation_location)

        return self._poll(fn=do_get, done=_operation_terminal, timeout_s=timeout_s, what="operation")

    @staticmethod
    def _poll(*, fn, done, timeout_s: int, what: str) -> CUResponse:
        deadline = time.monotonic() + timeout_s
        delay = 1.0
        last: Optional[CUResponse] = None
        while True:
            last = fn()
            if done(last):
                return last
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out after {timeout_s}s polling {what}; last body={last.body!r}")
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


def _analyzer_terminal(resp: CUResponse) -> bool:
    if resp.status != 200 or not isinstance(resp.body, dict):
        return False
    status = str(resp.body.get("status", "")).lower()
    return status in {"ready", "succeeded", "failed", "canceled", "cancelled"}


def _operation_terminal(resp: CUResponse) -> bool:
    if not isinstance(resp.body, dict):
        return False
    status = str(resp.body.get("status", "")).lower()
    return status in {"succeeded", "failed", "canceled", "cancelled"}

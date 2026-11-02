"""call_api: the single guarded path to every connector.

Enforces what the approved manifest says and nothing more: one exact host, declared endpoints and
parameters only, read-only GET, rate limit and budgets (counted in the registry so they survive
restarts), no redirects, a size cap, the key injected from the environment and redacted from
everything stored. Every response is archived under its SHA-256 like any other evidence.
"""

import time
from urllib.parse import quote, urlencode

import httpx
from dotenv import dotenv_values

from config import PROJECT_ROOT
from connectors.manifest import Manifest
from connectors.registry import ConnectorRegistry
from tools.archive import USER_AGENT, archive_bytes, check_public_url, clip, utc_now
from tools.web import extract_text

MAX_BYTES = 5_000_000
REDACTED = "[REDACTED]"
PLACEHOLDER_PREFIXES = ("your_", "changeme", "xxx", "<")


class ConnectorError(Exception):
    """A call refused or failed before/around the HTTP request. The message is safe to show and log."""


def get_key(env_name: str) -> str | None:
    """The key from the process environment, else from .env (read fresh, so a key added while Titan runs works)."""
    import os
    value = os.environ.get(env_name) or dotenv_values(PROJECT_ROOT / ".env").get(env_name)
    value = (value or "").strip()
    return None if not value or value.lower().startswith(PLACEHOLDER_PREFIXES) else value


def _coerce(param, value) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    try:
        if param.type == "integer":
            int(text)
        elif param.type == "number":
            float(text)
        elif param.type == "boolean" and text not in ("true", "false"):
            raise ValueError
    except ValueError:
        raise ConnectorError(f"parameter {param.name!r} must be {param.type}, got {value!r}") from None
    if len(text) > 2000:
        raise ConnectorError(f"parameter {param.name!r} is too long")
    return text


def build_request(manifest: Manifest, endpoint_name: str, params: dict | None, key: str | None):
    """(url_with_key_if_query_auth, display_url_without_key, headers). Validates params against the manifest."""
    try:
        endpoint = manifest.endpoint(endpoint_name)
    except KeyError as exc:
        raise ConnectorError(str(exc.args[0])) from None
    params = dict(params or {})
    declared = {p.name: p for p in endpoint.params}
    if unknown := set(params) - set(declared):
        raise ConnectorError(f"unknown parameter(s) {sorted(unknown)} for {endpoint_name}; allowed: {sorted(declared)}")
    if missing := [n for n, p in declared.items() if p.required and n not in params]:
        raise ConnectorError(f"missing required parameter(s) {missing} for {endpoint_name}")

    path = endpoint.path
    query = {}
    for name, value in params.items():
        text = _coerce(declared[name], value)
        if declared[name].in_ == "path":
            path = path.replace("{" + name + "}", quote(text, safe=""))
        else:
            query[name] = text
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain;q=0.5, */*;q=0.1"}
    auth_query = {}
    if manifest.auth.type == "header":
        headers[manifest.auth.name] = key
    elif manifest.auth.type == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif manifest.auth.type == "query":
        auth_query[manifest.auth.name] = key
    base = manifest.base_url + path
    display = base + ("?" + urlencode(query) if query else "")
    full = base + ("?" + urlencode({**query, **auth_query}) if (query or auth_query) else "")
    return full, display, headers


def call_api(registry: ConnectorRegistry, connector_id: str, endpoint: str, params: dict | None = None,
             run_id: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 20.0,
             candidate: tuple[int, Manifest] | None = None) -> dict:
    """Call an active connector. Returns an ingest-ready result (see EvidenceStore.ingest); raises ConnectorError if refused.

    `candidate` = (version, manifest) lets the capability manager's test list exercise a connector that is
    approved but not yet activated. Every other guard still applies."""
    active = candidate or registry.active(connector_id)
    if active is None:
        raise ConnectorError(f"no active connector {connector_id!r}")
    if candidate and candidate[1].connector_id != connector_id:
        raise ConnectorError("candidate manifest does not match connector id")
    version, manifest = active

    key = None
    if manifest.auth.type != "none":
        key = get_key(manifest.key_env)
        if key is None:
            raise ConnectorError(f"{manifest.key_env} is not set in .env")
    full_url, display_url, headers = build_request(manifest, endpoint, params, key)

    # Budgets and rate limit come from the persisted call log
    now, day_ago, minute_ago = time.time(), time.time() - 86_400, time.time() - 60
    if run_id and registry.count_calls(connector_id, run_id=run_id) >= manifest.budget.max_calls_per_run:
        raise ConnectorError(f"{connector_id}: per-run budget of {manifest.budget.max_calls_per_run} calls reached")
    if registry.count_calls(connector_id, day_ago) >= manifest.budget.max_calls_per_day:
        raise ConnectorError(f"{connector_id}: daily budget of {manifest.budget.max_calls_per_day} calls reached")
    cost = manifest.budget.cost_per_call_usd or 0.0
    if manifest.budget.max_cost_per_day_usd is not None and registry.cost_since(connector_id, day_ago) + cost > manifest.budget.max_cost_per_day_usd:
        raise ConnectorError(f"{connector_id}: daily spend cap of ${manifest.budget.max_cost_per_day_usd} would be exceeded")
    if registry.count_calls(connector_id, minute_ago) >= manifest.rate_limit.requests_per_minute:
        raise ConnectorError(f"{connector_id}: rate limit of {manifest.rate_limit.requests_per_minute}/minute reached; retry shortly")

    if transport is None:  # a real network call: the host must resolve to a public address
        check_public_url(full_url)
    status, body, content_type, encoding = None, b"", "", "utf-8"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", full_url, headers=headers) as resp:
                status = resp.status_code
                if 300 <= status < 400:
                    raise ConnectorError(f"{connector_id}: redirect to {resp.headers.get('location', '?')[:80]} not followed "
                                         "(the manifest must name the final host)")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ConnectorError(f"{connector_id}: response larger than {MAX_BYTES} bytes")
                    chunks.append(chunk)
                body = b"".join(chunks)
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                encoding = resp.encoding or "utf-8"
    except httpx.HTTPError as exc:
        registry.log_call(connector_id, version, endpoint, run_id, status, False)
        raise ConnectorError(f"{connector_id}: network error {type(exc).__name__}") from None
    except ConnectorError:
        registry.log_call(connector_id, version, endpoint, run_id, status, False)
        raise

    ok = 200 <= status < 300
    registry.log_call(connector_id, version, endpoint, run_id, status, ok, cost if ok else 0.0)

    redacted = False
    if key and key.encode() in body:  # some APIs echo the key back
        body, redacted = body.replace(key.encode(), REDACTED.encode()), True
    suffix, text = extract_text(body, content_type, encoding, display_url)
    sha256, path = archive_bytes(body, suffix if suffix != ".bin" else ".json")
    text, truncated = clip(text, 2000)
    return {
        "ok": ok, "url": display_url, "final_url": display_url, "status": status, "content_type": content_type,
        "encoding": encoding, "method": "api", "fetched_at": utc_now(), "sha256": sha256, "archive_path": path,
        "text": text if ok else text[:300], "truncated": truncated, "connector_id": connector_id,
        "connector_version": version, "source_tier": manifest.tier, "key_redacted_from_response": redacted,
    }

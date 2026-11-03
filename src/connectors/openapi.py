"""Turn an OpenAPI 3.x document into manifest endpoints, deterministically. Read-only operations only."""

import re

from connectors.manifest import ENDPOINT_RE, MAX_ENDPOINTS, PLACEHOLDER_RE, Endpoint, Param

TYPE_MAP = {"integer": "integer", "number": "number", "boolean": "boolean"}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (slug if slug[:1].isalpha() else f"op_{slug}")[:40]


def _resolve(spec: dict, node: dict) -> dict:
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref or not ref.startswith("#/"):
        return node
    for part in ref[2:].split("/"):
        spec = spec.get(part.replace("~1", "/").replace("~0", "~"), {})
    return spec


def is_openapi(doc) -> bool:
    return isinstance(doc, dict) and str(doc.get("openapi", "")).startswith("3") and isinstance(doc.get("paths"), dict)


def base_url_from(doc: dict) -> str | None:
    servers = doc.get("servers") or []
    url = servers[0].get("url") if servers and isinstance(servers[0], dict) else None
    return url if url and url.startswith("https://") and "{" not in url else None


def drop_auth_param(endpoints: list[Endpoint], auth_type: str, auth_name: str | None) -> list[Endpoint]:
    """Specs often list the API key as a required query parameter on every endpoint. The caller adds the key from
    .env itself, so the endpoint must not also ask the user (or a model) to supply it."""
    if auth_type == "query" and auth_name:
        for e in endpoints:
            e.params = [p for p in e.params if not (p.in_ == "query" and p.name == auth_name)]
    return endpoints


def endpoints_from(doc: dict) -> list[Endpoint]:
    """GET operations without a request body, as manifest endpoints (at most MAX_ENDPOINTS)."""
    found, names = [], set()
    for path, item in doc["paths"].items():
        item = _resolve(doc, item)
        op = item.get("get") if isinstance(item, dict) else None
        if not op or not PLACEHOLDER_RE.sub("x", path).startswith("/") or "requestBody" in op:
            continue
        name = _slug(op.get("operationId") or f"get_{path}")
        if not ENDPOINT_RE.match(name) or name in names:
            continue
        raw = [_resolve(doc, p) for p in (item.get("parameters") or []) + (op.get("parameters") or [])]
        params, seen = [], set()
        for p in raw:
            if not isinstance(p, dict) or p.get("in") not in ("path", "query") or p.get("name") in seen:
                continue
            seen.add(p["name"])
            schema = _resolve(doc, p.get("schema") or {})
            try:
                params.append(Param(name=p["name"], **{"in": p["in"]}, required=bool(p.get("required")),
                                    type=TYPE_MAP.get(schema.get("type"), "string"),
                                    description=str(p.get("description", ""))[:200]))
            except ValueError:
                continue
        try:
            found.append(Endpoint(name=name, path=path, description=str(op.get("summary") or op.get("description") or name)[:200],
                                  params=params))
        except ValueError:
            continue  # a path the manifest rules refuse (unmatched placeholders etc.)
        names.add(name)
        if len(found) >= MAX_ENDPOINTS:
            break
    return found

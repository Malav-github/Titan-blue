"""Langfuse tracing. Optional: with no keys set, everything here is a no-op and Titan runs as before.

Traces carry prompts, tool outputs and scraped page text to the Langfuse server (cloud by default),
so obvious secrets are masked before export. Tracing observes; it is never part of the evidence trail.
"""

import os
import re

import config  # noqa: F401  (loads .env into os.environ before the SDK reads it)

SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|pk-lf-[A-Za-z0-9-]{8,}|sk-lf-[A-Za-z0-9-]{8,}|BSA[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,})"
)

_client = None


def enabled() -> bool:
    off = os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in ("false", "0", "no")
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")) and not off


def mask(*, data, **kwargs):
    """Redact key-like strings (and the configured contact address) anywhere inside traced data."""
    if isinstance(data, str):
        data = SECRET_PATTERN.sub("[REDACTED]", data)
        return data.replace(config.TITAN_CONTACT, "[CONTACT]") if config.TITAN_CONTACT else data
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return type(data)(mask(data=v) for v in data)
    return data


def client():
    """The shared Langfuse client, or None when tracing is off."""
    global _client
    if not enabled():
        return None
    if _client is None:
        from langfuse import Langfuse
        _client = Langfuse(mask=mask)
    return _client


def callbacks() -> list:
    """LangChain/LangGraph callbacks to pass as config={"callbacks": callbacks()}."""
    if client() is None:
        return []
    from langfuse.langchain import CallbackHandler
    return [CallbackHandler()]


def flush() -> None:
    """Send buffered traces. Call before a short-lived script exits."""
    if _client is not None:
        _client.flush()


def check_connection() -> dict:
    host = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com"
    if not enabled():
        return {"ok": False, "host": host, "error": "keys not set (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY) or tracing disabled"}
    try:
        return {"ok": bool(client().auth_check()), "host": host, "error": None}
    except Exception as exc:
        return {"ok": False, "host": host, "error": f"{type(exc).__name__}: {exc}"}

"""Checks for what a block needs. Each probe returns None when the need is met, else a reason."""

import subprocess
import time

import httpx

import config
from app import ollama_models

_CACHE: dict[str, tuple[float, str | None]] = {}


def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _CACHE[key] = (now, value)
    return value


def clear_cache() -> None:
    _CACHE.clear()


def network() -> str | None:
    return None   # fetches fail loudly with their own error; a probe here would only add latency


def ollama() -> str | None:
    return None if ollama_models() is not None else f"Ollama is not reachable at {config.OLLAMA_BASE_URL}"


def searxng() -> str | None:
    def check():
        try:
            httpx.get(config.SEARXNG_URL, timeout=2).raise_for_status()
            return None
        except httpx.HTTPError:
            return f"SearXNG is not reachable at {config.SEARXNG_URL} (uv run python infra/searxng.py start)"
    return _cached("searxng", 15, check)


def brave_key() -> str | None:
    return None if config.BRAVE_API_KEY else "BRAVE_API_KEY is not set"


def docker() -> str | None:
    def check():
        try:
            done = subprocess.run(["wsl", "-e", "docker", "info"], capture_output=True, timeout=10)
            return None if done.returncode == 0 else "Docker is not running in WSL"
        except (subprocess.TimeoutExpired, OSError):
            return "Docker in WSL did not answer"
    return _cached("docker", 30, check)


def playwright() -> str | None:
    try:
        import playwright  # noqa: F401
        return None
    except ImportError:
        return "Playwright is not installed"


def connector() -> str | None:
    from connectors.registry import ConnectorRegistry
    return None if ConnectorRegistry().list_active() else "No connector is active (use Add a data source)"


PROBES = {
    "network": network, "ollama": ollama, "searxng": searxng, "brave_key": brave_key,
    "docker": docker, "playwright": playwright, "connector": connector,
}

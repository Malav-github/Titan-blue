"""Titan HTTP API. Run from the project root: uv run python serve.py

Binds to localhost only. Every /api request needs the session token (header X-Titan-Token) and a
local Host header, so a web page open in the user's browser cannot talk to it.
"""

import asyncio
import hmac
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import config
from api import probes
from api.engine import TERMINAL, Engine, RunError
from api.executors import MAX_UPLOAD_BYTES, save_upload
from api.registry import BlockDef, Registry
from app import ollama_models
from evidence import EvidenceStore
from runs import RunStore

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "testserver"}
TOKEN_FILE = Path(config.DATA_DIR) / "api_token.txt"


def hostname(host_header: str) -> str:
    """Host header without the port; IPv6 literals keep their brackets."""
    if host_header.startswith("["):
        return host_header.split("]")[0] + "]"
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def load_token() -> str:
    """Token from TITAN_API_TOKEN, else a random one kept in data/ (git-ignored) across restarts."""
    env = os.getenv("TITAN_API_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


class RunRequest(BaseModel):
    inputs: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)


class Decision(BaseModel):
    approved: bool
    note: str = ""


def create_app(registry: Registry | None = None, token: str | None = None,
               probe_map: dict | None = None, store: EvidenceStore | None = None,
               engine: Engine | None = None) -> FastAPI:
    registry = registry or Registry.load()
    token = token or load_token()
    probe_map = probe_map if probe_map is not None else probes.PROBES
    if engine is None:
        from connectors.registry import ConnectorRegistry
        engine = Engine(registry, store or EvidenceStore(config.EVIDENCE_DB_PATH), RunStore(), probe_map,
                        connectors=ConnectorRegistry())
    app = FastAPI(title="Titan", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if hostname(request.headers.get("host", "")) not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-titan-token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "Missing or wrong session token"}, status_code=401)
        return await call_next(request)

    def block_json(block: BlockDef, availability: dict | None = None) -> dict:
        data = block.model_dump()
        if availability is not None:
            data["availability"] = availability
        return data

    def availability_for(blocks: list[BlockDef]) -> dict[str, dict]:
        """Each need is probed once per request, however many blocks share it."""
        results: dict[str, str | None] = {}
        out = {}
        for b in blocks:
            reasons = []
            for need in b.badges.needs:
                key = need.split(":")[0]
                if key not in probe_map:
                    reasons.append(f"cannot check requirement '{need}'")
                    continue
                if key not in results:
                    results[key] = probe_map[key]()
                if results[key]:
                    reasons.append(results[key])
            out[b.id] = {"available": not reasons, "reasons": reasons}
        return out

    @app.get("/api/port-types")
    def port_types():
        return [t.model_dump() for t in registry.port_types.values()]

    @app.get("/api/blocks")
    def blocks():
        latest = registry.list_latest()
        avail = availability_for(latest)
        return [block_json(b, avail[b.id]) for b in latest]

    @app.get("/api/blocks/{block_id}")
    def block(block_id: str):
        b = registry.latest(block_id)
        if b is None:
            raise HTTPException(404, f"No block {block_id}")
        return block_json(b, availability_for([b])[b.id])

    @app.get("/api/models")
    def models():
        installed = ollama_models()
        tasks = {"default": config.LOCAL_LLM_MODEL, **config.TASK_MODELS}
        return {
            "ollama_reachable": installed is not None,
            "installed": installed or [],
            "mode": config.LLM_MODE,
            "tasks": [{"task": t, "model": m, "installed": installed is not None and m in installed}
                      for t, m in tasks.items()],
            "power_mode_tasks": sorted(config.POWER_MODE_TASKS) if config.LLM_MODE == "power" else [],
        }

    @app.get("/api/health")
    def health():
        checks = {name: probe_map[name]() for name in ("ollama", "searxng", "docker") if name in probe_map}
        st = store or EvidenceStore(config.EVIDENCE_DB_PATH)
        problems = st.audit()
        return {
            "mode": config.LLM_MODE,
            "checks": {n: {"ok": r is None, "detail": r} for n, r in checks.items()},
            "search_providers": config.SEARCH_PROVIDERS,
            "brave_key_set": bool(config.BRAVE_API_KEY),
            "langfuse_configured": bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")),
            "evidence": {**st.counts(), "audit_problems": problems},
        }

    @app.post("/api/blocks/{block_id}/run", status_code=202)
    def run_block(block_id: str, body: RunRequest):
        if registry.latest(block_id) is None:
            raise HTTPException(404, f"No block {block_id}")
        graph, run_inputs = engine.single_block_graph(block_id, body.settings, body.inputs)
        try:
            return {"run_id": engine.start(graph, run_inputs, registry.latest(block_id).title)}
        except RunError as exc:
            raise HTTPException(422, {"problems": exc.problems})

    @app.get("/api/runs")
    def runs(limit: int = 50):
        return engine.runs.runs(min(max(limit, 1), 200))

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        data = engine.describe(run_id)
        if data is None:
            raise HTTPException(404, "No such run")
        return data

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, after: int = 0):
        return engine.runs.events(run_id, after)

    @app.get("/api/runs/{run_id}/stream")
    async def run_stream(run_id: str, after: int = 0):
        """Server-sent events. Ends at a final state or when approval is needed; reconnect with ?after=<last id>."""
        async def gen():
            last = after
            while True:
                status = engine.runs.status(run_id)       # read first: every event up to this status is visible below
                events = engine.runs.events(run_id, last)
                for e in events:
                    last = e["event_id"]
                    yield {"event": e["type"], "id": str(last), "data": json.dumps(e)}
                if not events and (status in TERMINAL or status == "awaiting_approval"):
                    return
                await asyncio.sleep(0.3)
        return EventSourceResponse(gen())

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, body: Decision):
        try:
            engine.resume(run_id, body.approved, body.note)
        except RunError as exc:
            raise HTTPException(409, {"problems": exc.problems})
        return {"ok": True}

    @app.post("/api/uploads", status_code=201)
    async def upload(request: Request, filename: str):
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large")
        data = await request.body()
        try:
            return save_upload(engine.uploads_dir, filename, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    return app

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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import config
from api import probes
from api.engine import TERMINAL, Engine, RunError
from api.executors import MAX_UPLOAD_BYTES, save_upload
from api.jobs import Jobs, run_kind
from api.registry import BlockDef, Registry
from api.templates import Template, load_templates
import harness
import policy as usage_policy
from app import ollama_models
from evidence import EvidenceStore
from runs import RunStore

STOPS = TERMINAL | {"awaiting_approval", "needs_capability", "paused"}      # a stream ends here; reconnect after acting
DIST_DIR = config.PROJECT_ROOT / "frontend" / "dist"
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
    queries: list[str] | None = None        # research plans only: the person's edits
    checklist: list[str] | None = None


class Continue(BaseModel):
    capability: str | None = None           # connector id to use, or null to carry on without a new source


class TemplateRun(BaseModel):
    inputs: dict = Field(default_factory=dict)


class CapabilityDecision(BaseModel):
    approve: str | None = None              # connector id, or null to cancel
    terms_ok: bool = False


def terms_documents(candidate: dict) -> list[dict]:
    """The documents a person should read before approving: the manifest's terms link and the provider's own
    rule pages that Titan enforces. Empty means none was found, and the gate says so."""
    plan = candidate.get("integration_plan") or {}
    docs, seen = [], set()

    def add(url, why):
        if url and url not in seen:
            seen.add(url)
            docs.append({"url": url, "why": why})

    add((plan.get("terms") or {}).get("url"), "terms of use found while researching")
    pol = usage_policy.policy_for(plan.get("host", ""))
    for rule in (pol.rules if pol else ()):
        add(rule["source_url"], f"{rule['id'].replace('_', ' ')}: {rule['summary']}")
    return docs


def create_app(registry: Registry | None = None, token: str | None = None,
               probe_map: dict | None = None, store: EvidenceStore | None = None,
               engine: Engine | None = None, jobs: Jobs | None = None, templates: dict[str, Template] | None = None,
               dist_dir=None) -> FastAPI:
    registry = registry or Registry.load()
    token = token or load_token()
    probe_map = probe_map if probe_map is not None else probes.PROBES
    if engine is None:
        from connectors.registry import ConnectorRegistry
        engine = Engine(registry, store or EvidenceStore(config.EVIDENCE_DB_PATH), RunStore(), probe_map,
                        connectors=ConnectorRegistry())
    connectors = engine.connectors
    if connectors is None:
        from connectors.registry import ConnectorRegistry
        connectors = ConnectorRegistry()
    jobs = jobs or Jobs(engine.runs, connectors)
    templates = templates if templates is not None else load_templates()
    dist_dir = DIST_DIR if dist_dir is None else dist_dir
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

    @app.get("/api/harness/research")
    def research_harness():
        """The research pipeline as nodes and edges, for the flowchart."""
        return harness.research()

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

    def describe_research(run_id: str, events: list[dict]) -> dict:
        status = engine.runs.status(run_id)
        first = next((e for e in events if e["type"] in ("run_started", "queued")), None)
        started = next((e["payload"] for e in events if e["type"] == "run_started"), first["payload"] if first else {})

        def last(kind):
            return next((e["payload"] for e in reversed(events) if e["type"] == kind), None)

        return {"run_id": run_id, "kind": "research", "status": status, "title": started.get("question"),
                "subject": started.get("subject"), "budgets": started.get("budgets"),
                "approval": last("awaiting_approval") if status == "awaiting_approval" else None,
                "capability": last("capability_needed") if status == "needs_capability" else None,
                "report": jobs.report(run_id) if status == "finished" else None, "event_count": len(events)}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        events = engine.runs.events(run_id)
        kind = run_kind(events)
        if kind is None:
            raise HTTPException(404, "No such run")
        return {**engine.describe(run_id), "kind": "block"} if kind == "block" else describe_research(run_id, events)

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
                if not events and status in STOPS:
                    return
                await asyncio.sleep(0.3)
        return EventSourceResponse(gen())

    @app.post("/api/runs/{run_id}/decision", status_code=202)
    def decide(run_id: str, body: Decision):
        kind = run_kind(engine.runs.events(run_id))
        if kind is None:
            raise HTTPException(404, "No such run")
        if kind == "research":
            if engine.runs.status(run_id) != "awaiting_approval":
                raise HTTPException(409, {"problems": ["this run is not waiting for approval"]})
            decision = {"approved": body.approved}
            if body.queries:
                decision["queries"] = body.queries
            if body.checklist:
                decision["checklist"] = body.checklist
            jobs.resume_research(run_id, decision)
            return {"ok": True}
        try:
            engine.resume(run_id, body.approved, body.note)
        except RunError as exc:
            raise HTTPException(409, {"problems": exc.problems})
        return {"ok": True}

    @app.post("/api/runs/{run_id}/continue", status_code=202)
    def continue_run(run_id: str, body: Continue):
        """A research run paused for a data source: carry on with the new connector, or without one."""
        if run_kind(engine.runs.events(run_id)) != "research" or engine.runs.status(run_id) != "needs_capability":
            raise HTTPException(409, {"problems": ["this run is not waiting for a data source"]})
        if body.capability and connectors.active(body.capability) is None:
            raise HTTPException(422, {"problems": [f"{body.capability!r} is not an active connector"]})
        jobs.resume_research(run_id, {"capability": body.capability} if body.capability else {})
        return {"ok": True}

    # -- templates: ready-made apps -------------------------------------------------------------------

    @app.get("/api/templates")
    def list_templates():
        return [t.model_dump() for t in templates.values()]

    @app.post("/api/templates/{template_id}/run", status_code=202)
    def run_template(template_id: str, body: TemplateRun):
        t = templates.get(template_id)
        if t is None:
            raise HTTPException(404, f"No template {template_id}")
        values, problems = t.validate_inputs(body.inputs)
        if t.kind == "capability" and bool(values.get("api_name")) != bool(values.get("docs_url")):
            problems.append("give both the API name and its documentation URL, or neither")
        if problems:
            raise HTTPException(422, {"problems": problems})
        if t.kind == "research":
            budgets = {k: values[k] for k in ("max_searches", "max_fetches") if k in values}
            run_id = jobs.start_research(values["question"], values["subject"], budgets, bool(values.get("allow_capabilities")))
            return {"kind": "research", "run_id": run_id}
        known = {"name": values["api_name"], "docs_url": values["docs_url"]} if values.get("api_name") else None
        return {"kind": "capability", "request_id": jobs.start_capability(values["need"], known)}

    # -- capability requests and connectors ----------------------------------------------------------

    @app.get("/api/capability/requests")
    def capability_requests():
        return [{k: r[k] for k in ("request_id", "run_id", "need", "status", "created_at")} for r in connectors.requests()]

    @app.get("/api/capability/requests/{request_id}")
    def capability_request(request_id: str):
        req = connectors.get_request(request_id)
        if req is None:
            raise HTTPException(404, "No such request")
        gate = jobs.gate(request_id) if req["status"] == "awaiting_user" else None
        if gate:
            gate = {**gate, "candidates": [{**c, "terms_documents": terms_documents(c)} for c in gate["candidates"]]}
        return {**req, "gate": gate}

    @app.post("/api/capability/requests/{request_id}/decision", status_code=202)
    def capability_decision(request_id: str, body: CapabilityDecision):
        req = connectors.get_request(request_id)
        if req is None:
            raise HTTPException(404, "No such request")
        if req["status"] != "awaiting_user":
            raise HTTPException(409, {"problems": ["this request is not waiting for a decision"]})
        if body.approve and not body.terms_ok:
            raise HTTPException(422, {"problems": ["tick that you have read the provider's terms before approving"]})
        jobs.decide_capability(request_id, {"approve": body.approve, "terms_ok": body.terms_ok} if body.approve else {"approve": None})
        return {"ok": True}

    @app.get("/api/connectors")
    def list_connectors():
        import time
        day_ago = time.time() - 86_400
        return [{"connector_id": m.connector_id, "version": v, "name": m.name, "host": m.host, "tier": m.tier,
                 "auth": m.auth.type, "endpoints": [e.name for e in m.endpoints],
                 "budget": m.budget.model_dump(), "calls_last_24h": connectors.count_calls(m.connector_id, day_ago)}
                for v, m in connectors.list_active()]

    @app.get("/api/connectors/{connector_id}")
    def connector(connector_id: str):
        active = connectors.active(connector_id)
        if active is None:
            raise HTTPException(404, "No such active connector")
        return {"version": active[0], "manifest": active[1].model_dump(by_alias=True), "events": connectors.events(connector_id)}

    # -- evidence (read only) -------------------------------------------------------------------------

    @app.get("/api/evidence/claims")
    def evidence_claims(entity: str | None = None, limit: int = 100, offset: int = 0):
        return engine.store.list_claims(entity, min(max(limit, 1), 500), max(offset, 0))

    @app.get("/api/evidence/claims/{claim_id}")
    def evidence_claim(claim_id: int):
        detail = engine.store.claim_detail(claim_id)
        if detail is None:
            raise HTTPException(404, "No such claim")
        return detail

    @app.post("/api/uploads", status_code=201)
    async def upload(request: Request, filename: str):
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large")
        data = await request.body()
        try:
            return save_upload(engine.uploads_dir, filename, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    index = dist_dir / "index.html" if dist_dir else None
    if index and index.exists():
        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(404, "No such endpoint")
            target = (dist_dir / path).resolve()
            if path and target.is_file() and dist_dir.resolve() in target.parents:
                return FileResponse(target)
            # Served only to a local Host header and unreadable by other sites, so the token can ride in the page.
            html = index.read_text(encoding="utf-8").replace("<head>", f'<head><meta name="titan-token" content="{token}">', 1)
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return app

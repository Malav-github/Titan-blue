"""Titan HTTP API. Run from the project root: uv run python serve.py

Binds to localhost only. Every /api request needs the session token (header X-Titan-Token) and a
local Host header, so a web page open in the user's browser cannot talk to it.
"""

import asyncio
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from typing import Literal

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
import stores
from evidence import EvidenceStore
from runs import RunStore

STOPS = TERMINAL | {"awaiting_approval", "needs_capability", "needs_source", "paused"}      # a stream ends here; reconnect after acting
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
    ask_sources: bool = True                # pause for a source when something cannot be established (headless callers send false)
    settings: dict = Field(default_factory=dict)


class Decision(BaseModel):
    approved: bool
    note: str = ""
    queries: list[str] | None = None        # research plans only: the person's edits
    checklist: list[str] | None = None
    comment: str | None = None              # a correction in words: the agent rebuilds its plan with it
    url: str | None = None                  # a source for the question's premise, read and checked on the next look
    override: bool = False                  # research anyway although the premise could not be established


class SourceGiven(BaseModel):
    url: str | None = None                  # a web page to read and check; null (or empty) means "stop here"


class Continue(BaseModel):
    capability: str | None = None           # connector id to use, or null to carry on without a new source


class TemplateRun(BaseModel):
    inputs: dict = Field(default_factory=dict)
    ask_sources: bool = True                # pause for a source when something cannot be established (headless callers send false)


class ArchiveRequest(BaseModel):
    label: str = Field("", max_length=60)
    reason: str = Field("", max_length=300)
    confirm: bool = False                   # the person ticked "archive the current store and start a new one"


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
               dist_dir=None, layout: stores.Layout | None = None) -> FastAPI:
    layout = layout or stores.Layout.live()
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
            if request.method == "POST" and stores.busy() and not request.url.path.startswith("/api/stores"):
                return JSONResponse({"detail": {"problems": ["the evidence store is being archived; try again in a moment"]}}, status_code=409)
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
        return harness.research(strict=False)      # a drift between code and file still draws the chart, said in "problems"

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

        asked = next((e["payload"] for e in events if e["type"] == "queued" and "question" in e["payload"]), {})
        return {"run_id": run_id, "kind": "research", "status": status, "title": started.get("question"), "rerun_of": asked.get("rerun_of"),
                "subject": started.get("subject"), "budgets": started.get("budgets"),
                "approval": last("awaiting_approval") if status == "awaiting_approval" else None,
                "capability": last("capability_needed") if status == "needs_capability" else None,
                "source_request": last("source_needed") if status == "needs_source" else None,
                "report": with_reviews(jobs.report(run_id)) if status in ("finished", "stopped") else None, "event_count": len(events)}

    def with_reviews(report: dict | None) -> dict | None:
        """Each claim of a report with the person's latest check of it (manual verification), read live from the store."""
        if not report:
            return report
        rows = [r for k in ("established", "partial", "context") for r in report.get(k) or []]
        latest = engine.store.latest_reviews([r["claim_id"] for r in rows])
        for r in rows:
            r["review"] = latest.get(r["claim_id"])
        return report

    def describe_verification(run_id: str, events: list[dict]) -> dict:
        started = next((e["payload"] for e in events if e["type"] == "run_started"),
                       next((e["payload"] for e in events if e["type"] == "queued"), {}))
        return {"run_id": run_id, "kind": "verify", "status": engine.runs.status(run_id), "title": started.get("question") or "Verification",
                "of_run": started.get("of_run"), "subject": started.get("subject"), "claims": started.get("claims", []),
                "results": [e["payload"] for e in events if e["type"] == "claim_verified"], "event_count": len(events)}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        events = engine.runs.events(run_id)
        kind = run_kind(events)
        if kind is None:
            raise HTTPException(404, "No such run")
        if kind == "block":
            return {**engine.describe(run_id), "kind": "block"}
        if kind == "verify":
            return describe_verification(run_id, events)
        return describe_research(run_id, events)

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
            if (body.comment or "").strip():
                decision["comment"] = body.comment.strip()
            if (body.url or "").strip():
                if not re.match(r"https?://\S+$", body.url.strip()):
                    raise HTTPException(422, {"problems": ["give a web address that starts with http:// or https://"]})
                decision["url"] = body.url.strip()
            if body.override:
                decision["override"] = True
            jobs.resume_research(run_id, decision)
            return {"ok": True}
        try:
            engine.resume(run_id, body.approved, body.note)
        except RunError as exc:
            raise HTTPException(409, {"problems": exc.problems})
        return {"ok": True}

    @app.post("/api/runs/{run_id}/source", status_code=202)
    def give_source(run_id: str, body: SourceGiven):
        """A research run paused because something could not be established: read the source the person gives, or stop."""
        if run_kind(engine.runs.events(run_id)) != "research" or engine.runs.status(run_id) != "needs_source":
            raise HTTPException(409, {"problems": ["this run is not waiting for a source"]})
        url = (body.url or "").strip()
        if url and not re.match(r"https?://\S+$", url):
            raise HTTPException(422, {"problems": ["give a web address that starts with http:// or https://"]})
        jobs.resume_research(run_id, {"url": url} if url else {"decline": True})
        return {"ok": True}

    ACTIVE = {"queued", "running", "pausing"}
    WAITING = {"paused", "awaiting_approval", "needs_capability", "needs_source"}

    def research_status(run_id: str) -> str:
        if run_kind(engine.runs.events(run_id)) not in ("research", "verify"):
            raise HTTPException(404, "No such research or verification run")
        return engine.runs.status(run_id)

    def is_verify(run_id: str) -> bool:
        return run_kind(engine.runs.events(run_id)) == "verify"

    @app.post("/api/runs/{run_id}/pause", status_code=202)
    def pause_run(run_id: str):
        """Pause at the next safe point (between steps, or between model calls inside a long step). Nothing done is lost."""
        status = research_status(run_id)
        if status not in ACTIVE:
            raise HTTPException(409, {"problems": [f"this run is {status}, not running"]})
        engine.runs.emit(run_id, "api", "pause_requested", {})
        return {"ok": True, "status": "pausing"}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str):
        """Carry on a paused run from where it stopped."""
        status = research_status(run_id)
        if status == "pausing":
            engine.runs.emit(run_id, "api", "resumed", {"before_pause": True})      # changed your mind before it paused
            return {"ok": True, "status": "running"}
        if status != "paused":
            raise HTTPException(409, {"problems": [f"this run is {status}, not paused"]})
        (jobs.resume_verification(run_id) if is_verify(run_id) else jobs.resume_research(run_id, None))
        return {"ok": True, "status": "queued"}

    @app.post("/api/runs/{run_id}/stop", status_code=202)
    def stop_run(run_id: str):
        """End the run: what is left is skipped and the report is written from what was gathered so far."""
        status = research_status(run_id)
        if status in TERMINAL or status == "stopping":
            raise HTTPException(409, {"problems": [f"this run is already {status}"]})
        engine.runs.emit(run_id, "api", "stop_requested", {})
        if status in WAITING:
            (jobs.stop_verification(run_id) if is_verify(run_id) else jobs.stop_research(run_id))   # not working now: end it now
        return {"ok": True, "status": "stopping"}

    @app.post("/api/runs/{run_id}/rerun", status_code=202)
    def rerun(run_id: str):
        """Run the same request again as a new run (fresh searches and sources); the old run and its report are kept."""
        if run_kind(engine.runs.events(run_id)) != "research":
            raise HTTPException(404, "No such research run")
        status = engine.runs.status(run_id)
        if status not in TERMINAL:
            raise HTTPException(409, {"problems": [f"this run is {status}; stop it first, or let it finish, then run it again"]})
        try:
            return {"run_id": jobs.rerun_research(run_id)}
        except ValueError as exc:
            raise HTTPException(409, {"problems": [str(exc)]})

    class VerifyRequest(BaseModel):
        claim_ids: list[int] = []                      # empty: every claim of the answer

    @app.post("/api/runs/{run_id}/verify", status_code=202)
    def verify_run(run_id: str, body: VerifyRequest):
        """Start the verification agent on a finished research run's answer (a separate run, which can be paused and stopped)."""
        if run_kind(engine.runs.events(run_id)) != "research":
            raise HTTPException(404, "No such research run")
        if engine.runs.status(run_id) not in ("finished", "stopped"):
            raise HTTPException(409, {"problems": ["verify a run once it has its answer"]})
        return {"verify_run_id": jobs.start_verification(run_id, body.claim_ids or None)}

    @app.get("/api/runs/{run_id}/verification")
    def run_verification(run_id: str):
        """The verification agent's findings for a research run: the latest verification run, its status and each claim's result."""
        found = engine.store.verifications_of_run(run_id)
        verify_runs = [r for r in engine.runs.runs(200) if r.get("kind") == "verify"]
        mine = [r["run_id"] for r in verify_runs
                if next((e["payload"].get("of_run") for e in engine.runs.events(r["run_id"]) if e["type"] in ("run_started", "queued")), None) == run_id]
        latest = mine[0] if mine else None
        return {"verify_run_id": latest, "status": engine.runs.status(latest) if latest else None,
                "results": [v for v in found if v["verify_run_id"] == latest] if latest else []}

    class Review(BaseModel):
        verdict: Literal["confirmed", "wrong", "not_what_i_asked"]
        note: str = ""
        run_id: str = ""

    @app.post("/api/evidence/claims/{claim_id}/review", status_code=201)
    def review_claim(claim_id: int, body: Review):
        """Your own check of a claim against its source: confirmed, wrong, or not what you asked. Kept with the claim."""
        if engine.store.get_claim(claim_id) is None:
            raise HTTPException(404, "No such claim")
        rid = engine.store.record_review(claim_id, body.verdict, body.note, body.run_id)
        return {"review_id": rid, "latest": engine.store.latest_reviews([claim_id]).get(claim_id)}

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
            run_id = jobs.start_research(values["question"], values["subject"], budgets, bool(values.get("allow_capabilities")),
                                          ask_sources=body.ask_sources, report_type=values.get("report_type"))
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

    @app.get("/api/evidence/sources/{source_sha256}/statements")
    def evidence_statements(source_sha256: str, primary_only: bool = False):
        """The financial statements code read from one archived document: every value with its period, and every check."""
        if engine.store.get_source(source_sha256) is None:
            raise HTTPException(404, "No such source")
        return engine.store.statements_for(source_sha256, primary_only)

    @app.get("/api/evidence/statements")
    def evidence_statement_values(concept: str, period_end: str | None = None, limit: int = 200):
        """One line item (revenue, total_assets, ...) across every document read: the index for checking figures."""
        return engine.store.find_values(concept, period_end)[:min(max(limit, 1), 1000)]

    @app.get("/api/evidence/claims/{claim_id}/figures")
    def evidence_claim_figures(claim_id: int):
        """The figures in a claim's quote, as last checked against the company's statements."""
        if engine.store.get_claim(claim_id) is None:
            raise HTTPException(404, "No such claim")
        return engine.store.claim_figures_for(claim_id)

    class FigureCheck(BaseModel):
        text: str
        entity: str | None = None                  # the company whose statements are the index
        source_sha256: list[str] = []              # or the documents whose statements are
        context: str = ""

    @app.post("/api/evidence/figures/check")
    def evidence_figures_check(req: FigureCheck):
        """Check the figures in any text against a company's financial statements (code; nothing is recorded)."""
        from skills.statements import figures
        entity_id = None
        if req.entity:
            found = engine.store.find_entity(req.entity)
            if found is None:
                raise HTTPException(404, "No such entity")
            entity_id = found["entity_id"]
        if entity_id is None and not req.source_sha256:
            raise HTTPException(400, "Give an entity or source_sha256")
        index = engine.store.statement_figures(entity_id=entity_id, source_sha256s=req.source_sha256)
        return {"figures_indexed": len(index), "fiscal_year_end_month": figures.fiscal_year_end(index),
                "results": figures.check(req.text[:20000], index, req.context[:4000])}

    # -- evidence stores: archive the live one and start a new one; browse archived ones (read only) -------

    def outside_blockers() -> list[str]:
        """Work that keeps its state in the live store but is not a run: a capability request under way or waiting."""
        try:
            pending = [r for r in connectors.requests(100) if r["status"] in ("requested", "researching", "integrating", "awaiting_user")]
        except Exception:
            pending = []
        return [f"data-source request {r['request_id'][:8]} is {r['status'].replace('_', ' ')} ({r['need'][:60]}); "
                "finish or cancel it first" for r in pending]

    def forget_in_memory() -> list[str]:
        cleared = stores.reload_definitions()
        probes.clear_cache()
        runner = getattr(jobs, "_runner", None)
        if runner is not None and hasattr(runner, "_profiles"):
            runner._profiles.clear()
            cleared.append("runner source descriptions")
        return cleared

    @app.get("/api/stores")
    def store_overview():
        return {"live": stores.preview(layout, outside_blockers()), "archives": stores.list_archives(layout),
                "log": stores.read_log(layout)[-20:]}

    @app.post("/api/stores/archive")
    def archive_store(body: ArchiveRequest):
        if not body.confirm:
            raise HTTPException(422, {"problems": ["tick the box to confirm: the current store is archived and a new, empty one is started"]})
        try:
            manifest = stores.archive(body.label, body.reason, by="app", layout=layout, git_commit=stores._commit(),
                                      extra_blockers=outside_blockers())
        except stores.StoreError as exc:
            raise HTTPException(409, {"problems": exc.problems})
        except OSError as exc:              # a file in use: nothing was changed
            raise HTTPException(409, {"problems": [f"could not move the store's files ({exc}); nothing was changed"]})
        forget_in_memory()
        return manifest

    @app.post("/api/maintenance/reload")
    def reload_definitions():
        """Forget what Titan read into memory (definition files, health checks, source descriptions); nothing stored is touched."""
        return {"cleared": forget_in_memory()}

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def busy() -> int:
        """Work this process is doing now. Run statuses are not used: a run left 'running' by a server that was killed is
        not working, and must not block a restart for ever."""
        return jobs.busy() + engine.busy()

    @app.get("/api/server")
    def server_info():
        """This server process: when it started, and whether it can restart itself (only when started by serve.py)."""
        return {"started_at": started_at, "pid": os.getpid(), "can_restart": callable(getattr(app.state, "request_restart", None)),
                "busy": busy()}

    @app.post("/api/server/restart", status_code=202)
    def restart_server():
        """Start a fresh server process with the current code. Refused while anything is working: pause or stop it first.
        Runs that are paused or waiting for you are kept (their state is on disk) and carry on after the restart."""
        restart = getattr(app.state, "request_restart", None)
        if not callable(restart):
            raise HTTPException(501, {"problems": ["this server was not started by serve.py, so it cannot restart itself"]})
        if busy():
            n = busy()
            raise HTTPException(409, {"problems": [f"{n} {'run is' if n == 1 else 'runs are'} in progress. Pause or stop "
                                                   f"{'it' if n == 1 else 'them'}, then restart."]})
        threading.Timer(0.5, restart).start()          # answer first, then shut down
        return {"ok": True, "started_at": started_at}

    def archived(aid: str) -> stores.ArchivedStore:
        try:
            return stores.ArchivedStore(aid, layout)
        except KeyError:
            raise HTTPException(404, "No such archive")

    @app.get("/api/archives/{aid}")
    def archive_overview(aid: str, limit: int = 200):
        a = archived(aid)
        return {"manifest": a.manifest, "runs": a.runs.runs(min(max(limit, 1), 1000))}

    @app.post("/api/archives/{aid}/verify")
    def archive_verify(aid: str):
        return archived(aid).verify()

    @app.get("/api/archives/{aid}/runs/{run_id}")
    def archive_run(aid: str, run_id: str):
        a = archived(aid)
        events = a.runs.events(run_id)
        kind = run_kind(events)
        if kind is None:
            raise HTTPException(404, "No such run in this archive")
        started = next((e["payload"] for e in events if e["type"] in ("run_started", "queued")), {})
        status = a.runs.status(run_id)
        return {"run_id": run_id, "kind": kind, "status": status, "title": started.get("question") or started.get("title"),
                "subject": started.get("subject"), "report": a.report(run_id) if kind == "research" and status == "finished" else None,
                "archive": aid, "event_count": len(events)}

    @app.get("/api/archives/{aid}/runs/{run_id}/events")
    def archive_run_events(aid: str, run_id: str, after: int = 0):
        return archived(aid).runs.events(run_id, after)

    @app.get("/api/archives/{aid}/evidence/claims")
    def archive_claims(aid: str, entity: str | None = None, limit: int = 100, offset: int = 0):
        return archived(aid).evidence.list_claims(entity, min(max(limit, 1), 500), max(offset, 0))

    @app.get("/api/archives/{aid}/evidence/claims/{claim_id}")
    def archive_claim(aid: str, claim_id: int):
        detail = archived(aid).evidence.claim_detail(claim_id)
        if detail is None:
            raise HTTPException(404, "No such claim in this archive")
        return detail

    @app.get("/api/archives/{aid}/evidence/sources/{source_sha256}/statements")
    def archive_statements(aid: str, source_sha256: str, primary_only: bool = False):
        store = archived(aid).evidence
        try:
            return store.statements_for(source_sha256, primary_only)
        except sqlite3.OperationalError:
            return []                                  # archived before statements were recorded

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

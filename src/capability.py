"""Capability manager: finds, vets and integrates the API a stalled task needs, with a human in the loop.

    identify -> research -> draft -> recommend -> [gate: you add the key and approve] -> register -> test -> done

- Requirements (free tier, auth, rate limits, terms...) are only reported if a verbatim quote from a page we
  fetched backs them, checked by code. Everything else is listed as unverified.
- Free options come first. A paid option is recommended over a free one only with a stated reason, shown to you
  as the model's opinion, with the free alternative alongside.
- Only a human adds the key (to .env), accepts the terms and approves. The manager checks the key by name and
  never reads its value into a prompt, a log or a trace.
- The connector is activated only after a todo list of tests passes; then the stalled run is resumed.
"""

import json
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlparse

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

from config import DATA_DIR
from connectors import openapi
from connectors.caller import ConnectorError, call_api, get_key
from connectors.manifest import (ID_RE, PLACEHOLDER_RE, Auth, Budget, Evidence, Manifest, Requirements, SmokeTest, Terms)
from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore, normalize, verify_quote
from runs import RunStore
from support import CONTEXT_CHARS, PROMPT as SUPPORT_PROMPT, SupportVerdict
import policy
from tools.archive import check_public_url, utc_now
from tools.search.fusion import normalize_url
from tools.web import fetch_url as default_fetch

CHECKPOINT_PATH = str(DATA_DIR / "checkpoints.db")
FACT_FIELDS = ("signup", "auth", "free_tier", "pricing", "rate_limit", "identity", "payment_required", "terms_storage")
REQUIREMENT_TERMS = {"price", "pricing", "free", "plan", "tier", "rate", "limit", "quota", "key", "authentication",
                     "token", "terms", "storage", "cache", "commercial", "register", "sign", "account", "cost"}
MIN_READABLE_CHARS = 200
SPEC_PATHS = ("/openapi.json", "/api/openapi.json", "/swagger.json", "/v1/openapi.json")
REGULATORY_HOSTS = (".gov", ".europa.eu", "epo.org", "wipo.int", ".int")


# --- model schemas ----------------------------------------------------------------------------------

class Queries(BaseModel):
    queries: list[str] = Field(description="Distinct web search queries")


class Cand(BaseModel):
    name: str
    docs_url: str = Field(description="A URL copied from the numbered results")
    kind: Literal["rest", "mcp"] = "rest"
    reason: str


class Candidates(BaseModel):
    candidates: list[Cand]


class Fact(BaseModel):
    field: Literal[FACT_FIELDS]
    value: str = Field(description="Short value; for identity and payment_required answer yes or no")
    quote: str = Field(description="Exact words copied from the passage that state it")
    passage: int


class FactList(BaseModel):
    facts: list[Fact] = Field(default_factory=list)


class DraftParam(BaseModel):
    name: str
    in_: Literal["path", "query"] = Field("query", alias="in")
    required: bool = False
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""


class DraftEndpoint(BaseModel):
    name: str = Field(description="lowercase_snake_case")
    path: str = Field(description="Starts with /, placeholders like {id}; no query string")
    description: str
    params: list[DraftParam] = Field(default_factory=list)


class DraftManifest(BaseModel):
    base_url: str = Field(description="https://host[/prefix], no trailing slash")
    auth_type: Literal["none", "header", "bearer", "query"]
    auth_name: str | None = Field(None, description="Header or query parameter name for the key")
    endpoints: list[DraftEndpoint] = Field(description="At most 8 read-only (GET) endpoints")
    smoke_endpoint: str
    smoke_params: dict[str, str] = Field(default_factory=dict)


class Recommendation(BaseModel):
    choice: str = Field(description="connector_id of the recommended candidate")
    reasoning: str = Field(description="Honest trade-offs in plain language")
    paid_justification: str | None = Field(None, description="Required if the choice is not verified free while a free option exists")


class State(TypedDict, total=False):
    request_id: str
    need: str
    parent_run_id: str | None
    known_api: dict | None      # {"name", "docs_url"}: the user named the API, so skip guessing candidates
    candidates: list[dict]
    research: dict
    drafts: dict
    recommendation: dict
    decision: dict | None
    gate_note: str
    connector_id: str | None
    version: int | None
    tests: list[dict]
    outcome: str


def draft_prompt(name: str, docs_url: str, page_text: str, error: str = "") -> str:
    """Shared with the model eval so it tests what the pipeline sends."""
    return (f"API: {name} ({docs_url}). Draft a read-only connector from the documentation below. Use only "
            f"endpoints and parameters the documentation shows; do not guess. {error}\n\nDOCUMENTATION:\n{page_text}")


def with_path_params(path: str, params: list[dict]) -> list[dict]:
    """Declare any {placeholder} the model used in a path but forgot to list as a parameter. Meaning-preserving."""
    in_path = set(PLACEHOLDER_RE.findall(path))
    # a parameter named like a placeholder IS the path parameter, whatever 'in' the model wrote (it defaults to query)
    fixed = [{**p, "in": "path", "required": True} if p["name"] in in_path else p for p in params]
    declared = {p["name"] for p in fixed}
    return fixed + [{"name": n, "in": "path", "required": True, "type": "string", "description": ""}
                    for n in sorted(in_path - declared)]


def resolve_smoke_endpoint(smoke: str, endpoints: list[dict]) -> str:
    """The model sometimes names the smoke test by path or display name instead of the endpoint's name."""
    names = [e["name"] for e in endpoints]
    if slug(smoke) in names:
        return slug(smoke)
    bare = smoke.strip().split("?")[0].rstrip("/")
    for e in endpoints:
        if e["path"].rstrip("/") == bare:
            return e["name"]
    return names[0] if len(names) == 1 else slug(smoke)


def normalize_draft(draft: "DraftManifest") -> dict:
    """Meaning-preserving repairs of a model's manifest draft. Shared by the manager and the model eval so both
    judge the same thing: names to snake_case, query strings out of paths, undeclared path parameters declared,
    the smoke test pointed at a real endpoint and limited to parameters that endpoint declares."""
    eps = []
    for e in draft.endpoints[:8]:
        path = e.path.split("?")[0]
        eps.append({"name": slug(e.name), "path": path, "description": e.description,
                    "params": with_path_params(path, [p.model_dump(by_alias=True) for p in e.params])})
    smoke = resolve_smoke_endpoint(draft.smoke_endpoint, eps)
    declared = {p["name"] for e in eps if e["name"] == smoke for p in e["params"]}
    return {"base_url": draft.base_url.rstrip("/"), "auth": {"type": draft.auth_type, "name": draft.auth_name}, "endpoints": eps,
            "smoke_test": {"endpoint": smoke, "params": {k: v for k, v in draft.smoke_params.items() if k in declared}}}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    s = s if s[:1].isalpha() else f"api_{s}"
    return s[:30].rstrip("_") or "api"


def connector_slug(name: str) -> str:
    """Connector id from a candidate's name. A trailing 'API' is dropped, so the key variable that is derived
    from the id reads OPENALEX_API_KEY and not OPENALEX_API_API_KEY."""
    name = re.sub(r"\(.*?\)|\bvia\b.*$", " ", name.lower())      # "OpenAlex API (via openalexR Package)" -> "openalex api"
    words = re.sub(r"[^a-z0-9]+", " ", name).split() or ["api"]
    while len(words) > 1 and words[-1] in ("api", "rest"):
        words.pop()
    return slug(" ".join(words))


GENERIC_NAME_WORDS = {"api", "rest", "mcp", "server", "official", "the", "data", "web", "service", "services", "public", "open"}


def host_fits_name(host: str, name: str) -> bool:
    """Does an API host plausibly belong to the API named `name`? A blog or an aggregator hosting the docs does not
    (a manifest for dev.to is not a manifest for OpenAlex). Needs a distinctive word of the name inside the host."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 3 and w not in GENERIC_NAME_WORDS]
    host = host.lower()
    return any(w in host for w in words) or bool(words and "".join(words) in host)


def best_windows(text: str, terms: set[str], k: int = 2, size: int = 1800) -> list[str]:
    text = normalize(text)
    windows = [text[i:i + size] for i in range(0, max(len(text), 1), size - 200)]
    words = lambda t: set(re.findall(r"[a-z0-9]{3,}", t.lower()))  # noqa: E731
    ranked = sorted(enumerate(windows), key=lambda iw: (-len(terms & words(iw[1])), iw[0]))
    return [w for _, w in sorted(ranked[:k])]


class CapabilityManager:
    def __init__(self, registry: ConnectorRegistry | None = None, runs: RunStore | None = None,
                 store: EvidenceStore | None = None, checkpoint_path: str | None = None, llm_factory=None,
                 search_fn=None, fetch_fn=None, transport=None, on_ready=None, render_fn=None):
        self.registry = registry or ConnectorRegistry()
        self.runs = runs or RunStore()
        self.store = store or EvidenceStore()
        if search_fn is None:
            from tools.search import search as search_fn
        self.search_fn, self.fetch_fn = search_fn, fetch_fn or default_fetch
        self.render_fn = render_fn or self._default_render
        if llm_factory is None:
            from llm import get_chat_model
            llm_factory = lambda task: get_chat_model(task=task, temperature=0)  # noqa: E731
        self.llm_factory, self.transport, self.on_ready = llm_factory, transport, on_ready
        self.graph = self._build(SqliteSaver(sqlite3.connect(checkpoint_path or CHECKPOINT_PATH, check_same_thread=False)))

    # -- public API ---------------------------------------------------------------------------------

    def start(self, need: str, parent_run_id: str | None = None, request_id: str | None = None,
              known_api: dict | None = None) -> dict:
        """`known_api` = {"name": ..., "docs_url": ...} when the user already knows which API they want. Everything
        after identification (quote-checked research, manifest, host check, gate, tests) still applies."""
        request_id = request_id or self.registry.create_request(parent_run_id, need, "requested directly")
        return self._drive(request_id, {"request_id": request_id, "need": need, "parent_run_id": parent_run_id,
                                        "known_api": known_api, "tests": [], "gate_note": ""})

    def pending(self, request_id: str) -> dict | None:
        """The result shape of start/resume for a request already waiting at the gate, without running anything."""
        snap = self.graph.get_state(self._config(request_id))
        asks = [i.value for t in snap.tasks for i in t.interrupts]
        if not asks:
            return None
        v = snap.values
        return {"request_id": request_id, "status": "awaiting_user", "gate": asks[0], "connector_id": v.get("connector_id"),
                "tests": v.get("tests", []), "outcome": v.get("outcome"), "problems": {}}

    def resume(self, request_id: str, decision: dict) -> dict:
        """Answer the gate: {"approve": "<connector_id>", "terms_ok": True, "budget": {...optional}} or {"approve": None} to cancel."""
        from langgraph.types import Command
        return self._drive(request_id, Command(resume=decision))

    # -- plumbing -----------------------------------------------------------------------------------

    def _config(self, request_id: str) -> dict:
        import tracing
        return {"configurable": {"thread_id": f"cap-{request_id}"}, "callbacks": tracing.callbacks(),
                "run_name": f"titan-capability-{request_id}", "recursion_limit": 60,
                "metadata": {"langfuse_session_id": f"cap-{request_id}", "langfuse_tags": ["titan", "capability"]}}

    def _event(self, s_or_id, event: str, detail: dict | None = None, parent: str | None = None) -> None:
        request_id = s_or_id if isinstance(s_or_id, str) else s_or_id["request_id"]
        self.registry.request_event(request_id, event, detail)
        parent = parent or (None if isinstance(s_or_id, str) else s_or_id.get("parent_run_id"))
        if parent:
            self.runs.emit(parent, "capability", f"capability_{event}", {"request_id": request_id, **(detail or {})})

    def _drive(self, request_id: str, input_) -> dict:
        config = self._config(request_id)
        request = self.registry.get_request(request_id)
        try:
            for _ in self.graph.stream(input_, config, stream_mode="updates"):
                pass
        except Exception as exc:
            self._event(request_id, "failed", {"error": f"{type(exc).__name__}: {exc}"}, request["run_id"])
            return {"request_id": request_id, "status": "failed", "error": str(exc)}
        snap = self.graph.get_state(config)
        asks = [i.value for t in snap.tasks for i in t.interrupts]
        if asks:
            self._event(request_id, "awaiting_user", {"recommendation": asks[0]["recommendation"].get("choice"),
                                                      "note": asks[0].get("note", "")}, request["run_id"])
        v = snap.values
        return {"request_id": request_id, "status": self.registry.get_request(request_id)["status"],
                "gate": asks[0] if asks else None, "connector_id": v.get("connector_id"), "tests": v.get("tests", []),
                "outcome": v.get("outcome"),
                "problems": {n: d["error"] for n, d in (v.get("drafts") or {}).items() if "error" in d}}

    def _ask(self, schema, prompt: str, s: State, task: str = "capability"):
        last = None
        for _ in range(2):
            try:
                return self.llm_factory(task).with_structured_output(schema).invoke(prompt)
            except Exception as exc:
                last = exc
        self._event(s, "llm_failed", {"schema": schema.__name__, "error": f"{type(last).__name__}: {str(last)[:200]}"})
        return None

    def _build(self, checkpointer):
        g = StateGraph(State)
        for name in ("identify", "research", "draft", "recommend", "gate", "register", "test", "finish"):
            g.add_node(name, getattr(self, f"n_{name}"))
        g.add_edge(START, "identify")
        g.add_conditional_edges("identify", lambda s: "research" if s.get("candidates") else "finish")
        g.add_edge("research", "draft")
        g.add_conditional_edges("draft", lambda s: "recommend" if any("manifest" in d for d in s["drafts"].values()) else "finish")
        g.add_edge("recommend", "gate")
        g.add_conditional_edges("gate", self._after_gate)
        g.add_edge("register", "test")
        g.add_edge("test", "finish")
        g.add_edge("finish", END)
        return g.compile(checkpointer=checkpointer)

    def _after_gate(self, s: State) -> str:
        d = s.get("decision") or {}
        if not d.get("approve"):
            return "finish"
        return "register" if not s.get("gate_note") else "gate"

    # -- nodes --------------------------------------------------------------------------------------

    def n_identify(self, s: State) -> dict:
        self._event(s, "researching", {"need": s["need"]})
        known = s.get("known_api")
        if known:
            try:
                check_public_url(known["docs_url"])
            except ValueError as exc:
                return {"candidates": [], "outcome": f"docs URL refused: {exc}"}
            cand = Cand(name=known["name"], docs_url=known["docs_url"], kind="rest", reason="named by the user").model_dump()
            self._event(s, "candidates_found", {"candidates": [cand["name"]], "source": "named by the user"})
            return {"candidates": [cand], "outcome": ""}
        q = self._ask(Queries, (
            f"A research task is blocked because it lacks this data source: {s['need']}\n"
            "Write up to 3 web search queries to find APIs (preferably free or open-data) that provide it, including "
            "'API documentation' in at least one and an MCP-server query in one."), s)
        queries = (q.queries if q else [f"{s['need']} API documentation"])[:3]
        results: dict[str, dict] = {}
        for query in queries:
            try:
                for r in self.search_fn(query, max_results=8)["results"]:
                    results.setdefault(normalize_url(r["url"]), r)
            except Exception as exc:
                self._event(s, "search_failed", {"query": query, "error": str(exc)[:150]})
        shown = list(results.values())[:24]
        if not shown:
            return {"candidates": [], "outcome": "no search results"}
        listing = "\n".join(f"{i}. {r['title'][:80]} - {r['url']}\n   {r['snippet'][:140]!r}" for i, r in enumerate(shown, 1))
        pick = self._ask(Candidates, (
            f"NEED: {s['need']}\n\nSearch results:\n{listing}\n\nChoose up to 3 distinct APIs or MCP servers that could provide "
            "this data. docs_url must be copied from the results. Prefer official sources and free or open-data APIs."), s)
        valid = {normalize_url(r["url"]) for r in shown}
        seen, cands = set(), []
        for c in (pick.candidates if pick else []):
            if normalize_url(c.docs_url) in valid and connector_slug(c.name) not in seen:
                seen.add(connector_slug(c.name))
                item = c.model_dump()
                if re.search(r"\bmcp\b", f"{c.name} {c.docs_url} {c.reason}", re.I):  # the model's kind label is unreliable
                    item["kind"] = "mcp"
                cands.append(item)
        self._event(s, "candidates_found", {"candidates": [c["name"] for c in cands]})
        return {"candidates": cands, "outcome": "" if cands else "no usable candidates"}

    def _fetch_page(self, s: State, url: str) -> dict | None:
        try:
            r = self.fetch_fn(url, max_chars=2000)
            if r["status"] != 200:
                return None
            sha = self.store.ingest(r)
            text = Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
            if len(text.strip()) < MIN_READABLE_CHARS and "html" in r.get("content_type", ""):
                # An app that needs JavaScript (e.g. a developer portal): render it in the browser
                r = self.render_fn(url, max_chars=2000)
                sha = self.store.ingest(r)
                text = Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
            if len(text.strip()) < MIN_READABLE_CHARS:
                return None  # nothing readable: better no page than a page that invites made-up "facts"
            return {"url": r.get("final_url", url), "sha": sha, "text": text, "raw_path": r["archive_path"],
                    "content_type": r.get("content_type", ""), "fetched_at": r["fetched_at"]}
        except Exception:
            return None

    @staticmethod
    def _default_render(url: str, max_chars: int = 2000) -> dict:
        from tools.browser import render_url  # lazy: starts Playwright only when needed
        return render_url(url, max_chars=max_chars, wait_ms=4000)  # portals are slow single-page apps

    def _fact_supported(self, s: State, field: str, value: str, quote: str, page_text: str) -> bool:
        """The quote exists on the page (checked already); does it actually state this fact? Same test claims get."""
        located = verify_quote(page_text, quote)
        norm = normalize(page_text)
        passage = norm[max(0, located["start"] - CONTEXT_CHARS): located["end"] + CONTEXT_CHARS]
        verdict = self._ask(SupportVerdict, SUPPORT_PROMPT.format(
            claim=f"{field.replace('_', ' ')}: {value}", value=value, quote=normalize(quote), passage=passage),
            s, task="claim_support")   # the same checker model that judges research claims
        return bool(verdict and verdict.verdict == "supported")

    def n_research(self, s: State) -> dict:
        research = {}
        for cand in s["candidates"]:
            name, docs = cand["name"], cand["docs_url"]
            pages = [p for p in [self._fetch_page(s, docs)] if p]
            if cand["kind"] == "rest":
                try:
                    extra = self.search_fn(f"{name} API pricing free tier rate limits terms of use", max_results=6)["results"]
                except Exception:
                    extra = []
                host = (urlparse(docs).hostname or "").split(".")[-2:]
                near = [r["url"] for r in extra if (urlparse(r["url"]).hostname or "").split(".")[-2:] == host and r["url"] != docs]
                for url in (near + [r["url"] for r in extra])[:3]:
                    if len(pages) >= 3:
                        break
                    if url not in {p["url"] for p in pages} and (page := self._fetch_page(s, url)):
                        pages.append(page)
            facts, unverified, rejected = [], list(FACT_FIELDS), []
            if pages and cand["kind"] == "rest":
                passages = [(i, p, w) for i, p in enumerate(pages, 1) for w in best_windows(p["text"], REQUIREMENT_TERMS)]
                listing = "\n\n".join(f"[{i}] {p['url']}\n{w}" for i, p, w in passages)
                ask = self._ask(FactList, (
                    f"API: {name}. From the numbered passages, extract facts about: {', '.join(FACT_FIELDS)}.\n"
                    "`quote` must be copied EXACTLY from the passage (20-300 characters). Only include a fact the passage "
                    "states; skip anything you are unsure of. For identity (ID verification needed?) and payment_required "
                    f"(card or payment needed?) answer yes or no.\n\n{listing}"), s)
                for f in (ask.facts if ask else []):
                    page = pages[f.passage - 1] if 1 <= f.passage <= len(pages) else None
                    if f.field in {e.field for e in facts}:
                        continue                       # first supported fact per field wins
                    if not page or not verify_quote(page["text"], f.quote)["ok"]:
                        rejected.append({"field": f.field, "reason": "quote not found on the page"})
                    elif not self._fact_supported(s, f.field, f.value, f.quote, page["text"]):
                        rejected.append({"field": f.field, "reason": "the quote does not state this fact", "quote": normalize(f.quote)[:120]})
                    else:
                        facts.append(Evidence(field=f.field, value=f.value.strip()[:200], quote=normalize(f.quote),
                                              source_url=page["url"], source_sha256=page["sha"], retrieved_at=page["fetched_at"]))
                unverified = [x for x in FACT_FIELDS if x not in {e.field for e in facts}]
            research[name] = {"pages": [{"url": p["url"], "sha": p["sha"], "content_type": p["content_type"],
                                         "raw_path": p["raw_path"]} for p in pages],
                              "evidence": [e.model_dump() for e in facts], "unverified": unverified, "rejected": rejected}
            self._event(s, "researched", {"candidate": name, "pages": len(pages), "verified_facts": len(facts),
                                          "rejected_facts": len(rejected)})
        return {"research": research}

    @staticmethod
    def _requirements(cand: dict, res: dict) -> tuple[Requirements, Terms]:
        by = {}
        for e in res["evidence"]:
            by.setdefault(e["field"], e)
        yn = lambda f: None if f not in by else by[f]["value"].strip().lower().startswith("y")  # noqa: E731
        req = Requirements(
            signup_url=by.get("signup", {}).get("source_url") or cand["docs_url"], needs_account=True if "signup" in by else None,
            needs_identity=yn("identity"), needs_payment=yn("payment_required"),
            free_tier=by.get("free_tier", {}).get("value"), pricing_notes=by.get("pricing", {}).get("value"),
            rate_limit_note=by.get("rate_limit", {}).get("value"), auth_note=by.get("auth", {}).get("value"),
            integration=cand["kind"], unverified=res["unverified"], evidence=[Evidence(**e) for e in res["evidence"]])
        stored = yn("terms_storage")
        terms = Terms(storage_allowed=stored, url=by.get("terms_storage", {}).get("source_url"),
                      notes=by.get("terms_storage", {}).get("value", "") or "unknown: read the terms before approving")
        return req, terms

    @staticmethod
    def _cost(req: Requirements) -> tuple[bool | None, str]:
        """(is_free, label). Only an ongoing free tier counts as free; a time-limited trial or per-call pricing does not."""
        if req.free_tier:
            if re.search(r"\btrial\b|\bexpires?\b|\b\d+[- ]days?\b", req.free_tier, re.I):
                return None, "free trial only (then paid)"
            return True, "free (verified)"
        if req.needs_payment is True or req.pricing_notes:
            return False, "paid"
        if req.needs_payment is False:
            return True, "no payment required (verified)"
        return None, "cost unknown"

    def _spec_from_pages(self, s: State, cand: dict, res: dict):
        """An OpenAPI spec for this API: among the pages already read, else at standard paths on the hosts of those
        pages. A spec is used only if its server host fits the API's name, so a docs link on someone else's site
        can never supply the manifest."""
        def usable(doc) -> bool:
            base = openapi.base_url_from(doc) if openapi.is_openapi(doc) else None
            return bool(base) and host_fits_name(urlparse(base).hostname or "", cand["name"])

        for p in res["pages"]:
            if "json" in p["content_type"]:
                try:
                    doc = json.loads(Path(p["raw_path"]).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if usable(doc):
                    return doc
        hosts = []
        for url in [cand["docs_url"], *(p["url"] for p in res["pages"])]:
            host = urlparse(url).hostname
            if host and host not in hosts:
                hosts.append(host)
        for host in hosts[:4]:
            for path in SPEC_PATHS:
                page = self._fetch_page(s, f"https://{host}{path}")
                if page and "json" in page["content_type"]:
                    try:
                        doc = json.loads(Path(page["raw_path"]).read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        continue
                    if usable(doc):
                        return doc
        return None

    def n_draft(self, s: State) -> dict:
        drafts = {}
        for cand in s["candidates"]:
            name, res = cand["name"], s["research"][cand["name"]]
            cid = connector_slug(name)
            if cand["kind"] != "rest":
                drafts[name] = {"error": "MCP servers are not integrated yet: installing one runs third-party code and "
                                         "needs a pinned version, a code review and a sandboxed run"}
                continue
            if not ID_RE.match(cid):
                drafts[name] = {"error": f"cannot derive a connector id from {name!r}"}
                continue
            req, terms = self._requirements(cand, res)
            try:
                manifest, source = self._draft_manifest(s, cand, res, cid, req, terms)
                if not host_fits_name(manifest.host, name):
                    raise ValueError(f"the API host {manifest.host} has nothing in common with the name {name!r}; "
                                     "refusing a manifest for a host that may belong to someone else")
                rule_problems = policy.manifest_problems(manifest.host, manifest.auth.type, manifest.rate_limit.requests_per_minute)
                if rule_problems:
                    raise ValueError("breaks the provider's usage rules: " + "; ".join(rule_problems))
                is_free, cost_label = self._cost(req)
                drafts[name] = {"manifest": manifest.model_dump(by_alias=True), "is_free": is_free,
                                "cost_label": cost_label, "manifest_source": source}
            except (ValidationError, ValueError, KeyError) as exc:
                drafts[name] = {"error": f"could not draft a valid manifest: {str(exc)[:300]}"}
            self._event(s, "drafted", {"candidate": name, "ok": "manifest" in drafts[name]})
        return {"drafts": drafts}

    def _draft_manifest(self, s: State, cand: dict, res: dict, cid: str, req: Requirements, terms: Terms) -> tuple[Manifest, str]:
        """(manifest, source): "openapi" when parsed from the API's own spec, "model" when the model read the docs."""
        spec = self._spec_from_pages(s, cand, res)
        if spec:
            base = openapi.base_url_from(spec)
            endpoints = openapi.endpoints_from(spec)
            schemes = (spec.get("components") or {}).get("securitySchemes") or {}
            scheme = next(iter(schemes.values()), {}) if schemes else {}
            if scheme.get("type") == "apiKey" and scheme.get("in") in ("header", "query"):
                auth = Auth(type=scheme["in"], name=scheme.get("name"))
            elif scheme.get("type") == "http" and scheme.get("scheme") == "bearer":
                auth = Auth(type="bearer")
            else:
                auth = Auth()
            endpoints = openapi.drop_auth_param(endpoints, auth.type, auth.name)
            smoke = self._smoke_from(endpoints)
            if base and endpoints and smoke:
                return self._assemble(cid, cand, base, auth, endpoints, smoke, req, terms, s['need']), "openapi"
        if not res["pages"]:
            # Never let the model invent an integration from nothing: no readable documentation, no manifest
            raise ValueError("no readable documentation was available to draft a manifest from")
        page_text = "\n\n".join(w for p in res["pages"][:2] for w in best_windows(
            self.store_text(p["sha"]), {"endpoint", "get", "api", "base", "url", "parameter", "query", "authentication", "key"}, 2, 2200))
        error, last_error = "", "no draft was produced"
        for _ in range(2):
            draft = self._ask(DraftManifest, draft_prompt(cand["name"], cand["docs_url"], page_text, error), s)
            if draft is None:
                break
            try:
                norm = normalize_draft(draft)   # cosmetic slips fixed by code; anything meaningful still fails validation
                return Manifest.model_validate(self._manifest_dict(
                    cid, cand, norm["base_url"], norm["auth"], norm["endpoints"], norm["smoke_test"], req, terms, s["need"])), "model"
            except ValidationError as exc:
                last_error = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:3])
                error = f"A previous attempt was invalid: {last_error[:300]}"
        raise ValueError(f"no valid manifest could be drafted from the documentation ({last_error[:200]})")

    def store_text(self, sha: str) -> str:
        return Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")

    @staticmethod
    def _smoke_from(endpoints) -> dict | None:
        for e in endpoints:
            if not any(p.required for p in e.params):
                return {"endpoint": e.name, "params": {}}
        return None

    def _manifest_dict(self, cid, cand, base, auth, endpoints, smoke, req: Requirements, terms: Terms, need: str) -> dict:
        host = urlparse(base).hostname or ""
        free = self._cost(req)[0] is True
        return {"connector_id": cid, "name": cand["name"], "description": cand["reason"], "topics": [need[:100]],
                "base_url": base, "tier": "regulatory" if host.endswith(REGULATORY_HOSTS) else "secondary",
                "auth": auth, "endpoints": endpoints, "smoke_test": smoke,
                "budget": Budget(max_calls_per_day=500 if free else 100).model_dump(),
                "requirements": req.model_dump(), "terms": terms.model_dump()}

    def _assemble(self, cid, cand, base, auth: Auth, endpoints, smoke, req, terms, need: str) -> Manifest:
        return Manifest.model_validate(self._manifest_dict(
            cid, cand, base, auth.model_dump(), [e.model_dump(by_alias=True) for e in endpoints], smoke, req, terms, need))

    def n_recommend(self, s: State) -> dict:
        usable = {n: d for n, d in s["drafts"].items() if "manifest" in d}
        rank = lambda n: (0 if usable[n]["is_free"] is True else 1 if usable[n]["is_free"] is None else 2, n)  # noqa: E731
        order = sorted(usable, key=rank)
        default = order[0]
        summary = "\n".join(
            f"- {n} (id {connector_slug(n)}): free={usable[n]['is_free']}; verified facts: "
            + "; ".join(f"{e['field']}={e['value']}" for e in s["research"][n]["evidence"]) + f"; unverified: {s['research'][n]['unverified']}"
            for n in order)
        rec = self._ask(Recommendation, (
            f"NEED: {s['need']}\nCandidates:\n{summary}\n\nRecommend one by connector id. Free options are the default; recommend a "
            "paid or unknown-cost one over a verified-free one only if it is clearly better for this need, and then fill "
            "paid_justification. Be honest about weaknesses and about what is unverified."), s)
        choice, note, justification = connector_slug(default), "", None
        if rec:
            ids = {connector_slug(n): n for n in usable}
            wanted = ids.get(rec.choice) or ids.get(connector_slug(rec.choice))
            if wanted:
                better_free_exists = usable[order[0]]["is_free"] is True and usable[wanted]["is_free"] is not True
                if better_free_exists and not rec.paid_justification:
                    note = f"The model preferred {wanted}, but gave no reason for choosing it over the free option; defaulting to the free one."
                else:
                    choice, justification = connector_slug(wanted), rec.paid_justification
        if choice == connector_slug(default):
            justification = None   # nothing to justify: this is the cost-ranked default, not a departure from it
        reasoning = rec.reasoning if rec else "No model opinion available; ranked free options first."
        recommendation = {"choice": choice, "reasoning": reasoning, "paid_justification": justification, "note": note,
                          "order": [connector_slug(n) for n in order]}
        self._event(s, "recommended", {"choice": choice})
        return {"recommendation": recommendation}

    def _gate_payload(self, s: State) -> dict:
        candidates = []
        for cand in s["candidates"]:
            name, res, draft = cand["name"], s["research"][cand["name"]], s["drafts"][cand["name"]]
            item = {"name": name, "connector_id": connector_slug(name), "kind": cand["kind"], "docs_url": cand["docs_url"],
                    "why_considered": cand["reason"], "verified_facts": res["evidence"], "unverified": res["unverified"],
                    "facts_rejected": len(res.get("rejected", []))}
            if "manifest" in draft:
                m = Manifest.model_validate(draft["manifest"])
                warnings = []
                if draft.get("manifest_source") == "model":
                    warnings.append("Manifest was drafted by the model from the docs, not an official spec: check the host and endpoints yourself.")
                if "auth" in res["unverified"]:
                    warnings.append(f"Auth type '{draft['manifest']['auth']['type']}' is NOT backed by any quote from the docs; the smoke test will reveal the truth.")
                item.update(is_free=draft["is_free"], cost_label=draft.get("cost_label", "cost unknown"),
                            manifest_source=draft.get("manifest_source"), warnings=warnings, integration_plan={
                    "host": m.host, "tier": m.tier, "auth": m.auth.type, "key_env": m.key_env,
                    "endpoints": {e.name: e.path for e in m.endpoints}, "smoke_test": m.smoke_test.model_dump(),
                    "budget": m.budget.model_dump(), "rate_limit_per_minute": m.rate_limit.requests_per_minute,
                    "terms": m.terms.model_dump(), "signup_url": m.requirements.signup_url})
            else:
                item["cannot_integrate"] = draft["error"]
            candidates.append(item)
        choice = s["recommendation"]["choice"]
        chosen = next((c for c in candidates if c["connector_id"] == choice), {})
        plan = chosen.get("integration_plan", {})
        steps = []
        if plan.get("key_env"):
            steps += [f"Create an account / key at {plan.get('signup_url')}", f"Add {plan['key_env']}=<your key> to .env"]
        steps += ["Read the terms (check whether storing results is allowed)",
                  f'Reply with {{"approve": "{choice}", "terms_ok": true}} to integrate, or {{"approve": null}} to cancel']
        return {"type": "capability_gate", "need": s["need"], "recommendation": s["recommendation"],
                "candidates": candidates, "your_steps": steps, "note": s.get("gate_note", "")}

    def n_gate(self, s: State) -> dict:
        decision = interrupt(self._gate_payload(s)) or {}
        if not decision.get("approve"):
            return {"decision": decision, "gate_note": "", "outcome": "cancelled by user"}
        cid = decision["approve"]
        draft = next((d for n, d in s["drafts"].items() if connector_slug(n) == cid and "manifest" in d), None)
        if draft is None:
            return {"decision": decision, "gate_note": f"{cid!r} is not an integrable candidate; choose one listed."}
        if decision.get("terms_ok") is not True:
            return {"decision": decision, "gate_note": "Confirm you have read the terms: include \"terms_ok\": true."}
        manifest = Manifest.model_validate(draft["manifest"])
        if manifest.key_env and get_key(manifest.key_env) is None:
            return {"decision": decision, "gate_note": f"{manifest.key_env} is not set in .env yet (checked by name only). Add it, then approve again."}
        return {"decision": decision, "gate_note": ""}

    def n_register(self, s: State) -> dict:
        cid = s["decision"]["approve"]
        draft = next(d for n, d in s["drafts"].items() if connector_slug(n) == cid and "manifest" in d)
        data = draft["manifest"]
        if overrides := s["decision"].get("budget"):
            data = {**data, "budget": {**data["budget"], **overrides}}
        manifest = Manifest.model_validate(data)           # overrides pass the same validation
        version = self.registry.propose(manifest, by="capability-manager")
        self.registry.event(cid, version, "approved", {"decision": s["decision"], "request_id": s["request_id"]}, by="user")
        self._event(s, "integrated", {"connector_id": cid, "version": version})
        return {"connector_id": cid, "version": version}

    # -- the todo list ------------------------------------------------------------------------------

    def n_test(self, s: State) -> dict:
        cid, version = s["connector_id"], s["version"]
        manifest = self.registry.get_manifest(cid, version)
        results: list[dict] = []

        def item(title: str, fn) -> bool:
            try:
                detail = fn()
                status = "pass"
            except Exception as exc:
                detail, status = f"{type(exc).__name__}: {str(exc)[:300]}", "fail"
            results.append({"item": title, "status": status, "detail": str(detail or "")})
            self._event(s, "test_item", {"item": title, "status": status, "detail": str(detail or "")[:200]})
            return status == "pass"

        state = {}

        def check_host():
            check_public_url(manifest.base_url)
            return f"{manifest.host} resolves to a public address"

        def check_key():
            if manifest.key_env is None:
                return "no key required"
            if get_key(manifest.key_env) is None:
                raise RuntimeError(f"{manifest.key_env} missing")
            return f"{manifest.key_env} present (value not read into any log)"

        def smoke():
            r = call_api(self.registry, cid, manifest.smoke_test.endpoint, manifest.smoke_test.params,
                         run_id=f"test-{s['request_id']}", transport=self.transport, candidate=(version, manifest))
            if not r["ok"]:
                raise RuntimeError(f"HTTP {r['status']}: {r['text'][:120]}")
            if "json" in r["content_type"]:
                json.loads(Path(r["archive_path"]).read_text(encoding="utf-8"))
            state["result"] = r
            return f"HTTP {r['status']}, {r['content_type']}"

        def evidence_path():
            r = state["result"]
            sha = self.store.ingest(r)
            text = self.store_text(sha)
            if sha != r["sha256"] or not text.strip():
                raise RuntimeError("ingested source does not match the archived response, or has no readable text")
            return f"ingested as source {sha[:12]}, {len(text)} readable characters"

        def secrets():
            r = state["result"]
            key = get_key(manifest.key_env) if manifest.key_env else None
            if key and (key in Path(r["archive_path"]).read_text(encoding="utf-8", errors="replace") or key in r["url"]
                        or key in json.dumps(self.registry.events(cid), default=str)):
                raise RuntimeError("the key appears in stored output")
            return "key absent from the archived response, URL and event log"

        def guards():
            import httpx
            with tempfile.TemporaryDirectory() as tmp:
                reg = ConnectorRegistry(Path(tmp) / "c.db")
                tight = manifest.model_copy(update={
                    "rate_limit": manifest.rate_limit.model_copy(update={"requests_per_minute": 2}),
                    "budget": manifest.budget.model_copy(update={"max_calls_per_run": 1})})
                ok = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
                endpoint, params = manifest.smoke_test.endpoint, manifest.smoke_test.params

                def call(run_id):
                    return call_api(reg, cid, endpoint, params, run_id=run_id, transport=ok, candidate=(1, tight))

                def refused(run_id, expected):
                    try:
                        call(run_id)
                    except ConnectorError as exc:
                        if expected not in str(exc):
                            raise RuntimeError(f"expected a {expected} refusal, got: {exc}") from None
                        return
                    raise RuntimeError(f"{expected} was not enforced")

                call("t")                       # 1 call in the last minute
                refused("t", "per-run budget")  # same run again: over its budget of 1
                call("t2")                      # a new run is fine: 2 calls in the last minute
                refused("t3", "rate limit")     # a third call in the minute: over 2/minute
            return "per-run budget and per-minute rate limit are enforced (offline self-test)"

        passed = (item("Manifest host resolves to a public address", check_host)
                  and item("API key present (checked by name)", check_key)
                  and item("Read-only smoke call succeeds", smoke)
                  and item("Response archives and ingests as evidence", evidence_path)
                  and item("Secrets are absent from stored output", secrets)
                  and item("Rate-limit and budget guards enforce limits", guards))
        if not passed:
            self.registry.event(cid, version, "tested_fail", {"tests": results}, by="capability-manager")
            self._event(s, "tests_failed", {"connector_id": cid})
            return {"tests": results, "outcome": "tests failed; connector not activated"}
        self.registry.event(cid, version, "tested_pass", {"tests": results}, by="capability-manager")
        self.registry.activate(cid, version, by="capability-manager")
        def visible():
            if not any(m.connector_id == cid for _, m in self.registry.list_active()):
                raise RuntimeError("not listed as active")
            return f"active as v{self.registry.active(cid)[0]}"

        if not item("Connector is active and visible to the runner", visible):
            self.registry.disable(cid, "post-activation check failed", by="capability-manager")
            return {"tests": results, "outcome": "tests failed; connector disabled again"}
        return {"tests": results, "outcome": "integrated"}

    def n_finish(self, s: State) -> dict:
        outcome = s.get("outcome") or ""
        if outcome == "integrated":
            self._event(s, "ready", {"connector_id": s["connector_id"]})
            if self.on_ready and s.get("parent_run_id"):
                result = self.on_ready(s["parent_run_id"], s["connector_id"], s["request_id"])
                self._event(s, "resumed", {"parent_status": (result or {}).get("status")})
        elif outcome.startswith("tests failed"):
            pass  # tests_failed already recorded
        else:
            self._event(s, "cancelled" if "cancelled" in outcome else "failed",
                        {"reason": outcome or "no integrable candidate"})
        return {}

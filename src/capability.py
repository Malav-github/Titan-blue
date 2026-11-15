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
from skills import advisor, sourcing
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


class LookUp(BaseModel):
    found: bool = Field(description="True only if a passage answers the question")
    passage: int = Field(0, description="Number of the passage that answers it")
    quote: str = Field("", description="The exact words copied from that passage that answer the question (20-400 characters)")
    answer: str = Field("", description="The answer in one short plain sentence, saying only what the quote says")


class State(TypedDict, total=False):
    request_id: str
    need: str
    brief: dict                 # the person's brief (skills/sourcing): fields, coverage, kind, budget, volume, licence needs
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


def site_of(url_or_host: str) -> str:
    """The site a URL belongs to, roughly its registered domain: help.openalex.org and api.openalex.org are one site."""
    host = (urlparse(url_or_host).hostname if "/" in url_or_host else url_or_host) or ""
    return ".".join(host.lower().split(".")[-2:])


def tag_sources(evidence: list[dict], *own_hosts: str) -> list[dict]:
    """Mark each verified fact `official` when its quote comes from the provider's own site (the docs link or the API host)."""
    own = {site_of(h) for h in own_hosts if h}
    return [{**e, "official": site_of(e["source_url"]) in own} for e in evidence]


GENERIC_NAME_WORDS = {"api", "rest", "mcp", "server", "official", "the", "data", "web", "service", "services", "public", "open"}


def _name_words(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", name.lower()))


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
                 search_fn=None, fetch_fn=None, transport=None, on_ready=None, render_fn=None, directory_fn=None,
                 discovery: tuple[str, ...] = ("catalogue", "directories", "search")):
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
        # a directory's listing (APIs.guru, the MCP Registry): url -> parsed JSON. Its own loader: the APIs.guru list is larger
        # than a page fetch allows. None in tests that do not use directories.
        self.directory_fn = directory_fn if directory_fn is not None else self._default_directory
        self._directory_cache: dict[str, dict] = {}
        self.discovery = discovery                      # where candidates are looked for, in order
        self.graph = self._build(SqliteSaver(sqlite3.connect(checkpoint_path or CHECKPOINT_PATH, check_same_thread=False)))

    # -- public API ---------------------------------------------------------------------------------

    def start(self, need: str, parent_run_id: str | None = None, request_id: str | None = None,
              known_api: dict | None = None, brief: dict | None = None) -> dict:
        """`known_api` = {"name": ..., "docs_url": ...} when the user already knows which API they want. Everything
        after identification (quote-checked research, manifest, host check, gate, tests) still applies. `brief`: the
        person's requirements (skills/sourcing.brief_from); a need alone makes a brief with no constraints."""
        request_id = request_id or self.registry.create_request(parent_run_id, need, "requested directly")
        return self._drive(request_id, {"request_id": request_id, "need": need, "parent_run_id": parent_run_id,
                                        "known_api": known_api, "brief": brief or sourcing.brief_from({"need": need}),
                                        "tests": [], "gate_note": ""})

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
        # an MCP server is recommended even though it cannot be installed yet: the person sees it, labelled, at the gate
        g.add_conditional_edges("draft", lambda s: "recommend" if any("manifest" in d or d.get("kind") == "mcp"
                                                                      for d in s["drafts"].values()) else "finish")
        # the gate is for approving an API; with none that can be connected the request ends, its findings (MCP servers
        # included, labelled) kept with it
        g.add_conditional_edges("recommend", lambda s: "gate" if any("manifest" in d for d in s["drafts"].values()) else "finish")
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
            if known.get("manifest"):
                cand["manifest"] = known["manifest"]     # {"base_url", "auth", "endpoints", "smoke_test"} written by a person
            self._event(s, "candidates_found", {"candidates": [cand["name"]], "source": "named by the user"})
            return {"candidates": [{**cand, "found_in": "named by you"}], "outcome": ""}
        brief = s.get("brief") or sourcing.brief_from({"need": s["need"]})
        want = brief.get("kind", "either")
        caps = {"rest": sourcing.settings()["max_candidates"]["api"], "mcp": sourcing.settings()["max_candidates"]["mcp"]}
        if want == "api":
            caps["mcp"] = 0
        elif want == "mcp":
            caps["rest"] = 0
        cands: list[dict] = []
        seen: set[str] = set()
        first = lambda n: (re.findall(r"[a-z0-9]{3,}", n.lower()) or [""])[0]      # noqa: E731  'EPO OPS API' -> 'epo'
        # "look for other sources": the ones already looked at are left out, with every other name for the same provider
        left_out = {connector_slug(n) for n in brief.get("exclude") or []}
        left_out_first = {first(n) for n in brief.get("exclude") or []} - {""}

        def add(c: dict) -> None:
            key = connector_slug(c["name"])
            if key in left_out or first(c["name"]) in left_out_first:
                if key not in seen:
                    seen.add(key)
                    self._event(s, "candidate_skipped", {"candidate": c["name"], "url": c.get("docs_url"),
                                                         "reason": "you asked for sources other than this one"})
                return
            if not key or key in seen or sum(x["kind"] == c["kind"] for x in cands) >= caps.get(c["kind"], 0):
                return
            if c.get("docs_url"):
                try:
                    check_public_url(c["docs_url"])
                except Exception as exc:                # a private address, or a host that does not resolve: skip this one
                    self._event(s, "candidate_skipped", {"candidate": c["name"], "url": c["docs_url"],
                                                         "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
                    return
            elif c["kind"] == "rest":
                return                                  # an API with no documentation to read cannot be vetted
            seen.add(key)
            cands.append(c)

        if "catalogue" in self.discovery:
            for c in sourcing.from_catalogue(brief):
                add(c)
        if "directories" in self.discovery:
            dirs = sourcing.settings()["directories"]
            if caps["rest"]:
                for c in sourcing.from_apis_guru(self._directory(s, dirs["apis_guru"]["list_url"]), brief):
                    add(c)
            if caps["mcp"]:
                from urllib.parse import quote
                found = [self._directory(s, dirs["mcp_registry"]["search_url"].format(q=quote(q))) for q in sourcing.mcp_queries(brief)]
                for c in sourcing.from_mcp_registry(found, brief):
                    add(c)
        if "search" in self.discovery and sum(c["kind"] == "rest" for c in cands) < caps["rest"]:
            for c in self._search_candidates(s):
                if any(x["kind"] == c["kind"] and first(x["name"]) == first(c["name"]) for x in cands):
                    # web search often finds an article about a provider already found ('EPO OPS API', a blog post on EPO)
                    self._event(s, "candidate_skipped", {"candidate": c["name"], "url": c["docs_url"],
                                                         "reason": "names a provider already found"})
                    continue
                add({**c, "found_in": "web search"})
        by_kind: dict[str, list[str]] = {}
        for c in cands:
            by_kind.setdefault(sourcing.kind_label(c["kind"]), []).append(c["name"])
        self._event(s, "candidates_found", {"candidates": [c["name"] for c in cands], "by_kind": by_kind,
                                            "found_in": {c["name"]: c.get("found_in") for c in cands}})
        return {"candidates": cands, "outcome": "" if cands else "no usable candidates"}

    def _directory(self, s: State, url: str) -> dict:
        """A directory's listing, once per manager. A failure is logged and the search carries on without it."""
        if url not in self._directory_cache:
            try:
                self._directory_cache[url] = self.directory_fn(url) or {}
            except Exception as exc:
                self._event(s, "directory_failed", {"url": url, "error": f"{type(exc).__name__}: {str(exc)[:150]}"})
                return {}
        return self._directory_cache[url]

    @staticmethod
    def _default_directory(url: str) -> dict:
        from tools.archive import archive_bytes
        from tools.web import download
        dl = download(url, max_bytes=25_000_000)        # the APIs.guru list is about 9 MB
        archive_bytes(dl.body, ".json")                 # kept, like every page read
        return json.loads(dl.body)

    def _search_candidates(self, s: State) -> list[dict]:
        """Web search, then the model picks up to 3 from the numbered results (its docs_url must be one of them)."""
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
            self._event(s, "search_failed", {"query": "; ".join(queries), "error": "no search results"})
            return []
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
        return cands

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
            name, docs = cand["name"], cand.get("docs_url")
            pages = [p for p in [self._fetch_page(s, docs)] if p] if docs else []
            if docs:            # an API's or an MCP server's pricing and terms pages: both are researched the same way
                try:
                    what = "MCP server" if cand["kind"] == "mcp" else "API"
                    extra = self.search_fn(f"{name} {what} pricing free tier rate limits terms of use", max_results=6)["results"]
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
            if pages:
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

        if cand.get("spec_url"):                       # a directory (APIs.guru) gave the API's own OpenAPI spec
            page = self._fetch_page(s, cand["spec_url"])
            if page:
                try:
                    doc = json.loads(Path(page["raw_path"]).read_text(encoding="utf-8"))
                    if usable(doc):
                        return doc
                except (ValueError, OSError):
                    pass
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
                drafts[name] = {"error": "MCP servers are not installed yet: installing one runs or trusts third-party code and "
                                         "needs a pinned version, a review of its tools and a sandboxed run", "kind": "mcp"}
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
        """(manifest, source): "provided" when a person wrote the technical part, "openapi" when parsed from the
        API's own spec, "model" when the model read the docs."""
        given = cand.get("manifest")
        if given:
            return Manifest.model_validate(self._manifest_dict(
                cid, cand, given["base_url"], given["auth"], given["endpoints"], given["smoke_test"], req, terms, s["need"])), "provided"
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

    def _fits(self, s: State) -> dict[str, dict]:
        """Each candidate judged against the brief by code (skills/sourcing.fit), on its quoted facts and its documentation."""
        brief = s.get("brief") or sourcing.brief_from({"need": s["need"]})
        out = {}
        for cand in s["candidates"]:
            name = cand["name"]
            res, draft = s["research"].get(name) or {"pages": [], "evidence": []}, s["drafts"].get(name) or {}
            text = " ".join(self.store_text(p["sha"])[:30_000] for p in res["pages"])
            out[name] = sourcing.fit(brief, cand, res["evidence"], draft, text)
        return out

    def n_recommend(self, s: State) -> dict:
        fits = self._fits(s)
        usable = {n: d for n, d in s["drafts"].items() if "manifest" in d}
        cost_rank = lambda n: 0 if usable[n]["is_free"] is True else 1 if usable[n]["is_free"] is None else 2  # noqa: E731
        # best fit first (nothing that fails a hard requirement), then the cheaper, then by name
        order = sorted(usable, key=lambda n: (sourcing.rank_key(fits[n]), cost_rank(n), n))
        allowed = [n for n in order if not fits[n]["excluded"]]
        mcps = sorted((c["name"] for c in s["candidates"] if c["kind"] == "mcp"), key=lambda n: (sourcing.rank_key(fits[n]), n))
        best_mcp = next((n for n in mcps if not fits[n]["excluded"] or
                         all(c["id"] == "installable" for c in fits[n]["criteria"] if c["hard"] and c["verdict"] == "fails")), None)

        def line(n):
            f = fits[n]
            return (f"- {n} (id {connector_slug(n)}, {sourcing.kind_label(next(c['kind'] for c in s['candidates'] if c['name'] == n))}): "
                    + "; ".join(f"{c['label']}: {c['verdict']} ({c['why']})" for c in f["criteria"])
                    + ("; RULED OUT: " + "; ".join(f["exclusion_reasons"]) if f["excluded"] else ""))

        default = allowed[0] if allowed else None
        choice, note, justification, rec = (connector_slug(default) if default else None), "", None, None
        if not allowed:
            note = ("None of the APIs found fits the brief: " + " | ".join(f"{n}: {'; '.join(fits[n]['exclusion_reasons'])}" for n in order)
                    if order else "No API could be connected; see the MCP servers and the reasons below.")
        if usable or mcps:
            rec = self._ask(Recommendation, (
                f"NEED: {s['need']}\nHow each candidate fits the person's brief (worked out by code from quoted facts):\n"
                + "\n".join(line(n) for n in [*order, *mcps]) +
                f"\n\nRecommend one API by connector id from: {[connector_slug(n) for n in allowed] or 'none'}. Explain the trade-offs "
                "honestly in plain language, including what is unverified. A paid or unknown-cost API over a verified-free one needs "
                "paid_justification."), s)
        if rec and allowed:
            ids = {connector_slug(n): n for n in allowed}
            wanted = ids.get(rec.choice) or ids.get(connector_slug(rec.choice))
            if wanted and wanted != default:
                better_free_exists = usable[default]["is_free"] is True and usable[wanted]["is_free"] is not True
                if better_free_exists and not rec.paid_justification:
                    note = f"The model preferred {wanted}, but gave no reason for choosing it over the free option; keeping {default}."
                else:
                    choice, justification = connector_slug(wanted), rec.paid_justification
        if choice == (connector_slug(default) if default else None):
            justification = None   # nothing to justify: this is the code's best fit, not a departure from it
        reasoning = rec.reasoning if rec else "No model opinion available; ranked by fit to the brief, then cost."
        recommendation = {"choice": choice, "reasoning": reasoning, "paid_justification": justification, "note": note,
                          "order": [connector_slug(n) for n in order], "best_mcp": connector_slug(best_mcp) if best_mcp else None,
                          "fits": {connector_slug(n): f for n, f in fits.items()}}
        self._event(s, "recommended", {"choice": choice, "best_mcp": recommendation["best_mcp"]})
        if not usable:
            return {"recommendation": recommendation,
                    "outcome": "no API could be connected" + (f"; MCP server found: {best_mcp} (not installable yet)" if best_mcp else "")}
        return {"recommendation": recommendation}

    def _gate_payload(self, s: State) -> dict:
        candidates = []
        for cand in s["candidates"]:
            name, res, draft = cand["name"], s["research"][cand["name"]], s["drafts"][cand["name"]]
            fit = (s["recommendation"].get("fits") or {}).get(connector_slug(name))
            item = {"name": name, "connector_id": connector_slug(name), "kind": cand["kind"], "kind_label": sourcing.kind_label(cand["kind"]),
                    "docs_url": cand.get("docs_url"), "found_in": cand.get("found_in"), "description": cand.get("description"),
                    "registry": cand.get("registry"), "why_considered": cand["reason"],
                    "verified_facts": tag_sources(res["evidence"], cand.get("docs_url") or ""),
                    "unverified": res["unverified"], "facts_rejected": len(res.get("rejected", [])), "fit": fit}
            if "manifest" in draft:
                m = Manifest.model_validate(draft["manifest"])
                warnings = []
                item["verified_facts"] = tag_sources(res["evidence"], cand.get("docs_url") or "", m.host)
                outside = [f for f in item["verified_facts"] if not f["official"]]
                if outside:
                    sites = sorted({site_of(f["source_url"]) for f in outside})
                    warnings.append(f"{len(outside)} of {len(item['verified_facts'])} verified facts are quoted from sites that are "
                                    f"not the provider's own ({', '.join(sites)}): the quotes are real, but treat them as secondary.")
                if draft.get("manifest_source") == "provided":
                    warnings.append("The endpoints were written by hand, not read from the docs: check them against the provider's documentation.")
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
                  (f'Reply with {{"approve": "{choice}", "terms_ok": true}} to integrate, or {{"approve": null}} to cancel' if choice
                   else 'No API fits the brief: reply with {"approve": null} to close, or approve a listed API anyway')]
        recommendation = {k: v for k, v in s["recommendation"].items() if k != "fits"}       # the fits travel with each candidate
        return {"type": "capability_gate", "need": s["need"], "brief": s.get("brief"), "recommendation": recommendation,
                "criteria": [{k: c[k] for k in ("id", "label", "question", "hard")} for c in sourcing.ontology()["criteria"]],
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
            detail = {"reason": outcome or "no integrable candidate"}
            if s.get("recommendation") and "cancelled" not in outcome and s.get("research"):
                # what was found stays with the request: each candidate, its kind, its fit, why it could not be connected
                detail["findings"] = self._gate_payload(s)["candidates"]
                detail["best_mcp"] = s["recommendation"].get("best_mcp")
            self._event(s, "cancelled" if "cancelled" in outcome else "failed", detail)
        return {}

    # -- after the run: the conversation on a request (skills/advisor) --------------------------------------

    def retest(self, request_id: str) -> dict:
        """Run the tests again on the version approved earlier, after the person fixed what failed (added the key, waited out a
        rate limit). Nothing is redrafted and no new approval is assumed: the same manifest, the same checks."""
        req = self.registry.get_request(request_id)
        if req is None or req["status"] != "tests_failed":
            raise ValueError("only a request whose tests failed can be tested again")
        approved = next((e["detail"] for e in reversed(req["events"]) if e["event"] == "integrated"), None)
        if not approved:
            raise ValueError("this request has no approved connection to test")
        s = {"request_id": request_id, "connector_id": approved["connector_id"], "version": approved["version"],
             "parent_run_id": req["run_id"]}
        self._event(s, "retest", {"connector_id": s["connector_id"], "version": s["version"]})
        out = self.n_test(s)
        self.n_finish({**s, "outcome": out["outcome"]})
        return out

    def look_up(self, request_id: str, candidate: str, docs_url: str, question: str, known_urls: list[str] | None = None) -> dict:
        """Answer a question the request could not answer, from the provider's own site only: the pages of that site this request
        already read (`known_urls`), its documentation, and a search limited to its domain. The model picks the passage that
        answers, and code keeps the answer only if the quote is on the page. The model's one-line answer is shown as its
        reading of that quote, never as a fact on its own. A page that refuses or has nothing readable is reported, never
        worked around."""
        s = {"request_id": request_id, "parent_run_id": None}
        cfg = advisor.settings()["look_up"]
        check_public_url(docs_url)
        host = urlparse(docs_url).hostname or ""
        domain = ".".join(host.split(".")[-2:])
        own = lambda u: (urlparse(u).hostname or "").endswith(domain)      # noqa: E731
        # a search engine reads a whole question as words to match ('can' found dictionaries): the question's key words only
        words = [w for w in sourcing._words(question) if w not in _name_words(candidate)][:6]
        try:
            hits = self.search_fn(f"site:{domain} {candidate} {' '.join(words)}", max_results=8)["results"]
        except Exception:
            hits = []
        urls = [u for u in (known_urls or []) if own(u)] + [docs_url] + [h["url"] for h in hits if own(h["url"])]
        pages, seen, unread = [], set(), []
        for url in urls:
            if len(pages) >= cfg["max_pages"]:
                break
            if url in seen:
                continue
            seen.add(url)
            if page := self._fetch_page(s, url):
                pages.append(page)
            else:
                unread.append(url)
        result = {"candidate": candidate, "question": question, "found": False, "pages": [p["url"] for p in pages], "unread": unread}
        if pages:
            terms = set(sourcing._words(question)) | {"individual", "organisation", "organization", "register", "terms"} & set(question.lower().split())
            passages = [(i, p, w) for i, p in enumerate(pages, 1) for w in best_windows(p["text"], terms or REQUIREMENT_TERMS, 2, cfg["window_chars"])]
            numbered = {n: (p, w) for n, (_, p, w) in enumerate(passages, 1)}
            listing = "\n\n".join(f"[{n}] {p['url']}\n{w}" for n, (p, w) in numbered.items())
            got = self._ask(LookUp, (
                f"QUESTION about {candidate}: {question}\n\nFrom the numbered passages (from {candidate}'s own website), copy EXACTLY "
                "the words that answer the question into `quote`, and give the passage number. If no passage answers it, set "
                f"found to false. Do not answer from memory.\n\n{listing}"), s)
            if got and got.found and got.passage in numbered:
                page = numbered[got.passage][0]
                if verify_quote(page["text"], got.quote)["ok"]:
                    result.update(found=True, quote=normalize(got.quote), source_url=page["url"], answer=got.answer.strip()[:300])
                else:
                    result["rejected"] = "the model's quote is not on the page"
        self._event(s, "looked_up", {k: v for k, v in result.items() if k != "question"} | {"question": question[:300]})
        return result

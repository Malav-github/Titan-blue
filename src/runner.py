"""Research runner: a LangGraph pipeline that turns a question into verified, support-checked claims.

    plan -> approve -> discover -> select -> fetch -> extract -> check -> assess -> (loop | report)

The LLM judges at specific steps (plan, pick sources, extract quoted facts, check support, judge
coverage, summarise); code does everything that must be exact: searching, fetching and archiving,
quote verification, budgets, and validating what the model returns. Each step appends to the run
event log, state is checkpointed to SQLite so a run can pause and resume (even from a new process),
and Langfuse traces are tagged with the run id.
"""

import re
import sqlite3
import uuid
from pathlib import Path
from typing import ClassVar, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError, model_validator

import tracing
from config import DATA_DIR, LLM_MODE
from evidence import MIN_QUOTE_CHARS, EvidenceStore, normalize, widen_quote
from runs import RunStore
from connectors.caller import ConnectorError, call_api
from connectors.registry import ConnectorRegistry
from support import check_claim
from tools import primary
from tools.search import search as default_search
from tools.search.fusion import normalize_url
from tools.web import fetch_url as default_fetch

CHECKPOINT_PATH = str(DATA_DIR / "checkpoints.db")
CATEGORY_NAMES = tuple(name for name, _ in primary.CATEGORIES)
CHUNK_CHARS, CHUNK_OVERLAP = 4500, 300
MIN_READABLE_CHARS = 200

DEFAULT_BUDGETS = {
    "max_iterations": 2,        # discover -> ... -> assess rounds
    "max_searches": 6,
    "max_fetches": 8,
    "max_fetches_per_iter": 5,
    "max_chunks_per_source": 3,
    "max_queries_per_iter": 3,
    "max_api_calls": 6,         # calls to approved connectors per run
}


# --- What the model is asked to return -------------------------------------------------------------

class Plan(BaseModel):
    subject: str = Field(description="The company, product or technology the question is about")
    company: str | None = Field(None, description="Official company name if its own website matters, else null")
    page_categories: list[Literal[CATEGORY_NAMES]] = Field(default_factory=list)
    queries: list[str] = Field(description="Distinct web search queries")
    checklist: list[str] = Field(description="3 to 6 specific facts that would fully answer the question")
    needs_data_source: str | None = Field(
        None, description="If answering needs structured data from a particular kind of source that ordinary web pages "
                          "cannot give (e.g. patent family records, SEC filings), name it; otherwise null")


class ApiCall(BaseModel):
    connector: str
    endpoint: str
    params: dict[str, str] = Field(default_factory=dict)


class ApiPlan(BaseModel):
    calls: list[ApiCall] = Field(default_factory=list, description="Empty if no connector helps")


class Selection(BaseModel):
    numbers: list[int] = Field(description="Numbers of the most useful pages, best first")


class ExtractedClaim(BaseModel):
    predicate: str = Field(description="Short dotted name for the fact, e.g. patent.assignee")
    value: str = Field(description="The specific value, copied from the passage")
    claim: str = Field(description="One self-contained sentence stating the fact")
    quote: str = Field(description="Exact words copied from the passage that state the fact (20-300 characters)")
    needed_fact: int = Field(description="Number of the NEEDED FACT this claim helps establish (1-based)")


class Extraction(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)
    _dropped: ClassVar[int] = 0      # malformed claims dropped since the last take_dropped() (telemetry only)
    _examples: ClassVar[list] = []   # what was dropped, so a dropped claim is never invisible

    @model_validator(mode="before")
    @classmethod
    def _keep_well_formed(cls, data):
        """One malformed claim (say, a missing field) must not cost the whole batch, as it did on a real patent page.
        The malformed claim is dropped, never repaired or guessed; the rest still face every later check."""
        if isinstance(data, dict) and isinstance(data.get("claims"), list):
            good = []
            for item in data["claims"]:
                try:
                    good.append(ExtractedClaim.model_validate(item))
                except ValidationError as exc:
                    cls._dropped += 1
                    if len(cls._examples) < 3:
                        cls._examples.append({"claim": str(item)[:300], "problem": "; ".join(
                            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:2])})
            data = {**data, "claims": good}
        return data

    @classmethod
    def take_dropped(cls) -> int:
        n, cls._dropped = cls._dropped, 0
        return n

    @classmethod
    def take_dropped_examples(cls) -> list:
        ex, cls._examples = cls._examples, []
        return ex


class FollowUps(BaseModel):
    new_queries: list[str] = Field(default_factory=list, description="Search queries aimed at the missing facts")


class Relevance(BaseModel):
    # `reason` comes first on purpose: a model fills the fields in order, and one that has to commit to true/false
    # before explaining contradicted itself ("directly provides the needed fact" -> addresses: false).
    reason: str = Field(description="One sentence: what the claim states, and whether that is the needed fact or a direct part of it")
    addresses: bool = Field(description="True only if the claim itself states the needed fact (or a direct part of it); must agree with the reason")


NON_FACT = re.compile(r"^\s*(the\s+)?(passage|text|quote|excerpt|page|document|article|source)\b|^\s*quote\s*:|"
                      r"\bdoes not (provide|mention|state|specify)\b", re.I)


class Summary(BaseModel):
    text: str = Field(description="Short summary; cite claims as [claim:ID]")


class State(TypedDict, total=False):
    run_id: str
    question: str
    subject: str
    company: str | None
    entity_name: str
    entity_id: int
    domain: str | None
    page_categories: list[str]
    checklist: list[str]
    queries: list[str]
    candidates: list[dict]
    selected: list[dict]
    new_sources: list[str]
    new_claim_ids: list[int]
    all_claim_ids: list[int]
    verdicts: dict
    relevant: dict
    claim_items: dict
    allow_capabilities: bool
    data_need: str | None
    capability_checked: bool
    capability_id: str | None
    fetch_failures: int
    api_calls_used: int
    covered: list[int]
    fetched_keys: list[str]
    iteration: int
    searches_used: int
    fetches_used: int
    rejected: int
    budgets: dict
    require_approval: bool
    approved: bool
    warnings: list[str]
    report: dict


def extraction_prompt(question: str, needed: str, page_url: str, chunk: str) -> str:
    """`needed` is the numbered list of facts wanted. Shared with the model eval so it tests what the pipeline sends."""
    return (
        f"QUESTION: {question}\nNEEDED FACTS:\n{needed}\nPAGE: {page_url}\n\n"
        f"PASSAGE:\n{chunk}\n\n"
        "Extract up to 5 facts from the passage, but ONLY facts that establish one of the NEEDED FACTS "
        "(give its number in `needed_fact`). Skip headings, marketing language and anything off-topic. "
        "`claim` is ONE complete sentence in your own words stating the fact (for example: \"The plant's annual "
        "capacity is 50,000 metric tons.\"); never paste the quote into it and never start it with \"quote:\". "
        "`quote` must be copied EXACTLY from the passage (20-300 characters, no paraphrase, no fixing "
        "typos, no ellipses). State only what the passage says; do not add facts or links between facts "
        "that it does not state. If nothing in the passage establishes a needed fact, return no claims."
    )


def relevance_prompt(question: str, fact: str, claim_text: str) -> str:
    return (
        f"QUESTION: {question}\nNEEDED FACT: {fact}\n\nCLAIM: {claim_text}\n"
        "Does the claim itself state the needed fact (or a direct part of it)? Answer false for claims that "
        "are merely related, generic, about something else, or about the document rather than the fact. "
        "A value written as a code or in a compact format (0926 for a September 26 date, 3571 for an industry code, "
        "a year, an id) DOES state the fact when the claim gives it for the thing the fact asks about; do not require "
        "it to be spelled out in words. But an identifier of the document itself (its arXiv id, URL or catalogue "
        "number) is about the document, not the fact, unless the fact asks for that identifier."
    )


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = normalize(item).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(normalize(item))
    return out


def invented_values(params: dict | None, *texts: str) -> list[str]:
    """Identifier-like parameter values (anything with a digit: an id, a CIK, a year) that appear nowhere in the question
    or checklist. A model that was not given an identifier makes one up (a real run used CIK 0000096660 for Dropbox and
    OpenAlex id W123456789), and a made-up id can fetch a real but unrelated record. Leading zeros do not matter."""
    haystack = normalize(" ".join(texts)).lower()
    bad = []
    for value in (params or {}).values():
        v = normalize(str(value)).lower()
        if not any(ch.isdigit() for ch in v):
            continue
        stripped = v.lstrip("0")                         # "0000320193" is "320193"; "0000000000" is nothing at all
        if v not in haystack and not (stripped and stripped in haystack):
            bad.append(str(value))
    return bad


def _terms(*texts: str) -> set[str]:
    return {w for t in texts for w in re.findall(r"[a-z0-9]{4,}", t.lower())}


def top_chunks(text: str, terms: set[str], k: int) -> list[str]:
    """Overlapping windows of a page, ranked by overlap with the question's terms. Cheap and deterministic."""
    step = CHUNK_CHARS - CHUNK_OVERLAP
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, max(len(text), 1), step)]
    scored = sorted(enumerate(chunks), key=lambda ic: (-len(terms & _terms(ic[1])), ic[0]))
    return [c for _, c in sorted(scored[:k])]  # best k, kept in page order


class Runner:
    def __init__(self, store: EvidenceStore | None = None, runs: RunStore | None = None,
                 checkpoint_path: str | None = None, llm_factory=None, search_fn=None, fetch_fn=None,
                 render_fn=None, registry=None, call_api_fn=None):
        self.store = store or EvidenceStore()
        self.runs = runs or RunStore()
        self.registry = registry or ConnectorRegistry()
        self.call_api_fn = call_api_fn or call_api
        self.search_fn = search_fn or default_search
        self.fetch_fn = fetch_fn or default_fetch
        self.render_fn = render_fn or self._default_render
        if llm_factory is None:
            from llm import get_chat_model
            llm_factory = lambda task: get_chat_model(task=task, temperature=0)  # noqa: E731
        self.llm_factory = llm_factory
        conn = sqlite3.connect(checkpoint_path or CHECKPOINT_PATH, check_same_thread=False)
        self.graph = self._build(SqliteSaver(conn))

    @staticmethod
    def _default_render(url: str, max_chars: int = 2000) -> dict:
        from tools.browser import render_url  # imported lazily: starts Playwright only when needed
        return render_url(url, max_chars=max_chars)

    # -- public API ------------------------------------------------------------------------------

    def start(self, question: str, subject: str | None = None, require_approval: bool = False,
              pause_after: list[str] | None = None, budgets: dict | None = None,
              allow_capabilities: bool = False, run_id: str | None = None) -> dict:
        """`allow_capabilities`: let the run pause and ask for a new data source (API) when ordinary pages are not enough."""
        run_id = run_id or uuid.uuid4().hex     # a caller (the API) may pick the id so it can hand it back at once
        merged = {**DEFAULT_BUDGETS, **(budgets or {})}
        self.runs.emit(run_id, "runner", "run_started", {
            "question": question, "subject": subject, "require_approval": require_approval,
            "budgets": merged, "llm_mode": LLM_MODE, "pause_after": pause_after or [],
        })
        state: State = {
            "run_id": run_id, "question": question, "require_approval": require_approval, "budgets": merged,
            "iteration": 0, "searches_used": 0, "fetches_used": 0, "rejected": 0, "fetched_keys": [],
            "all_claim_ids": [], "verdicts": {}, "relevant": {}, "claim_items": {}, "covered": [], "warnings": [], "new_sources": [],
            "new_claim_ids": [], "candidates": [], "selected": [], "approved": False,
            "allow_capabilities": allow_capabilities, "capability_checked": False, "capability_id": None,
            "fetch_failures": 0, "api_calls_used": 0, "data_need": None,
        }
        if subject:
            state["subject"] = subject
        return self._drive(run_id, state, pause_after)

    def resume(self, run_id: str, decision: dict | None = None, pause_after: list[str] | None = None) -> dict:
        """Continue a paused, awaiting-approval, or failed run. `decision` answers an approval request:
        {"approved": bool, "queries": [...], "checklist": [...]} (the last two optional edits)."""
        self.runs.emit(run_id, "runner", "resumed", {"decision": decision})
        return self._drive(run_id, Command(resume=decision) if decision is not None else None, pause_after)

    def snapshot(self, run_id: str) -> dict:
        snap = self.graph.get_state(self._config(run_id))
        return {"next": list(snap.next), "values": snap.values}

    # -- driving the graph -----------------------------------------------------------------------

    def _config(self, run_id: str) -> dict:
        return {
            "configurable": {"thread_id": run_id}, "callbacks": tracing.callbacks(),
            "run_name": f"titan-run-{run_id[:8]}", "recursion_limit": 80,
            "metadata": {"langfuse_session_id": run_id, "langfuse_tags": ["titan"]},
        }

    def _drive(self, run_id: str, input_, pause_after) -> dict:
        config = self._config(run_id)
        kwargs = {"interrupt_after": pause_after} if pause_after else {}
        try:
            for _ in self.graph.stream(input_, config, stream_mode="updates", **kwargs):
                pass
        except Exception as exc:  # the checkpoint survives: resume() retries from the failed step
            self.runs.emit(run_id, "runner", "run_failed", {"error": f"{type(exc).__name__}: {exc}"})
            tracing.flush()
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

        snap = self.graph.get_state(config)
        asks = [i.value for t in snap.tasks for i in t.interrupts]
        capability = None
        if asks and asks[0].get("type") == "capability_needed":
            request_id = self.registry.create_request(run_id, asks[0]["need"], asks[0]["reason"])
            capability = {"request_id": request_id, "need": asks[0]["need"], "reason": asks[0]["reason"]}
            self.runs.emit(run_id, "runner", "capability_needed", capability)
        elif asks:
            self.runs.emit(run_id, "runner", "awaiting_approval", asks[0])
        elif snap.next:
            self.runs.emit(run_id, "runner", "paused", {"next": list(snap.next)})
        elif snap.values.get("approved") is False:
            self.runs.emit(run_id, "runner", "run_stopped", {"reason": "plan rejected"})
        else:
            report = snap.values.get("report", {})
            self.runs.emit(run_id, "runner", "run_finished", {
                "established": len(report.get("established", [])), "partial": len(report.get("partial", [])),
                "gaps": len(report.get("gaps", [])), "rejected_quotes": report.get("rejected_quotes", 0)})
        tracing.flush()
        return {"run_id": run_id, "status": self.runs.status(run_id), "report": snap.values.get("report"),
                "approval_request": asks[0] if asks and not capability else None, "capability_request": capability,
                "next": list(snap.next)}

    # -- plumbing --------------------------------------------------------------------------------

    def _ask(self, task: str, schema, prompt: str, run_id: str, node: str):
        """Structured LLM call, one retry. Returns None on failure; the caller falls back, and it is logged."""
        last = None
        for _ in range(2):
            try:
                return self.llm_factory(task).with_structured_output(schema).invoke(prompt)
            except Exception as exc:
                last = exc
        self.runs.emit(run_id, node, "llm_failed", {"task": task, "schema": schema.__name__,
                                                    "error": f"{type(last).__name__}: {str(last)[:200]}"})
        return None

    def _build(self, checkpointer):
        g = StateGraph(State)
        for name in ("plan", "approve", "discover", "select", "fetch", "api_lookup", "extract", "check", "assess",
                     "capability", "report"):
            g.add_node(name, getattr(self, f"n_{name}"))
        g.add_edge(START, "plan")
        g.add_edge("plan", "approve")
        g.add_conditional_edges("approve", lambda s: "discover" if s.get("approved") else END)
        for a, b in (("discover", "select"), ("select", "fetch"), ("fetch", "api_lookup"), ("api_lookup", "extract"),
                     ("extract", "check"), ("check", "assess")):
            g.add_edge(a, b)
        g.add_conditional_edges("assess", self._more_or_report)
        g.add_conditional_edges("capability", lambda s: "discover" if s.get("capability_id") else "report")
        g.add_edge("report", END)
        return g.compile(checkpointer=checkpointer)

    @staticmethod
    def _more_or_report(s: State) -> str:
        b = s["budgets"]
        gaps = [i for i in range(len(s["checklist"])) if i not in s["covered"]]
        room = (s["iteration"] < b["max_iterations"] and s["searches_used"] < b["max_searches"]
                and s["fetches_used"] < b["max_fetches"])
        attempts = s["fetches_used"]
        blocked = attempts >= 3 and s["fetch_failures"] * 2 >= attempts  # most pages were unusable
        if gaps and s.get("allow_capabilities") and not s.get("capability_checked") and (s.get("data_need") or blocked):
            return "capability"
        return "discover" if gaps and s["queries"] and room else "report"

    # -- nodes -----------------------------------------------------------------------------------

    def n_plan(self, s: State) -> dict:
        rid, b = s["run_id"], s["budgets"]
        prompt = (
            "You plan web research that must be backed by quoted evidence.\n"
            f"QUESTION: {s['question']}\n\n"
            f"Return: subject (what the question is about); company (the official company name only if the "
            f"company's own website matters, else null); page_categories (company pages worth reading, from "
            f"{list(CATEGORY_NAMES)}; empty if no company); queries (up to {b['max_queries_per_iter']} distinct "
            f"web search queries using exact names and distinguishing terms; search operators work, e.g. "
            f"site:patents.google.com, site:sec.gov, or a company's own domain); checklist (as few facts as fully "
            f"answer the question, at most 6; one is right for a one-fact question). Write each checklist item as a short "
            f"label for a fact a web page could state directly (a name, number, date or place), for example 'Year Dropbox was "
            f"founded' or 'Legal name of the company with CIK 0000320193'. Never write the answer or any value, even if you "
            f"think you know it: your guess may be wrong and would mislead the search. Never add steps such as verifying, "
            f"cross-checking or confirming sources; the system does that itself."
        )
        plan = self._ask("plan", Plan, prompt, rid, "plan")
        warnings = list(s.get("warnings", []))
        if plan is None:
            warnings.append("planner failed: using the question itself as the only query and checklist item")
            plan = Plan(subject=s["question"][:60], queries=[s["question"]], checklist=[s["question"]])
        subject = s.get("subject") or plan.subject
        entity_name = plan.company or subject
        queries = _dedupe(plan.queries)[:b["max_queries_per_iter"]] or [s["question"]]
        checklist = _dedupe(plan.checklist)[:6] or [s["question"]]
        categories = [c for c in plan.page_categories if plan.company]
        entity_id = self.store.add_entity(entity_name, kind="subject")
        self.runs.emit(rid, "plan", "plan_ready", {"subject": subject, "company": plan.company,
                       "page_categories": categories, "queries": queries, "checklist": checklist})
        return {"subject": subject, "company": plan.company, "entity_name": entity_name, "entity_id": entity_id,
                "page_categories": categories, "queries": queries, "checklist": checklist, "warnings": warnings,
                "data_need": plan.needs_data_source}

    def n_approve(self, s: State) -> dict:
        if not s.get("require_approval") or s.get("approved"):
            return {"approved": True}
        decision = interrupt({"question": s["question"], "subject": s["subject"], "queries": s["queries"],
                              "checklist": s["checklist"], "page_categories": s["page_categories"]}) or {}
        if not decision.get("approved", True):
            return {"approved": False}
        update = {"approved": True}
        if decision.get("queries"):
            update["queries"] = _dedupe(decision["queries"])[:s["budgets"]["max_queries_per_iter"]]
        if decision.get("checklist"):
            update["checklist"] = _dedupe(decision["checklist"])[:6]
        self.runs.emit(s["run_id"], "approve", "approved", {k: v for k, v in update.items() if k != "approved"})
        return update

    def n_discover(self, s: State) -> dict:
        rid, b = s["run_id"], s["budgets"]
        found: dict[str, dict] = {}
        warnings = list(s.get("warnings", []))
        domain, entity_id = s.get("domain"), s["entity_id"]

        if s["iteration"] == 0 and s.get("company") and s["page_categories"] and not domain:
            try:
                resolution = primary.resolve_domain(s["company"])
                self.store.register_resolution(resolution)
                confirmed = [c for c in resolution["candidates"] if c["status"] == "confirmed"]
                statuses = [f"{c['domain']}={c['status']}" for c in resolution["candidates"]]
                if confirmed:
                    domain = confirmed[0]["domain"]
                    entity_id = self.store.find_entity(s["entity_name"])["entity_id"]
                    pages = primary.discover_pages(domain, per_category=3)
                    for p in pages["pages"]:
                        if p["category"] in s["page_categories"]:
                            found[normalize_url(p["url"])] = {"url": p["url"], "title": f"[{p['category']}] {p['url']}",
                                                              "snippet": "", "score": 1.0, "tier": "primary"}
                    self.runs.emit(rid, "discover", "primary_pages", {"domain": domain, "pages": len(found),
                                   "categories_not_found": pages["categories_not_found"]})
                else:
                    warnings.append(f"no confirmed official domain for {s['company']} ({', '.join(statuses) or 'none found'})")
                    self.runs.emit(rid, "discover", "domain_unconfirmed", {"candidates": statuses})
            except Exception as exc:
                warnings.append(f"primary-source discovery failed: {type(exc).__name__}: {exc}")
                self.runs.emit(rid, "discover", "primary_failed", {"error": f"{type(exc).__name__}: {exc}"})

        searches = s["searches_used"]
        for query in s["queries"]:
            if searches >= b["max_searches"]:
                self.runs.emit(rid, "discover", "budget_reached", {"budget": "max_searches"})
                break
            result = self.search_fn(query, max_results=8)
            searches += 1
            for hit in result["results"]:
                key = normalize_url(hit["url"])
                official = bool(domain) and primary.is_official(hit["url"], [domain])
                known = found.get(key)
                if known is None or (known["tier"] != "primary" and official):
                    found[key] = {"url": hit["url"], "title": hit["title"], "snippet": hit["snippet"],
                                  "score": hit["score"], "tier": "primary" if official else "secondary"}
            self.runs.emit(rid, "discover", "search_done", {
                "query": query, "results": len(result["results"]), "search_id": result["search_id"],
                "providers": result["providers"], "warnings": result["warnings"]})
            warnings += [f"search '{query}': {w}" for w in result["warnings"]]

        fetched = set(s["fetched_keys"])
        ranked = sorted((c for k, c in found.items() if k not in fetched),
                        key=lambda c: (c["tier"] != "primary", -c["score"]))[:25]
        return {"candidates": ranked, "searches_used": searches, "domain": domain, "entity_id": entity_id,
                "warnings": warnings}

    def n_select(self, s: State) -> dict:
        rid, b = s["run_id"], s["budgets"]
        n = min(b["max_fetches_per_iter"], b["max_fetches"] - s["fetches_used"])
        cands = s["candidates"]
        if n <= 0 or not cands:
            self.runs.emit(rid, "select", "sources_selected", {"urls": [], "reason": "nothing to fetch or budget spent"})
            return {"selected": []}
        shown = cands[:15]
        listing = "\n".join(f"{i}. [{c['tier']}] {c['title'][:90]} - {c['url']}\n   {c['snippet'][:160]!r}"
                            for i, c in enumerate(shown, 1))
        prompt = (f"QUESTION: {s['question']}\nFACTS NEEDED: {s['checklist']}\n\nCandidate pages:\n{listing}\n\n"
                  f"Choose up to {n} pages most likely to state the needed facts. Prefer the organisation's own "
                  f"pages and primary documents (patent records, filings) over commentary. Return their numbers, best first.")
        choice = self._ask("select", Selection, prompt, rid, "select")
        picked = [shown[i - 1] for i in dict.fromkeys(choice.numbers if choice else []) if 1 <= i <= len(shown)][:n]
        used_llm = bool(picked)
        if not picked:
            picked = cands[:n]
        self.runs.emit(rid, "select", "sources_selected", {"urls": [c["url"] for c in picked], "model_choice": used_llm})
        return {"selected": picked}

    def n_fetch(self, s: State) -> dict:
        rid = s["run_id"]
        new_sources, fetches, keys = [], s["fetches_used"], list(s["fetched_keys"])
        warnings = list(s.get("warnings", []))
        for cand in s["selected"]:
            url = cand["url"]
            keys.append(normalize_url(url))
            fetches += 1
            try:
                result = self.fetch_fn(url, max_chars=2000)
                if result["status"] != 200:  # blocked or missing: not retried with other tools
                    raise RuntimeError(f"HTTP {result['status']}")
                sha = self.store.ingest(result)
                text = Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
                if len(text.strip()) < MIN_READABLE_CHARS and "html" in result.get("content_type", ""):
                    # The page answered but has no text without JavaScript: render it in the browser
                    result = self.render_fn(url, max_chars=2000)
                    sha = self.store.ingest(result)
                    text = Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
                    self.runs.emit(rid, "fetch", "source_rendered", {"url": url, "readable_chars": len(text.strip())})
                if len(text.strip()) < MIN_READABLE_CHARS:
                    raise RuntimeError(f"only {len(text.strip())} readable characters (scanned PDF or empty page)")
            except Exception as exc:
                warnings.append(f"could not use {url}: {exc}")
                self.runs.emit(rid, "fetch", "source_failed", {"url": url, "error": f"{type(exc).__name__}: {exc}"})
                continue
            new_sources.append(sha)
            self.runs.emit(rid, "fetch", "source_fetched", {"url": url, "source_sha256": sha, "tier": cand["tier"],
                           "readable_chars": len(text)})
        failed = len(s["selected"]) - len(new_sources)
        return {"new_sources": new_sources, "fetches_used": fetches, "fetched_keys": keys, "warnings": warnings,
                "fetch_failures": s["fetch_failures"] + failed}

    def n_api_lookup(self, s: State) -> dict:
        """Ask the model which approved connector calls would help, then make them through the guarded path."""
        rid, b = s["run_id"], s["budgets"]
        active = self.registry.list_active()
        room = b["max_api_calls"] - s["api_calls_used"]
        if not active or room <= 0:
            return {}
        docs = "\n".join(
            f"{m.connector_id}: {m.description}\n" + "\n".join(
                f"  - {e.name}({', '.join(p.name + ('*' if p.required else '') for p in e.params)}): {e.description}"
                for e in m.endpoints) for _, m in active)
        plan = self._ask("api_plan", ApiPlan, (
            f"QUESTION: {s['question']}\nNEEDED FACTS: {s['checklist']}\n\nApproved data sources (* = required parameter):\n{docs}\n\n"
            f"Choose up to {min(room, 3)} calls that would help establish the needed facts. Use only the listed connectors, "
            "endpoints and parameters, with concrete values taken from the question. Return no calls if none help."), rid, "api_lookup")
        new_sources, used = list(s["new_sources"]), s["api_calls_used"]
        for call in (plan.calls if plan else [])[:min(room, 3)]:
            invented = invented_values(call.params, s["question"], " ".join(s["checklist"]))
            if invented:                                   # never spend a call (or trust a record) on a guessed identifier
                self.runs.emit(rid, "api_lookup", "api_skipped", {
                    "connector": call.connector, "endpoint": call.endpoint, "params": call.params,
                    "reason": f"{', '.join(invented)} is not in the question; identifiers are never guessed"})
                continue
            used += 1
            try:
                result = self.call_api_fn(self.registry, call.connector, call.endpoint, call.params, run_id=rid)
                if not result["ok"]:
                    raise ConnectorError(f"HTTP {result['status']}: {result['text'][:100]}")
                sha = self.store.ingest(result)
                if len(Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8").strip()) < 20:
                    raise ConnectorError("empty response")
            except Exception as exc:
                self.runs.emit(rid, "api_lookup", "api_failed", {"connector": call.connector, "endpoint": call.endpoint,
                                                                 "params": call.params, "error": str(exc)[:200]})
                continue
            new_sources.append(sha)
            self.runs.emit(rid, "api_lookup", "api_called", {
                "connector": call.connector, "endpoint": call.endpoint, "params": call.params, "source_sha256": sha,
                "source_tier": result.get("source_tier"), "url": result["url"]})
        return {"new_sources": new_sources, "api_calls_used": used}

    def n_capability(self, s: State) -> dict:
        """Pause: this run needs a data source it does not have. The capability manager (a person in the loop) provides it."""
        gaps = [t for i, t in enumerate(s["checklist"]) if i not in s["covered"]]
        need = s.get("data_need") or f"A reliable source for: {s['question']}"
        reason = (f"{s['fetch_failures']} of {s['fetches_used']} pages were unusable" if s["fetches_used"] else "no pages fetched") \
            + f"; still unestablished: {gaps}"
        decision = interrupt({"type": "capability_needed", "need": need, "reason": reason, "gaps": gaps}) or {}
        if not decision.get("capability"):
            return {"capability_checked": True, "capability_id": None}
        b = s["budgets"]  # a new source earns one more round and a few more fetches
        return {"capability_checked": True, "capability_id": decision["capability"],
                "budgets": {**b, "max_iterations": b["max_iterations"] + 1, "max_fetches": b["max_fetches"] + 3,
                            "max_searches": b["max_searches"] + 2},
                "queries": s["queries"] or [f"{s['subject']} {g}"[:120] for g in gaps[:b["max_queries_per_iter"]]]}

    def n_extract(self, s: State) -> dict:
        rid, b = s["run_id"], s["budgets"]
        terms = _terms(s["question"], " ".join(s["checklist"]))
        new_ids, rejected, seen = [], s["rejected"], set()
        items = dict(s["claim_items"])
        needed = "\n".join(f"{i}. {t}" for i, t in enumerate(s["checklist"], 1))
        for sha in s["new_sources"]:
            source = self.store.get_source(sha)
            text = normalize(Path(source["text_path"]).read_text(encoding="utf-8"))
            for chunk in top_chunks(text, terms, b["max_chunks_per_source"]):
                prompt = extraction_prompt(s["question"], needed, source["final_url"], chunk)
                Extraction.take_dropped(), Extraction.take_dropped_examples()
                extraction = self._ask("extract", Extraction, prompt, rid, "extract")
                if dropped := Extraction.take_dropped():
                    self.runs.emit(rid, "extract", "claims_malformed", {
                        "count": dropped, "source_url": source["final_url"], "examples": Extraction.take_dropped_examples()})
                for c in (extraction.claims if extraction else [])[:5]:
                    key = (sha, normalize(c.quote))
                    if key in seen:
                        continue
                    seen.add(key)
                    if len(c.claim.split()) < 4 or NON_FACT.search(c.claim):  # "No claims", headings, remarks about the passage
                        self.runs.emit(rid, "extract", "claim_rejected", {
                            "claim": c.claim, "quote": c.quote, "reason": "not a statement of a fact",
                            "source_url": source["final_url"]})
                        continue
                    if not 1 <= c.needed_fact <= len(s["checklist"]):
                        self.runs.emit(rid, "extract", "claim_rejected", {
                            "claim": c.claim, "quote": c.quote, "reason": "does not address any needed fact",
                            "source_url": source["final_url"]})
                        continue
                    # `value` is metadata the model often fills badly; blank it rather than lose a verifiable claim
                    value = c.value if normalize(c.value) and normalize(c.value) in normalize(f"{c.quote} {c.claim}") else ""
                    quote = c.quote
                    if len(normalize(quote)) < MIN_QUOTE_CHARS and (wider := widen_quote(text, quote)):
                        self.runs.emit(rid, "extract", "quote_widened", {"from": normalize(quote), "to": wider,
                                                                         "source_url": source["final_url"]})
                        quote = wider
                    r = self.store.add_claim(s["entity_id"], c.predicate, value, c.claim, sha, quote,
                                             created_by=f"runner:{rid[:8]}")
                    if r["ok"]:
                        new_ids.append(r["claim_id"])
                        items[str(r["claim_id"])] = c.needed_fact - 1
                        self.runs.emit(rid, "extract", "claim_recorded", {
                            "claim_id": r["claim_id"], "claim": c.claim, "value": value, "quote": normalize(c.quote),
                            "needed_fact": c.needed_fact,
                            "source_url": source["final_url"], "source_sha256": sha, "source_tier": r["source_tier"]})
                    else:
                        rejected += 1
                        self.runs.emit(rid, "extract", "claim_rejected", {
                            "claim": c.claim, "quote": c.quote, "reason": r["reason"],
                            "closest_passage": r.get("closest_passage"), "source_url": source["final_url"]})
        return {"new_claim_ids": new_ids, "all_claim_ids": s["all_claim_ids"] + new_ids, "rejected": rejected,
                "new_sources": [], "claim_items": items}

    def n_check(self, s: State) -> dict:
        rid = s["run_id"]
        verdicts, relevant = dict(s["verdicts"]), dict(s["relevant"])
        for cid in s["new_claim_ids"]:
            try:
                res = check_claim(self.store, cid, llm=self.llm_factory("claim_support"))
            except Exception as exc:
                self.runs.emit(rid, "check", "llm_failed", {"task": "claim_support", "claim_id": cid,
                                                           "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                continue
            verdicts[str(cid)] = res["verdict"]
            self.runs.emit(rid, "check", "support_checked", {
                "claim_id": cid, "verdict": res["verdict"], "reasoning": res["reasoning"],
                "unsupported_parts": res["unsupported_parts"], "missing_tokens": res["missing_tokens"]})
            if res["verdict"] != "supported":
                continue
            # A true claim can still be beside the point. Relevance is relative to this question, so it is
            # judged here and logged with the run, not stored on the claim.
            fact = s["checklist"][s["claim_items"][str(cid)]]
            claim = self.store.get_claim(cid)
            verdict = self._ask("relevance", Relevance, relevance_prompt(s["question"], fact, claim["claim_text"]),
                                rid, "check")
            relevant[str(cid)] = bool(verdict and verdict.addresses)
            self.runs.emit(rid, "check", "relevance_checked", {
                "claim_id": cid, "needed_fact": fact, "addresses": relevant[str(cid)],
                "reason": verdict.reason if verdict else "relevance check failed; treated as not relevant"})
        return {"verdicts": verdicts, "relevant": relevant, "new_claim_ids": []}

    def n_assess(self, s: State) -> dict:
        """Coverage is computed by code: a needed fact is covered once a supported claim is filed under it.
        The model is only asked what to search for next."""
        rid, b = s["run_id"], s["budgets"]
        covered = set(s["covered"]) | {
            s["claim_items"][c] for c, v in s["verdicts"].items()
            if v == "supported" and s["relevant"].get(c) and c in s["claim_items"]}
        gaps = [s["checklist"][i] for i in range(len(s["checklist"])) if i not in covered]
        queries: list[str] = []
        if gaps:
            known = "\n".join(f"- {self.store.get_claim(int(c))['claim_text']}"
                              for c, v in list(s["verdicts"].items())[:20] if v == "supported") or "(none yet)"
            missing = "\n".join(f"- {g}" for g in gaps)
            prompt = (f"QUESTION: {s['question']}\nALREADY ESTABLISHED:\n{known}\n\nSTILL MISSING:\n{missing}\n\n"
                      f"Suggest up to {b['max_queries_per_iter']} web search queries (exact names and terms) "
                      "that could find evidence for the missing facts.")
            follow = self._ask("assess", FollowUps, prompt, rid, "assess")
            queries = _dedupe(follow.new_queries)[:b["max_queries_per_iter"]] if follow else []
        if gaps and not queries:  # no usable follow-ups from the model: aim queries at the gaps themselves
            queries = [f"{s['subject']} {g}"[:120] for g in gaps[:b["max_queries_per_iter"]]]
        iteration = s["iteration"] + 1
        self.runs.emit(rid, "assess", "coverage_assessed", {
            "iteration": iteration, "covered": sorted(covered), "gaps": gaps, "next_queries": queries if gaps else []})
        return {"covered": sorted(covered), "queries": queries, "iteration": iteration}

    def n_report(self, s: State) -> dict:
        rid = s["run_id"]
        rows = {"supported": [], "partially_supported": [], "not_supported": [], "unchecked": [], "off_topic": []}
        for cid in s["all_claim_ids"]:
            claim = self.store.get_claim(cid)
            source = self.store.get_source(claim["source_sha256"])
            row = {"claim_id": cid, "claim": claim["claim_text"], "value": claim["value"], "quote": claim["quote"],
                   "source_url": source["final_url"], "source_sha256": claim["source_sha256"],
                   "source_tier": claim["source_tier"], "as_of": claim["as_of"]}
            checks = self.store.support_checks_for(cid)
            if checks:
                row["why"] = checks[-1]["reasoning"]
            verdict = s["verdicts"].get(str(cid), "unchecked")
            if verdict == "supported" and not s["relevant"].get(str(cid)):
                verdict = "off_topic"  # true to its source, but does not answer a needed fact
            rows[verdict].append(row)
        gaps = [t for i, t in enumerate(s["checklist"]) if i not in s["covered"]]

        summary, dropped = "", []
        allowed = {r["claim_id"] for r in rows["supported"]}
        if allowed:
            listing = "\n".join(f"[claim:{r['claim_id']}] {r['claim']}" for r in rows["supported"])
            prompt = (f"QUESTION: {s['question']}\n\nVERIFIED CLAIMS:\n{listing}\n\nWrite a short factual answer using ONLY "
                      f"these claims. Cite each statement as [claim:ID]. Say plainly what the claims do not establish. "
                      f"Do not add anything that is not in the claims.")
            draft = self._ask("synthesis", Summary, prompt, rid, "report")
            if draft:
                cited = {int(i) for i in re.findall(r"\[claim:(\d+)\]", draft.text)}
                dropped = sorted(cited - allowed)
                summary = re.sub(r"\[claim:(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in allowed else "[invalid citation removed]", draft.text)
        report = {
            "question": s["question"], "subject": s["subject"], "summary": summary, "invalid_citations_removed": dropped,
            "established": rows["supported"], "partial": rows["partially_supported"],
            "not_supported": len(rows["not_supported"]), "unchecked": len(rows["unchecked"]),
            "off_topic": len(rows["off_topic"]),
            "gaps": gaps, "rejected_quotes": s["rejected"], "warnings": s.get("warnings", []),
            "budget_used": {"searches": s["searches_used"], "fetches": s["fetches_used"], "iterations": s["iteration"]},
            "domain": s.get("domain"),
        }
        self.runs.emit(rid, "report", "report_ready", {k: (len(v) if isinstance(v, list) else v)
                                                       for k, v in report.items() if k != "summary"})
        return {"report": report}

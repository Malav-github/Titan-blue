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
from concurrent.futures import ThreadPoolExecutor
import uuid
from pathlib import Path
from typing import ClassVar, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError, model_validator

import tracing
from skills.temporal import clock as tclock, freshness, intent as tintent, setting as temporal_setting
from skills.temporal.display import short_date
from skills.temporal import periods as tperiods
from skills import planning
from skills.temporal.extract import DateFact, best_date, parse_date
import corroboration
import sources
from config import DATA_DIR, LLM_MODE
from evidence import MIN_QUOTE_CHARS, EvidenceStore, narrow_quote, normalize, widen_quote
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
    checklist: list[str] = Field(description="One short label per distinct fact the question itself asks for (1 to 6), nothing more")
    needs_data_source: str | None = Field(
        None, description="If answering needs structured data from a particular kind of source that ordinary web pages "
                          "cannot give (e.g. patent family records, SEC filings), name it; otherwise null")


class PremiseList(BaseModel):
    premise: list[str] = Field(default_factory=list, description=(
        "Specific things the question refers to as if they exist, each written as a plain statement. Empty if it names none."))


class SourceAdvice(BaseModel):
    site: str = Field(description="A website or domain worth looking at first, taken from the search results or the subject's own site")
    why: str = Field(description="One short sentence: what kind of source it is and why it ranks where it does")


class Advice(BaseModel):
    sources: list[SourceAdvice] = Field(default_factory=list, description="Up to 4 places to look first, best first")
    suggestion: str | None = Field(None, description=(
        "Only if the question names something the results do not show but a close match appears (the same document for another "
        "year, the same event under another name): one sentence asking whether that was meant. Otherwise null."))


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
    time_scope: dict           # what time frame the question is about (skills.temporal.intent.TimeScope.as_dict())
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
    ask_sources: bool          # pause and ask the person for a source when something cannot be established
    scout: list[dict]          # what a first quick search showed: context for the plan, never evidence
    brief: dict                # the planning skill's brief: kinds, ranked places, document types, target time
    advice: dict               # ranked places to look and an optional suggested correction, shown at the decision step
    premise: list[str]         # what the question takes for granted
    premise_status: str        # none | verified | unverified | overridden
    premise_missing: list[str]
    premise_claim_ids: list[int]
    premise_url: str | None    # a source the person gave for the premise, read on the next check
    corrections: list[str]     # the person's comments on the agent's reading of the question
    original_question: str
    route: str                 # where the approval step sends the run next
    sources_asked: int
    gate: str                  # what the gate decided: continue | read | stop
    outcome: str | None        # premise_unverified when the question's premise could not be established
    data_need: str | None
    capability_checked: bool
    capability_id: str | None
    fetch_failures: int
    api_calls_used: int
    covered: list[int]
    fetched_keys: list[str]
    backlog: list[str]         # sources fetched but not read yet because the needed facts were already backed
    settle_sites: int          # independent sites that settle a fact (2 for research, 1 when only checking a premise)
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
                 render_fn=None, registry=None, call_api_fn=None, now_fn=None, scout: bool = True):
        self.now_fn = now_fn                  # replaceable clock (tests, replays); None means the real one
        self.network_workers = 4              # searches and page downloads run side by side; models stay one at a time
        self.scout_enabled = scout            # a first quick search before planning (tests that count searches switch it off)
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
              allow_capabilities: bool = False, run_id: str | None = None, ask_sources: bool = False) -> dict:
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
            "ask_sources": ask_sources, "backlog": [], "settle_sites": 2, "premise": [], "premise_status": "none", "premise_missing": [], "premise_claim_ids": [],
            "premise_url": None, "corrections": [], "sources_asked": 0, "outcome": None, "route": "discover",
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
        elif asks and asks[0].get("type") == "source_needed":
            self.runs.emit(run_id, "runner", "source_needed", asks[0])
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

    def _side_by_side(self, fn, items: list) -> list:
        """Run fn over items with network waiting overlapped (searches, page downloads). Results come back in the order of
        items; an exception is returned in its place so one failure never hides the others. Models are not run this way: they
        share one graphics card, so they stay one at a time (see n_check for how their work is ordered instead)."""
        if self.network_workers <= 1 or len(items) <= 1:
            out = []
            for item in items:
                try:
                    out.append(fn(item))
                except Exception as exc:
                    out.append(exc)
            return out
        def safe(item):
            try:
                return fn(item)
            except Exception as exc:
                return exc
        with ThreadPoolExecutor(max_workers=min(self.network_workers, len(items))) as pool:
            return list(pool.map(safe, items))

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
        for name in ("scout", "plan", "approve", "premise", "gate", "discover", "select", "fetch", "api_lookup", "extract", "check", "assess",
                     "capability", "report"):
            g.add_node(name, getattr(self, f"n_{name}"))
        g.add_edge(START, "scout")
        g.add_edge("scout", "plan")
        g.add_edge("plan", "approve")
        g.add_edge("premise", "approve")
        g.add_conditional_edges("approve", lambda s: s.get("route", "discover"), {
            "discover": "discover", "plan": "plan", "premise": "premise", "report": "report", "end": END})
        for a, b in (("discover", "select"), ("select", "fetch"), ("fetch", "api_lookup"), ("api_lookup", "extract"),
                     ("extract", "check"), ("check", "assess")):
            g.add_edge(a, b)
        g.add_conditional_edges("assess", self._more_or_report, {"capability": "capability", "discover": "discover", "gate": "gate", "report": "report"})
        g.add_conditional_edges("gate", lambda s: s.get("gate", "stop"), {"read": "fetch", "stop": "report"})
        g.add_conditional_edges("capability", lambda s: "discover" if s.get("capability_id") else "report", {"discover": "discover", "report": "report"})
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
        if gaps and s["queries"] and room:
            return "discover"
        return "gate" if gaps and s.get("ask_sources") and s.get("sources_asked", 0) < 2 else "report"

    # -- nodes -----------------------------------------------------------------------------------

    # A first quick look before planning: two searches, titles and snippets only. What it shows is context for the plan (which
    # entity is meant, which sites exist, which period is available). It is never evidence: no claim is taken from it.
    SCOUT_SEARCHES = 2

    def n_scout(self, s: State) -> dict:
        if not self.scout_enabled:
            return {"scout": []}
        rid, q = s["run_id"], s["question"]
        queries = _dedupe([q, f"{q} official"])[:self.SCOUT_SEARCHES]
        results, warnings, seen = [], list(s.get("warnings", [])), set()
        for found in self._side_by_side(lambda query: self.search_fn(query, max_results=6), queries):
            if isinstance(found, Exception):               # the scout is a help, never a reason to stop the run
                warnings.append(f"scout search failed: {type(found).__name__}: {str(found)[:120]}")
                continue
            for hit in found["results"]:
                key = normalize_url(hit["url"])
                if key in seen:
                    continue
                seen.add(key)
                results.append({"title": (hit.get("title") or "")[:120], "url": hit["url"], "site": corroboration.site(hit["url"]),
                                "snippet": (hit.get("snippet") or "")[:220], "published": short_date(hit.get("published"))})
        results = results[:10]
        self.runs.emit(rid, "scout", "scout_done", {"queries": queries, "results": len(results),
                                                    "sites": _dedupe([r["site"] for r in results])[:8],
                                                    "searched_at": tclock.now(self.now_fn).as_dict()})
        return {"scout": results, "warnings": warnings}

    @staticmethod
    def _scout_context(s: State) -> str:
        hits = (s.get("scout") or [])[:6]
        if not hits:
            return ""
        lines = "\n".join(f"- {h['title'][:80]} ({h['site']}{', ' + h['published'] if h['published'] else ''})" for h in hits)
        return ("WEB CONTEXT (a first quick search; unverified, use it only to see which entity, sites and period exist, and never "
                "copy a value from it into the checklist):\n" + lines + "\n")

    def _year_suggestion(self, s: State) -> str | None:
        """Plain arithmetic, not a model: the question names a year that no search result mentions, while results do mention
        another year. A person may have mistyped it, so offer the nearest year the results show. It only asks; it never decides."""
        named = {int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", s["question"])}
        hits = s.get("scout") or []
        if len(named) != 1 or not hits:
            return None
        year = next(iter(named))
        seen: dict[int, set] = {}
        for h in hits:
            for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", f"{h['title']} {h['url']} {h['snippet']} {h['published'] or ''}"):
                seen.setdefault(int(y), set()).add(h["site"])
        if year in seen or not seen:
            return None
        this_year = tclock.now(self.now_fn).local.year
        reachable = {y: sites for y, sites in seen.items() if y <= this_year}
        if not reachable:
            return None
        best = max(reachable, key=lambda y: (len(reachable[y]), y))     # the year most sites mention; the later one on a tie
        where = ", ".join(sorted(reachable[best])[:3])
        return (f"The question names {year}, but no search result mentions it. The results do mention {best} ({where}). "
                f"Did you mean {best}?")

    def _advice_of(self, s: State) -> dict:
        """Ranked places to look and, when the question names something the results do not show but a close match exists, a
        suggested correction for the person to accept or ignore. Nothing here changes the question by itself."""
        hits = s.get("scout") or []
        if not hits:
            return {"sources": [], "suggestion": None}
        listing = "\n".join(f"- {h['title'][:90]} | {h['site']} | {h['published'] or 'no date'} | {h['snippet'][:160]}" for h in hits)
        prompt = (f"QUESTION: {s['question']}\n\nA first quick search showed (unverified; titles and snippets only):\n{listing}\n\n"
                  f"{sources.priority_advice()}\n\n"
                  "Return sources: up to 4 places to look first, best first, each a site taken from the results or the subject's own "
                  "site, with one short reason. Return suggestion only if the question names something the results do not show but "
                  f"a close match appears ({sources.priority()['suggest_correction_when']}); else null. "
                  "Never state a value, date or quote from the snippets as an answer.")
        out = self._ask("plan", Advice, prompt, s["run_id"], "plan")
        suggestion = self._year_suggestion(s)       # computed by code; the model's own suggestion is used only when there is none
        if out is None:
            return {"sources": [], "suggestion": suggestion}
        return {"sources": [{"site": a.site[:80], "why": a.why[:160]} for a in out.sources[:4] if a.site.strip()],
                "suggestion": suggestion or (out.suggestion or "").strip()[:300] or None}

    def n_plan(self, s: State) -> dict:
        rid, b = s["run_id"], s["budgets"]
        now = tclock.now(self.now_fn)
        scope = tintent.classify(s["question"], now)
        brief = planning.build(s["question"], None, scope, now)
        prompt = (
            "You plan web research that must be backed by quoted evidence.\n"
            f"TODAY: {now.describe()}. Your training data is older than today, so never assume what the current year is.\n"
            f"TIME SCOPE (worked out from the question): {scope.guidance(now)}\n"
            f"SEARCH ADVICE: {sources.search_advice()}\n"
            f"{brief.guidance() + chr(10) if not brief.empty else ''}"
            f"{self._scout_context(s)}"
            + (f"THE PERSON CORRECTED YOUR EARLIER READING: {'; '.join(s['corrections'])}. The correction overrides the original "
               f"wording: plan for the corrected question.\n" if s.get("corrections") else "")
            + f"QUESTION: {s['question']}\n\n"
            f"Return: subject (what the question is about); company (the official company name only if the "
            f"company's own website matters, else null); page_categories (company pages worth reading, from "
            f"{list(CATEGORY_NAMES)}; empty if no company); queries (up to {b['max_queries_per_iter']} web search queries, "
            f"each naming the company or subject and each taking a DIFFERENT angle (for example the company's own investor-relations "
            f"or annual report, its filings, and the concrete events the question may mean, such as acquisitions, spin-offs or "
            f"layoffs), never three phrasings of one search; use exact names and distinguishing terms; search operators work, e.g. "
            f"site:patents.google.com, site:sec.gov, or a company's own domain); checklist: ONE item for each distinct fact "
            f"the question itself asks for, and nothing more. A question that asks one thing gets exactly ONE item; a question "
            f"that asks for a name, a ticker and a date gets THREE. Never add related facts the question did not ask for "
            f"(headquarters, funding, history, sources). Write each item as a short label for the fact (for example 'Year "
            f"Dropbox was founded'), never the answer or any value, even if you think you know it: your guess may be wrong and "
            f"would mislead the search. Never add steps such as verifying, cross-checking or confirming sources; the system "
            f"does that itself."
        )
        plan = self._ask("plan", Plan, prompt, rid, "plan")
        warnings = list(s.get("warnings", []))
        if plan is None:
            warnings.append("planner failed: using the question itself as the only query and checklist item")
            plan = Plan(subject=s["question"][:60], queries=[s["question"]], checklist=[s["question"]])
        subject = s.get("subject") or plan.subject
        entity_name = plan.company or subject
        queries = _dedupe(plan.queries)[:b["max_queries_per_iter"]] or [s["question"]]
        queries = _dedupe(tintent.apply_year_terms(queries, scope, now))[:b["max_queries_per_iter"]]
        queries = _dedupe(planning.diversify(queries, s["question"], subject, plan.company, brief, scope, now, b["max_queries_per_iter"]))
        checklist = _dedupe(plan.checklist)[:6] or [s["question"]]
        categories = [c for c in plan.page_categories if plan.company]
        entity_id = self.store.add_entity(entity_name, kind="subject")
        advice = self._advice_of(s)
        self.runs.emit(rid, "plan", "plan_ready", {"subject": subject, "company": plan.company,
                       "page_categories": categories, "queries": queries, "checklist": checklist,
                       "today": now.as_dict(), "time_scope": scope.as_dict(),
                       "advice": advice, "brief": brief.as_dict()})
        return {"subject": subject, "company": plan.company, "entity_name": entity_name, "entity_id": entity_id,
                "page_categories": categories, "queries": queries, "checklist": checklist, "warnings": warnings,
                "data_need": plan.needs_data_source, "time_scope": scope.as_dict(), "brief": brief.as_dict(),
                "premise": (listed := self._premise_of(s, subject)), "premise_status": "pending" if listed else "none",
                "advice": advice,
                # a fresh plan starts clean: nothing from an earlier reading of the question carries over
                "approved": False, "outcome": None, "premise_missing": [], "premise_claim_ids": [],
                "premise_url": None, "all_claim_ids": [], "verdicts": {}, "relevant": {}, "claim_items": {}, "covered": [],
                "new_sources": [], "rejected": 0, "fetched_keys": [], "backlog": []}

    # Verify first, act later: what the question takes for granted is established like any other claim (search, read,
    # verbatim quote, support check) before the person is asked to approve anything. Nothing is decided from the date or
    # from the model's memory, and the person sees the finding together with the plan and can answer it.
    PREMISE_BUDGETS = {"max_iterations": 1, "max_searches": 2, "max_fetches": 4}

    def n_premise(self, s: State) -> dict:
        premise = s.get("premise") or []
        if not premise:
            return {"premise_status": "none", "premise_missing": []}
        rid, b = s["run_id"], s["budgets"]
        self.runs.emit(rid, "premise", "premise_check", {"premise": premise, "given_source": s.get("premise_url")})
        sub = {**s, "checklist": premise, "queries": premise[:b["max_queries_per_iter"]], "covered": [], "iteration": 0,
               "budgets": {**b, **self.PREMISE_BUDGETS}, "searches_used": 0, "fetches_used": 0, "fetch_failures": 0, "fetched_keys": [],
               "claim_items": {}, "new_sources": [], "new_claim_ids": [], "all_claim_ids": [], "verdicts": {}, "relevant": {},
               "rejected": 0, "selected": [], "candidates": [], "page_categories": [], "company": None, "warnings": [],
               "backlog": [], "settle_sites": 1}
        for step in (self.n_discover, self.n_select, "extra", self.n_fetch, self.n_extract, self.n_check):
            if step == "extra":                 # a source the person gave is read as well, never instead of the search
                if s.get("premise_url"):
                    sub["selected"] = sub["selected"] + [{"url": s["premise_url"], "title": s["premise_url"], "snippet": "",
                                                          "score": 1.0, "tier": "user_suggested"}]
                continue
            sub.update(step(sub))
        covered = {sub["claim_items"][c] for c, v in sub["verdicts"].items()
                   if v == "supported" and sub["relevant"].get(c) and c in sub["claim_items"]}
        missing = [t for i, t in enumerate(premise) if i not in covered]
        status = "unverified" if missing else "verified"
        self.runs.emit(rid, "premise", "premise_checked", {"status": status, "missing": missing, "claims": list(sub["all_claim_ids"])})
        return {"premise_status": status, "premise_missing": missing, "premise_claim_ids": list(sub["all_claim_ids"]),
                "all_claim_ids": list(sub["all_claim_ids"]), "verdicts": sub["verdicts"], "relevant": sub["relevant"],
                "rejected": sub["rejected"], "warnings": list(s.get("warnings", [])) + sub["warnings"], "premise_url": None,
                # the real research starts clean: its own budget, no pages marked as already read, no fact marked as covered
                "claim_items": {}, "covered": [], "new_sources": [], "new_claim_ids": [], "fetched_keys": [], "selected": [],
                "candidates": [], "searches_used": 0, "fetches_used": 0, "fetch_failures": 0, "iteration": 0,
                "backlog": [], "settle_sites": 2}

    def _premise_view(self, s: State) -> dict:
        claims = []
        for cid in (s.get("premise_claim_ids") or [])[:4]:
            if s["verdicts"].get(str(cid)) == "supported":
                claim = self.store.get_claim(cid)
                claims.append({"claim_id": cid, "claim": claim["claim_text"],
                               "source_url": self.store.get_source(claim["source_sha256"])["final_url"]})
        return {"status": s.get("premise_status", "none"), "items": s.get("premise") or [], "missing": s.get("premise_missing") or [],
                "claims": claims}

    def _premise_of(self, s: State, subject: str) -> list[str]:
        """What the question refers to as if it existed (a document, event, statement, role): the things to verify first.
        A focused call of its own, because the planner's prompt is already crowded and a small model then skips it."""
        prompt = (
            "A person asked a question. List the specific things the question refers to as if they certainly exist: a named "
            "document, report, letter, paper, event, announcement, statement, deal or office. Write each as a plain statement "
            "that could be true or false. List only what must exist for the question to make sense, never the thing the question asks for, "
            "and never facts the question did not mention. "
            "A question that only asks for a general fact (a number, a date, a name) and refers to nothing specific gets an empty list.\n\n"
            "Examples:\n"
            "Question: What did Acme's founder say in the 2040 annual report?\n"
            "premise: [\"Acme published a 2040 annual report\"]\n"
            "Question: Who wrote the paper 'Orange Lattices in Tuesday Space'?\n"
            "premise: [\"A paper titled 'Orange Lattices in Tuesday Space' exists\"]\n"
            "Question: What is the population of Norway?\n"
            "premise: []\n\n"
            + (f"The person later corrected the reading of the question: {'; '.join(s['corrections'])}.\n" if s.get("corrections") else "")
            + f"Question: {s['question']}\npremise:")
        out = self._ask("plan", PremiseList, prompt, s["run_id"], "plan")
        return _dedupe(out.premise)[:3] if out else []

    def n_approve(self, s: State) -> dict:
        """The person answers the agent's plan and its finding about the question's premise: approve it, accept a refusal,
        overrule a refusal, correct the agent in words (the plan is rebuilt), or hand it a source for the premise."""
        rid, b = s["run_id"], s["budgets"]
        status = s.get("premise_status")
        unverified = status == "unverified"
        if status == "verified":             # the person approved the plan and the premise held up: nothing more to ask
            self.runs.emit(rid, "approve", "approved", {"premise": "verified"})
            return {"route": "discover", "approved": True}
        if not s.get("require_approval"):
            if status == "pending":
                return {"route": "premise"}
            if unverified:
                self.runs.emit(rid, "approve", "premise_unverified", {"missing": s["premise_missing"]})
                return {"route": "report", "approved": True, "outcome": "premise_unverified"}
            return {"route": "discover", "approved": True}
        decision = interrupt({"question": s["question"], "subject": s["subject"], "queries": s["queries"],
                              "checklist": s["checklist"], "page_categories": s["page_categories"],
                              "premise": self._premise_view(s), "corrections": s.get("corrections") or [],
                              "advice": s.get("advice") or {"sources": [], "suggestion": None},
                              "brief": s.get("brief") or {}}) or {}
        comment = str(decision.get("comment") or "").strip()
        url = str(decision.get("url") or "").strip()
        if comment:
            original = s.get("original_question") or s["question"]
            corrections = list(s.get("corrections") or []) + [comment]
            self.runs.emit(rid, "approve", "plan_corrected", {"comment": comment})
            return {"route": "plan", "original_question": original, "corrections": corrections,
                    "question": f"{original} [Correction from the person: {'; '.join(corrections)}]"}
        if url and re.match(r"https?://\S+$", url):
            self.runs.emit(rid, "approve", "source_supplied", {"url": url, "stage": "premise"})
            return {"route": "premise", "premise_url": url}
        if not decision.get("approved", True):
            return {"route": "end", "approved": False}
        if unverified and not decision.get("override"):
            self.runs.emit(rid, "approve", "premise_unverified", {"missing": s["premise_missing"], "accepted_by_person": True})
            return {"route": "report", "approved": True, "outcome": "premise_unverified"}
        update: dict = {"route": "premise" if status == "pending" else "discover", "approved": True}
        if unverified:        # the person overruled the agent: the research goes ahead, and the report says the premise stands unverified
            update["premise_status"] = "overridden"
            update["warnings"] = list(s.get("warnings", [])) + [
                "the premise was not established (" + "; ".join(s["premise_missing"]) + "); the research went ahead at your request"]
        if decision.get("queries"):
            update["queries"] = _dedupe(decision["queries"])[:b["max_queries_per_iter"]]
        if decision.get("checklist"):
            update["checklist"] = _dedupe(decision["checklist"])[:6]
        self.runs.emit(rid, "approve", "approved", {k: v for k, v in update.items() if k not in ("approved", "route", "warnings")})
        return update

    def n_gate(self, s: State) -> dict:
        """The research ended with something missing: ask the person for a source to read, or stop and say what is missing."""
        rid = s["run_id"]
        gaps = [t for i, t in enumerate(s["checklist"]) if i not in s["covered"]]
        asked = s.get("sources_asked", 0)
        if not s.get("ask_sources") or asked >= 2:
            self.runs.emit(rid, "gate", "gate_stopped", {"missing": gaps, "sources_asked": asked})
            return {"gate": "stop"}
        decision = interrupt({"type": "source_needed", "stage": "answer", "missing": gaps,
                              "question": s["question"], "sources_asked": asked}) or {}
        url = str(decision.get("url") or "").strip()
        if not re.match(r"https?://\S+$", url):
            self.runs.emit(rid, "gate", "gate_stopped", {"missing": gaps, "sources_asked": asked, "person_declined": True})
            return {"gate": "stop"}
        self.runs.emit(rid, "gate", "source_supplied", {"url": url, "stage": "answer"})
        return {"gate": "read", "sources_asked": asked + 1,
                "selected": [{"url": url, "title": url, "snippet": "", "score": 1.0, "tier": "user_suggested"}]}

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
        recency = (s.get("time_scope") or {}).get("recency")
        room = max(0, b["max_searches"] - searches)
        todo = s["queries"][:room]
        if len(s["queries"]) > room:
            self.runs.emit(rid, "discover", "budget_reached", {"budget": "max_searches"})
        outcomes = self._side_by_side(lambda q: self.search_fn(q, max_results=8, **({"recency": recency} if recency else {})), todo)
        for query, result in zip(todo, outcomes):
            if isinstance(result, Exception):
                raise result
            searched_at = tclock.now(self.now_fn).as_dict()
            searches += 1
            for hit in result["results"]:
                key = normalize_url(hit["url"])
                official = bool(domain) and primary.is_official(hit["url"], [domain])
                known = found.get(key)
                if known is None or (known["tier"] != "primary" and official):
                    found[key] = {"url": hit["url"], "title": hit["title"], "snippet": hit["snippet"],
                                  "score": hit["score"], "tier": "primary" if official else "secondary",
                                  "published": hit.get("published")}
            self.runs.emit(rid, "discover", "search_done", {
                "query": query, "results": len(result["results"]), "search_id": result["search_id"],
                "providers": result["providers"], "warnings": result["warnings"],
                "searched_at": searched_at, "recency": recency})
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
        now = tclock.now(self.now_fn)
        scope = s.get("time_scope") or {}
        listing = "\n".join(
            f"{i}. [{c['tier']}] {c['title'][:90]} - {c['url']}"
            f"{' (dated ' + short_date(c.get('published')) + ')' if short_date(c.get('published')) else ' (no date shown)'}"
            f"\n   {c['snippet'][:160]!r}" for i, c in enumerate(shown, 1))
        timing = ""
        if scope:
            ts = tintent.TimeScope(scope["intent"], scope["recent"], scope["recency"], tuple(scope["years"]), scope["window_start"],
                                   scope["window_end"], scope["period_sensitive"], tuple(scope["reasons"]))
            timing = f"TODAY: {now.describe()}.\nTIME SCOPE: {ts.guidance(now)}\n"
        prompt = (f"{timing}QUESTION: {s['question']}\nFACTS NEEDED: {s['checklist']}\n\nCandidate pages:\n{listing}\n\n"
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
        downloads = self._side_by_side(lambda c: self.fetch_fn(c["url"], max_chars=2000), s["selected"])
        for cand, downloaded in zip(s["selected"], downloads):
            url = cand["url"]
            keys.append(normalize_url(url))
            fetches += 1
            try:
                if isinstance(downloaded, Exception):
                    raise downloaded
                result = downloaded
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
            self._record_hit_date(sha, cand.get("published"))
            facts = self._source_dates(sha)
            best = best_date(facts)
            self.runs.emit(rid, "fetch", "source_fetched", {
                "url": url, "source_sha256": sha, "tier": cand["tier"], "readable_chars": len(text),
                "dates": [{"kind": f.kind, "value": f.value, "origin": f.origin} for f in facts][:8],
                "best_date": best.value if best else None, "best_date_kind": best.kind if best else None})
        failed = len(s["selected"]) - len(new_sources)
        return {"new_sources": list(s.get("backlog") or []) + new_sources, "backlog": [], "fetches_used": fetches,
                "fetched_keys": keys, "warnings": warnings, "fetch_failures": s["fetch_failures"] + failed}

    def _source_dates(self, sha: str) -> list[DateFact]:
        return [DateFact(d["kind"], d["value"], d["precision"], d["raw"], d["origin"], bool(d["relative"]))
                for d in self.store.source_dates(sha)]

    def _record_hit_date(self, sha: str, published) -> None:
        """The date a search engine showed for this page is one more signal about it, kept with its origin."""
        if not published:
            return
        parsed = parse_date(published, tclock.now(self.now_fn).utc)
        if parsed:
            self.store.record_source_dates(sha, [DateFact("search_result", parsed[0], parsed[1], str(published)[:120], "search", parsed[2])])

    def n_api_lookup(self, s: State) -> dict:
        """Ask the model which approved connector calls would help, then make them through the guarded path."""
        rid, b = s["run_id"], s["budgets"]
        active = self.registry.list_active()
        room = b["max_api_calls"] - s["api_calls_used"]
        if not active or room <= 0:
            return {}
        routed = sources.route([m.connector_id for _, m in active], s["question"], " ".join(s["checklist"]))
        self.runs.emit(rid, "api_lookup", "sources_routed", routed)
        active = [(v, m) for v, m in active if m.connector_id in routed["offered"]]
        if not active:     # no approved connector fits this question: skip the model call, the web search carries on
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
        todo, backlog = list(s["new_sources"]), []
        for pos, sha in enumerate(todo):
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
                    if narrowed := narrow_quote(quote, c.claim, value):
                        self.runs.emit(rid, "extract", "quote_narrowed", {"from_chars": len(normalize(quote)), "to": narrowed,
                                                                           "source_url": source["final_url"]})
                        quote = narrowed
                    if len(normalize(quote)) < MIN_QUOTE_CHARS and (wider := widen_quote(text, quote)):
                        self.runs.emit(rid, "extract", "quote_widened", {"from": normalize(quote), "to": wider,
                                                                         "source_url": source["final_url"]})
                        quote = wider
                    r = self.store.add_claim(s["entity_id"], c.predicate, value, c.claim, sha, quote,
                                             created_by=f"runner:{rid[:8]}")
                    if r["ok"]:
                        new_ids.append(r["claim_id"])
                        items[str(r["claim_id"])] = c.needed_fact - 1
                        self._record_times(rid, r["claim_id"])
                        self.runs.emit(rid, "extract", "claim_recorded", {
                            "claim_id": r["claim_id"], "claim": c.claim, "value": value, "quote": normalize(c.quote),
                            "needed_fact": c.needed_fact,
                            "source_url": source["final_url"], "source_sha256": sha, "source_tier": r["source_tier"]})
                    else:
                        rejected += 1
                        self.runs.emit(rid, "extract", "claim_rejected", {
                            "claim": c.claim, "quote": c.quote, "reason": r["reason"],
                            "closest_passage": r.get("closest_passage"), "source_url": source["final_url"]})
            if pos + 1 < len(todo) and self._backed(items, len(s["checklist"]), s.get("settle_sites", 2)):
                backlog = todo[pos + 1:]               # every needed fact already has claims from enough sites: read the rest only if needed
                self.runs.emit(rid, "extract", "extract_stopped", {
                    "read": pos + 1, "kept_for_later": len(backlog),
                    "reason": "every needed fact already has claims from enough independent sites"})
                break
        return {"new_claim_ids": new_ids, "all_claim_ids": s["all_claim_ids"] + new_ids, "rejected": rejected,
                "new_sources": [], "claim_items": items, "backlog": backlog}

    def _backed(self, items: dict, facts: int, need_sites: int) -> bool:
        """Does every needed fact have recorded claims from at least `need_sites` independent sites (or one primary source)?"""
        sites: dict[int, set] = {}
        primary: set[int] = set()
        for cid, fact in items.items():
            claim = self.store.get_claim(int(cid))
            sites.setdefault(fact, set()).add(corroboration.site(self.store.get_source(claim["source_sha256"])["final_url"]))
            if claim["source_tier"] == "primary":
                primary.add(fact)
        return all(i in primary or len(sites.get(i, ())) >= need_sites for i in range(facts))

    def _record_times(self, rid: str, claim_id: int) -> None:
        """Attach period and event dates to a recorded claim, read by code from its quote and the text around it (never by a
        model). A claim carried by an SEC XBRL fact gets its exact period from the fact itself."""
        try:
            claim = self.store.get_claim(claim_id)
            source = self.store.get_source(claim["source_sha256"])
            text = normalize(Path(source["text_path"]).read_text(encoding="utf-8"))
            lo, hi = claim["quote_start"], claim["quote_end"]
            best = best_date(self._source_dates(claim["source_sha256"]))
            published = best.value if best and best.kind == "published" else None
            items = tperiods.analyse(claim["quote"], f"{text[max(0, lo - 250):lo]} {text[hi:hi + 250]}", published,
                                     best.precision if best else None)
            if exact := tperiods.period_from_xbrl(text, lo, hi):
                items = exact + [i for i in items if i.kind != "period"]
            if items and self.store.record_claim_time(claim_id, items, created_by=f"runner:{rid[:8]}"):
                self.runs.emit(rid, "extract", "claim_time_recorded", {
                    "claim_id": claim_id, "items": [{"kind": i.kind, "value": i.value, "origin": i.origin} for i in items][:8]})
        except Exception as exc:   # time attributes are an addition: a failure here must not lose the claim
            self.runs.emit(rid, "extract", "claim_time_failed", {"claim_id": claim_id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})

    # Stop when the answer is in. A fact is settled once a supported, relevant claim from a primary source backs it, or supported
    # relevant claims from `settle_sites` independent sites do. The remaining claims for that fact are not checked (they stay
    # listed as unchecked, never hidden): the run's answer would not change and each check costs two model calls. Checks run in
    # small waves, all support checks (one model) and then all relevance checks (the other), so the two models are not swapped
    # in and out of the graphics card for every claim.
    CHECK_WAVE = 3

    def _settled_facts(self, verdicts: dict, relevant: dict, items: dict, need_sites: int) -> set[int]:
        sites: dict[int, set] = {}
        primary: set[int] = set()
        for c, v in verdicts.items():
            if v == "supported" and relevant.get(c) and c in items:
                claim = self.store.get_claim(int(c))
                sites.setdefault(items[c], set()).add(corroboration.site(self.store.get_source(claim["source_sha256"])["final_url"]))
                if claim["source_tier"] == "primary":
                    primary.add(items[c])
        return primary | {f for f, found in sites.items() if len(found) >= need_sites}

    def _check_order(self, ids: list[int]) -> list[int]:
        """Primary-source claims first, then one claim per site before a second from the same site, so the first wave can settle."""
        seen: dict[str, int] = {}
        keyed = []
        for cid in ids:
            claim = self.store.get_claim(cid)
            where = corroboration.site(self.store.get_source(claim["source_sha256"])["final_url"])
            seen[where] = seen.get(where, 0) + 1
            keyed.append((claim["source_tier"] != "primary", seen[where], len(keyed), cid))
        return [cid for *_, cid in sorted(keyed)]

    def n_check(self, s: State) -> dict:
        rid = s["run_id"]
        verdicts, relevant = dict(s["verdicts"]), dict(s["relevant"])
        need = s.get("settle_sites", 2)
        pending = self._check_order(list(s["new_claim_ids"]))
        while pending:
            settled = self._settled_facts(verdicts, relevant, s["claim_items"], need)
            wave, rest = [], []
            for cid in pending:
                fact = s["claim_items"].get(str(cid))
                if fact in settled:
                    self.runs.emit(rid, "check", "check_skipped", {
                        "claim_id": cid, "needed_fact": s["checklist"][fact],
                        "reason": "this fact is already backed; the claim stays on record, unchecked"})
                elif len(wave) < self.CHECK_WAVE:
                    wave.append(cid)
                else:
                    rest.append(cid)
            pending = rest
            if not wave:
                break
            supported = []
            for cid in wave:
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
                if res["verdict"] == "supported":
                    supported.append(cid)
            for cid in supported:
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
            best = best_date(self._source_dates(claim["source_sha256"]))
            row = {"claim_id": cid, "claim": claim["claim_text"], "value": claim["value"], "quote": claim["quote"],
                   "source_url": source["final_url"], "source_sha256": claim["source_sha256"],
                   "source_tier": claim["source_tier"], "fact": s["claim_items"].get(str(cid)),
                   "times": self.store.claim_times(cid), "premise": cid in set(s.get("premise_claim_ids") or []),
                   "source_date": best.value if best else None, "source_date_kind": best.kind if best else None}
            checks = self.store.support_checks_for(cid)
            if checks:
                row["why"] = checks[-1]["reasoning"]
            verdict = s["verdicts"].get(str(cid), "unchecked")
            if verdict == "supported" and not s["relevant"].get(str(cid)):
                verdict = "off_topic"  # true to its source, but does not answer a needed fact
            rows[verdict].append(row)
        gaps = [t for i, t in enumerate(s["checklist"]) if i not in s["covered"]]

        # how well is each fact backed, and do the sources agree? (labels only: nothing is hidden)
        shown = rows["supported"] + rows["partially_supported"]
        conflicts = corroboration.find_conflicts(rows["supported"])
        for c in conflicts:
            a, b = c["claims"]
            self.store.flag_contradiction(a, b, c["note"], created_by=f"runner:{rid[:8]}")
        disputed = {cid for c in conflicts for cid in c["claims"]}
        sites: dict = {}
        for r in rows["supported"]:
            sites.setdefault(r["fact"], set()).add(corroboration.site(r["source_url"]))
        for r in shown:
            r["corroboration"] = corroboration.label(r, sites.get(r["fact"], set()), r["claim_id"] in disputed)
            r["as_of"] = next((t["value"] for t in r["times"] if t["kind"] == "as_of"), None)

        # how old is what the answer rests on? (established claims first; partial ones only if nothing is established)
        basis = rows["supported"] or rows["partially_supported"]
        timing = freshness.assess({r["source_sha256"]: self._source_dates(r["source_sha256"]) for r in basis},
                                  s.get("time_scope") or {}, tclock.now(self.now_fn), temporal_setting("latest_max_age_days"))
        time_warning = freshness.warning(timing)

        premise_failed = s.get("outcome") == "premise_unverified"
        summary, dropped = "", []
        allowed = {r["claim_id"] for r in rows["supported"] if not r["premise"]}
        if allowed and not premise_failed:
            listing = "\n".join(f"[claim:{r['claim_id']}] {r['claim']}" for r in rows["supported"] if not r["premise"])
            prompt = (f"QUESTION: {s['question']}\n\nVERIFIED CLAIMS:\n{listing}\n\nWrite a short factual answer using ONLY "
                      f"these claims. Cite each statement as [claim:ID]. Say plainly what the claims do not establish. "
                      f"Do not add anything that is not in the claims.")
            draft = self._ask("synthesis", Summary, prompt, rid, "report")
            if draft:
                cited = {int(i) for i in re.findall(r"\[claim:(\d+)\]", draft.text)}
                dropped = sorted(cited - allowed)
                summary = re.sub(r"\[claim:(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in allowed else "[invalid citation removed]", draft.text)
        if premise_failed:
            outcome = "premise_unverified"
        elif not gaps:
            outcome = "answered"
        else:
            outcome = "partial" if len(gaps) < len(s["checklist"]) else "not_found"
        report = {
            "outcome": outcome, "premise": [{"text": t, "verified": s.get("premise_status") == "verified"} for t in (s.get("premise") or [])],
            "question": s["question"], "subject": s["subject"], "summary": summary, "invalid_citations_removed": dropped,
            "established": rows["supported"], "partial": rows["partially_supported"],
            "not_supported": len(rows["not_supported"]), "unchecked": len(rows["unchecked"]),
            "off_topic": len(rows["off_topic"]), "conflicts": conflicts,
            "gaps": gaps, "rejected_quotes": s["rejected"], "warnings": s.get("warnings", []) + ([time_warning] if time_warning else []),
            "time": timing,
            "budget_used": {"searches": s["searches_used"], "fetches": s["fetches_used"], "iterations": s["iteration"]},
            "domain": s.get("domain"),
        }
        self.runs.emit(rid, "report", "report_ready", {k: (len(v) if isinstance(v, list) else v)
                                                       for k, v in report.items() if k != "summary"})
        return {"report": report}

"""Turn the planner's queries into a set that names the subject and looks from different angles.

A small model tends to write three phrasings of one search, and sometimes leaves the company out altogether. This step is plain
code: it puts the subject into every query, drops near-duplicates, and fills the freed slots with lenses from
definitions/guidance/query_lenses.json: the company's own investor-relations pages or filings, and the concrete events a loose
word such as "strategy" usually means (acquisitions, spin-offs, layoffs ...). It never decides what the answer is.
"""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT
from skills.planning.brief import PlanningBrief
from skills.temporal.clock import Now
from skills.temporal.intent import TimeScope, YEAR, apply_year_terms

LENSES_PATH = PROJECT_ROOT / "definitions" / "guidance" / "query_lenses.json"
_WORD = re.compile(r"[a-z0-9&][a-z0-9&'.-]*")
_ASK = {"what", "which", "who", "when", "where", "why", "how", "is", "are", "was", "were", "does", "did", "do", "tell", "give", "show",
        "find", "list", "the", "a", "an"}


@lru_cache(maxsize=1)
def lenses() -> dict:
    data = json.loads(LENSES_PATH.read_text(encoding="utf-8"))
    for key in ("source_lens", "generic_subjects", "similarity_threshold", "noise_words", "themes"):
        if key not in data:
            raise ValueError(f"query_lenses.json: {key} is missing")
    re.compile(data["source_lens"]["covers"])
    for t in data["themes"]:
        re.compile(t["route_when"])
    return data


def _tokens(text: str, drop: set[str]) -> set[str]:
    return {w.strip(".'-") for w in _WORD.findall(text.lower()) if w.strip(".'-") not in drop and len(w) > 1} - drop


def named_in_question(question: str) -> str | None:
    """The first run of capitalised words (or an all-caps ticker) that is not just the start of the sentence: 'S&P Global'."""
    words = re.findall(r"[A-Za-z0-9&][A-Za-z0-9&.'-]*", question)
    run, best = [], None
    for i, w in enumerate(words):
        bare = re.sub(r"'s$", "", w)
        proper = bool(bare) and (bare[0].isupper() or "&" in bare) and bare.lower() not in _ASK and not (i == 0 and not bare.isupper())
        if proper:
            run.append(bare)
        else:
            if run and best is None:
                best = " ".join(run)
            run = []
    best = best or (" ".join(run) if run else None)
    return best


def the_subject(subject: str, company: str | None, question: str) -> str:
    """The name to put in every query. A planner that picked a generic word ('Strategy') or something the question never says
    is overruled by the name the question itself uses."""
    name = (company or subject or "").strip()
    generic = {g.lower() for g in lenses()["generic_subjects"]}
    q = question.lower()
    unusable = not name or name.lower() in generic or not (_tokens(name, generic) & _tokens(q, set()))
    if unusable and (found := named_in_question(question)):
        return found
    return name


def _mentions(query: str, subject: str) -> bool:
    q = query.lower()
    if subject.lower() in q:
        return True
    toks = _tokens(subject, set(lenses()["generic_subjects"]))
    return bool(toks) and toks <= _tokens(q, set())


def _anchor(query: str, subject: str) -> str:
    if not subject or _mentions(query, subject):
        return query
    return f'{subject} {query}'.strip()


def _alike(a: str, b: str, drop: set[str]) -> bool:
    x, y = _tokens(a, drop), _tokens(b, drop)
    if not x or not y:
        return a.strip().lower() == b.strip().lower()
    return len(x & y) / len(x | y) >= lenses()["similarity_threshold"]


def _lens_candidates(text: str, brief: PlanningBrief, subject: str, company: str | None) -> list[dict]:
    """Lenses that apply to this question, best first: {covers (regex), query}."""
    cfg, out = lenses(), []
    src = cfg["source_lens"]
    themed = [t for t in cfg["themes"] if re.search(t["route_when"], text, re.I)]
    if brief.documents or company or themed:
        if brief.lead_operator:
            doc = f"{brief.doc_hint or 'annual report 10-K'} "
            query = f"{subject} {doc}{brief.lead_operator}".replace("  ", " ")
        else:
            query = src["company_owned"].format(subject=subject)
        out.append({"covers": src["covers"], "query": query, "required": True})
    for theme in themed:
        for i, lens in enumerate(theme["lenses"]):
            terms = lens["terms"]
            out.append({"covers": "|".join(re.escape(t.split()[0]) for t in terms), "query": f"{subject} {' '.join(terms)}",
                        "required": i == 0})
    return out


def diversify(queries: list[str], question: str, subject: str, company: str | None, brief: PlanningBrief,
              scope: TimeScope, now: Now, limit: int) -> list[str]:
    name = the_subject(subject, company, question)
    cfg = lenses()
    drop = set(cfg["noise_words"]) | {w for w in _tokens(name, set())}      # the subject is in every query; it cannot tell them apart
    kept: list[str] = []
    for q in queries:
        q = _anchor(q.strip(), name)
        if q and not any(_alike(q, k, drop) for k in kept):
            kept.append(q)
    need, optional = [], []
    for lens in _lens_candidates(question, brief, name, company):
        if not any(re.search(lens["covers"], q, re.I) for q in kept + need + optional):
            q = lens["query"]
            q = apply_year_terms([q], scope, now)[0] if scope.recent and not YEAR.search(q) else q
            (need if lens["required"] else optional).append(q)
    need = need[:max(0, limit - 1)] if kept else need[:limit]
    free = limit - len(kept) - len(need)             # optional lenses only use slots the planner left empty
    need += optional[:max(0, free)]
    room = max(1, limit - len(need)) if kept else 0
    final = kept[:room] + need
    for q in kept[room:]:                       # spare slots go back to the planner's own queries
        if len(final) >= limit:
            break
        final.append(q)
    return final[:limit]

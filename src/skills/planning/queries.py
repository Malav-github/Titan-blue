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


def names_in_question(question: str) -> list[str]:
    """Every run of capitalised words (or all-caps tickers) that is not just the start of the sentence, in order, without
    generic acronyms ('AI', 'CEO', 'Q3'): 'What AI initiatives did S&P Global announce?' -> ['S&P Global']."""
    words = re.findall(r"[A-Za-z0-9&][A-Za-z0-9&.'-]*", question)
    acronyms = {a.lower() for a in lenses().get("generic_acronyms", [])}
    runs, run = [], []
    for i, w in enumerate(words + [""]):
        bare = re.sub(r"[’']s$", "", w)
        proper = bool(bare) and (bare[0].isupper() or "&" in bare) and bare.lower() not in _ASK and not (i == 0 and not bare.isupper())
        if proper and bare.lower().strip(".") not in acronyms and not re.fullmatch(r"(?:Q[1-4]|H[12]|FY\d{0,4}|\d+)", bare):
            run.append(bare)
        else:
            if run:
                runs.append(" ".join(run))
            run = []
    return runs


def named_in_question(question: str) -> str | None:
    """The organisation the question names: a run of two or more words or one with '&' first ('S&P Global'), else the first."""
    runs = names_in_question(question)
    return next((r for r in runs if len(r.split()) > 1 or "&" in r), runs[0] if runs else None)


def the_subject(subject: str, company: str | None, question: str) -> str:
    """The name to put in every query. A planner that picked a generic word ('Strategy') or something the question never says
    is overruled by the name the question itself uses."""
    name = (company or subject or "").strip()
    generic = {g.lower() for g in lenses()["generic_subjects"]}
    q = question.lower()
    sentence = len(name.split()) >= 5 or "?" in name          # a whole question is not a name
    acronym = name.lower() in {a.lower() for a in lenses().get("generic_acronyms", [])}
    unusable = not name or sentence or acronym or name.lower() in generic or not (_tokens(name, generic) & _tokens(q, set()))
    # the planner picked something the question mentions, but not the organisation the question names ('AI' for S&P Global)
    found = named_in_question(question)
    if not unusable and found and not company and _tokens(found, generic) and not (_tokens(found, generic) & _tokens(name, generic)):
        unusable = True
    if unusable and (found := named_in_question(question)):
        return found
    return organisation_in(name, question)


def organisation_in(name: str, question: str) -> str:
    """The organisation in a subject that also names a topic: 'S&P Global's AI push' -> 'S&P Global'. Every query names
    the organisation; the topic belongs in the queries themselves, not stuck to the front of each one (a real run searched
    "S&P Global's AI push S&P Global AI announcements ..."). Unchanged when the subject is just a name."""
    for found in [named_in_question(question), *names_in_question(name)]:
        if not found or len(found) >= len(name):
            continue
        i = name.lower().find(found.lower())
        if i >= 0 and (len(found.split()) > 1 or "&" in found):
            return name[i:i + len(found)]               # the subject's own spelling ('S&P Global', not 'S&P GLobal')
    if m := re.match(r"^(.+?)['’]s\s+\S", name):          # "<organisation>'s <topic>"
        return m.group(1).strip()
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


def _meaning(tokens: set[str]) -> set[str]:
    """Words that ask a search for the same thing ('updates', 'product launches', 'press releases') counted as one."""
    groups = lenses().get("same_meaning", {})
    canon = {w: key for key, words in groups.items() for w in words}
    return {canon.get(t, t) for t in tokens}


def _alike(a: str, b: str, drop: set[str]) -> bool:
    x, y = _meaning(_tokens(a, drop)), _meaning(_tokens(b, drop))
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


_SITE = re.compile(r"\bsite:(\S+)", re.I)
_WIRES = re.compile(r"(?:^|\.)(?:prnewswire|businesswire|globenewswire|accesswire|newswire)\.", re.I)
_PATENT_SITES = re.compile(r"(?:^|\.)(?:patents\.google|patentscope|espacenet|worldwide\.espacenet|uspto|ppubs\.uspto|freepatentsonline)\.", re.I)
_FILINGS_SITES = re.compile(r"(?:^|\.)sec\.gov$", re.I)


def site_rules(queries: list[str], question: str, subject: str) -> tuple[list[str], list[dict]]:
    """Keep a planner's `site:` only where it fits the question; otherwise search the same words everywhere. A small model copies
    the examples in its prompt: a question about a technology portfolio was searched only on sec.gov, only on Google Patents
    and only on one trade magazine, so most of the web was never looked at. Kept: the subject's own site, the press-release
    wires (the company's own releases), sec.gov when the question is about what filings hold, patent sites when it is about
    patents. Dropped: any other single site. Returns (queries, changes)."""
    from skills.planning import catalog
    routes = {t["id"]: t["route_when"] for t in catalog()}
    asks = lambda kind: bool(re.search(routes.get(kind, r"(?!)"), question, re.I))          # noqa: E731
    own = re.sub(r"[^a-z0-9]", "", subject.lower())
    out, changes = [], []
    for q in queries:
        new = q
        for m in _SITE.finditer(q):
            host = m.group(1).lower().strip("/").split("/")[0]
            mine = len(own) >= 3 and own[:12] in re.sub(r"[^a-z0-9]", "", host)
            fits = (mine or bool(_WIRES.search(host))
                    or (bool(_FILINGS_SITES.search(host)) and (asks("company_disclosure") or asks("earnings_materials") or asks("leadership")
                                                               or asks("press_release")))
                    or (bool(_PATENT_SITES.search(host)) and asks("patent")))
            if not fits:
                new = re.sub(r"\s{2,}", " ", new.replace(m.group(0), " ")).strip()
                changes.append({"query": q, "dropped": m.group(0),
                                "why": "one site that the question does not point to; the same words are searched on the whole web"})
        out.append(new)
    return out, changes


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

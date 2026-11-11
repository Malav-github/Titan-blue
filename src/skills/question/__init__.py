"""The question skill (definitions/skills/question.json): what kind of answer a question wants, by code.

    analyse(question, subject, scope) -> {kind, shape, checklist, queries, cues, role, metric, period, ...}

One frame for every later step: the planner's checklist is replaced when the kind is known, the searches suited to the kind
join the planner's, and the shape says what an answering claim must contain (a person and the role, an amount for the
period, a date), which later steps check by code. A list question (skills/planning/enumerate.py) is framed as one too. A
question no kind matches is 'open': the planner's checklist stands and no shape is checked.
"""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT
from skills.planning.enumerate import enumeration
from skills.planning.queries import named_in_question

PATH = PROJECT_ROOT / "definitions" / "skills" / "question.json"

_PERIOD = re.compile(
    r"\b(?:(?:for|in|during|of) )?(?:the )?"
    r"(?P<p>(?:(?:first|second|third|fourth|1st|2nd|3rd|4th) (?:fiscal )?quarter|q[1-4]|(?:first|second) half|h[12]|"
    r"(?:fiscal|financial|calendar) year|fiscal|fy|full[- ]year|year)\s*(?:of\s*|ended\s*)?(?:fy\s*)?'?(?:19|20)?\d{2})\b"
    r"|\b(?:in|for|during) (?P<y>(?:19|20)\d{2})\b", re.I)
_ACRONYM_ROLES = {"ceo", "cfo", "coo", "cto", "cio", "cmo"}


@lru_cache(maxsize=1)
def config() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    for k in d["kinds"]:
        re.compile(k["route_when"], re.I)
        re.compile(k["cues"], re.I)
        if k["shape"] not in d["shapes"]:
            raise ValueError(f"question.json: {k['id']} has unknown shape {k['shape']}")
    return d


def _period(question: str) -> tuple[str, str]:
    """(' for the third quarter of 2026', 'third quarter 2026') or ('', '')."""
    m = _PERIOD.search(question)
    if not m:
        return "", ""
    words = m.group("p") or m.group("y")
    the = bool(m.group("p")) and not re.match(r"(?:fiscal|fy|q[1-4]|h[12])\s*'?\d", words, re.I)     # 'the third quarter', 'fiscal 2025'
    return f" for {'the ' if the else ''}{words}", re.sub(r"\b(?:of|ended)\b", "", words).replace("  ", " ").strip()


def _event(question: str, route: re.Match) -> str:
    """What a 'when' question asks the date of: the question after its asking words, without the question mark."""
    rest = question[route.end():].strip(" ?.")
    return re.sub(r"^(?:was|were|did|is|will|the)\s+", "", rest, flags=re.I).strip()


def analyse(question: str, subject: str | None = None, scope: dict | None = None) -> dict:
    cfg = config()
    q = " ".join(question.split())
    quoted = re.search(r"[\"“']([A-Z][^\"”']{3,120})[\"”']", q)        # a title in quotes is the subject: 'Attention Is All You Need'
    name = ((quoted.group(1) if quoted else None) or subject or named_in_question(q) or "").strip()
    # 'What were Apple's net sales for fiscal 2025 and what period does that cover?' asks two things: the kind and its
    # searches still help, but the planner's checklist (one item per thing asked) stands
    several = bool(re.search(r"\band (?:what|when|who|which|how|where|why)\b|\?\s*\S.*\?", q, re.I))
    listing = enumeration(q, name, scope or {}) if name else None
    if listing:
        kind = listing.get("kind", "events")
        shape = "items" if kind == "inventory" else "events"
        return {"kind": kind, "label": "A portfolio's items" if kind == "inventory" else "A list of events", "subject": name,
                "shape": shape, "shape_text": cfg["shapes"][shape], "checklist": [listing["item"]], "queries": listing["queries"],
                "cues": "", "why": "a list question (skills/planning/enumerate.py)"}
    for k in cfg["kinds"]:
        m = re.search(k["route_when"], q, re.I)
        if not m:
            continue
        role = (m.groupdict().get("role") or "").lower()
        role = role.upper() if role in _ACRONYM_ROLES else role
        metric = (m.groupdict().get("metric") or "").lower()
        period, period_words = _period(q)
        years = (scope or {}).get("years") or []
        as_of = f" in {years[0]}" if role and len(years) == 1 else (" (current)" if role else "")
        event = _event(q, m) if k["id"] == "date_of" else ""
        if k["id"] == "date_of" and not event:
            continue
        if k["id"] in ("person_role", "metric") and not name:
            continue                           # 'Who is the CEO?' of whom: the planner decides
        fill = {"subject": name, "Subject": name[:1].upper() + name[1:], "role": role, "Role": role[:1].upper() + role[1:],
                "metric": metric, "period": period, "period_words": period_words, "as_of": as_of, "event": event,
                "event_words": event}
        fmt = lambda t: re.sub(r"\s{2,}", " ", t.format(**fill)).strip().replace("'", '"') if "{event" in t else \
            re.sub(r"\s{2,}", " ", t.format(**fill)).strip()                                             # noqa: E731
        item = fmt(k["checklist"]).replace('"', "'")
        if k["id"] == "date_of" and re.search(r"\b(?:what|which) year\b", q, re.I):
            item = re.sub(r"^Date\b", "Year", item)
        return {"kind": k["id"], "label": k["label"], "subject": name, "shape": k["shape"], "shape_text": cfg["shapes"][k["shape"]],
                "checklist": None if several else [item], "queries": list(dict.fromkeys(fmt(t) for t in k["queries"] if fmt(t))),
                "cues": k["cues"], "role": role, "metric": metric, "period": period.strip(), "event": event,
                "why": f"the question matches the '{k['label'].lower()}' pattern"}
    return {"kind": "open", "label": "Open question", "subject": name, "shape": "open", "shape_text": cfg["shapes"]["open"],
            "checklist": None, "queries": [], "cues": "", "why": "no kind of question matched; the planner's checklist stands"}

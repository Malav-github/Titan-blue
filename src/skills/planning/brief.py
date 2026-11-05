"""Build the planning brief for one question: kinds of information, ranked places, document types, expected newest period,
and metadata to expect. Deterministic; nothing here touches the network."""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from skills import planning
from skills.temporal.clock import Now
from skills.temporal.intent import TimeScope, YEAR, apply_year_terms

ROLE_TEXT = {"primary": "the originator itself", "keeps_original": "republishes the original unchanged",
             "secondary": "second-hand; use to find the original"}
ROLE_ORDER = {"primary": 0, "keeps_original": 1, "secondary": 2}
SITE = re.compile(r"\bsite:([^\s]+)", re.I)


@dataclass(frozen=True)
class PlanningBrief:
    kinds: tuple[str, ...]            # labels of the kinds of information the question points to
    places: tuple[dict, ...]          # {site, operator, role, why, connector?} best first
    documents: tuple[dict, ...]       # {id, label, holds, tip, lag_note, expect}
    metadata: tuple[str, ...]         # fields worth expecting on the source
    target: str                       # which time the answer should come from
    recent: bool
    operators: tuple[str, ...]        # concrete site: operators for the best places (no placeholders)
    lead_operator: str | None         # the top place's operator, when it is concrete (not "the company's own site")
    doc_hint: str | None              # the document type to name in a query, e.g. "10-Q"
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.kinds and not self.documents

    def as_dict(self) -> dict:
        return {"kinds": list(self.kinds), "places": list(self.places), "documents": list(self.documents),
                "metadata": list(self.metadata), "target": self.target}

    def guidance(self) -> str:
        """A short paragraph for the planner prompt. Empty when the question points to no known kind of source."""
        if self.empty:
            return ""
        lines = ["PLANNING BRIEF (from the project's source catalog; it says where to look, never what the answer is):"]
        if self.kinds:
            lines.append("Kind of information: " + "; ".join(self.kinds) + ".")
        if self.places:
            lines.append("Places, best first: " + " ".join(
                f"{i}. {p['site']} ({ROLE_TEXT[p['role']]}): {p['why']}." for i, p in enumerate(self.places, 1)))
        for d in self.documents:
            lines.append(f"Document {d['id']} ({d['label']}): {d['holds']}. {d['expect']}"
                         + (f" {d['tip']}" if d.get("tip") else ""))
        if self.metadata:
            lines.append("Expect these fields on the source (put the one the question needs in the checklist label): "
                         + ", ".join(self.metadata) + ".")
        lines.append(f"Target time: {self.target}")
        lines.append("Write one query for each of the first two places using its site: operator, name the document type and the "
                     "period or year in the query, and write the checklist labels with the period or date they need.")
        return "\n".join(lines)


def _hits(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.I))


def _quarter_end(today: date, lag_days: int) -> date:
    """Latest calendar quarter end whose filing is certainly due by today."""
    ends = [date(y, m, d) for y in (today.year - 1, today.year) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(e for e in ends if e + timedelta(days=lag_days) <= today)


def _year_end(today: date, lag_days: int) -> date:
    ends = [date(y, 12, 31) for y in (today.year - 2, today.year - 1, today.year)]
    return max(e for e in ends if e + timedelta(days=lag_days) <= today)


def _expect(doc: dict, scope: TimeScope, now: Now) -> str:
    kind, lag = doc["period_kind"], doc["lag_days"]
    if not scope.recent:
        return f"Timing: {doc['lag_note']}."
    today = now.local.date()
    if kind == "quarter":
        end = _quarter_end(today, lag)
        return (f"Latest wanted as of {now.date}: a filing for a quarter ended on or before {end.isoformat()} certainly exists "
                f"({doc['lag_note']}); a newer one may exist if the company files early. Fiscal quarters differ by company, so "
                "the filing's own 'period of report' decides.")
    if kind == "year":
        end = _year_end(today, lag)
        return (f"Latest wanted as of {now.date}: a filing for a fiscal year ended on or before {end.isoformat()} certainly exists "
                f"({doc['lag_note']}); fiscal years differ by company, so the filing's own 'period of report' decides.")
    return f"Latest wanted as of {now.date}: take the newest by date, and check its event date as well as its filing date."


def _target(scope: TimeScope, now: Now) -> str:
    fallback = planning.setting("latest_fallback_note")
    if scope.recent:
        window = f"the last {scope.recency}" if scope.recency else "the most recent period"
        return (f"the latest, meaning the date or period closest to today ({now.date}); prefer items from {window}. {fallback}")
    if scope.intent == "timeline":
        return f"cover the whole window ({scope.window_start or '...'} to {scope.window_end or '...'}), not only the newest items."
    if scope.years:
        return (f"the period named in the question ({', '.join(map(str, scope.years))}); a source from another period is a "
                "fallback and must be labelled as such.")
    return "no particular date: age matters little, authority matters."


def build(question: str, checklist: list[str] | None, scope: TimeScope, now: Now) -> PlanningBrief:
    text = f"{question} {' '.join(checklist or [])}"
    typed = sorted(((_hits(t["route_when"], text), i, t) for i, t in enumerate(planning.catalog())), key=lambda x: (-x[0], x[1]))
    typed = [t for n, _, t in typed if n > 0][:2]
    docs = sorted(((_hits(d["route_when"], text), i, d) for i, d in enumerate(planning.documents())), key=lambda x: (-x[0], x[1]))
    docs = [d for n, _, d in docs if n > 0][:planning.setting("max_documents")]

    places, seen = [], set()
    for t in typed:
        for p in t["places"]:
            if p["site"] in seen:
                continue
            seen.add(p["site"])
            places.append(p)
    # within the same kind of information the catalog's own order is the author's ranking; across kinds, the strongest match leads
    places = places[:planning.setting("max_places")]
    metadata, kinds = [], [t["label"] for t in typed]
    for m in [m for t in typed for m in t["metadata"]] + [m for d in docs for m in d["metadata"]]:
        if m not in metadata:
            metadata.append(m)
    operators = tuple(p["operator"] for p in places if "<" not in p["operator"])
    sec_doc = next((d["id"] for d in docs if d["family"] == "sec"), None)
    brief_docs = tuple({"id": d["id"], "label": d["label"], "holds": d["holds"], "tip": d.get("tip", ""),
                        "lag_note": d["lag_note"], "expect": _expect(d, scope, now)} for d in docs)
    shown = tuple({k: p[k] for k in ("site", "operator", "role", "why", "connector") if k in p} for p in places)
    return PlanningBrief(tuple(kinds), shown, brief_docs, tuple(metadata[:8]), _target(scope, now), scope.recent, operators,
                         places[0]["operator"] if places and "<" not in places[0]["operator"] else None, sec_doc)


def ensure_sources(queries: list[str], brief: PlanningBrief, subject: str, scope: TimeScope, now: Now, limit: int) -> list[str]:
    """Make sure the plan reaches the best place for this kind of information. If none of the queries uses a site: operator from
    the brief, add one for the top concrete place (replacing the last query when the budget is full). Existing queries are kept."""
    if not brief.lead_operator:
        return list(queries)
    used = {m.lower() for q in queries for m in SITE.findall(q)}
    if any(op.split(":", 1)[1].lower() in used for op in brief.operators):
        return list(queries)
    extra = " ".join(x for x in (subject, brief.doc_hint, brief.lead_operator) if x)
    if scope.recent and not YEAR.search(extra):
        extra = apply_year_terms([extra], scope, now)[0]
    out = list(queries)
    if len(out) >= limit:
        out = out[:limit - 1]
    out.append(extra)
    return out

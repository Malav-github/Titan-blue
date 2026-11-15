"""The request advisor: a conversation on an 'Add a data source' request (settings: definitions/skills/advisor.json).

    reply(request, message)      -> what the person asked about, and an answer built only from what the request holds
    candidates(request)          -> the candidates the request found (at the gate, or kept with a failure)
    problems(request)            -> every failure the request recorded, in words
    diagnose(request)            -> each problem matched to a known cause, what to check, and what can be done next
    constraints(message, brief)  -> a new brief for "look for alternatives": free only, API or MCP only, cheaper

Everything here is code. The local model does not write these answers: it is unreliable at keeping to the facts it is
given, and an answer about cost or terms that drifts from the quote is worse than none. A question the request cannot
answer is offered as a look-up on the provider's own pages (CapabilityManager.look_up), where the model only picks a
passage and code checks its words are on the page.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

SETTINGS = PROJECT_ROOT / "definitions" / "skills" / "advisor.json"
GENERIC = {"api", "apis", "the", "open", "data", "server", "servers", "mcp", "service", "services", "web", "rest", "public", "official", "v1", "v2", "v3"}
ACTION_LABELS = {
    "alternatives": "Look for other sources",
    "retest": "Run the tests again",
    "retry": "Try the whole request again",
    "close": "Close this request",
    "look_up": "Look it up on the provider's own pages",
}
ACTION_ALLOWED = {
    "retest": {"tests_failed"},
    "retry": {"failed", "cancelled", "tests_failed"},
    "close": {"requested", "failed", "tests_failed", "awaiting_user"},
    "alternatives": {"awaiting_user", "failed", "cancelled", "tests_failed", "ready", "done"},
    "look_up": {"awaiting_user", "failed", "cancelled", "tests_failed", "ready", "done"},
}


@lru_cache(maxsize=1)
def settings() -> dict:
    d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    for g in d["diagnoses"]:
        re.compile(g["match"])
        unknown = set(g["actions"]) - set(ACTION_LABELS)
        if unknown:
            raise ValueError(f"advisor.json: {g['id']}: unknown actions {sorted(unknown)}")
    return d


def _has(text: str, words) -> bool:
    t = f" {text.lower()} "
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", t) for w in words)


# -- what the request holds ---------------------------------------------------------------------------

def candidates(request: dict) -> list[dict]:
    """The candidates as shown on the page: from the approval gate while it waits, else kept with the failure."""
    if request.get("gate"):
        return request["gate"].get("candidates") or []
    for e in reversed(request.get("events") or []):
        if e["event"] == "failed" and e["detail"].get("findings"):
            return e["detail"]["findings"]
    return []


def chosen(request: dict) -> str | None:
    gate = request.get("gate") or {}
    if gate.get("recommendation", {}).get("choice"):
        return gate["recommendation"]["choice"]
    for e in reversed(request.get("events") or []):
        if e["event"] == "recommended":
            return e["detail"].get("choice")
    return None


def problems(request: dict) -> list[dict]:
    """Each failure in the request's record: {where, text}. The newest attempt only: earlier steps that later passed are not problems."""
    out = []
    events = request.get("events") or []
    attempt = max((i for i, e in enumerate(events) if e["event"] in ("integrated", "retest")), default=0)
    for i, e in enumerate(events):
        d = e["detail"]
        if e["event"] == "test_item" and d.get("status") == "fail" and i > attempt:
            out.append({"where": f"Test: {d.get('item')}", "text": str(d.get("detail") or "")})
        elif e["event"] in ("failed", "cancelled") and (d.get("reason") or d.get("error")) and "closed by you" not in str(d.get("reason")):
            out.append({"where": "The request", "text": str(d.get("reason") or d.get("error"))})
        elif e["event"] == "llm_failed":
            out.append({"where": "A model call", "text": f"llm_failed: {d.get('error', '')}"})
        elif e["event"] == "candidate_skipped":
            out.append({"where": f"Candidate {d.get('candidate')}", "text": str(d.get("reason") or "")})
    for c in candidates(request):
        if c.get("cannot_integrate") and c.get("kind") != "mcp":
            out.append({"where": f"Connecting {c['name']}", "text": str(c["cannot_integrate"])})
    note = (request.get("gate") or {}).get("note")
    if note:
        out.append({"where": "The approval step", "text": note})
    return out


def diagnose(request: dict) -> list[dict]:
    """Each problem with the first known cause that matches it; unmatched problems are kept, said plainly."""
    out = []
    for p in problems(request):
        g = next((g for g in settings()["diagnoses"] if re.search(g["match"], p["text"], re.I)), None)
        out.append({**p, "diagnosis": g["id"] if g else None, "cause": g["cause"] if g else None,
                    "check": g["check"] if g else [], "actions": g["actions"] if g else [],
                    "model_limit": bool(g and g.get("limit") == "model")})
    return out


# -- reading the message ------------------------------------------------------------------------------

def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]{2,}", name.lower()) if w not in GENERIC]


def named(message: str, cands: list[dict]) -> list[dict]:
    """Candidates the message names: by the whole name, its id, or its first distinctive word ('EPO', 'FindIP')."""
    m = message.lower()
    hits = []
    for c in cands:
        words = _name_words(c["name"])
        if c["name"].lower() in m or c.get("connector_id", "~") in m or (words and _has(m, [words[0]])):
            hits.append(c)
    return hits


def topics(message: str) -> list[dict]:
    return [t for t in settings()["topics"] if _has(message, t["words"])]


def intent(message: str, request: dict) -> str:
    """alternatives | troubleshoot | status | question | help. The order matters: 'is there a free alternative?' asks for
    alternatives, not about cost; 'why did EPO fail?' is a problem, not a question about EPO."""
    for i in settings()["intents"]:
        if _has(message, i["words"]):
            return i["id"]
    if topics(message) or named(message, candidates(request)) or message.strip().endswith("?"):
        return "question"
    return "help"


def constraints(message: str, brief: dict | None) -> dict:
    """The brief for a new search, changed only where the message says so; the rest is kept as the person set it."""
    from skills import sourcing
    b = dict(brief or {})
    budgets = list(sourcing.settings()["budget_options"])             # free, up_to_10, ... any
    m = message.lower()
    if _has(m, ["free", "no cost", "without paying"]):
        b["budget"] = "free"
    elif _has(m, ["cheaper", "less expensive", "lower cost"]) and b.get("budget", "any") in budgets:
        i = budgets.index(b.get("budget", "any"))
        b["budget"] = budgets[max(0, (len(budgets) - 2 if b.get("budget", "any") == "any" else i - 1))]
    wants_mcp, wants_api = _has(m, ["mcp", "mcp server", "mcp servers"]), _has(m, ["api", "apis"])
    if wants_mcp and not wants_api:
        b["kind"] = "mcp"
    elif wants_api and not wants_mcp and _has(m, ["only", "just"]):
        b["kind"] = "api"
    if b.get("budget"):
        opt = sourcing.settings()["budget_options"][b["budget"]]
        b["budget_label"], b["budget_monthly_usd"] = opt["label"], opt["monthly_usd"]
    return b


# -- the answers --------------------------------------------------------------------------------------

def _action(aid: str, status: str, params: dict | None = None, label: str | None = None) -> dict | None:
    if status not in ACTION_ALLOWED[aid]:
        return None
    return {"id": aid, "label": label or ACTION_LABELS[aid], "params": params or {}}


def _actions(*items) -> list[dict]:
    seen, out = set(), []
    for a in items:
        if a and a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out


def _fit_line(c: dict) -> str:
    f = c.get("fit") or {}
    crit = f.get("criteria") or []
    meets = sum(x["verdict"] == "meets" for x in crit)
    unknown = sum(x["verdict"] == "unknown" for x in crit)
    bits = [c.get("kind_label") or ("MCP server" if c.get("kind") == "mcp" else "API")]
    if c.get("cost_label"):
        bits.append(c["cost_label"])
    if crit:
        bits.append(f"meets {meets} of {len(crit)} criteria, {unknown} unknown")
    if f.get("excluded"):
        bits.append("ruled out: " + "; ".join(f.get("exclusion_reasons") or []))
    elif c.get("cannot_integrate"):
        why = str(c["cannot_integrate"])
        bits.append(("not installable yet" if c.get("kind") == "mcp" else "cannot be connected")
                    + ("" if c.get("kind") == "mcp" else f": {why[:140]}{'…' if len(why) > 140 else ''}"))
    return f"{c['name']} ({', '.join(bits)})"


def _alternatives(request: dict, message: str) -> dict:
    cands, choice, status = candidates(request), chosen(request), request["status"]
    others = [c for c in cands if c.get("connector_id") != choice]
    new_brief = constraints(message, request.get("brief"))
    changed = [k for k in ("budget", "kind") if new_brief.get(k) != (request.get("brief") or {}).get(k)]
    points = [_fit_line(c) for c in others]
    if others:
        text = "Besides the recommendation, this request also looked at:" if choice else "This request looked at:"
    else:
        text = "This request found no other candidates."
    how = []
    if "budget" in changed:
        how.append(f"budget {new_brief.get('budget_label')}")
    if "kind" in changed:
        how.append("MCP servers only" if new_brief["kind"] == "mcp" else "APIs only")
    label = f"Look for more, leaving out these {len(cands)}" + (f" ({', '.join(how)})" if how else "") if cands else \
        "Look for sources again" + (f" ({', '.join(how)})" if how else "")
    params = {"exclude": [c["name"] for c in cands], "brief": new_brief}
    return {"intent": "alternatives", "text": text, "points": points, "facts": [],
            "notes": ["A new search starts as a new request; this one is kept as it is."],
            "actions": _actions(_action("alternatives", status, params, label))}


def _troubleshoot(request: dict) -> dict:
    status, found = request["status"], diagnose(request)
    if not found:
        text = {"researching": "Nothing has gone wrong so far; the request is still working.",
                "integrating": "The connection is being set up and tested now.",
                "ready": "The data source is active and its tests passed: nothing to fix.",
                "awaiting_user": "Nothing has failed: the request is waiting for your decision below."}.get(
            status, "The request has no recorded failure to explain.")
        return {"intent": "troubleshoot", "text": text, "points": [], "facts": [], "notes": [], "actions": []}
    points, notes, acts = [], [], []
    for p in found[-4:]:
        if p["cause"]:
            points.append(f"{p['where']}: {p['cause']} (recorded: “{p['text'][:160]}”)")
            notes.extend(x for x in p["check"] if x not in notes)
            acts.extend(p["actions"])
        else:
            points.append(f"{p['where']}: “{p['text'][:200]}”. This failure does not match a known cause.")
    if any(p["model_limit"] for p in found):
        notes.append("Part of this is a limit of the local model, not of the source. The workaround is listed above.")
    return {"intent": "troubleshoot", "text": "Here is what went wrong, and what to check:", "points": points, "facts": [],
            "notes": notes, "actions": _actions(*(_action(a, status) for a in acts))}


def _status(request: dict) -> dict:
    status, cands, choice = request["status"], candidates(request), chosen(request)
    name = next((c["name"] for c in cands if c.get("connector_id") == choice), choice)
    text = {
        "requested": "Nothing has been looked for yet. Start the request with your brief.",
        "researching": "WordBench is looking for candidates and reading their own pages. This takes a few minutes.",
        "awaiting_user": (f"WordBench recommends {name}. " if name else "No API fits your brief. ")
        + "It is waiting for you: read the terms, add a key if one is needed, then approve or close.",
        "integrating": "You approved a source; it is being connected and tested now.",
        "tests_failed": "The source was approved but its tests failed, so it is not active.",
        "ready": f"{name or 'The source'} is active and can be used by research runs.",
        "done": "The source is active, and the research run that asked for it carried on.",
        "failed": "No API could be connected for this request.",
        "cancelled": "This request is closed.",
    }.get(status, f"The request is {status}.")
    steps = list((request.get("gate") or {}).get("your_steps") or [])
    out = {"intent": "status", "text": text, "points": steps, "facts": [], "notes": [], "actions": []}
    if status in ("failed", "tests_failed"):
        t = _troubleshoot(request)
        out["points"], out["notes"], out["actions"] = t["points"], t["notes"], t["actions"]
    return out


def _question(request: dict, message: str) -> dict:
    status, cands, choice = request["status"], candidates(request), chosen(request)
    about = named(message, cands)
    about = about or cands                     # "is any of them free?": every candidate, each with its own quotes
    asked = topics(message)
    if not cands:
        return {"intent": "question", "text": "This request has not found any candidates yet, so there is nothing to answer from.",
                "points": [], "facts": [], "notes": [], "actions": []}
    facts, missing = [], []
    for c in about:
        fields = {f for t in asked for f in t["fields"]}
        crit = {k for t in asked for k in t["criteria"]}
        found = []
        for f in c.get("verified_facts") or []:
            if asked and f["field"] not in fields:
                continue
            same = next((x for x in found if x["quote"] == f["quote"]), None)
            if same:                                   # one quote backing two facts is shown once, with both labels
                same["label"] += f", {f['field'].replace('_', ' ')}"
                continue
            found.append({"candidate": c["name"], "label": f["field"].replace("_", " "), "value": f["value"], "quote": f["quote"],
                          "source_url": f["source_url"], "official": f.get("official", True)})
        for v in (c.get("fit") or {}).get("criteria") or []:
            if v["id"] in crit and v.get("quote") and not any(x["quote"] == v["quote"] for x in found):
                found.append({"candidate": c["name"], "label": v["label"], "value": f"{v['verdict']}: {v['why']}", "quote": v["quote"],
                              "source_url": v.get("source_url"), "official": True})
        facts.extend(found)
        if not found:
            missing.append(c)
    topic_words = ", ".join(t["label"] for t in asked) or "this"
    text = (f"What the pages WordBench read say about {topic_words}, word for word:" if facts
            else f"WordBench has no quoted fact on {topic_words} for {', '.join(c['name'] for c in about)}.")
    notes = ["Only facts with a quote from a page WordBench read are shown. A fact that is not here is unknown, not false."]
    if any(not f["official"] for f in facts):
        notes.append("Some quotes come from a site that is not the provider's own: treat those as secondary.")
    acts = [_action("look_up", status, {"candidate": c["name"], "docs_url": c.get("docs_url"), "question": message},
                    f"Look it up on {c['name']}'s own pages") for c in missing if c.get("docs_url")]
    return {"intent": "question", "text": text, "points": [], "facts": facts, "notes": notes, "actions": _actions(*acts)}


HELP = ("I can suggest other sources (“what else is there?”, “anything free?”), help when connecting fails (“why did the test "
        "fail?”), answer questions about a candidate from the quotes WordBench collected (“does EPO allow individuals?”, “what "
        "are its limits?”), and say where things stand (“what now?”). I never sign up, pay, accept terms or handle keys: those "
        "stay with you.")


def reply(request: dict, message: str) -> dict:
    message = " ".join((message or "").split())[:1000]
    kind = intent(message, request) if message else "help"
    if kind == "alternatives":
        return _alternatives(request, message)
    if kind == "troubleshoot":
        return _troubleshoot(request)
    if kind == "status":
        return _status(request)
    if kind == "question":
        return _question(request, message)
    return {"intent": "help", "text": HELP, "points": [], "facts": [], "notes": [], "actions": []}

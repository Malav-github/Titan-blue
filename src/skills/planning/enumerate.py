"""List questions (definitions/guidance/enumeration.json): 'What did S&P Global announce in 2026?' wants every distinct event,
not one fact. Code recognises them and writes the checklist item and the searches; nothing here is decided by a model."""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "enumeration.json"


@lru_cache(maxsize=1)
def config() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    for k in ("list_nouns", "list_cues", "announce_question", "singular"):
        re.compile(d[k], re.I)
    return d


def _topic(question: str, noun: str | None, subject: str) -> str:
    """The qualifier before the list noun ('AI' in 'What AI initiatives ...'), without the subject's own words."""
    if not noun:
        return ""
    m = re.search(rf"\b((?:[\w&.'’-]+\s+){{0,3}}){re.escape(noun)}\b", question, re.I)
    words = (m.group(1) if m else "").split()
    drop = {w.lower() for w in re.findall(r"[\w&.-]+", subject)} | {"what", "which", "list", "all", "every", "each", "the", "key", "major",
                                                                     "main", "recent", "did", "has", "have", "of", "its", "their", "how", "many",
                                                                     "latest", "newest", "new", "ytd", "so", "far"}   # time words, not the topic
    keep = [w for w in words if len(w) > 1 and w.lower().rstrip("'s") not in drop and not re.search(r"['’]s$", w.lower())]
    return " ".join(keep[-2:])


def inventory(question: str, subject: str) -> dict | None:
    """{kind: inventory, ...} when the question asks for the items of a portfolio (technologies, products, brands), else None."""
    cfg, q = config()["inventory"], question.strip()
    noun_m = re.search(rf"\b({cfg['nouns']})\b", q, re.I)
    if not noun_m:
        return None
    portfolio = re.search(cfg["portfolio_words"], q, re.I)
    events = re.search(config()["announce_question"], q, re.I) or re.search(r"\b(?:19|20)\d{2}\b|\b(?:ytd|this year|recent|latest)\b", q, re.I)
    if not (portfolio or (re.search(cfg["cues"], q, re.I) and not events)):
        return None                                  # 'What products did Acme launch in 2026?' is a list of events
    noun = noun_m.group(1).lower()
    topic = _topic(q, noun, subject)
    if portfolio and not topic:                      # 'portfolio of chemical recycling technologies': the words between
        m = re.search(rf"{cfg['portfolio_words']}\s+((?:[\w&-]+\s+){{0,3}}?){re.escape(noun_m.group(1))}", q, re.I)
        topic = " ".join(w for w in (m.group(1) if m else "").split() if w.lower() not in ("of", "the", "its", "their"))
    single = re.sub(r"ies$", "y", noun) if noun.endswith("ies") else noun.rstrip("s")
    what = f"{topic + ' ' if topic else ''}{single}"
    queries = [re.sub(r"\s{2,}", " ", t.format(subject=subject, topic=topic, noun=noun if noun.endswith("s") else noun + "s")).strip()
               for t in cfg["queries"]]
    return {"kind": "inventory", "noun": noun, "topic": topic, "what": what, "item": cfg["item"].format(what=what, subject=subject),
            "queries": list(dict.fromkeys(queries)), "budgets": cfg["budgets"], "checks_per_document": config()["checks_per_document"],
            "form": cfg["form"]}


def enumeration(question: str, subject: str, scope: dict | None) -> dict | None:
    """{noun, topic, what, item, queries} when the question asks for a list of events (or, `kind: inventory`, of a portfolio's
    items), else None."""
    cfg = config()
    q = question.strip()
    if re.search(cfg["singular"], q, re.I):
        return None                                   # 'the latest move' asks for one thing
    if found := inventory(q, subject):
        return found
    noun_m = re.search(rf"\b({cfg['list_nouns']})\b", q, re.I)
    announce = re.search(cfg["announce_question"], q, re.I)
    if not ((noun_m and re.search(cfg["list_cues"], q, re.I)) or announce):
        return None
    noun = noun_m.group(1).lower() if noun_m else None
    topic = _topic(q, noun, subject)
    what = f"{topic + ' ' if topic else ''}{noun or 'announcement'}".rstrip("s") if noun != "news" else f"{topic + ' ' if topic else ''}news item"
    years = sorted((scope or {}).get("years") or [])
    year = str(years[-1]) if years else ""
    window = f" in {years[0]}" if len(years) == 1 else (f" from {years[0]} to {years[-1]}" if years else "")
    vague = {"announcements", "news", "updates", "developments", "events", "changes", "moves", "milestones", "releases", "initiatives"}
    keyword = topic or (noun.rstrip("s") if noun and noun not in vague else "")
    queries = [re.sub(r"\s{2,}", " ", t.format(subject=subject, topic=keyword, year=year)).strip() for t in cfg["queries"]]
    return {"kind": "events", "noun": noun, "topic": topic, "what": what, "item": cfg["item"].format(what=what, subject=subject, window=window),
            "queries": list(dict.fromkeys(queries)), "budgets": cfg["budgets"], "checks_per_document": cfg["checks_per_document"],
            "form": cfg["form"]}

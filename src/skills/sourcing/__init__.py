"""The sourcing skill: from a person's brief to a ranked shortlist of data sources, APIs and MCP servers labelled apart.

    brief_from(inputs)                        -> the structured brief (definitions/ontology/data_sources.json brief_fields)
    keywords(brief)                           -> the words a candidate is matched on
    from_catalogue(brief)                     -> leads from WordBench's own catalogue (definitions/catalogue/providers.json)
    from_apis_guru(listing, brief)            -> API leads from the APIs.guru directory (its list.json, already fetched)
    from_mcp_registry(responses, brief)       -> MCP server leads from the official MCP Registry (its search results)
    fit(brief, candidate, facts, draft, text) -> each criterion's verdict with the evidence for it, and whether it is excluded
    rank(fits)                                -> best first: not excluded, most criteria met, fewest unknown

Everything here is code: a model only reads documentation for quoted facts (src/capability.py). A directory's description
is a lead, never evidence; cost, limits, identity and terms are judged only on quoted facts from the provider's own pages.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

ONTOLOGY = PROJECT_ROOT / "definitions" / "ontology" / "data_sources.json"
SETTINGS = PROJECT_ROOT / "definitions" / "skills" / "sourcing.json"
CATALOGUE = PROJECT_ROOT / "definitions" / "catalogue" / "providers.json"


@lru_cache(maxsize=1)
def ontology() -> dict:
    d = json.loads(ONTOLOGY.read_text(encoding="utf-8"))
    ids = [c["id"] for c in d["criteria"]]
    if len(ids) != len(set(ids)):
        raise ValueError("data_sources.json: a criterion appears twice")
    return d


@lru_cache(maxsize=1)
def settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def catalogue() -> list[dict]:
    d = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    kinds = {k["id"] for k in ontology()["kinds"]}
    for p in d["providers"]:
        if p["kind"] not in kinds:
            raise ValueError(f"providers.json: {p['id']}: unknown kind {p['kind']}")
        if not p["docs_url"].startswith("https://"):
            raise ValueError(f"providers.json: {p['id']}: docs_url must be https")
    return d["providers"]


def kind_label(kind: str) -> str:
    """'rest' (the manager's word) and 'api' are both an API; 'mcp' is an MCP server."""
    for k in ontology()["kinds"]:
        if kind in (k["id"], k["internal"]):
            return k["label"]
    return kind


def internal_kind(kind: str) -> str:
    return next((k["internal"] for k in ontology()["kinds"] if kind in (k["id"], k["internal"])), kind)


# --- the brief ----------------------------------------------------------------------------------------------------

def brief_from(inputs: dict) -> dict:
    """The structured brief from a form's inputs (or a run's need alone). Unknown or empty fields stay out."""
    budgets = settings()["budget_options"]
    budget_key = inputs.get("budget") or "any"
    lines = lambda v: [x.strip(" -•\t") for x in re.split(r"[\n;]+", v or "") if x.strip(" -•\t")]      # noqa: E731
    vol = inputs.get("volume")
    return {
        "need": " ".join(str(inputs.get("need") or "").split()),
        "fields": lines(inputs.get("fields")),
        "examples": lines(inputs.get("examples")),
        "coverage": " ".join(str(inputs.get("coverage") or "").split()),
        "kind": inputs.get("kind") if inputs.get("kind") in ("api", "mcp") else "either",
        "budget": budget_key if budget_key in budgets else "any",
        "budget_label": budgets.get(budget_key, budgets["any"])["label"],
        "budget_monthly_usd": budgets.get(budget_key, budgets["any"])["monthly_usd"],
        "volume": int(vol) if str(vol or "").strip().isdigit() and int(vol) > 0 else None,
        "freshness": inputs.get("freshness") if inputs.get("freshness") in ("daily", "realtime") else "any",
        "commercial_use": bool(inputs.get("commercial_use")),
        "identity_ok": bool(inputs.get("identity_ok")),
    }


_NONSENSE = {"yes", "no", "true", "false", "none", "n/a", "na", "null", "ok", "okay", "maybe", "unknown"}


def usable_need(text: str | None) -> bool:
    """A need someone could act on: at least two words with letters ('SEC filings'), not 'yes' (a planner once answered
    'yes' to 'does this need a data source?' and that became the request)."""
    t = " ".join(str(text or "").split())
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&-]+", t)]
    return t.lower().strip(".!") not in _NONSENSE and len(words) >= 2


def _words(text: str) -> list[str]:
    stop = set(settings()["stop_words"])
    return [w for w in re.findall(r"[a-z][a-z0-9&'-]{2,}", (text or "").lower()) if w not in stop]


def keywords(brief: dict) -> list[str]:
    """The words candidates are matched on, in the order the person wrote them: the need first (its first words are its
    topic), then the fields, then the coverage, without filler."""
    return list(dict.fromkeys(_words(" ".join([brief.get("need", ""), *brief.get("fields", []), brief.get("coverage", "")]))))


def topic(brief: dict, n: int = 2) -> list[str]:
    """The need's own first words: what it is about ('patent', 'family' in 'patent family records for European filings').
    A candidate must match one of them: 'filings' or 'publication' alone once brought in an LLC-filings service."""
    return list(dict.fromkeys(_words(brief.get("need", ""))))[:n]


def _base(w: str) -> str:
    """A word's singular: 'patents' -> 'patent', 'families' -> 'family', 'filings' -> 'filing'. Not a prefix: comparing
    first letters made 'applicant' meet 'application' (9% of all APIs) and 'publication' meet 'public'."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def overlap(words: list[str], text: str) -> list[str]:
    """Which of the words appear in the text, singular and plural alike."""
    have = {_base(w) for w in re.findall(r"[a-z][a-z0-9&'-]{2,}", (text or "").lower())}
    return [w for w in words if _base(w) in have]


def _relevant(brief: dict, hit: list[str]) -> bool:
    """The need's first word (its subject: 'patent'), or enough of the brief's words with one of its topic words. A short
    directory description ('Semantic patent search') names the subject and little else."""
    words, top = keywords(brief), topic(brief)
    if top and top[0] in hit:
        return True
    return len(hit) >= min(settings()["min_overlap"], len(words) or 1) and bool(set(hit) & set(top))


def _order(brief: dict, hit: list[str]) -> tuple:
    """Best lead first: one that names the need's subject, then the most of the brief's words."""
    top = topic(brief)
    return (bool(top) and top[0] in hit, len(hit))


# --- where candidates come from -----------------------------------------------------------------------------------

def _candidate(name: str, docs_url: str | None, kind: str, reason: str, found_in: str, **extra) -> dict:
    return {"name": name, "docs_url": docs_url, "kind": internal_kind(kind), "reason": reason, "found_in": found_in, **extra}


def from_catalogue(brief: dict) -> list[dict]:
    words = keywords(brief)
    out = []
    for p in catalogue():
        hit = overlap(words, " ".join([p["name"], *p["topics"]]))             # regions describe reach, not subject
        if _relevant(brief, hit):
            out.append((_order(brief, hit), _candidate(p["name"], p["docs_url"], p["kind"], f"in WordBench's catalogue; matches {', '.join(hit[:5])}",
                                             "WordBench catalogue", matched=hit)))
    return [c for _, c in sorted(out, key=lambda x: x[0], reverse=True)]


def from_apis_guru(listing: dict, brief: dict, limit: int = 8) -> list[dict]:
    """APIs.guru's list.json: {name: {preferred, versions: {v: {info: {title, description, x-apisguru-categories, x-origin},
    swaggerUrl, ...}}}}. Each match carries the spec URL, so the manifest can be built from the API's own spec."""
    words = keywords(brief)
    scored = []
    for key, entry in (listing or {}).items():
        v = (entry.get("versions") or {}).get(entry.get("preferred") or "")
        if not v:
            continue
        info = v.get("info") or {}
        text = " ".join([key, info.get("title") or "", (info.get("description") or "")[:600], " ".join(info.get("x-apisguru-categories") or [])])
        hit = overlap(words, text)
        if not _relevant(brief, hit):
            continue
        docs = ((info.get("x-origin") or [{}])[0] or {}).get("url") or (info.get("contact") or {}).get("url") or v.get("swaggerUrl")
        docs = (v.get("externalDocs") or {}).get("url") or docs
        scored.append((_order(brief, hit), _candidate(info.get("title") or key, docs, "api",
                                            f"in the APIs.guru directory; matches {', '.join(hit[:5])}", "APIs.guru",
                                            spec_url=v.get("swaggerUrl"), description=(info.get("description") or "")[:300], matched=hit)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:limit]]


def from_mcp_registry(responses: list[dict], brief: dict, limit: int = 6) -> list[dict]:
    """The official MCP Registry's search results: {servers: [{server: {name, title, description, websiteUrl, repository,
    remotes: [{type, url, headers: [{name, isRequired, isSecret}]}], packages: [{registryType, identifier}]}, _meta}]}.
    Only active, latest versions. What the registry says about transport and keys is shown as the registry's word."""
    words = keywords(brief)
    seen, scored = set(), []
    for resp in responses:
        for item in (resp or {}).get("servers") or []:
            srv, meta = item.get("server") or {}, ((item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {})
            name = srv.get("name") or ""
            if not name or name in seen or meta.get("status", "active") != "active" or meta.get("isLatest") is False:
                continue
            seen.add(name)
            text = " ".join([name.replace("/", " ").replace("-", " "), srv.get("title") or "", srv.get("description") or ""])
            hit = overlap(words, text)
            if not _relevant(brief, hit):
                continue
            remotes, packages = srv.get("remotes") or [], srv.get("packages") or []
            headers = [h for r in remotes for h in (r.get("headers") or [])]
            registry = {
                "id": name, "version": srv.get("version"),
                "transport": "remote" if remotes else "local" if packages else "unknown",
                "remote_url": remotes[0].get("url") if remotes else None,
                "packages": [f"{p.get('registryType')}: {p.get('identifier')}" for p in packages][:3],
                "needs_key": any(h.get("isRequired") and h.get("isSecret") for h in headers) or any(
                    e.get("isRequired") and e.get("isSecret") for p in packages for e in (p.get("environmentVariables") or [])),
                "key_note": next((h.get("description") for h in headers if h.get("isRequired")), None),
            }
            docs = srv.get("websiteUrl") or (srv.get("repository") or {}).get("url")
            scored.append((_order(brief, hit), _candidate(srv.get("title") or name, docs, "mcp",
                                                f"in the official MCP Registry; matches {', '.join(hit[:5])}", "MCP Registry",
                                                registry=registry, description=(srv.get("description") or "")[:300], matched=hit)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:limit]]


def mcp_queries(brief: dict, n: int = 2) -> list[str]:
    """The registry searches by substring: the need's topic words, one search each, singular (a 'patent' search finds
    'patents' too)."""
    return [_base(w) for w in topic(brief, n)]


# --- fit ----------------------------------------------------------------------------------------------------------

_PRICE = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d+)?)\s*(?:USD|EUR|GBP)?\s*(?:/|per|a|an)\s*(month|mo|year|yr|annum)", re.I)
_RATE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million|m)?\s*(?:requests?|calls?|queries|hits)\s*(?:/|per|a|an|each)\s*(second|sec|s|minute|min|hour|hr|day|month)", re.I)
_PER_DAY = {"second": 86_400, "sec": 86_400, "s": 86_400, "minute": 1_440, "min": 1_440, "hour": 24, "hr": 24, "day": 1, "month": 1 / 30}
_NON_COMMERCIAL = re.compile(r"\bnon-?commercial\b|\bpersonal use only\b|\bnot for commercial\b|\bacademic use only\b", re.I)


def monthly_price(text: str) -> float | None:
    """The lowest monthly price a quoted pricing statement gives ('$49 per month', '$300 a year' -> 25.0), or None."""
    prices = []
    for amount, unit in _PRICE.findall(text or ""):
        value = float(amount.replace(",", ""))
        prices.append(value / 12 if unit.lower() in ("year", "yr", "annum") else value)
    return min(prices) if prices else None


def calls_per_day(text: str) -> float | None:
    """The tightest daily allowance a quoted limit gives ('10 requests per second' -> 864,000; '1,000 calls a day' -> 1,000)."""
    found = []
    for amount, mult, unit in _RATE.findall(text or ""):
        n = float(amount.replace(",", "")) * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get((mult or "").lower(), 1)
        found.append(n * _PER_DAY[unit.lower()])
    return min(found) if found else None


def _v(cid: str, verdict: str, why: str, quote: str | None = None, source_url: str | None = None) -> dict:
    label = next(c["label"] for c in ontology()["criteria"] if c["id"] == cid)
    hard = next(c["hard"] for c in ontology()["criteria"] if c["id"] == cid)
    out = {"id": cid, "label": label, "verdict": verdict, "why": why, "hard": hard}
    if quote:
        out.update(quote=quote, source_url=source_url)
    return out


def fit(brief: dict, cand: dict, facts: list[dict], draft: dict | None, docs_text: str = "") -> dict:
    """Each criterion's verdict for one candidate, with the evidence. `facts`: quoted, support-checked facts
    ({field, value, quote, source_url}); `draft`: the manager's draft ({manifest, is_free, cost_label} or {error})."""
    by = {}
    for f in facts or []:
        by.setdefault(f["field"], f)
    kind = internal_kind(cand.get("kind", "rest"))
    out = []

    # kind
    wanted = brief.get("kind", "either")
    if wanted == "either" or internal_kind(wanted) == kind:
        out.append(_v("kind", "meets", f"{kind_label(kind)}" + ("" if wanted == "either" else ", as asked")))
    else:
        out.append(_v("kind", "fails", f"an {kind_label(kind)}, and the brief asks for an {kind_label(wanted)} only"))

    # coverage: the fields and the need's words in its documentation (or, failing that, its directory description)
    words = keywords(brief)
    text = f"{docs_text} {cand.get('description') or ''}"
    fields = brief.get("fields") or []
    if not text.strip():
        out.append(_v("coverage", "unknown", "no documentation could be read"))
    elif fields:
        found = [f for f in fields if overlap(keywords({"need": f}), text) or f.lower() in text.lower()]
        missing = [f for f in fields if f not in found]
        verdict = "meets" if not missing else "partly" if found else "fails"
        out.append(_v("coverage", verdict, (f"its documentation mentions {', '.join(found)}" if found else "its documentation mentions none of the fields")
                      + (f"; not mentioned: {', '.join(missing)}" if missing else "") + " (the tests check what it actually returns)"))
    else:
        hit = overlap(words, text)
        share = len(hit) / max(1, len(words))
        out.append(_v("coverage", "meets" if share >= 0.6 else "partly" if hit else "fails",
                      f"its documentation mentions {', '.join(hit[:8])}" if hit else "its documentation mentions none of the brief's terms"))

    # cost
    budget = brief.get("budget_monthly_usd")
    pricing = by.get("pricing") or by.get("free_tier")
    price = monthly_price(f"{(by.get('pricing') or {}).get('value', '')} {(by.get('pricing') or {}).get('quote', '')}")
    is_free = (draft or {}).get("is_free")
    if "is_free" not in (draft or {}):
        # no manifest (an MCP server, or an API that could not be drafted): judged on the quoted facts themselves
        paid = by.get("payment_required", {}).get("value", "").strip().lower().startswith("y") or bool(by.get("pricing"))
        is_free = True if by.get("free_tier") and not paid else False if paid else None
    label = (draft or {}).get("cost_label") or ("free (verified)" if by.get("free_tier") else "cost unknown")
    q = (pricing or {}).get("quote"), (pricing or {}).get("source_url")
    if is_free is False and not (draft or {}).get("cost_label"):
        label = "paid"
    if is_free is True:
        out.append(_v("cost", "meets", label if (draft or {}).get("cost_label") else "free (verified)", *q))
    elif budget == 0:
        out.append(_v("cost", "fails" if (is_free is False or price) else "unknown",
                      "paid, and the brief asks for free sources only" if (is_free is False or price) else "cost not verified; the brief asks for free only", *q))
    elif price is not None:
        ok = budget is None or price <= budget
        out.append(_v("cost", "meets" if ok else "fails",
                      f"from ${price:,.0f} a month, verified" + ("" if budget is None else f" ({'within' if ok else 'over'} {brief.get('budget_label')})"), *q))
    elif is_free is None and "trial" in label:
        out.append(_v("cost", "partly", "a free trial, then paid: the price was not verified", *q))
    else:
        out.append(_v("cost", "unknown", f"{label}: no price could be verified", *q))

    # storage of results (WordBench archives every response)
    st = by.get("terms_storage")
    if st:
        yes = st["value"].strip().lower().startswith("y")
        out.append(_v("storage", "meets" if yes else "fails",
                      "its terms allow keeping results" if yes else "its terms do not allow keeping results, and WordBench archives every response",
                      st["quote"], st["source_url"]))
    else:
        out.append(_v("storage", "unknown", "no quote found on keeping results: read its terms"))

    # commercial use
    if not brief.get("commercial_use"):
        out.append(_v("commercial", "meets", "not needed: the brief does not ask for commercial use"))
    else:
        bad = next((f for f in facts or [] if _NON_COMMERCIAL.search(f"{f.get('value', '')} {f.get('quote', '')}")), None)
        out.append(_v("commercial", "fails", "its terms limit use to non-commercial", bad["quote"], bad["source_url"]) if bad
                   else _v("commercial", "unknown", "no quote found on commercial use: read its terms"))

    # identity check
    idf = by.get("identity")
    needs_id = None if not idf else idf["value"].strip().lower().startswith("y")
    if needs_id is None:
        out.append(_v("identity", "unknown", "no quote found on identity checks"))
    elif not needs_id:
        out.append(_v("identity", "meets", "no identity check required", idf["quote"], idf["source_url"]))
    else:
        out.append(_v("identity", "meets" if brief.get("identity_ok") else "fails",
                      "requires an identity check" + (", which you can complete" if brief.get("identity_ok") else ", and the brief cannot complete one"),
                      idf["quote"], idf["source_url"]))

    # volume
    vol = brief.get("volume")
    rl = by.get("rate_limit") or by.get("free_tier")
    allowance = calls_per_day(f"{(rl or {}).get('value', '')} {(rl or {}).get('quote', '')}") if rl else None
    if not vol:
        out.append(_v("volume", "meets" if rl else "unknown", "no volume given" + (f"; its limit: {rl['value']}" if rl else ""),
                      *((rl["quote"], rl["source_url"]) if rl else (None, None))))
    elif allowance is None:
        out.append(_v("volume", "unknown", f"no verified limit to compare with {vol:,} calls a day"))
    else:
        out.append(_v("volume", "meets" if allowance >= vol else "partly",
                      f"about {allowance:,.0f} calls a day allowed, for {vol:,} expected", rl["quote"], rl["source_url"]))

    # authority
    manifest = (draft or {}).get("manifest") or {}
    if manifest.get("tier") == "regulatory":
        out.append(_v("authority", "meets", "an official or regulatory source"))
    elif cand.get("found_in") in ("WordBench catalogue", "named by you") or facts:
        out.append(_v("authority", "meets", "the provider's own service, researched from its own pages"))
    else:
        out.append(_v("authority", "partly", f"found in {cand.get('found_in') or 'a search'}; nothing verified from its own pages"))

    # can be connected now
    if kind == "mcp":
        why = next(k["why_not_yet"] for k in ontology()["kinds"] if k["id"] == "mcp")
        out.append(_v("installable", "fails", f"an MCP server: {why}"))
    elif manifest:
        out.append(_v("installable", "meets", f"a manifest for {manifest.get('base_url', '')} is ready to test"))
    else:
        out.append(_v("installable", "fails", (draft or {}).get("error") or "no manifest could be drafted"))

    excluded = [c for c in out if c["hard"] and c["verdict"] == "fails"]
    return {"criteria": out, "excluded": bool(excluded), "exclusion_reasons": [f"{c['label']}: {c['why']}" for c in excluded],
            "meets": sum(c["verdict"] == "meets" for c in out), "unknown": sum(c["verdict"] == "unknown" for c in out)}


def rank_key(f: dict) -> tuple:
    return (f["excluded"], -f["meets"], f["unknown"])

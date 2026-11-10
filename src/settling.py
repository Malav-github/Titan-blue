"""When one source is enough to answer (definitions/guidance/answer_settling.json, decided with the user).

A claim from a strong source settles its needed fact on its own, and the run answers at once: the company's own filing or
release, the other party's own filing or release in a deal, a regulatory filing (whoever filed it), or a Tier 1 news
publication. A claim from any other source needs claims from two independent sites. Read by code; nothing here asks a model.

    strength(profile, url, question, names) -> {"id", "label", "why"} | None     None: not strong, needs a second site
"""

import json
import re
from functools import lru_cache
from urllib.parse import urlparse

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "answer_settling.json"


@lru_cache(maxsize=1)
def rules() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    d["_by_id"] = {r["id"]: r for r in d["strong"]}
    for need in ("company_document", "deal_party_document", "regulatory_filing", "tier1_news"):
        if need not in d["_by_id"]:
            raise ValueError(f"answer_settling.json: rule {need} is missing")
    d["_deal"] = re.compile(rf"\b(?:{d['_by_id']['deal_party_document']['deal_cues']})\b", re.I)
    return d


def others_need_sites() -> int:
    return int(rules()["others_need_sites"])


def _host(url: str) -> str:
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _on(host: str, hosts) -> str | None:
    return next((h for h in hosts if host == h or host.endswith("." + h)), None)


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(?:inc|corp|corporation|ltd|limited|plc|llc|co|company|group|holdings?)\b\.?", "",
                                          (name or "").lower()))


def strength(profile: dict, url: str, question: str = "", names: list[str] | None = None) -> dict | None:
    """Why this source settles a fact on its own, or None. `profile` is the source's description (source_class.classify)."""
    r = rules()["_by_id"]
    host = _host(url)

    def strong(rule_id: str, why: str) -> dict:
        return {"id": rule_id, "label": r[rule_id]["label"], "why": why}

    if profile.get("tier") == "primary":
        return strong("company_document", f"{profile.get('label') or 'the company itself'}")
    if reg := _on(host, r["regulatory_filing"]["hosts"]):
        return strong("regulatory_filing", f"a document on {reg}" + (f" filed by {profile['issuer']}" if profile.get("issuer") else ""))
    if pub := _on(host, r["tier1_news"]["hosts"]):
        return strong("tier1_news", r["tier1_news"]["hosts"][pub])
    if party := _deal_party(profile, host, question, names or []):
        return strong("deal_party_document", f"{profile.get('document_label') or 'a document'} by {party}, a party to the deal asked about")
    return None


def _deal_party(profile: dict, host: str, question: str, names: list[str]) -> str | None:
    """The target, buyer or investor in the deal the question asks about, when this is that party's own document: a release
    or filing whose issuer the question names, on a wire service, or on a site whose name is the party's name."""
    if not question or not rules()["_deal"].search(question) or not profile.get("issuer_document"):
        return None
    issuer = profile.get("issuer") or ""
    if not issuer or any(_compact(issuer) == _compact(n) for n in names if n):
        return None                                   # the subject's own document is covered by company_document
    if _compact(issuer) not in _compact(question) or len(_compact(issuer)) < 3:
        return None
    site = host.split(".")[-2] if host.count(".") >= 1 else host
    if profile.get("venue") == "wire_service" or _compact(site) == _compact(issuer):
        return issuer
    return None

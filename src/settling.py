"""Source tiers, and when one source is enough to answer (definitions/guidance/answer_settling.json, the user's tiers).

Tier 1, the official note or document: the company's own filing or release (on its website, EDGAR, or a wire service such
as PR Newswire), the other involved company's own filing or release in a deal, or a regulatory filing whoever filed it.
Tier 2, a reputed top-tier publisher's report (Bloomberg, WSJ, Reuters, ...): an exclusive or a report of a possible
announcement, not the official document. Tier 3, everything else. Tiers 1 and 2 settle a fact on their own (Tier 1 is read
first); Tier 3 needs two independent sites. Read by code; nothing here asks a model.

    strength(profile, url, question, names) -> {"id", "tier", "label", "tier_label", "why"} | None    None: Tier 3
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
    for need in ("company_document", "deal_party_document", "regulatory_filing", "top_publisher"):
        if need not in d["_by_id"]:
            raise ValueError(f"answer_settling.json: rule {need} is missing")
    for rule in d["strong"]:
        if str(rule["tier"]) not in d["tiers"] or rule["tier"] not in d["settles_alone"]:
            raise ValueError(f"answer_settling.json: rule {rule['id']} has a tier that does not settle alone")
    d["_deal"] = re.compile(rf"\b(?:{d['_by_id']['deal_party_document']['deal_cues']})\b", re.I)
    return d


def others_need_sites() -> int:
    """Independent sites a Tier 3 fact needs."""
    return int(rules()["tier3_need_sites"])


def tier_label(tier: int) -> str:
    return rules()["tiers"][str(tier)]["label"]


def _host(url: str) -> str:
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _on(host: str, hosts) -> str | None:
    return next((h for h in hosts if host == h or host.endswith("." + h)), None)


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(?:inc|corp|corporation|ltd|limited|plc|llc|co|company|group|holdings?)\b\.?", "",
                                          (name or "").lower()))


def strength(profile: dict, url: str, question: str = "", names: list[str] | None = None, blog: dict | None = None) -> dict | None:
    """Why this source settles a fact on its own, or None. `profile` is the source's description (source_class.classify);
    `blog` the result of the blog check (`blog_kind` and tools.primary.brand_of_domain) when the page is a blog post."""
    r = rules()["_by_id"]
    host = _host(url)

    def strong(rule_id: str, why: str) -> dict:
        tier = r[rule_id]["tier"]
        return {"id": rule_id, "tier": tier, "label": r[rule_id]["label"], "tier_label": tier_label(tier), "why": why}

    if profile.get("tier") == "primary":
        return strong("company_document", f"{profile.get('label') or 'the company itself'}")
    if reg := _on(host, r["regulatory_filing"]["hosts"]):
        return strong("regulatory_filing", f"a document on {reg}" + (f" filed by {profile['issuer']}" if profile.get("issuer") else ""))
    if blog and blog.get("official") and (brand := blog.get("brand")):
        # the official blog of a major brand: its own note, Tier 1 when it is the company asked about or one the question names
        if any(_compact(brand) == _compact(n) or _compact(n) in _compact(brand) for n in names or [] if n and len(_compact(n)) > 2):
            return strong("company_document", f"the official blog of {brand} ({blog['why']})")
        if len(_compact(brand)) > 2 and _compact(brand) in _compact(question):
            return strong("official_blog", f"the official blog of {brand}, named in the question ({blog['why']})")
    if party := _deal_party(profile, host, question, names or []):
        return strong("deal_party_document", f"{profile.get('document_label') or 'a document'} by {party}, a party to the deal asked about")
    if pub := _on(host, r["top_publisher"]["hosts"]):
        return strong("top_publisher", f"reported by {r['top_publisher']['hosts'][pub]}; not the official document")
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


def blog_kind(url: str) -> str | None:
    """'platform' for a post on a blogging platform (Medium, Substack, LinkedIn posts, Forbes contributors: anyone can write
    there, so it is never taken as official), 'own_domain' for a blog on a site's own domain (to be checked: is it a major
    brand's official blog?), None when the page is not a blog."""
    b = rules()["blogs"]
    u = urlparse(url or "")
    host, path = _host(url), u.path or "/"
    if _on(host, b["platform_hosts"]):
        return "platform"
    for site, pattern in b["platform_paths"].items():
        if _on(host, [site]) and re.search(pattern, path):
            return "platform"
    if re.search(b["host_cues"], host) or re.search(b["path_cues"], path, re.I):
        return "own_domain"
    return None


def platform_note(url: str) -> dict:
    return {"official": False, "brand": None, "why": f"a post on {_host(url)}, a blogging platform where anyone can publish: not an official blog"}


def min_wikipedias() -> int:
    return int(rules()["blogs"]["major_brand_min_wikipedias"])

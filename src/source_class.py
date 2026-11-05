"""What kind of place a page is on (definitions/guidance/source_classes.json), and so how its claims are labelled.

Primary: the subject company speaking for itself (its site, its filings, its press releases, wherever they are hosted).
Secondary: anyone reporting on those. Reference works (Wikipedia and the like) serve general knowledge only, never a key
development or news about a company. This is advice about labels and about which candidates to read; a claim still needs a
verbatim quote from a page that was read.
"""

import json
import re
from functools import lru_cache
from urllib.parse import urlparse

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "source_classes.json"
_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|ltd|plc|llc|holdings|group|incorporated)\b\.?", re.I)


@lru_cache(maxsize=1)
def data() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    for c in d["classes"]:
        if c["tier"] not in ("primary", "secondary"):
            raise ValueError(f"source_classes.json: {c['id']}: tier must be primary or secondary")
    re.compile(d["development_cues"])
    return d


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _SUFFIX.sub(" ", (text or "").lower()))


def names_subject(text: str, names: list[str | None]) -> bool:
    """Does `text` (a title, snippet, URL or claim) name the subject, in any of the ways it is known?"""
    hay = _squash(text)
    return any(len(n := _squash(x or "")) >= 3 and n in hay for x in names)


def classify(url: str, official_domains: list[str] | None = None, *, text: str = "", names: list[str | None] | None = None) -> dict:
    """{id, label, tier} for a page. `text` is whatever is known about it (title, snippet, claim); `names` are the ways the subject is
    called. A filing or wire-service page is primary only when `text` or the URL names the subject (or no subject is known)."""
    host = _host(url)
    if official_domains and any(_on(host, d) for d in official_domains):
        return _out("company_site")
    for c in data()["classes"]:
        if not any(_on(host, h) for h in c["hosts"]):
            continue
        if c.get("needs_subject") and names and any(names) and not names_subject(f"{text} {url}", names):
            return {**_out(c["id"]), "tier": "secondary", "label": f"{c['label'].split(' (')[0]}, issuer not confirmed"}
        return _out(c["id"])
    return _out("other")


def label_of(class_id: str) -> str:
    return _out(class_id)["label"]


def _out(class_id: str) -> dict:
    c = next(x for x in data()["classes"] if x["id"] == class_id)
    return {"id": c["id"], "label": c["label"], "tier": c["tier"]}


def is_development(question: str, scope: dict | None = None) -> bool:
    """Does the question ask about a key development or news around a company? Then reference works are not used."""
    if (scope or {}).get("intent") == "latest":
        return True
    return bool(re.search(rf"\b(?:{data()['development_cues']})\b", (question or "").lower()))


def allowed(cls_id: str, development: bool) -> bool:
    return not (development and cls_id == "reference")


def attribution(cls: dict) -> str:
    """How the writer should credit it: what the company itself said, or what a publication reported."""
    if cls["tier"] == "primary":
        return "the company's own statement: say the company said, announced or filed it"
    if cls["id"] == "major_publication":
        return "a publication's report: name the publication ('Reuters reported')"
    return "a second-hand source: say it was reported, and by whom"

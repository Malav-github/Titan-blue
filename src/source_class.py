"""How a source's claims are labelled and used, from the source ontology (definitions/ontology/sources.json, read by
ontology.source_profile).

Primary: the subject company speaking for itself (its site, its filings, its press releases, wherever they are hosted).
Secondary: anyone reporting on those. Reference works (Wikipedia and the like) serve general knowledge only, never a key
development or news about a company (definitions/guidance/source_classes.json holds the cues for that). This is advice about
labels and about which candidates to read; a claim still needs a verbatim quote from a page that was read.
"""

import json
import re
from functools import lru_cache

import ontology
from config import PROJECT_ROOT
from ontology.source_profile import describe, names_subject  # noqa: F401  (names_subject is used by callers)

PATH = PROJECT_ROOT / "definitions" / "guidance" / "source_classes.json"


@lru_cache(maxsize=1)
def data() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    re.compile(d["development_cues"])
    return d


def classify(url: str, official_domains: list[str] | None = None, *, text: str = "", names: list[str | None] | None = None,
             title: str = "", ciks: list[str] | None = None) -> dict:
    """The source's description: {id (class), label, tier, issuer, document_type, venue, publisher, ...}. `text` is whatever is
    known about the page (its text once read; before that its title and snippet); `names` are the ways the subject is called.
    An issuer document hosted elsewhere (a filing on EDGAR, a release on a wire) is primary only when it is the subject's."""
    return describe(url, text, title=title, names=names, official_domains=official_domains, ciks=ciks)


def label_of(class_id: str) -> str:
    return next(c["label"] for c in ontology.sources()["classes"] if c["id"] == class_id)


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
        return f"a publication's report: name the publication ('{cls.get('publisher') or 'Reuters'} reported')"
    return "a second-hand source: say it was reported, and by whom"

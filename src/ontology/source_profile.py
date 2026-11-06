"""What a source is, by code: issuer, document type, venue and publisher, and from them the tier and the label
(definitions/ontology/sources.json).

    describe("https://investor.apple.com/.../apple-reports-fourth-quarter-results", text, names=["Apple Inc."], official_domains=["apple.com"])
    -> issuer Apple Inc., earnings release, investor relations site, primary,
       "Earnings release by Apple Inc., on its investor relations site"

The same release found on EDGAR keeps its issuer and type and changes venue ("..., on EDGAR"). Nothing here is read by a model.
"""

import re
from urllib.parse import urlparse

from ontology import sources as _data  # the loader function, not this module

_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|ltd|plc|llc|holdings|group|incorporated)\b\.?", re.I)
_EXHIBIT_99 = re.compile(r"ex-?99|exhibit-?99|ex991|dex99", re.I)


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _on(host: str, domain: str) -> bool:
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _site(host: str) -> str:
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _SUFFIX.sub(" ", (text or "").lower()))


def names_subject(text: str, names: list[str | None] | None) -> bool:
    """Does `text` (a title, snippet, URL, page or issuer name) name the subject, in any of the ways it is known?"""
    hay = _squash(text)
    return any(len(n := _squash(x or "")) >= 3 and n in hay for x in names or [])


def _venue(host: str, path: str, official: list[str]) -> dict:
    d = _data()
    venues = {v["id"]: v for v in d["venues"]}
    if any(_on(host, dom) for dom in official):
        first = host.split(".")[0]
        for vid in ("investor_relations", "company_newsroom"):
            v = venues[vid]
            if first in v["host_prefixes"] or re.search(v["path"], path, re.I):
                return v
        return venues["company_site"]
    for v in d["venues"]:
        if any(_on(host, h) for h in v.get("hosts", [])):
            return v
    return venues["other"]


def _publisher(venue: dict, host: str, subject: str) -> str:
    if venue.get("publisher") == "issuer":
        return subject or _site(host)
    if venue.get("publisher") and venue["publisher"] != "site":
        return venue["publisher"]
    for h, name in venue.get("publishers", {}).items():
        if _on(host, h):
            return name
    return _site(host) or "unknown site"


def _doc_type(venue: dict, url: str, title: str, head: str) -> tuple[dict, str]:
    """(document type, filed_as) for the page. Cover-page form names are matched in capitals ('FORM 10-K'), so a filing that
    only mentions another form in passing is not taken for it."""
    d = _data()
    path = urlparse(url or "").path.lower()
    lead = f"{title}\n{head[:300]}"
    exhibit = venue["id"] == "edgar" and bool(_EXHIBIT_99.search(path))
    for t in d["document_types"]:
        if venue["id"] not in t["venues"]:
            continue
        if exhibit and t.get("text"):          # an exhibit is not the form it is attached to
            continue
        hit = ((t.get("text") and venue["id"] == "edgar" and re.search(t["text"], head)) or
               (t.get("title") and re.search(t["title"], lead, re.I)) or
               (t.get("url") and re.search(t["url"], path, re.I)))
        if hit:
            return t, (t.get("filed_as", "Exhibit 99 to a filing") if exhibit else "")
    default = d["default_type"][venue["id"]]
    return next(t for t in d["document_types"] if t["id"] == default), ("Exhibit 99 to a filing" if exhibit else "")


def _registrant(words: str) -> str:
    """The name in the words before '(Exact name of registrant ...)': what follows the last label or number on the cover
    ('Commission File Number: 001-36743 Apple Inc.' -> 'Apple Inc.')."""
    tokens = words.split()
    cut = max((i + 1 for i, t in enumerate(tokens) if re.search(r"\d|:", t)), default=0)
    return " ".join(tokens[cut:]).strip(" ,:") or words.strip(" ,:")


def _head(venue: dict, text: str) -> str:
    """The opening of the document, where its type and issuer are stated. On EDGAR that is the cover page, which in an
    inline-XBRL filing comes after the filing's hidden data."""
    rules = _data()["issuer"]
    start = 0
    if venue["id"] == "edgar":
        at = text.find(rules["edgar_cover"], 0, rules["edgar_cover_within"])
        start = max(at, 0)
    return text[start:start + rules["head_chars"]]


def _issuer(venue: dict, url: str, title: str, text: str, head: str, names: list, subject: str, ciks: list[str]) -> tuple[str, str]:
    """(issuer, how it was found). Empty issuer when the page does not say whose document it is."""
    rules = _data()["issuer"]
    if venue.get("official"):
        return subject, "the subject's own site"
    if venue["id"] == "edgar":
        if m := re.search(rules["registrant"], head):
            return _registrant(m.group(1)), "registrant named on the filing's cover"
        if (m := re.search(rules["edgar_cik"], url or "")) and m.group(1) in {c.lstrip("0") for c in ciks if c}:
            return subject, f"filer CIK {m.group(1)} is the subject's"
        if names_subject(f"{title} {head} {url}", names):
            return subject, "the subject is named in the filing's title or opening"
        return "", ""
    if venue["id"] == "wire_service":
        found = re.findall(rules["wire_source"], text)
        if found:
            return found[-1].strip(" ,:"), "the release's SOURCE line"
        if names_subject(f"{title} {head} {url}", names):
            return subject, "the subject is named in the release's headline or opening"
        return "", ""
    return "", "publisher"


def _where(venue: dict, publisher: str) -> str:
    return _data()["labels"]["where"][venue["id"]].format(publisher=publisher)


def describe(url: str, text: str = "", *, title: str = "", names: list[str | None] | None = None,
             official_domains: list[str] | None = None, ciks: list[str] | None = None) -> dict:
    """The facets of one source and what follows from them. `text` is the page text (or, before a page is read, its search
    title and snippet); `names` are the ways the subject is called, first the one to print; `ciks` the subject's SEC CIKs."""
    names = [n for n in (names or []) if n]
    subject = names[0] if names else ""
    host, path = _host(url), urlparse(url or "").path
    venue = _venue(host, path, [d for d in official_domains or [] if d])
    publisher = _publisher(venue, host, subject)
    head = _head(venue, text)
    dtype, filed_as = _doc_type(venue, url, title, head)
    issuer, basis = _issuer(venue, url, title, text, head, names, subject, ciks or [])
    if not dtype["issuer_document"]:
        issuer, basis = (issuer or publisher), (basis or "publisher")
    is_subject = None if not names else (bool(issuer) and (issuer == subject or names_subject(issuer, names)))
    labels = _data()["labels"]
    fill = {"type": dtype["label"], "issuer": issuer, "publisher": publisher, "where": _where(venue, publisher)}
    if dtype["issuer_document"] and issuer and is_subject is not False:
        tier, label = "primary", labels["primary"].format(**fill)
    elif dtype["issuer_document"] and issuer:
        tier, label = "secondary", labels["secondary_issuer_document"].format(**fill)
    elif dtype["issuer_document"]:
        tier, label = "secondary", labels["unconfirmed"].format(**fill)
    else:
        tier, label = "secondary", labels["reported"].format(**fill)
    if filed_as and tier == "primary":
        label += f" (filed as {filed_as})"
    cls = next(c for c in _data()["classes"] if c["id"] == venue["class"])
    return {"id": cls["id"], "class_label": cls["label"], "tier": tier, "label": label,
            "issuer": issuer, "issuer_basis": basis, "is_subject": is_subject,
            "document_type": dtype["id"], "document_label": dtype["label"], "issuer_document": dtype["issuer_document"], "filed_as": filed_as,
            "venue": venue["id"], "venue_label": venue["label"], "publisher": publisher}

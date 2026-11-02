"""Primary sources: find a company's official domain and the pages it publishes itself.

Nothing here is trusted blindly. Domain candidates carry the evidence for them, robots.txt is
obeyed, and every discovery file (robots.txt, sitemaps) is archived like any other evidence.
"""

import gzip
import io
import re
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from tools.archive import USER_AGENT, archive_bytes, check_public_url, utc_now
from tools.web import download

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
MAX_SITEMAPS = 6
MAX_SITEMAP_DOWNLOAD = 30_000_000  # large sites split sitemaps into multi-MB partitions
MAX_SITEMAP_BYTES = 60_000_000  # decompressed cap, guards against gzip bombs

# Page categories in priority order; first matching pattern wins
CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("pricing", re.compile(r"/(pricing|plans?|prices?)(/|$|\.)", re.I)),
    ("changelog", re.compile(r"/(changelog|release-notes|releases|whats-new|updates)(/|$|\.)", re.I)),
    ("press", re.compile(r"/(press|newsroom|news|media)(/|$|\.)", re.I)),
    ("about", re.compile(r"/(about|company|team|leadership|about-us)(/|$|\.)", re.I)),
    ("careers", re.compile(r"/(careers|jobs|join-us|work-with-us)(/|$|\.)", re.I)),
    ("customers", re.compile(r"/(customers|case-studies|stories|success-stories)(/|$|\.)", re.I)),
    ("security", re.compile(r"/(security|trust|compliance|privacy)(/|$|\.)", re.I)),
    ("docs", re.compile(r"/(docs|documentation|developers|api)(/|$|\.)", re.I)),
    ("blog", re.compile(r"/(blog|insights|articles)(/|$|\.)", re.I)),
    ("product", re.compile(r"/(product|products|platform|solutions|features)(/|$|\.)", re.I)),
]
PROBE_PATHS = ["/pricing", "/about", "/blog", "/changelog", "/careers", "/press", "/customers", "/security"]


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_official(url: str, official_domains: list[str]) -> bool:
    """True if the URL's host is an official domain or a subdomain of one."""
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in official_domains)


def tier_for(url: str, official_domains: list[str]) -> str:
    return "primary" if is_official(url, official_domains) else "secondary"


LOCALE = re.compile(r"^/([a-z]{2}(?:-[a-z]{2})?)(?=/|$)", re.I)
PREFERRED_LOCALES = {"", "en", "us", "en-us"}


def locale_key(url: str) -> tuple[str, str]:
    """(url without its leading locale segment, that locale). /gb/pricing and /in/pricing share a key."""
    p = urlparse(url)
    m = LOCALE.match(p.path)
    locale = m.group(1).lower() if m else ""
    path = p.path[m.end():] if m else p.path
    return f"{host_of(url)}{path or '/'}?{p.query}", locale


def categorize(url: str) -> str | None:
    path = urlparse(url).path or "/"
    for name, pattern in CATEGORIES:
        if pattern.search(path):
            return name
    return None


# --- Domain resolution ------------------------------------------------------------------------

def _wikidata(params: dict) -> dict:
    resp = httpx.get(WIKIDATA_API, params={**params, "format": "json"}, timeout=20,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


def resolve_domain(company: str, limit: int = 5) -> dict:
    """Candidate official domains from Wikidata's 'official website' property, each with its evidence.

    `confirmed` needs an exact label match, a homepage that mentions the company, and no other
    candidate passing the same checks on a DIFFERENT domain. Several passing on different domains
    (e.g. two things called "Notion") are all `ambiguous`; everything else is `needs_review`. A person or a later step must decide those.
    """
    found = _wikidata({"action": "wbsearchentities", "search": company, "language": "en",
                       "type": "item", "limit": limit}).get("search", [])
    ids = [f["id"] for f in found]
    entities = _wikidata({"action": "wbgetentities", "ids": "|".join(ids), "props": "claims",
                          "languages": "en"}).get("entities", {}) if ids else {}

    candidates = []
    for f in found:
        claims = entities.get(f["id"], {}).get("claims", {}).get("P856", [])
        sites = [c["mainsnak"]["datavalue"]["value"] for c in claims
                 if c.get("mainsnak", {}).get("datavalue")]
        if not sites:
            continue
        domain = host_of(sites[0])
        evidence = [f"Wikidata {f['id']} '{f['label']}' ({f.get('description', 'no description')}) "
                    f"lists official website {sites[0]}"]
        exact = f["label"].strip().lower() == company.strip().lower()
        evidence.append("label matches the name exactly" if exact else "label differs from the name")
        mentions = False
        try:
            home = download(sites[0], timeout=15)
            text = home.body.decode(home.encoding, errors="replace")
            mentions = company.lower() in text.lower()
            evidence.append(f"homepage fetched (HTTP {home.status}); "
                            + ("mentions the company name" if mentions else "does not mention the company name"))
        except Exception as exc:
            evidence.append(f"homepage fetch failed: {type(exc).__name__}: {exc}")
        candidates.append({
            "domain": domain, "website": sites[0], "wikidata_id": f["id"],
            "description": f.get("description"), "evidence": evidence,
            "status": "confirmed" if exact and mentions else "needs_review",
        })
    passing = [c for c in candidates if c["status"] == "confirmed"]
    if len({c["domain"] for c in passing}) > 1:  # several entities sharing ONE domain is not ambiguity
        for c in passing:
            c["status"] = "ambiguous"
            c["evidence"].append(f"{len(passing)} entities with this name pass the checks on different domains; cannot tell which is meant")
    return {"company": company, "resolved_at": utc_now(), "candidates": candidates}


# --- Page discovery ---------------------------------------------------------------------------

def _locs(xml: str) -> list[str]:
    # A regex, not an XML parser: no entity expansion to worry about, and we only need <loc> values
    return [m.strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, flags=re.S | re.I)]


def _sitemap_text(url: str) -> tuple[str, str] | None:
    dl = download(url, timeout=30, max_bytes=MAX_SITEMAP_DOWNLOAD)
    if dl.status != 200:
        return None
    body = dl.body
    if body[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            body = gz.read(MAX_SITEMAP_BYTES + 1)
        if len(body) > MAX_SITEMAP_BYTES:
            return None
    sha, _ = archive_bytes(body, ".xml")
    return body.decode("utf-8", errors="replace"), sha


def discover_pages(domain: str, max_pages: int = 40, per_category: int = 6) -> dict:
    """Find the company's own pages: sitemaps first, then homepage links, then a few guessed paths."""
    base = f"https://{domain}"
    check_public_url(base)
    archived: list[dict] = []
    notes: list[str] = []

    # robots.txt: the rules we follow and often the sitemap locations
    robots = RobotFileParser()
    sitemap_urls: list[str] = []
    try:
        dl = download(f"{base}/robots.txt", timeout=15)
        if dl.status == 200:
            sha, _ = archive_bytes(dl.body, ".txt")
            archived.append({"what": "robots.txt", "sha256": sha})
            robots_text = dl.body.decode(dl.encoding, errors="replace")
            robots.parse(robots_text.splitlines())
            sitemap_urls = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_text)
        else:
            robots.parse([])  # no usable robots.txt: nothing disallowed
            notes.append(f"robots.txt returned HTTP {dl.status}")
    except Exception as exc:
        robots.parse([])
        notes.append(f"robots.txt unavailable: {type(exc).__name__}")
    sitemap_urls = sitemap_urls or [f"{base}/sitemap.xml"]

    urls: dict[str, str] = {}  # url -> how we found it

    # Sitemaps, including one level of index files
    queue, seen = list(sitemap_urls), set()
    while queue and len(seen) < MAX_SITEMAPS:
        sm = queue.pop(0)
        if sm in seen or not is_official(sm, [domain]):
            continue
        seen.add(sm)
        try:
            got = _sitemap_text(sm)
        except Exception as exc:
            notes.append(f"sitemap {sm} failed: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        if not got:
            continue
        text, sha = got
        archived.append({"what": sm, "sha256": sha})
        for loc in _locs(text):
            if re.search(r"\.xml(\.gz)?$", loc, re.I):
                queue.append(loc)
            else:
                urls.setdefault(loc, "sitemap")

    # Homepage links and feed autodiscovery
    feeds: list[str] = []
    try:
        home = download(base, timeout=15)
        if "html" in home.content_type:
            sha, _ = archive_bytes(home.body, ".html")
            archived.append({"what": "homepage", "sha256": sha})
            soup = BeautifulSoup(home.body, "html.parser")
            for link in soup.find_all("link", rel="alternate"):
                if link.get("type") in ("application/rss+xml", "application/atom+xml") and link.get("href"):
                    feeds.append(urljoin(home.final_url, link["href"]))
            for a in soup.find_all("a", href=True):
                urls.setdefault(urljoin(home.final_url, a["href"]).split("#")[0], "homepage")
    except Exception as exc:
        notes.append(f"homepage unavailable: {type(exc).__name__}")

    by_key: dict[str, dict] = {}  # one entry per page, whatever the locale
    blocked = 0
    for url, how in urls.items():
        category = categorize(url)
        if not category or not is_official(url, [domain]):
            continue
        if not robots.can_fetch(USER_AGENT, url):
            blocked += 1
            continue
        key, locale = locale_key(url)
        current = by_key.get(key)
        if current is None or (locale in PREFERRED_LOCALES and current["locale"] not in PREFERRED_LOCALES):
            by_key[key] = {"url": url, "category": category, "found_via": how, "locale": locale}
    pages = {p["url"]: p for p in by_key.values()}

    # Probe a few conventional paths for categories we still have nothing for
    have = {p["category"] for p in pages.values()}
    for path in PROBE_PATHS:
        url = base + path
        if categorize(url) in have or not robots.can_fetch(USER_AGENT, url):
            continue
        try:
            dl = download(url, timeout=10)
        except Exception:
            continue
        if dl.status == 200 and is_official(dl.final_url, [domain]):
            pages.setdefault(dl.final_url, {"url": dl.final_url, "category": categorize(url), "found_via": "probe"})
            have.add(categorize(url))

    order = [name for name, _ in CATEGORIES]
    ranked, counts = [], {}
    for p in sorted(pages.values(), key=lambda p: (order.index(p["category"]), len(p["url"]))):
        counts[p["category"]] = counts.get(p["category"], 0) + 1
        if counts[p["category"]] <= per_category:  # keeps one noisy category from crowding out the rest
            ranked.append(p)
    missing = [c for c in ("pricing", "about", "blog", "careers", "press") if c not in have]
    return {
        "domain": domain, "discovered_at": utc_now(), "tier": "primary",
        "pages": ranked[:max_pages], "total_candidates": len(pages),
        "feeds": feeds[:10], "blocked_by_robots": blocked,
        "categories_not_found": missing, "archived_discovery_files": archived, "notes": notes,
    }

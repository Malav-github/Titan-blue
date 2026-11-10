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

import policy
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


_TWO_LEVEL = {"co.uk", "org.uk", "co.in", "net.in", "org.in", "gov.in", "com.ph", "gov.ph", "org.ph", "com.au", "co.jp",
              "com.sg", "com.cn", "co.kr", "com.br", "co.za", "com.mx", "com.hk", "com.tw", "co.id", "com.my"}


def registrable(host: str) -> str:
    """The domain a company registers ('nvidia.com' for blogs.nvidia.com, 'tata.co.in' for blog.tata.co.in)."""
    parts = host.lower().removeprefix("www.").split(".")
    return ".".join(parts[-3:]) if ".".join(parts[-2:]) in _TWO_LEVEL else ".".join(parts[-2:])


def brand_label(host: str) -> str:
    """The brand a host is named for: 'nvidia' (blogs.nvidia.com), 'google' (blog.google, a brand's own top-level domain)."""
    reg = registrable(host)
    first = reg.split(".")[0]
    if first in ("blog", "blogs") and "." in reg:
        return reg.split(".")[-1]
    return first


def brand_of_domain(host: str, min_wikipedias: int = 10, limit: int = 5) -> dict:
    """Is `host` the domain of a major brand? From Wikidata: an item found by the host's brand name whose 'official website'
    (P856) is on the same registrable domain (or, for a brand's own top-level domain like .google, whose official site is
    named for it), with articles in at least `min_wikipedias` Wikipedias. Returns {"official", "brand", "id",
    "official_website", "wikipedias", "why"}; never raises (a failed lookup says so, and the blog is not counted as official)."""
    label = brand_label(host)
    try:
        found = _wikidata({"action": "wbsearchentities", "search": label, "language": "en", "type": "item", "limit": limit}).get("search", [])
        ids = [f["id"] for f in found]
        entities = _wikidata({"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|sitelinks|labels",
                              "languages": "en"}).get("entities", {}) if ids else {}
    except Exception as exc:
        return {"official": False, "brand": None, "why": f"the brand behind {host} could not be looked up ({type(exc).__name__})"}
    dot_brand = host.lower().endswith("." + label) and registrable(host).startswith(("blog.", "blogs."))
    seen = []
    for f in found:
        ent = entities.get(f["id"], {})
        sites = [c["mainsnak"]["datavalue"]["value"] for c in ent.get("claims", {}).get("P856", []) if c.get("mainsnak", {}).get("datavalue")]
        for site in sites:
            site_host = host_of(site)
            same = registrable(site_host) == registrable(host) or (
                dot_brand and (brand_label(site_host) == label or site_host.endswith("." + label)))     # about.google, blog.google
            if not same:
                continue
            wikis = sum(1 for k in ent.get("sitelinks", {}) if k.endswith("wiki") and k not in ("commonswiki", "specieswiki", "metawiki",
                                                                                                  "wikidatawiki", "mediawikiwiki"))
            name = ent.get("labels", {}).get("en", {}).get("value") or f.get("label") or label
            seen.append({"brand": name, "id": f["id"], "official_website": site, "wikipedias": wikis})
            break
    # several items may list the same domain (Google and 'Google Knowledge Graph'): the brand itself is the one named exactly
    # for the domain, else the best-known one
    seen.sort(key=lambda i: (re.sub(r"[^a-z0-9]", "", i["brand"].lower()) != label, -i["wikipedias"]))
    if seen and seen[0]["wikipedias"] >= min_wikipedias:
        i = seen[0]
        return {**i, "official": True, "why": f"Wikidata {i['id']} '{i['brand']}' lists {i['official_website']} as its official website, "
                                              f"with articles in {i['wikipedias']} Wikipedias"}
    if seen:
        s = seen[0]
        return {**s, "official": False, "why": f"{host} is {s['brand']}'s own domain (Wikidata {s['id']}), but with articles in only "
                                              f"{s['wikipedias']} Wikipedias it is not a major brand by Titan's measure"}
    return {"official": False, "brand": None, "why": f"no Wikidata organisation lists {registrable(host)} as its official website"}


def major_brand(name: str, min_wikipedias: int = 10, limit: int = 5) -> dict:
    """Is `name` (a publication's handle on a blogging platform, 'netflix') a major brand? A Wikidata item named exactly so,
    with articles in at least `min_wikipedias` Wikipedias. It says nothing about who runs the account. Never raises."""
    want = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if len(want) < 3:
        return {"major": False, "brand": None, "why": f"'{name}' is too short to be a brand's name"}
    try:
        found = _wikidata({"action": "wbsearchentities", "search": name, "language": "en", "type": "item", "limit": limit}).get("search", [])
        exact = [f for f in found if re.sub(r"[^a-z0-9]", "", f.get("label", "").lower()) == want]
        entities = _wikidata({"action": "wbgetentities", "ids": "|".join(f["id"] for f in exact), "props": "sitelinks|labels|claims",
                              "languages": "en"}).get("entities", {}) if exact else {}
    except Exception as exc:
        return {"major": False, "brand": None, "why": f"'{name}' could not be looked up ({type(exc).__name__})"}
    not_brands = {"Q202444", "Q101352", "Q4167410", "Q12308941", "Q11879590", "Q3409032"}   # given names, family names, disambiguations
    best = None
    for f in exact:
        claims = entities.get(f["id"], {}).get("claims", {})
        kinds = {c["mainsnak"]["datavalue"]["value"]["id"] for c in claims.get("P31", []) if c.get("mainsnak", {}).get("datavalue")}
        if kinds & not_brands or not claims.get("P856"):
            continue                       # a person's name or a word, not an organisation with its own website
        wikis = sum(1 for k in entities.get(f["id"], {}).get("sitelinks", {}) if k.endswith("wiki") and k not in (
            "commonswiki", "specieswiki", "metawiki", "wikidatawiki", "mediawikiwiki"))
        if best is None or wikis > best[1]:
            best = (f, wikis)
    if best and best[1] >= min_wikipedias:
        f, wikis = best
        return {"major": True, "brand": f["label"], "id": f["id"], "wikipedias": wikis,
                "why": f"Wikidata {f['id']} '{f['label']}' has articles in {wikis} Wikipedias"}
    return {"major": False, "brand": None, "why": f"no major brand named '{name}' on Wikidata"}


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
    policy.before_request(base, kind="crawl")     # some sites forbid automated crawling outright
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

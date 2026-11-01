import gzip

import pytest

from tools import primary
from tools.web import Download

ROBOTS = "User-agent: *\nDisallow: /private/\nSitemap: https://acme.com/sitemap.xml\n"
INDEX = "<sitemapindex><sitemap><loc>https://acme.com/sm-pages.xml.gz</loc></sitemap></sitemapindex>"
PAGES = """<urlset>
<url><loc>https://acme.com/pricing</loc></url>
<url><loc>https://acme.com/blog/launch-post</loc></url>
<url><loc>https://acme.com/private/pricing</loc></url>
<url><loc>https://evil.com/pricing</loc></url>
<url><loc>https://acme.com/random-page</loc></url>
</urlset>"""
HOME = ('<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>'
        '<body><a href="/about-us">About</a><a href="https://other.com/careers">x</a></body></html>')


def fake_download(routes):
    def _download(url, timeout=20.0, max_bytes=0):
        if url in routes:
            body, ctype = routes[url]
            return Download(body, 200, url, ctype, "utf-8")
        return Download(b"", 404, url, "text/html", "utf-8")
    return _download


@pytest.fixture
def acme(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(primary, "check_public_url", lambda url: None)
    routes = {
        "https://acme.com/robots.txt": (ROBOTS.encode(), "text/plain"),
        "https://acme.com/sitemap.xml": (INDEX.encode(), "application/xml"),
        "https://acme.com/sm-pages.xml.gz": (gzip.compress(PAGES.encode()), "application/gzip"),
        "https://acme.com": (HOME.encode(), "text/html"),
    }
    monkeypatch.setattr(primary, "download", fake_download(routes))
    return routes


def test_categorize_and_official_matching():
    assert primary.categorize("https://acme.com/en/pricing/") == "pricing"
    assert primary.categorize("https://acme.com/random-page") is None
    assert primary.is_official("https://docs.acme.com/x", ["acme.com"])
    assert not primary.is_official("https://acme.com.evil.io/x", ["acme.com"])
    assert primary.tier_for("https://news.site/acme", ["acme.com"]) == "secondary"


def test_discover_pages_follows_index_gzip_and_robots(acme):
    out = primary.discover_pages("acme.com")
    urls = {p["url"]: p["category"] for p in out["pages"]}
    assert urls["https://acme.com/pricing"] == "pricing"
    assert urls["https://acme.com/blog/launch-post"] == "blog"
    assert urls["https://acme.com/about-us"] == "about"           # from homepage link
    assert "https://acme.com/private/pricing" not in urls   # robots.txt disallow
    assert out["blocked_by_robots"] == 1
    assert not any("evil.com" in u or "other.com" in u for u in urls)  # off-domain excluded
    assert out["feeds"] == ["https://acme.com/feed.xml"]
    assert "careers" in out["categories_not_found"]
    assert {a["what"] for a in out["archived_discovery_files"]} >= {"robots.txt", "https://acme.com/sitemap.xml"}


def test_discover_pages_probes_when_sitemap_missing(acme):
    acme.pop("https://acme.com/sitemap.xml")
    acme["https://acme.com/careers"] = (b"<html>jobs</html>", "text/html")
    out = primary.discover_pages("acme.com")
    careers = [p for p in out["pages"] if p["category"] == "careers"]
    assert careers and careers[0]["found_via"] == "probe"


def test_resolve_domain_confirms_only_with_exact_label_and_homepage_mention(monkeypatch):
    def fake_wikidata(params):
        if params["action"] == "wbsearchentities":
            return {"search": [
                {"id": "Q1", "label": "Acme", "description": "software company"},
                {"id": "Q2", "label": "Acme Corp (film)", "description": "1999 film"},
            ]}
        site = lambda u: [{"mainsnak": {"datavalue": {"value": u}}}]  # noqa: E731
        return {"entities": {
            "Q1": {"claims": {"P856": site("https://www.acme.com/")}},
            "Q2": {"claims": {"P856": site("https://acmefilm.example/")}},
        }}

    monkeypatch.setattr(primary, "_wikidata", fake_wikidata)
    monkeypatch.setattr(primary, "download", fake_download({
        "https://www.acme.com/": (b"<html>Welcome to Acme</html>", "text/html"),
        "https://acmefilm.example/": (b"<html>A film site</html>", "text/html"),
    }))
    out = primary.resolve_domain("Acme")
    by_id = {c["wikidata_id"]: c for c in out["candidates"]}
    assert by_id["Q1"]["domain"] == "acme.com" and by_id["Q1"]["status"] == "confirmed"
    assert by_id["Q2"]["status"] == "needs_review"


@pytest.mark.network
def test_live_stripe():
    from config import TITAN_CONTACT
    if not TITAN_CONTACT:
        pytest.skip("set TITAN_CONTACT in .env: Wikidata rejects bots without contact details")
    out = primary.resolve_domain("Stripe")
    confirmed = [c for c in out["candidates"] if c["status"] == "confirmed"]
    assert confirmed and confirmed[0]["domain"] == "stripe.com", out
    pages = primary.discover_pages("stripe.com")
    assert any(p["category"] == "pricing" for p in pages["pages"]), pages["notes"]


def test_locale_variants_collapse_to_one_page_preferring_default():
    a, la = primary.locale_key("https://acme.com/gb/pricing")
    b, lb = primary.locale_key("https://acme.com/pricing")
    c, lc = primary.locale_key("https://acme.com/en-us/pricing")
    assert a == b == c and (la, lb, lc) == ("gb", "", "en-us")
    assert primary.locale_key("https://acme.com/blog/a")[1] == ""  # "blog" is not a locale


def test_per_category_cap_keeps_other_categories(acme):
    pages = "".join(f"<url><loc>https://acme.com/press/story-{i}/</loc></url>" for i in range(30))
    acme["https://acme.com/sm-pages.xml.gz"] = (gzip.compress(f"<urlset>{pages}</urlset>".encode()), "x")
    out = primary.discover_pages("acme.com", per_category=3)
    cats = [p["category"] for p in out["pages"]]
    assert cats.count("press") == 3 and "about" in cats


def test_same_name_entities_are_ambiguous_not_confirmed(monkeypatch):
    site = lambda u: [{"mainsnak": {"datavalue": {"value": u}}}]  # noqa: E731

    def fake_wikidata(params):
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q1", "label": "Notion", "description": "productivity software"},
                               {"id": "Q2", "label": "notion", "description": "window manager"}]}
        return {"entities": {"Q1": {"claims": {"P856": site("https://notion.example/")}},
                             "Q2": {"claims": {"P856": site("https://wm.example/")}}}}

    monkeypatch.setattr(primary, "_wikidata", fake_wikidata)
    monkeypatch.setattr(primary, "download", fake_download({
        "https://notion.example/": (b"Notion", "text/html"), "https://wm.example/": (b"notion wm", "text/html")}))
    out = primary.resolve_domain("Notion")
    assert [c["status"] for c in out["candidates"]] == ["ambiguous", "ambiguous"]

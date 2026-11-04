"""The temporal skill, phase 2: collecting the dates sources carry, storing them, showing them, and flagging stale answers."""

import sqlite3
from datetime import date, datetime, timezone

import httpx
import pytest

import config
from evidence import EvidenceStore
from runner import Plan, Selection
from skills.temporal import freshness
from skills.temporal.clock import now
from skills.temporal.extract import (DateFact, best_date, collect_dates, is_content_date, latest_possible, newest_content_date,
                                     parse_date)
from tests.test_runner import (GOOD_QUOTE, PAGE_TEXT, ScriptedLLM, World, default_handlers, env, make_runner,  # noqa: F401
                               types)
from tools import web
from tools.archive import archive_bytes

ANCHOR = datetime(2026, 11, 4, 3, 51, tzinfo=timezone.utc)


# --- reading one date --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("2007", ("2007", "year", False)),
    ("2024-05", ("2024-05", "month", False)),
    ("2024-05-03", ("2024-05-03", "day", False)),
    ("2017/06/12", ("2017-06-12", "day", False)),
    ("2017-06-12T03:04:05Z", ("2017-06-12T03:04:05+00:00", "datetime", False)),
    ("2026-10-30T08:00:00+05:30", ("2026-10-30T08:00:00+05:30", "datetime", False)),
    ("May 2007", ("2007-05", "month", False)),
    ("12 Jun 2017", ("2017-06-12", "day", False)),
    ("June 12, 2017", ("2017-06-12", "day", False)),
    ("Jun 12, 2017 3:04 PM", ("2017-06-12T15:04:00", "datetime", False)),
    ("3 November 2026", ("2026-11-03", "day", False)),
])
def test_dates_are_read_with_their_precision(raw, expected):
    assert parse_date(raw, ANCHOR) == expected


@pytest.mark.parametrize("raw", ["June 12", "garbage", "", "no digits here", "1850", "2099", "2024-13", "2024-02-31", "x" * 100, "12:30"])
def test_unusable_dates_are_refused_not_guessed(raw):
    assert parse_date(raw, ANCHOR) is None


@pytest.mark.parametrize("raw, expected", [
    ("today", ("2026-11-04", "day", True)), ("yesterday", ("2026-11-03", "day", True)),
    ("3 days ago", ("2026-11-01", "day", True)), ("2 weeks ago", ("2026-10-21", "day", True)),
    ("2 months ago", ("2026-09", "month", True)), ("a year ago", ("2025", "year", True)),
    ("5 hours ago", ("2026-11-03T22:51:00+00:00", "datetime", True)),
])
def test_relative_dates_are_anchored_and_flagged(raw, expected):
    assert parse_date(raw, ANCHOR) == expected


def test_a_relative_date_needs_an_anchor():
    assert parse_date("3 days ago", None) is None


# --- finding dates in pages --------------------------------------------------------------------------------------

ARXIV = """<html><head><meta name="citation_title" content="Attention Is All You Need">
<meta name="citation_date" content="2017/06/12"><meta name="citation_online_date" content="2023/08/02"></head>
<body><div>[Submitted on 12 Jun 2017 (v1), last revised 2 Aug 2023 (this version, v7)]</div></body></html>"""

NEWS = """<html><head><meta property="article:published_time" content="2026-10-30T08:00:00+05:30">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle","datePublished":"2026-10-30T08:00:00+05:30",
"dateModified":"2026-11-01T10:15:00Z","author":{"name":"A"}}</script></head><body><article><h1>Funding round</h1></article></body></html>"""

WIKI = """<html><body><p>Dropbox is a company.</p><ul><li>This page was last edited on 3 November 2026, at 10:01 (UTC).</li></ul></body></html>"""

BLOG = """<html><body><article><time datetime="2024-05-03">May 3</time><p>Launch post</p></article></body></html>"""


def kinds(facts):
    return {(f.kind, f.value, f.origin) for f in facts}


def test_an_arxiv_style_page_gives_published_and_revised_dates_with_origins():
    facts = collect_dates(ARXIV, None, "https://arxiv.org/abs/1706.03762", retrieved_at=ANCHOR)
    got = kinds(facts)
    assert ("published", "2017-06-12", "meta:citation_date") in got
    assert ("published", "2023-08-02", "meta:citation_online_date") in got
    assert ("published", "2017-06-12", "text:byline") in got          # "Submitted on 12 Jun 2017"
    assert ("modified", "2023-08-02", "text:byline") in got           # "last revised 2 Aug 2023"
    assert best_date(facts).value == "2017-06-12"                    # first publication, not the latest upload


def test_json_ld_and_meta_agree_and_keep_separate_origins():
    got = kinds(collect_dates(NEWS, None, "https://news.example/story", retrieved_at=ANCHOR))
    assert ("published", "2026-10-30T08:00:00+05:30", "jsonld:datePublished") in got
    assert ("published", "2026-10-30T08:00:00+05:30", "meta:article:published_time") in got
    assert ("modified", "2026-11-01T10:15:00+00:00", "jsonld:dateModified") in got


def test_a_wikipedia_footer_gives_the_last_edit_date():
    facts = collect_dates(WIKI, None, "https://en.wikipedia.org/wiki/Dropbox", retrieved_at=ANCHOR)
    assert ("modified", "2026-11-03", "text:byline") in kinds(facts) and best_date(facts).kind == "modified"


def test_time_elements_and_dates_in_the_url_are_collected():
    got = kinds(collect_dates(BLOG, None, "https://blog.example/2024/05/03/launch-post", retrieved_at=ANCHOR))
    assert ("time_element", "2024-05-03", "time:datetime") in got and ("url_date", "2024-05-03", "url") in got
    assert ("url_date", "2023-07", "url") in kinds(collect_dates(None, None, "https://x.example/2023/07/report", retrieved_at=ANCHOR))


def test_http_headers_are_kept_but_never_taken_as_the_date_of_the_content():
    headers = {"last-modified": "Wed, 21 Oct 2026 07:28:00 GMT", "date": "Wed, 04 Nov 2026 03:51:00 GMT"}
    facts = collect_dates("<html></html>", headers, "https://x.example/p", retrieved_at=ANCHOR)
    assert kinds(facts) == {("modified", "2026-10-21T07:28:00+00:00", "http:Last-Modified"),
                            ("server_date", "2026-11-04T03:51:00+00:00", "http:Date")}
    assert not any(is_content_date(f) for f in facts)
    assert best_date(facts) is None and newest_content_date(facts) is None


def test_the_search_engines_date_is_one_more_signal():
    facts = collect_dates(None, None, "https://x.example/p", hit_published="2024-05-03T10:11:12", retrieved_at=ANCHOR)
    assert kinds(facts) == {("search_result", "2024-05-03T10:11:12", "search")}
    rel = collect_dates(None, None, "", hit_published="3 days ago", retrieved_at=ANCHOR)
    assert rel[0].value == "2026-11-01" and rel[0].relative is True


def test_bad_or_implausible_dates_and_broken_markup_never_raise():
    html = ('<html><head><meta name="date" content="1850-01-01"><meta name="datepublished" content="2099-01-01">'
            '<meta name="citation_date" content="not a date"><script type="application/ld+json">{broken json</script></head></html>')
    assert collect_dates(html, {"last-modified": "garbage"}, "https://x.example/", retrieved_at=ANCHOR) == []
    assert collect_dates("", None, "") == [] and collect_dates("<<<>>>", None, "") == []


def test_duplicates_are_removed():
    html = '<meta name="citation_date" content="2017/06/12"><meta name="citation_date" content="2017-06-12">'
    assert len(collect_dates(html, None, "", retrieved_at=ANCHOR)) == 1


def test_best_date_prefers_published_then_the_most_precise_then_the_earliest():
    facts = [DateFact("modified", "2026-01-01", "day", "", "meta:x"), DateFact("published", "2020", "year", "", "meta:x"),
             DateFact("published", "2020-03-04", "day", "", "meta:y"), DateFact("published", "2020-03-05", "day", "", "meta:z")]
    assert best_date(facts).value == "2020-03-04"


def test_the_latest_possible_day_is_used_for_age():
    assert latest_possible(DateFact("published", "2024", "year", "", "x")) == date(2024, 12, 31)
    assert latest_possible(DateFact("published", "2024-02", "month", "", "x")) == date(2024, 2, 29)
    assert latest_possible(DateFact("published", "2024-02-03T10:00:00+00:00", "datetime", "", "x")) == date(2024, 2, 3)


# --- the store ---------------------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    return EvidenceStore(tmp_path / "e.db")


def fetched(body=b"<html><body><p>Hello</p></body></html>", url="https://acme.com/p", dates=()):
    sha, path = archive_bytes(body, ".html")
    return {"url": url, "final_url": url, "content_type": "text/html", "encoding": "utf-8", "method": "http",
            "fetched_at": "2026-11-04T03:51:00+00:00", "sha256": sha, "archive_path": path, "dates": [d.as_dict() for d in dates]}


def test_dates_are_stored_with_the_source_and_never_duplicated(store):
    d = DateFact("published", "2024-05-03", "day", "May 3, 2024", "meta:article:published_time")
    sha = store.ingest(fetched(dates=[d]))
    store.ingest(fetched(dates=[d]))                                         # fetching the same page again adds nothing
    assert store.source_dates(sha) == [{"kind": "published", "value": "2024-05-03", "precision": "day", "raw": "May 3, 2024",
                                        "origin": "meta:article:published_time", "relative": 0}]
    assert store.record_source_dates(sha, [d]) == 0                         # already known
    assert store.record_source_dates(sha, [DateFact("search_result", "2024-05-04", "day", "x", "search")]) == 1
    assert len(store.source_dates(sha)) == 2 and store.source_dates("0" * 64) == []


@pytest.mark.parametrize("sql", ["UPDATE source_dates SET value = '1999'", "DELETE FROM source_dates"])
def test_the_dates_table_is_append_only(store, sql):
    store.ingest(fetched(dates=[DateFact("published", "2024-05-03", "day", "", "meta:x")]))
    db = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute(sql)
    db.close()


def test_a_claim_shows_the_dates_of_its_source(store):
    page = b"<html><body><article><p>The Starter plan costs $29 per month, billed annually.</p></article></body></html>"
    sha = store.ingest(fetched(page, dates=[DateFact("published", "2026-10-01", "day", "", "jsonld:datePublished")]))
    cid = store.add_claim(store.add_entity("Acme"), "p", "$29", "Starter costs $29.", sha, "The Starter plan costs $29 per month, billed annually.")["claim_id"]
    detail = store.claim_detail(cid)
    assert [d["value"] for d in detail["source_dates"]] == ["2026-10-01"]
    assert "source_dates" in store.counts()


# --- the fetchers ------------------------------------------------------------------------------------------------

def test_fetch_url_collects_dates_from_the_page_and_the_headers(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    handler = lambda request: httpx.Response(200, text=ARXIV, headers={"content-type": "text/html; charset=utf-8",  # noqa: E731
                                                                      "last-modified": "Wed, 21 Oct 2026 07:28:00 GMT"})
    real = httpx.Client
    monkeypatch.setattr(web.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(web, "check_public_url", lambda url: None)
    result = web.fetch_url("https://arxiv.example/abs/1706.03762")
    got = {(d["kind"], d["value"], d["origin"]) for d in result["dates"]}
    assert ("published", "2017-06-12", "meta:citation_date") in got and ("modified", "2026-10-21T07:28:00+00:00", "http:Last-Modified") in got
    sha = EvidenceStore(tmp_path / "e.db").ingest(result)                 # and they are stored when the page is ingested
    assert any(d["origin"] == "meta:citation_date" for d in EvidenceStore(tmp_path / "e.db").source_dates(sha))


# --- freshness ---------------------------------------------------------------------------------------------------

TODAY = now(lambda: ANCHOR, zone="UTC")
LATEST = {"intent": "latest", "recent": True}
FIXED = {"intent": "historical", "recent": False}


def dated(value, kind="published", precision="day"):
    return [DateFact(kind, value, precision, "", "meta:x")]


def test_a_recent_source_is_not_flagged():
    a = freshness.assess({"s1": dated("2026-10-20")}, LATEST, TODAY, 548)
    assert a["stale"] is False and a["age_days"] == 15 and a["newest_source_date"] == "2026-10-20" and freshness.warning(a) is None


def test_an_old_source_is_flagged_for_a_question_about_the_current_state():
    a = freshness.assess({"s1": dated("2024-05-03"), "s2": dated("2023-01-01")}, LATEST, TODAY, 548)
    assert a["stale"] is True and a["newest_source_date"] == "2024-05-03" and a["age_days"] == 915
    assert "2024-05-03" in freshness.warning(a) and "may be out of date" in freshness.warning(a)


def test_a_year_only_date_is_given_the_benefit_of_the_doubt():
    assert freshness.assess({"s": dated("2025", precision="year")}, LATEST, TODAY, 548)["age_days"] == 308     # from 31 December 2025


def test_undated_sources_are_flagged_for_current_state_questions_but_not_for_fixed_facts():
    a = freshness.assess({"s": []}, LATEST, TODAY, 548)
    assert a["stale"] is True and a["undated_sources"] == 1 and "carries a date" in freshness.warning(a)
    assert freshness.assess({"s": []}, FIXED, TODAY, 548)["stale"] is False and freshness.warning(freshness.assess({"s": []}, FIXED, TODAY, 548)) is None
    assert freshness.assess({}, LATEST, TODAY, 548)["stale"] is False                  # nothing established: nothing to flag


def test_http_dates_do_not_make_an_undated_source_look_fresh():
    http = [DateFact("modified", "2026-11-04T03:00:00+00:00", "datetime", "", "http:Last-Modified")]
    assert freshness.assess({"s": http}, LATEST, TODAY, 548)["stale"] is True


# --- the runner and the report -----------------------------------------------------------------------------------

def run(env, question, published=None, dates_on_fetch=None, **kw):
    original_search, original_fetch = env["world"].search, env["world"].fetch

    def search(query, max_results=8, **extra):
        out = original_search(query, max_results)
        for r in out["results"]:
            r["published"] = published
        return out

    def fetch(url, max_chars=2000):
        out = original_fetch(url, max_chars)
        if dates_on_fetch:
            out["dates"] = [d.as_dict() for d in dates_on_fetch]
        return out

    runner = make_runner(env, Plan=lambda p: Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"]),
                         Selection=lambda p: Selection(numbers=[1]))
    runner.now_fn = lambda: ANCHOR
    runner.search_fn, runner.fetch_fn = search, fetch
    return runner.start(question, budgets={"max_iterations": 1}, **kw)


def test_each_fetched_page_reports_its_dates_in_the_history(env):
    result = run(env, "Who is the current CEO of Acme?", published="2026-10-20T00:00:00",
                 dates_on_fetch=[DateFact("published", "2026-10-01", "day", "Oct 1", "jsonld:datePublished")])
    fetched_events = [e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "source_fetched"]
    assert fetched_events and fetched_events[0]["best_date"] == "2026-10-01" and fetched_events[0]["best_date_kind"] == "published"
    origins = {d["origin"] for d in fetched_events[0]["dates"]}
    assert origins == {"jsonld:datePublished", "search"}                      # the page's own date and the search engine's


def test_the_search_date_alone_dates_a_source_that_carries_none(env):
    result = run(env, "Who is the current CEO of Acme?", published="2026-10-20T00:00:00")
    event = next(e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "source_fetched")
    assert event["best_date"] == "2026-10-20T00:00:00" and event["best_date_kind"] == "search_result"


def test_a_recent_answer_to_a_current_question_has_no_staleness_warning(env):
    report = run(env, "Who is the current CEO of Acme?", published="2026-10-20T00:00:00")["report"]
    assert report["established"] and report["established"][0]["source_date"] == "2026-10-20T00:00:00"
    assert report["time"]["applies"] is True and report["time"]["stale"] is False and report["time"]["today"] == "2026-11-04"
    assert not any("out of date" in w for w in report["warnings"])


def test_an_old_answer_to_a_current_question_is_flagged_in_the_report(env):
    report = run(env, "Who is the current CEO of Acme?", published="2024-05-03T00:00:00")["report"]
    assert report["established"]                                                # it is still shown, never hidden
    assert report["time"]["stale"] is True and report["time"]["newest_source_date"] == "2024-05-03T00:00:00"
    assert any("2024-05-03" in w and "out of date" in w for w in report["warnings"])


def test_an_undated_answer_to_a_current_question_is_flagged(env):
    report = run(env, "Who is the current CEO of Acme?")["report"]
    assert report["established"][0]["source_date"] is None
    assert report["time"]["stale"] is True and any("carries a date" in w for w in report["warnings"])


def test_a_fixed_fact_is_never_called_stale_however_old_its_source(env):
    report = run(env, "In what year was Acme founded?", published="2001-01-01T00:00:00")["report"]
    assert report["time"]["applies"] is False and report["time"]["stale"] is False
    assert not any("out of date" in w for w in report["warnings"])
    assert report["established"][0]["source_date"] == "2001-01-01T00:00:00"      # but the date is still shown


# --- a real failure: an embedded video's upload date was taken as the page's published date --------------------------

CNBC_STYLE = """<html><head>
<meta property="article:published_time" content="2026-05-26T13:15:01+0000">
<meta property="article:modified_time" content="2026-05-26T17:27:27+0000">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"NewsArticle","datePublished":"2026-05-26T13:15:01+0000","dateCreated":"2026-05-26T13:15:01+0000","dateModified":"2026-05-26T17:27:27+0000",
  "video":{"@type":"VideoObject","uploadDate":"2023-06-21T18:20:56+0000","datePublished":"2023-06-21T18:20:56+0000"}},
 {"@type":"VideoObject","uploadDate":"2023-06-21T18:20:56+0000"}]}</script></head>
<body><time datetime="2026-05-26T13:15:01+0000">May 26</time></body></html>"""


def test_dates_of_an_embedded_video_are_not_the_dates_of_the_page():
    facts = collect_dates(CNBC_STYLE, None, "https://www.cnbc.com/2026/05/26/dropbox-ceo.html", retrieved_at=ANCHOR)
    assert not any(f.value.startswith("2023-06-21") for f in facts)                 # nothing from inside the VideoObject
    assert best_date(facts).value == "2026-05-26T13:15:01+00:00" and best_date(facts).origin == "jsonld:datePublished"
    assert newest_content_date(facts).value.startswith("2026-05-26")                # age is judged by day, so same-day times tie


def test_an_upload_date_found_at_the_top_level_is_kept_but_never_taken_as_the_page_date():
    html = '<script type="application/ld+json">{"uploadDate":"2023-06-21"}</script>'
    facts = collect_dates(html, None, "", retrieved_at=ANCHOR)
    assert [(f.kind, f.value) for f in facts] == [("media_upload", "2023-06-21")]
    assert best_date(facts) is None and not is_content_date(facts[0])


def test_the_most_authoritative_origin_wins_over_an_earlier_but_weaker_date():
    facts = [DateFact("published", "2010-01-01", "day", "", "text:byline"), DateFact("published", "2026-05-26", "day", "", "jsonld:datePublished"),
             DateFact("published", "2009-01-01", "day", "", "meta:citation_online_date")]
    assert best_date(facts).value == "2026-05-26"                                    # not simply the earliest
    arxiv = collect_dates(ARXIV, None, "", retrieved_at=ANCHOR)
    assert best_date(arxiv).origin == "meta:citation_date" and best_date(arxiv).value == "2017-06-12"

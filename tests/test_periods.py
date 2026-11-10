"""The temporal skill, phase 3: periods, event dates, exact SEC periods, period mismatches, corroboration labels."""

import sqlite3

import pytest

import corroboration
from evidence import EvidenceStore
from runner import Plan
from tests.scripted import Extraction, ExtractedClaim
from skills.temporal.periods import analyse, find_events, find_periods, period_conflicts, period_from_xbrl
from support import SupportVerdict, check_claim
from tests.test_evidence import QUOTE, make_fetch, store, source, acme  # noqa: F401
from tests.test_runner import ScriptedLLM, World, env, make_runner  # noqa: F401
from tools.archive import archive_bytes, utc_now


# --- periods ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, label, fields", [
    ("Net sales for the three months ended September 26, 2026 were $94 billion", "3 months ended 2026-09-26",
     {"span": "quarter", "months": 3, "end": "2026-09-26"}),
    ("Q3 FY26 revenue", "FY2026 Q3", {"basis": "fiscal", "quarter": 3, "fiscal_year": 2026}),
    ("fourth quarter of fiscal 2026", "FY2026 Q4", {"basis": "fiscal", "quarter": 4}),
    ("fiscal 2026 fourth quarter results", "FY2026 Q4", {"quarter": 4, "fiscal_year": 2026}),
    ("second quarter of 2025", "Q2 2025", {"quarter": 2, "fiscal_year": 2025}),
    ("Q1 2026 earnings", "Q1 2026", {"quarter": 1}),
    ("H1 2026", "H1 2026", {"span": "half", "half": 1, "months": 6}),
    ("the first half of calendar 2025", "CY2025 H1", {"basis": "calendar", "half": 1}),
    ("CY2025", "CY2025", {"basis": "calendar", "span": "full_year"}),
    ("calendar year 2024", "CY2024", {"basis": "calendar", "fiscal_year": 2024}),
    ("full year 2025 revenue", "full year 2025", {"span": "full_year", "fiscal_year": 2025}),
    ("FY2026 revenue", "FY2026", {"basis": "fiscal", "span": "full_year", "fiscal_year": 2026}),
    ("fiscal year ended June 30, 2026", "fiscal year ended 2026-06-30", {"basis": "fiscal", "span": "full_year", "end": "2026-06-30"}),
    ("nine months ended 2026-09-30", "9 months ended 2026-09-30", {"span": "ytd", "months": 9}),
    ("trailing twelve months", "trailing twelve months", {"span": "ttm"}),
])
def test_period_parser_table(text, label, fields):
    found = find_periods(text)
    assert [p.label for p in found] == [label]
    assert fields.items() <= found[0].as_dict().items()


def test_a_period_inside_a_longer_one_is_not_reported_twice():
    assert [p.label for p in find_periods("revenue in Q3 FY26")] == ["FY2026 Q3"]


def test_no_boundary_is_invented():
    p = find_periods("three months ended September 26, 2026")[0]
    assert p.end == "2026-09-26" and p.start is None            # a start would need a fiscal calendar the text does not give


def test_not_every_q_number_is_a_period():
    assert find_periods("Q3 25 million users") == [] and find_periods("version H2 12 released") == []


def test_year_over_year_change_is_read_with_its_sign():
    up = find_periods("Q3 FY26 revenue rose 12% year-over-year")[0]
    assert (up.comparison, up.change) == ("yoy", "+12%")
    down = find_periods("fiscal 2026 net income was down 3.5% compared to the prior year")[0]
    assert down.comparison == "yoy" and down.change == "-3.5%"
    qoq = find_periods("Q2 2026 sales increased 4% quarter over quarter")[0]
    assert (qoq.comparison, qoq.change) == ("qoq", "+4%")
    assert find_periods("Q3 FY26 revenue was $5 billion")[0].comparison is None


# --- events ------------------------------------------------------------------------------------------------------

def kinds(items):
    return {(i.kind, i.value) for i in items}


def test_event_dates_are_read_with_their_kind():
    text = "The deal closed on March 3, 2025, effective March 1, 2025, as of 2025-03-01 and was filed on 4 March 2025."
    assert kinds(find_events(text)) == {("closed", "2025-03-03"), ("effective", "2025-03-01"), ("as_of", "2025-03-01"),
                                        ("filed", "2025-03-04")}


def test_deal_start_end_and_round_dates():
    assert kinds(find_events("The agreement runs from January 1, 2026 to December 31, 2026.")) == {
        ("start", "2026-01-01"), ("end", "2026-12-31")}
    assert ("round_date", "2021-05-05") in kinds(find_events("It closed its Series B round on 5 May 2021."))


def test_announcement_with_no_effective_date_is_marked_pending_and_dated_by_the_page():
    text = ("Today, we announced that Ashraf Alkarmi will become Dropbox co-CEO. Following a transition period, "
            "Ashraf will become sole CEO.")
    items = find_events(text, "2026-05-26", "day")
    assert ("announced", "2026-05-26") in kinds(items) and any(i.kind == "pending" for i in items)
    assert not any(i.kind == "effective" for i in items)         # the effective date is simply not there
    announced = next(i for i in items if i.kind == "announced")
    assert announced.derived and announced.origin == "source_date"


def test_today_is_not_resolved_without_a_page_date():
    assert not any(i.kind == "announced" for i in find_events("Today, we announced a new plan.", None))
    assert not any(i.kind == "announced" for i in find_events("Today, we announced a new plan.", "2026", "year"))


def test_a_stated_effective_date_is_kept_beside_the_pending_marker():
    items = find_events("Smith will become CEO, effective January 1, 2027.")
    assert ("effective", "2027-01-01") in kinds(items) and any(i.kind == "pending" for i in items)


def test_context_fills_only_what_the_quote_lacks_and_is_labelled():
    items = analyse("Net sales were $94.0 billion.", "In the three months ended September 26, 2026 the company reported")
    period = next(i for i in items if i.kind == "period")
    assert period.origin == "context" and period.value == "3 months ended 2026-09-26"
    quoted = analyse("Q3 FY26 net sales were $94.0 billion.", "in fiscal 2020 the company reported")
    assert [i.value for i in quoted if i.kind == "period"] == ["FY2026 Q3"] and quoted[0].origin == "quote"


# --- exact SEC periods -------------------------------------------------------------------------------------------

FACT = ('{"units":{"USD":[{"start":"2024-09-29","end":"2025-09-27","val":391035000000,"accn":"0000320193-25-000079",'
        '"fy":2025,"fp":"FY","form":"10-K","filed":"2025-10-31"},{"end":"2025-06-28","val":94036000000,"fy":2025,"fp":"Q3",'
        '"form":"10-Q","filed":"2025-08-01"}]}}')


def test_a_quote_inside_an_xbrl_fact_gets_that_facts_exact_period():
    at = FACT.index('"val":391035000000')
    items = period_from_xbrl(FACT, at, at + 18)
    period = next(i for i in items if i.kind == "period")
    assert period.origin == "edgar_xbrl" and period.value == "FY2025" and not period.derived
    assert period.detail["start"] == "2024-09-29" and period.detail["end"] == "2025-09-27" and period.detail["form"] == "10-K"
    assert ("filed", "2025-10-31") in kinds(items)


def test_xbrl_quarter_fact_has_an_end_but_no_invented_start():
    at = FACT.index('"val":94036000000')
    period = next(i for i in period_from_xbrl(FACT, at, at + 17) if i.kind == "period")
    assert period.value == "FY2025 Q3" and period.detail["end"] == "2025-06-28" and "start" not in period.detail


def test_prose_is_not_mistaken_for_an_xbrl_fact():
    assert period_from_xbrl("Net sales were $94 billion {in the quarter}", 10, 30) == []


# --- period mismatch ---------------------------------------------------------------------------------------------

def test_claim_and_quote_naming_different_periods_conflict():
    assert period_conflicts("Revenue in Q3 FY26 was $5 billion", "revenue for the fourth quarter of fiscal 2026 was $5 billion")
    assert period_conflicts("FY2026 revenue was $5 billion", "revenue for fiscal 2025 was $5 billion")


def test_matching_or_unstated_periods_do_not_conflict():
    assert period_conflicts("Revenue in Q3 FY26 was $5", "Q3 FY26 revenue was $5") == []
    assert period_conflicts("Revenue was $5", "fiscal 2025 revenue was $5") == []        # the claim names no period
    assert period_conflicts("FY2026 revenue was $5", "revenue was $5") == []             # the quote names none


class Verdict:
    def __init__(self, v):
        self.v = v

    def with_structured_output(self, schema):
        v = self.v

        class C:
            def invoke(self_, prompt):
                return SupportVerdict(verdict=v, reasoning="the model is satisfied")
        return C()


def test_support_check_downgrades_a_period_mismatch(store, acme):
    text = "Revenue for the fourth quarter of fiscal 2026 was 5 billion dollars in total."
    sha, path = archive_bytes(f"<html><body><p>{text}</p></body></html>".encode(), ".html")
    src = store.ingest({"url": "https://acme.com/r", "final_url": "https://acme.com/r", "content_type": "text/html", "encoding": "utf-8",
                        "method": "http", "fetched_at": utc_now(), "sha256": sha, "archive_path": path})
    cid = store.add_claim(acme, "revenue", "", "Acme revenue in Q3 FY26 was 5 billion dollars.", src, text)["claim_id"]
    res = check_claim(store, cid, llm=Verdict("supported"))
    assert res["verdict"] == "partially_supported" and any("FY2026 Q3" in p for p in res["unsupported_parts"])


# --- storage -----------------------------------------------------------------------------------------------------

def test_claim_time_is_stored_once_and_never_changed(store, source, acme):
    cid = store.add_claim(acme, "pricing", "$29", "Starter costs $29.", source, QUOTE)["claim_id"]
    items = analyse("Q3 FY26 sales rose 12% year over year, effective March 1, 2026")
    assert store.record_claim_time(cid, items, "test") == len(items) > 1
    assert store.record_claim_time(cid, items, "test") == 0
    times = store.claim_times(cid)
    period = next(t for t in times if t["kind"] == "period")
    assert period["detail"]["change"] == "+12%" and period["detail"]["comparison"] == "yoy"
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE claim_time SET value = 'x'")
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM claim_time")
    assert store.claim_detail(cid)["times"] == times


# --- corroboration -----------------------------------------------------------------------------------------------

def row(cid, value, url="https://a.example/x", tier="secondary", fact=0, times=()):
    return {"claim_id": cid, "value": value, "source_url": url, "source_tier": tier, "fact": fact, "times": list(times)}


def period_time(label):
    return {"kind": "period", "value": label}


def test_site_groups_subdomains_and_country_suffixes():
    assert corroboration.site("https://blog.dropbox.com/a") == corroboration.site("https://www.dropbox.com/b") == "dropbox.com"
    assert corroboration.site("https://www.bbc.co.uk/news") == "bbc.co.uk"


def test_labels():
    assert corroboration.label(row(1, "x", tier="primary"), {"a.com"}, False) == "primary source"
    assert corroboration.label(row(1, "x"), {"a.com"}, False) == "Tier 3: single source, unconfirmed"
    assert corroboration.label(row(1, "x"), {"a.com", "b.com"}, False) == "Tier 3: 2 independent sources"
    assert corroboration.label(row(1, "x", tier="primary"), {"a.com"}, True) == "sources disagree"


def test_different_numbers_for_the_same_fact_conflict():
    found = corroboration.find_conflicts([row(1, "2008"), row(2, "2007", url="https://b.example/y")])
    assert found == [{"claims": [1, 2], "note": "2008 (claim 1) vs 2007 (claim 2) for the same fact"}]


def test_same_value_other_fact_names_and_different_periods_do_not_conflict():
    assert corroboration.find_conflicts([row(1, "2007"), row(2, "2007")]) == []
    assert corroboration.find_conflicts([row(1, "2007", fact=0), row(2, "2008", fact=1)]) == []
    assert corroboration.find_conflicts([row(1, "Drew Houston"), row(2, "Ashraf Alkarmi")]) == []
    assert corroboration.find_conflicts([row(1, "$90B", times=[period_time("FY2025")]), row(2, "$94B", times=[period_time("FY2026")])]) == []
    assert corroboration.find_conflicts([row(1, "$90B", times=[period_time("FY2025")]), row(2, "$94B")]) == []
    assert corroboration.find_conflicts([row(1, "500", times=[{"kind": "as_of", "value": "2024-01-01"}]),
                                         row(2, "900", times=[{"kind": "as_of", "value": "2026-01-01"}])]) == []


def test_same_period_different_values_conflict():
    both = [row(1, "$90B", times=[period_time("FY2026")]), row(2, "$94B", times=[period_time("FY2026")])]
    assert len(corroboration.find_conflicts(both)) == 1


# --- through the runner ------------------------------------------------------------------------------------------

EARNINGS = ("Apple reported results. Net sales for the three months ended September 26, 2026 were $94.0 billion, up 8% year over year. "
            "Today, we announced that Smith will become CEO. " + "Filler sentence. " * 8)
Q_QUOTE = "Net sales for the three months ended September 26, 2026 were $94.0 billion, up 8% year over year."


def run_earnings(env, claims, summary=None):
    def fetch(url, max_chars=2000):
        sha, path = archive_bytes(f"<html><body><article><p>{EARNINGS}</p></article></body></html>".encode(), ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    env["world"].fetch = fetch
    runner = make_runner(env, Plan=lambda p: Plan(subject="Acme", queries=["acme net sales"], checklist=["Acme net sales"]),
                         Extraction=lambda p: Extraction(claims=claims))
    runner.search_fn = lambda query, max_results=8, **extra: env["world"].search(query, max_results)
    return runner.start("What were Acme's net sales?", budgets={"max_iterations": 1})


def test_a_recorded_claim_gets_its_period_and_the_report_shows_it(env):
    result = run_earnings(env, [ExtractedClaim(predicate="net_sales", value="$94.0 billion", claim="Acme's net sales were $94.0 billion.",
                                               quote=Q_QUOTE, needed_fact=1)])
    assert result["status"] == "finished", result
    row_ = result["report"]["established"][0]
    period = next(t for t in row_["times"] if t["kind"] == "period")
    assert period["value"] == "3 months ended 2026-09-26" and period["detail"]["change"] == "+8%"
    assert row_["corroboration"] == "Tier 3: single source, unconfirmed"
    assert result["report"]["conflicts"] == []
    assert "claim_time_recorded" in [e["type"] for e in env["runs"].events(result["run_id"])]


def test_conflicting_values_are_flagged_in_the_store_and_the_report(env):
    result = run_earnings(env, [
        ExtractedClaim(predicate="a", value="$94.0 billion", claim="Acme's net sales were $94.0 billion.", quote=Q_QUOTE, needed_fact=1),
        ExtractedClaim(predicate="b", value="8%", claim="Acme's net sales grew 8% year over year.",
                       quote="up 8% year over year. Today, we announced that Smith will become CEO.", needed_fact=1),
    ])
    report = result["report"]
    assert len(report["conflicts"]) == 1 and report["conflicts"][0]["claims"] == [1, 2]
    assert {r["corroboration"] for r in report["established"]} == {"sources disagree"}
    assert env["store"].conflicts_for(1)[0]["other_claim"] == 2


def test_a_failure_in_time_reading_does_not_lose_the_claim(env, monkeypatch):
    monkeypatch.setattr("runner.tperiods.analyse", lambda *a, **k: 1 / 0)
    result = run_earnings(env, [ExtractedClaim(predicate="net_sales", value="$94.0 billion", claim="Acme's net sales were $94.0 billion.",
                                               quote=Q_QUOTE, needed_fact=1)])
    assert len(result["report"]["established"]) == 1
    assert "claim_time_failed" in [e["type"] for e in env["runs"].events(result["run_id"])]


def test_same_calendar_day_written_two_ways_is_not_a_conflict():
    a = {"claim_id": 1, "fact": 0, "value": "October 7, 2024", "times": []}
    b = {"claim_id": 2, "fact": 0, "value": "7 October 2024", "times": []}
    assert corroboration.find_conflicts([a, b]) == []
    c = {"claim_id": 3, "fact": 0, "value": "October 8, 2024", "times": []}
    assert len(corroboration.find_conflicts([a, c])) == 1

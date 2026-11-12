"""A financial statement asked for is answered with the table itself, read by code from the filing (skills/statements/answer.py,
the 'statement' kind of skills/question, the EDGAR skill's newest results filings, and the runner's statements_answer)."""

import json
from datetime import date, timedelta

import pytest

from api import answer as answer_view
from skills import edgar, statements
from skills.question import analyse, statements_asked
from skills.statements import answer
from skills.temporal import reporting
from tests.test_edgar_skill import Sec, edgar_runner
from tests.test_runner import env  # noqa: F401
from tests.test_statements import INCOME_ROWS, RELEASE, _ixbrl
from tools.archive import archive_bytes, utc_now


# --- the question -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question, subject, ids, item", [
    ("S&P Global's Complete consolidated income statement latest earnings", None, ["income"],
     "S&P Global's consolidated income statement from its latest earnings filing"),
    ("Apple balance sheet and cash flow statement for fiscal 2025", "Apple", ["balance", "cash_flow"],
     "Apple's consolidated balance sheet for fiscal year 2025"),
    ("Show me Reliance Industries standalone profit and loss statement for Q2 FY26", None, ["income"],
     "Reliance Industries' standalone income statement for the second quarter of fiscal 2026"),
    ("S&P Global balance sheet as of June 30, 2026", "S&P Global", ["balance"],
     "S&P Global's consolidated balance sheet as of 2026-06-30"),
    ("S&P Global income statement announced in July 2026", "S&P Global", ["income"],
     "S&P Global's consolidated income statement from the earnings filing announced between 2026-07-01 and 2026-07-31"),
    ("Microsoft financial statements, latest quarter", "Microsoft", ["income", "balance", "cash_flow"],
     "Microsoft's consolidated income statement from its latest earnings filing"),
])
def test_a_question_naming_a_statement_is_framed_as_one(question, subject, ids, item):
    years = [int(y) for y in __import__("re").findall(r"\b20\d\d\b", question)]
    f = analyse(question, subject, {"years": years})
    assert f["kind"] == "statement" and f["shape"] == "table" and f["statements"] == ids and f["checklist"][0] == item


def test_a_figure_is_still_a_figure_and_a_statement_without_a_company_waits_for_the_planner():
    assert analyse("What was Apple cash flow in 2025?", "Apple", {"years": [2025]})["kind"] == "metric"
    assert statements_asked("Apple balance sheet") == ["balance"]
    assert analyse("Apple balance sheet")["kind"] == "open"              # 'Apple' opens the sentence: the planner names it


# --- the filings and the table ------------------------------------------------------------------------------------------

TODAY = date(2026, 11, 13)


@pytest.mark.parametrize("question, fye, end, months", [
    ("S&P Global income statement Q2 2026", "1231", "2026-06-30", 3),
    ("S&P Global income statement for the second quarter of 2026", "1231", "2026-06-30", 3),
    ("Apple income statement Q2 FY2026", "0927", "2026-03-31", 3),            # Apple's year ends in September
    ("Apple income statement for fiscal 2025", "0927", "2025-09-30", 12),
    ("Reliance P&L Q2 FY26", "0331", "2025-09-30", 3),                       # India: April to March
    ("Acme income statement H1 2026", "1231", "2026-06-30", 6),
    ("Acme income statement for the nine months ended September 30, 2026", None, "2026-09-30", 9),
    ("Acme balance sheet as of June 30, 2026", None, "2026-06-30", None),
    ("Acme income statement for the September 2026 quarter", None, "2026-09-30", None),
    ("Acme income statement last year", "1231", "2025-12-31", 12),
])
def test_the_period_asked_is_dated_by_the_companys_fiscal_year(question, fye, end, months):
    t = reporting.target(question, TODAY)
    pe = reporting.period_end(t, fye)
    assert (str(pe[0]), pe[1]) == (end, months) and not t.latest


def test_the_announcement_window_and_latest_and_an_undatable_fiscal_period():
    t = reporting.target("S&P Global income statement announced in July 2026", TODAY)
    assert t.latest and (t.announced_from, t.announced_to) == ("2026-07-01", "2026-07-31")
    t = reporting.target("Acme results released on 27 October 2026 income statement", TODAY)
    assert (t.announced_from, t.announced_to) == ("2026-10-27", "2026-10-27")
    assert reporting.target("S&P Global's consolidated income statement latest earnings", TODAY).latest
    t = reporting.target("Acme income statement fiscal Q3 2026", TODAY)
    assert t.basis == "fiscal" and reporting.period_end(t, None) is None                  # no year end known: never guessed
    assert reporting.column_fits({"end": "2026-03-28", "months": 3, "kind": "duration"}, date(2026, 3, 31), 3)   # a 52/53-week quarter
    assert not reporting.column_fits({"end": "2026-03-28", "months": 6, "kind": "duration"}, date(2026, 3, 31), 3)


FILINGS = [  # newest first, as the SEC lists them
    {"form": "10-Q", "filed": "2026-10-28", "report_date": "2026-09-30", "items": []},
    {"form": "8-K", "filed": "2026-10-27", "report_date": "2026-10-27", "items": ["2.02", "7.01"]},
    {"form": "10-Q", "filed": "2026-07-28", "report_date": "2026-06-30", "items": []},
    {"form": "8-K", "filed": "2026-07-28", "report_date": "2026-07-28", "items": ["2.02"]},
    {"form": "8-K", "filed": "2026-07-02", "report_date": "2026-07-02", "items": ["7.01"]},
]


def test_the_filings_for_the_asked_period_are_its_report_and_the_first_release_after_it():
    latest, _ = edgar.results_for(FILINGS)
    assert [(f["form"], f["filed"]) for f in latest] == [("10-Q", "2026-10-28"), ("8-K", "2026-10-27")]
    q2, why = edgar.results_for(FILINGS, reporting.target("S&P Global income statement Q2 2026", TODAY).as_dict(), "1231")
    assert [(f["form"], f["filed"]) for f in q2] == [("10-Q", "2026-07-28"), ("8-K", "2026-07-28")] and "2026-06-30" in why
    assert edgar.statement_window(reporting.target("Q2 2026", TODAY).as_dict(), TODAY, "1231") == (date(2026, 6, 20), date(2026, 10, 28))
    july = reporting.target("income statement announced in July 2026", TODAY).as_dict()
    assert edgar.statement_window(july, TODAY) == (date(2026, 7, 1), date(2026, 7, 31))


def _stored(store, body: bytes, url: str) -> list[dict]:
    sha, path = archive_bytes(body, ".html")
    sha = store.ingest({"url": url, "final_url": url, "sha256": sha, "archive_path": path, "content_type": "text/html",
                        "encoding": "utf-8", "method": "http", "fetched_at": utc_now()})
    statements.read_source(store, sha, url, "US")
    return [{**t, "source_sha256": sha} for t in store.statements_for(sha, primary_only=True)]


def test_the_filings_inline_xbrl_is_chosen_over_the_release_for_the_same_period_and_laid_out_as_printed(env):
    tables = _stored(env["store"], RELEASE, "https://www.sec.gov/Archives/edgar/data/1/1/release.htm") + \
        _stored(env["store"], _ixbrl(INCOME_ROWS), "https://www.sec.gov/Archives/edgar/data/1/2/acme-10q.htm")
    t, why = answer.choose(tables, "income")
    assert t["adapter"] == "ixbrl" and "inline XBRL" in why and "2026-09-30" in why
    v = answer.view(t, {"url": "u", "label": "Quarterly report", "form": "10-Q", "filed": "2026-10-28"})
    assert v["title"] == "Consolidated Statements of Income (Unaudited)" and v["checks"] == {"passed": 2, "failed": 0}
    assert [r["label"] for r in v["rows"]][:2] == ["Revenue", "Income before taxes"]
    assert [c["printed"] for c in v["rows"][0]["cells"]] == ["1,200", "1,000"]                # the cell text, never recomputed
    assert answer.describe(v, "Acme").startswith("Acme's Consolidated Statements of Income (Unaudited) (in millions, USD), from its Form 10-Q filed 2026-10-28")
    assert answer.choose(tables, "balance") == (None, "no balance sheet was read from the documents of this run")
    assert answer.choose(tables, "income", target={"latest": False, "span": "full_year", "months": 12, "fiscal_year": 2024}, fye="1231")[0] is None


def test_a_comment_before_a_table_does_not_stop_the_reading():
    body = RELEASE.replace(b"<p>Exhibit 1</p>", b"<p>Exhibit 1</p><!-- wire service markup -->")
    [t] = statements.extract(body, "text/html")
    assert t.statement == "income" and t.primary


# --- in a run -----------------------------------------------------------------------------------------------------------

class ResultsSec(Sec):
    """A fake SEC with an announcement, an earnings release (8-K Item 2.02) and the 10-Q: the release prints the income
    statement as an HTML table, the 10-Q as inline XBRL."""
    def call(self, registry, connector, endpoint, params, run_id=None):
        self.calls.append((connector, endpoint, params))
        d = lambda n: (self.today - timedelta(days=n)).isoformat()                          # noqa: E731
        rows = [("8-K", d(3), "7.01,9.01", "acme-8k.htm", "0000001234-26-000011"),
                ("8-K", d(5), "2.02,9.01", "acme-8k.htm", "0000001234-26-000009"),
                ("10-Q", d(6), "", "acme-10q.htm", "0000001234-26-000010")]
        sub = {"name": "Acme Corp", "filings": {"recent": {
            "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows], "items": [r[2] for r in rows],
            "primaryDocument": [r[3] for r in rows], "accessionNumber": [r[4] for r in rows], "reportDate": [r[1] for r in rows],
            "primaryDocDescription": ["" for _ in rows]}}}
        sha, path = archive_bytes(json.dumps(sub).encode(), ".json")
        url = f"https://data.sec.gov/submissions/CIK{params['cik']}.json"
        return {"ok": True, "url": url, "final_url": url, "status": 200, "content_type": "application/json", "encoding": "utf-8",
                "method": "api", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "source_tier": "regulatory"}

    def fetch(self, url, max_chars=2000):
        body = RELEASE if url.endswith("acme3q-release.htm") else _ixbrl(INCOME_ROWS) if url.endswith("acme-10q.htm") else None
        if body is None:
            return super().fetch(url, max_chars)
        self.world.fetched.append(url)
        sha, path = archive_bytes(body, ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}


def test_a_run_answers_a_statement_question_with_the_10q_table_and_no_model_reads_the_filings(env):
    extracted = []
    runner, _, _ = edgar_runner(env, Extraction=lambda p: extracted.append(p) or __import__("tests.scripted", fromlist=["Extraction"]).Extraction(claims=[]))
    sec = ResultsSec(env["world"], date.today())
    runner.fetch_fn, runner.call_api_fn = sec.fetch, sec.call
    result = runner.start("Acme's consolidated income statement, latest earnings", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    framed = next(e["payload"] for e in events if e["type"] == "question_framed")
    assert framed["kind"] == "statement" and framed["checklist"] == ["Acme's consolidated income statement from its latest earnings filing"]
    filings = next(e["payload"] for e in events if e["type"] == "edgar_filings")
    assert [(f["form"], f["items"]) for f in filings["filings"]] == [("8-K", ["2.02", "9.01"]), ("10-Q", [])]   # not the 7.01 announcement
    assert any(e["type"] == "statements_answer" for e in events)
    assert not any("acme-10q" in p or "Statements of Income" in p for p in extracted)        # the model never read the filings
    report = result["report"]
    assert report["outcome"] == "answered" and report["gaps"] == [] and report["established"] == []
    [t] = report["tables"]
    assert t["reading"] == "ixbrl" and t["form"] == "10-Q" and t["source_url"].endswith("acme-10q.htm")
    assert report["summary"].endswith("[table:1]") and "from its Form 10-Q filed" in report["summary"]
    view = answer_view.build(report)
    assert view["answer"].endswith("[table:1]") and view["tables"][0]["source_n"] == 1
    assert view["sources"][0]["tables"] == ["Consolidated Statements of Income (Unaudited)"]
    doc = __import__("zipfile").ZipFile(__import__("io").BytesIO(answer_view.to_docx(view))).read("word/document.xml").decode()
    assert "<w:tbl>" in doc and "1,200" in doc and "Revenue" in doc


def _q2(body: bytes) -> bytes:
    """The same filing a quarter earlier: contexts, headers and figures for the quarter ended June 30."""
    for a, b in (("2026-07-01", "2026-04-01"), ("2026-09-30", "2026-06-30"), ("2025-07-01", "2025-04-01"), ("2025-09-30", "2025-06-30"),
                 ("September", "June"), (">1,200<", ">1,100<"),
                 ("Acme Corp. ", "")):        # the name is not in the text (a real 10-Q opens with hidden XBRL): its CIK says whose it is
        body = body.replace(a.encode(), b.encode())
    return body


class TwoQuartersSec(ResultsSec):
    """The SEC with two quarters filed: Q3 (newest) and Q2, each a 10-Q and an earnings release."""
    def call(self, registry, connector, endpoint, params, run_id=None):
        self.calls.append((connector, endpoint, params))
        rows = [("10-Q", "2026-10-28", "2026-09-30", "", "acme-10q.htm", "0000001234-26-000010"),
                ("8-K", "2026-10-27", "2026-10-27", "2.02,9.01", "acme-8k.htm", "0000001234-26-000009"),
                ("10-Q", "2026-07-28", "2026-06-30", "", "acme-10q2.htm", "0000001234-26-000006"),
                ("8-K", "2026-07-27", "2026-07-27", "2.02,9.01", "acme-8k.htm", "0000001234-26-000005")]
        sub = {"name": "Acme Corp", "fiscalYearEnd": "1231", "filings": {"recent": {
            "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows], "reportDate": [r[2] for r in rows],
            "items": [r[3] for r in rows], "primaryDocument": [r[4] for r in rows], "accessionNumber": [r[5] for r in rows],
            "primaryDocDescription": ["" for _ in rows]}}}
        sha, path = archive_bytes(json.dumps(sub).encode(), ".json")
        url = f"https://data.sec.gov/submissions/CIK{params['cik']}.json"
        return {"ok": True, "url": url, "final_url": url, "status": 200, "content_type": "application/json", "encoding": "utf-8",
                "method": "api", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "source_tier": "regulatory"}

    def fetch(self, url, max_chars=2000):
        if url.endswith("acme-10q2.htm"):
            self.world.fetched.append(url)
            sha, path = archive_bytes(_q2(_ixbrl(INCOME_ROWS)), ".html")
            return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                    "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}
        return super().fetch(url, max_chars)


def test_a_run_asking_for_an_earlier_quarter_gets_that_quarters_10q_not_the_newest(env):
    from tests.scripted import Extraction
    runner, _, _ = edgar_runner(env, Extraction=lambda p: Extraction(claims=[]))
    sec = TwoQuartersSec(env["world"], date.today())
    runner.fetch_fn, runner.call_api_fn = sec.fetch, sec.call
    result = runner.start("Acme's consolidated income statement for Q2 2026", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    filings = next(e["payload"] for e in events if e["type"] == "edgar_filings")
    assert [(f["form"], f["filed"]) for f in filings["filings"]] == [("10-Q", "2026-07-28"), ("8-K", "2026-07-27")]
    assert filings["fiscal_year_end"] == "1231" and "2026-06-30" in filings["why"]
    [t] = result["report"]["tables"]
    assert t["source_url"].endswith("acme-10q2.htm") and t["form"] == "10-Q" and t["period"] == "the second quarter of 2026"
    assert [c["header"] for c in t["columns"] if c["asked"]] == ["Three Months Ended June 30, 2026"]
    assert t["rows"][0]["cells"][0]["printed"] == "1,100"                               # the Q2 figure, not Q3's 1,200


def test_a_filing_refused_as_a_web_copy_and_accepted_from_edgar_is_used_and_one_only_refused_is_not(env):
    from tests.test_runner import make_runner
    runner = make_runner(env)
    url = "https://www.sec.gov/Archives/edgar/data/1234/2/acme-10q.htm"
    [t] = _stored(env["store"], _ixbrl(INCOME_ROWS), url)
    s = {"run_id": "r-linked", "frame": analyse("Acme's consolidated income statement, latest earnings", "Acme"),
         "checklist": ["Acme's consolidated income statement from its latest earnings filing"]}
    read = {"url": url, "source_sha256": t["source_sha256"], "statements": [], "market": "US"}
    env["runs"].emit("r-linked", "fetch", "statements_read", {**read, "linked": False})      # a web hit, before the CIK was known
    assert runner._statement_answers(s) == {}
    env["runs"].emit("r-linked", "fetch", "statements_read", {**read, "linked": True})       # the same filing, from EDGAR
    assert runner._statement_answers(s)[0]["source_url"] == url


def test_a_period_not_reported_yet_is_said_so_and_nothing_else_is_given_in_its_place(env):
    from tests.scripted import Extraction
    runner, _, _ = edgar_runner(env, Extraction=lambda p: Extraction(claims=[]))
    sec = ResultsSec(env["world"], date.today())
    runner.fetch_fn, runner.call_api_fn = sec.fetch, sec.call
    year = date.today().year + 1
    result = runner.start(f"Acme's consolidated income statement for Q1 {year}", budgets={"max_iterations": 1})
    report = result["report"]
    assert report["tables"] == [] and report["outcome"] != "answered"
    assert report["gaps"] == [f"Acme's consolidated income statement for the first quarter of {year}"]

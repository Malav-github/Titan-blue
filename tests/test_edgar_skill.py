"""The EDGAR skill: the CIK comes from the SEC's own company list (a model only ever guessed it), the filings are chosen by
form and 8-K item, and the 8-K's Exhibit 99 press release is read as the company's own document."""

import json
from datetime import date, timedelta

import pytest

from skills import edgar
from tests.test_runner import PAGE_TEXT, env, make_runner, types  # noqa: F401
from tests.test_runner_capability import activate, manifest
from tools.archive import archive_bytes, utc_now

TICKERS = [{"cik": "0000064040", "ticker": "SPGI", "title": "S&P Global Inc."}, {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."},
           {"cik": "0001467623", "ticker": "DBX", "title": "DROPBOX, INC."}, {"cik": "0000000011", "ticker": "ABCA", "title": "ABC Corp"},
           {"cik": "0000000012", "ticker": "ABCB", "title": "ABC Holdings"}]


@pytest.mark.parametrize("names,text,cik", [
    (["S&P Global"], "", "0000064040"),
    (["Apple"], "", "0000320193"),
    (["Dropbox"], "", "0001467623"),
    (["SPGI"], "", "0000064040"),
    (["Google"], "", None),                                    # the registrant is Alphabet Inc.: no guess
    (["ABC"], "", None),                                       # two registrants: ambiguous, so none
    (["Whoever"], "the company with SEC CIK 320193", "0000320193"),
])
def test_the_cik_comes_from_the_secs_list_or_the_question_never_from_a_guess(names, text, cik):
    assert edgar.resolve(names, TICKERS, text)["cik"] == cik


def test_an_ambiguous_name_says_which_registrants_it_matches():
    assert "ABC Corp" in edgar.resolve(["ABC"], TICKERS)["reason"]


@pytest.mark.parametrize("question,kinds", [
    ("What did S&P Global announce in 2026?", ["strategy"]),
    ("Did S&P Global acquire anyone?", ["deals"]),
    ("Who is Apple's CFO?", ["leadership"]),
    ("What was Apple's revenue last quarter?", ["results"]),
    ("In what year was Dropbox founded?", ["company"]),
    ("Tell me about Acme", ["deals", "strategy"]),               # nothing points anywhere: announcements and deals
])
def test_the_question_decides_which_filings_fit(question, kinds):
    assert [g["id"] for g in edgar.groups_for(question)] == kinds


def test_the_window_follows_the_question():
    today = date(2026, 11, 7)
    assert edgar.window({"years": [2026]}, today) == (date(2026, 1, 1), today)
    assert edgar.window({"years": [2024]}, today) == (date(2024, 1, 1), date(2024, 12, 31))
    assert edgar.window({"intent": "latest"}, today)[0] == today - timedelta(days=200)


def submissions(today):
    d = lambda n: (today - timedelta(days=n)).isoformat()                               # noqa: E731
    rows = [("8-K", d(5), "7.01,9.01", "acme-8k.htm", "0000001234-26-000009"),
            ("4", d(6), "", "xslF345X06/form4.xml", "0000001234-26-000008"),
            ("8-K", d(20), "5.02,9.01", "acme-8k2.htm", "0000001234-26-000007"),
            ("8-K", d(900), "8.01", "old.htm", "0000001234-23-000001")]
    return {"name": "Acme Corp", "tickers": ["ACME"], "filings": {"recent": {
        "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows], "items": [r[2] for r in rows], "primaryDocument": [r[3] for r in rows],
        "accessionNumber": [r[4] for r in rows], "reportDate": [r[1] for r in rows], "primaryDocDescription": ["" for _ in rows]}}}


def test_filings_are_chosen_by_form_item_and_window():
    today = date(2026, 11, 7)
    picked = edgar.select_filings(submissions(today), edgar.groups_for("What did Acme announce recently?"), *edgar.window({"intent": "latest"}, today))
    assert [(f["form"], f["items"]) for f in picked] == [("8-K", ["7.01", "9.01"])]       # no Form 4, no leadership item, nothing old
    picked = edgar.select_filings(submissions(today), edgar.groups_for("Who is Acme's new CEO?"), *edgar.window({}, today))
    assert [f["items"] for f in picked] == [["5.02", "9.01"]]


INDEX = """<table class="tableFile" summary="Document Format Files">
<tr><td scope="row">1</td><td scope="row">8-K</td><td scope="row"><a href="/ix?doc=/Archives/edgar/data/1234/000000123426000009/acme-8k.htm">acme-8k.htm</a></td><td scope="row">8-K</td><td>1</td></tr>
<tr><td scope="row">2</td><td scope="row">EX-99.1</td><td scope="row"><a href="/Archives/edgar/data/1234/000000123426000009/acme3q-release.htm">acme3q-release.htm</a></td><td scope="row">EX-99.1</td><td>2</td></tr>
<tr><td scope="row">3</td><td scope="row">GRAPHIC</td><td scope="row"><a href="/Archives/edgar/data/1234/000000123426000009/logo.jpg">logo.jpg</a></td><td scope="row">GRAPHIC</td><td>3</td></tr>
</table>"""


def test_the_press_release_is_found_by_its_exhibit_type_not_its_file_name():
    docs = edgar.index_documents(INDEX)
    assert [(d["name"], d["type"]) for d in docs] == [("acme-8k.htm", "8-K"), ("acme3q-release.htm", "EX-99.1"), ("logo.jpg", "GRAPHIC")]
    announcement = {"form": "8-K", "items": ["7.01", "9.01"], "primary": "acme-8k.htm", "accession": "0000001234-26-000009"}
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000009/"
    assert edgar.documents(announcement, "0000001234", docs) == [base + "acme3q-release.htm"]
    deal = {**announcement, "items": ["1.01", "2.01", "9.01"]}                             # the 8-K itself describes a deal
    assert edgar.documents(deal, "0000001234", docs) == [base + "acme3q-release.htm", base + "acme-8k.htm"]
    form4 = {"form": "4", "items": [], "primary": "xslF345X06/form4.xml", "accession": "0000001234-26-000008"}
    assert edgar.documents(form4, "0000001234", []) == []


# --- in a run ---

class Sec:
    """A fake SEC: the company list, one 8-K's index page and its press release (which states the fact the run needs)."""
    def __init__(self, world, today):
        self.world, self.today, self.calls = world, today, []

    def fetch(self, url, max_chars=2000):
        if "sec.gov" not in url:
            return self.world.fetch(url, max_chars)
        self.world.fetched.append(url)
        if url == edgar.manifest()["tickers_url"]:
            body, ctype = json.dumps({"0": {"cik_str": 1234, "ticker": "ACME", "title": "Acme Corp"}}).encode(), "application/json"
        elif url.endswith("-index.html"):
            body, ctype = INDEX.encode(), "text/html"
        else:
            body, ctype = f"<html><body><article><p>Acme announces new pricing. {PAGE_TEXT}</p></article></body></html>".encode(), "text/html"
        sha, path = archive_bytes(body, ".json" if "json" in ctype else ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": ctype, "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    def call(self, registry, connector, endpoint, params, run_id=None):
        self.calls.append((connector, endpoint, params))
        sha, path = archive_bytes(json.dumps(submissions(self.today)).encode(), ".json")
        url = f"https://data.sec.gov/submissions/CIK{params['cik']}.json"
        return {"ok": True, "url": url, "final_url": url, "status": 200, "content_type": "application/json", "encoding": "utf-8",
                "method": "api", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "source_tier": "regulatory"}


def edgar_runner(env, **handlers):
    from runner import ApiPlan
    asked = []
    activate(env["registry"], manifest().model_copy(update={"connector_id": "sec_edgar"}))
    runner = make_runner(env, ApiPlan=lambda p: asked.append(p) or ApiPlan(calls=[]), **handlers)
    sec = Sec(env["world"], date.today())
    runner.fetch_fn, runner.call_api_fn = sec.fetch, sec.call
    runner.search_fn = lambda query, max_results=8, **extra: env["world"].search(query, max_results)
    return runner, sec, asked


def test_a_run_reads_the_companys_8k_press_release_from_edgar_with_no_model_choosing_anything(env):
    runner, sec, asked = edgar_runner(env)
    result = runner.start("What did Acme announce recently?", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    cik = next(e["payload"] for e in events if e["type"] == "edgar_cik")
    assert cik["cik"] == "0000001234" and cik["title"] == "Acme Corp" and "registrant name" in cik["matched_on"]
    assert sec.calls == [("sec_edgar", "submissions", {"cik": "0000001234"})] and asked == []      # no model planned an EDGAR call
    filings = next(e["payload"] for e in events if e["type"] == "edgar_filings")
    assert [f["items"] for f in filings["filings"]] == [["7.01", "9.01"]]
    read = [e["payload"]["url"] for e in events if e["type"] == "edgar_document_read"]
    assert read == ["https://www.sec.gov/Archives/edgar/data/1234/000000123426000009/acme3q-release.htm"]
    rows = [r for r in result["report"]["established"] if "sec.gov" in r["source_url"]]
    assert rows and rows[0]["source_tier"] == "primary" and rows[0]["source"]["issuer_basis"].startswith("filer CIK")
    assert rows[0]["source_label"].startswith("Press release by Acme, on EDGAR") and rows[0]["source_date"] == (date.today() - timedelta(days=5)).isoformat()


def test_the_statements_in_a_filing_are_read_by_code_and_recorded_during_the_run(env):
    from tests.test_statements import RELEASE
    runner, sec, _ = edgar_runner(env)
    plain = sec.fetch

    def fetch(url, max_chars=2000):
        if url.endswith("acme3q-release.htm"):                 # the press release carries its income statement
            sec.world.fetched.append(url)
            body = RELEASE.replace(b"</body>", f"<p>{PAGE_TEXT}</p></body>".encode())
            sha, path = archive_bytes(body, ".html")
            return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                    "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}
        return plain(url, max_chars)

    runner.fetch_fn = fetch
    result = runner.start("What did Acme announce recently?", budgets={"max_iterations": 1})
    read = next(e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "statements_read")
    [income] = read["statements"]
    assert read["market"] == "US" and income["statement"] == "income" and income["checks_failed"] == 0 and income["checks_passed"] == 4
    revenue = env["store"].find_values("revenue", "2026-09-30")
    assert [(v["value"], v["period_months"]) for v in revenue] == [("1200000000", 3), ("3400000000", 9)]


def test_the_figures_claims_quote_are_checked_against_the_statements_and_labelled(env):
    from tests.scripted import ExtractedClaim, Extraction
    from tests.test_statements import RELEASE
    right = "Acme reported revenue of $1.2 billion for the three months ended September 30, 2026."
    wrong = "Acme said net income reached $900 million for the three months ended September 30, 2026."
    runner, sec, _ = edgar_runner(env, Extraction=lambda p: Extraction(claims=[
        ExtractedClaim(predicate="financials.revenue", value="$1.2 billion", claim="Acme's third-quarter revenue was $1.2 billion.",
                       quote=right, needed_fact=1),
        ExtractedClaim(predicate="financials.net_income", value="$900 million", claim="Acme's third-quarter net income was $900 million.",
                       quote=wrong, needed_fact=1)]))
    plain = sec.fetch

    def fetch(url, max_chars=2000):
        if url.endswith("acme3q-release.htm"):
            sec.world.fetched.append(url)
            body = RELEASE.replace(b"</body>", f"<p>{PAGE_TEXT} {right} {wrong}</p></body>".encode())
            sha, path = archive_bytes(body, ".html")
            return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                    "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}
        return plain(url, max_chars)

    runner.fetch_fn = fetch
    result = runner.start("What did Acme report for the third quarter?", budgets={"max_iterations": 1})
    report = result["report"]
    rows = {("$1.2 billion" if "1.2 billion" in r["quote"] else "$900 million"): r for r in report["established"] if "sec.gov" in r["source_url"]}
    assert all(r["claim"] == r["quote"] for r in rows.values())        # the claim is the source's own sentence
    assert rows["$1.2 billion"]["figures"][0]["verdict"] == "matches"
    assert rows["$1.2 billion"]["figures_label"] == "matches the company's statements"
    assert rows["$900 million"]["figures"][0]["verdict"] == "differs"
    assert rows["$900 million"]["figures_label"].startswith("differs from the company's statements")
    assert any("differ" in w and "financial statements" in w for w in report["warnings"])
    assert any(d["id"] == "claim_figure_differs" for d in report["diagnosis"])
    stored = env["store"].claim_figures_for(rows["$900 million"]["claim_id"])
    assert stored[0]["verdict"] == "differs" and stored[0]["compared_with"]


def test_a_company_that_is_not_an_sec_registrant_is_said_so_and_the_web_carries_on(env):
    from runner import Plan
    runner, sec, _ = edgar_runner(env, Plan=lambda p: Plan(subject="Zeta Labs", queries=["zeta pricing"], checklist=["Starter plan price"]))
    result = runner.start("What does Zeta Labs charge?", budgets={"max_iterations": 1})
    no = next(e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "edgar_no_cik")
    assert "Zeta Labs" in no["reason"] and sec.calls == [] and result["status"] == "finished"


def test_a_statement_row_claim_is_established_by_code_when_the_model_cannot_read_the_row(env):
    from runner import Relevance
    from tests.scripted import ExtractedClaim, Extraction
    from tests.test_statements import RELEASE
    asked = []
    runner, sec, _ = edgar_runner(env, Extraction=lambda p: Extraction(claims=[
        ExtractedClaim(predicate="financials.revenue", quote="| Revenue |  |  | $ | 1,200 |  | $ | 1,000 |", value="1,200")]),
        Relevance=lambda p: asked.append(p) or Relevance(addresses=False, reason="the values are not labelled with a period"),
        Plan=lambda p: __import__("runner").Plan(subject="Acme", queries=["acme results"], checklist=["Acme's revenue for the third quarter of 2026"]))
    plain = sec.fetch

    def fetch(url, max_chars=2000):
        if url.endswith("acme3q-release.htm"):
            sec.world.fetched.append(url)
            sha, path = archive_bytes(RELEASE.replace(b"</body>", f"<p>{PAGE_TEXT}</p></body>".encode()), ".html")
            return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                    "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}
        return plain(url, max_chars)

    runner.fetch_fn = fetch
    report = runner.start("What did Acme announce recently about its third-quarter 2026 revenue?", budgets={"max_iterations": 1})["report"]
    rows = [r for r in report["established"] if "Revenue" in r["quote"]]
    assert rows and rows[0]["statement_row"]["figure"]["printed"] == "1,200"
    assert "'Revenue' for the 3 months ended 2026-09-30 is 1,200" in report["summary"]
    checked = [e["payload"] for e in env["runs"].events(report.get("run_id") or env["runs"].runs(1)[0]["run_id"]) if e["type"] == "relevance_checked"]
    assert any(c.get("checker") == "code:statement_row" for c in checked)


def test_another_companys_filing_does_not_index_the_subjects_figures(env):
    runner, _, _ = edgar_runner(env)
    s = {"run_id": "r", "cik": "0000001234", "subject": "Acme", "entity_name": "Acme", "company": "Acme"}
    assert "not the subject's CIK" in runner._not_the_subjects(s, "x", "https://www.sec.gov/Archives/edgar/data/1065280/000106528026000290/ex991_q326.htm")
    assert runner._not_the_subjects(s, "x", "https://www.sec.gov/Archives/edgar/data/1234/000000123426000009/acme3q-release.htm") is None

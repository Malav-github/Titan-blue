"""The roles skill (skills/roles) in Verify: who holds a role, from the company's own SEC filings (the officer who certified
the latest 10-Q, the 8-K Item 5.02 filings since), with names compared as people write them. Fictional company and people,
built on the shape of a real case (a chief executive's resignation filed on 8-K, a misspelled name in the statement)."""

import json
from datetime import date

import pytest

from checking import Checker, describe, subject_of
from skills import edgar, roles
from tests.test_edgar_skill import edgar_runner
from tests.test_runner import env  # noqa: F401
from tools.archive import archive_bytes, utc_now


@pytest.mark.parametrize("statement, org, role, person, event, reason", [
    ("NVIDIA CEO Jensen Hu-ang resigns over governance failure", "NVIDIA", "ceo", "Jensen Hu-ang", "departure", "governance failure"),
    ("Jensen Huang, CEO of NVIDIA, unveiled new chips.", "NVIDIA", "ceo", "Jensen Huang", "holds", None),
    ("Martina Cheung is the President and Chief Executive Officer of S&P Global.", "S&P Global", "ceo", "Martina Cheung", "holds", None),
    ("NVIDIA named Colette Kress as interim CEO.", "NVIDIA", "ceo", "Colette Kress", "appointment", None),
    ("NVIDIA's CFO Colette Kress said revenue grew.", "NVIDIA", "cfo", "Colette Kress", "holds", None),
    ("Acme Corp appointed Jane Doe as chief financial officer in March 2026.", "Acme Corp", "cfo", "Jane Doe", "appointment", None),
])
def test_a_role_statement_is_read_by_code(statement, org, role, person, event, reason):
    r = roles.parse(statement)
    assert (r.org, r.role, r.person, r.event, r.reason) == (org, role, person, event, reason)


def test_a_statement_with_no_named_person_is_not_a_role_statement():
    assert roles.parse("The CEO said demand was strong.") is None


@pytest.mark.parametrize("written, filed, match", [
    ("Jensen Hu-ang", "Jen-Hsun Huang", "spelling"),     # a hyphen in the surname is a misspelling
    ("Jensen Huang", "Jen-Hsun Huang", "same"),          # the name used and the name filed
    ("Jensen Haung", "Jen-Hsun Huang", "spelling"),
    ("Colette Kress", "Colette M. Kress", "same"),
    ("Lisa Su", "Jen-Hsun Huang", "different"),
])
def test_names_are_compared_as_people_write_them(written, filed, match):
    assert roles.same_person(written, filed)["match"] == match


def test_the_certifying_officer_and_title_are_read_from_exhibit_31():
    c = roles.certifier(EX311)
    assert c["name"] == "Jonathan Q. Smithers" and c["title"] == "President and Chief Executive Officer"


def test_the_subject_of_a_role_statement_is_the_company_not_the_person():
    assert subject_of("NVIDIA CEO Jensen Hu-ang resigns over governance failure") == "NVIDIA"


# --- a fake SEC ---------------------------------------------------------------------------------------------------------

EX311 = ("EXHIBIT 31.1\nCERTIFICATION\nI, Jonathan Q. Smithers, certify that:\n1. I have reviewed this Quarterly Report on Form 10-Q of "
         "Acme Corp;\nDate: August 26, 2026\n/s/JONATHAN Q. SMITHERS\nJonathan Q. Smithers\nPresident and Chief Executive Officer")
EX312 = ("EXHIBIT 31.2\nCERTIFICATION\nI, Maria Lopez, certify that:\n1. I have reviewed this Quarterly Report on Form 10-Q of "
         "Acme Corp;\nDate: August 26, 2026\n/s/MARIA LOPEZ\nMaria Lopez\nExecutive Vice President and Chief Financial Officer")
EIGHT_K = ("Item 5.02. Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers.\n"
           "On November 11, 2026, the Board of Directors of Acme Corp accepted the resignation of Jonathan Q. Smithers as President and "
           "Chief Executive Officer, effective immediately. Following Mr. Smithers’s resignation, the Board appointed Executive Vice "
           "President and Chief Financial Officer Maria Lopez, age 52, to serve as interim Chief Executive Officer. Ms. Lopez will "
           "continue to serve as Chief Financial Officer.")
BASE = "https://www.sec.gov/Archives/edgar/data/1234/"


def _index(rows):
    cells = "".join(f'<tr><td scope="row">{i}</td><td scope="row">{t}</td><td scope="row"><a href="/Archives/edgar/data/1234/{acc}/{n}">{n}</a>'
                    f'</td><td scope="row">{t}</td><td>1</td></tr>' for i, (acc, n, t) in enumerate(rows, 1))
    return f'<table class="tableFile" summary="Document Format Files">{cells}</table>'


class RolesSec:
    def __init__(self, world, filings_after_10q=True):
        self.world, self.after = world, filings_after_10q

    def _page(self, url, body: bytes, ctype="text/html"):
        sha, path = archive_bytes(body, ".json" if "json" in ctype else ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": ctype, "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    def fetch(self, url, max_chars=2000):
        if url == edgar.manifest()["tickers_url"]:
            return self._page(url, json.dumps({"0": {"cik_str": 1234, "ticker": "ACME", "title": "Acme Corp"}}).encode(), "application/json")
        pages = {
            BASE + "000000123426000001/0000001234-26-000001-index.html": _index([("000000123426000001", "acme-10q.htm", "10-Q"),
                                                                               ("000000123426000001", "acme-ex311.htm", "EX-31.1"),
                                                                               ("000000123426000001", "acme-ex312.htm", "EX-31.2")]),
            BASE + "000000123426000001/acme-ex311.htm": "<html><body><pre>" + EX311 + "</pre></body></html>",
            BASE + "000000123426000001/acme-ex312.htm": "<html><body><pre>" + EX312 + "</pre></body></html>",
            BASE + "000000123426000002/acme-8k.htm": "<html><body><p>" + EIGHT_K.replace("\n", "</p><p>") + "</p></body></html>",
        }
        if url in pages:
            return self._page(url, pages[url].encode())
        return {"url": url, "final_url": url, "status": 404}

    def call(self, registry, connector, endpoint, params, run_id=None):
        rows = ([("8-K", "2026-11-12", "5.02,9.01", "acme-8k.htm", "0000001234-26-000002")] if self.after else []) + \
               [("10-Q", "2026-08-26", "", "acme-10q.htm", "0000001234-26-000001")]
        sub = {"name": "Acme Corp", "fiscalYearEnd": "1231", "filings": {"recent": {
            "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows], "items": [r[2] for r in rows],
            "primaryDocument": [r[3] for r in rows], "accessionNumber": [r[4] for r in rows], "reportDate": [r[1] for r in rows],
            "primaryDocDescription": ["" for _ in rows]}}}
        sha, path = archive_bytes(json.dumps(sub).encode(), ".json")
        url = f"https://data.sec.gov/submissions/CIK{params['cik']}.json"
        return {"ok": True, "url": url, "final_url": url, "status": 200, "content_type": "application/json", "encoding": "utf-8",
                "method": "api", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "source_tier": "regulatory"}


def _checked(env, text, after=True):
    runner, _, _ = edgar_runner(env)
    sec = RolesSec(env["world"], after)
    runner.fetch_fn, runner.call_api_fn = sec.fetch, sec.call
    runner.search_fn = lambda q, max_results=8, **k: {"results": []}
    Checker(runner).start("c-roles", {"kind": "text", "text": text})
    return {s["text"]: s for s in describe(env["runs"], "c-roles")["statements"] if s["checkable"]}


def test_a_misspelled_resignation_with_an_unfiled_reason_is_confirmed_in_part(env):
    [s] = _checked(env, "Acme Corp CEO Jonathan Smithrs resigns over accounting fraud").values()
    assert s["outcome"] == "partial"
    checks = {c["check"]: c for c in s["checks"]}
    assert checks["role_holder"]["quote"] == "I, Jonathan Q. Smithers, certify"
    assert checks["name"]["result"] == "the same person, misspelled" and "'Smithrs' where the filing writes 'Smithers'" in checks["name"]["detail"]
    assert checks["role_event"]["quote"].startswith("On November 11, 2026, the Board of Directors of Acme Corp accepted the resignation of Jonathan Q. Smithers")
    assert checks["reason"]["result"] == "not stated" and any("accounting fraud" in n for n in s["notes"])


@pytest.mark.parametrize("statement, outcome", [
    ("Maria Lopez is the interim CEO of Acme Corp.", "corroborated"),            # appointed after the 10-Q was certified
    ("Acme Corp's CFO Maria Lopez resigned last week.", "contradicted"),           # she stays CFO: no departure filed
    ("Lisa Su is the CEO of Acme Corp.", "contradicted"),                          # another person certified; no appointment filed
    ("Acme Corp CEO Jonathan Smithers resigned.", "corroborated"),
])
def test_role_statements_against_the_filings(env, statement, outcome):
    [s] = _checked(env, statement).values()
    assert s["outcome"] == outcome, s["checks"]


def test_without_an_8k_a_resignation_of_the_certified_ceo_is_contradicted_with_the_four_day_window(env):
    [s] = _checked(env, "Acme Corp CEO Jonathan Smithers resigned.", after=False).values()
    event = next(c for c in s["checks"] if c["check"] == "role_event")
    assert s["outcome"] == "contradicted" and "four business days" in event["detail"]

"""Verify (checking.py): a pasted text, a link or an uploaded file checked against independent sources, apart from research."""

import pytest

from checking import Checker, classify, describe, subject_of
from tests.test_run_control import H, api
from tests.test_runner import env, make_runner  # noqa: F401
from tests.test_verification import FILLER, page, with_web

PASTED = ("Acme Corp Pricing Update\n"
          "Acme Corp said its Starter plan costs $29 per month for small teams. "
          "We expect prices to rise next year as demand grows. "
          "Acme Corp appointed Jane Doe as chief financial officer in March 2026.")


@pytest.mark.parametrize("statement, checkable", [
    ("Acme Corp said its Starter plan costs $29 per month for small teams.", True),
    ("Acme Corp appointed Jane Doe as chief financial officer in March 2026.", True),
    ("We expect prices to rise next year as demand grows.", False),            # a forecast
    ("The company believes its product is the best on the market today.", False),   # an opinion
    ("Acme Corp Pricing Update", False),                                       # a heading
    ("Our team works hard to make customers happy every single day.", False),  # nothing to check
])
def test_code_decides_which_statements_can_be_checked(statement, checkable):
    assert classify(statement)[0] is checkable


def test_the_subject_is_the_organisation_the_text_names_most_unless_given():
    assert subject_of(PASTED) == "Acme Corp"
    assert subject_of(PASTED, "Acme Corporation") == "Acme Corporation"


def _check(runner, request, check_id="c1"):
    out = Checker(runner).start(check_id, request)
    return out, describe(runner.runs, check_id)


def test_a_pasted_text_is_split_word_for_word_and_each_checkable_statement_checked(env):
    runner = make_runner(env)
    agree = FILLER + "Acme Corp's Starter plan costs $29 per month, the company confirmed on Monday. " + FILLER
    with_web(runner, {"https://news.example/acme": agree}, ["https://news.example/acme"])
    out, d = _check(runner, {"kind": "text", "text": PASTED})
    assert out["status"] == "finished" and d["subject"] == "Acme Corp"
    by_text = {s["text"]: s for s in d["statements"]}
    price = by_text["Acme Corp said its Starter plan costs $29 per month for small teams."]
    assert price["outcome"] == "corroborated" and d["text"][price["span"][0]:price["span"][1]] == price["text"]
    second = next(c for c in price["checks"] if c["check"] == "second_source")
    assert second["url"] == "https://news.example/acme" and second["quote"].startswith("Acme Corp's Starter plan costs $29")
    forecast = by_text["We expect prices to rise next year as demand grows."]
    assert not forecast["checkable"] and "forecast" in forecast["why"] and "outcome" not in forecast
    assert d["counts"]["corroborated"] == 1 and d["counts"]["not_checkable"] == 2


def test_a_copy_of_the_text_elsewhere_is_never_its_own_evidence(env):
    runner = make_runner(env)
    copy = FILLER + PASTED.replace("\n", " ") + " " + FILLER                    # the same text, syndicated on another site
    with_web(runner, {"https://wire.example/acme": copy}, ["https://wire.example/acme"])
    _, d = _check(runner, {"kind": "text", "text": PASTED})
    assert {s["outcome"] for s in d["statements"] if s["checkable"]} == {"unconfirmed"}


def test_another_figure_for_the_same_thing_is_a_contradiction(env):
    runner = make_runner(env)
    other = FILLER + "Acme Corp raised its Starter plan price to $39 per month this week. " + FILLER
    with_web(runner, {"https://news.example/acme-raises": other}, ["https://news.example/acme-raises"])
    _, d = _check(runner, {"kind": "text", "text": PASTED})
    price = next(s for s in d["statements"] if "$29" in s["text"])
    contra = next(c for c in price["checks"] if c["check"] == "contradiction")
    assert price["outcome"] == "contradicted" and "$39" in contra["quote"]


@pytest.mark.parametrize("written, outcome", [("$1,200 million", "corroborated"), ("$1,500 million", "contradicted")])
def test_a_figure_is_checked_against_the_companys_own_statements(env, written, outcome):
    from tests.test_statement_answer import _stored
    from tests.test_statements import RELEASE
    runner = make_runner(env)
    with_web(runner, {}, [])                                                      # no web source at all
    [t] = _stored(env["store"], RELEASE, "https://www.sec.gov/Archives/edgar/data/1/1/release.htm")
    env["store"].link_source_entity(t["source_sha256"], env["store"].add_entity("Acme Corp", kind="subject"))
    # 'the third quarter of 2026' needs the company's year end, which this release's statements do not show: a stated period
    _, d = _check(runner, {"kind": "text", "text": f"Acme Corp reported revenue of {written} for the three months ended September 30, 2026."})
    [s] = [s for s in d["statements"] if s["checkable"]]
    figures = next(c for c in s["checks"] if c["check"] == "figures")
    assert s["outcome"] == outcome and figures["sources"][0]["url"].endswith("release.htm"), figures


def test_a_link_is_read_and_its_own_site_is_not_a_second_source(env):
    runner = make_runner(env)
    url = "https://blog.example/acme-pricing"
    same_site = FILLER + "Acme Corp's Starter plan costs $29 per month, we can confirm. " + FILLER
    with_web(runner, {url: PASTED.replace("\n", ". "), "https://blog.example/other": same_site}, [url, "https://blog.example/other"])
    _, d = _check(runner, {"kind": "url", "url": url})
    assert d["input"]["url"] == url and d["status"] == "finished"
    assert {s["outcome"] for s in d["statements"] if s["checkable"]} == {"unconfirmed"}


def test_a_bad_input_fails_with_the_reason(env):
    runner = make_runner(env)
    with pytest.raises(ValueError, match="no text"):
        Checker(runner).start("c2", {"kind": "text", "text": "   "})
    with pytest.raises(ValueError, match="http"):
        Checker(runner).start("c3", {"kind": "url", "url": "acme.example"})


def test_the_api_starts_a_check_reports_it_and_gives_a_word_copy(env, tmp_path):
    client, jobs, runner = api(env, tmp_path)
    agree = FILLER + "Acme Corp's Starter plan costs $29 per month, the company confirmed on Monday. " + FILLER
    with_web(jobs.runner, {"https://news.example/acme": agree}, ["https://news.example/acme"])
    assert client.post("/api/check", json={"kind": "url", "url": "nope"}, headers=H).status_code == 422
    rid = client.post("/api/check", json={"kind": "text", "text": PASTED}, headers=H).json()["run_id"]
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["kind"] == "check" and run["status"] == "finished" and run["counts"]["corroborated"] == 1
    listed = next(r for r in client.get("/api/runs", headers=H).json() if r["run_id"] == rid)
    assert listed["kind"] == "check" and listed["question"].startswith("Verify: Acme Corp Pricing Update")
    doc = client.get(f"/api/runs/{rid}/check.docx", headers=H)
    assert doc.status_code == 200 and doc.headers["content-type"].startswith("application/vnd.openxmlformats")
    xml = __import__("zipfile").ZipFile(__import__("io").BytesIO(doc.content)).read("word/document.xml").decode()
    assert "Corroborated" in xml and "Starter plan costs $29" in xml and "a forecast or an opinion" in xml

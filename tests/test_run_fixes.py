"""Fixes from live runs on 2026-11-07: each covers for a limit of the local model or a fault in Titan's own rules.

- the planner listed 'S&P Global made an announcement in 2026' as a premise; the 8B premise screen then refused the question
- the planner wrote a guessed answer ('revenue $3.68B') into a search query
- a list question ('What AI initiatives did S&P Global announce in 2026?') was planned as one fact and stopped at one release
- a release and its PR Newswire copy counted as two independent sources
- SEC company metadata ('its ticker is SPGI') stopped the reading before the 10-Q that holds the revenue
"""

import pytest

import corroboration
import diagnosis
from runner import Plan, no_guessed_figures, specific_premise
from tests.scripted import Extraction, ExtractedClaim
from skills.planning import enumeration
from tests.test_runner import GOOD_QUOTE, env, make_runner, types  # noqa: F401

SPGI = ["S&P Global", "S&P Global Inc."]


@pytest.mark.parametrize("premise,specific", [
    ("S&P Global made an announcement in 2026", False),
    ("S&P Global announced something in 2026", False),
    ("S&P Global announced a strategic review of Capital IQ", True),
    ("A paper titled 'Orange Lattices' exists", True),
    ("Acme published a 2040 annual report", True),
])
def test_a_premise_must_name_something_specific(premise, specific):
    assert specific_premise(premise, SPGI) is specific


def test_a_guessed_figure_is_taken_out_of_a_query_but_a_given_one_stays():
    assert no_guessed_figures("S&P Global 3Q 2026 revenue $3.68B source site:tradingview.com", "What was S&P Global's Q3 2026 revenue?") == \
        "S&P Global 3Q 2026 revenue source site:tradingview.com"
    assert no_guessed_figures("Acme $2 billion deal", "Did Acme agree a $2 billion deal?") == "Acme $2 billion deal"
    assert no_guessed_figures("Acme layoffs 12%", "How many did Acme lay off?") == "Acme layoffs"


@pytest.mark.parametrize("question,listed", [
    ("What did S&P Global announce in 2026?", True),
    ("What AI initiatives did S&P Global announce in 2026?", True),
    ("List Acme's acquisitions since 2020", True),
    ("Which partnerships has Dropbox announced?", True),
    ("S&P Global's latest strategic move.", False),
    ("What is the latest announcement from Acme?", False),
    ("What was Apple's revenue for fiscal 2025?", False),
    ("Who is the CEO of Dropbox?", False),
])
def test_a_list_question_is_recognised_by_code(question, listed):
    assert bool(enumeration(question, "S&P Global", {"years": [2026]})) is listed


def test_a_list_question_gets_one_item_for_every_event_and_searches_aimed_at_releases():
    e = enumeration("What AI initiatives did S&P Global announce in 2026?", "S&P Global", {"years": [2026]})
    assert e["item"] == "Each distinct AI initiative S&P Global announced in 2026, with its date"
    assert 'site:prnewswire.com "S&P Global" AI 2026' in e["queries"] and all("S&P Global" in q for q in e["queries"])
    assert enumeration("List Acme's acquisitions", "Acme", {})["queries"][0] == '"Acme" announces acquisition'


RELEASE = ("S&P Global today announced the expansion of AI-powered capabilities across its private markets ecosystem, bringing "
           "agentic workflows to iLEVEL customers worldwide. The enhancements include an MCP server that connects iLEVEL data to "
           "AI tools used by investment teams. The company said the launch advances its broader private markets strategy for 2026.")


def test_a_release_and_its_wire_copy_are_one_document_but_two_releases_are_two():
    other = ("S&P Global announced a strategic review of its Capital IQ platform and associated data businesses, including a "
             "possible separation. The company has retained advisors to assist with the review of strategic alternatives for 2026. "
             "No timetable has been set for the completion of the review, the company said in its statement today.")
    prints = {"newsroom": corroboration.fingerprint("S&P Global Press Release\n" + RELEASE),
              "wire": corroboration.fingerprint("PR Newswire / NEW YORK, Oct. 22, 2026 /\n" + RELEASE + " SOURCE S&P Global"),
              "other": corroboration.fingerprint(other)}
    groups = corroboration.copy_groups(prints)
    assert groups["newsroom"] == groups["wire"] == "newsroom" and groups["other"] == "other"


def test_the_failures_of_a_run_are_named_with_their_kind_and_workaround():
    events = [{"type": "claim_rejected", "payload": {"claim": "x", "reason": "quote not found verbatim in the source text"}},
              {"type": "claim_rejected", "payload": {"claim": "y", "reason": "quote not found verbatim in the source text"}},
              {"type": "api_skipped", "payload": {"reason": "0001564590 is not in the question; identifiers are never guessed"}},
              {"type": "source_failed", "payload": {"url": "https://investor.spglobal.com/x", "error": "RuntimeError: HTTP 403"}},
              {"type": "search_done", "payload": {"warnings": [], "notes": ["searxng engine duckduckgo: CAPTCHA"]}},
              {"type": "search_done", "payload": {"warnings": [], "notes": []}}]
    found = {d["id"]: d for d in diagnosis.diagnose(events)}
    assert found["misquoted"]["count"] == 2 and found["misquoted"]["kind"] == "llm"
    assert found["guessed_id"]["kind"] == "llm" and "EDGAR skill" in found["guessed_id"]["handled_by"]
    assert found["blocked"]["kind"] == "source" and found["search_degraded"]["count"] == 1
    assert [d["kind"] for d in diagnosis.diagnose(events)][0] == "llm"            # model limits are listed first


# --- in a run ---

def test_company_metadata_does_not_stop_the_reading_before_the_page_with_the_fact(env):
    """Two pages: the first only says what the company is; the second states the price. The early stop must not fire on
    the first (the model files every claim under the one needed fact)."""
    pages = iter(["Acme Corp is a company listed on NYSE. Acme Corp is incorporated in New York state. " * 6, None])

    def extract(p):
        if "listed on NYSE" in p:
            return Extraction(claims=[ExtractedClaim(predicate="company.listing", value="NYSE", claim="Acme Corp is listed on NYSE.",
                                                     quote="Acme Corp is a company listed on NYSE.", needed_fact=1)])
        return Extraction(claims=[ExtractedClaim(predicate="pricing.starter", value="$29", claim="Acme's Starter plan costs $29 per month.",
                                                 quote=GOOD_QUOTE, needed_fact=1)])

    from runner import Selection
    runner = make_runner(env, Extraction=extract, Plan=lambda p: Plan(subject="Acme", queries=["acme"], checklist=["Starter plan price"]),
                         Selection=lambda p: Selection(numbers=[1, 2]))
    world = env["world"]
    real_fetch = world.fetch

    def fetch(url, max_chars=2000):
        text = next(pages, None)
        if text is None:
            return real_fetch(url, max_chars)
        from tools.archive import archive_bytes, utc_now
        sha, path = archive_bytes(f"<html><body><article><p>{text}</p></article></body></html>".encode(), ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    runner.fetch_fn = fetch
    result = runner.start("What does Acme charge?", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    assert not any(e["type"] == "extract_stopped" and e["payload"]["read"] == 1 for e in events)   # not stopped by the metadata page
    assert any("$29" in r["claim"] for r in result["report"]["established"])


def test_a_list_question_is_planned_by_code_and_written_as_a_round_up(env):
    runner = make_runner(env, Plan=lambda p: Plan(subject="AI", queries=["AI investor relations annual report 10-K"], checklist=["AI initiatives"]))
    result = runner.start("What AI initiatives did Acme announce in 2026?", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    listed = next(e["payload"] for e in events if e["type"] == "list_question")
    assert listed["item"] == "Each distinct AI initiative Acme announced in 2026, with its date"
    plan = next(e["payload"] for e in events if e["type"] == "plan_ready")
    assert plan["checklist"] == [listed["item"]] and all("Acme" in q for q in plan["queries"])
    assert result["report"]["list_question"] == listed["item"] and "extract_stopped" not in types(env, result["run_id"])
    assert (result["report"].get("quality") or {}).get("form") in ("roundup", None)


def test_claims_not_yet_checked_never_stop_the_reading(env):
    """The real case (LyondellBasell): unchecked claims from an old earnings release looked like the answer and stopped the
    reading after one document of five; the checks then rejected them and the other documents were never read."""
    runner = make_runner(env)
    s = {"checklist": ["Names of the chemical recycling technologies"], "verdicts": {}, "relevant": {}, "report_type": "brief",
         "question": "q", "subject": "Acme", "company": "Acme", "entity_name": "Acme"}
    assert not runner._backed(s, {"1": 0}, 1, 2)                     # recorded, not checked: keep reading


def test_the_round_up_is_written_by_code_one_dated_cited_paragraph_per_event(env):
    from runner import Plan
    from skills import writing
    runner = make_runner(env)
    s = {"run_id": "r1", "question": "What did Acme announce in 2026?", "subject": "Acme", "company": "Acme", "entity_name": "Acme",
         "time_scope": {"years": [2026]}, "enumerate": enumeration("What did Acme announce in 2026?", "Acme", {"years": [2026]})}
    from tools.archive import archive_bytes, utc_now
    def source(text, url):
        sha, path = archive_bytes(f"<html><body><article><p>{text}</p></article></body></html>".encode(), ".html")
        return env["store"].ingest({"url": url, "final_url": url, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                                    "fetched_at": utc_now(), "sha256": sha, "archive_path": path})
    a = source("Acme launched Beacon, an AI search tool, for business customers worldwide today. " * 3, "https://acme.example/beacon")
    b = source("Acme agreed to acquire Fabrikam for one billion dollars in cash, the company said today. " * 3, "https://acme.example/fabrikam")
    rows = [{"claim_id": 1, "claim": "Acme launched Beacon, an AI search tool.", "source_sha256": a, "source_url": "https://acme.example/beacon",
             "times": [{"kind": "announced", "value": "2026-03-02"}], "role": "answer"},
            {"claim_id": 2, "claim": "Acme agreed to acquire Fabrikam.", "source_sha256": b, "source_url": "https://acme.example/fabrikam",
             "times": [{"kind": "announced", "value": "2026-09-14"}], "role": "answer"}]
    ctx = [{"claim_id": 3, "claim": "The deal values Fabrikam at $1 billion in cash.", "source_sha256": b, "source_url": "https://acme.example/fabrikam",
            "times": [], "role": "context"}]
    facts = writing.build_facts(rows) + writing.build_facts(ctx, role="context")
    text = runner._roundup(s, rows, ctx, facts, ["Each distinct announcement in December"])
    paras = text.split("\n\n")
    assert paras[0].startswith("The evidence read establishes 2 distinct announcements by Acme in 2026")
    # each event: its date, then its own sentences quoted, with the exact address
    assert paras[1] == ("On 14 September 2026:\n- “Acme agreed to acquire Fabrikam.” (acme.example, https://acme.example/fabrikam) [claim:2]"
                        "\n- “The deal values Fabrikam at $1 billion in cash.” (acme.example, https://acme.example/fabrikam) [claim:3]")
    assert paras[2] == "On 2 March 2026:\n- “Acme launched Beacon, an AI search tool.” (acme.example, https://acme.example/beacon) [claim:1]"
    assert paras[3].startswith("Not established from what was read")


@pytest.mark.parametrize("claim,remark", [
    ("S&P Global's revenue for the third quarter of 2026 is not directly provided in the passage.", True),   # a live run's 'answer'
    ("The figure is not stated.", True),
    ("Revenue was $3,678 million in the third quarter of 2026.", False),
    ("Terms of the deal were not disclosed.", False),
    ("Acme said the deal was not disclosed to regulators.", False),
])
def test_a_remark_about_the_page_is_not_a_claim(claim, remark):
    from runner import NON_FACT
    assert bool(NON_FACT.search(claim)) is remark

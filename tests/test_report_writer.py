"""Writing a report (skills/writing/report.py): section by section, each sentence locked to the claims it cites."""

from datetime import date

from skills.writing import report
from skills.writing.facts import build_facts
from tests.test_runner import env, make_runner  # noqa: F401

ROWS = [{"claim_id": 1, "claim": "S&P Global launched the AI Data Portal on 15 October 2026.", "quote": "S&P Global launched the AI Data Portal.",
         "value": "", "source_url": "https://press.spglobal.com/a", "fact": 0, "times": []},
        {"claim_id": 2, "claim": "Revenue was $3,678 million in the third quarter.", "quote": "Revenue was $3,678 million in the third quarter.",
         "value": "$3,678 million", "source_url": "https://www.sec.gov/x.htm", "fact": 1, "times": []}]


def test_the_lock_keeps_cited_sentences_and_removes_uncited_ones_and_unsupported_figures():
    facts = build_facts(ROWS)
    text = ("S&P Global launched its AI Data Portal [claim:1]. It has long led the market. Revenue reached $3,678 million [claim:2]. "
            "Revenue reached $4.1 billion [claim:2]. Revenue grew [claim:9].")
    kept, removed = report.lock(text, facts)
    assert kept == "S&P Global launched its AI Data Portal [claim:1]. Revenue reached $3,678 million [claim:2]."
    assert removed == ["It has long led the market.", "Revenue reached $4.1 billion [claim:2].", "Revenue grew [claim:9]."]


def test_sections_are_written_one_call_each_with_a_summary_last():
    calls = []

    def ask(prompt):
        calls.append(prompt)
        if "summary" in prompt.lower() and "SECTIONS" in prompt:
            return "S&P Global launched the AI Data Portal [claim:1] and reported $3,678 million in revenue [claim:2]."
        return "S&P Global launched its AI Data Portal [claim:1]." if "QUESTION: What AI" in prompt else "Revenue was $3,678 million [claim:2]."

    sections = ["What AI products did S&P Global launch?", "What figures did it report?", "What risks were raised?"]
    body, info = report.write(ask, "Deep research report: S&P Global AI", sections, lambda i: [r for r in ROWS if r["fact"] == i], None,
                              date(2026, 11, 11), lambda: 500)
    assert len(calls) == 3                                                     # two sections and the summary; the empty one costs nothing
    assert body.startswith("## Summary\n\nS&P Global launched the AI Data Portal [claim:1]")
    assert "## What risks were raised?\n\nNot established from what was read." in body
    assert info["sections_empty"] == ["What risks were raised?"] and info["summary"]


def test_when_time_runs_short_a_section_is_its_quotes_not_skipped():
    body, info = report.write(lambda p: "never called [claim:1].", "Report", ["What AI products did S&P Global launch?"],
                              lambda i: ROWS[:1], None, date(2026, 11, 11), lambda: 5, quote_line=lambda r: f"“{r['quote']}” [claim:{r['claim_id']}]")
    assert "- “S&P Global launched the AI Data Portal.” [claim:1]" in body and info["sections_from_quotes"] == ["What AI products did S&P Global launch?"]


def test_a_general_report_run_has_its_outline_summary_sources_and_method(env):
    from runner import Plan, Summary
    runner = make_runner(env, Plan=lambda p: Plan(subject="Acme", company="Acme", queries=["acme pricing"],
                                                  checklist=["What does the Starter plan cost?", "What does the Team plan cost?",
                                                             "Who are Acme's customers?", "What changed this year?"]),
                         Summary=lambda p: Summary(text="The Starter plan costs $29 a month [claim:1]." if "SECTIONS" not in p
                                                   else "Acme's Starter plan costs $29 a month [claim:1]."))
    result = runner.start("What does Acme charge?", report_type="general_report", budgets={"max_iterations": 1})
    text = result["report"]["summary"]
    assert text.startswith("# General report: What does Acme charge?")
    for part in ("## Summary", "## What does the Starter plan cost?", "## Sources", "## How the research was done", "## Not established"):
        assert part in text
    assert "characters scanned in full by code" in text and "https://acme.example/pricing" in text
    assert result["report"]["quality"]["writer"] == "model:sections"

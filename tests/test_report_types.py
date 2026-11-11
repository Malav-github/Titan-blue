"""Report types (report_types.py): the type asked for decides the outline, budgets, settling, relevance and the deadline."""

import pytest

import report_types as R
from runner import MAX_PICKS, Extraction, _on_topic, _topic_stems
from tests.test_runner import env, make_runner  # noqa: F401


@pytest.mark.parametrize("question, kind", [
    ("What did Acme report for the third quarter?", "brief"),                          # the verb is not a request for a report
    ("Write a report on S&P Global AI announcements in 2026", "general_report"),
    ("An overview of Infosys deals this year", "general_report"),
    ("A deep dive into Tata Motors EV strategy", "deep_report"),
    ("Give me a comprehensive analysis of Jollibee expansion", "deep_report"),
    ("Draft a white paper on AI in credit ratings", "white_paper"),
])
def test_the_wording_chooses_a_type_when_the_person_does_not(question, kind):
    assert R.choose(None, question)["id"] == kind


def test_the_persons_choice_wins_and_an_unknown_type_is_refused():
    assert R.choose("deep_report", "What does Acme charge?")["id"] == "deep_report"
    with pytest.raises(ValueError):
        R.choose("novel", "q")


def test_every_type_fits_the_ten_minute_limit_with_time_to_write():
    for t in R.data()["types"]:
        assert t["deadline_seconds"] <= 600 and t["writing_reserve_seconds"] < t["deadline_seconds"]


def test_a_report_plans_an_outline_and_uses_its_own_budgets(env):
    from runner import Plan
    seen = []
    runner = make_runner(env, Plan=lambda p: seen.append(p) or Plan(
        subject="Acme", company="Acme", queries=["acme ai"],
        checklist=["What AI products did Acme launch?", "When?", "Partners?", "Figures?", "Outlook?", "Risks?", "Extra"]))
    result = runner.start("What is Acme doing in AI?", report_type="general_report", require_approval=True,
                          budgets={"max_searches": 6, "max_fetches": 8})               # the form's defaults
    plan = result["approval_request"]
    assert "OUTLINE" in seen[0] and "REPORT REQUESTED: General report" in seen[0]
    assert len(plan["checklist"]) == 5                                                  # the type's most sections
    started = next(e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "run_started")
    assert started["report_type"] == "general_report" and started["budgets"]["max_fetches"] == 18     # raised, never lowered
    assert "premise_screened" not in [e["type"] for e in env["runs"].events(result["run_id"])]          # moved to verification


def test_a_report_judges_relevance_when_it_reads_and_settles_a_section_on_several_sites(env):
    result = make_runner(env).start("What does Acme charge?", report_type="general_report", budgets={"max_iterations": 1})
    checked = [e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "relevance_checked"]
    assert checked and all(c["checker"] == "code:extraction" for c in checked)        # no model call per claim
    assert result["report"]["gaps"]                       # one site (even Acme's own) does not settle a report's section


def test_the_deadline_moves_the_run_to_its_report(env, monkeypatch):
    from runner import Runner
    monkeypatch.setattr(Runner, "_time_left", staticmethod(lambda s: 0.0))     # no time left: it is time to write
    result = make_runner(env).start("What does Acme charge?", budgets={"max_iterations": 3})
    events = env["runs"].events(result["run_id"])
    reached = [e["payload"] for e in events if e["type"] == "deadline_reached"]
    assert result["status"] == "finished" and "report_ready" in [e["type"] for e in events]
    assert reached and reached[0]["stage"] == "extract"                        # stopped reading, wrote with what it had
    assert env["store"].counts()["claims"] == 0 and result["report"]["gaps"] == ["Starter plan price"]


def test_working_time_is_counted_by_every_step_and_waiting_is_not(env):
    runner = make_runner(env)
    waiting = runner.start("What does Acme charge?", require_approval=True)
    used_before = runner.snapshot(waiting["run_id"])["values"]["time_used"]
    done = runner.resume(waiting["run_id"], {"approved": True})
    used_after = runner.snapshot(waiting["run_id"])["values"]["time_used"]
    assert done["status"] == "finished" and used_after >= used_before >= 0 and used_after < 60


def test_a_report_refuses_a_sentence_off_its_sections_topic():
    topic = _topic_stems("Each distinct AI announcement S&P Global announced in 2026, with its date", {"global"})
    assert topic == {"ai"}
    assert not _on_topic("As previously announced, the Board has authorized a quarterly cash dividend of $0.97 per share.", topic)
    assert _on_topic("S&P Global today announced that Visible Alpha data is now available through the S&P Global AI Data Portal.", topic)


def test_the_model_answers_with_numbers_only_and_is_capped():
    schema = Extraction.model_json_schema()
    assert set(schema["properties"]) == {"picks"} and schema["properties"]["picks"]["maxItems"] == MAX_PICKS
    many = Extraction.model_validate({"picks": [[1, n] for n in range(1, 200)] + [["x"], [1]]})
    assert len(many.picks) == MAX_PICKS and [c.sentences for c in many.claims][:2] == [[1], [2]]
    assert all(c.needed_fact == 1 and c.predicate == "" for c in many.claims)

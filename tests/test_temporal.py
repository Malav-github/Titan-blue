"""The temporal skill, phase 1: the clock, the time scope of a question, and search that uses both."""

from datetime import datetime, timezone

import pytest

import config
from skills.temporal import manifest, setting
from skills.temporal.clock import now, zone_problem
from skills.temporal.display import short_date
from skills.temporal.intent import TimeScope, apply_year_terms, classify
from tests.test_runner import (GOOD_QUOTE, PAGE_TEXT, ScriptedLLM, World, default_handlers, env, make_runner,  # noqa: F401
                               types)
from runner import Plan


def at(*args):
    moment = datetime(*args, tzinfo=timezone.utc)
    return lambda: moment


# --- the clock ---------------------------------------------------------------------------------------------------

def test_now_describes_the_date_weekday_zone_and_quarter():
    n = now(at(2026, 11, 4, 3, 51), zone="Asia/Kolkata")
    assert n.local.hour == 9 and n.local.minute == 21 and n.zone == "Asia/Kolkata"
    assert n.describe() == "2026-11-04 (Wednesday), 09:21 IST (UTC+05:30); calendar quarter Q4 2026"
    assert n.as_dict() == {"local": "2026-11-04T09:21:00+05:30", "utc": "2026-11-04T03:51:00+00:00", "zone": "Asia/Kolkata"}
    assert (n.year, n.quarter, str(n.date)) == (2026, 4, "2026-11-04")


@pytest.mark.parametrize("month, day, quarter", [(1, 1, 1), (3, 31, 1), (4, 1, 2), (9, 30, 3), (10, 1, 4), (12, 31, 4)])
def test_calendar_quarter_boundaries(month, day, quarter):
    assert now(at(2026, month, day, 12), zone="UTC").quarter == quarter


def test_the_local_date_can_differ_from_the_utc_date():
    n = now(at(2026, 12, 31, 20, 0), zone="Asia/Kolkata")           # 01:30 on 1 January in India
    assert str(n.utc.date()) == "2026-12-31" and str(n.date) == "2027-01-01" and n.year == 2027


def test_daylight_saving_changes_the_offset_not_the_zone():
    assert now(at(2026, 7, 1, 12), zone="America/New_York").offset_label() == "UTC-04:00"
    assert now(at(2026, 12, 1, 12), zone="America/New_York").offset_label() == "UTC-05:00"


def test_the_configured_zone_is_used_and_a_bad_one_falls_back_to_the_system_zone(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "Europe/Berlin")
    assert now(at(2026, 6, 1, 12)).zone == "Europe/Berlin"
    monkeypatch.setattr(config, "TIMEZONE", "Mars/Olympus")
    fallback = now(at(2026, 6, 1, 12))
    assert fallback.zone == "system" and fallback.local.tzinfo is not None           # planning never fails over a typo
    assert "not a time zone name" in zone_problem("Mars/Olympus") and zone_problem("") is None and zone_problem("UTC") is None


def test_a_clock_without_a_timezone_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        now(lambda: datetime(2026, 1, 1))


def test_the_configuration_check_reports_a_bad_time_zone(monkeypatch, capsys):
    monkeypatch.setattr(config, "TIMEZONE", "Mars/Olympus")
    assert config.validate_config() is False
    assert "TITAN_TIMEZONE" in capsys.readouterr().out


# --- the time scope of a question --------------------------------------------------------------------------------

NOW = now(at(2026, 11, 4, 3, 51), zone="Asia/Kolkata")


@pytest.mark.parametrize("question, intent, recent, recency", [
    ("In what year was Dropbox founded?", "historical", False, None),
    ("Who founded Stripe?", "historical", False, None),
    ("Who is the current CEO of Dropbox?", "latest", True, "year"),
    ("Who is the CEO of Dropbox?", "latest", True, "year"),
    ("What is Apple's latest iPhone?", "latest", True, "year"),
    ("What happened today in AI regulation?", "latest", True, "day"),
    ("What did OpenAI announce this week?", "latest", True, "week"),
    ("Which companies raised funding this month?", "latest", True, "month"),
    ("When was the paper 'Attention Is All You Need' first submitted?", "historical", False, None),
    ("Who won the Nobel Prize in Physics in 2018?", "point_in_time", False, None),
    ("What were Apple's net sales for fiscal 2025?", "period", False, None),
    ("What was Tesla's revenue in Q3 2025?", "period", False, None),
    ("What is Apple's net income?", "period", True, "year"),
    ("What was Microsoft's latest quarterly revenue?", "period", True, "year"),
    ("Give me a timeline of Dropbox since 2015", "timeline", False, None),
    ("Key events for Nvidia between 2019 and 2023", "timeline", False, None),
    ("History of OpenAI funding rounds", "timeline", False, None),
])
def test_question_scopes(question, intent, recent, recency):
    scope = classify(question, NOW)
    assert (scope.intent, scope.recent, scope.recency) == (intent, recent, recency), scope


def test_windows_and_years_come_from_the_question():
    assert classify("Give me a timeline of Dropbox since 2015", NOW).window_start == "2015-01-01"
    assert classify("Give me a timeline of Dropbox since 2015", NOW).window_end == "2026-11-04"
    between = classify("Key events for Nvidia between 2019 and 2023", NOW)
    assert (between.window_start, between.window_end, between.years) == ("2019-01-01", "2023-12-31", (2019, 2023))
    year = classify("What were Apple's net sales for fiscal 2025?", NOW)
    assert (year.window_start, year.window_end, year.period_sensitive) == ("2025-01-01", "2025-12-31", True)
    assert classify("In what year was Dropbox founded?", NOW).window_start is None


def test_a_named_year_means_the_age_of_sources_is_not_the_point():
    assert classify("What were the latest results for 2024?", NOW).recent is False


def test_classification_is_stable_and_case_insensitive():
    a, b = classify("WHO IS THE CURRENT CEO of Dropbox?", NOW), classify("who is the current ceo of dropbox?", NOW)
    assert a == b and a.intent == "latest"
    assert all(i in ("historical", "point_in_time", "latest", "period", "timeline") for i in [classify(q, NOW).intent for q in ("", "?", "x")])


def test_guidance_names_today_and_the_recency_window():
    text = classify("Who is the current CEO of Dropbox?", NOW).guidance(NOW)
    assert "2026-11-04" in text and "last year" in text and "CURRENT" in text
    assert "latest reported period" in classify("What is Apple's net income?", NOW).guidance(NOW)
    assert "fixed past fact" in classify("In what year was Dropbox founded?", NOW).guidance(NOW)


# --- year terms in queries ---------------------------------------------------------------------------------------

def test_year_terms_are_added_only_for_questions_about_the_current_state():
    latest = classify("Who is the current CEO of Dropbox?", NOW)
    assert apply_year_terms(["dropbox ceo", "dropbox leadership 2021"], latest, NOW) == ["dropbox ceo 2026", "dropbox leadership 2021"]
    fixed = classify("In what year was Dropbox founded?", NOW)
    assert apply_year_terms(["when was dropbox founded"], fixed, NOW) == ["when was dropbox founded"]
    named = classify("What did Apple report in 2024?", NOW)
    assert apply_year_terms(["apple results"], named, NOW) == ["apple results"]


def test_time_scope_round_trips_through_a_dict():
    scope = classify("Key events for Nvidia between 2019 and 2023", NOW)
    d = scope.as_dict()
    assert TimeScope(d["intent"], d["recent"], d["recency"], tuple(d["years"]), d["window_start"], d["window_end"],
                     d["period_sensitive"], tuple(d["reasons"])) == scope


# --- small helpers and the manifest ------------------------------------------------------------------------------

def test_short_dates():
    assert short_date("2024-05-03T10:11:12Z") == "2024-05-03" and short_date("2024-05") == "2024-05"
    assert short_date("3 days ago") == "3 days ago" and short_date(None) is None and short_date("  ") is None


def test_the_manifest_holds_the_thresholds():
    assert manifest()["name"] == "temporal" and setting("latest_max_age_days") == 548
    assert setting("default_recency_for_latest") in ("day", "week", "month", "year")


# --- the runner uses all of it -----------------------------------------------------------------------------------

def run_with(env, question, planner_prompts=None, search_calls=None, select_prompts=None, handlers=None, **kw):
    seen = planner_prompts if planner_prompts is not None else []

    def plan(prompt):
        seen.append(prompt)
        return Plan(subject="Acme", queries=["acme ceo"], checklist=["Current chief executive of Acme"])

    def select(prompt):
        (select_prompts if select_prompts is not None else []).append(prompt)
        from runner import Selection
        return Selection(numbers=[1])

    runner = make_runner(env, Plan=plan, Selection=select, **(handlers or {}))
    runner.now_fn = at(2026, 11, 4, 3, 51)
    original = env["world"].search

    def spy(query, max_results=8, **extra):
        (search_calls if search_calls is not None else []).append((query, extra))
        return original(query, max_results)

    runner.search_fn = spy
    return runner, runner.start(question, budgets={"max_iterations": 1}, **kw)


def test_the_planner_is_told_todays_date_and_the_time_scope(env):
    prompts = []
    runner, result = run_with(env, "Who is the current CEO of Acme?", planner_prompts=prompts)
    assert "TODAY: 2026-11-0" in prompts[0] and "calendar quarter Q4 2026" in prompts[0]
    assert "never assume what the current year is" in prompts[0] and "CURRENT state" in prompts[0]
    plan = next(e for e in env["runs"].events(result["run_id"]) if e["type"] == "plan_ready")["payload"]
    assert plan["time_scope"]["intent"] == "latest" and plan["time_scope"]["recency"] == "year"
    assert plan["today"]["utc"] == "2026-11-04T03:51:00+00:00"
    # today's year is added to every query; the question skill's searches for a role take turns with the planner's
    assert plan["queries"][0].lower() == "acme ceo 2026" and all(q.endswith("2026") for q in plan["queries"])
    assert "Acme leadership team 2026" in plan["queries"] or "Acme names CEO 2026" in plan["queries"]


def test_searches_use_the_recency_window_and_record_their_exact_time(env):
    calls = []
    runner, result = run_with(env, "Who is the current CEO of Acme?", search_calls=calls)
    assert calls and all(extra == {"recency": "year"} for _, extra in calls)
    done = [e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "search_done"]
    assert done and done[0]["recency"] == "year" and done[0]["searched_at"]["utc"] == "2026-11-04T03:51:00+00:00"


def test_questions_about_fixed_facts_search_without_a_recency_window(env):
    calls = []
    runner, result = run_with(env, "In what year was Acme founded?", search_calls=calls)
    assert calls and all(extra == {} for _, extra in calls)          # no recency argument at all
    plan = next(e for e in env["runs"].events(result["run_id"]) if e["type"] == "plan_ready")["payload"]
    assert plan["time_scope"]["intent"] == "historical" and "acme ceo" in plan["queries"]
    assert "Acme founded" in plan["queries"] and not any("2026" in q for q in plan["queries"])     # no year for a fixed fact


def test_the_page_chooser_sees_each_pages_date_and_today(env):
    prompts = []
    original = env["world"].search

    def dated(query, max_results=8):
        out = original(query, max_results)
        for r in out["results"]:
            r["published"] = "2024-05-03T10:11:12"
        return out

    env["world"].search = dated
    run_with(env, "Who is the current CEO of Acme?", select_prompts=prompts)
    assert prompts and "(dated 2024-05-03)" in prompts[0] and "TODAY: 2026-11-0" in prompts[0] and "TIME SCOPE" in prompts[0]


def test_pages_without_a_date_say_so(env):
    prompts = []
    run_with(env, "In what year was Acme founded?", select_prompts=prompts)
    assert prompts and "(no date shown)" in prompts[0]

"""The quick look before planning: two searches, titles and snippets only; context for the plan and advice for the person, never evidence."""

import sources
from runner import Advice, Plan, SourceAdvice
from tests.test_runner import env, make_runner, types  # noqa: F401


def test_the_priority_policy_loads_and_ranks_the_subjects_own_site_first():
    ranks = sources.priority()["ranks"]
    assert [r["rank"] for r in ranks] == sorted({r["rank"] for r in ranks})
    assert "own site" in ranks[0]["kind"]
    advice = sources.priority_advice()
    assert advice.startswith("Prefer sources in this order") and "Never copy a value" in advice


def test_the_scout_searches_twice_and_its_results_reach_the_plan_and_the_decision(env):
    prompts = []

    def planner(p):
        prompts.append(p)
        return Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"])

    advice = Advice(sources=[SourceAdvice(site="acme.example", why="the company's own pricing page")],
                    suggestion="No 2031 page appears, but a 2026 one does. Did you mean 2026?")
    runner = make_runner(env, scout=True, Plan=planner, Advice=lambda p: advice)
    first = runner.start("What does Acme charge in 2031?", require_approval=True)

    assert env["world"].queries[:2] == ["What does Acme charge in 2031?", "What does Acme charge in 2031? official"]
    assert "WEB CONTEXT" in prompts[0] and "Acme pricing (acme.example" in prompts[0]
    assert "never copy a value" in prompts[0]
    ask = first["approval_request"]
    assert ask["advice"]["sources"] == [{"site": "acme.example", "why": "the company's own pricing page"}]
    assert ask["advice"]["suggestion"].endswith("Did you mean 2026?")
    scout = next(e["payload"] for e in env["runs"].events(first["run_id"]) if e["type"] == "scout_done")
    assert scout["results"] == 2 and "acme.example" in scout["sites"]


def test_nothing_from_the_scout_becomes_a_claim(env):
    runner = make_runner(env, scout=True)
    result = runner.start("What does Acme charge?")
    assert result["status"] == "finished"
    assert all(r["source_url"] not in ("https://news.example/acme",) or r["claim"] for r in result["report"]["established"])
    # every claim still rests on a page that was read, with a verified quote
    assert all(row["quote"] for row in result["report"]["established"])


def test_a_failing_scout_never_stops_the_run(env):
    calls = {"n": 0}
    world = env["world"]

    def flaky(query, max_results=8):
        calls["n"] += 1
        if calls["n"] <= 2:                       # both scout searches fail
            raise RuntimeError("search is down")
        return world.search(query, max_results=max_results)

    runner = make_runner(env, scout=True)
    runner.search_fn = flaky
    result = runner.start("What does Acme charge?")
    assert result["status"] == "finished"
    assert any("scout search failed" in w for w in result["report"]["warnings"])
    assert result["report"]["outcome"] == "answered"


def test_the_scout_can_be_switched_off(env):
    result = make_runner(env).start("What does Acme charge?")
    assert "scout_done" not in types(env, result["run_id"])


def hit(title, url, snippet="", published=""):
    from corroboration import site
    return {"title": title, "url": url, "snippet": snippet, "published": published, "site": site(url)}


def test_a_year_the_results_never_mention_gets_a_suggestion_computed_by_code(env):
    runner = make_runner(env)
    state = {"question": "What did the CEO say in the 2031 shareholder letter?", "scout": [
        hit("CEO letter", "https://www.cnbc.com/2026/05/26/ceo-letter.html"),
        hit("Annual letter 2026", "https://blog.example.com/letter", "the 2026 letter"),
        hit("History", "https://old.example.org/a", "in 2019 the company")]}
    text = runner._year_suggestion(state)
    assert text and "names 2031" in text and "mention 2026" in text and text.endswith("Did you mean 2026?")


def test_no_suggestion_when_the_year_appears_or_nothing_dated_is_found(env):
    runner = make_runner(env)
    assert runner._year_suggestion({"question": "the 2026 letter", "scout": [hit("2026 letter", "https://a.example/x")]}) is None
    assert runner._year_suggestion({"question": "the 2031 letter", "scout": [hit("A page", "https://a.example/x")]}) is None
    assert runner._year_suggestion({"question": "from 2020 to 2024", "scout": [hit("2019", "https://a.example/x")]}) is None   # two years named
    assert runner._year_suggestion({"question": "the 2031 letter", "scout": [hit("plan for 2040", "https://a.example/x")]}) is None   # only future years

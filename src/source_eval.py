"""Grading for evals/source_cases.json: a finished research run against what the case expects.

Outcomes, from best to worst:
  correct        the established answer states what the case expects (or, for a trap, nothing was established)
  honest_miss    nothing was established for a question that has an answer: a gap, not a lie
  review         something was established that does not match the expected strings: a person must look (a true answer
                 phrased differently is possible), so it is never counted as wrong automatically
  wrong          a trap question received an established answer, or a time-sensitive answer rests on an undated source
The routing check is separate: the connectors offered in the run must be exactly those the case lists.
"""

import json
import re

from config import PROJECT_ROOT

CASES_PATH = PROJECT_ROOT / "evals" / "source_cases.json"
OUTCOMES = ("correct", "honest_miss", "review", "wrong")


def load_cases(path=CASES_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _forms(text: str) -> list[str]:
    low = re.sub(r"\s+", " ", text.lower())
    return [low, low.replace(",", "")]


def states(text: str, needle: str) -> bool:
    n = re.sub(r"\s+", " ", needle.lower())
    return any(n in f or n.replace(",", "") in f for f in _forms(text))


def answer_text(report: dict) -> str:
    """Only what the run stands behind: the written summary and the established claims (never the partly supported ones)."""
    parts = [report.get("summary") or ""]
    for row in report.get("established", []):
        parts += [row.get("claim", ""), row.get("quote", ""), row.get("value", "")]
    return "\n".join(parts)


def grade(case: dict, report: dict) -> dict:
    expect = case["expect"]
    established = report.get("established", [])
    text = answer_text(report)
    kind = expect["kind"]
    if kind == "not_established":
        outcome, why = (("correct", "nothing was established, as it should be") if not established
                        else ("wrong", f"{len(established)} claim(s) were established for a question that has no answer"))
    elif kind == "time_sensitive":
        undated = [r for r in established if not r.get("source_date")]
        outcome, why = (("honest_miss", "nothing was established") if not established
                        else ("wrong", f"{len(undated)} established claim(s) rest on an undated source") if undated
                        else ("correct", "established, and every source is dated"))
    else:
        groups = [expect["answers"]] if kind == "contains_any" else expect["groups"]
        hit = all(any(states(text, a) for a in g) for g in groups)
        outcome, why = (("correct", "the answer states the expected value") if hit
                        else ("honest_miss", "nothing was established") if not established
                        else ("review", "something was established but not the expected value: check by hand"))
    return {"outcome": outcome, "why": why}


def check_route(case: dict, events: list[dict]) -> dict:
    routed = next((e["payload"] for e in events if e["type"] == "sources_routed"), None)
    offered = sorted(routed["offered"]) if routed else []
    expected = sorted(case["route"])
    return {"offered": offered, "expected": expected, "ok": offered == expected}


def run_facts(events: list[dict], report: dict) -> dict:
    count = lambda t: sum(e["type"] == t for e in events)   # noqa: E731
    return {"established": len(report.get("established", [])), "partial": len(report.get("partial", [])),
            "rejected_quotes": report.get("rejected_quotes", 0), "conflicts": len(report.get("conflicts") or []),
            "api_calls": count("api_called"), "api_failed": count("api_failed"), "api_skipped": count("api_skipped"),
            "stale": bool((report.get("time") or {}).get("stale"))}


def summarize(results: list[dict]) -> dict:
    by_outcome = {o: sum(r["outcome"] == o for r in results) for o in OUTCOMES}
    return {"cases": len(results), **by_outcome, "route_ok": sum(r["route"]["ok"] for r in results)}

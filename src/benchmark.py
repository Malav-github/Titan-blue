"""The search benchmark (definitions/benchmark/questions.json): run each question end to end and score it on evidence.

    uv run python src/benchmark.py                 every case, then the comparison with the previous run
    uv run python src/benchmark.py lyb_portfolio   one case (or several, by id)

Each result is kept in data/benchmark/<time>.json. Scored per case:
  found        expected items whose pattern is in an established claim's quote (the source's own words)
  established  claims established; sites: independent sites they come from; tier1: claims from official documents
  recorded     claims recorded (precision: established / recorded, how much of what was picked was usable)
  seconds      wall time; deadline: whether the deadline cut the run short
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import corroboration  # noqa: E402

QUESTIONS = config.PROJECT_ROOT / "definitions" / "benchmark" / "questions.json"
RESULTS = Path(config.DATA_DIR) / "benchmark"


def cases() -> list[dict]:
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    for c in data["cases"]:
        for e in c["expected"]:
            for p in e["patterns"]:
                re.compile(p)
    return data["cases"]


def score(case: dict, result: dict, events: list[dict], seconds: float) -> dict:
    """The numbers for one case, from the run's result and its events. Plain code: the same run always scores the same."""
    report = result.get("report") or {}
    rows = report.get("established") or []
    quotes = [r.get("quote") or "" for r in rows]
    found = [e["item"] for e in case["expected"] if any(re.search(p, q, re.I) for p in e["patterns"] for q in quotes)]
    recorded = sum(1 for e in events if e["type"] == "claim_recorded")
    return {
        "id": case["id"], "status": result.get("status"), "seconds": round(seconds),
        "found": found, "missing": [e["item"] for e in case["expected"] if e["item"] not in found],
        "recall": round(len(found) / len(case["expected"]), 2) if case["expected"] else None,
        "established": len(rows), "recorded": recorded,
        "precision": round(len(rows) / recorded, 2) if recorded else None,
        "sites": len({corroboration.site(r.get("source_url") or "") for r in rows if r.get("source_url")}),
        "tier1": sum(1 for r in rows if r.get("source_tier") == "primary"),
        "gaps": len(report.get("gaps") or []),
        "deadline": any(e["type"] == "deadline_reached" for e in events),
        "searches": sum(1 for e in events if e["type"] == "search_done"),
        "documents_read": sum(1 for e in events if e["type"] == "source_read"),
        "run_id": result.get("run_id"),
    }


def run_case(runner, case: dict) -> dict:
    start = time.monotonic()
    try:
        result = runner.start(case["question"], subject=case.get("subject"), require_approval=False, ask_sources=False,
                              report_type=case.get("report_type"))
    except Exception as exc:                       # one failed case is a result, not the end of the benchmark
        return {"id": case["id"], "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}", "seconds": round(time.monotonic() - start)}
    return score(case, result, runner.runs.events(result["run_id"]), time.monotonic() - start)


def previous() -> dict | None:
    files = sorted(RESULTS.glob("*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def table(scores: list[dict], before: dict | None = None) -> str:
    old = {s["id"]: s for s in (before or {}).get("scores", [])}
    cols = ("recall", "established", "sites", "tier1", "precision", "seconds")
    lines = [f"{'case':22} " + " ".join(f"{c:>12}" for c in cols) + "  missing"]
    for s in scores:
        if s.get("status") == "error":
            lines.append(f"{s['id']:22} ERROR {s['error']}")
            continue
        cells = []
        for c in cols:
            now, was = s.get(c), old.get(s["id"], {}).get(c)
            cells.append(f"{now}" + (f" ({was})" if was is not None and was != now else ""))
        lines.append(f"{s['id']:22} " + " ".join(f"{x:>12}" for x in cells) + f"  {', '.join(s.get('missing') or []) or '-'}")
    return "\n".join(lines)


def main(ids: list[str]) -> int:
    from runner import Runner
    chosen = [c for c in cases() if not ids or c["id"] in ids]
    if not chosen:
        print(f"no case named {', '.join(ids)}; cases: {', '.join(c['id'] for c in cases())}")
        return 2
    before = previous()
    runner = Runner()
    scores = []
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    for case in chosen:
        print(f"running {case['id']}: {case['question']}", flush=True)
        scores.append(run_case(runner, case))
        print(f"  {json.dumps(scores[-1])}", flush=True)
        # saved after every case: a benchmark cut short keeps what it measured
        out.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "cases": [c["id"] for c in chosen],
                                   "scores": scores}, indent=2), encoding="utf-8")
    print("\n" + table(scores, before) + f"\n\n(in brackets: the previous run's value; saved to {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

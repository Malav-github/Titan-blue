"""Run the source test ladder (evals/source_cases.json) through the real app and grade each run.

The Titan server must be running (titan.bat, or `uv run python serve.py`) with its models and search available.

  uv run python eval_sources.py --list
  uv run python eval_sources.py --source sec_edgar
  uv run python eval_sources.py --case web-3 --case edgar-6
  uv run python eval_sources.py --levels 1,2,6          # a quick pass
"""

import argparse
import json
import sys
import time
from datetime import datetime

sys.path.insert(0, "src")

import httpx  # noqa: E402

import config  # noqa: E402
from source_eval import grade, load_cases, check_route, run_facts, summarize  # noqa: E402

TERMINAL = ("finished", "failed", "stopped", "needs_capability")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--source", action="append", help="only this source (repeatable)")
    ap.add_argument("--case", action="append", help="only this case id (repeatable)")
    ap.add_argument("--levels", help="comma-separated ladder levels, e.g. 1,2,6")
    ap.add_argument("--searches", type=int, default=3)
    ap.add_argument("--fetches", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=1500, help="seconds allowed per case")
    ap.add_argument("--list", action="store_true", help="show the selected cases and stop")
    args = ap.parse_args()

    cases = load_cases()
    if args.source:
        cases = [c for c in cases if c["source"] in args.source]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.levels:
        wanted = {int(x) for x in args.levels.split(",")}
        cases = [c for c in cases if c["level"] in wanted]
    if args.list:
        for c in cases:
            print(f"{c['id']:<11} L{c['level']} {c['expect']['kind']:<15} {c['question']}")
        return 0
    if not cases:
        print("no cases selected")
        return 2

    token = (config.DATA_DIR / "api_token.txt").read_text(encoding="utf-8").strip()
    http = httpx.Client(base_url=args.base, headers={"x-titan-token": token}, timeout=60)
    try:
        http.get("/api/health").raise_for_status()
    except Exception as exc:
        print(f"Cannot reach Titan at {args.base}: {exc}\nStart it first (titan.bat).")
        return 2

    results = []
    for case in cases:
        started = time.time()
        print(f"\n{case['id']} (level {case['level']}): {case['question']}", flush=True)
        try:
            run_id = http.post("/api/templates/research-company/run", json={"inputs": {
                "subject": case["question"][:60], "question": case["question"], "allow_capabilities": False,
                "max_searches": args.searches, "max_fetches": args.fetches}}).raise_for_status().json()["run_id"]
            status, deadline, approved = "queued", started + args.timeout, False
            while time.time() < deadline:
                run = http.get(f"/api/runs/{run_id}").raise_for_status().json()
                status = run["status"]
                if status == "awaiting_approval" and not approved:
                    http.post(f"/api/runs/{run_id}/decision", json={"approved": True}).raise_for_status()
                    approved = True
                elif status in TERMINAL:
                    break
                time.sleep(5)
            events = http.get(f"/api/runs/{run_id}/events").raise_for_status().json()
            report = run.get("report") or {}
        except Exception as exc:
            results.append({"id": case["id"], "source": case["source"], "level": case["level"], "outcome": "wrong",
                            "why": f"the eval itself failed: {type(exc).__name__}: {exc}", "route": {"ok": False, "offered": [], "expected": case["route"]}})
            print("  ERROR", exc)
            continue
        if status != "finished":
            graded = {"outcome": "wrong", "why": f"the run ended as {status}, not finished"}
        else:
            graded = grade(case, report)
        route = check_route(case, events)
        result = {"id": case["id"], "source": case["source"], "level": case["level"], "question": case["question"],
                  **graded, "route": route, "seconds": round(time.time() - started), "run_id": run_id,
                  "summary": (report.get("summary") or "")[:300], **(run_facts(events, report) if report else {})}
        results.append(result)
        print(f"  {graded['outcome'].upper():<12} {graded['why']}  | route {'ok' if route['ok'] else 'MISMATCH ' + str(route)}  | {result['seconds']}s", flush=True)

    totals = summarize(results)
    print("\n" + "=" * 70)
    print(f"{'case':<11} {'outcome':<12} {'route':<6} {'time':>5}  why")
    for r in results:
        print(f"{r['id']:<11} {r['outcome']:<12} {'ok' if r['route']['ok'] else 'WRONG':<6} {r.get('seconds', 0):>4}s  {r['why']}")
    print(f"\n{totals['cases']} cases: {totals['correct']} correct, {totals['honest_miss']} honest misses, "
          f"{totals['review']} to review by hand, {totals['wrong']} wrong; routing right in {totals['route_ok']}")
    out = config.PROJECT_ROOT / "logs" / f"source_eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"totals": totals, "results": results}, indent=2), encoding="utf-8")
    print(f"details: {out}")
    return 1 if totals["wrong"] else 0


if __name__ == "__main__":
    sys.exit(main())

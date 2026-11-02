"""Run, pause, resume and inspect research runs.

  uv run python run_research.py "Which patent families underpin X?"            # run to the end
  uv run python run_research.py "..." --approve                                  # stop for plan approval
  uv run python run_research.py "..." --pause-after discover                     # stop after a step
  uv run python run_research.py --resume RUN_ID [--pause-after fetch]            # continue a run
  uv run python run_research.py --list | --events RUN_ID
"""

import argparse
import json
import sys

sys.path.insert(0, "src")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from runner import Runner  # noqa: E402
from runs import RunStore  # noqa: E402

SHOW = {
    "plan_ready": lambda p: f"subject={p['subject']!r} queries={p['queries']} checklist={len(p['checklist'])} items",
    "primary_pages": lambda p: f"{p['pages']} official pages on {p['domain']}",
    "domain_unconfirmed": lambda p: f"official domain not confirmed: {p['candidates']}",
    "search_done": lambda p: f"{p['query']!r}: {p['results']} results" + (f" ({len(p['warnings'])} warnings)" if p["warnings"] else ""),
    "sources_selected": lambda p: f"{len(p['urls'])} pages: " + ", ".join(u[:60] for u in p["urls"]),
    "source_fetched": lambda p: f"{p['url'][:70]} [{p['tier']}] {p['readable_chars']} chars",
    "source_failed": lambda p: f"{p['url'][:70]}: {p['error']}",
    "claim_recorded": lambda p: f"#{p['claim_id']} {p['claim'][:90]}",
    "claim_rejected": lambda p: f"{p['reason']}: {p['quote'][:60]!r}",
    "support_checked": lambda p: f"claim #{p['claim_id']} -> {p['verdict']}",
    "coverage_assessed": lambda p: f"round {p['iteration']}: covered {p['covered']}, {len(p['gaps'])} gaps",
    "llm_failed": lambda p: f"{p['task']}: {p['error']}",
    "awaiting_approval": lambda p: "plan needs approval",
    "paused": lambda p: f"next: {p['next']}",
    "capability_needed": lambda p: f"this run needs a data source: {p['need']}",
    "api_called": lambda p: f"{p['connector']}.{p['endpoint']} {p['params']} [{p['source_tier']}]",
    "api_failed": lambda p: f"{p['connector']}.{p['endpoint']}: {p['error']}",
    "source_rendered": lambda p: f"{p['url'][:70]} rendered in the browser",
    "capability_test_item": lambda p: f"[{p['status']}] {p['item']}",
    "capability_ready": lambda p: f"connector '{p['connector_id']}' is active",
    "run_failed": lambda p: p["error"],
}


def show(event: dict) -> None:
    fmt = SHOW.get(event["type"])
    detail = fmt(event["payload"]) if fmt else ""
    print(f"  {event['node']:>9} | {event['type']:<18} {detail}")


def print_report(report: dict) -> None:
    print("\n" + "=" * 72 + f"\n{report['question']}\n" + "=" * 72)
    if report["summary"]:
        print(f"\n{report['summary']}\n")
    for title, rows in (("ESTABLISHED", report["established"]), ("PARTIALLY SUPPORTED (treat with caution)", report["partial"])):
        if rows:
            print(f"{title}:")
        for r in rows:
            print(f"  [{r['claim_id']}] {r['claim']}\n       \"{r['quote'][:140]}\"\n       {r['source_url']} ({r['source_tier']})")
    if report["gaps"]:
        print("\nNOT ESTABLISHED:\n  - " + "\n  - ".join(report["gaps"]))
    print(f"\nExcluded: {report['not_supported']} not-supported claims, {report['rejected_quotes']} quotes that failed verification.")
    print(f"Used: {report['budget_used']}")


def finish(result: dict, runner: Runner) -> int:
    print(f"\nRun {result['run_id']}: {result['status']}")
    if result.get("approval_request"):
        plan = result["approval_request"]
        print(f"\nPlan awaiting approval:\n  queries:   {plan['queries']}\n  checklist: {plan['checklist']}")
        answer = input("Approve? [y]es / [n]o: ").strip().lower()
        decision = {"approved": answer in ("", "y", "yes")}
        return finish(runner.resume(result["run_id"], decision), runner)
    if result.get("capability_request"):
        req = result["capability_request"]
        print(f"\nThis run is paused: it needs a data source it does not have.\n  need:   {req['need']}\n  reason: {req['reason']}")
        if input("Run the capability manager now to find and integrate one? [y/n]: ").strip().lower() in ("y", "yes"):
            from manage_capability import run_interactive
            run_interactive(req["need"], req["request_id"], parent=result["run_id"], runner=runner)
            return 0
        print(f"Later: uv run python manage_capability.py --request {req['request_id']}\n"
              f"Or continue without it: --resume {result['run_id']} (then answer skip)")
        return 0
    if result["status"] == "paused":
        print(f"Paused before {result['next']}. Continue with: --resume {result['run_id']}")
    if result["status"] == "failed":
        print(f"Failed: {result.get('error')}. Retry the failed step with: --resume {result['run_id']}")
        return 1
    if result.get("report") and result["status"] == "finished":
        print_report(result["report"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?")
    ap.add_argument("--subject")
    ap.add_argument("--approve", action="store_true", help="pause for plan approval before searching")
    ap.add_argument("--allow-capabilities", action="store_true",
                    help="let the run pause and ask for a new data source (API) when ordinary pages are not enough")
    ap.add_argument("--pause-after", action="append", metavar="NODE")
    ap.add_argument("--resume", metavar="RUN_ID")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--events", metavar="RUN_ID")
    ap.add_argument("--max-fetches", type=int)
    ap.add_argument("--max-searches", type=int)
    args = ap.parse_args()

    runs = RunStore()
    if args.list:
        for r in runs.runs():
            print(f"{r['run_id']}  {r['status']:<18} {r['started_at'][:19]}  {r['question']}")
        return 0
    if args.events:
        for e in runs.events(args.events):
            print(json.dumps({k: e[k] for k in ("event_id", "ts", "node", "type", "payload")}, default=str)[:300])
        return 0

    runs.listener = show
    runner = Runner(runs=runs)
    if args.resume:
        return finish(runner.resume(args.resume, pause_after=args.pause_after), runner)
    if not args.question:
        ap.error("give a question, or --resume / --list / --events")
    budgets = {k: v for k, v in (("max_fetches", args.max_fetches), ("max_searches", args.max_searches)) if v}
    return finish(runner.start(args.question, subject=args.subject, require_approval=args.approve,
                               pause_after=args.pause_after, budgets=budgets,
                               allow_capabilities=args.allow_capabilities), runner)


if __name__ == "__main__":
    sys.exit(main())

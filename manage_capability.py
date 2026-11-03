"""Find, vet and integrate a data-source API a task needs. You stay in the loop at one gate.

  uv run python manage_capability.py --need "patent family records" [--parent RUN_ID]
  uv run python manage_capability.py --need "scholarly works" --api "OpenAlex" --docs https://help.openalex.org/
                                                            # you name the API and its official docs: no guessing
  uv run python manage_capability.py --request REQUEST_ID      # pick up a request a run created
  uv run python manage_capability.py --list
"""

import argparse
import json
import sys

sys.path.insert(0, "src")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from capability import CapabilityManager  # noqa: E402
from connectors.registry import ConnectorRegistry  # noqa: E402


def show_gate(gate: dict) -> None:
    rec = gate["recommendation"]
    print("\n" + "=" * 72 + f"\nNeed: {gate['need']}\n" + "=" * 72)
    for c in gate["candidates"]:
        free = c.get("cost_label", "").upper()
        star = "  <- recommended" if c["connector_id"] == rec["choice"] else ""
        print(f"\n[{c['connector_id']}] {c['name']} ({c['kind']}) {free}{star}\n  docs: {c['docs_url']}")
        if "cannot_integrate" in c:
            print(f"  cannot integrate: {c['cannot_integrate']}")
            continue
        for e in c["verified_facts"]:
            print(f"  verified  {e['field']}: {e['value']}\n            \"{e['quote'][:110]}\" ({e['source_url'][:60]})")
        print(f"  UNVERIFIED (no quote found): {', '.join(c['unverified']) or 'none'}"
              + (f"  | facts the quotes did not actually support: {c['facts_rejected']}" if c.get("facts_rejected") else ""))
        for w in c.get("warnings", []):
            print(f"  !! {w}")
        p = c["integration_plan"]
        print(f"  plan: host {p['host']} | auth {p['auth']} | key in .env as {p['key_env']} | tier {p['tier']}")
        print(f"        endpoints {list(p['endpoints'])} | smoke test {p['smoke_test']['endpoint']}")
        print(f"        budget {p['budget']['max_calls_per_day']}/day, {p['rate_limit_per_minute']}/min | terms: {p['terms']['notes'][:80]}")
    print(f"\nRecommendation: {rec['choice']}\n  {rec['reasoning']}")
    if rec.get("paid_justification"):
        print(f"  Why not free (model's opinion, unverified): {rec['paid_justification']}")
    if rec.get("note"):
        print(f"  Note: {rec['note']}")
    print("\nYour steps:\n" + "\n".join(f"  - {s}" for s in gate["your_steps"]))
    if gate.get("note"):
        print(f"\n!! {gate['note']}")


def print_tests(tests: list[dict]) -> None:
    print("\nTest todo list:")
    for t in tests:
        print(f"  [{t['status'].upper():4}] {t['item']}" + (f" - {t['detail'][:110]}" if t["detail"] else ""))


def drive(manager: CapabilityManager, result: dict) -> dict:
    while result["status"] == "awaiting_user":
        show_gate(result["gate"])
        choice = input("\nConnector id to approve (after adding the key to .env), or 'no' to cancel: ").strip()
        if choice.lower() in ("no", "n", ""):
            result = manager.resume(result["request_id"], {"approve": None})
            break
        terms = input("Have you read the terms and are you happy with them? [y/N]: ").strip().lower() in ("y", "yes")
        result = manager.resume(result["request_id"], {"approve": choice, "terms_ok": terms})
    if result.get("tests"):
        print_tests(result["tests"])
    print(f"\nRequest {result['request_id']}: {result['status']}" + (f" - {result['outcome']}" if result.get("outcome") else ""))
    for name, why in (result.get("problems") or {}).items():
        print(f"  {name}: {why}")
    return result


def run_interactive(need: str, request_id: str | None = None, parent: str | None = None, runner=None,
                    known_api: dict | None = None) -> dict:
    on_ready = None
    if runner is not None and parent:
        import run_research

        def on_ready(run_id, connector_id, req_id):
            print(f"\nResuming run {run_id} with the new connector '{connector_id}'...")
            result = runner.resume(run_id, {"capability": connector_id})
            run_research.finish(result, runner)
            return result

    manager = CapabilityManager(on_ready=on_ready)
    waiting = manager.pending(request_id) if request_id else None   # already researched: go straight to the gate
    return drive(manager, waiting or manager.start(need, parent_run_id=parent, request_id=request_id, known_api=known_api))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--need")
    ap.add_argument("--parent", help="run to resume once the connector is ready")
    ap.add_argument("--request")
    ap.add_argument("--api", help="name of the API, when you already know which one (needs --docs)")
    ap.add_argument("--docs", help="the API's official documentation URL")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    registry = ConnectorRegistry()
    if args.list:
        for r in registry.requests():
            print(f"{r['request_id']}  {r['status']:<15} run={r['run_id']}  {r['need'][:70]}")
        print("\nActive connectors:", [m.connector_id for _, m in registry.list_active()] or "none")
        return 0
    runner = None
    parent = args.parent
    if args.request:
        req = registry.get_request(args.request)
        if req is None:
            ap.error(f"unknown request {args.request}")
        args.need, parent = req["need"], parent or req["run_id"]
    if not args.need:
        ap.error("give --need, --request or --list")
    if parent:
        from runner import Runner
        runner = Runner()
    if bool(args.api) != bool(args.docs):
        ap.error("--api and --docs go together")
    known = {"name": args.api, "docs_url": args.docs} if args.api else None
    result = run_interactive(args.need, args.request, parent, runner, known)
    return 0 if result["status"] in ("done", "ready", "cancelled") else 1


if __name__ == "__main__":
    sys.exit(main())

"""Re-check that each usage rule Titan enforces is still stated, word for word, on the provider's own page.

  uv run python check_policies.py

A rule that has disappeared or been reworded means the provider's policy may have changed: review it,
update definitions/policies/*.json, and only then trust the connector again.
"""

import sys

sys.path.insert(0, "src")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import policy  # noqa: E402
from evidence import normalize, verify_quote  # noqa: E402
from tools import web  # noqa: E402


def check(pol: policy.Policy, fetch=web.fetch_url) -> list[dict]:
    """One result per rule: {id, ok, detail}. Each page is fetched once, through the same policy as any other request."""
    pages: dict[str, str] = {}
    results = []
    for rule in pol.rules:
        url = rule["source_url"]
        try:
            if url not in pages:
                r = fetch(url, max_chars=200_000)
                _, text = web.extract_text(open(r["archive_path"], "rb").read(), r["content_type"], r["encoding"], r["final_url"])
                pages[url] = text
            found = verify_quote(pages[url], rule["quote"])
            results.append({"id": rule["id"], "ok": found["ok"],
                            "detail": "" if found["ok"] else f"quote not found on {url}: {found.get('reason')}"})
        except Exception as exc:
            results.append({"id": rule["id"], "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:150]}"})
    return results


def main() -> int:
    failed = 0
    for pol in policy.POLICIES:
        print(f"\n{pol.name}")
        for r in check(pol):
            print(f"  {'OK  ' if r['ok'] else 'FAIL'} {r['id']}" + (f" - {r['detail']}" if r["detail"] else ""))
            failed += not r["ok"]
    print("\nAll rules still stated by the provider." if not failed else f"\n{failed} rule(s) need review.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

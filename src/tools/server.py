"""MCP server exposing Titan's core tools. Run from src/: python -m tools.server"""

from functools import partial

import anyio
from mcp.server.fastmcp import FastMCP

from tools import browser, primary, sandbox, web
from evidence import EvidenceStore
from tools.search import search

mcp = FastMCP("titan-tools")
store = EvidenceStore()


def _with_source_id(result: dict) -> dict:
    """Every fetch becomes a registered source. `source_id` is what record_claim cites."""
    try:
        result["source_id"] = store.ingest(result)
    except Exception as exc:  # never lose the fetch because registration failed
        result["ingest_error"] = f"{type(exc).__name__}: {exc}"
    return result


# Tools run in worker threads: keeps the server responsive and lets Playwright's sync API work


@mcp.tool()
async def web_search(query: str, max_results: int = 10, recency: str = "", site: str = "") -> dict:
    """Search the web. Returns ranked leads (url, title, snippet), not evidence: call fetch_url on a
    result before relying on it. `recency` is day, week, month or year; `site` limits to one domain.
    Check `warnings`: failed or degraded search engines mean an empty result proves nothing."""
    return await anyio.to_thread.run_sync(
        partial(search, query, max_results, recency or None, site or None)
    )


@mcp.tool()
async def resolve_company_domain(company: str) -> dict:
    """Find a company's official website domain. Returns candidates with the evidence for each and a
    status: `confirmed`, `ambiguous` or `needs_review`. Never treat a non-confirmed domain as the company's own."""
    def run() -> dict:
        result = primary.resolve_domain(company)
        store.register_resolution(result)  # so claims from a confirmed domain are tiered primary
        return result
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def discover_company_pages(domain: str, max_pages: int = 40) -> dict:
    """List a company's own pages (pricing, about, blog, changelog, careers, press, docs...) from its
    sitemaps, homepage and feeds, honoring robots.txt. Pass the confirmed domain. Then call
    fetch_url on the pages you need. Check `categories_not_found` for gaps."""
    return await anyio.to_thread.run_sync(partial(primary.discover_pages, domain, max_pages))


@mcp.tool()
async def fetch_url(url: str, max_chars: int = 12000) -> dict:
    """Fetch a web page or API over plain HTTP. Fast. Returns readable text, the SHA-256 of the
    raw response and where it was archived. Try this first; use render_url if the text is empty
    or the page needs JavaScript."""
    return _with_source_id(await anyio.to_thread.run_sync(partial(web.fetch_url, url, max_chars=max_chars)))


@mcp.tool()
async def render_url(url: str, max_chars: int = 12000) -> dict:
    """Load a page in a headless browser so JavaScript runs. Slower than fetch_url. Archives the
    rendered HTML and a full-page screenshot, returning both hashes."""
    return _with_source_id(await anyio.to_thread.run_sync(partial(browser.render_url, url, max_chars=max_chars)))


@mcp.tool()
async def record_claim(subject: str, predicate: str, value: str, claim: str, source_id: str, quote: str,
                       as_of: str = "", subject_domain: str = "") -> dict:
    """Record one factual claim about `subject` (a company). It is stored ONLY if `quote` appears word
    for word in the page identified by `source_id` (returned by fetch_url / render_url). Copy the quote
    exactly from the page text, 20+ characters, without paraphrasing or fixing typos. `value` is the
    specific fact (e.g. "$29"), `predicate` names it (e.g. "pricing.starter_monthly_usd"), `claim` is
    one sentence. If rejected, read `reason` and `closest_passage` and retry with an exact quote."""
    def run() -> dict:
        entity = store.find_entity(subject)
        if subject_domain or entity is None:
            entity_id = store.add_entity(subject, domain=subject_domain)
        else:
            entity_id = entity["entity_id"]
        return store.add_claim(entity_id, predicate, value, claim, source_id, quote, as_of or None)
    return await anyio.to_thread.run_sync(run)


@mcp.tool()
async def list_claims(subject: str) -> list[dict]:
    """List the verified, still-current claims recorded about `subject`, with their quotes and source URLs."""
    rows = await anyio.to_thread.run_sync(partial(store.claims_for, subject))
    keep = ("claim_id", "predicate", "value", "claim_text", "quote", "source_url", "source_tier", "as_of")
    return [{k: r[k] for k in keep} for r in rows]


@mcp.tool()
async def run_python(code: str, stdin: str = "", timeout: int = 30) -> dict:
    """Run Python 3.13 code in an isolated sandbox with no network and no access to project files.
    Print results to stdout. Only the standard library is available.
    The sandbox cannot see other tools' results: to process text (e.g. from fetch_url), pass it
    in `stdin` and read it with sys.stdin.read()."""
    return await anyio.to_thread.run_sync(partial(sandbox.run_python, code, timeout, stdin))


@mcp.tool()
async def run_shell(script: str, timeout: int = 30) -> dict:
    """Run a POSIX shell script in the same isolated sandbox (no network, no project files)."""
    return await anyio.to_thread.run_sync(partial(sandbox.run_shell, script, timeout))


if __name__ == "__main__":
    mcp.run()

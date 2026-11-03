# Usage rules for outside sites and APIs

Titan talks to other people's servers. Each provider has rules (request limits, who you must say you are, what is
off limits). Titan follows them in code, so no tool, run or model can forget one.

## How it works

1. A provider's rules live in `definitions/policies/<provider>.json`: the numbers Titan enforces, and for every rule
   the provider's own words (a verbatim quote and the page it came from).
2. `src/policy.py` is the one place that enforces them. The page fetcher, the connector caller, the browser and the
   site-discovery crawler all call it before each request and after each response.
3. A manifest for a covered host is checked twice: when the capability manager drafts it (so a non-compliant draft
   never reaches the approval gate) and again when it is stored.
4. `uv run python check_policies.py` fetches each provider page and confirms every quote is still there word for
   word. A missing quote means the provider may have changed its rules: review, update the JSON, then trust it again.

## SEC EDGAR (reviewed 2026-11-03)

Covers `sec.gov` and every subdomain (`www.sec.gov`, `data.sec.gov`, ...). Titan's numbers are deliberately tighter
than the SEC's: 8 requests per second (SEC: 10) and a 15 minute pause (SEC: 10).

| Rule (SEC's words, abridged) | What Titan does |
|---|---|
| "no more than 10 requests per second, regardless of the number of machines" | One shared budget of 8 a second for all tools and all processes on this machine; extra requests wait, and give up after 10 seconds |
| "Once the rate of requests has dropped below the threshold for 10 minutes, the user may resume" | A 403 or 429 from any sec.gov host pauses all sec.gov requests for 15 minutes |
| "Please declare your user agent in request headers" | Requests are refused unless the User-Agent carries a contact email (from `TITAN_CONTACT`) |
| "Accept-Encoding: gzip, deflate" (sample headers) | Sent on every sec.gov request |
| "The SEC does not allow botnets or automated tools to crawl the site" | Site discovery and browser rendering are refused for sec.gov. Only named documents and the data.sec.gov APIs are fetched |
| "These APIs do not require any authentication or API keys" | A sec.gov manifest must use auth type none, and may not ask for more than 480 requests a minute |
| "Download only what you need ..." and "use the bulk archive ZIP files" | Guideline only: call budgets in the manifest limit volume, but nothing decides what is "needed" |

## Limits you should know about

- The shared budget works across processes on this computer. The SEC counts per user "regardless of the number of
  machines", so running Titan on a second computer would not share it. The margin (8 of 10) is the protection.
- The browser check on a redirect runs after the page has loaded, so a redirect onto sec.gov is caught, not prevented.
- The contact email sent to the SEC is whatever `TITAN_CONTACT` holds. It identifies you to the SEC in every request.
- `check_policies.py` is run by hand. It is not scheduled yet.
- Only SEC EDGAR has a policy so far. OpenAlex has call caps in its manifest but its "$1 a day" budget is not enforced
  in dollars. EPO and Google BigQuery need their own policy files, written from their official pages, before connecting.

## Adding a provider

Read the provider's official terms and fair-use pages. Write `definitions/policies/<id>.json` with the hosts, the
numbers to enforce, and one rule per requirement with its exact quote and source URL. Run `check_policies.py` to
prove each quote is on the page, then add tests for anything the provider forbids.

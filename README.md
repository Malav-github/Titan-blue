# Titan

A multi-agent competitive-intelligence platform built on open-source parts. Information integrity comes first: every claim should trace to archived evidence, contradictions are tracked, and history is append-only.

Titan is being built as a general capability platform (tools driven by skills and instructions). Competitive intelligence is the first domain.

## Status

Built and tested:

- **Model layer** ([src/llm.py](src/llm.py)): local models through Ollama (`qwen3:8b` default, `mistral:7b`, `llama3.1:8b`). Switch with `uv run python switch_model.py`.
- **Standard and Power modes**: Standard uses open-source models only. Power swaps in Anthropic for the tasks listed in `POWER_MODE_TASKS`.
- **Search** ([src/tools/search/](src/tools/search)): `web_search` fans a query out to the configured providers in parallel, merges them by reciprocal rank fusion, and logs every search (including failed engines) to an append-only ledger. SearXNG is the open-source default; Brave Search API is an opt-in paid provider.
- **Primary sources** ([src/tools/primary.py](src/tools/primary.py)): `resolve_company_domain` (Wikidata candidates with evidence; `confirmed`, `ambiguous` or `needs_review`) and `discover_company_pages` (sitemaps, homepage, feeds; honors robots.txt; archives discovery files).
- **Evidence store** ([src/evidence.py](src/evidence.py)): sources, entities and claims in append-only SQLite (triggers block UPDATE and DELETE). A claim is stored only if its quote appears word for word in the archived page text; code checks this, not the model. Rejected attempts are logged. Corrections are new rows (retraction, supersession, contradiction). `audit()` re-verifies every stored claim and file hash. MCP tools: `record_claim`, `list_claims`; every `fetch_url` / `render_url` result carries a `source_id`.
- **Research runner** ([src/runner.py](src/runner.py)): a LangGraph pipeline (plan, approve, discover, select, fetch, extract, support-check, relevance-check, assess, report). The model judges at set steps; code does the searching, archiving, quote verification, budgets and coverage. State is checkpointed to SQLite so a run can pause and resume (even from a new process), and every step is appended to an event log ([src/runs.py](src/runs.py)) for the frontend. Langfuse traces carry the run id.
- **Connectors and the capability manager** ([src/connectors/](src/connectors), [src/capability.py](src/capability.py)): APIs enter Titan only as validated, versioned manifests (HTTPS, one host, read-only, the key's variable name derived by code). When a run is blocked it pauses and files a capability request. The manager searches for candidate APIs, extracts their requirements (free tier, auth, rate limits, terms), and reports only what a verbatim quote from a fetched page backs, listing the rest as unverified. It ranks free options first, drafts a manifest (from the OpenAPI spec when there is one), and stops at one approval gate where you add the key to `.env`, confirm the terms and approve. It then runs a todo list of tests, activates the connector only if they pass, and resumes the stalled run. `call_api` is the one guarded path (rate limits, budgets, no redirects, key redaction, every response archived). MCP servers are not integrated yet.
- **Model eval** ([src/model_eval.py](src/model_eval.py), [evals/](evals)): compares local models on Titan's own tasks (quoted-fact extraction, claim support, relevance, manifest drafting) against known answers, scoring false "supported" verdicts first. Fixtures are frozen real pages.
- **Tools over MCP** ([src/tools/](src/tools)): `web_search`, `fetch_url`, `render_url` (headless browser), `list_connectors` / `call_api`, `record_claim` / `list_claims`, `run_python` and `run_shell` (sandbox). Every fetch is archived under its SHA-256 hash. An SSRF guard blocks localhost, LAN and metadata addresses.
- **Sandbox**: throwaway Docker container in WSL2 with no network, a read-only filesystem, capped resources, and a hard timeout.

- **Block registry and API** ([definitions/](definitions), [src/api/](src/api), [docs/block-spec.md](docs/block-spec.md)): every capability is a JSON block definition (typed ports, settings schema, needs and costs). `uv run python serve.py` starts a localhost-only API (session token in `data/api_token.txt`, header `X-Titan-Token`; foreign Host headers refused) with block listing and availability, models, health, single-block runs, run events (JSON and server-sent events), approval decisions and file uploads. One engine runs a single block and a graph of blocks; its history is the append-only run log, so a run waiting for approval survives a restart. Claims and sources can only be references to the evidence store. Uploaded files become sources with tier `user_provided`. `research.run` and `capability.request` are not wired to the engine yet (use the CLIs).

- **Usage rules for outside sites** ([definitions/policies/](definitions/policies), [src/policy.py](src/policy.py), [docs/api-usage-rules.md](docs/api-usage-rules.md)): a provider's own rules (request limits, declared contact, no crawling, pauses after a refusal) are stored as data with their verbatim quotes and enforced in every request path, including a request budget shared across processes. `uv run python check_policies.py` re-checks that each quote is still on the provider's page. SEC EDGAR is the first policy.

- **Web app** ([frontend/](frontend)): React, TypeScript, Tailwind. Home (ready-made apps drawn from [definitions/templates/](definitions/templates)), Runs (live step-by-step history, plan approval, the result with each claim's quote and source), Data sources (the approval gate, with the provider's terms linked and facts from third-party sites labelled), Evidence (every claim with its quote in its page), Health. Build once with `cd frontend && npm install && npm run build`, then `uv run python serve.py` and open http://127.0.0.1:8000/. For development run `npm run dev` in `frontend/` (it proxies to the API and adds the session token itself). Browser tests: `uv run pytest tests/test_e2e.py`.

- **Temporal skill, phase 1** ([src/skills/temporal/](src/skills/temporal), [definitions/skills/temporal.json](definitions/skills/temporal.json)): planning is told today's date and time zone (`TITAN_TIMEZONE`, default this computer's), a deterministic classifier works out the time scope of each question (fixed past fact, named date, latest, reporting period, timeline), queries get today's year and a recency window when the question is about the current state, every search records its exact time, and the page chooser sees each page's date. Later phases (date collection, reporting periods, timelines) follow the plan in the project notes.

Not built yet: Lab (run any single block), Models and Chat screens, more templates, the block canvas, templates, chat endpoint, evidence and connector endpoints, skills loader, search reranker, A2A, MCP-server integration.

## Requirements

- Windows with WSL2 (Ubuntu) and Docker Engine installed inside it
- [Ollama](https://ollama.com) with the models pulled
- [uv](https://docs.astral.sh/uv/)
- A GPU with about 8 GB of VRAM for the default model

## Setup

```powershell
uv sync                                  # Python 3.13 and dependencies
uv run playwright install chromium       # browser for render_url
copy .env.example .env                   # then edit if needed
uv run python setup_local_llm.py         # smoke-test the local model
uv run python infra/searxng.py           # start private SearXNG (Docker in WSL, localhost:8080)
```

To add Brave: put `BRAVE_API_KEY` in `.env` and set `SEARCH_PROVIDERS=searxng,brave`.

Power mode needs `uv sync --extra power` and `ANTHROPIC_API_KEY` in `.env`. Standard mode needs no keys.

## Run

```powershell
uv run python src/app.py                 # status check
uv run pytest                            # full suite (needs network, Docker and Ollama)
uv run pytest -m "not network and not docker"   # offline subset
```

## Research runs

```powershell
uv run python run_research.py "Which patent families belong to X?"      # run to the end
uv run python run_research.py "..." --approve                           # stop for plan approval first
uv run python run_research.py "..." --pause-after fetch                 # stop after a step
uv run python run_research.py --resume RUN_ID                           # continue (also retries a failed step)
uv run python run_research.py --list                                    # past runs
uv run python run_research.py --events RUN_ID                           # full event log
```

## Comparing local models

```powershell
uv run python eval_models.py                                   # default candidates (must be pulled in Ollama)
uv run python eval_models.py --models qwen3:8b gemma4:e4b --tasks support relevance
uv run python eval_models.py --show data/evals/a.json data/evals/b.json   # merge saved reports
```

The test set is small (13 support, 8 relevance, 4 extraction, 2 manifest cases), so a one-case difference is not a finding.

## Adding a data source

```powershell
uv run python run_research.py "..." --allow-capabilities    # a blocked run pauses and offers the manager
uv run python manage_capability.py --need "patent family records"   # or start it directly
uv run python manage_capability.py --request REQUEST_ID     # pick up a request waiting at the gate
uv run python manage_capability.py --list                   # requests and active connectors
```

The key goes in `.env` as `<CONNECTOR_ID>_API_KEY` (the gate tells you the exact name). Payment, sign-up and accepting terms are always yours.

## Layout

```text
src/config.py       settings from .env
src/llm.py          get_chat_model(task): Ollama or Anthropic
src/tools/          fetch, browser, sandbox, archive, MCP server and client
src/tools/search/   providers, rank fusion, append-only search ledger
infra/              SearXNG config and launcher
src/evidence.py     append-only evidence store with quote verification
src/support.py      claim support check (does the quote back the claim?)
src/runner.py       LangGraph research pipeline; src/runs.py is its event log
src/tracing.py      Langfuse tracing (optional)
src/connectors/     manifests, append-only registry, guarded call_api, OpenAPI converter
src/capability.py   capability manager (research, gate, tests, resume)
tests/              tool and sandbox tests
data/               archive and databases (git-ignored, back up separately)
```

`.env` holds local settings and is never committed. `.env.example` is the template.

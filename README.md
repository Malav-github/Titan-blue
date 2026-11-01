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
- **Tools over MCP** ([src/tools/](src/tools)): `web_search`, `fetch_url`, `render_url` (headless browser), `run_python` and `run_shell` (sandbox). Every fetch is archived under its SHA-256 hash. An SSRF guard blocks localhost, LAN and metadata addresses.
- **Sandbox**: throwaway Docker container in WSL2 with no network, a read-only filesystem, capped resources, and a hard timeout.

Not built yet: skills loader, search reranker and more direct-source connectors (EDGAR, RSS), LangGraph runner with pause/resume, a check that each claim is actually supported by its quote, tracing, evals, dashboard, A2A.

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

## Layout

```text
src/config.py       settings from .env
src/llm.py          get_chat_model(task): Ollama or Anthropic
src/tools/          fetch, browser, sandbox, archive, MCP server and client
src/tools/search/   providers, rank fusion, append-only search ledger
infra/              SearXNG config and launcher
src/evidence.py     append-only evidence store with quote verification
tests/              tool and sandbox tests
data/               archive and databases (git-ignored, back up separately)
```

`.env` holds local settings and is never committed. `.env.example` is the template.

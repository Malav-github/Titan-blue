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

- **Temporal skill, phase 1** ([src/skills/temporal/](src/skills/temporal), [definitions/skills/temporal.json](definitions/skills/temporal.json)): planning is told today's date and time zone (`TITAN_TIMEZONE`, default this computer's), a deterministic classifier works out the time scope of each question (fixed past fact, named date, latest, reporting period, timeline), queries get today's year and a recency window when the question is about the current state, every search records its exact time, and the page chooser sees each page's date. Phase 2 collects every date a page carries (with where each came from) and flags stale answers. **Phase 3** ([periods.py](src/skills/temporal/periods.py), [corroboration.py](src/corroboration.py)) reads each claim's own time frame by code from its quote: the period (fiscal or calendar year, quarter, half, "three months ended", year over year change) and event dates (announced, effective, filed, closed, deal start and end). A boundary the source does not state is left empty, and a statement about the future ("will become") is marked as planned, not done. SEC facts give their exact period from the fact itself. A claim about one period cannot be "supported" by a quote about another, and the report labels each fact (primary source, independent sources, single source unconfirmed, sources disagree) without hiding anything. Timelines come next.
- **Flowchart** ([src/harness.py](src/harness.py), [definitions/harness/research.json](definitions/harness/research.json), [frontend/src/Flow.tsx](frontend/src/Flow.tsx)): the research pipeline drawn as a flowchart, on every run page (steps light up as the run moves; click one to see what it does and its events) and on the "How it works" page. Steps and arrows are read from the real LangGraph graph, and a test fails if the description file ever drifts from it.
- **Source profiles** ([definitions/sources/](definitions/sources), [src/sources.py](src/sources.py)): what each source covers, how to query it, what it is weak at, its time behaviour, evidence tier and limits, kept as data in the project. The planner is given the web advice, and a deterministic router offers an approved connector only when the question points to it (the run log shows `Chose data sources`). [evals/source_cases.json](evals/source_cases.json) holds a seven-step ladder of questions per source (direct, variant, disambiguation, period, several facts, a trap that must not be answered, and a wrong-source case); the routing of every case is a test.

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

**Verify first, act later.** A separate model call lists what the question refers to as if it existed (a named letter, paper, event, role).
That premise is searched for and checked like every other claim (word-for-word quote, support check) *before you are asked to approve
anything*. Nothing is decided from the date or from the model's memory. You then see the plan and the finding together and can approve,
accept a refusal ("no evidence this happened"), overrule it and research anyway, hand over a source (a web address; it is read and checked
like everything else and labelled as yours), or correct the agent in words ("that should have been 2026"): the plan is rebuilt and the
premise checked again. If the answer itself cannot be found, the run pauses and asks for a source. The report says plainly when there
was not enough information (`outcome`: answered, partial, not_found, premise_unverified).

**Quick look before planning.** Two web searches (titles and snippets only) show which entity, sites and period exist. The plan reads them
together with the source-priority policy (`definitions/guidance/source_priority.json`: the subject's own site, then regulators and
official records, then scholarly bodies, then established news, then everything else) and the decision screen shows the ranked places
to look first. If the question names a year that no result mentions while another year appears, code (not the model) offers
"Did you mean 2026?" for you to accept or ignore. What the quick look shows is context, never evidence: no claim is taken from it.

**Ontologies.** `definitions/ontology/` holds two files read by code, validated when loaded. `sources.json` describes every source by
its issuer (whose document it is), document type (press release, earnings release, Form 10-K ...), venue (investor relations site,
newsroom, EDGAR, wire service, publication) and publisher, and derives the tier from them: primary when it is the subject's own
document, wherever it is hosted. Apple's earnings release on investor.apple.com and the same release filed on EDGAR as Exhibit 99.1
share issuer and type and differ in venue. One description per source is used by every step, so the tier that decides when to stop
reading is the tier the report shows. `company_events.json` lists the kinds of company event (acquisitions, divestitures, leadership,
results, guidance ...) with their required parts, cues, a worked example (made-up companies only) and rules such as "completion is not
before announcement"; it is not yet used in extraction (next step: offer the model only the few predicates that fit the question).

## Skills that cover for the local model

Some steps are done by code because a small model cannot do them reliably. Each run's report lists what failed and why
(*Why parts of this run failed*: the local model's capability, a source, a search provider, Titan's own rules, or the question),
with what Titan already does about it and what could be done next (`definitions/guidance/failure_causes.json`).

- **EDGAR skill** (`definitions/skills/edgar.json`, `src/skills/edgar/`). A local model does not know CIKs: it invented 320196
  for Apple (320193) and 0001564590 for S&P Global (64040), so every EDGAR call it planned was refused. Now code looks the CIK up
  in the SEC's own `company_tickers.json` (archived and cited), reads the filing list, chooses filings by form and 8-K item for
  the kind of question (deals: 1.01, 2.01; announcements: 7.01, 8.01; results: 2.02 and the 10-Q/10-K; leadership: 5.02), and
  reads the documents, the 8-K's Exhibit 99 press release first (found by its exhibit type in the filing index, not its file
  name). A model never plans an EDGAR call.
- **List questions** (`definitions/guidance/enumeration.json`). "What did S&P Global announce in 2026?" asks for every event. A
  small model plans it as one fact and stops at the first release; code now recognises the question, writes one checklist item
  for every distinct event, searches the company's releases on wire services, raises the budgets, never stops early, checks two
  claims per document, and writes a dated round-up, newest first.
- **The sources' own words, and where they are** (`src/sentences.py`). Nothing in a claim or an answer is paraphrased:
  - *Extraction by sentence choice.* The model is shown each passage as numbered sentences and answers with numbers; code
    copies those sentences, so a claim **is** the source's sentence (one, or up to three adjacent ones), word for word. A model
    asked to retype quotes misquoted 26-33 times a run and wrote "revenue ... is not directly provided in the passage" as a
    claim; it now has nothing to retype. A fact spread over a page (patent family members in separate table rows) gives one
    claim per run of adjacent sentences. The `value` it names is kept only if the quote writes it exactly. A verbatim claim
    is "supported" by code (no model call). On the extraction eval qwen3:8b chose valid sentences every time in two runs
    (quote_verbatim_rate 1.0) and found 8 of 9 facts.
  - *The answer is quotes.* Code builds it: under each fact asked for, the quotes that establish it (primary sources first,
    then the newest, three at most), each with its source, date, page and exact address, then context quotes and what was
    not established. No model writes it; list round-ups are quoted the same way, dated, newest first.
  - *The page of a PDF.* PDF text keeps its page breaks, so each claim from a PDF gives the page it is on ("page 18 of 120")
    and, when the PDF prints its own numbers, the printed page ("printed page 17"). Shown in the answer, the report and the
    claim page; PDFs read before this change have no page and none is guessed.
  - *The exact address.* Every claim gives the address of the report itself. A site's homepage (`/`, `/en-us/`,
    `index.html`) is never fetched from search results and never quoted; an address that redirected to the homepage (a
    moved or paywalled report) is reported as not reaching the report (`source_not_citable`).
- **Source tiers, and answering at once** (`definitions/guidance/answer_settling.json`, `src/settling.py`; the user's tiers).
  **Tier 1, the official note or document**: the company's own website (a confirmed domain), its filings, its releases
  (also on PR Newswire and other wires); in an acquisition, merger or investment, the other involved company's own release
  or filing; and regulatory filings, whoever filed them (sec.gov; SEBI, MCA and RBI in India; the SEC and BSP in the
  Philippines). **Tier 2, a top-tier publisher's report** (Bloomberg, WSJ, Reuters, Financial Times, AP): an exclusive or a
  report of a possible announcement, labelled as not the official document. **Tier 3**: everything else. A Tier 1 or Tier 2
  claim settles its fact on its own and the run answers without reading or checking further (Tier 1 is read and checked
  first); a Tier 3 fact needs two independent sites. Each claim's label names its tier ("Tier 1: official document, a
  regulatory filing"). The code checks (word-for-word quote, exact address and page, period, figures against the
  statements) always run; corroboration beyond this is a separate verification step.
- **Pause, resume, stop** (run page buttons; `POST /api/runs/{id}/pause`, `/resume`, `/stop`). A pause takes effect at the
  next safe point: between steps, or while reading pages and checking claims, between one model call and the next (on a
  real S&P Global run it paused 13.8 s after the press, after the call in progress). Everything done is checkpointed; Resume
  carries on from the page and chunk it reached, even after a restart. Stop skips what is left and writes the report from
  what was gathered, marked as stopped early; it also ends a run that is paused or waiting for you.
- **Speed.** On this machine's 8 GB GPU Ollama serves one call at a time (three calls at once took 29.7 s, one by one 29.0 s),
  so model calls stay in order and the time is saved elsewhere: statements (tables, OCR) are read on the processor in the
  background while the model works; a chunk sharing no word with the question is not sent to the model; a verbatim claim's
  support check is code, not a model call (and the second model is no longer swapped in); models stay loaded 30 minutes
  (`OLLAMA_KEEP_ALIVE`; a reload costs about 21 s). Searches and page downloads already ran side by side.
- **A table row is read by its statement.** A claim that is a row of a statement read from the same document ("| Revenue |
  $ | 3,678 | | | $ | 3,443 | ...") is identified by code as that row, with each column's period; when it is the line item
  and period the fact asks for, code settles relevance (qwen3:8b cannot see a row's column headings and rejected the 10-Q's
  revenue row), and the answer says "read from the income statement: 'Revenue' for the 3 months ended 2026-09-30 is 3,678".
  Statements are linked to the subject only when the document is its own (an SEC filing by its CIK, otherwise naming it).
- **Planner guards**: a generic acronym ('AI') is never taken as the subject when the question names an organisation; guessed
  figures are removed from queries; premises that name nothing specific are dropped before the premise check; an SEC registrant
  the question names is treated as a company (so its official domain is found and its own pages are labelled primary).
- **Copies are one source**: pages that share several long sentences word for word (a release on the newsroom and on PR
  Newswire) count once for corroboration and are shown as copies.
- **Financial statements** (`definitions/skills/statements.json`, `definitions/ontology/financial_statements.json`,
  `src/skills/statements/`). A small model cannot read a wide table: given S&P Global's 10-Q it quoted the revenue row and
  wrote "revenue is not provided in the passage". Code reads the tables instead, and no model touches a number:
  - *Inline XBRL* (10-Q, 10-K, and any market that files in XBRL): each value's concept, period, unit, scale and sign are read
    from its tag, exactly. The page decides the sign shown (capital expenditure is tagged +93 and printed "(93)"); the fact
    keeps its own value. In a tagged table the tag says what a row is: "Cash provided by operating activities" under
    discontinued operations is not the company's operating cash flow.
  - *HTML tables* (8-K earnings releases): cells split around a number ("$" | "3,678" | ")") are joined, colspans are spread,
    and "Three Months 2026" under a caption "Three and nine months ended September 30" is dated from the caption.
  - *PDF reports* with a text layer (Indian results, Philippine 17-Q and 17-A): columns are found where numbers' right edges
    line up, headers are placed over the columns they span, notes columns and wrapped labels are recognised, a statement that
    runs onto the next page is joined, and an unlabelled total is named after its section only when code has checked that it
    is the sum of the section's lines.
  - *Scanned PDFs*: each page with no text layer is read by **two OCR engines in parallel**, Tesseract and RapidOCR
    (PaddleOCR's models through ONNX; both Apache 2.0, both local). RapidOCR gives the layout (it reads shaded header bands
    that defeat Tesseract); every number must be read the same by both, character for character. The words then go through
    the same table reader and checks as any PDF. Tesseract is a Windows program: `winget install UB-Mannheim.TesseractOCR`
    (or set `TITAN_TESSERACT`); without it scanned pages are listed as unread, never silently skipped.
  - Every figure gets a verdict. **verified**: inside a sum that adds up and, if scanned, read the same by both engines;
    **agreed**: scanned, read the same by both, confidently, but no sum covers it; **read**: text layer or XBRL, no sum
    covers it; **doubt**: the engines disagree, only one read it, one was unsure, its grouping or decimals are wrong for its
    column, it sits in a sum that does not add up, or its column's period could not be read. One unconfirmed figure in a sum
    that adds up, with every other figure confirmed, is settled by the arithmetic (and says so); two are not.
  - A doubtful figure is never dropped. A scanned one is taken as its likeliest reading, and says which and why: first a
    reading with which its sums add up, then one that fits its column (either engine's, or one repaired by the column's
    format: "7.316.94" in a column of two-decimal figures is 7,316.94), then RapidOCR's as it stands. A figure read
    otherwise than RapidOCR printed it stays doubt however well it adds up. It keeps what was printed, both readings and
    its place on the page, is left out of the index unless asked for, and is reported in the run with "please
    double-check" (`statements_doubt`, and in *Why parts of this run failed*). On a real scanned Indian results page (VTM
    Limited, Q2 FY26) the pair caught Tesseract reading 1,071.38 as 4,071.38 and 152.84 as 162.84; RapidOCR read 7,316.94
    as "7.316.94", which is taken as 7,316.94 (Tesseract's reading, and with it the total adds up) and flagged.
  - Numbers as markets print them: 12,34,567, 1.234.567,89, brackets and dashes, lakh and crore, Arabic-Indic digits; a
    lone "1.234" is left unread unless the market's decimal mark settles it.
  - Periods are read, never invented: "Sept'25" stays at month precision; "Q2 FY26" is dated only where the law fixes the
    financial year (India: April to March, marked derived); "04/05/2026" is left unread.
  - Every statement is checked by arithmetic (assets = liabilities + equity, subtotals, profit before tax - tax, the cash
    roll-forward), with the ways companies differ written into the rules (discontinued operations, redeemable non-controlling
    interests, the exchange-rate line inside or after the net change). A misread cell fails loudly; a rule that cannot run is
    not reported as passed.
  - Stored append-only in the evidence store (`statement_tables`, `statement_values`, `statement_checks`) with each value's
    cell, period and source. `GET /api/evidence/sources/{sha}/statements` shows a document's statements;
    `GET /api/evidence/statements?concept=revenue&period_end=2026-09-30` is the index across documents.
  - **Figures in sentences checked against the statements** (`skills/statements/figures.py`, code only). The statements
    read for a run's subject index what the company reported; every figure a claim's quote states ("revenue rose 5.2% to
    ₹4,767 crore in Q2 FY26") is read for its line item, period, scale, currency and written precision, and compared:
    **matches** (equal at the precision written: $3.68 billion = 3,678 million), **differs** (same line item and period,
    another figure, shown beside it), **matches a figure in doubt** (a scanned figure: double-check), **not covered**,
    **found** (no period written: the statement figures it equals), or **not compared** (adjusted, segment, EBITDA, a
    forecast, another currency). A percentage change is compared with the change the printed figures give, allowing for
    their rounding. A fiscal quarter is placed only from the company's own financial year as its statements show it, and
    how is said; "a year earlier" and "against ₹368 crore in Q2 FY25" give a figure its own period. Each claim in the report
    carries its figure checks and a label; a figure that differs puts a warning in the report and a cause in *Why parts of
    this run failed*. Recorded append-only (`claim_figures`; sources are linked to their company in `source_entities`).
    `POST /api/evidence/figures/check` checks any text against a company's statements. The prose words are in the
    ontology's `mentions`.
  - Where documents come from: SEC EDGAR for the US; for India and the Philippines, the companies' own investor pages. The
    exchange portals are not used: NSE's terms forbid automated collection, BSE's terms could not be read, and PSE EDGE allows
    personal, non-commercial use only. Their filings (NSE's are inline XBRL) would need the exchanges' written permission.

## Evidence stores and archives

Nothing Titan records is deleted. To start clean (after changing how sources are labelled, say), open **Stores and archives** in
the app, name the archive, tick the box and choose *Archive and start a new store*. Titan copies the evidence, the run logs, the
saved run states (which hold each run's report), the search ledger, the archived pages and uploads into
`data/archives/<date>-<time>-<name>/`, checks the copy (row counts, database integrity, and an audit that re-hashes every page
and re-verifies every quote), and only then empties the live store. If a check fails the archive is undone and the live store is
left as it was. Connectors, approvals, budgets and settings are not part of a store and stay. It refuses while a run is working;
a run still marked as working but silent for 30 minutes (cut off by a restart) is closed in its own log as abandoned first.

Archived stores are read-only on disk and open read-only. Each is browsed in the app at `/archives/<name>`: its runs with their
reports, its claims with the quote in context, and *Check it again now* to re-audit it. From a terminal:

```powershell
cd src
uv run python -m stores list                                   # the live store and every archive
uv run python -m stores archive --label "before ontologies"    # shows what would be archived; add --yes (server stopped)
uv run python -m stores verify <archive name>                  # re-audit an archive
```

*Reload definitions* on the same page makes Titan re-read its definition files (ontologies, skills, guidance) without a restart.

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

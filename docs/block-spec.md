# Titan Block and Template Spec (v1 draft)

Status: draft for review. No UI code is written until this is approved.

## 1. Purpose

Everything Titan can do is described as data in one registry. The REST API, the UI forms, the MCP tools and the harness all read that registry, so a capability is defined once. The first UI is a fixed set of screens plus ready-made templates. Customization (a canvas, new blocks, new templates) later means editing data that already exists, not changing the frontend.

Two kinds of definition:

- **Block**: one capability with typed inputs and outputs (a search, a fetch, a claim check).
- **Template**: a saved graph of blocks with an input form and a result view (a "Company profile" app).

## 2. Block definition

```json
{
  "id": "web.fetch_url",
  "version": 1,
  "kind": "capability",
  "title": "Fetch a page",
  "summary": "Download a URL, extract the text, archive it by hash.",
  "group": "read",
  "inputs":  [{ "name": "url", "type": "URL", "required": true }],
  "outputs": [{ "name": "source", "type": "Source" }],
  "settings": { "...JSON Schema...": "max_bytes, render_js, ..." },
  "badges": { "needs": ["network"], "costs": "free", "side_effects": ["archives page"] },
  "executor": { "type": "python", "ref": "tools.web:fetch" },
  "owner": "system",
  "permissions": { "roles": ["user", "admin"] }
}
```

Rules:

- `id` is stable and namespaced. `version` is an integer. A published version is never edited; a change creates a new version (append-only, like the evidence store).
- `kind`: `capability` (calls code), `model` (calls an LLM for a named task), `control` (branch, loop, approval gate), `agent` (model plus tools plus a goal), `composite` (a saved graph used as a block). Only `capability`, `model`, `control` ship in v1.
- `group` sets color, so the UI never hardcodes it: `find`, `read`, `check`, `compute`, `connect`, `model`, `control`.
- `settings` is JSON Schema. The UI renders a form from it; the server validates against it.
- `badges.needs` lists what must exist (`network`, `ollama`, `brave_key`, `docker`, `connector:<id>`). A block whose needs are unmet shows as unavailable, with the reason.
- `badges.costs`: `free`, `metered:<unit>`, or `paid`. `side_effects` is shown before running.
- `owner` and `permissions.roles` exist from day one and are **not enforced** in v1.

## 3. Port types (the "shapes")

A connection is valid only when output type equals input type, or the output is a subtype. The UI picks the widget per type.

| Type | Meaning | Widget |
|---|---|---|
| `Text` | plain string | text / markdown |
| `Query` | search or question string | single-line input |
| `URL` | public http(s) URL | link input, shows SSRF check result |
| `Source` | archived document: url, sha256, tier, text | source card with hash and open-original |
| `Claim` | statement with verbatim quote, source, status | claim card: quote next to source |
| `Claims` | list of `Claim` | evidence table |
| `Verdict` | support check result: supported / partially / not, reason | colored badge plus reason |
| `Model` | installed model name | model picker (from Ollama) |
| `File` | uploaded blob: size-capped, type-checked, archived by SHA-256 | drop zone, shows hash after upload |
| `Json` | anything else, schema attached | JSON viewer (renders an array of objects as a grid) |

Types are registry entries (name, parent type, widget). Adding one later changes no existing block or template, and a subtype keeps existing connections valid. `Table` is deferred: in v1 a table is a result widget over `Claims` (for example `comparison_table`), and `api.call` and `sandbox.python` return `Json`. Make `Table` a subtype of `Json` when a block needs to pass typed rows to another block.

Rule: a `Claim` can only be created by the evidence store, which verifies the quote. No block can emit a `Claim` from free text, and no UI action can bypass this.

## 4. Built-in blocks (v1, all wrap existing code)

| Group | Block | Wraps |
|---|---|---|
| find | `search.web` | `tools/search` (SearXNG, Brave, fusion) |
| find | `company.resolve_domain` | `tools/primary` |
| find | `company.discover_pages` | `tools/primary` |
| read | `web.fetch_url`, `web.render_url` | `tools/web`, `tools/browser` |
| read | `evidence.ingest_file` (new) | `File` to `Source`, same text extraction as fetched PDFs |
| connect | `api.call` | `connectors/caller` |
| connect | `capability.request` | `capability.CapabilityManager` (human gate) |
| check | `evidence.extract_claims` | runner extraction |
| check | `evidence.check_support` | `support.check_claim` |
| check | `evidence.relevance` | runner relevance gate |
| compute | `sandbox.python`, `sandbox.shell` | `tools/sandbox` |
| model | `model.chat`, `model.list` | `llm.get_chat_model`, Ollama |
| control | `control.approve` | pauses a run until a human decides |
| control | `research.run` | the existing LangGraph runner as one block |

`research.run` is the first composite. Later it can be expressed as a graph of the other blocks.

## 5. Template definition

```json
{
  "id": "company-profile",
  "version": 1,
  "title": "Company profile from primary sources",
  "summary": "...",
  "inputs": { "...JSON Schema form...": "company, focus, budget" },
  "graph": {
    "nodes": [{ "id": "n1", "block": "company.resolve_domain@1", "settings": {} }],
    "edges": [{ "from": "n1.domain", "to": "n2.domain" }]
  },
  "result": [{ "from": "n5.claims", "widget": "evidence_table" }],
  "approvals": ["control.approve"],
  "owner": "system"
}
```

The UI renders the input form, shows live progress per node, pauses at approval nodes, and renders `result` with the widget registry. The server executes the graph on LangGraph and writes every step to the append-only run log (`runs.py`).

## 6. Execution and API

- `GET /api/blocks`, `GET /api/blocks/{id}`: registry (with availability and reasons).
- `POST /api/blocks/{id}/run`: run one block (the Lab screen). Returns a run id.
- `GET /api/templates`, `POST /api/templates/{id}/run`.
- `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/events` (server-sent events, resumable by event number), `POST /api/runs/{id}/decision` (approve / reject).
- `GET /api/models`, `POST /api/chat` (streaming).
- `GET /api/evidence/...` (read only), `GET /api/connectors`, gate endpoints for the capability manager.
- `GET /api/health`: Ollama, SearXNG, Brave, Langfuse, evidence audit.

Every run, including single-block runs from the Lab, is a run with events. There is one execution path.

## 7. Invariants (cannot be bypassed by any block, template or UI)

1. Claims are created only through the evidence store (verbatim quote verified, append-only).
2. Connector activation needs the single human gate; API keys are referenced by name, never shown or stored in definitions.
3. Network access goes through the SSRF guard; API calls through the guarded caller (rate limit, budget, no redirects, size cap).
4. Budgets (searches, fetches, API calls, tokens) are enforced by the runner; a template can lower them, not raise them past the configured maximum.
5. Block and template versions are append-only; runs record the exact versions used.
6. Power mode (Anthropic) applies only to tasks in `POWER_MODE_TASKS`, and the UI shows which model produced each result.
7. Server binds to localhost only, requires a session token, checks the Host header.
8. Uploaded files become sources with tier `user_provided`, never `primary`. Claims from them still need a verbatim quote from the extracted text.

## 8. Frontend (fixed screens, rendered from the registry)

Home (template gallery), Models, Chat, Lab (any single block), Runs (timeline, approvals), Evidence (claims beside quotes, verdicts), Connectors (the gate), Health. Routes are keyed by id: `/apps/:id`, `/blocks/:id`, `/runs/:id`.

Stack: React, TypeScript, Vite, Tailwind with shadcn/ui, TanStack Query. React Flow is added later for the canvas; nothing in v1 conflicts with it.

## 9. Templates shipped in v1

1. **Company profile**: resolve domain, discover pages, fetch, extract claims, check support, evidence table.
2. **Competitor pricing and plans**: same chain, focus on pricing pages, comparison table.
3. **Check a claim**: user gives a claim and sources; verdict plus quote.
4. **Research report**: the existing runner with approval gates.
5. **Analyze a URL or PDF**: fetch, archive, extract claims.
6. **Add a data source**: the capability manager flow.

Later: patent and IP family lookup (needs a working connector), model comparison, sandbox number-crunching.

## 10. Out of scope for v1

Canvas editor, composite and agent authoring, role enforcement, theme editor, drag-and-drop layouts, scheduled jobs, MCP-server connectors.

## 11. Decisions (accepted)

1. Templates allow user-edited settings only (budgets, models) in v1. Swapping blocks is later.
2. Built-in definitions are JSON files in the repo, versioned in git. User-made definitions go to SQLite later, with the same schema.
3. Chat is plain model chat plus an opt-in tool list; every tool call is shown.
4. Port types: the section 3 list including `File`. `Table` is deferred (see section 3).

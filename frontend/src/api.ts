// The one place the app talks to the Titan API. Nothing here decides anything: the server owns the rules.

const token = document.querySelector('meta[name="titan-token"]')?.getAttribute('content') ?? ''

export class ApiError extends Error {
  status: number
  problems: string[]
  constructor(status: number, problems: string[]) {
    super(problems.join('; ') || `Request failed (${status})`)
    this.status = status
    this.problems = problems
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['X-Titan-Token'] = token // production only; the dev server adds it itself
  const res = await fetch(path, { ...init, headers: { ...headers, ...(init?.headers as Record<string, string>) } })
  if (!res.ok) {
    let problems: string[] = []
    try {
      const body = await res.json()
      const d = body.detail
      problems = Array.isArray(d?.problems) ? d.problems : typeof d === 'string' ? [d] : Array.isArray(d) ? d.map((x: { msg?: string }) => x.msg ?? String(x)) : []
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, problems.length ? problems : [`The server answered ${res.status}`])
  }
  return res.json() as Promise<T>
}

const post = <T,>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })

// ---- types -------------------------------------------------------------------------------------

export interface SchemaProp {
  type: 'string' | 'integer' | 'boolean'
  title?: string
  description?: string
  default?: unknown
  minimum?: number
  maximum?: number
  minLength?: number
  'x-multiline'?: boolean
}
export interface Template {
  id: string
  title: string
  summary: string
  kind: 'research' | 'capability'
  inputs: { properties: Record<string, SchemaProp>; required?: string[] }
}
export interface RunSummary {
  run_id: string
  started_at: string
  question: string | null
  kind: string | null
  status: string | null
}
export interface RunEvent {
  event_id: number
  ts: string
  node: string
  type: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: Record<string, any>
}
export interface Approval {
  question: string
  subject: string
  queries: string[]
  checklist: string[]
  page_categories: string[]
  premise?: { status: 'none' | 'verified' | 'unverified' | 'overridden'; items: string[]; missing: string[]; claims: { claim_id: number; claim: string; source_url: string }[] }
  corrections?: string[]
  advice?: { sources: { site: string; why: string }[]; suggestion: string | null }
}
export interface ReportClaim {
  claim_id: number
  claim: string
  value: string
  quote: string
  source_url: string
  source_sha256: string
  source_tier: string
  source_date?: string | null
  source_date_kind?: string | null
  times?: ClaimTime[]
  corroboration?: string
  as_of?: string | null
  why?: string
}
export interface ClaimTime {
  kind: string
  value: string
  precision: string
  raw: string
  origin: string
  derived: number
  detail: { label?: string; basis?: string; span?: string; fiscal_year?: number; quarter?: number; half?: number; start?: string; end?: string; comparison?: string; change?: string; form?: string }
}
export interface Report {
  question: string
  subject: string
  summary: string
  established: ReportClaim[]
  partial: ReportClaim[]
  not_supported: number
  unchecked: number
  off_topic: number
  conflicts?: { claims: number[]; note: string }[]
  outcome?: 'answered' | 'partial' | 'not_found' | 'premise_unverified'
  premise?: { text: string; verified: boolean }[]
  gaps: string[]
  rejected_quotes: number
  warnings: string[]
  budget_used: { searches: number; fetches: number; iterations: number }
  time?: {
    today: string
    scope: string | null
    applies: boolean
    newest_source_date: string | null
    age_days: number | null
    undated_sources: number
    stale: boolean
  }
}
export interface RunDetail {
  run_id: string
  kind: 'research' | 'block'
  status: string | null
  title: string | null
  subject?: string | null
  approval?: Approval | null
  capability?: { request_id: string; need: string; reason: string } | null
  source_request?: { stage: 'premise' | 'answer'; missing: string[]; sources_asked: number } | null
  report?: Report | null
  outputs?: unknown
}
export interface Fact {
  field: string
  value: string
  quote: string
  source_url: string
  official?: boolean
}
export interface Candidate {
  name: string
  connector_id: string
  kind: string
  docs_url: string
  cost_label?: string
  verified_facts: Fact[]
  unverified: string[]
  warnings?: string[]
  cannot_integrate?: string
  terms_documents: { url: string; why: string }[]
  integration_plan?: {
    host: string
    tier: string
    auth: string
    key_env: string | null
    endpoints: Record<string, string>
    smoke_test: { endpoint: string }
    budget: { max_calls_per_day: number; max_calls_per_run: number }
    rate_limit_per_minute: number
    signup_url: string | null
  }
}
export interface Gate {
  need: string
  recommendation: { choice: string; reasoning: string }
  candidates: Candidate[]
  your_steps: string[]
  note?: string
}
export interface CapabilityRequest {
  request_id: string
  run_id: string | null
  need: string
  status: string
  created_at: string
  events: { event: string; detail: Record<string, unknown>; at: string }[]
  gate: Gate | null
}
export interface ConnectorSummary {
  connector_id: string
  version: number
  name: string
  host: string
  tier: string
  auth: string
  endpoints: string[]
  budget: { max_calls_per_day: number }
  calls_last_24h: number
}
export interface ClaimRow {
  claim_id: number
  claim_text: string
  value: string
  entity: string
  source_tier: string
  source_url: string
  verdict: string | null
}
export interface ClaimDetail {
  claim_id: number
  claim_text: string
  quote: string
  before: string
  after: string
  entity: { name: string; domain: string; domain_status: string | null }
  source_url: string
  source_tier: string
  source_sha256: string
  text_sha256: string
  fetched_at: string
  created_at: string
  created_by: string
  support_checks: { verdict: string; reasoning: string; checker: string; created_at: string; unsupported_parts: string; missing_tokens: string }[]
  times?: ClaimTime[]
  conflicts?: { other_claim: number; note: string }[]
  source_dates?: { kind: string; value: string; precision: string; raw: string; origin: string; relative: number }[]
}
export interface Health {
  mode: string
  checks: Record<string, { ok: boolean; detail: string | null }>
  search_providers: string[]
  brave_key_set: boolean
  langfuse_configured: boolean
  evidence: Record<string, number | string[]>
}
export interface Models {
  ollama_reachable: boolean
  installed: string[]
  mode: string
  tasks: { task: string; model: string; installed: boolean }[]
}

export interface HarnessNode { id: string; label: string; col: number; row: number; does: string }
export interface HarnessEdge { from: string; to: string; label?: string; bend?: number; via?: [number, number][] }
export interface Harness { id: string; title: string; nodes: HarnessNode[]; edges: HarnessEdge[] }

// ---- calls -------------------------------------------------------------------------------------

export const api = {
  harness: () => request<Harness>('/api/harness/research'),
  templates: () => request<Template[]>('/api/templates'),
  runTemplate: (id: string, inputs: Record<string, unknown>) =>
    post<{ kind: 'research' | 'capability'; run_id?: string; request_id?: string }>(`/api/templates/${id}/run`, { inputs }),
  runs: () => request<RunSummary[]>('/api/runs'),
  run: (id: string) => request<RunDetail>(`/api/runs/${id}`),
  runEvents: (id: string, after: number) => request<RunEvent[]>(`/api/runs/${id}/events?after=${after}`),
  decide: (id: string, body: { approved: boolean; queries?: string[]; checklist?: string[]; comment?: string; url?: string; override?: boolean }) => post(`/api/runs/${id}/decision`, body),
  giveSource: (id: string, url: string | null) => post(`/api/runs/${id}/source`, { url }),
  continueRun: (id: string, capability: string | null) => post(`/api/runs/${id}/continue`, { capability }),
  connectors: () => request<ConnectorSummary[]>('/api/connectors'),
  requests: () => request<Omit<CapabilityRequest, 'events' | 'gate'>[]>('/api/capability/requests'),
  request: (id: string) => request<CapabilityRequest>(`/api/capability/requests/${id}`),
  decideRequest: (id: string, body: { approve: string | null; terms_ok: boolean }) => post(`/api/capability/requests/${id}/decision`, body),
  claims: (entity?: string) => request<ClaimRow[]>(`/api/evidence/claims${entity ? `?entity=${encodeURIComponent(entity)}` : ''}`),
  claim: (id: string) => request<ClaimDetail>(`/api/evidence/claims/${id}`),
  health: () => request<Health>('/api/health'),
  models: () => request<Models>('/api/models'),
}

import type { RunEvent } from './api'

const short = (s: unknown, n = 80) => {
  const t = String(s ?? '')
  return t.length > n ? t.slice(0, n - 1) + '…' : t
}

type Fmt = (p: RunEvent['payload']) => string
// What a person reads for each step a run takes. Anything unlisted still shows its type and a trimmed payload.
const DESCRIBE: Record<string, [string, Fmt]> = {
  queued: ['Waiting for its turn', () => 'Runs start one at a time because the local models use the same graphics card.'],
  run_started: ['Run started', (p) => short(p.question ?? p.title, 120)],
  plan_ready: ['Plan drafted', (p) => `${(p.queries ?? []).length} searches, ${(p.checklist ?? []).length} facts to establish${p.time_scope ? ` · time scope: ${String(p.time_scope.intent).replace('_', ' ')}` : ''}`],
  model_used: ['Language model', (p) => `${p.model} (${String(p.task).replace(/_/g, ' ')})`],
  scout_done: ['Quick look', (p) => `${p.results} results from ${(p.sites ?? []).length} sites · searched with ${(p.engines ?? []).join(', ') || 'no engine answered'}`],
  premise_screened: ['Premise screen', (p) => `${p.status === 'reported' ? 'something relevant is reported' : p.status === 'unverified' ? 'nothing relevant reported' : 'not judged'} · “${short(p.query, 60)}”, top ${p.results} results via ${(p.engines ?? []).join(', ')}`],
  premise_screen_failed: ['Premise screen could not search', (p) => short(p.error, 100)],
  awaiting_approval: ['Waiting for your approval', () => 'Review the plan below.'],
  approved: ['Plan approved', () => ''],
  resumed: ['Continued', () => ''],
  primary_pages: ['Found the company’s own pages', (p) => `${p.pages} pages on ${p.domain}`],
  domain_unconfirmed: ['Official website not confirmed', () => 'Pages from other sites are treated as secondary.'],
  search_done: ['Searched the web', (p) => `“${short(p.query, 60)}”: ${p.results} results${p.recency ? ` · last ${p.recency} only` : ''}${p.warnings?.length ? ` (${p.warnings.length} warnings)` : ''}`],
  sources_selected: ['Chose pages to read', (p) => `${(p.urls ?? []).length} pages`],
  source_fetched: ['Read a page', (p) => `${short(p.url, 70)} · ${p.tier} · ${p.readable_chars} characters · ${p.best_date ? `dated ${String(p.best_date).slice(0, 10)}` : 'no date found'}`],
  source_rendered: ['Read a page in the browser', (p) => short(p.url, 70)],
  source_failed: ['Could not read a page', (p) => `${short(p.url, 60)}: ${short(p.error, 80)}`],
  claim_recorded: ['Recorded a claim', (p) => `#${p.claim_id} ${short(p.claim, 100)}`],
  sources_routed: ['Chose data sources', (p) => `offered: ${(p.offered ?? []).join(', ') || 'none'}${(p.hidden ?? []).length ? ` · skipped: ${p.hidden.map((h: { id: string }) => h.id).join(', ')}` : ''}`],
  claim_time_recorded: ['Read when it holds', (p) => `#${p.claim_id} ${(p.items ?? []).map((i: { kind: string; value: string }) => `${i.kind} ${i.value}`.trim()).join(', ')}`],
  claims_malformed: ['Dropped a malformed claim', (p) => `${p.count} from ${short(p.source_url, 50)}: ${short(p.examples?.[0]?.problem ?? 'the model left out a required field', 90)}`],
  quote_narrowed: ['Shortened a long quote', (p) => `${p.from_chars} characters → “${short(p.to, 80)}” (same source text, so it is still word for word)`],
  quote_widened: ['Extended a short quote', (p) => `“${short(p.from, 40)}” → “${short(p.to, 70)}” (same source text, so it is still word for word)`],
  claim_rejected: ['Rejected a claim', (p) => `${p.reason}: “${short(p.quote, 60)}”`],
  support_checked: ['Checked a claim against its quote', (p) => `claim #${p.claim_id}: ${String(p.verdict).replace('_', ' ')}`],
  relevance_checked: ['Checked relevance', (p) => `claim #${p.claim_id}: ${p.addresses ? 'answers a needed fact' : 'does not answer a needed fact'}`],
  coverage_assessed: ['Assessed coverage', (p) => `round ${p.iteration}: ${(p.gaps ?? []).length} facts still missing`],
  api_called: ['Called a connector', (p) => `${p.connector}.${p.endpoint}`],
  api_skipped: ['Skipped a connector call', (p) => `${p.connector}.${p.endpoint}: ${short(p.reason, 120)}`],
  api_failed: ['A connector call failed', (p) => `${p.connector}.${p.endpoint}: ${short(p.error, 80)}`],
  capability_needed: ['Needs a data source', (p) => short(p.need, 120)],
  statements_read: ['Read financial statements', (p) => `${short(p.url, 60)} · ${(p.statements ?? []).map((t: { statement: string }) => t.statement).join(', ')}`],
  statements_doubt: ['Statement figures to double-check', (p) => `${p.doubt_count} figure${p.doubt_count === 1 ? '' : 's'} in doubt${(p.unread_pages ?? []).length ? ` · ${p.unread_pages.length} page(s) unread` : ''}`],
  figures_checked: ['Checked figures against the statements', (p) => `claim #${p.claim_id}: ${p.label || Object.entries(p.counts ?? {}).map(([k, v]) => `${v} ${String(k).replace('_', ' ')}`).join(', ')}`],
  pause_requested: ['You asked to pause', () => 'the run pauses at the next safe point (after the model call in progress)'],
  stop_requested: ['You asked to stop', () => 'what is left is skipped; the report is written from what was gathered'],
  paused: ['Paused', (p) => (p.by === 'you' ? 'everything done so far is saved; Resume carries on from here' : `before: ${(p.next ?? []).join(', ')}`)],
  extract_paused: ['Paused while reading', (p) => `${p.sources_left} page(s) left to read; ${p.claims_so_far} claim(s) so far`],
  extract_resumed: ['Carried on reading', (p) => `${p.sources_left} page(s) left`],
  check_paused: ['Paused while checking', (p) => `${p.claims_left} claim(s) left to check`],
  claim_verified: ['Verified a claim', (p) => `#${p.claim_id}: ${p.outcome}`],
  blog_checked: ['Checked a blog', (p) => `${short(p.url, 60)}: ${p.tier ? `Tier ${p.tier}` : p.official ? 'official' : 'not official'}`],
  report_ready: ['Report written', () => ''],
  run_finished: ['Finished', () => ''],
  run_stopped: ['Stopped', (p) => short(p.reason, 100)],
  run_failed: ['Failed', (p) => short(p.error, 160)],
  llm_failed: ['A model call failed', (p) => `${p.task}: ${short(p.error, 100)}`],
  node_started: ['Step started', (p) => short(p.block, 60)],
  node_finished: ['Step finished', () => ''],
  node_failed: ['Step failed', (p) => short(p.error, 160)],
}

export function describeEvent(e: RunEvent): { label: string; detail: string; tone: 'ok' | 'bad' | 'warn' | 'info' | 'neutral' } {
  const [label, fmt] = DESCRIBE[e.type] ?? [e.type.replace(/_/g, ' '), (p: RunEvent['payload']) => short(JSON.stringify(p), 120)]
  const tone = ['run_finished', 'approved', 'claim_recorded'].includes(e.type) ? 'ok'
    : ['run_failed', 'node_failed', 'source_failed', 'claim_rejected', 'llm_failed', 'api_failed'].includes(e.type) ? 'bad'
    : e.type === 'figures_checked' ? ((e.payload.differs ?? []).length ? 'bad' : (e.payload.to_double_check ?? []).length ? 'warn' : 'ok')
    : ['awaiting_approval', 'capability_needed', 'domain_unconfirmed', 'api_skipped', 'claims_malformed', 'statements_doubt'].includes(e.type) ? 'warn'
    : ['run_started', 'resumed'].includes(e.type) ? 'info' : 'neutral'
  return { label, detail: fmt(e.payload), tone }
}

export const ACTIVE = new Set(['queued', 'running', 'pausing', 'stopping'])

export function lines(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter(Boolean)
}

export function time(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

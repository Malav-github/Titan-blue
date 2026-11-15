import type { RunEvent } from './api'
import type { NodeState } from './flowState'

/** The kinds of run that check things rather than research them: Verify (`check`: a text, page or file) and a research run's
 * claims checked again (`verify`). Neither runs the research pipeline, so neither may be drawn as one. */
export type CheckKind = 'check' | 'verify'

export interface CheckStage { id: string; label: string; state: NodeState; detail: string }
export interface CheckItem {
  n: number
  text: string
  state: 'waiting' | 'checking' | 'checked' | 'skipped'
  outcome?: string
  route?: string                       // how it was checked: the company's own filings, or independent sources
  checks: { check: string; result: string }[]
  why?: string                          // why it was not checked
}
export interface CheckFlowView { stages: CheckStage[]; items: CheckItem[] }

const WORKING = new Set(['queued', 'running', 'pausing'])
const payloads = (events: RunEvent[], type: string) => events.filter((e) => e.type === type).map((e) => e.payload as Record<string, unknown>)

/** What a check run has done so far, from its own events only: never a step it does not have. */
export function checkFlowView(kind: CheckKind, events: RunEvent[], status: string | null | undefined): CheckFlowView {
  const started = payloads(events, 'run_started')[0]
  const failed = status === 'failed'
  const finished = status === 'finished'
  const stopped = status === 'stopped'
  const working = WORKING.has(status ?? '')
  const items = kind === 'check' ? statementItems(events) : claimItems(events)
  // the first item not yet checked is the one being checked while the run works
  const next = items.find((i) => i.state === 'waiting')
  if (next && working) next.state = 'checking'
  const checked = items.filter((i) => i.state === 'checked').length
  const checkable = items.filter((i) => i.state !== 'skipped').length

  const stage = (id: string, label: string, state: NodeState, detail: string): CheckStage => ({ id, label, state, detail })
  const read = started ? 'done' : failed ? 'failed' : working ? 'active' : 'pending'
  const listed = kind === 'check' ? payloads(events, 'statements_found')[0] : started
  const split: NodeState = listed ? 'done' : read === 'done' && failed ? 'failed' : read === 'done' ? 'active' : 'pending'
  let each: NodeState = 'pending'
  if (listed) each = checked === checkable ? 'done' : failed ? 'failed' : status === 'paused' ? 'waiting' : stopped ? 'done' : 'active'
  const end: NodeState = finished || stopped ? 'done' : failed ? 'failed' : status === 'paused' ? 'waiting' : 'pending'
  const stages = kind === 'check'
    ? [stage('input', 'Read the input', read, 'Your text, page or file is archived first, so every check traces back to exactly what was read.'),
       stage('split', 'Find statements', split, 'Code splits the text into statements and keeps those with a figure, a date, a role or an event; forecasts and opinions are not checked.'),
       stage('each', 'Check each statement', each, 'A statement naming who holds a role is checked against the company’s own SEC filings; any other against independent sources, never against the input itself.'),
       stage('end', stopped ? 'Stopped' : 'Verdicts', end, stopped ? 'Stopped by you: the statements checked so far are kept.' : 'Each statement ends corroborated, partly corroborated, contradicted or unconfirmed.')]
    : [stage('input', 'Claims chosen', read, 'The claims of a research run, chosen to be checked again.'),
       stage('each', 'Check each claim', each, 'For each claim: is the quote still on its page, does a second independent source say the same, does any source contradict it, is there a newer report, and do its figures match the company’s statements.'),
       stage('end', stopped ? 'Stopped' : 'Verdicts', end, stopped ? 'Stopped by you: the claims checked so far are kept.' : 'Each claim ends corroborated, partly corroborated, contradicted or unconfirmed.')]
  return { stages, items }
}

function statementItems(events: RunEvent[]): CheckItem[] {
  const found = (payloads(events, 'statements_found')[0]?.statements ?? []) as { n: number; text: string; checkable: boolean; why?: string; claim_id?: number }[]
  const done = new Map(payloads(events, 'statement_checked').map((p) => [p.n as number, p]))
  return found.map((s) => {
    const r = done.get(s.n)
    if (!s.checkable || !s.claim_id) return { n: s.n, text: s.text, state: 'skipped', checks: [], why: s.why }
    if (!r) return { n: s.n, text: s.text, state: 'waiting', checks: [] }
    const checks = ((r.checks ?? []) as { check: string; result: string }[]).map((c) => ({ check: c.check, result: c.result }))
    return { n: s.n, text: s.text, state: 'checked', outcome: r.outcome as string, checks,
             route: r.role ? 'the company’s own SEC filings' : 'independent sources' }
  })
}

function claimItems(events: RunEvent[]): CheckItem[] {
  const claims = (payloads(events, 'run_started')[0]?.claims ?? []) as (number | { claim_id: number; text?: string })[]
  const done = new Map(payloads(events, 'claim_verified').map((p) => [p.claim_id as number, p]))
  return claims.map((c, i) => {
    const id = typeof c === 'number' ? c : c.claim_id
    const r = done.get(id)
    const text = typeof c === 'number' ? `Claim ${id}` : (c.text ?? `Claim ${id}`)
    if (!r) return { n: i + 1, text, state: 'waiting', checks: [] }
    return { n: i + 1, text, state: 'checked', outcome: r.outcome as string, route: 'independent sources',
             checks: ((r.checks ?? []) as { check: string; result: string }[]).map((x) => ({ check: x.check, result: x.result })) }
  })
}

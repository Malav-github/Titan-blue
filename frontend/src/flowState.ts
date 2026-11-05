import type { RunEvent } from './api'

/** The straight path through the pipeline, in order. Later steps imply the earlier ones ran, even if a step logged nothing. */
export const MAIN_ORDER = ['plan', 'approve', 'discover', 'select', 'fetch', 'api_lookup', 'extract', 'check', 'assess']

export type NodeState = 'pending' | 'done' | 'active' | 'waiting' | 'failed'
export interface NodeView {
  state: NodeState
  badges: { text: string; warn?: boolean }[]
  events: RunEvent[]
}

// Events written by the runner itself, not by a node, belong to the step they are about.
const OWNER: Record<string, string> = { awaiting_approval: 'approve', approved: 'approve', capability_needed: 'capability', source_needed: 'gate' }
const ownerOf = (e: RunEvent) => OWNER[e.type] ?? e.node

const count = (events: RunEvent[], type: string) => events.filter((e) => e.type === type).length

/** What each step of a run has done so far, worked out from the run's event log. */
export function flowView(ids: string[], events: RunEvent[], status: string | null | undefined): Record<string, NodeView> {
  const by: Record<string, RunEvent[]> = Object.fromEntries(ids.map((i) => [i, []]))
  for (const e of events) (by[ownerOf(e)] ??= []).push(e)

  const furthest = Math.max(-1, ...MAIN_ORDER.map((id, i) => (by[id]?.length ? i : -1)))
  const last = [...events].reverse().find((e) => MAIN_ORDER.includes(e.node) || ['capability', 'report', 'premise', 'gate'].includes(e.node))
  const finished = status === 'finished'
  const view: Record<string, NodeView> = {}
  for (const id of ids) {
    const mine = by[id] ?? []
    let state: NodeState = 'pending'
    const idx = MAIN_ORDER.indexOf(id)
    if (mine.length || (idx >= 0 && idx < furthest) || (id === 'report' && finished)) state = 'done'
    if (id === 'approve' && status === 'awaiting_approval') state = 'waiting'
    else if (id === 'capability' && status === 'needs_capability') state = 'waiting'
    else if (id === 'gate' && status === 'needs_source') state = 'waiting'
    else if (status === 'running' && last && ownerOf(last) === id) state = 'active'
    if (status === 'failed' && last && ownerOf(last) === id) state = 'failed'
    view[id] = { state, badges: [], events: mine }
  }

  const ok = (id: string, text: string, warn = false) => view[id] && view[id].badges.push({ text, warn })
  const searches = count(by.discover ?? [], 'search_done')
  if (searches) ok('discover', `${searches} search${searches === 1 ? '' : 'es'}`)
  const chosen = (by.select ?? []).filter((e) => e.type === 'sources_selected').reduce((n, e) => n + (e.payload.urls?.length ?? 0), 0)
  if (chosen) ok('select', `${chosen} pages chosen`)
  const read = count(by.fetch ?? [], 'source_fetched') + count(by.fetch ?? [], 'source_rendered')
  if (read) ok('fetch', `${read} read`)
  if (count(by.fetch ?? [], 'source_failed')) ok('fetch', `${count(by.fetch ?? [], 'source_failed')} blocked`, true)
  if (count(by.api_lookup ?? [], 'api_called')) ok('api_lookup', `${count(by.api_lookup ?? [], 'api_called')} calls`)
  const routed = (by.api_lookup ?? []).find((e) => e.type === 'sources_routed')
  if (routed && !(routed.payload.offered ?? []).length) ok('api_lookup', 'none fit the question')
  if (count(by.api_lookup ?? [], 'api_failed') + count(by.api_lookup ?? [], 'api_skipped')) {
    ok('api_lookup', `${count(by.api_lookup ?? [], 'api_failed') + count(by.api_lookup ?? [], 'api_skipped')} refused or failed`, true)
  }
  const claims = count(by.extract ?? [], 'claim_recorded')
  if (claims) ok('extract', `${claims} claim${claims === 1 ? '' : 's'}`)
  const rejected = count(by.extract ?? [], 'claim_rejected')
  if (rejected) ok('extract', `${rejected} rejected`, true)
  const checked = count(by.check ?? [], 'support_checked')
  if (checked) ok('check', `${checked} checked`)
  if (count(by.premise ?? [], 'premise_check')) ok('premise', 'checking')
  if (count(by.gate ?? [], 'premise_verified')) ok('gate', 'premise held')
  if (count(by.gate ?? [], 'source_supplied')) ok('gate', 'your source read')
  if (count(by.gate ?? [], 'gate_stopped')) ok('gate', 'stopped', true)
  const rounds = count(by.assess ?? [], 'coverage_assessed')
  if (rounds) ok('assess', `round ${rounds}`)
  return view
}

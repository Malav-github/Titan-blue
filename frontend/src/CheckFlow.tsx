import { useState } from 'react'
import type { RunEvent } from './api'
import { checkFlowView, type CheckKind } from './checkFlowState'
import type { NodeState } from './flowState'
import { Badge, Card } from './ui'

const FILL: Record<NodeState, string> = {
  pending: 'border-line bg-surface', done: 'border-ok bg-ok-bg', active: 'border-info bg-info-bg',
  waiting: 'border-warn bg-warn-bg', failed: 'border-bad bg-bad-bg',
}
const STATE_WORDS: Record<NodeState, string> = {
  pending: 'not reached yet', done: 'done', active: 'working now', waiting: 'paused', failed: 'failed',
}
const OUTCOME: Record<string, ['ok' | 'info' | 'bad' | 'warn', string]> = {
  corroborated: ['ok', 'Corroborated'], partial: ['info', 'Partly corroborated'], contradicted: ['bad', 'Contradicted'], unconfirmed: ['warn', 'Unconfirmed'],
}

/** The steps of a check run (Verify, or a research run's claims checked again), lit from its own events. It has no research
 * steps (plan, search, extract ...): a check run does not take them, so it is not drawn as if it had. */
export function CheckFlow({ kind, events, status }: { kind: CheckKind; events: RunEvent[]; status?: string | null }) {
  const view = checkFlowView(kind, events, status)
  const [picked, setPicked] = useState<string | null>(null)
  const selected = view.stages.find((s) => s.id === picked)
  const word = kind === 'check' ? 'statement' : 'claim'
  return (
    <Card>
      <h2 className="font-semibold">How this {kind === 'check' ? 'check' : 'verification'} runs</h2>
      <ol className="mt-3 flex flex-wrap items-stretch gap-2" aria-label={`Steps of this ${kind === 'check' ? 'check' : 'verification'}`}>
        {view.stages.map((s, i) => (
          <li key={s.id} className="flex items-center gap-2">
            <button type="button" aria-label={`${s.label}: ${STATE_WORDS[s.state]}`} aria-pressed={picked === s.id}
              className={`min-w-36 rounded-lg border-2 px-3 py-2 text-left text-sm ${FILL[s.state]}`} onClick={() => setPicked(picked === s.id ? null : s.id)}>
              <span className="block font-medium">{s.label}</span>
              <span className="block text-xs text-muted">{STATE_WORDS[s.state]}</span>
            </button>
            {i < view.stages.length - 1 && <span aria-hidden className="text-muted">→</span>}
          </li>
        ))}
      </ol>
      {selected && <p className="mt-2 text-sm text-muted">{selected.detail}</p>}
      {view.items.length > 0 && (
        <>
          <h3 className="mt-4 text-sm font-semibold">Each {word}</h3>
          <ol className="mt-2 flex flex-col gap-1 text-sm">
            {view.items.map((it) => (
              <li key={it.n} className="flex flex-wrap items-baseline gap-2 border-t border-line pt-1">
                <span className="w-6 shrink-0 text-xs text-muted">{it.n}</span>
                <span className="min-w-0 flex-1 break-words">{it.text}</span>
                {it.state === 'skipped' && <Badge>not checked{it.why ? `: ${it.why}` : ''}</Badge>}
                {it.state === 'waiting' && <Badge>waiting</Badge>}
                {it.state === 'checking' && <Badge tone="info">checking now</Badge>}
                {it.state === 'checked' && it.outcome && <Badge tone={(OUTCOME[it.outcome] ?? ['neutral'])[0] as 'ok'}>{(OUTCOME[it.outcome] ?? [, it.outcome])[1]}</Badge>}
                {it.state === 'checked' && (
                  <span className="basis-full pl-8 text-xs text-muted">
                    Checked against {it.route}{it.checks.length > 0 && <>: {it.checks.map((c) => `${c.check.replace(/_/g, ' ')} ${c.result}`).join('; ')}</>}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </Card>
  )
}

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type HarnessEdge, type HarnessNode, type RunEvent } from './api'
import { describeEvent } from './format'
import { flowView, type NodeState } from './flowState'
import { Badge, Card, Loading } from './ui'

const W = 150
const H = 80
const GX = 250
const GY = 124
const PAD = 20

const FILL: Record<NodeState, string> = {
  pending: 'fill-surface stroke-line',
  done: 'fill-ok-bg stroke-ok',
  active: 'fill-info-bg stroke-info',
  waiting: 'fill-warn-bg stroke-warn',
  failed: 'fill-bad-bg stroke-bad',
}
const STATE_WORDS: Record<NodeState, string> = {
  pending: 'not reached yet', done: 'done', active: 'working now', waiting: 'waiting for you', failed: 'failed',
}

type Pt = { x: number; y: number }
const centre = (n: HarnessNode): Pt => ({ x: PAD + n.col * GX + W / 2, y: PAD + n.row * GY + H / 2 })

/** Where a line from this box towards a point leaves the box. */
function edgePoint(c: Pt, toward: Pt): Pt {
  const dx = toward.x - c.x
  const dy = toward.y - c.y
  const k = Math.min(dx === 0 ? Infinity : W / 2 / Math.abs(dx), dy === 0 ? Infinity : H / 2 / Math.abs(dy))
  return { x: c.x + dx * k, y: c.y + dy * k }
}

/** A path through waypoints given in grid units (column, row); the ends are clipped to the boxes. */
function routeVia(a: Pt, b: Pt, via: [number, number][]) {
  const pts = via.map(([c, r]) => ({ x: PAD + c * GX + W / 2, y: PAD + r * GY + H / 2 }))
  const p1 = edgePoint(a, pts[0])
  const p2 = edgePoint(b, pts[pts.length - 1])
  const all = [p1, ...pts, p2]
  // the label sits on the longest stretch, where there is most room
  let best = 0
  for (let i = 1; i < all.length; i++) if (Math.hypot(all[i].x - all[i - 1].x, all[i].y - all[i - 1].y) > Math.hypot(all[best + 1].x - all[best].x, all[best + 1].y - all[best].y)) best = i - 1
  return { d: 'M' + all.map((p) => `${p.x},${p.y}`).join(' L'), labelAt: { x: (all[best].x + all[best + 1].x) / 2, y: (all[best].y + all[best + 1].y) / 2 } }
}

function route(a: Pt, b: Pt, bend = 0) {
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
  const len = Math.hypot(b.x - a.x, b.y - a.y) || 1
  const ctl = { x: mid.x - ((b.y - a.y) / len) * bend * len, y: mid.y + ((b.x - a.x) / len) * bend * len }
  const p1 = edgePoint(a, bend ? ctl : b)
  const p2 = edgePoint(b, bend ? ctl : a)
  const labelAt = bend ? { x: 0.25 * p1.x + 0.5 * ctl.x + 0.25 * p2.x, y: 0.25 * p1.y + 0.5 * ctl.y + 0.25 * p2.y } : { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 }
  return { d: bend ? `M${p1.x},${p1.y} Q${ctl.x},${ctl.y} ${p2.x},${p2.y}` : `M${p1.x},${p1.y} L${p2.x},${p2.y}`, labelAt }
}

function wrap(label: string, max = 16): string[] {
  const words = label.split(' ')
  const out: string[] = []
  for (const w of words) out.length && (out[out.length - 1] + ' ' + w).length <= max ? (out[out.length - 1] += ' ' + w) : out.push(w)
  return out
}

/** The research pipeline as a flowchart. With a run's events it lights up as the run moves; without them it is the plain diagram. */
export function Flow({ events, status }: { events?: RunEvent[]; status?: string | null }) {
  const harness = useQuery({ queryKey: ['harness', 'research'], queryFn: api.harness, staleTime: Infinity })
  const [picked, setPicked] = useState<string | null>(null)
  if (harness.isLoading) return <Loading what="Loading the flowchart" />
  if (!harness.data) return null
  const { nodes } = harness.data
  const edges = harness.data.edges.filter((e) => !e.control)      // pause and stop links are said in words, not drawn
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const live = events !== undefined
  const view = flowView(nodes.map((n) => n.id), events ?? [], status)
  const width = Math.max(...nodes.map((n) => n.col)) * GX + W + PAD * 2
  const lowest = Math.max(...nodes.map((n) => n.row), ...edges.flatMap((e) => (e.via ?? []).map(([, r]) => r - 0.3)))
  const height = lowest * GY + H + PAD * 2
  const selected = picked ? byId[picked] : null
  const taken = (e: HarnessEdge) => live && view[e.from].state !== 'pending' && view[e.to].state !== 'pending'

  return (
    <section aria-labelledby="flow-h" className="flex flex-col gap-3">
      <h2 id="flow-h" className={live ? 'text-lg font-semibold' : 'sr-only'}>{live ? 'Where this run is' : harness.data.title}</h2>
      <Card className="overflow-x-auto p-2">
        <svg viewBox={`0 0 ${width} ${height}`} className="mx-auto w-full min-w-[640px] max-w-[980px]" role="group" aria-label="Flowchart of the research steps">
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" className="fill-muted" />
            </marker>
          </defs>
          {edges.map((e) => {
            const r = e.via ? routeVia(centre(byId[e.from]), centre(byId[e.to]), e.via) : route(centre(byId[e.from]), centre(byId[e.to]), e.bend)
            return (
              <g key={`${e.from}-${e.to}`}>
                <path d={r.d} fill="none" markerEnd="url(#arrow)" strokeWidth={taken(e) ? 2 : 1.25} strokeDasharray={e.label && !taken(e) ? '5 4' : undefined}
                  className={taken(e) ? 'stroke-muted' : 'stroke-line'} />
                {e.label && (
                  <text x={r.labelAt.x} y={r.labelAt.y - 4} textAnchor="middle" className="fill-muted" fontSize="11.5" paintOrder="stroke" strokeWidth="3" stroke="var(--bg, white)">
                    {e.label}
                  </text>
                )}
              </g>
            )
          })}
          {nodes.map((n) => {
            const v = view[n.id]
            const lines = wrap(n.label)
            const x = PAD + n.col * GX
            const y = PAD + n.row * GY
            return (
              <g key={n.id} transform={`translate(${x},${y})`} role="button" tabIndex={0} aria-pressed={picked === n.id}
                aria-label={`${n.label}: ${live ? STATE_WORDS[v.state] : 'step'}${v.badges.length ? ', ' + v.badges.map((b) => b.text).join(', ') : ''}`}
                onClick={() => setPicked(picked === n.id ? null : n.id)}
                onKeyDown={(ev) => (ev.key === 'Enter' || ev.key === ' ') && (ev.preventDefault(), setPicked(picked === n.id ? null : n.id))}
                className="cursor-pointer outline-none focus-visible:[&>rect]:stroke-[3]">
                <rect width={W} height={H} rx="10" strokeWidth={picked === n.id ? 3 : 1.5} className={`${FILL[live ? v.state : 'pending']} ${v.state === 'active' ? 'motion-safe:animate-pulse' : ''}`} />
                {lines.map((l, i) => (
                  <text key={i} x={W / 2} y={(lines.length === 1 ? 28 : 22) + i * 15} textAnchor="middle" fontSize="15" fontWeight="600" className="fill-fg">{l}</text>
                ))}
                {live && v.badges.length > 0 && (
                  <text x={W / 2} y={H - 24} textAnchor="middle" fontSize="11.5" className={v.badges.some((b) => b.warn) ? 'fill-warn' : 'fill-muted'}>
                    {v.badges.map((b) => b.text).join(' · ').slice(0, 30)}
                  </text>
                )}
                {live && (v.models.length > 0 || v.sources.length > 0) && (
                  <text x={W / 2} y={H - 8} textAnchor="middle" fontSize="10.5" className="fill-info">
                    {(() => { const t = [...v.models, ...v.sources].join(' · '); return t.length > 30 ? t.slice(0, 29) + '…' : t })()}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      </Card>
      {live && (
        <p className="flex flex-wrap gap-2 text-xs text-muted" aria-hidden="true">
          <Badge tone="ok">done</Badge><Badge tone="info">working now</Badge><Badge tone="warn">waiting for you</Badge><Badge tone="bad">failed</Badge>
          <span>Dashed lines are decisions. Click a step to see what it does{live ? ' and what it has done in this run' : ''}.</span>
          {harness.data.control_note && <span>{harness.data.control_note}</span>}
          {(harness.data.problems ?? []).length > 0 && <span>The chart's description is out of step with the running code (restart the server to match): {(harness.data.problems ?? []).join('; ')}</span>}
        </p>
      )}
      {selected ? (
        <Card>
          <h3 className="font-semibold">{selected.label}{live && <> <Badge tone={{ done: 'ok', active: 'info', waiting: 'warn', failed: 'bad', pending: 'neutral' }[view[selected.id].state] as 'ok'}>{STATE_WORDS[view[selected.id].state]}</Badge></>}</h3>
          <p className="mt-1 text-sm">{selected.does}</p>
          {live && (view[selected.id].models.length > 0 || view[selected.id].sources.length > 0) && (
            <p className="mt-2 text-sm">
              {view[selected.id].models.length > 0 && <><strong>Language model:</strong> {view[selected.id].models.join(', ')}. </>}
              {view[selected.id].sources.length > 0 && <><strong>Search engine or data source:</strong> {view[selected.id].sources.join(', ')}.</>}
            </p>
          )}
          {live && view[selected.id].events.length > 0 && (
            <ul className="mt-2 flex flex-col gap-1 border-t border-line pt-2 text-sm">
              {view[selected.id].events.slice(-12).map((e) => {
                const d = describeEvent(e)
                return <li key={e.event_id} className="flex flex-wrap items-baseline gap-2"><Badge tone={d.tone}>{d.label}</Badge>{d.detail && <span className="min-w-0 break-words">{d.detail}</span>}</li>
              })}
            </ul>
          )}
        </Card>
      ) : (
        !live && <p className="text-sm text-muted">Click a step to see what it does.</p>
      )}
    </section>
  )
}

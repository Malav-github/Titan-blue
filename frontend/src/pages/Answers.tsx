// The quiet side of Titan: questions and their answers, with citations and a Word copy. How each answer was
// found (every search, page and check) stays on the Runs page; this page only links there.
import type { ReactElement } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, type AnswerView, type RunSummary, type StatementTableView } from '../api'
import { StatementTable } from '../StatementTable'
import { time } from '../format'
import { Badge, Button, Card, EmptyState, ErrorNote, ExternalLink, Loading, PageHeader } from '../ui'

const WORKING = new Set(['queued', 'running', 'pausing', 'stopping'])
const NEEDS_YOU = new Set(['awaiting_approval', 'needs_capability', 'needs_source', 'paused'])
const DONE = new Set(['finished', 'stopped'])

function stateOf(status: string | null | undefined): { tone: 'ok' | 'warn' | 'bad' | 'info' | 'neutral'; label: string } {
  if (DONE.has(status ?? '')) return { tone: 'ok', label: 'Answer ready' }
  if (WORKING.has(status ?? '')) return { tone: 'info', label: 'Working on it' }
  if (NEEDS_YOU.has(status ?? '')) return { tone: 'warn', label: status === 'paused' ? 'Paused' : 'Needs you' }
  if (status === 'failed') return { tone: 'bad', label: 'Could not finish' }
  return { tone: 'neutral', label: status ?? 'Unknown' }
}

export function Answers() {
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: 3000 })
  const questions = (runs.data ?? []).filter((r: RunSummary) => r.kind === 'research' && r.question)
  return (
    <>
      <PageHeader
        title="Answers"
        subtitle="Your questions and Titan's answers, with the sources behind them. To see how an answer was found, open it in Runs."
        actions={<Link to="/apps/research-company" className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg hover:opacity-90">Ask a question</Link>}
      />
      {runs.isLoading && <Loading />}
      <ErrorNote error={runs.error} />
      {runs.data && questions.length === 0 && <EmptyState title="No questions yet">Ask one and its answer appears here.</EmptyState>}
      {questions.length > 0 && (
        <ul className="flex flex-col gap-2">
          {questions.map((r) => {
            const s = stateOf(r.status)
            return (
              <li key={r.run_id}>
                <Link to={`/answers/${r.run_id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-surface p-3 hover:bg-surface-2">
                  <span className="min-w-0 font-medium">{r.question}</span>
                  <span className="flex items-center gap-3 text-sm text-muted"><Badge tone={s.tone}>{s.label}</Badge>{time(r.started_at)}</span>
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

/** The answer's text: headings, bullet lines, statement tables and [n] citations linked to their source below. */
function AnswerText({ text, tables = [] }: { text: string; tables?: StatementTableView[] }) {
  const inline = (line: string, key: number) => line.split(/(\[\d+\])/g).map((part, i) => {
    const m = /^\[(\d+)\]$/.exec(part)
    return m
      ? <sup key={`${key}-${i}`}><a href={`#source-${m[1]}`} className="text-accent no-underline hover:underline" aria-label={`Source ${m[1]}`}>[{m[1]}]</a></sup>
      : part
  })
  const blocks: ReactElement[] = []
  let bullets: string[] = []
  const flush = () => {
    if (bullets.length) blocks.push(<ul key={`ul-${blocks.length}`} className="my-2 list-disc space-y-1 pl-6">{bullets.map((b, i) => <li key={i}>{inline(b, i)}</li>)}</ul>)
    bullets = []
  }
  text.split('\n').forEach((line, i) => {
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line)
    if (bullet) { bullets.push(bullet[1]); return }
    flush()
    const tm = /^\s*\[table:(\d+)\]\s*$/.exec(line)
    const table = tm ? tables[Number(tm[1]) - 1] : undefined
    const h = /^(#{1,3})\s+(.*)$/.exec(line)
    if (table) blocks.push(<StatementTable key={i} table={table} source={table.source_n
      ? <>Source <a href={`#source-${table.source_n}`} className="text-accent no-underline hover:underline">[{table.source_n}]</a>.</> : null} />)
    else if (tm) return
    else if (h) blocks.push(<h3 key={i} className={`mt-4 font-semibold ${h[1].length === 1 ? 'text-xl' : 'text-lg'}`}>{inline(h[2], i)}</h3>)
    else if (line.trim().endsWith(':') && line.length < 80) blocks.push(<h4 key={i} className="mt-4 font-semibold">{inline(line.trim(), i)}</h4>)
    else if (line.trim()) blocks.push(<p key={i} className="my-2">{inline(line, i)}</p>)
  })
  flush()
  return <div className="leading-relaxed">{blocks}</div>
}

function Sources({ view }: { view: AnswerView }) {
  return (
    <section aria-labelledby="sources-h">
      <h2 id="sources-h" className="mb-2 text-lg font-semibold">Sources</h2>
      <ol className="flex flex-col gap-3">
        {view.sources.map((s) => (
          <li key={s.n} id={`source-${s.n}`} className="scroll-mt-4 rounded-lg border border-line bg-surface p-3 target:border-accent">
            <p className="text-sm">
              <strong>[{s.n}]</strong> {s.label}{s.date ? <span className="text-muted">, dated {s.date}</span> : null}
            </p>
            {s.url && <p className="text-sm"><ExternalLink href={s.url}>{s.url}</ExternalLink></p>}
            <ul className="mt-2 flex flex-col gap-1">
              {(s.tables ?? []).map((t) => <li key={t} className="text-sm text-muted">Table: {t}</li>)}
              {s.quotes.map((q) => (
                <li key={q.claim_id} className="border-l-2 border-line pl-3 text-sm italic text-muted">
                  “{q.quote}”{q.caution && <span className="not-italic"> <Badge tone="warn">partly supported</Badge></span>}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </section>
  )
}

export function AnswerPage() {
  const { id = '' } = useParams()
  const q = useQuery({
    queryKey: ['answer', id],
    queryFn: () => api.answer(id),
    refetchInterval: (s) => (s.state.data && !DONE.has(s.state.data.status ?? '') && s.state.data.status !== 'failed' ? 2000 : false),
  })
  const save = useMutation({ mutationFn: () => api.downloadAnswer(id) })
  if (q.isLoading) return <Loading />
  if (q.error || !q.data) return <><PageHeader title="Answer not found" /><ErrorNote error={q.error} /></>
  const { status, question, answer: view } = q.data
  const s = stateOf(status)
  const runLink = <Link className="text-accent underline" to={`/runs/${id}`}>how this was found</Link>

  return (
    <>
      <PageHeader
        title={question ?? 'Answer'}
        subtitle={<>{view?.as_of ? <>As of {view.as_of} · </> : null}<Link className="text-accent underline" to="/answers">All answers</Link> · {runLink}</>}
        actions={view && <Button onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? 'Preparing…' : 'Download Word'}</Button>}
      />
      <ErrorNote error={save.error} />
      {!view && (
        <Card>
          <p className="flex items-center gap-2"><Badge tone={s.tone}>{s.label}</Badge></p>
          <p className="mt-2 text-sm">
            {WORKING.has(status ?? '') && 'Titan is researching this. The answer appears here when it is ready; this page updates by itself.'}
            {NEEDS_YOU.has(status ?? '') && <>Titan is waiting for you before it can go on. Open {<Link className="text-accent underline" to={`/runs/${id}`}>the run</Link>} to decide.</>}
            {status === 'failed' && <>The run stopped with an error. The details are in {runLink}.</>}
          </p>
        </Card>
      )}
      {view && (
        <div className="flex flex-col gap-6">
          <Card>
            {view.outcome !== 'answered' && view.outcome_text && <p className="mb-2"><Badge tone="warn">{view.outcome_text}</Badge></p>}
            {view.answer ? <AnswerText text={view.answer} tables={view.tables ?? []} /> : <p className="text-sm text-muted">No answer could be established from sources Titan could read and quote.</p>}
            {view.notes.length > 0 && (
              <ul className="mt-4 list-disc border-t border-line pl-5 pt-3 text-sm text-muted">
                {view.notes.map((n) => <li key={n}>{n}</li>)}
              </ul>
            )}
          </Card>
          {view.sources.length > 0 && <Sources view={view} />}
          <p className="text-xs text-muted">Every quote above appears word for word in the archived copy of its source. For each search, page and check, see {runLink}.</p>
        </div>
      )}
    </>
  )
}

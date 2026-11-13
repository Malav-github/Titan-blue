// Verify: check a pasted text, a web page or a document against independent sources. A system of its own, apart from
// research runs; each check is still a run, traced step by step in Runs.
import { useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, type CheckRequest, type CheckRun, type CheckStatement } from '../api'
import { time } from '../format'
import { Badge, Button, Card, EmptyState, ErrorNote, ExternalLink, Loading, PageHeader } from '../ui'

type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral'
const VERDICT: Record<string, { tone: Tone; label: string; mark: string }> = {
  corroborated: { tone: 'ok', label: 'Corroborated', mark: 'bg-ok-bg' },
  partial: { tone: 'info', label: 'Partly corroborated', mark: 'bg-info-bg' },
  contradicted: { tone: 'bad', label: 'Contradicted', mark: 'bg-bad-bg' },
  unconfirmed: { tone: 'warn', label: 'Unconfirmed', mark: 'bg-warn-bg' },
}
const WORKING = new Set(['queued', 'running', 'pausing', 'stopping'])

const TABS = [
  { id: 'text', label: 'Paste text' },
  { id: 'url', label: 'Web link' },
  { id: 'file', label: 'Upload a document' },
] as const

export function VerifyPage() {
  const navigate = useNavigate()
  const handed = (useLocation().state as { text?: string } | null)?.text ?? ''   // 'Verify this answer' from Answers
  const [tab, setTab] = useState<CheckRequest['kind']>('text')
  const [text, setText] = useState(handed)
  const [url, setUrl] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [subject, setSubject] = useState('')
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: 5000 })
  const checks = (runs.data ?? []).filter((r) => r.kind === 'check')

  const start = useMutation({
    mutationFn: async () => {
      const body: CheckRequest = { kind: tab, subject: subject.trim() || undefined }
      if (tab === 'text') body.text = text
      if (tab === 'url') body.url = url.trim()
      if (tab === 'file') {
        if (!file) throw new Error('Choose a document to upload')
        body.upload_id = (await api.upload(file)).upload_id
      }
      return api.startCheck(body)
    },
    onSuccess: (r) => navigate(`/verify/${r.run_id}`),
  })
  const ready = tab === 'text' ? text.trim().length > 0 : tab === 'url' ? /^https?:\/\/\S+$/.test(url.trim()) : !!file
  const submit = (e: FormEvent) => { e.preventDefault(); if (ready) start.mutate() }

  return (
    <>
      <PageHeader title="Verify"
        subtitle="Check a text, a web page or a document against independent sources. Each statement with a figure, a date, a role or an event is checked on its own; forecasts and opinions are set aside. Your text, its own site and copies of it never count as evidence." />
      <Card className="max-w-3xl">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div role="tablist" aria-label="What to check" className="flex flex-wrap gap-1">
            {TABS.map((t) => (
              <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}
                className={`rounded-md px-3 py-1.5 text-sm ${tab === t.id ? 'bg-accent text-accent-fg' : 'border border-line hover:bg-surface-2'}`}>
                {t.label}
              </button>
            ))}
          </div>
          {tab === 'text' && (
            <div>
              <label htmlFor="check-text" className="text-sm font-medium">Text to check</label>
              <textarea id="check-text" rows={10} value={text} onChange={(e) => setText(e.target.value)}
                placeholder="Paste an analyst note, an email, a press release, another assistant's answer…"
                className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm" />
            </div>
          )}
          {tab === 'url' && (
            <div>
              <label htmlFor="check-url" className="text-sm font-medium">Web address</label>
              <input id="check-url" type="url" placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)}
                className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm" />
              <p className="mt-1 text-xs text-muted">WrAIph reads the page and keeps a copy of it, so the check can be traced back to exactly what it read.</p>
            </div>
          )}
          {tab === 'file' && (
            <div>
              <label htmlFor="check-file" className="text-sm font-medium">Document (PDF, Word saved as HTML, text; up to 20 MB)</label>
              <input id="check-file" type="file" accept=".pdf,.txt,.htm,.html,.md" onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="mt-1 block w-full text-sm" />
            </div>
          )}
          <div>
            <label htmlFor="check-subject" className="text-sm font-medium">Who is it about? <span className="font-normal text-muted">(optional)</span></label>
            <input id="check-subject" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="For example: S&P Global"
              className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm" />
            <p className="mt-1 text-xs text-muted">Left empty, WrAIph takes the organisation the text names most.</p>
          </div>
          <ErrorNote error={start.error} />
          <div><Button type="submit" disabled={!ready || start.isPending}>{start.isPending ? 'Starting…' : 'Check it'}</Button></div>
        </form>
      </Card>

      <h2 className="mb-2 mt-8 text-lg font-semibold">Earlier checks</h2>
      {runs.isLoading && <Loading />}
      {runs.data && checks.length === 0 && <EmptyState title="Nothing checked yet" />}
      <ul className="flex flex-col gap-2">
        {checks.map((r) => (
          <li key={r.run_id}>
            <Link to={`/verify/${r.run_id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-surface p-3 hover:bg-surface-2">
              <span className="min-w-0 truncate font-medium">{(r.question ?? '').replace(/^Verify: /, '')}</span>
              <span className="text-sm text-muted">{WORKING.has(r.status ?? '') ? <Badge tone="info">Checking</Badge> : null} {time(r.started_at)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  )
}

/** The text with each statement marked by its verdict; a click goes to the statement's evidence. */
function MarkedText({ run }: { run: CheckRun }) {
  const placed = run.statements.filter((s) => s.span).sort((a, b) => a.span![0] - b.span![0])
  const parts: ReactNode[] = []
  let at = 0
  for (const s of placed) {
    const [a, b] = s.span!
    if (a < at) continue
    if (a > at) parts.push(<span key={`t${at}`}>{run.text.slice(at, a)}</span>)
    const v = s.outcome ? VERDICT[s.outcome] : null
    const cls = v ? v.mark : s.checkable ? 'underline decoration-dotted decoration-info' : 'text-muted'
    parts.push(s.checkable
      ? <a key={`s${s.n}`} href={`#st-${s.n}`} className={`rounded px-0.5 text-fg no-underline ${cls}`} title={v?.label ?? 'being checked'}>{run.text.slice(a, b)}</a>
      : <span key={`s${s.n}`} className={cls} title={s.why}>{run.text.slice(a, b)}</span>)
    at = b
  }
  parts.push(<span key="end">{run.text.slice(at)}</span>)
  return <div className="whitespace-pre-wrap text-sm leading-relaxed">{parts}</div>
}

function Evidence({ s }: { s: CheckStatement }) {
  const lines: ReactNode[] = []
  for (const [i, c] of (s.checks ?? []).entries()) {
    if (c.check === 'role_holder' && c.quote) {
      lines.push(<li key={i}><strong>Who holds the role, from the company’s filing</strong>: {c.detail} <q className="italic">{c.quote}</q> {c.url && <ExternalLink href={c.url}>{c.url}</ExternalLink>}</li>)
    } else if (c.check === 'role_holder') {
      lines.push(<li key={i} className="text-muted">Not checked against filings: {c.detail}</li>)
    } else if (c.check === 'name') {
      lines.push(<li key={i} className={c.result === 'another person' ? 'text-bad' : ''}><strong>Name</strong>: {c.result}{c.detail ? `: ${c.detail}` : ''}</li>)
    } else if (c.check === 'role_event' && c.quote) {
      lines.push(<li key={i}><strong>{c.detail}</strong>: <q className="italic">{c.quote}</q> {c.url && <ExternalLink href={c.url}>{c.url}</ExternalLink>}</li>)
    } else if (c.check === 'role_event') {
      lines.push(<li key={i} className="text-muted">{c.detail}.</li>)
    } else if (c.check === 'reason') {
      lines.push(<li key={i} className="text-warn"><strong>Reason</strong>: {c.detail}.</li>)
    } else if (c.check === 'second_source' && c.result === 'found') {
      lines.push(<li key={i}><strong>Independent source</strong>{c.tier_label ? ` (${c.tier_label})` : ''}: <q className="italic">{c.quote}</q> {c.url && <ExternalLink href={c.url}>{c.url}</ExternalLink>}</li>)
    } else if (c.check === 'second_source') {
      lines.push(<li key={i} className="text-muted">No independent source found{c.detail ? ` (${c.detail})` : ''}.</li>)
    } else if (c.check === 'contradiction' && c.result === 'found') {
      lines.push(<li key={i} className="text-bad"><strong>Contradicting source</strong>: <q className="italic">{c.quote}</q> {c.url && <ExternalLink href={c.url}>{c.url}</ExternalLink>}{c.detail && <span className="block">{c.detail}</span>}</li>)
    } else if (c.check === 'figures' && !['no statements read', 'no figures'].includes(c.result)) {
      lines.push(<li key={i}><strong>Against the company’s financial statements</strong>: {c.result}; {c.detail}</li>)
    } else if (c.check === 'newer' && (c.items ?? []).length > 0) {
      lines.push(<li key={i} className="text-muted">Newer reports to look at: {(c.items ?? []).slice(0, 3).map((it, j) => <span key={j}>{j ? '; ' : ''}<ExternalLink href={it.url}>{it.title || it.url}</ExternalLink></span>)}</li>)
    } else if (c.check === 'source' && ['quote gone', 'unreachable'].includes(c.result)) {
      lines.push(<li key={i} className="text-warn">The page now: {c.result}.</li>)
    } else if (c.check === 'error') {
      lines.push(<li key={i} className="text-bad">Could not be checked: {c.result}</li>)
    }
  }
  return <ul className="mt-2 flex flex-col gap-1 text-sm">{lines}</ul>
}

export function CheckPage() {
  const { id = '' } = useParams()
  const q = useQuery({
    queryKey: ['check', id], queryFn: () => api.check(id),
    refetchInterval: (s) => (s.state.data && (WORKING.has(s.state.data.status ?? '') || s.state.data.status == null) ? 2000 : false),
  })
  const save = useMutation({ mutationFn: () => api.downloadCheck(id) })
  const stop = useMutation({ mutationFn: () => api.stopRun(id), onSuccess: () => q.refetch() })
  if (q.isLoading) return <Loading />
  if (q.error || !q.data) return <><PageHeader title="Check not found" /><ErrorNote error={q.error} /></>
  const run = q.data
  const working = WORKING.has(run.status ?? '')
  const checked = run.statements.filter((s) => s.checkable)
  const skipped = run.statements.filter((s) => !s.checkable)
  const c = run.counts
  return (
    <>
      <PageHeader title={run.title.replace(/^Verify: /, 'Verify: ')}
        subtitle={<>{run.subject ? <>About <strong>{run.subject}</strong> · </> : null}
          {run.input?.url && !run.input.url.startsWith('upload:') ? <><ExternalLink href={run.input.url}>{run.input.url}</ExternalLink> · </> : null}
          <Link className="text-accent underline" to="/verify">All checks</Link> · <Link className="text-accent underline" to={`/runs/${id}`}>how this was checked</Link></>}
        actions={<span className="flex flex-wrap items-center gap-2">
          {working && <Button variant="danger" disabled={stop.isPending} onClick={() => stop.mutate()}>Stop</Button>}
          {run.statements.length > 0 && !working && <Button onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? 'Preparing…' : 'Download Word'}</Button>}
        </span>}
      />
      <ErrorNote error={save.error ?? stop.error} />
      {run.status === 'failed' && <div role="alert" className="mb-4 rounded-md border border-bad bg-bad-bg p-3 text-sm text-bad">The check could not run: {run.error}</div>}
      {run.statements.length === 0 && working && <Loading what="Reading what you gave and finding the statements to check" />}
      {run.statements.length > 0 && (
        <div className="flex flex-col gap-6">
          <p className="flex flex-wrap items-center gap-2 text-sm" aria-live="polite">
            <Badge tone="ok">{c.corroborated} corroborated</Badge>
            {c.partial > 0 && <Badge tone="info">{c.partial} partly corroborated</Badge>}
            <Badge tone="bad">{c.contradicted} contradicted</Badge>
            <Badge tone="warn">{c.unconfirmed} unconfirmed</Badge><Badge>{c.not_checkable} not checkable</Badge>
            {c.waiting > 0 && <Badge tone="info">{c.waiting} still being checked</Badge>}
          </p>
          <Card><MarkedText run={run} /></Card>
          <section aria-labelledby="checked-h">
            <h2 id="checked-h" className="mb-2 text-lg font-semibold">Statements checked ({checked.length})</h2>
            <ol className="flex flex-col gap-3">
              {checked.map((s) => {
                const v = s.outcome ? VERDICT[s.outcome] : null
                return (
                  <li key={s.n} id={`st-${s.n}`} className="scroll-mt-4 rounded-lg border border-line bg-surface p-3 target:border-accent">
                    <p className="flex flex-wrap items-start gap-2">
                      {v ? <Badge tone={v.tone}>{v.label}</Badge> : <Badge tone="info">Checking…</Badge>}
                      <q className="min-w-0 flex-1 italic">{s.text}</q>
                    </p>
                    {(s.notes ?? []).length > 0 && (
                      <ul className="mt-2 flex flex-col gap-1 rounded-md bg-warn-bg p-2 text-sm text-warn">
                        {(s.notes ?? []).map((n) => <li key={n}>{n}</li>)}
                      </ul>
                    )}
                    {s.checks && <Evidence s={s} />}
                  </li>
                )
              })}
            </ol>
          </section>
          {skipped.length > 0 && (
            <details className="rounded-lg border border-line p-3 text-sm">
              <summary className="cursor-pointer font-medium">Not checked ({skipped.length})</summary>
              <ul className="mt-2 flex flex-col gap-1">
                {skipped.map((s) => <li key={s.n}><q className="italic">{s.text}</q> <span className="text-muted">— {s.why}</span></li>)}
              </ul>
            </details>
          )}
          <p className="text-xs text-muted">
            Corroborated: an independent source states the same, or the company’s own filings do. Partly corroborated: what happened
            is confirmed, a part of the statement (such as the reason given) is not. Contradicted: a source gives another figure or
            person, or the company’s own filings differ. Unconfirmed: no independent source was found, which is not the same as wrong.
          </p>
        </div>
      )}
    </>
  )
}

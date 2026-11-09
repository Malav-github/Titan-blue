import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, pageText, type Approval, type FigureCheck, type Report, type ReportClaim, type RunDetail, type RunEvent } from '../api'
import { ACTIVE, describeEvent, lines, time } from '../format'
import { Flow } from '../Flow'
import { useRunEvents } from '../useRunEvents'
import { useArchive, useLinks } from '../scope'
import { Badge, Button, Card, ErrorNote, ExternalLink, Loading, Mono, PageHeader, StatusBadge, TierBadge, timeChips } from '../ui'

function ClaimCard({ claim, caution }: { claim: ReportClaim; caution?: boolean }) {
  const links = useLinks()
  return (
    <li className={`rounded-lg border bg-surface p-4 ${caution ? 'border-warn' : 'border-line'}`}>
      {claim.claim === claim.quote
        ? <blockquote className="border-l-4 border-accent pl-3 font-medium">“{claim.quote}”</blockquote>
        : <><p className="font-medium">{claim.claim}</p>
            <blockquote className="mt-2 border-l-4 border-line pl-3 text-sm italic text-muted">“{claim.quote}”</blockquote></>}
      <p className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <TierBadge tier={claim.source_tier} />
        {claim.page && <Badge tone="info">{pageText(claim.page)}</Badge>}
        {claim.source_label && <span className="text-muted">{claim.source_label}</span>}
        <Badge tone={claim.source_date ? 'neutral' : 'warn'}>
          {claim.source_date ? `Dated ${claim.source_date.slice(0, 10)}${claim.source_date_kind ? ` (${claim.source_date_kind.replace('_', ' ')})` : ''}` : 'No date found'}
        </Badge>
        {claim.corroboration && <Badge tone={claim.corroboration === 'sources disagree' ? 'bad' : claim.corroboration === 'single source, unconfirmed' ? 'warn' : 'ok'}>{claim.corroboration}</Badge>}
        <ExternalLink href={claim.source_url}>{claim.source_url}</ExternalLink>
      </p>
      {(claim.copies ?? []).length > 0 && <p className="mt-1 text-xs text-muted">The same document is also on {(claim.copies ?? []).join(', ')}: counted as one source, not several.</p>}
      {(claim.figures ?? []).length > 0 && <Figures checks={claim.figures ?? []} label={claim.figures_label} />}
      {timeChips(claim.times).length > 0 && (
        <p className="mt-2 flex flex-wrap gap-2">{timeChips(claim.times).map((c, i) => <Badge key={i} tone={c.tone}>{c.text}</Badge>)}</p>
      )}
      {claim.why && <p className="mt-2 text-sm text-muted">Why: {claim.why}</p>}
      <p className="mt-2 text-sm"><Link className="text-accent underline" to={links.claim(claim.claim_id)}>Open claim #{claim.claim_id} with the quote in context</Link></p>
    </li>
  )
}

const FIGURE_WORDS: Record<FigureCheck['verdict'], [string, 'ok' | 'bad' | 'warn' | 'neutral']> = {
  matches: ['matches the statements', 'ok'],
  found: ['equals a statement figure', 'ok'],
  matches_doubtful: ['matches a figure in doubt: double-check', 'warn'],
  differs: ['differs from the statements', 'bad'],
  not_covered: ['not in the statements read', 'neutral'],
  not_found: ['no statement figure equals it', 'neutral'],
  not_compared: ['not compared', 'neutral'],
}

/** Each figure the quote states, checked by code against the company's own financial statements. */
function Figures({ checks, label }: { checks: FigureCheck[]; label?: string }) {
  return (
    <div className="mt-2 text-sm">
      <p className="font-medium">Figures against the company's statements{label ? `: ${label}` : ''}</p>
      <ul className="mt-1 flex flex-col gap-1">
        {checks.map((f, i) => {
          const [words, tone] = FIGURE_WORDS[f.verdict]
          return (
            <li key={i} className="flex flex-wrap items-baseline gap-2">
              <Mono>{f.mention.raw}</Mono>
              <span className="text-muted">{f.mention.phrase}{f.mention.periods.length ? `, ${f.mention.periods.join(', ')}` : f.mention.as_of ? `, at ${f.mention.as_of}` : ''}</span>
              <Badge tone={tone}>{words}</Badge>
              <span className="text-muted">{f.why}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** The written answer, with each [claim:N] marker turned into a link to that claim. */
function Answer({ text }: { text: string }) {
  const links = useLinks()
  return <>{text.split(/(\[claim:\d+\])/g).map((part, i) => {
    const m = /^\[claim:(\d+)\]$/.exec(part)
    return m ? <Link key={i} className="text-accent underline" to={links.claim(m[1])}>[claim {m[1]}]</Link> : part
  })}</>
}

function ReportView({ report }: { report: Report }) {
  return (
    <section aria-labelledby="report-h" className="flex flex-col gap-4">
      <h2 id="report-h" className="text-xl font-semibold">Result</h2>
      {report.time && (
        <p className="text-sm text-muted">
          As of <strong>{report.time.today}</strong>
          {report.time.scope ? <> · time scope: {report.time.scope.replace('_', ' ')}</> : null}
          {report.time.newest_source_date ? <> · newest source dated {report.time.newest_source_date.slice(0, 10)}{report.time.age_days !== null ? ` (${report.time.age_days} days old)` : ''}</> : <> · no source carries a date</>}
        </p>
      )}
      {report.time?.stale && (
        <div role="alert" className="rounded-md border border-warn bg-warn-bg p-3 text-sm text-warn">
          This question asks for the current state, but the sources behind the answer are old or undated. Treat it as possibly out of date.
        </div>
      )}
      {(report.conflicts ?? []).length > 0 && (
        <div role="alert" className="rounded-md border border-bad bg-bad-bg p-3 text-sm text-bad">
          Sources disagree: {(report.conflicts ?? []).map((c) => c.note).join('; ')}. Both are shown below; neither was chosen for you.
        </div>
      )}
      {(report.outcome === 'premise_unverified' || report.outcome === 'not_found') && (
        <div role="status" className="rounded-md border border-warn bg-warn-bg p-3 text-sm">
          <strong>{report.outcome === 'premise_unverified'
            ? 'No evidence was found that what the question assumes actually happened, so the research was not carried out.'
            : 'There was not enough information to answer this question.'}</strong>
          {report.outcome === 'premise_unverified' && (report.premise ?? []).length > 0 && (
            <ul className="mt-1 list-disc pl-5">{(report.premise ?? []).map((p) => <li key={p.text}>Not established: {p.text}</li>)}</ul>
          )}
          <span className="mt-1 block">Nothing here is a statement that it did not happen: it is that no source could be read and quoted for it. A source from you would let me look again.</span>
        </div>
      )}
      {report.summary ? (
        <Card><p className="whitespace-pre-line"><Answer text={report.summary} /></p>
          <p className="mt-2 text-xs text-muted">Written by a model from the verified claims below only; each [claim:N] points to one of them.</p>
          {report.quality?.score != null && (
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer">
                Writing score by the evaluation matrix: <strong>{report.quality.score}/100 ({report.quality.grade})</strong>
                {report.quality.blockers.length > 0 && <span className="text-bad"> · capped by {report.quality.blockers.map((b) => b.id.replace(/_/g, ' ')).join(', ')}</span>}
                {(report.quality.revisions ?? 0) > 0 && <span className="text-muted"> · written again once</span>}
              </summary>
              <table className="mt-2 w-full text-left text-xs">
                <thead><tr className="text-muted"><th>Dimension</th><th>Points (0-5)</th><th>What the checks found</th></tr></thead>
                <tbody>{Object.entries(report.quality.dimensions).map(([id, d]) => (
                  <tr key={id} className="align-top"><td className="pr-3">{d.label}</td><td className="pr-3">{d.points ?? '-'}</td>
                    <td>{d.checks.filter((c) => c.score !== null && c.score < 1).map((c) => c.detail).join('; ') || 'nothing to correct'}</td></tr>
                ))}</tbody>
              </table>
              {(report.quality.uncited_removed ?? []).length > 0 && (
                <p className="mt-2 text-xs text-warn">Removed because no claim backs it: {(report.quality.uncited_removed ?? []).map((s) => `“${s}”`).join(' ')}</p>
              )}
            </details>
          )}
        </Card>
      ) : (
        report.outcome === 'premise_unverified' || report.outcome === 'not_found' ? null :
          <Card><p className="text-sm text-muted">No answer was written: nothing was established strongly enough.</p></Card>
      )}
      {report.list_question && (
        <p className="text-sm text-muted">A list question: Titan collected every distinct event it could read ({report.events_found ?? 0} documents with established claims) for “{report.list_question}”. The list covers what was found, not everything that happened.</p>
      )}
      {(report.diagnosis ?? []).length > 0 && (
        <details className="rounded-lg border border-line bg-surface p-3 text-sm">
          <summary className="cursor-pointer font-medium">Why parts of this run failed ({(report.diagnosis ?? []).reduce((n, d) => n + d.count, 0)}), and what covers for it</summary>
          <table className="mt-2 w-full text-left text-xs">
            <thead><tr className="text-muted"><th className="pr-2">Kind</th><th className="pr-2">What happened</th><th className="pr-2">Times</th><th className="pr-2">What Titan does</th><th>Could be done next</th></tr></thead>
            <tbody>{(report.diagnosis ?? []).map((d) => (
              <tr key={d.id} className="align-top">
                <td className="pr-2"><Badge tone={d.kind === 'llm' ? 'warn' : d.kind === 'design' ? 'neutral' : 'bad'}>{d.kind_label}</Badge></td>
                <td className="pr-2">{d.label}{d.examples.length > 0 && <span className="block text-muted">e.g. {d.examples[0]}</span>}</td>
                <td className="pr-2">{d.count}</td><td className="pr-2">{d.handled_by}</td><td>{d.workaround}</td>
              </tr>
            ))}</tbody>
          </table>
        </details>
      )}
      <div>
        <h3 className="mb-2 font-semibold">Established ({report.established.length})</h3>
        {report.established.length === 0 ? <p className="text-sm text-muted">No claim passed every check.</p> :
          <ul className="flex flex-col gap-3">{report.established.map((c) => <ClaimCard key={c.claim_id} claim={c} />)}</ul>}
      </div>
      {(report.context ?? []).length > 0 && (
        <div>
          <h3 className="mb-1 font-semibold">Context ({(report.context ?? []).length})</h3>
          <p className="mb-2 text-sm text-muted">Verified, and not the fact asked for, but material to it: terms, parties, reasons, circumstances. The answer may use them.</p>
          <ul className="flex flex-col gap-3">{(report.context ?? []).map((c) => <ClaimCard key={c.claim_id} claim={c} />)}</ul>
        </div>
      )}
      {(report.reference_only ?? []).length > 0 && (
        <p className="text-sm text-muted">{(report.reference_only ?? []).length} verified claim(s) came from a reference work such as Wikipedia. That is for general knowledge, not for a company's news, so they are kept on record but not used in the answer: {(report.reference_only ?? []).map((c) => `#${c.claim_id}`).join(', ')}.</p>
      )}
      {report.partial.length > 0 && (
        <div>
          <h3 className="mb-2 font-semibold">Partly supported: treat with caution ({report.partial.length})</h3>
          <ul className="flex flex-col gap-3">{report.partial.map((c) => <ClaimCard key={c.claim_id} claim={c} caution />)}</ul>
        </div>
      )}
      {report.gaps.length > 0 && (
        <Card>
          <h3 className="font-semibold">Not established</h3>
          <ul className="mt-1 list-disc pl-5 text-sm">{report.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
        </Card>
      )}
      <p className="text-sm text-muted">
        Also: {report.not_supported} claims were not supported by their quote, {report.off_topic} were true but off topic,
        {' '}{report.unchecked} were not checked, and {report.rejected_quotes} quotes were rejected because they were not in the page.
        Used {report.budget_used.searches} searches and {report.budget_used.fetches} pages in {report.budget_used.iterations} rounds.
      </p>
      {report.warnings.length > 0 && <ul className="list-disc pl-5 text-sm text-warn">{report.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
    </section>
  )
}

function ApprovalPanel({ runId, plan }: { runId: string; plan: Approval }) {
  const qc = useQueryClient()
  const [queries, setQueries] = useState(plan.queries.join('\n'))
  const [checklist, setChecklist] = useState(plan.checklist.join('\n'))
  const [comment, setComment] = useState('')
  const [url, setUrl] = useState('')
  type Answer = { approved: boolean; override?: boolean; comment?: string; url?: string }
  const decide = useMutation({
    mutationFn: (a: Answer) => api.decide(runId, { ...a, queries: lines(queries), checklist: lines(checklist) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['run', runId] }),
  })
  const premise = plan.premise
  const unverified = premise?.status === 'unverified'
  const screen = premise?.screen
  const busy = decide.isPending
  return (
    <Card className="border-warn">
      <h2 className="text-lg font-semibold">{unverified ? 'The agent could not verify what your question assumes' : 'Your decision on the plan'}</h2>
      {(plan.corrections ?? []).length > 0 && (
        <p className="mt-1 text-sm text-muted">Rebuilt after your correction{(plan.corrections ?? []).length > 1 ? 's' : ''}: {(plan.corrections ?? []).join('; ')}</p>
      )}
      {(plan.sense ?? []).length > 0 && (
        <div role="status" className="mt-3 rounded-md border border-warn bg-warn-bg p-3 text-sm">
          <strong>Common-sense check</strong>
          <ul className="mt-1 list-disc pl-5">{(plan.sense ?? []).map((c) => <li key={c.id}>{c.text}</li>)}</ul>
        </div>
      )}
      {premise && premise.items.length > 0 && (
        <div role="status" className={`mt-3 rounded-md border p-3 text-sm ${unverified ? 'border-warn bg-warn-bg' : premise.status === 'reported' ? 'border-line bg-surface-2/50' : 'border-ok bg-ok-bg'}`}>
          <strong>{unverified ? 'A quick search found nothing relevant that reports this:' : premise.status === 'reported' ? 'Your question assumes this. A quick search reports something relevant (not yet read):' : 'Checked from the source you gave, and confirmed:'}</strong>
          <ul className="mt-1 list-disc pl-5">{premise.items.map((p) => <li key={p}>{p}</li>)}</ul>
          {!unverified && premise.claims.length > 0 && (
            <ul className="mt-1 flex flex-col gap-1">
              {premise.claims.map((c) => (
                <li key={c.claim_id}>{c.claim} <Link className="text-accent underline" to={`/evidence/${c.claim_id}`}>claim {c.claim_id}</Link></li>
              ))}
            </ul>
          )}
          {screen && (screen.results ?? []).length > 0 && (
            <details className="mt-2" open={unverified}>
              <summary className="cursor-pointer">The top {(screen.results ?? []).length} results for “{screen.query}”{(screen.providers ?? []).length > 0 && <span className="text-muted"> via {(screen.providers ?? []).join(', ')}</span>}</summary>
              <ol className="mt-1 list-decimal pl-5">
                {(screen.results ?? []).map((r) => (
                  <li key={r.n} className={r.reports_it ? 'font-medium' : 'text-muted'}>
                    <a className="underline" href={r.url} target="_blank" rel="noreferrer">{r.title || r.url}</a> ({r.site}{r.published ? `, ${r.published}` : ''}){r.reports_it ? ' reports it' : ''}
                  </li>
                ))}
              </ol>
              {screen.reason && <p className="mt-1 text-muted">{screen.reason}</p>}
            </details>
          )}
          {unverified && <p className="mt-1">This is not a statement that it did not happen, only that the top results do not report it. You can accept this, correct me, or give me a source.</p>}
        </div>
      )}
      {plan.advice && (plan.advice.suggestion || plan.advice.sources.length > 0) && (
        <div className="mt-3 rounded-md border border-line bg-surface-2/50 p-3 text-sm">
          <strong>From a quick look at the web</strong>
          <span className="text-muted"> (unverified: a guide to where to look, never evidence)</span>
          {plan.advice.suggestion && (
            <p className="mt-2">
              {plan.advice.suggestion}{' '}
              <Button type="button" variant="secondary" className="ml-1" disabled={busy}
                onClick={() => { setComment(`Please use this: ${plan.advice?.suggestion}`); document.getElementById('plan-comment')?.focus() }}>
                Use this suggestion
              </Button>
            </p>
          )}
          {plan.advice.sources.length > 0 && (
            <>
              <p className="mt-2 font-medium">Places to look first, best first</p>
              <ol className="mt-1 list-decimal pl-5">
                {plan.advice.sources.map((a) => <li key={a.site}><strong>{a.site}</strong>: {a.why}</li>)}
              </ol>
            </>
          )}
        </div>
      )}
      {plan.brief && (plan.brief.places.length > 0 || plan.brief.documents.length > 0) && (
        <div className="mt-3 rounded-md border border-line bg-surface-2/50 p-3 text-sm">
          <strong>Planning brief</strong>
          <span className="text-muted"> ({plan.brief.kinds.join('; ') || 'general'})</span>
          <p className="mt-2"><strong>Target time:</strong> {plan.brief.target}</p>
          {plan.brief.places.length > 0 && (
            <ol className="mt-2 list-decimal pl-5">
              {plan.brief.places.map((p) => (
                <li key={p.site}><strong>{p.site}</strong> <span className="text-muted">({p.role === 'keeps_original' ? 'unchanged copy' : p.role})</span>: {p.why}</li>
              ))}
            </ol>
          )}
          {plan.brief.documents.map((d) => (
            <p key={d.id} className="mt-2"><strong>{d.id}</strong> ({d.label}): {d.expect}</p>
          ))}
          {plan.brief.metadata.length > 0 && <p className="mt-2 text-muted">Expect on the source: {plan.brief.metadata.join(', ')}</p>}
        </div>
      )}
      <p className="mt-3 text-sm text-muted">
        If you go ahead, Titan will run these searches and then read pages looking for the facts listed. You can edit either list, one item per line.
        {plan.page_categories.length > 0 && <> It will also look for the company’s own pages about: {plan.page_categories.join(', ')}.</>}
      </p>
      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div>
          <label htmlFor="plan-queries" className="text-sm font-medium">Searches</label>
          <textarea id="plan-queries" rows={5} className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm"
            value={queries} onChange={(e) => setQueries(e.target.value)} />
        </div>
        <div>
          <label htmlFor="plan-checklist" className="text-sm font-medium">Facts to establish</label>
          <textarea id="plan-checklist" rows={5} className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm"
            value={checklist} onChange={(e) => setChecklist(e.target.value)} />
        </div>
      </div>
      <div className="mt-4">
        <label htmlFor="plan-comment" className="text-sm font-medium">Correct the agent in your own words</label>
        <textarea id="plan-comment" rows={2} placeholder="For example: that should have been 2026" className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm"
          value={comment} onChange={(e) => setComment(e.target.value)} />
        <Button className="mt-2" variant="secondary" disabled={busy || !comment.trim()} onClick={() => decide.mutate({ approved: true, comment: comment.trim() })}>
          Send correction and rebuild the plan
        </Button>
      </div>
      {unverified && (
        <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); if (url.trim()) decide.mutate({ approved: true, url: url.trim() }) }}>
          <div className="min-w-64 flex-1">
            <label htmlFor="premise-url" className="block text-sm font-medium">Or give a source for it (web address)</label>
            <input id="premise-url" type="url" placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)}
              className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm" />
          </div>
          <Button type="submit" variant="secondary" disabled={busy || !url.trim()}>Read and check this source</Button>
        </form>
      )}
      <div className="mt-3"><ErrorNote error={decide.error} /></div>
      <div className="mt-3 flex flex-wrap gap-2">
        {unverified ? (
          <>
            <Button onClick={() => decide.mutate({ approved: true })} disabled={busy}>Accept: stop here</Button>
            <Button variant="secondary" onClick={() => decide.mutate({ approved: true, override: true })} disabled={busy}>Research anyway</Button>
          </>
        ) : (
          <Button onClick={() => decide.mutate({ approved: true })} disabled={busy}>Approve and run</Button>
        )}
        <Button variant="danger" onClick={() => decide.mutate({ approved: false })} disabled={busy}>Reject the run</Button>
      </div>
    </Card>
  )
}

/** The run could not establish something: ask for a source to read, or stop. What is added is checked like everything else. */
function SourcePanel({ run }: { run: RunDetail }) {
  const qc = useQueryClient()
  const [url, setUrl] = useState('')
  const ask = run.source_request
  const go = useMutation({
    mutationFn: (u: string | null) => api.giveSource(run.run_id, u),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['run', run.run_id] }),
  })
  const premise = ask?.stage === 'premise'
  return (
    <Card className="border-warn">
      <h2 className="text-lg font-semibold">{premise ? 'I could not find evidence that this happened' : 'I could not find the answer'}</h2>
      <p className="mt-1 text-sm">
        {premise ? 'The question assumes the following, and no source I could read and quote confirms it:' : 'These facts are still missing:'}
      </p>
      <ul className="mt-1 list-disc pl-5 text-sm">{(ask?.missing ?? []).map((m) => <li key={m}>{m}</li>)}</ul>
      <p className="mt-2 text-sm text-muted">
        If you have a source, give me its web address. I will read it and check it like everything else: the exact words must be on the page,
        and it will be marked as a source you supplied, never as independent or official.
      </p>
      <form className="mt-3 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); if (url.trim()) go.mutate(url.trim()) }}>
        <div className="min-w-64 flex-1">
          <label htmlFor="source-url" className="block text-sm font-medium">Web address of a source</label>
          <input id="source-url" type="url" placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)}
            className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm" />
        </div>
        <Button type="submit" disabled={go.isPending || !url.trim()}>Read this source</Button>
        <Button type="button" variant="danger" onClick={() => go.mutate(null)} disabled={go.isPending}>No source, stop here</Button>
      </form>
      <p className="mt-2 text-xs text-muted">Documents and data-source APIs can be added from the Data sources page; reading them inside a run comes next.</p>
      <div className="mt-3"><ErrorNote error={go.error} /></div>
    </Card>
  )
}

function CapabilityPanel({ run }: { run: RunDetail }) {
  const qc = useQueryClient()
  const connectors = useQuery({ queryKey: ['connectors'], queryFn: api.connectors })
  const [choice, setChoice] = useState('')
  const go = useMutation({
    mutationFn: (capability: string | null) => api.continueRun(run.run_id, capability),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['run', run.run_id] }),
  })
  const need = run.capability?.need ?? ''
  return (
    <Card className="border-warn">
      <h2 className="text-lg font-semibold">This run needs a data source</h2>
      <p className="mt-1 text-sm">{need}</p>
      <p className="mt-1 text-sm text-muted">{run.capability?.reason}</p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <Link className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg" to={`/apps/add-data-source?need=${encodeURIComponent(need)}`}>
          Find a data source for this
        </Link>
        <div>
          <label htmlFor="connector-choice" className="block text-sm font-medium">Use an active one</label>
          <select id="connector-choice" className="mt-1 rounded-md border border-line bg-surface px-2 py-1.5 text-sm" value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">Choose…</option>
            {connectors.data?.map((c) => <option key={c.connector_id} value={c.connector_id}>{c.name}</option>)}
          </select>
        </div>
        <Button variant="secondary" disabled={!choice || go.isPending} onClick={() => go.mutate(choice)}>Continue with it</Button>
        <Button variant="secondary" disabled={go.isPending} onClick={() => go.mutate(null)}>Continue without</Button>
      </div>
      <div className="mt-3"><ErrorNote error={go.error} /></div>
    </Card>
  )
}

function Timeline({ events, working }: { events: RunEvent[]; working: boolean }) {
  return (
    <section aria-labelledby="timeline-h">
      <h2 id="timeline-h" className="mb-2 text-lg font-semibold">What happened</h2>
      {events.length === 0 && <Loading what="Waiting for the first step" />}
      <ol className="flex flex-col gap-1" aria-live={working ? 'polite' : 'off'}>
        {events.map((e) => {
          const d = describeEvent(e)
          return (
            <li key={e.event_id} className="flex flex-wrap items-baseline gap-x-2 rounded-md px-2 py-1 text-sm odd:bg-surface-2/50">
              <span className="w-20 shrink-0 text-xs text-muted">{new Date(e.ts).toLocaleTimeString()}</span>
              <Badge tone={d.tone}>{d.label}</Badge>
              {d.detail && <span className="min-w-0 break-words">{d.detail}</span>}
            </li>
          )
        })}
      </ol>
    </section>
  )
}

export function RunPage() {
  const { id = '' } = useParams()
  const archive = useArchive()
  const run = useQuery({
    queryKey: archive ? ['archived-run', archive, id] : ['run', id],     // the panels below refresh ['run', id]
    queryFn: () => api.run(id, archive),
    refetchInterval: (q) => (!archive && q.state.data && ACTIVE.has(q.state.data.status ?? '') ? 1500 : false),
  })
  const events = useRunEvents(id, run.data?.status, archive)
  const data = run.data

  if (run.isLoading) return <Loading />
  if (run.error || !data) return <><PageHeader title="Run not found" /><ErrorNote error={run.error} /></>

  const working = ACTIVE.has(data.status ?? '')
  const failure = [...events].reverse().find((e) => e.type === 'run_failed')
  return (
    <>
      <PageHeader
        title={data.title ?? 'Run'}
        subtitle={<>{data.subject ? <>About <strong>{data.subject}</strong> · </> : null}started {events[0] ? time(events[0].ts) : '…'} · run <Mono>{id.slice(0, 8)}</Mono></>}
        actions={<span aria-live="polite"><StatusBadge status={data.status} /></span>}
      />
      <div className="flex flex-col gap-6">
        {archive && (
          <div role="note" className="rounded-md border border-line bg-surface-2 p-3 text-sm">
            Archived run, read only: from the evidence store archived as <Link className="text-accent underline" to={`/archives/${archive}`}>{archive}</Link>.
            {ACTIVE.has(data.status ?? '') || ['awaiting_approval', 'needs_capability', 'needs_source', 'paused'].includes(data.status ?? '') ? ' It was archived before it finished and cannot be continued.' : ''}
          </div>
        )}
        {!archive && data.status === 'awaiting_approval' && data.approval && <ApprovalPanel key={id + JSON.stringify(data.approval)} runId={id} plan={data.approval} />}
        {!archive && data.status === 'needs_capability' && <CapabilityPanel run={data} />}
        {!archive && data.status === 'needs_source' && <SourcePanel key={id + (data.source_request?.sources_asked ?? 0)} run={data} />}
        {data.status === 'failed' && (
          <div role="alert" className="rounded-md border border-bad bg-bad-bg p-3 text-sm text-bad">
            The run failed: {String(failure?.payload.error ?? 'unknown error')}
          </div>
        )}
        {data.status === 'stopped' && <Card><p className="text-sm">This run was stopped before doing any research.</p></Card>}
        <Flow events={events} status={data.status} />
        {data.report && <ReportView report={data.report} />}
        <Timeline events={events} working={working} />
      </div>
    </>
  )
}

import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type Brief, type CapabilityRequest, type Candidate, type ChatAction, type ChatLine, type FitCriterion, type Gate } from '../api'
import { time } from '../format'
import { Badge, Button, Card, ErrorNote, ExternalLink, Loading, Mono, PageHeader, StatusBadge, TierBadge } from '../ui'

const WORKING = new Set(['researching', 'integrating'])
const VERDICT: Record<FitCriterion['verdict'], ['ok' | 'warn' | 'bad' | 'neutral', string]> = {
  meets: ['ok', 'Meets'], partly: ['warn', 'Partly'], fails: ['bad', 'Fails'], unknown: ['neutral', 'Unknown'],
}

/** What each step of a request means, in words (the raw event names are kept in the store). */
const EVENT_WORDS: Record<string, string> = {
  requested: 'Request filed', brief: 'Started with your brief', researching: 'Looking for candidates',
  candidates_found: 'Candidates found', directory_failed: 'A directory could not be read', candidate_skipped: 'A candidate was skipped (its site could not be reached safely)', search_failed: 'A web search failed',
  researched: 'Read a candidate’s own pages and checked its facts', drafted: 'Drafted how to connect it', recommended: 'Scored against your brief',
  awaiting_user: 'Waiting for your decision', integrated: 'Approved: connecting', test_item: 'Test', tests_failed: 'Tests failed: not activated',
  ready: 'Active', resumed: 'The research run carried on with it', cancelled: 'Closed', failed: 'Stopped: nothing could be connected',
  llm_failed: 'A model call failed', retest: 'Tests run again', looked_up: 'Looked something up on a provider’s own pages',
}

const SUGGESTIONS: Record<string, string[]> = {
  awaiting_user: ['What else is there?', 'Is any of them free?', 'Who can sign up?', 'What now?'],
  failed: ['Why did it fail?', 'What else is there?', 'Anything free?'],
  tests_failed: ['Why did the test fail?', 'What now?'],
  cancelled: ['What else is there?'],
  ready: ['What are its limits?', 'What else is there?'],
  done: ['What are its limits?'],
}

/** The conversation on this request: answered by code from what the request holds (quotes, fit, tests, failures). */
function Conversation({ r }: { r: CapabilityRequest }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [text, setText] = useState('')
  const lines = r.events.filter((e) => e.event === 'chat').map((e) => e.detail as unknown as ChatLine)
  const refresh = () => qc.invalidateQueries({ queryKey: ['request', r.request_id] })
  const send = useMutation({ mutationFn: (m: string) => api.chatRequest(r.request_id, m), onSuccess: () => { setText(''); void refresh() } })
  const act = useMutation({
    mutationFn: (a: ChatAction) => api.actOnRequest(r.request_id, a.id, a.params),
    onSuccess: (x) => { void qc.invalidateQueries({ queryKey: ['requests'] }); if (x.request_id) navigate(`/connectors/requests/${x.request_id}`); else void refresh() },
  })
  const waiting = lines.length > 0 && lines[lines.length - 1].role === 'you'
  const busy = WORKING.has(r.status)
  return (
    <section aria-labelledby="chat-h">
      <Card>
        <h2 id="chat-h" className="font-semibold">Ask about this request</h2>
        <p className="mt-1 text-xs text-muted">Ask for other sources, help with a failed connection, or a question about a candidate. Answers come only from quotes on the providers’ own pages and from this request’s record; nothing is signed up for, paid or accepted on your behalf.</p>
        {lines.length > 0 && (
          <ol className="mt-3 flex flex-col gap-3" aria-live="polite">
            {lines.map((l, i) => (
              <li key={i} className={l.role === 'you' ? 'self-end max-w-[85%] rounded-lg bg-accent/10 px-3 py-2 text-sm' : 'max-w-[95%] rounded-lg border border-line px-3 py-2 text-sm'}>
                <p>{l.text}</p>
                {l.points && l.points.length > 0 && <ul className="mt-1 list-disc pl-5">{l.points.map((p) => <li key={p} className="break-words">{p}</li>)}</ul>}
                {l.facts && l.facts.length > 0 && (
                  <ul className="mt-2 flex flex-col gap-2">
                    {l.facts.map((f, j) => (
                      <li key={j} className="rounded-md border border-line p-2">
                        <p><strong>{f.candidate}</strong> · {f.label}{f.value ? `: ${f.value}` : ''}{f.official === false && <> <Badge tone="warn">third-party site</Badge></>}</p>
                        <blockquote className="mt-1 border-l-4 border-line pl-2 text-xs italic text-muted">“{f.quote}”</blockquote>
                        {f.source_url && <p className="mt-1 text-xs"><ExternalLink href={f.source_url}>{f.source_url}</ExternalLink></p>}
                      </li>
                    ))}
                  </ul>
                )}
                {l.notes && l.notes.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-xs text-muted">{l.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
                {l.link && <p className="mt-1"><Link className="text-accent underline" to={`/connectors/requests/${l.link}`}>Open the new request</Link></p>}
                {l.actions && l.actions.length > 0 && i === lines.length - 1 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {l.actions.map((a) => <Button key={a.id + a.label} variant={a.id === 'close' ? 'danger' : 'secondary'} disabled={act.isPending} onClick={() => act.mutate(a)}>{a.label}</Button>)}
                  </div>
                )}
              </li>
            ))}
          </ol>
        )}
        {waiting && <p role="status" className="mt-2 text-xs text-muted">Working on it: this page updates by itself.</p>}
        <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (text.trim()) send.mutate(text.trim()) }}>
          <label htmlFor="chat-input" className="sr-only">Your message</label>
          <input id="chat-input" className="min-w-0 flex-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={text} maxLength={1000}
            placeholder={busy ? 'You can ask once the search finishes' : 'For example: is there a free alternative?'} disabled={busy || send.isPending} onChange={(e) => setText(e.target.value)} />
          <Button type="submit" disabled={busy || send.isPending || !text.trim()}>Send</Button>
        </form>
        {!busy && (SUGGESTIONS[r.status] ?? []).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {(SUGGESTIONS[r.status] ?? []).map((s) => <button key={s} type="button" className="rounded-full border border-line px-3 py-1 text-xs hover:bg-surface-2" disabled={send.isPending} onClick={() => send.mutate(s)}>{s}</button>)}
          </div>
        )}
        <div className="mt-2"><ErrorNote error={send.error ?? act.error} /></div>
      </Card>
    </section>
  )
}

const isApi = (c: Candidate) => c.kind !== 'mcp'

function BriefCard({ brief }: { brief: Brief }) {
  const rows: [string, string][] = [
    ['Fields', brief.fields.join('; ')], ['Examples', brief.examples.join('; ')], ['Coverage', brief.coverage],
    ['Kind', brief.kind === 'either' ? 'API or MCP server' : brief.kind === 'api' ? 'API only' : 'MCP server only'],
    ['Budget', brief.budget_label], ['Volume', brief.volume ? `${brief.volume.toLocaleString()} calls a day` : ''],
    ['Freshness', brief.freshness === 'any' ? '' : brief.freshness === 'daily' ? 'Updated daily' : 'Real time'],
    ['Use', brief.commercial_use ? 'Commercial' : 'Not commercial'], ['Identity check', brief.identity_ok ? 'Can complete one' : 'Cannot complete one'],
    ['Leaving out', (brief.exclude ?? []).join(', ')],
  ]
  return (
    <Card>
      <h2 className="font-semibold">Your brief</h2>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        {rows.filter(([, v]) => v).map(([k, v]) => <Fragment key={k}><dt className="text-muted">{k}</dt><dd>{v}</dd></Fragment>)}
      </dl>
    </Card>
  )
}

/** Every candidate against every criterion, APIs first, MCP servers after, each cell with its reason. */
function FitTable({ candidates, criteria }: { candidates: Candidate[]; criteria: Gate['criteria'] }) {
  const shown = [...candidates.filter(isApi), ...candidates.filter((c) => !isApi(c))].filter((c) => c.fit)
  // the gate sends the criteria; a request that ended keeps them inside each candidate's fit
  criteria = criteria?.length ? criteria : (shown[0]?.fit?.criteria ?? []).map((c) => ({ id: c.id, label: c.label, question: '', hard: c.hard }))
  if (!shown.length || !criteria.length) return null
  return (
    <section aria-labelledby="fit-h">
      <h2 id="fit-h" className="mb-2 text-lg font-semibold">How each fits your brief</h2>
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">Each candidate judged on each criterion, from quoted facts</caption>
          <thead className="bg-surface-2">
            <tr>
              <th scope="col" className="p-2 text-left font-semibold">Criterion</th>
              {shown.map((c) => (
                <th key={c.connector_id} scope="col" className="min-w-48 p-2 text-left align-bottom">
                  <span className="block font-semibold">{c.name}</span>
                  <span className="mt-1 flex flex-wrap gap-1"><Badge tone={isApi(c) ? 'info' : 'neutral'}>{c.kind_label ?? (isApi(c) ? 'API' : 'MCP server')}</Badge>
                    {c.fit?.excluded && <Badge tone="bad">Ruled out</Badge>}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {criteria.map((cr) => (
              <tr key={cr.id} className="border-t border-line align-top">
                <th scope="row" className="p-2 text-left font-normal">
                  <span className="font-medium">{cr.label}</span>{cr.hard && <span className="text-xs text-muted"> · must</span>}
                </th>
                {shown.map((c) => {
                  const v = c.fit?.criteria.find((x) => x.id === cr.id)
                  if (!v) return <td key={c.connector_id} className="p-2 text-muted">–</td>
                  const [tone, word] = VERDICT[v.verdict]
                  return (
                    <td key={c.connector_id} className="p-2">
                      <Badge tone={tone}>{word}</Badge>
                      <span className="mt-1 block text-xs text-muted">{v.why}</span>
                      {v.quote && <blockquote className="mt-1 border-l-2 border-line pl-2 text-xs italic text-muted" title={`${v.quote}\n${v.source_url ?? ''}`}>
                        “{v.quote.length > 180 ? `${v.quote.slice(0, 180).trimEnd()}…` : v.quote}”</blockquote>}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-xs text-muted">Worked out by code from facts quoted on each provider’s own pages. “Must” criteria rule a source out when it fails them. A directory’s description is a lead, never evidence.</p>
    </section>
  )
}

function CandidateView({ c }: { c: Candidate }) {
  const plan = c.integration_plan
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-semibold">{c.name}</h3>
        <Badge tone={isApi(c) ? 'info' : 'neutral'}>{c.kind_label ?? (isApi(c) ? 'API' : 'MCP server')}</Badge>
        {c.cost_label && <Badge tone={c.cost_label.toLowerCase().includes('free (verified)') ? 'ok' : 'warn'}>{c.cost_label}</Badge>}
        {c.found_in && <Badge>found in {c.found_in}</Badge>}
      </div>
      {c.description && <p className="mt-1 text-sm text-muted">{c.description}</p>}
      {c.docs_url && <p className="mt-1 text-sm">Documentation: <ExternalLink href={c.docs_url}>{c.docs_url}</ExternalLink></p>}
      {c.fit?.excluded && <p role="alert" className="mt-2 rounded-md bg-bad-bg p-2 text-sm text-bad">Ruled out for your brief: {c.fit.exclusion_reasons.join('; ')}</p>}
      {c.cannot_integrate && <p className="mt-2 rounded-md bg-warn-bg p-2 text-sm text-warn">{isApi(c) ? 'Cannot be connected' : 'Not installable yet'}: {c.cannot_integrate}</p>}

      {c.registry && (
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted">Registry entry</dt><dd><Mono>{c.registry.id}</Mono>{c.registry.version ? ` v${c.registry.version}` : ''}</dd>
          <dt className="text-muted">How it runs</dt><dd>{c.registry.transport === 'remote' ? 'Remote: reached at a URL, nothing runs on this computer' : c.registry.transport === 'local' ? 'Local: a package that runs on this computer' : 'Not stated'}</dd>
          {c.registry.remote_url && <><dt className="text-muted">Endpoint</dt><dd><Mono>{c.registry.remote_url}</Mono></dd></>}
          {c.registry.packages.length > 0 && <><dt className="text-muted">Packages</dt><dd>{c.registry.packages.join(', ')}</dd></>}
          <dt className="text-muted">Key</dt><dd>{c.registry.needs_key ? `Required${c.registry.key_note ? `: ${c.registry.key_note}` : ''}` : 'None listed'}</dd>
          <dd className="col-span-2 text-xs text-muted">As listed in the official MCP Registry: the registry’s word, not checked against the provider’s pages.</dd>
        </dl>
      )}

      {c.verified_facts.length > 0 && (
        <div className="mt-3">
          <h4 className="text-sm font-semibold">Facts backed by a quote from a page WordBench read</h4>
          <ul className="mt-1 flex flex-col gap-2">
            {c.verified_facts.map((f) => (
              <li key={f.field + f.quote} className="rounded-md border border-line p-2 text-sm">
                <p><strong>{f.field.replace(/_/g, ' ')}:</strong> {f.value}{f.official === false && <> <Badge tone="warn">third-party site</Badge></>}</p>
                <blockquote className="mt-1 border-l-4 border-line pl-2 text-xs italic text-muted">“{f.quote}”</blockquote>
                <p className="mt-1 text-xs"><ExternalLink href={f.source_url}>{f.source_url}</ExternalLink></p>
              </li>
            ))}
          </ul>
        </div>
      )}
      {c.unverified.length > 0 && <p className="mt-2 text-sm text-warn">No quote found for: {c.unverified.join(', ')}. Treat these as unknown.</p>}
      {c.warnings?.map((w) => <p key={w} className="mt-2 rounded-md bg-warn-bg p-2 text-sm text-warn">{w}</p>)}

      {plan && (
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted">Host</dt><dd><Mono>{plan.host}</Mono> <TierBadge tier={plan.tier} /></dd>
          <dt className="text-muted">Key</dt><dd>{plan.key_env ? <>Put it in <Mono>.env</Mono> as <Mono>{plan.key_env}</Mono> (WordBench checks the name only)</> : 'None needed'}</dd>
          <dt className="text-muted">Endpoints</dt><dd className="break-words">{Object.keys(plan.endpoints).join(', ')}</dd>
          <dt className="text-muted">Test call</dt><dd><Mono>{plan.smoke_test.endpoint}</Mono></dd>
          <dt className="text-muted">Limits</dt><dd>{plan.budget.max_calls_per_day} calls a day, {plan.budget.max_calls_per_run} per run, {plan.rate_limit_per_minute} a minute</dd>
        </dl>
      )}

      {(c.terms_documents ?? []).length > 0 ? (
        <div className="mt-3 rounded-md border border-line p-3">
          <h4 className="text-sm font-semibold">Terms and usage rules to read before approving</h4>
          <ul className="mt-1 flex flex-col gap-1 text-sm">
            {c.terms_documents.map((d) => <li key={d.url}><ExternalLink href={d.url}>{d.url}</ExternalLink><span className="block text-xs text-muted">{d.why}</span></li>)}
          </ul>
        </div>
      ) : isApi(c) && !c.cannot_integrate ? (
        <p className="mt-3 rounded-md border border-line p-3 text-sm text-warn">No terms document was found for this provider. Find and read its terms yourself before approving.</p>
      ) : null}
    </Card>
  )
}

function Findings({ candidates, criteria }: { candidates: Candidate[]; criteria: Gate['criteria'] }) {
  const apis = candidates.filter(isApi), mcps = candidates.filter((c) => !isApi(c))
  return (
    <>
      <FitTable candidates={candidates} criteria={criteria} />
      <section aria-labelledby="apis-h" className="flex flex-col gap-3">
        <h2 id="apis-h" className="text-lg font-semibold">APIs ({apis.length})</h2>
        {apis.length === 0 ? <p className="text-sm text-muted">No API was found for this brief.</p> : apis.map((c) => <CandidateView key={c.connector_id} c={c} />)}
      </section>
      <section aria-labelledby="mcps-h" className="flex flex-col gap-3">
        <h2 id="mcps-h" className="text-lg font-semibold">MCP servers ({mcps.length})</h2>
        <p className="text-sm text-muted">MCP servers are found, vetted and recommended. Installing one runs or trusts third-party code, so it needs a pinned version, a review of its tools and a sandbox: that comes in a later phase.</p>
        {mcps.length === 0 ? <p className="text-sm text-muted">No MCP server was found for this brief.</p> : mcps.map((c) => <CandidateView key={c.connector_id} c={c} />)}
      </section>
    </>
  )
}

function GateView({ req, gate }: { req: CapabilityRequest; gate: Gate }) {
  const qc = useQueryClient()
  const integrable = gate.candidates.filter((c) => isApi(c) && !c.cannot_integrate)
  const [choice, setChoice] = useState(integrable.find((c) => c.connector_id === gate.recommendation.choice)?.connector_id ?? integrable[0]?.connector_id ?? '')
  const [terms, setTerms] = useState(false)
  const decide = useMutation({
    mutationFn: (approve: string | null) => api.decideRequest(req.request_id, { approve, terms_ok: approve ? terms : false }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['request', req.request_id] }),
  })
  const chosen = gate.candidates.find((c) => c.connector_id === choice)
  const name = (id?: string | null) => gate.candidates.find((c) => c.connector_id === id)?.name ?? id
  return (
    <section aria-labelledby="gate-h" className="flex flex-col gap-6">
      <Card>
        <h2 id="gate-h" className="text-lg font-semibold">Recommendation</h2>
        <p className="mt-1 text-sm"><strong>API:</strong> {gate.recommendation.choice ? name(gate.recommendation.choice) : 'none fits your brief'}
          {gate.recommendation.best_mcp && <> · <strong>MCP server:</strong> {name(gate.recommendation.best_mcp)} (not installable yet)</>}</p>
        {gate.recommendation.note && <p className="mt-1 text-sm text-warn">{gate.recommendation.note}</p>}
        <p className="mt-2 text-sm text-muted">Chosen by code from the fit to your brief, then cost. The reasoning below was written by a model and is an opinion, not a verified fact: {gate.recommendation.reasoning}</p>
      </Card>
      <Findings candidates={gate.candidates} criteria={gate.criteria} />
      {gate.note && <p role="alert" className="rounded-md bg-warn-bg p-3 text-sm text-warn">{gate.note}</p>}
      <Card className="border-warn">
        <h2 className="text-lg font-semibold">Your decision</h2>
        {integrable.length === 0 ? (
          <p className="mt-1 text-sm">None of the APIs can be connected. You can close this request.</p>
        ) : (
          <fieldset className="mt-1">
            <legend className="text-sm font-semibold">Approve one API</legend>
            {integrable.map((c) => (
              <label key={c.connector_id} className="mt-1 flex items-center gap-2 text-sm">
                <input type="radio" name="candidate" checked={choice === c.connector_id} onChange={() => setChoice(c.connector_id)} /> {c.name}
                {c.fit?.excluded && <Badge tone="bad">ruled out for your brief</Badge>}
              </label>
            ))}
            <label className="mt-3 flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-1" checked={terms} onChange={(e) => setTerms(e.target.checked)} />
              <span>I have read the provider’s terms and usage rules linked above{chosen?.integration_plan?.key_env && <>, and I have added <Mono>{chosen.integration_plan.key_env}</Mono> to <Mono>.env</Mono></>}.</span>
            </label>
          </fieldset>
        )}
        <div className="mt-3"><ErrorNote error={decide.error} /></div>
        <div className="mt-3 flex gap-2">
          {integrable.length > 0 && <Button disabled={!terms || !choice || decide.isPending} onClick={() => decide.mutate(choice)}>Approve and connect</Button>}
          <Button variant="danger" disabled={decide.isPending} onClick={() => decide.mutate(null)}>Close this request</Button>
        </div>
      </Card>
    </section>
  )
}

function Actions({ r }: { r: CapabilityRequest }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const cancel = useMutation({ mutationFn: () => api.cancelRequest(r.request_id), onSuccess: () => qc.invalidateQueries({ queryKey: ['request', r.request_id] }) })
  const retry = useMutation({ mutationFn: () => api.retryRequest(r.request_id), onSuccess: (x) => { void qc.invalidateQueries({ queryKey: ['requests'] }); navigate(`/connectors/requests/${x.request_id}`) } })
  const canCancel = ['requested', 'failed', 'tests_failed'].includes(r.status)
  const canRetry = ['failed', 'cancelled', 'tests_failed'].includes(r.status)
  if (!canCancel && !canRetry) return null
  return (
    <span className="flex flex-wrap items-center gap-2">
      {canRetry && <Button variant="secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>↻ Try again</Button>}
      {canCancel && <Button variant="danger" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Close</Button>}
      <ErrorNote error={cancel.error ?? retry.error} />
    </span>
  )
}

export function RequestPage() {
  const { id = '' } = useParams()
  const req = useQuery({
    queryKey: ['request', id],
    queryFn: () => api.request(id),
    refetchInterval: (q) => {
      const d = q.state.data
      if (!d) return false
      const chat = d.events.filter((e) => e.event === 'chat')
      const answerPending = chat.length > 0 && (chat[chat.length - 1].detail as { role?: string }).role === 'you'   // a look-up or a retest is running
      return WORKING.has(d.status) || (d.status === 'requested' && !d.run_id) || answerPending ? 2000 : false
    },
  })
  if (req.isLoading) return <Loading />
  if (req.error || !req.data) return <><PageHeader title="Request not found" /><ErrorNote error={req.error} /></>
  const r = req.data
  // the latest attempt's tests only: a retest after a fix replaces the earlier results on screen (all stay in History)
  const attempt = r.events.reduce((at, e, i) => (e.event === 'integrated' || e.event === 'retest' ? i : at), -1)
  const tests = r.events.filter((e, i) => e.event === 'test_item' && i > attempt)
  const failure = [...r.events].reverse().find((e) => e.event === 'failed')
  const findings = (failure?.detail.findings ?? []) as Candidate[]
  const waitingToStart = r.status === 'requested' && !!r.run_id
  const working = WORKING.has(r.status) || (r.status === 'requested' && !r.run_id)
  return (
    <>
      <PageHeader title="Add a data source" subtitle={<>{r.need}{r.retry_of && <> · tried again from <Link className="text-accent underline" to={`/connectors/requests/${r.retry_of}`}>{r.retry_of}</Link></>}
        {r.alternative_to && <> · other sources than in <Link className="text-accent underline" to={`/connectors/requests/${r.alternative_to}`}>{r.alternative_to}</Link></>}</>}
        actions={<span className="flex flex-wrap items-center gap-2"><Actions r={r} /><StatusBadge status={r.status} /></span>} />
      <div className="flex flex-col gap-6">
        {waitingToStart && (
          <Card className="border-warn">
            <p className="text-sm">A research run filed this request when it could not find what it needed (<Link className="text-accent underline" to={`/runs/${r.run_id}`}>the run</Link>). Nothing has been looked for yet.</p>
            <Link className="mt-2 inline-block rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg" to={`/apps/add-data-source?need=${encodeURIComponent(r.need)}&request=${r.request_id}`}>Start it with your brief</Link>
          </Card>
        )}
        {working && <Card><p role="status" className="text-sm">Looking in WordBench’s catalogue, the APIs.guru directory, the MCP Registry and the web, then reading each candidate’s own pages and checking every fact against a quote. This takes a few minutes.</p></Card>}
        {r.brief && <BriefCard brief={r.brief} />}
        {r.status === 'failed' && (
          <div role="alert" className="rounded-md border border-bad bg-bad-bg p-3 text-sm text-bad">
            No API could be connected. {String(failure?.detail.reason ?? failure?.detail.error ?? '')}
          </div>
        )}
        {r.status === 'failed' && findings.length > 0 && <Findings candidates={findings} criteria={undefined} />}
        {r.gate && <GateView req={r} gate={r.gate} />}
        {tests.length > 0 && (
          <Card>
            <h2 className="font-semibold">Tests</h2>
            <ul className="mt-2 flex flex-col gap-1 text-sm">
              {tests.map((t) => (
                <li key={String(t.detail.item)}><Badge tone={t.detail.status === 'pass' ? 'ok' : 'bad'}>{String(t.detail.status).toUpperCase()}</Badge> {String(t.detail.item)}
                  {t.detail.detail ? <span className="text-muted"> · {String(t.detail.detail)}</span> : null}</li>
              ))}
            </ul>
          </Card>
        )}
        {r.status === 'ready' && <Card><p className="text-sm">The data source is active. <Link className="text-accent underline" to="/connectors">See all data sources</Link></p></Card>}
        {!waitingToStart && <Conversation r={r} />}
        <section aria-labelledby="hist-h">
          <h2 id="hist-h" className="mb-2 text-lg font-semibold">History</h2>
          <ol className="flex flex-col gap-1 text-sm">
            {r.events.map((e, i) => e.event === 'chat' ? null : (
              <li key={i} className="flex gap-2">
                <span className="w-40 shrink-0 text-xs text-muted">{time(e.at)}</span>
                <span>{EVENT_WORDS[e.event] ?? e.event.replace(/_/g, ' ')}
                  {e.event === 'test_item' && e.detail.item ? `: ${String(e.detail.item)}` : ''}
                  {(e.event === 'cancelled' || e.event === 'failed') && (e.detail.reason || e.detail.error) ? ` (${String(e.detail.reason ?? e.detail.error)})` : ''}
                  {e.event === 'candidates_found' && Array.isArray(e.detail.candidates) ? `: ${(e.detail.candidates as string[]).join(', ')}` : ''}</span>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </>
  )
}

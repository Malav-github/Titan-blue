import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, type CapabilityRequest, type Candidate, type Gate } from '../api'
import { time } from '../format'
import { Badge, Button, Card, ErrorNote, ExternalLink, Loading, Mono, PageHeader, StatusBadge, TierBadge } from '../ui'

const WORKING = new Set(['requested', 'researching', 'integrating'])

function CandidateView({ c }: { c: Candidate }) {
  const plan = c.integration_plan
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-semibold">{c.name}</h3>
        {c.cost_label && <Badge tone={c.cost_label.startsWith('free verified') || c.cost_label.toLowerCase().includes('free (verified)') ? 'ok' : 'warn'}>{c.cost_label}</Badge>}
        <Badge>{c.kind === 'mcp' ? 'MCP server' : 'REST API'}</Badge>
      </div>
      <p className="mt-1 text-sm">Documentation: <ExternalLink href={c.docs_url}>{c.docs_url}</ExternalLink></p>
      {c.cannot_integrate && <p role="alert" className="mt-2 rounded-md bg-bad-bg p-2 text-sm text-bad">Cannot be integrated: {c.cannot_integrate}</p>}

      {c.verified_facts.length > 0 && (
        <div className="mt-3">
          <h4 className="text-sm font-semibold">Facts backed by a quote from a page Titan read</h4>
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
          <dt className="text-muted">Key</dt><dd>{plan.key_env ? <>Put it in <Mono>.env</Mono> as <Mono>{plan.key_env}</Mono> (Titan checks the name only)</> : 'None needed'}</dd>
          <dt className="text-muted">Endpoints</dt><dd className="break-words">{Object.keys(plan.endpoints).join(', ')}</dd>
          <dt className="text-muted">Test call</dt><dd><Mono>{plan.smoke_test.endpoint}</Mono></dd>
          <dt className="text-muted">Limits</dt><dd>{plan.budget.max_calls_per_day} calls a day, {plan.budget.max_calls_per_run} per run, {plan.rate_limit_per_minute} a minute</dd>
        </dl>
      )}

      <div className="mt-3 rounded-md border border-line p-3">
        <h4 className="text-sm font-semibold">Terms and usage rules to read before approving</h4>
        {c.terms_documents.length === 0 ? (
          <p className="mt-1 text-sm text-warn">No terms document was found for this provider. Find and read its terms yourself before approving.</p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1 text-sm">
            {c.terms_documents.map((d) => <li key={d.url}><ExternalLink href={d.url}>{d.url}</ExternalLink><span className="block text-xs text-muted">{d.why}</span></li>)}
          </ul>
        )}
      </div>
    </Card>
  )
}

function GateView({ req, gate }: { req: CapabilityRequest; gate: Gate }) {
  const qc = useQueryClient()
  const integrable = gate.candidates.filter((c) => !c.cannot_integrate)
  const [choice, setChoice] = useState(integrable.find((c) => c.connector_id === gate.recommendation.choice)?.connector_id ?? integrable[0]?.connector_id ?? '')
  const [terms, setTerms] = useState(false)
  const decide = useMutation({
    mutationFn: (approve: string | null) => api.decideRequest(req.request_id, { approve, terms_ok: approve ? terms : false }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['request', req.request_id] }),
  })
  const chosen = gate.candidates.find((c) => c.connector_id === choice)
  return (
    <section aria-labelledby="gate-h" className="flex flex-col gap-4">
      <h2 id="gate-h" className="text-xl font-semibold">Your decision</h2>
      {gate.candidates.map((c) => <CandidateView key={c.connector_id} c={c} />)}
      <Card>
        <p className="text-sm"><strong>Titan’s recommendation:</strong> {gate.recommendation.choice}</p>
        <p className="mt-1 text-sm text-muted">The reasoning below was written by a model and is an opinion, not a verified fact: {gate.recommendation.reasoning}</p>
      </Card>
      {gate.note && <p role="alert" className="rounded-md bg-warn-bg p-3 text-sm text-warn">{gate.note}</p>}
      <Card className="border-warn">
        {integrable.length === 0 ? (
          <p className="text-sm">None of the candidates can be integrated. You can cancel this request.</p>
        ) : (
          <fieldset>
            <legend className="text-sm font-semibold">Approve one</legend>
            {integrable.map((c) => (
              <label key={c.connector_id} className="mt-1 flex items-center gap-2 text-sm">
                <input type="radio" name="candidate" checked={choice === c.connector_id} onChange={() => setChoice(c.connector_id)} /> {c.name}
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
          {integrable.length > 0 && <Button disabled={!terms || !choice || decide.isPending} onClick={() => decide.mutate(choice)}>Approve and integrate</Button>}
          <Button variant="danger" disabled={decide.isPending} onClick={() => decide.mutate(null)}>Cancel this request</Button>
        </div>
      </Card>
    </section>
  )
}

export function RequestPage() {
  const { id = '' } = useParams()
  const req = useQuery({
    queryKey: ['request', id],
    queryFn: () => api.request(id),
    refetchInterval: (q) => (q.state.data && WORKING.has(q.state.data.status) ? 2000 : false),
  })
  if (req.isLoading) return <Loading />
  if (req.error || !req.data) return <><PageHeader title="Request not found" /><ErrorNote error={req.error} /></>
  const r = req.data
  const tests = r.events.filter((e) => e.event === 'test_item')
  const failure = [...r.events].reverse().find((e) => e.event === 'failed')
  return (
    <>
      <PageHeader title="Add a data source" subtitle={r.need} actions={<StatusBadge status={r.status} />} />
      <div className="flex flex-col gap-6">
        {WORKING.has(r.status) && <Card><p role="status" className="text-sm">Reading the provider’s documentation and checking every fact against a quote. This takes a few minutes.</p></Card>}
        {r.status === 'failed' && (
          <div role="alert" className="rounded-md border border-bad bg-bad-bg p-3 text-sm text-bad">
            No data source could be set up. {String(failure?.detail.reason ?? '')}
            {r.events.some((e) => e.event === 'drafted') && ' See the history below.'}
          </div>
        )}
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
        <section aria-labelledby="hist-h">
          <h2 id="hist-h" className="mb-2 text-lg font-semibold">History</h2>
          <ol className="flex flex-col gap-1 text-sm">
            {r.events.map((e, i) => <li key={i} className="flex gap-2"><span className="w-40 shrink-0 text-xs text-muted">{time(e.at)}</span><span>{e.event.replace(/_/g, ' ')}</span></li>)}
          </ol>
        </section>
      </div>
    </>
  )
}

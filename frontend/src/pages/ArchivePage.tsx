import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { time } from '../format'
import { Badge, Button, Card, EmptyState, ErrorNote, Loading, Mono, PageHeader, StatusBadge, TierBadge, VerdictBadge } from '../ui'
import { storeLine } from './Stores'

export function ArchivePage() {
  const { aid = '' } = useParams()
  const archive = useQuery({ queryKey: ['archive', aid], queryFn: () => api.archive(aid) })
  const claims = useQuery({ queryKey: ['claims', aid, ''], queryFn: () => api.claims(undefined, aid) })
  const verify = useMutation({ mutationFn: () => api.verifyArchive(aid) })
  if (archive.isLoading) return <Loading />
  if (archive.error || !archive.data) return <><PageHeader title="Archive not found" /><ErrorNote error={archive.error} /></>
  const { manifest: m, runs } = archive.data
  return (
    <>
      <PageHeader title={m.label || m.id} subtitle={<>Archived evidence store, read only · archived {time(m.archived_at)} by {m.archived_by}{m.live_since ? <> · in use from {time(m.live_since)}</> : null}</>}
        actions={<Link className="text-sm text-accent underline" to="/stores">All stores</Link>} />
      <div className="flex flex-col gap-4">
        <Card>
          <p className="text-sm">{storeLine(m.counts, m.runs, m.folders)}</p>
          {m.reason && <p className="mt-1 text-sm">Why it was archived: {m.reason}</p>}
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted">Name</dt><dd><Mono>{m.id}</Mono></dd>
            <dt className="text-muted">On disk</dt><dd><Mono>data/archives/{m.id}/</Mono></dd>
            {m.titan_commit && <><dt className="text-muted">Titan version</dt><dd><Mono>{m.titan_commit}</Mono></dd></>}
            <dt className="text-muted">Holds</dt><dd><ul className="list-disc pl-5">{m.contains.map((c) => <li key={c}>{c}</li>)}</ul></dd>
          </dl>
          <p className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            <Badge tone={m.audit.problems ? 'warn' : 'ok'}>{m.audit.problems ? `${m.audit.problems} audit problems when archived (the same as in the live store)` : 'Audit passed when archived'}</Badge>
            <Button variant="secondary" onClick={() => verify.mutate()} disabled={verify.isPending}>{verify.isPending ? 'Checking every file…' : 'Check it again now'}</Button>
          </p>
          <ErrorNote error={verify.error} />
          {verify.data && (
            <p role="status" className={`mt-2 text-sm ${verify.data.unchanged ? 'text-ok' : 'text-bad'}`}>
              {verify.data.unchanged ? `Unchanged: every page re-hashed and every quote re-verified (${verify.data.problems} problems, as recorded).` : `Changed since it was archived: ${verify.data.problems} problems now, ${verify.data.recorded_at_archive} then. ${verify.data.examples.join('; ')}`}
            </p>
          )}
          {m.unfinished_runs.length > 0 && <p className="mt-2 text-sm text-warn">{m.unfinished_runs.length} run(s) were archived before they finished and cannot be continued.</p>}
        </Card>
        <section aria-labelledby="runs-h">
          <h2 id="runs-h" className="mb-2 text-lg font-semibold">Runs</h2>
          {runs.length === 0 ? <EmptyState title="No runs in this archive" /> : (
            <div className="overflow-x-auto rounded-lg border border-line bg-surface">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Archived runs, newest first</caption>
                <thead className="border-b border-line text-muted"><tr><th scope="col" className="p-3">What</th><th scope="col" className="p-3">Status</th><th scope="col" className="p-3">Started</th></tr></thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.run_id} className="border-b border-line last:border-0">
                      <td className="p-3"><Link className="text-accent underline" to={`/archives/${aid}/runs/${r.run_id}`}>{r.question ?? r.run_id.slice(0, 8)}</Link></td>
                      <td className="p-3"><StatusBadge status={r.status} /></td>
                      <td className="p-3 text-muted">{time(r.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <section aria-labelledby="claims-h">
          <h2 id="claims-h" className="mb-2 text-lg font-semibold">Claims</h2>
          {claims.isLoading && <Loading />}
          <ErrorNote error={claims.error} />
          {claims.data?.length === 0 && <EmptyState title="No claims in this archive" />}
          {claims.data && claims.data.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-line bg-surface">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Archived claims, newest first</caption>
                <thead className="border-b border-line text-muted"><tr><th scope="col" className="p-3">Claim</th><th scope="col" className="p-3">About</th><th scope="col" className="p-3">Source</th><th scope="col" className="p-3">Check</th></tr></thead>
                <tbody>
                  {claims.data.map((c) => (
                    <tr key={c.claim_id} className="border-b border-line align-top last:border-0">
                      <td className="p-3"><Link className="text-accent underline" to={`/archives/${aid}/evidence/${c.claim_id}`}>#{c.claim_id}</Link> {c.claim_text}</td>
                      <td className="p-3">{c.entity}</td>
                      <td className="p-3"><TierBadge tier={c.source_tier} /></td>
                      <td className="p-3"><VerdictBadge verdict={c.verdict} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {claims.data.length === 100 && <p className="p-3 text-xs text-muted">Showing the newest 100 claims.</p>}
            </div>
          )}
        </section>
      </div>
    </>
  )
}

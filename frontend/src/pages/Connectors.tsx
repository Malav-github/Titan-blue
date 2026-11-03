import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { time } from '../format'
import { Badge, Card, EmptyState, ErrorNote, Loading, Mono, PageHeader, StatusBadge, TierBadge } from '../ui'

export function Connectors() {
  const active = useQuery({ queryKey: ['connectors'], queryFn: api.connectors })
  const requests = useQuery({ queryKey: ['requests'], queryFn: api.requests, refetchInterval: 4000 })
  return (
    <>
      <PageHeader
        title="Data sources"
        subtitle="APIs Titan may call. Each one was vetted from its own documentation and approved by you; it can only make read-only calls to one host."
        actions={<Link className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg" to="/apps/add-data-source">Add a data source</Link>}
      />
      <h2 className="mb-2 text-lg font-semibold">Active</h2>
      {active.isLoading && <Loading />}
      <ErrorNote error={active.error} />
      {active.data?.length === 0 && <EmptyState title="No data source is active yet">Use “Add a data source” to find and approve one.</EmptyState>}
      <ul className="grid gap-4 sm:grid-cols-2">
        {active.data?.map((c) => (
          <li key={c.connector_id}>
            <Card>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-semibold">{c.name}</h3><TierBadge tier={c.tier} />
              </div>
              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                <dt className="text-muted">Host</dt><dd><Mono>{c.host}</Mono></dd>
                <dt className="text-muted">Key</dt><dd>{c.auth === 'none' ? 'None needed' : 'Yes, stored in .env'}</dd>
                <dt className="text-muted">Calls</dt><dd>{c.calls_last_24h} in the last 24 hours (limit {c.budget.max_calls_per_day} a day)</dd>
                <dt className="text-muted">Endpoints</dt><dd>{c.endpoints.join(', ')}</dd>
              </dl>
            </Card>
          </li>
        ))}
      </ul>

      <h2 className="mb-2 mt-8 text-lg font-semibold">Requests</h2>
      {requests.data?.length === 0 && <p className="text-sm text-muted">No requests yet.</p>}
      <ul className="flex flex-col gap-2">
        {requests.data?.map((r) => (
          <li key={r.request_id} className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-surface px-3 py-2 text-sm">
            <Link className="text-accent underline" to={`/connectors/requests/${r.request_id}`}>{r.need}</Link>
            <StatusBadge status={r.status} />
            <Badge>{time(r.created_at)}</Badge>
          </li>
        ))}
      </ul>
    </>
  )
}

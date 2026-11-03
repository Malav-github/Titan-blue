import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { time } from '../format'
import { EmptyState, ErrorNote, Loading, PageHeader, StatusBadge } from '../ui'

export function Runs() {
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: 3000 })
  return (
    <>
      <PageHeader title="Runs" subtitle="Everything Titan has run, newest first. Each run keeps a permanent step-by-step history." />
      {runs.isLoading && <Loading />}
      <ErrorNote error={runs.error} />
      {runs.data && runs.data.length === 0 && (
        <EmptyState title="Nothing has run yet">Open an app on the Home page to start one.</EmptyState>
      )}
      {runs.data && runs.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Runs, newest first</caption>
            <thead className="border-b border-line text-muted">
              <tr><th scope="col" className="p-3">What</th><th scope="col" className="p-3">Status</th><th scope="col" className="p-3">Started</th></tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.run_id} className="border-b border-line last:border-0">
                  <td className="p-3"><Link className="text-accent underline" to={`/runs/${r.run_id}`}>{r.question ?? r.run_id.slice(0, 8)}</Link></td>
                  <td className="p-3"><StatusBadge status={r.status} /></td>
                  <td className="p-3 text-muted">{time(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

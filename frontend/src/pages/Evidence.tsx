import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { EmptyState, ErrorNote, Loading, PageHeader, TierBadge, VerdictBadge } from '../ui'

export function Evidence() {
  const [entity, setEntity] = useState('')
  const [applied, setApplied] = useState('')
  const claims = useQuery({ queryKey: ['claims', applied], queryFn: () => api.claims(applied || undefined) })
  return (
    <>
      <PageHeader title="Evidence" subtitle="Every claim Titan has stored. A claim exists only if its quote appears word for word in an archived page; nothing here can be edited." />
      <form className="mb-4 flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); setApplied(entity.trim()) }}>
        <div>
          <label htmlFor="entity" className="block text-sm font-medium">Company or subject</label>
          <input id="entity" className="mt-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={entity} onChange={(e) => setEntity(e.target.value)} placeholder="All" />
        </div>
        <button className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:bg-surface-2" type="submit">Filter</button>
      </form>
      {claims.isLoading && <Loading />}
      <ErrorNote error={claims.error} />
      {claims.data?.length === 0 && <EmptyState title="No claims yet">Run a research app to collect some.</EmptyState>}
      {claims.data && claims.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Stored claims, newest first</caption>
            <thead className="border-b border-line text-muted">
              <tr><th scope="col" className="p-3">Claim</th><th scope="col" className="p-3">About</th><th scope="col" className="p-3">Source</th><th scope="col" className="p-3">Check</th></tr>
            </thead>
            <tbody>
              {claims.data.map((c) => (
                <tr key={c.claim_id} className="border-b border-line align-top last:border-0">
                  <td className="p-3"><Link className="text-accent underline" to={`/evidence/${c.claim_id}`}>#{c.claim_id}</Link> {c.claim_text}</td>
                  <td className="p-3">{c.entity}</td>
                  <td className="p-3"><TierBadge tier={c.source_tier} /></td>
                  <td className="p-3"><VerdictBadge verdict={c.verdict} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

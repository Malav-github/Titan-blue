import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, type ArchiveManifest } from '../api'
import { time } from '../format'
import { Badge, Button, Card, EmptyState, ErrorNote, Loading, PageHeader } from '../ui'

export function size(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`
  return `${Math.round(bytes / 1e3)} KB`
}

export function storeLine(counts: Record<string, Record<string, number>>, runs: number, folders: Record<string, { files: number; bytes: number }>) {
  const e = counts.evidence ?? {}
  const files = folders.files ?? { files: 0, bytes: 0 }
  return `${runs} runs · ${e.claims ?? 0} claims · ${e.sources ?? 0} sources · ${e.rejected_claims ?? 0} rejected attempts · ${files.files} archived files (${size(files.bytes)})`
}

function ArchiveForm({ blocked }: { blocked: boolean }) {
  const qc = useQueryClient()
  const [label, setLabel] = useState('')
  const [reason, setReason] = useState('')
  const [confirm, setConfirm] = useState(false)
  const go = useMutation({
    mutationFn: () => api.archiveStore({ label, reason, confirm }),
    onSuccess: () => { setLabel(''); setReason(''); setConfirm(false); void qc.invalidateQueries() },
  })
  return (
    <form className="mt-3 flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); go.mutate() }}>
      <div>
        <label htmlFor="label" className="block text-sm font-medium">Name for the archive</label>
        <input id="label" maxLength={60} className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={label}
          onChange={(e) => setLabel(e.target.value)} placeholder="e.g. before ontologies" />
      </div>
      <div>
        <label htmlFor="reason" className="block text-sm font-medium">Why (kept in the archive)</label>
        <input id="reason" maxLength={300} className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-1.5 text-sm" value={reason}
          onChange={(e) => setReason(e.target.value)} placeholder="e.g. source labels changed; start clean" />
      </div>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
        <span>Archive the current store and start a new, empty one. Nothing is deleted: the archive is checked first and stays readable here.</span>
      </label>
      <div><Button variant="danger" type="submit" disabled={!confirm || blocked || go.isPending}>{go.isPending ? 'Archiving and checking…' : 'Archive and start a new store'}</Button></div>
      <ErrorNote error={go.error} />
      {go.data && <p role="status" className="text-sm text-ok">Archived as <Link className="underline" to={`/archives/${go.data.id}`}>{go.data.id}</Link>; the audit of the archive matched the live store. The new store is empty.</p>}
    </form>
  )
}

function ArchiveRow({ a }: { a: ArchiveManifest }) {
  return (
    <li className="rounded-md border border-line p-3 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <Link className="font-medium text-accent underline" to={`/archives/${a.id}`}>{a.label || a.id}</Link>
        <span className="text-muted">archived {time(a.archived_at)} by {a.archived_by}</span>
        <Badge tone={a.audit.problems ? 'warn' : 'ok'}>{a.audit.problems ? `${a.audit.problems} audit problems (as in the live store)` : 'Audit passed'}</Badge>
      </p>
      <p className="mt-1">{storeLine(a.counts, a.runs, a.folders)}</p>
      {a.reason && <p className="mt-1 text-muted">Why: {a.reason}</p>}
    </li>
  )
}

export function Stores() {
  const qc = useQueryClient()
  const overview = useQuery({ queryKey: ['stores'], queryFn: api.stores })
  const reload = useMutation({ mutationFn: api.reloadDefinitions, onSuccess: () => void qc.invalidateQueries() })
  const o = overview.data
  return (
    <>
      <PageHeader title="Stores and archives" subtitle="The evidence store Titan is using now, the ones archived before it, and what Titan holds in memory." />
      {overview.isLoading && <Loading />}
      <ErrorNote error={overview.error} />
      {o && (
        <div className="flex flex-col gap-4">
          <Card>
            <h2 className="font-semibold">Current store</h2>
            <p className="mt-1 text-sm">{storeLine(o.live.counts, o.live.runs, o.live.folders)}</p>
            <p className="mt-1 text-xs text-muted">{o.live.since ? `In use since ${time(o.live.since)}.` : 'Nothing stored yet.'} Connectors, their approvals, usage budgets and settings are not part of a store and stay as they are.</p>
            {o.live.blockers.length > 0 && (
              <div role="alert" className="mt-3 rounded-md border border-warn bg-warn-bg p-3 text-sm text-warn">
                <p className="font-medium">The store cannot be archived right now:</p>
                <ul className="list-disc pl-5">{o.live.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
              </div>
            )}
            {o.live.waiting.length > 0 && (
              <p className="mt-3 text-sm text-warn">
                {o.live.waiting.length} run{o.live.waiting.length > 1 ? 's are' : ' is'} waiting for you ({o.live.waiting.map((w) => w.question ?? w.run_id.slice(0, 8)).join('; ')}). Archived now, {o.live.waiting.length > 1 ? 'they' : 'it'} would be kept as {o.live.waiting.length > 1 ? 'they stand' : 'it stands'} and could not be continued.
              </p>
            )}
            {o.live.abandoned.length > 0 && (
              <div className="mt-3 text-sm">
                <p className="text-warn">{o.live.abandoned.length} run{o.live.abandoned.length > 1 ? 's are' : ' is'} still marked as working but stopped long ago (the server stopped while {o.live.abandoned.length > 1 ? 'they' : 'it'} ran). Archiving adds a "failed: abandoned" event to each first; nothing else changes.</p>
                <ul className="list-disc pl-5 text-muted">{o.live.abandoned.map((r) => <li key={r.run_id}><Link className="underline" to={`/runs/${r.run_id}`}>{r.question || r.run_id.slice(0, 8)}</Link>, last activity {time(r.last_activity)}</li>)}</ul>
              </div>
            )}
            <h3 className="mt-4 text-sm font-semibold">Start a new store</h3>
            <ArchiveForm blocked={o.live.blockers.length > 0} />
          </Card>
          <Card>
            <h2 className="font-semibold">Archived stores</h2>
            {o.archives.length === 0
              ? <EmptyState title="No archives yet">When you start a new store, the current one is archived here, read only.</EmptyState>
              : <ul className="mt-2 flex flex-col gap-2">{o.archives.map((a) => <ArchiveRow key={a.id} a={a} />)}</ul>}
            <p className="mt-2 text-xs text-muted">On disk: data/archives/&lt;name&gt;/ (evidence.db, runs.db, checkpoints.db, searches.db, files/, manifest.json), all read-only.</p>
          </Card>
          <Card>
            <h2 className="font-semibold">Clear what Titan holds in memory</h2>
            <p className="mt-1 text-sm">Re-reads the definition files (ontologies, skills, guidance) on next use, so edits take effect without a restart, and forgets cached health checks and source descriptions. Nothing stored is touched.</p>
            <div className="mt-3"><Button variant="secondary" onClick={() => reload.mutate()} disabled={reload.isPending}>Reload definitions</Button></div>
            <ErrorNote error={reload.error} />
            {reload.data && <p role="status" className="mt-2 text-sm text-ok">Cleared {reload.data.cleared.length} caches.</p>}
            <p className="mt-2 text-xs text-muted">Blocks and app templates are read when the server starts; changes to those still need a restart.</p>
          </Card>
          {o.log.length > 0 && (
            <Card>
              <h2 className="font-semibold">History</h2>
              <ul className="mt-2 flex flex-col gap-1 text-sm">
                {[...o.log].reverse().map((e, i) => (
                  <li key={i}>{time(e.at)}: {e.action === 'archive' ? <>archived as <Link className="text-accent underline" to={`/archives/${e.id}`}>{e.id}</Link> by {e.by} ({e.runs} runs, {e.claims} claims){e.reason ? ` · ${e.reason}` : ''}</> : e.action}</li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      )}
    </>
  )
}

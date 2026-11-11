import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { time } from '../format'
import { Badge, Button, Card, ErrorNote, Loading, PageHeader } from '../ui'

/** Restart the server with the current code. The page waits for the new process (a new start time), then reloads everything. */
function ServerCard() {
  const qc = useQueryClient()
  const [waiting, setWaiting] = useState<string | null>(null)       // the old start time, while the restart is under way
  const [note, setNote] = useState('')
  const info = useQuery({ queryKey: ['server'], queryFn: api.server, retry: false, refetchInterval: waiting ? 1000 : 10000 })
  const restart = useMutation({
    mutationFn: api.restartServer,
    onSuccess: (r) => { setWaiting(r.started_at); setNote('') },
  })
  const s = info.data
  useEffect(() => {
    if (waiting && s && s.started_at !== waiting) {                // the new process answers
      setWaiting(null)
      setNote(`Restarted at ${time(s.started_at)}.`)
      void qc.invalidateQueries()
    }
  }, [waiting, s, qc])
  return (
    <Card className="md:col-span-2">
      <h2 className="font-semibold">Server</h2>
      {s && <p className="mt-1 text-sm">Running since {time(s.started_at)} · process {s.pid} · {s.busy ? `${s.busy} ${s.busy === 1 ? 'run' : 'runs'} in progress — pause or stop ${s.busy === 1 ? 'it' : 'them'} to restart` : 'no runs in progress — safe to restart'}</p>}
      <p className="mt-1 text-xs text-muted">
        Restarting loads the current code (and rebuilds the app if its source changed). Runs that are paused or waiting for you are kept and carry on afterwards;
        a run that is working must be paused or stopped first.
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button variant="secondary" disabled={!s?.can_restart || !!s?.busy || restart.isPending || !!waiting}
          onClick={() => { if (window.confirm('Restart the Titan server? The app will be unavailable for a few seconds.')) restart.mutate() }}>
          <span aria-hidden="true">↻ </span>{waiting ? 'Restarting…' : 'Restart server'}
        </Button>
        <span aria-live="polite" className="text-sm">
          {waiting && 'Waiting for the new server to answer…'}
          {note && <span className="text-ok">{note}</span>}
          {s && !s.can_restart && <span className="text-muted">This server was not started with serve.py, so it cannot restart itself.</span>}
        </span>
      </div>
      <ErrorNote error={restart.error} />
    </Card>
  )
}

export function HealthPage() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health })
  const models = useQuery({ queryKey: ['models'], queryFn: api.models })
  const h = health.data
  const problems = (h?.evidence.audit_problems as string[] | undefined) ?? []
  return (
    <>
      <PageHeader title="Health" subtitle="Whether the parts Titan depends on are running, and whether the evidence store still checks out." />
      {(health.isLoading || models.isLoading) && <Loading />}
      <ErrorNote error={health.error ?? models.error} />
      <div className="grid gap-4 md:grid-cols-2">
        <ServerCard />
        {h && (
          <Card>
            <h2 className="font-semibold">Services</h2>
            <ul className="mt-2 flex flex-col gap-2 text-sm">
              {Object.entries(h.checks).map(([name, c]) => (
                <li key={name}><Badge tone={c.ok ? 'ok' : 'bad'}>{c.ok ? 'OK' : 'Down'}</Badge> <strong>{name}</strong>{c.detail && <span className="block text-xs text-muted">{c.detail}</span>}</li>
              ))}
              <li><Badge tone={h.brave_key_set ? 'ok' : 'neutral'}>{h.brave_key_set ? 'Set' : 'Not set'}</Badge> <strong>Brave search key</strong></li>
              <li><Badge tone={h.langfuse_configured ? 'ok' : 'neutral'}>{h.langfuse_configured ? 'Set' : 'Not set'}</Badge> <strong>Langfuse tracing</strong></li>
            </ul>
            <p className="mt-2 text-xs text-muted">Search providers in use: {h.search_providers.join(', ')} · mode: {h.mode}</p>
          </Card>
        )}
        {models.data && (
          <Card>
            <h2 className="font-semibold">Models</h2>
            {!models.data.ollama_reachable && <p role="alert" className="mt-2 text-sm text-bad">Ollama is not reachable, so no model can run.</p>}
            <ul className="mt-2 flex flex-col gap-1 text-sm">
              {models.data.tasks.map((t) => (
                <li key={t.task}><Badge tone={t.installed ? 'ok' : 'bad'}>{t.installed ? 'Installed' : 'Missing'}</Badge> <strong>{t.task}</strong>: {t.model}</li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-muted">Installed: {models.data.installed.join(', ') || 'none'}</p>
          </Card>
        )}
        {h && (
          <Card className="md:col-span-2">
            <h2 className="font-semibold">Evidence store</h2>
            <p className="mt-1 text-sm">
              {String(h.evidence.sources)} sources · {String(h.evidence.claims)} claims · {String(h.evidence.rejected_claims)} rejected attempts · {String(h.evidence.support_checks)} support checks
            </p>
            {problems.length === 0
              ? <p className="mt-2 text-sm text-ok">Audit passed: every stored claim and file still matches its hash.</p>
              : <ul role="alert" className="mt-2 list-disc pl-5 text-sm text-bad">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
          </Card>
        )}
      </div>
    </>
  )
}

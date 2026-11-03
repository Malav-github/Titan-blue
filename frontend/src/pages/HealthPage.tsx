import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { Badge, Card, ErrorNote, Loading, PageHeader } from '../ui'

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

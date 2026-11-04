import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { time } from '../format'
import { Badge, Card, ErrorNote, ExternalLink, Loading, Mono, PageHeader, TierBadge, VerdictBadge, timeChips } from '../ui'

export function ClaimPage() {
  const { id = '' } = useParams()
  const claim = useQuery({ queryKey: ['claim', id], queryFn: () => api.claim(id) })
  if (claim.isLoading) return <Loading />
  if (claim.error || !claim.data) return <><PageHeader title="Claim not found" /><ErrorNote error={claim.error} /></>
  const c = claim.data
  const latest = c.support_checks[c.support_checks.length - 1]
  return (
    <>
      <PageHeader title={`Claim #${c.claim_id}`} subtitle={c.claim_text} actions={<VerdictBadge verdict={latest?.verdict} />} />
      <div className="flex flex-col gap-4">
        <Card>
          <h2 className="font-semibold">The quote, in its page</h2>
          <p className="mt-2 text-sm leading-relaxed">…{c.before}<mark>{c.quote}</mark>{c.after}…</p>
          <p className="mt-3 flex flex-wrap items-center gap-2 text-sm"><TierBadge tier={c.source_tier} /> <ExternalLink href={c.source_url}>{c.source_url}</ExternalLink></p>
        </Card>
        <Card>
          <h2 className="font-semibold">When this claim holds</h2>
          {(c.times ?? []).length === 0 && <p className="mt-1 text-sm text-muted">The quote names no period and no event date.</p>}
          <ul className="mt-2 flex flex-col gap-1 text-sm">
            {(c.times ?? []).map((t, i) => (
              <li key={i}>
                <strong>{t.kind.replace('_', ' ')}</strong>{t.value ? `: ${t.value}` : ''}{' '}
                <span className="text-muted">(read from {t.origin === 'quote' ? 'the quote' : t.origin === 'context' ? 'the text around the quote' : t.origin === 'edgar_xbrl' ? 'the SEC fact itself' : 'the page date'}{t.derived ? ', derived' : ''}: “{t.raw}”)</span>
              </li>
            ))}
          </ul>
          {timeChips(c.times).length > 0 && <p className="mt-2 flex flex-wrap gap-2">{timeChips(c.times).map((x, i) => <Badge key={i} tone={x.tone}>{x.text}</Badge>)}</p>}
          {(c.conflicts ?? []).map((x, i) => (
            <p key={i} className="mt-2 text-sm text-bad">Conflicts with <Link className="underline" to={`/evidence/${x.other_claim}`}>claim #{x.other_claim}</Link>: {x.note}</p>
          ))}
        </Card>
        <Card>
          <h2 className="font-semibold">Dates this page carried</h2>
          {(c.source_dates ?? []).length === 0 && <p className="mt-1 text-sm text-muted">No date was found on this page.</p>}
          <ul className="mt-2 flex flex-col gap-1 text-sm">
            {(c.source_dates ?? []).map((d, i) => (
              <li key={i}>
                <strong>{d.kind.replace('_', ' ')}</strong>: {d.value} <span className="text-muted">({d.precision}{d.relative ? ', worked out from "' + d.raw + '"' : ''} · from {d.origin})</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted">HTTP header dates say when the server answered, not when the page was written, so they are never used as the page's date.</p>
        </Card>
        <Card>
          <h2 className="font-semibold">Support checks</h2>
          {c.support_checks.length === 0 && <p className="mt-1 text-sm text-muted">This claim has not been checked yet.</p>}
          <ul className="mt-2 flex flex-col gap-2">
            {c.support_checks.map((s, i) => (
              <li key={i} className="rounded-md border border-line p-2 text-sm">
                <VerdictBadge verdict={s.verdict} /> <span className="text-muted">by {s.checker} · {time(s.created_at)}</span>
                <p className="mt-1">{s.reasoning}</p>
              </li>
            ))}
          </ul>
        </Card>
        <Card>
          <h2 className="font-semibold">Proof it has not changed</h2>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted">About</dt><dd>{c.entity.name}{c.entity.domain ? <> · {c.entity.domain} ({c.entity.domain_status ?? 'unconfirmed'})</> : null}</dd>
            <dt className="text-muted">Page fetched</dt><dd>{time(c.fetched_at)}</dd>
            <dt className="text-muted">Page hash</dt><dd><Mono>{c.source_sha256}</Mono></dd>
            <dt className="text-muted">Text hash</dt><dd><Mono>{c.text_sha256}</Mono></dd>
            <dt className="text-muted">Recorded</dt><dd>{time(c.created_at)} by {c.created_by}</dd>
          </dl>
        </Card>
        <p><Link className="text-accent underline" to="/evidence">Back to all claims</Link></p>
      </div>
    </>
  )
}

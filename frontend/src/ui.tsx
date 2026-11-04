import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { ApiError } from './api'

const cx = (...parts: (string | false | undefined)[]) => parts.filter(Boolean).join(' ')

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cx('rounded-lg border border-line bg-surface p-4', className)}>{children}</section>
}

type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral'
const TONES: Record<Tone, string> = {
  ok: 'bg-ok-bg text-ok',
  warn: 'bg-warn-bg text-warn',
  bad: 'bg-bad-bg text-bad',
  info: 'bg-info-bg text-info',
  neutral: 'bg-surface-2 text-muted',
}
export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={cx('inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap', TONES[tone])}>{children}</span>
}

/** Short chips for a claim's time attributes: its period, event dates, and a note when the statement is about the future. */
export function timeChips(times: { kind: string; value: string; derived: number; detail: { change?: string; comparison?: string; end?: string } }[] = []) {
  const chips: { text: string; tone: Tone }[] = []
  for (const t of times) {
    if (t.kind === 'period') {
      const cmp = t.detail.change ? ` · ${t.detail.change}${t.detail.comparison ? ' ' + t.detail.comparison.toUpperCase() : ''}` : ''
      chips.push({ text: `Period: ${t.value}${t.detail.end && !t.value.includes(t.detail.end) ? ` (to ${t.detail.end})` : ''}${cmp}`, tone: 'info' })
    } else if (t.kind === 'pending') chips.push({ text: 'Describes a planned change', tone: 'warn' })
    else chips.push({ text: `${t.kind.replace('_', ' ')} ${t.value}${t.derived ? ' (from the page date)' : ''}`, tone: 'neutral' })
  }
  if (times.some((t) => t.kind === 'pending') && !times.some((t) => t.kind === 'effective')) chips.push({ text: 'No effective date stated', tone: 'warn' })
  return chips
}

const STATUS: Record<string, [Tone, string]> = {
  queued: ['neutral', 'Queued'], running: ['info', 'Running'], awaiting_approval: ['warn', 'Needs your approval'],
  needs_capability: ['warn', 'Needs a data source'], finished: ['ok', 'Finished'], stopped: ['neutral', 'Stopped'],
  failed: ['bad', 'Failed'], paused: ['neutral', 'Paused'], requested: ['neutral', 'Requested'],
  researching: ['info', 'Researching'], awaiting_user: ['warn', 'Needs your decision'], integrating: ['info', 'Integrating'],
  tests_failed: ['bad', 'Tests failed'], ready: ['ok', 'Ready'], done: ['ok', 'Done'], cancelled: ['neutral', 'Cancelled'],
}
export function StatusBadge({ status }: { status: string | null | undefined }) {
  const [tone, label] = STATUS[status ?? ''] ?? ['neutral', status ?? 'Unknown']
  return <Badge tone={tone}>{label}</Badge>
}

const VERDICT: Record<string, [Tone, string]> = {
  supported: ['ok', 'Supported'], partially_supported: ['warn', 'Partly supported'], not_supported: ['bad', 'Not supported'],
}
export function VerdictBadge({ verdict }: { verdict: string | null | undefined }) {
  const [tone, label] = VERDICT[verdict ?? ''] ?? ['neutral', 'Not checked']
  return <Badge tone={tone}>{label}</Badge>
}

const TIER: Record<string, [Tone, string]> = {
  primary: ['ok', 'Primary source'], secondary: ['neutral', 'Secondary source'], regulatory: ['ok', 'Regulatory source'],
  user_provided: ['warn', 'Your own file'],
}
export function TierBadge({ tier }: { tier: string }) {
  const [tone, label] = TIER[tier] ?? ['neutral', tier]
  return <Badge tone={tone}>{label}</Badge>
}

export function Button({ variant = 'primary', className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'danger' }) {
  const styles = {
    primary: 'bg-accent text-accent-fg hover:opacity-90',
    secondary: 'border border-line bg-surface text-fg hover:bg-surface-2',
    danger: 'border border-bad text-bad hover:bg-bad-bg',
  }[variant]
  return (
    <button
      {...props}
      className={cx('rounded-md px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50', styles, className)}
    />
  )
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  )
}

export function Loading({ what = 'Loading' }: { what?: string }) {
  return <p role="status" className="text-sm text-muted">{what}…</p>
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line p-6 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 text-sm text-muted">{children}</div>}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  const problems = error instanceof ApiError ? error.problems : [error instanceof Error ? error.message : String(error)]
  return (
    <div role="alert" className="rounded-md border border-bad bg-bad-bg p-3 text-sm text-bad">
      {problems.length === 1 ? problems[0] : <ul className="list-disc pl-5">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
    </div>
  )
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-xs break-all">{children}</code>
}

export function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline break-all">{children}</a>
}

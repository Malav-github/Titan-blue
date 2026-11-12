// A financial statement as the filing prints it: read by code (skills/statements), shown row for row. Nothing is recomputed.
import type { ReactNode } from 'react'
import type { StatementTableView } from './api'

const SAID: Record<string, string> = { verified: 'matched to the filing’s XBRL tags', agreed: 'agreed by the arithmetic', read: 'read as printed', doubt: 'in doubt' }
const TOTAL = /^(?:total|net income|net (?:cash|increase|decrease)|operating profit)/i

export function figuresText(t: StatementTableView): string {
  const parts = Object.entries(t.figures ?? {}).filter(([, n]) => n).map(([k, n]) => `${n} ${SAID[k] ?? k}`)
  const c = t.checks ?? { passed: 0, failed: 0 }
  const checks = c.passed || c.failed ? `arithmetic checks: ${c.passed} passed, ${c.failed} failed` : ''
  return [`Read by code from ${t.reading_label ?? 'the document'}`, parts.join(', '), checks].filter(Boolean).join('; ') + '.'
}

export function StatementTable({ table, source }: { table: StatementTableView; source?: ReactNode }) {
  return (
    <figure className="my-3">
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">{table.title}</caption>
          <thead className="bg-surface-2">
            <tr>
              <th scope="col" className="p-2 text-left text-xs font-normal text-muted">{table.scale ?? ''}</th>
              {table.columns.map((c, i) => (
                <th key={i} scope="col" className={`min-w-24 p-2 text-right align-bottom text-xs font-semibold ${c.asked ? 'bg-info-bg text-info' : ''}`}>
                  {c.header || c.label}{c.asked && <span className="sr-only"> (the period asked for)</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((r, i) => r.heading ? (
              <tr key={i} className="border-t border-line"><th scope="rowgroup" colSpan={table.columns.length + 1} className="p-2 pb-1 text-left font-semibold">{r.label}</th></tr>
            ) : (
              <tr key={i} className={`border-t border-line ${TOTAL.test(r.label) ? 'font-semibold' : 'font-normal'}`}>
                <th scope="row" className={`p-2 text-left ${TOTAL.test(r.label) ? 'font-semibold' : 'font-normal'}`}>{r.label}</th>
                {r.cells.map((c, j) => (
                  <td key={j} className={`whitespace-nowrap p-2 text-right tabular-nums ${c.verdict === 'doubt' ? 'bg-warn-bg text-warn' : table.columns[j]?.asked ? 'bg-info-bg' : ''}`}
                    title={c.verdict ? SAID[c.verdict] : undefined}>
                    {c.printed}{c.verdict === 'doubt' ? ' ?' : ''}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <figcaption className="mt-1 text-xs text-muted">
        {table.columns.some((c) => c.asked) && <span className="block">Shaded: {table.period}, the period asked for; the other columns are the filing’s comparatives.</span>}
        {figuresText(table)}{source ? <> {source}</> : null}
        {(table.notes ?? []).map((n) => <span key={n} className="block">Note: {n}</span>)}
      </figcaption>
    </figure>
  )
}

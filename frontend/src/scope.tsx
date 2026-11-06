// Which evidence store a page is showing: the live one (null) or an archived one (its id). Run and claim pages read it,
// so the same pages show archived evidence, read-only, under /archives/<id>/...
import { createContext, useContext, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'

const ArchiveContext = createContext<string | null>(null)

export function useArchive(): string | null {
  return useContext(ArchiveContext)
}

/** Where a claim or run of the store being shown lives in the app. */
export function useLinks() {
  const archive = useArchive()
  const base = archive ? `/archives/${archive}` : ''
  return {
    claim: (id: number | string) => `${base}/evidence/${id}`,
    run: (id: string) => `${base}/runs/${id}`,
    claims: archive ? `/archives/${archive}` : '/evidence',
  }
}

/** Wraps a page so that it shows the archive named in the address. */
export function InArchive({ children }: { children: ReactNode }) {
  const { aid = '' } = useParams()
  return <ArchiveContext.Provider value={aid}>{children}</ArchiveContext.Provider>
}

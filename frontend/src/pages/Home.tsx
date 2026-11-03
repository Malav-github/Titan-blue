import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Badge, Card, ErrorNote, Loading, PageHeader } from '../ui'

export function Home() {
  const templates = useQuery({ queryKey: ['templates'], queryFn: api.templates })
  return (
    <>
      <PageHeader
        title="What would you like to do?"
        subtitle="Each app below is a ready-made process. Every claim it produces is kept only if its quote appears word for word in the archived source."
      />
      {templates.isLoading && <Loading what="Loading apps" />}
      <ErrorNote error={templates.error} />
      <ul className="grid gap-4 sm:grid-cols-2">
        {templates.data?.map((t) => (
          <li key={t.id}>
            <Card className="flex h-full flex-col gap-3">
              <div>
                <Badge tone="info">{t.kind === 'research' ? 'Research' : 'Data source'}</Badge>
                <h2 className="mt-2 text-lg font-semibold">{t.title}</h2>
                <p className="mt-1 text-sm text-muted">{t.summary}</p>
              </div>
              <Link
                to={`/apps/${t.id}`}
                className="mt-auto self-start rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg hover:opacity-90"
              >
                Open
              </Link>
            </Card>
          </li>
        ))}
      </ul>
    </>
  )
}

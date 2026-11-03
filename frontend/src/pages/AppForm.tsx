import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, type SchemaProp, type Template } from '../api'
import { Button, Card, ErrorNote, Loading, PageHeader } from '../ui'

type Values = Record<string, string | number | boolean>

function initial(t: Template, preset: URLSearchParams): Values {
  const v: Values = {}
  for (const [key, p] of Object.entries(t.inputs.properties)) {
    const fromUrl = preset.get(key)
    if (fromUrl !== null) v[key] = fromUrl
    else if (p.default !== undefined) v[key] = p.default as string | number | boolean
    else v[key] = p.type === 'boolean' ? false : ''
  }
  return v
}

// Draws one input per schema property. The server validates; this only shows what it says.
function Field({ name, prop, required, value, onChange }: {
  name: string; prop: SchemaProp; required: boolean; value: string | number | boolean; onChange: (v: string | number | boolean) => void
}) {
  const label = prop.title ?? name
  const hint = prop.description ? <span id={`${name}-hint`} className="block text-xs text-muted">{prop.description}</span> : null
  const base = 'mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm'
  if (prop.type === 'boolean') {
    return (
      <div>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-1" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
          <span>{label}</span>
        </label>
      </div>
    )
  }
  return (
    <div>
      <label htmlFor={name} className="text-sm font-medium">
        {label}{required ? <span aria-hidden="true"> *</span> : <span className="font-normal text-muted"> (optional)</span>}
      </label>
      {hint}
      {prop['x-multiline'] ? (
        <textarea id={name} rows={3} className={base} aria-describedby={hint ? `${name}-hint` : undefined} required={required}
          value={String(value)} onChange={(e) => onChange(e.target.value)} />
      ) : prop.type === 'integer' ? (
        <input id={name} type="number" className={base} min={prop.minimum} max={prop.maximum} required={required}
          value={String(value)} onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))} />
      ) : (
        <input id={name} type="text" className={base} aria-describedby={hint ? `${name}-hint` : undefined} required={required}
          value={String(value)} onChange={(e) => onChange(e.target.value)} />
      )}
    </div>
  )
}

export function AppForm() {
  const { id = '' } = useParams()
  const [preset] = useSearchParams()
  const navigate = useNavigate()
  const templates = useQuery({ queryKey: ['templates'], queryFn: api.templates })
  const template = templates.data?.find((t) => t.id === id)
  const [values, setValues] = useState<Values | null>(null)
  const current = values ?? (template ? initial(template, preset) : null)

  const start = useMutation({
    mutationFn: (inputs: Record<string, unknown>) => api.runTemplate(id, inputs),
    onSuccess: (r) => navigate(r.kind === 'research' ? `/runs/${r.run_id}` : `/connectors/requests/${r.request_id}`),
  })

  if (templates.isLoading) return <Loading />
  if (!template || !current) return <PageHeader title="App not found" subtitle={`There is no app called “${id}”.`} />

  const submit = (e: FormEvent) => {
    e.preventDefault()
    // empty optional fields are left out so the server applies its defaults
    const inputs = Object.fromEntries(Object.entries(current).filter(([, v]) => v !== ''))
    start.mutate(inputs)
  }

  return (
    <>
      <PageHeader title={template.title} subtitle={template.summary} />
      <Card className="max-w-2xl">
        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          {Object.entries(template.inputs.properties).map(([name, prop]) => (
            <Field key={name} name={name} prop={prop} required={template.inputs.required?.includes(name) ?? false}
              value={current[name]} onChange={(v) => setValues({ ...current, [name]: v })} />
          ))}
          <ErrorNote error={start.error} />
          <div>
            <Button type="submit" disabled={start.isPending}>{start.isPending ? 'Starting…' : template.kind === 'research' ? 'Start research' : 'Start'}</Button>
          </div>
        </form>
      </Card>
    </>
  )
}

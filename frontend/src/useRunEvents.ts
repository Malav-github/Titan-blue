import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type RunEvent } from './api'
import { ACTIVE } from './format'

/** The run's history, appended to as it happens. Polls once a second while the run is working. */
export function useRunEvents(runId: string, status: string | null | undefined, archive: string | null = null): RunEvent[] {
  const [events, setEvents] = useState<RunEvent[]>([])
  const cursor = useRef(0)

  useEffect(() => {
    setEvents([])
    cursor.current = 0
  }, [runId, archive])

  const load = useCallback(async () => {
    try {
      const next = await api.runEvents(runId, cursor.current, archive)
      if (next.length) {
        cursor.current = Math.max(cursor.current, next[next.length - 1].event_id)
        // two fetches can overlap (the timer and a status change), so never add an event twice
        setEvents((prev) => {
          const seen = new Set(prev.map((e) => e.event_id))
          return [...prev, ...next.filter((e) => !seen.has(e.event_id))]
        })
      }
    } catch {
      /* the next poll retries */
    }
  }, [runId, archive])

  const working = !archive && (status === undefined || status === null || ACTIVE.has(status))   // an archive never changes
  useEffect(() => {
    void load()
    if (!working) return
    const timer = setInterval(() => void load(), 1000)
    return () => clearInterval(timer)
  }, [load, working, status])

  return events
}

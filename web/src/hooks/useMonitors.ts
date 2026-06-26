/**
 * Background-task monitors (card b6f5cc).
 *
 * Surfaces the long-running "service monitors" an agent starts — background Bash shells
 * (npm run dev, journalctl -f …) and Monitor/Workflow tasks — the same panel the terminal
 * Claude client shows. Read-only: hydrate once via REST, then live-merge {kind:"monitor"}
 * events off the project activity-stream.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from '../api'
import { Monitor } from '../types'
import { useProjectActivity } from './useProjectActivity'

export function useMonitors(projectId: string, active: boolean): {
  monitors: Monitor[]
  dismiss: (id: string) => void
} {
  // id → record, kept in a ref so live merges don't churn identity; mirrored to state for render.
  const mapRef = useRef<Map<string, Monitor>>(new Map())
  const [list, setList] = useState<Monitor[]>([])

  const flush = useCallback(() => {
    const arr = [...mapRef.current.values()]
    // running first, then most-recently-touched.
    arr.sort((a, b) => (a.status !== 'running' ? 1 : 0) - (b.status !== 'running' ? 1 : 0) || b.ts - a.ts)
    setList(arr)
  }, [])

  // Initial hydration on project switch.
  useEffect(() => {
    let cancelled = false
    mapRef.current = new Map()
    setList([])
    if (!active) return
    api.monitors(projectId)
      .then(r => {
        if (cancelled) return
        for (const m of r.monitors) mapRef.current.set(m.id, m)
        flush()
      })
      .catch(() => { /* no monitors / offline — empty is fine */ })
    return () => { cancelled = true }
  }, [projectId, active, flush])

  // Live merge.
  useProjectActivity(useCallback((evt) => {
    if (evt.kind !== 'monitor' || !evt.monitor?.id) return
    // Removal event (operator dismissed / cleared) → drop it.
    if ((evt.monitor as { removed?: boolean }).removed) {
      mapRef.current.delete(evt.monitor.id)
      flush()
      return
    }
    const cur = mapRef.current.get(evt.monitor.id)
    // Merge so a tail/status-only delta doesn't blank earlier fields.
    mapRef.current.set(evt.monitor.id, { ...cur, ...evt.monitor })
    flush()
  }, [flush]))

  // Optimistic local dismiss (also hits the API to clear the server registry + notify others).
  const dismiss = useCallback((id: string) => {
    mapRef.current.delete(id)
    flush()
    api.dismissMonitor(projectId, id).catch(() => { /* best-effort; bus will reconcile */ })
  }, [projectId, flush])

  return { monitors: list, dismiss }
}

/**
 * Background-task monitors (card b6f5cc).
 *
 * Surfaces the long-running "service monitors" an agent starts — background Bash shells
 * (npm run dev, journalctl -f …) and Monitor/Workflow tasks — the same panel the terminal
 * Claude client shows. Read-only: hydrate once via REST, then live-merge {kind:"monitor"}
 * events off the project activity-stream.
 *
 * Auto-clear: a monitor that reaches a terminal status (done/stopped/failed) used to linger
 * forever — the panel rendered every record unconditionally with no removal rule. We now
 * drop it after a short grace period (so the operator still sees the final status), keyed by
 * id so status flaps don't stack timers, and cleared on project switch so none fire stale.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from '../api'
import { Monitor } from '../types'
import { useProjectActivity } from './useProjectActivity'

const TERMINAL = new Set(['done', 'stopped', 'failed'])
const AUTO_REMOVE_MS = 5000  // grace window: show the final status, then the row clears itself

export function useMonitors(projectId: string, active: boolean): {
  monitors: Monitor[]
  dismiss: (id: string) => void
} {
  // id → record, kept in a ref so live merges don't churn identity; mirrored to state for render.
  const mapRef = useRef<Map<string, Monitor>>(new Map())
  // id → pending auto-remove timer (terminal-status monitors only).
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const [list, setList] = useState<Monitor[]>([])

  const flush = useCallback(() => {
    const arr = [...mapRef.current.values()]
    // running first, then most-recently-touched.
    arr.sort((a, b) => (a.status !== 'running' ? 1 : 0) - (b.status !== 'running' ? 1 : 0) || b.ts - a.ts)
    setList(arr)
  }, [])

  const clearTimer = useCallback((id: string) => {
    const t = timersRef.current.get(id)
    if (t) { clearTimeout(t); timersRef.current.delete(id) }
  }, [])

  // Arm (or re-arm) the auto-remove timer for one monitor. Re-arming always clears the previous
  // timer first, so a status that flaps running→done→running cancels a pending removal.
  const armAutoRemove = useCallback((id: string, status: string) => {
    clearTimer(id)
    if (!TERMINAL.has(status)) return
    timersRef.current.set(id, setTimeout(() => {
      timersRef.current.delete(id)
      mapRef.current.delete(id)
      flush()
    }, AUTO_REMOVE_MS))
  }, [clearTimer, flush])

  // Initial hydration on project switch.
  useEffect(() => {
    let cancelled = false
    mapRef.current = new Map()
    // Cancel timers from the previous project so they can't fire against the new map.
    for (const t of timersRef.current.values()) clearTimeout(t)
    timersRef.current = new Map()
    setList([])
    if (!active) return
    api.monitors(projectId)
      .then(r => {
        if (cancelled) return
        for (const m of r.monitors) {
          mapRef.current.set(m.id, m)
          armAutoRemove(m.id, m.status)  // a registry that hydrates an already-done monitor still clears it
        }
        flush()
      })
      .catch(() => { /* no monitors / offline — empty is fine */ })
    return () => {
      cancelled = true
      for (const t of timersRef.current.values()) clearTimeout(t)
      timersRef.current.clear()
    }
  }, [projectId, active, flush, armAutoRemove])

  // Live merge.
  useProjectActivity(useCallback((evt) => {
    if (evt.kind !== 'monitor' || !evt.monitor?.id) return
    const id = evt.monitor.id
    // Removal event (operator dismissed / cleared) → drop it.
    if ((evt.monitor as { removed?: boolean }).removed) {
      clearTimer(id)
      mapRef.current.delete(id)
      flush()
      return
    }
    const cur = mapRef.current.get(id)
    // Merge so a tail/status-only delta doesn't blank earlier fields.
    const merged = { ...cur, ...evt.monitor } as Monitor
    mapRef.current.set(id, merged)
    armAutoRemove(id, merged.status)
    flush()
  }, [flush, clearTimer, armAutoRemove]))

  // Optimistic local dismiss (also hits the API to clear the server registry + notify others).
  const dismiss = useCallback((id: string) => {
    clearTimer(id)
    mapRef.current.delete(id)
    flush()
    api.dismissMonitor(projectId, id).catch(() => { /* best-effort; bus will reconcile */ })
  }, [projectId, flush, clearTimer])

  return { monitors: list, dismiss }
}

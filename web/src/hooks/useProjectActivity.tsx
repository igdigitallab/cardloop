/**
 * Single SSE connection per project (provider in ProjectView), all tabs subscribe via hooks.
 *
 * Why: each tab used to open its own fetch+ReadableStream on /activity-stream.
 * 7 tabs = 7 sockets per open project. Via context — one per project tab.
 *
 * Usage:
 *   <ProjectActivityProvider projectId={p.id}>  ← in ProjectView
 *     ...
 *     useOnRunEnd(() => reload())               ← in each tab/section
 */
import {
  createContext, useContext, useEffect, useRef, useCallback, useMemo, ReactNode,
} from 'react'
import { ActivityEvent } from '../types'

type Handler = (evt: ActivityEvent) => void

interface BusValue {
  /** Subscribe to ALL bus events. Returns unsubscribe. */
  subscribe: (h: Handler) => () => void
  /** Spec-035 L2: seed the SSE cursor so reconnects skip already-applied events. */
  seedCursor: (seq: number) => void
}

const BusContext = createContext<BusValue | null>(null)

interface ProviderProps {
  projectId: string
  /**
   * Hold the SSE connection only while true (default). Hidden project tabs
   * (display:none slots) MUST pass false: browsers cap HTTP/1.1 at ~6
   * connections per origin, so N mounted tabs × 1 stream each + the global
   * EventSource exhaust the pool and every later fetch queues forever
   * (symptom: Board stuck on "Loading…"). HTTP/2 multiplexing hides this
   * in production, but plain-HTTP setups deadlock.
   */
  active?: boolean
  children: ReactNode
}

export function ProjectActivityProvider({ projectId, active = true, children }: ProviderProps) {
  // Set of active subscribers (mutable ref — no re-render on (un)subscribe)
  const handlersRef = useRef<Set<Handler>>(new Set())
  // True once we have been inactive — used to fire a catch-up event on return.
  const wasInactiveRef = useRef(false)
  // Spec-035 L2: track the highest seq seen so far — used as ?since= on reconnect to
  // avoid replaying events the client already processed. -1 means "no seq seen yet".
  const lastSeqRef = useRef<number>(-1)

  const subscribe = useCallback((h: Handler) => {
    handlersRef.current.add(h)
    return () => { handlersRef.current.delete(h) }
  }, [])

  // Allow external code (e.g. ChatTab after /live hydration) to seed the cursor so
  // the first connect does not replay events already applied from the snapshot.
  const seedCursor = useCallback((seq: number) => {
    if (seq > lastSeqRef.current) lastSeqRef.current = seq
  }, [])

  // Single SSE connection per projectId, held only while `active`.
  // Reconnects after 2s on disconnect.
  useEffect(() => {
    if (!active) {
      wasInactiveRef.current = true
      return
    }
    if (wasInactiveRef.current) {
      // Events were missed while the stream was down — nudge subscribers to
      // re-fetch (useOnRunEnd treats run_end as "refresh now"; ChatTab ignores
      // foreign run_end via its busActiveRef gate).
      wasInactiveRef.current = false
      const catchUp: ActivityEvent = { kind: 'run_end', outcome: 'ok', run_id: 'sse-catch-up' }
      for (const h of handlersRef.current) {
        try { h(catchUp) } catch { /* subscriber must not crash the bus */ }
      }
    }
    const ac = new AbortController()
    let alive = true

    async function connect() {
      while (alive) {
        try {
          // Spec-035 L2: pass ?since= cursor so the server replays only the gap.
          // lastSeqRef.current stays -1 until we see a seq-tagged event, at which point
          // reconnects will skip already-processed events.
          const since = lastSeqRef.current >= 0 ? `?since=${lastSeqRef.current}` : ''
          const res = await fetch(`/api/projects/${projectId}/activity-stream${since}`, {
            credentials: 'include',
            signal: ac.signal,
          })
          if (!res.ok || !res.body) {
            await new Promise(r => setTimeout(r, 2000))
            continue
          }
          const reader = res.body.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          while (alive) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })
            const lines = buf.split('\n')
            buf = lines.pop() ?? ''
            for (const ln of lines) {
              if (!ln.startsWith('data: ')) continue
              try {
                const evt = JSON.parse(ln.slice(6)) as ActivityEvent
                // Track highest seq for reconnect cursor
                const seq = (evt as unknown as Record<string, unknown>).seq
                if (typeof seq === 'number' && seq > lastSeqRef.current) {
                  lastSeqRef.current = seq
                }
                for (const h of handlersRef.current) {
                  try { h(evt) } catch { /* subscriber must not crash the bus */ }
                }
              } catch { /* skip malformed JSON / heartbeat */ }
            }
          }
        } catch (err: unknown) {
          const name = (err as { name?: string })?.name
          if (!alive || name === 'AbortError') break
          await new Promise(r => setTimeout(r, 2000))
        }
      }
    }
    connect()
    return () => { alive = false; ac.abort() }
  }, [projectId, active])

  // Memoize the context value so the object identity is stable between re-renders.
  // subscribe and seedCursor are already stable useCallbacks, but a plain object literal
  // would be recreated on every render — causing seedCursor identity to flip downstream
  // and triggering ChatTab's hydration effect (which aborts the live /chat SSE stream).
  const value = useMemo(() => ({ subscribe, seedCursor }), [subscribe, seedCursor])

  return (
    <BusContext.Provider value={value}>
      {children}
    </BusContext.Provider>
  )
}

/** Spec-035 L2: seed the SSE reconnect cursor (call after /live hydration). */
// eslint-disable-next-line react-refresh/only-export-components -- hooks + provider co-located by design
export function useSeedCursor(): (seq: number) => void {
  const ctx = useContext(BusContext)
  return useCallback((seq: number) => ctx?.seedCursor(seq), [ctx])
}

/** Subscribe to ALL bus events. handler may be unstable (we use a ref internally). */
// eslint-disable-next-line react-refresh/only-export-components -- hooks + provider co-located by design
export function useProjectActivity(handler: Handler) {
  const ctx = useContext(BusContext)
  const handlerRef = useRef(handler)
  useEffect(() => { handlerRef.current = handler }, [handler])

  useEffect(() => {
    if (!ctx) return
    return ctx.subscribe(evt => handlerRef.current(evt))
  }, [ctx])
}

/** Convenience hook: calls callback on every run_end from the bus. */
// eslint-disable-next-line react-refresh/only-export-components -- hooks + provider co-located by design
export function useOnRunEnd(callback: () => void) {
  useProjectActivity(evt => {
    if (evt.kind === 'run_end') callback()
  })
}

/** Hook: refresh on focus/visibility return + optionally on a polling interval. */
// eslint-disable-next-line react-refresh/only-export-components -- hooks + provider co-located by design
export function useFocusRefresh(callback: () => void, pollMs?: number) {
  const cbRef = useRef(callback)
  useEffect(() => { cbRef.current = callback }, [callback])

  useEffect(() => {
    const onFocus = () => cbRef.current()
    const onVis = () => { if (document.visibilityState === 'visible') cbRef.current() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVis)
    let id: ReturnType<typeof setInterval> | null = null
    if (pollMs && pollMs > 0) {
      id = setInterval(() => {
        if (document.visibilityState === 'visible') cbRef.current()
      }, pollMs)
    }
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVis)
      if (id) clearInterval(id)
    }
  }, [pollMs])
}

/** spec-094 — the ONE store behind the top-bar load meter.
 *
 *  Same rule as runtimeStatus.ts: every consumer reads this polled snapshot, nobody fetches
 *  /api/system-load on its own. The pure half (reduceLoad / viewOf / litSegments) carries the
 *  logic that must not lie, and is unit-tested without React (loadStatus.test.ts).
 *
 *  The one thing this module must get right is telling "the server is overloaded" from "the
 *  server is not answering" from "you are signed out": an expired cookie is NOT a dead cockpit,
 *  and a green meter over a frozen server is the worst possible lie. */
import { useSyncExternalStore } from 'react'
import { api, type SystemLoad } from '../api'

export type LoadKind = 'off' | 'loading' | 'signedout' | 'down' | 'stale' | 'unknown' | 'ok' | 'warn' | 'crit'
export type LoadView = { kind: LoadKind; data: SystemLoad | null }

export interface LoadState {
  data: SystemLoad | null
  /** Browser clock (ms) when `data` arrived. */
  fetchedAt: number
  /** Consecutive failed polls that were NOT an auth/feature-off answer. */
  failures: number
  status: number | null
  /** Consecutive 404s. One is a proxy hiccup or a deploy; only a run of them means "feature off". */
  notFound: number
  off: boolean
}

export type LoadEvent =
  | { type: 'ok'; data: SystemLoad; at: number }
  | { type: 'error'; status: number | null }

export const INITIAL_LOAD: LoadState = { data: null, fetchedAt: 0, failures: 0, status: null, notFound: 0, off: false }

/** Two failed polls (~20 s at the default cadence) before we say "no response". */
export const DOWN_AFTER = 2
/** Consecutive 404s before the meter hides itself. */
export const OFF_AFTER = 3
/** The server says its own sampler is older than this -> the numbers are stale. */
export const STALE_SERVER_S = 20
/** No successful poll for this long (one missed poll tolerated) -> stale. This also covers a tab
 *  waking after a long pause: polling was suspended, so what we hold really IS old, and the honest
 *  answer is "stale" until the first poll lands — never the last green reading. */
export const STALE_CLIENT_MS = 35_000

export function reduceLoad(s: LoadState, e: LoadEvent): LoadState {
  switch (e.type) {
    case 'ok':
      return { data: e.data, fetchedAt: e.at, failures: 0, status: 200, notFound: 0, off: false }
    case 'error':
      if (e.status === 404) {
        const notFound = s.notFound + 1
        return { ...s, notFound, off: notFound >= OFF_AFTER, failures: 0, status: 404 }
      }
      if (e.status === 401) return { ...s, status: 401, notFound: 0 }                 // signed out
      return { ...s, failures: s.failures + 1, status: e.status, notFound: 0 }
  }
}

export function viewOf(s: LoadState, nowMs: number): LoadView {
  if (s.off) return { kind: 'off', data: null }
  if (s.status === 401) return { kind: 'signedout', data: null }
  if (s.failures >= DOWN_AFTER) return { kind: 'down', data: s.data }
  if (!s.data) return { kind: 'loading', data: null }
  const d = s.data
  if ((d.age_s != null && d.age_s > STALE_SERVER_S) || nowMs - s.fetchedAt > STALE_CLIENT_MS) {
    return { kind: 'stale', data: d }
  }
  if (d.error) return { kind: 'unknown', data: d }          // the sampler is failing: say so
  if (d.warming_up) return { kind: 'loading', data: d }
  return { kind: d.level, data: d }
}

/** LED-bar segments, bottom to top: the lit count follows the worst signal's pressure, and the
 *  zone colours line up with the verdict (green below the warn line, amber up to crit, red at it). */
export const SEGMENTS: { zone: 'ok' | 'warn' | 'crit'; at: number }[] = [
  { zone: 'ok', at: 0.0001 }, { zone: 'ok', at: 0.25 },
  { zone: 'warn', at: 0.5 }, { zone: 'warn', at: 0.75 },
  { zone: 'crit', at: 1.0 },
]

export function litSegments(kind: LoadKind, score: number): number {
  if (kind !== 'ok' && kind !== 'warn' && kind !== 'crit' && kind !== 'stale') return 0
  const p = Math.max(0, Math.min(100, score)) / 100
  return Math.max(1, SEGMENTS.filter(s => p >= s.at).length)
}

export function verdict(v: LoadView): string {
  if (v.data?.error && v.kind === 'unknown') return 'Load monitor is failing'
  switch (v.kind) {
    case 'ok': return 'Server load: normal'
    case 'warn': return 'Server load: elevated'
    case 'crit': return 'Server overloaded'
    case 'stale': return 'Load data is stale'
    case 'down': return 'Cockpit is not responding'
    case 'signedout': return 'Signed out — load unavailable'
    case 'unknown': return 'Load: nothing measurable on this host'
    default: return 'Load: measuring…'
  }
}

// ── store ──────────────────────────────────────────────────────────────────────────────────
const POLL_MS = 10_000
const REQUEST_TIMEOUT_MS = 3_000
const TICK_MS = 5_000

let state: LoadState = INITIAL_LOAD
let view: LoadView = viewOf(state, Date.now())
const listeners = new Set<() => void>()
let timers: ReturnType<typeof setInterval>[] = []
let inFlight = false

/** Re-derive the view; hand subscribers a NEW object only when something visible changed, so a
 *  steady meter does not re-render every host component every few seconds. */
function refreshView() {
  const next = viewOf(state, Date.now())
  const same = next.kind === view.kind && next.data?.at === view.data?.at
    && next.data?.score === view.data?.score && next.data?.chats.live === view.data?.chats.live
  if (same) return
  view = next
  listeners.forEach(l => l())
}

function dispatch(e: LoadEvent) {
  state = reduceLoad(state, e)
  refreshView()
}

let offTicks = 0

async function poll(force = false) {
  if (inFlight) return
  // While the feature looks switched off keep probing, slowly (every ~60 s): re-enabling it
  // server-side, or a proxy that answered 404 for a moment, must not need a page reload.
  if (state.off && !force && ++offTicks % 6 !== 0) return
  inFlight = true
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS)
  try {
    const data = await api.systemLoad(ctl.signal)
    dispatch({ type: 'ok', data, at: Date.now() })
  } catch (e) {
    const status = (e as { status?: number }).status ?? null
    dispatch({ type: 'error', status })
  } finally {
    clearTimeout(timer)
    inFlight = false
  }
}

function onWake() {
  if (document.hidden) return
  refreshView()          // what we hold may be old after a pause: show that, then refresh it
  void poll(true)
}

function start() {
  void poll(true)
  timers = [
    setInterval(() => { if (!document.hidden) void poll() }, POLL_MS),
    setInterval(refreshView, TICK_MS),          // staleness is a function of time, not of events
  ]
  window.addEventListener('focus', onWake)
  document.addEventListener('visibilitychange', onWake)
}

function stop() {
  timers.forEach(clearInterval)
  timers = []
  window.removeEventListener('focus', onWake)
  document.removeEventListener('visibilitychange', onWake)
}

function subscribe(l: () => void) {
  listeners.add(l)
  if (listeners.size === 1) start()
  return () => {
    listeners.delete(l)
    if (listeners.size === 0) stop()
  }
}

export function getLoadView(): Readonly<LoadView> {
  return view
}

export function useLoadStatus(): LoadView {
  return useSyncExternalStore(subscribe, () => view)
}

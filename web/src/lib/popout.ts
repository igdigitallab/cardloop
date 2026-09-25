/**
 * Pop-out project windows — one project per OS window, for spreading the cockpit across
 * monitors.
 *
 * `/?popout=<projectId>[&tab=<TabId>]` boots a stripped cockpit (PopoutApp) that shows ONE
 * project: its left pane (browser / board / files …) and its chat, with the same draggable
 * divider as the main window, and nothing else. main.tsx branches on the URL before <App/>
 * mounts, so the main window's code path does not change.
 *
 * What a pop-out deliberately does NOT do, each for a concrete reason:
 *  - No global /api/activity-stream. On a plain-HTTP origin every window of the browser
 *    profile shares ONE HTTP/1.1 pool of 6 connections per host; the main window already
 *    holds that stream and owns the chime, toasts and unread badges.
 *  - No ui_state hydrate/save. The server keeps a single "default" layout; a pop-out saving
 *    its one-project layout would wipe the main window's open tabs.
 *  - Its own localStorage keys for the chat width / collapse / max state (see popoutKey), so
 *    dragging the divider here does not move the main window's.
 */
import { TabId } from '../types'

export const POPOUT_PARAM = 'popout'

const POPOUT_TABS: readonly TabId[] = [
  'agents', 'logs', 'board', 'files', 'memory', 'timeline', 'settings', 'specs', 'browser',
]

export interface PopoutParams {
  projectId: string
  /** Inner tab to land on; null when absent or not a known tab id. */
  tab: TabId | null
}

/** Parse `location.search`; null when this is not a pop-out window. */
export function parsePopoutParams(search: string): PopoutParams | null {
  const p = new URLSearchParams(search)
  const projectId = (p.get(POPOUT_PARAM) || '').trim()
  if (!projectId) return null
  const tab = p.get('tab')
  return {
    projectId,
    tab: tab && (POPOUT_TABS as readonly string[]).includes(tab) ? (tab as TabId) : null,
  }
}

export function popoutUrl(projectId: string, tab?: TabId | null): string {
  const p = new URLSearchParams({ [POPOUT_PARAM]: projectId })
  if (tab) p.set('tab', tab)
  return `/?${p.toString()}`
}

const WINDOW_NAME_PREFIX = 'cardloop-popout-'

/** One window per project: the name is what lets a second click find the first window. */
export function popoutWindowName(projectId: string): string {
  return `${WINDOW_NAME_PREFIX}${projectId.replace(/[^A-Za-z0-9_-]/g, '_')}`
}

/** True in a window opened by openProjectWindow (window.name survives the navigation). */
export function isPopoutWindow(name: string = window.name): boolean {
  return name.startsWith(WINDOW_NAME_PREFIX)
}

/** localStorage key a pop-out uses instead of the main window's `key`. */
export function popoutKey(key: string): string {
  return `${key}.popout`
}

// ── Remembered window bounds (per project) ─────────────────────────────────────

export interface WindowBounds {
  /** Screen position of the window (screenX / screenY). */
  left: number
  top: number
  /** Content size (innerWidth / innerHeight) — what window.open's width/height mean. */
  width: number
  height: number
}

const DEFAULT_SIZE = { width: 1400, height: 900 }
const MIN_W = 480
const MIN_H = 360

const boundsKey = (projectId: string) => `cops.popout.bounds.${projectId}`

export function parseBounds(raw: string | null): WindowBounds | null {
  if (!raw) return null
  try {
    const b = JSON.parse(raw) as Partial<WindowBounds>
    const nums = [b.left, b.top, b.width, b.height]
    if (!nums.every(n => typeof n === 'number' && Number.isFinite(n))) return null
    if ((b.width as number) < MIN_W || (b.height as number) < MIN_H) return null
    return { left: b.left!, top: b.top!, width: b.width!, height: b.height! }
  } catch {
    return null
  }
}

export function readBounds(projectId: string): WindowBounds | null {
  try { return parseBounds(localStorage.getItem(boundsKey(projectId))) } catch { return null }
}

export function saveBounds(projectId: string, b: WindowBounds): void {
  try { localStorage.setItem(boundsKey(projectId), JSON.stringify(b)) } catch { /* private mode */ }
}

/** window.open feature string: last remembered place and size, or a default size. */
export function windowFeatures(b: WindowBounds | null): string {
  const size = b ?? DEFAULT_SIZE
  const parts = ['popup', `width=${Math.round(size.width)}`, `height=${Math.round(size.height)}`]
  if (b) parts.push(`left=${Math.round(b.left)}`, `top=${Math.round(b.top)}`)
  return parts.join(',')
}

/** True when the window sits noticeably away from where it was last closed. */
export function isMisplaced(saved: WindowBounds, left: number, top: number): boolean {
  return Math.abs(left - saved.left) > 60 || Math.abs(top - saved.top) > 60
}

/**
 * Open (or bring forward) the pop-out window for a project. Returns false when the browser
 * blocked the popup.
 */
export function openProjectWindow(projectId: string, tab?: TabId | null): boolean {
  // window.open(url, name) on a window that is ALREADY open navigates it — the pop-out would
  // reload and drop its live browser stream and the turn it is showing. Opening by name with
  // an empty URL returns an existing window untouched, or a fresh about:blank one that we
  // then point at the pop-out.
  const w = window.open('', popoutWindowName(projectId), windowFeatures(readBounds(projectId)))
  if (!w) return false
  let fresh = false
  try { fresh = w.location.href === 'about:blank' } catch { fresh = false }
  if (fresh) w.location.href = popoutUrl(projectId, tab)
  w.focus()
  return true
}

// ── Multi-monitor placement (Window Management API, Chrome) ───────────────────
// Chrome may clamp a window to the opener's screen unless the origin holds the
// "window-management" permission; the pop-out asks for it only when that clamp actually
// moved it away from its remembered monitor.

export type PlacementState = 'granted' | 'denied' | 'prompt' | 'unsupported'

type ScreenDetailsWindow = Window & { getScreenDetails?: () => Promise<unknown> }

export async function placementState(): Promise<PlacementState> {
  if (typeof (window as ScreenDetailsWindow).getScreenDetails !== 'function') return 'unsupported'
  try {
    const st = await navigator.permissions.query({ name: 'window-management' as PermissionName })
    return st.state as PlacementState
  } catch {
    return 'unsupported'
  }
}

/** Ask for the permission (shows Chrome's prompt once). Needs a user gesture to be safe. */
export async function requestPlacement(): Promise<boolean> {
  const w = window as ScreenDetailsWindow
  if (typeof w.getScreenDetails !== 'function') return false
  try {
    await w.getScreenDetails()
    return (await placementState()) === 'granted'
  } catch {
    return false
  }
}

/** Move/resize THIS window (allowed for a script-opened popup) to the remembered bounds. */
export function applyBounds(b: WindowBounds): void {
  try {
    window.moveTo(b.left, b.top)
    const frameW = window.outerWidth - window.innerWidth
    const frameH = window.outerHeight - window.innerHeight
    window.resizeTo(b.width + frameW, b.height + frameH)
  } catch { /* not a popup, or the browser refused */ }
}

/**
 * Keep the remembered bounds current (there is no "move" event, so poll cheaply).
 * `armed=false` holds saving until the window has moved off the position it first landed
 * on — otherwise a window Chrome clamped onto the wrong monitor would overwrite the
 * remembered monitor within two seconds.
 */
export function trackBounds(projectId: string, armed: boolean): () => void {
  const landed = { left: window.screenX, top: window.screenY }
  let last = ''
  const snap = () => {
    if (!armed) {
      if (window.screenX === landed.left && window.screenY === landed.top) return
      armed = true
    }
    const b: WindowBounds = {
      left: window.screenX, top: window.screenY,
      width: window.innerWidth, height: window.innerHeight,
    }
    const key = JSON.stringify(b)
    if (key === last) return
    last = key
    if (b.width >= MIN_W && b.height >= MIN_H) saveBounds(projectId, b)
  }
  const id = window.setInterval(snap, 2000)
  window.addEventListener('pagehide', snap)
  return () => {
    window.clearInterval(id)
    window.removeEventListener('pagehide', snap)
  }
}

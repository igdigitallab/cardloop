import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from './api'
import { Project } from './types'
import { t } from './i18n'
import { LoginScreen } from './components/LoginScreen'
import { Sidebar } from './components/Sidebar'
import { ProjectView } from './components/ProjectView'
import { ProjectTabBar } from './components/ProjectTabBar'
import { Spinner } from './components/Spinner'
import { GlobalFilesTab } from './tabs/GlobalFilesTab'
import { SchedulesTab } from './tabs/SchedulesTab'
import { VaultTab } from './tabs/VaultTab'
import { TerminalTab } from './tabs/TerminalTab'
import { GlobalSettingsTab } from './tabs/GlobalSettingsTab'
import { useToast, ToastContainer } from './components/Toast'
import { useUnreadTracker } from './hooks/useUnreadTracker'
import { useTheme } from './hooks/useTheme'
import { useNotifications } from './hooks/useNotifications'

const GLOBAL_FILES_ID = '__global__'
const SCHEDULES_ID = '__schedules__'
const VAULT_ID = '__vault__'
const TERMINAL_ID = '__terminal__'
const SETTINGS_ID = '__settings__'

type AuthState = 'loading' | 'unauthed' | 'authed'

const LS_SIDEBAR_COLLAPSED = 'cops.sidebarCollapsed'
const LS_OPEN = 'cops.openProjects'
const LS_ACTIVE = 'cops.activeProject'
type MobileScreen = 'list' | 'project'
const LS_SPLIT_PAIRS = 'cops.splitPairs'
const LS_SPLIT_WIDTH = 'cops.splitWidth'
const LS_SIDEBAR_ORDER = 'cops.sidebarOrder'

function readSidebarOrder(): string[] {
  try {
    const raw = localStorage.getItem(LS_SIDEBAR_ORDER)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []
  } catch { return [] }
}


function readBool(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v === null) return fallback
    return v === 'true'
  } catch {
    return fallback
  }
}

function readStringList(key: string): string[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function readString(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function readSplitPairs(): Record<string, string> {
  try {
    const raw = localStorage.getItem(LS_SPLIT_PAIRS)
    if (!raw) return {}
    const obj = JSON.parse(raw)
    return (obj && typeof obj === 'object' && !Array.isArray(obj)) ? obj : {}
  } catch { return {} }
}

function readSplitWidth(): number {
  try {
    const v = localStorage.getItem(LS_SPLIT_WIDTH)
    if (v) { const n = parseFloat(v); if (!isNaN(n)) return Math.max(20, Math.min(80, n)) }
  } catch {}
  return 50
}

export default function App() {
  const [theme, setTheme] = useTheme()
  const { toasts, showToast, dismiss } = useToast()
  const [authState, setAuthState] = useState<AuthState>('loading')
  // Mobile navigation: 'list' = project list screen, 'project' = project detail screen
  const [mobileScreen, setMobileScreen] = useState<MobileScreen>(() => {
    const active = readString(LS_ACTIVE)
    return active ? 'project' : 'list'
  })
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [openIds, setOpenIds] = useState<string[]>(() => readStringList(LS_OPEN))
  const [activeId, setActiveId] = useState<string | null>(() => readString(LS_ACTIVE))
  const [sidebarOrder, setSidebarOrder] = useState<string[]>(() => readSidebarOrder())
  const { unreadBySession, incrementUnread, clearUnreadForSession, resetUnread } = useUnreadTracker()
  // Browser notifications: opt-in desktop alerts for background run completions (notifications A)
  const { notifyRunEnd } = useNotifications()
  // Reply-ready: project IDs where the agent finished a run while the tab was not active
  const [replyReadyIds, setReplyReadyIds] = useState<Set<string>>(() => new Set())
  // ops:b2a081 — project IDs where an agent turn is currently in flight (working indicator)
  const [runningIds, setRunningIds] = useState<Set<string>>(() => new Set())
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => readBool(LS_SIDEBAR_COLLAPSED, false))
  // Request to open a project's Settings tab (from sidebar context menu). nonce bumps so
  // repeat requests for an already-open project still fire the effect in ProjectView.
  const [settingsRequest, setSettingsRequest] = useState<{ id: string; nonce: number } | null>(null)
  // Off-canvas drawer (mobile/tablet ≤1024px): not persisted, default closed
  const [drawerOpen, setDrawerOpen] = useState(false)
  // Split-view: leftId → rightId (free chats only)
  const [splitPairs, setSplitPairs] = useState<Record<string, string>>(() => readSplitPairs())
  const [splitWidth, setSplitWidth] = useState<number>(() => readSplitWidth())
  // Global file browser (persisted in localStorage)
  const [globalFilesOpen, setGlobalFilesOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('cops.globalFilesOpen') === 'true' } catch { return false }
  })
  // Schedules tab (global)
  const [schedulesOpen, setSchedulesOpen] = useState<boolean>(false)
  // Vault tab (global)
  const [vaultOpen, setVaultOpen] = useState<boolean>(false)
  // Terminal tab (global)
  const [terminalOpen, setTerminalOpen] = useState<boolean>(false)
  // Global settings tab (global)
  const [settingsGlobalOpen, setSettingsGlobalOpen] = useState<boolean>(false)
  // Live model registry (fetched once after auth). undefined until loaded → ChatTab
  // falls back to the bundled static MODELS so the picker renders instantly / offline.
  const [models, setModels] = useState<{ value: string; label: string }[] | undefined>(undefined)
  // Current active project — for SSE handler, no re-subscription on every select
  const activeIdRef = useRef<string | null>(null)
  const projectsRef = useRef<Project[]>([])
  // After first successful load, don't show "Loading..." on background polls
  const projectsLoadedRef = useRef(false)
  // Cross-device UI layout: server is the source of truth (data/ui_state.json).
  // localStorage serves as an instant cache (no flash); server syncs on top.
  const uiHydratedRef = useRef(false)
  const uiSaveTimer = useRef<number | null>(null)
  // Stable ref for handleSelect — allows connectActivityStream (defined before handleSelect)
  // to call the most current version without being re-subscribed on every render. Same pattern as activeIdRef.
  const handleSelectRef = useRef<(id: string) => void>(() => { /* initialized after handleSelect is defined */ })
  // Stable ref for notifyRunEnd — avoids adding the hook result to connectActivityStream deps.
  const notifyRunEndRef = useRef(notifyRunEnd)

  const checkAuth = useCallback(async () => {
    try {
      const res = await api.me()
      setAuthState(res.authed ? 'authed' : 'unauthed')
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      setAuthState(status === 401 ? 'unauthed' : 'unauthed')
    }
  }, [])

  const loadProjects = useCallback(async () => {
    // Loading flag only on first load — otherwise the sidebar flashes "Loading..."
    // on every background poll (every 15s, on focus, on run_end)
    if (!projectsLoadedRef.current) setProjectsLoading(true)
    try {
      const res = await api.projects()
      // Stable comparison: skip state update when data hasn't changed
      // (prevents cascading effects on openIds/sidebarOrder/activeId)
      setProjects(prev => {
        // Stable comparison: avoid cascading effects when data hasn't changed.
        // Compare by id+model+health fields that drive sidebar/header rendering.
        const same =
          prev.length === res.projects.length &&
          prev.every((p, i) => {
            const n = res.projects[i]
            return (
              p.id === n.id &&
              p.name === n.name &&
              p.model === n.model &&
              p.incidents === n.incidents &&
              p.group === n.group &&
              p.favorite === n.favorite &&
              p.health.git?.branch === n.health.git?.branch &&
              p.health.git?.dirty === n.health.git?.dirty &&
              p.health.git?.unpushed === n.health.git?.unpushed
            )
          })
        return same ? prev : res.projects
      })
      // ops:b2a081 — sync running/awaiting sets from backend on every poll.
      // This makes the indicators refresh-durable: after a page reload the backend
      // knows the real state and we restore it here without needing SSE history.
      const backendRunning = new Set(res.projects.filter(p => p.running).map(p => p.id))
      const backendAwaiting = new Set(res.projects.filter(p => p.awaiting).map(p => p.id))
      setRunningIds(prev => {
        const same = prev.size === backendRunning.size && [...prev].every(id => backendRunning.has(id))
        return same ? prev : backendRunning
      })
      setReplyReadyIds(prev => {
        // Merge: keep locally known reply-ready ids PLUS backend-known ones
        // (SSE is the primary source, backend poll is the refresh-durability fallback)
        const merged = new Set([...prev, ...backendAwaiting])
        const same = merged.size === prev.size && [...merged].every(id => prev.has(id))
        return same ? prev : merged
      })
      projectsLoadedRef.current = true
    } catch {
      // A transient /api/projects failure must not unmount the whole UI — keep the
      // previously-loaded list so open project tabs and in-flight chat streams survive.
      // First-load behavior is unaffected: projects starts as [] and the Welcome screen
      // shows until the first successful poll.
    } finally {
      setProjectsLoading(false)
    }
  }, [])

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  useEffect(() => {
    if (authState === 'authed') {
      loadProjects()
    }
  }, [authState, loadProjects])

  // Fetch the live model registry once after auth. On failure, leave undefined →
  // ChatTab uses the bundled static MODELS fallback.
  useEffect(() => {
    if (authState !== 'authed') return
    api.models()
      .then(res => setModels(res.models))
      .catch(() => { /* keep static fallback */ })
  }, [authState])

  // Keep refs up-to-date (needed by SSE handler without re-subscription)
  useEffect(() => { activeIdRef.current = activeId }, [activeId])
  useEffect(() => { projectsRef.current = projects }, [projects])
  // notifyRunEndRef is updated whenever the hook's internal enabled/permission state changes
  useEffect(() => { notifyRunEndRef.current = notifyRunEnd }, [notifyRunEnd])

  // Persist openIds + activeId + split state
  useEffect(() => {
    try { localStorage.setItem(LS_OPEN, JSON.stringify(openIds)) } catch {}
  }, [openIds])
  useEffect(() => {
    try {
      if (activeId) localStorage.setItem(LS_ACTIVE, activeId)
      else localStorage.removeItem(LS_ACTIVE)
    } catch {}
  }, [activeId])
  useEffect(() => {
    try { localStorage.setItem(LS_SPLIT_PAIRS, JSON.stringify(splitPairs)) } catch {}
  }, [splitPairs])
  useEffect(() => {
    try { localStorage.setItem(LS_SPLIT_WIDTH, String(splitWidth)) } catch {}
  }, [splitWidth])
  useEffect(() => {
    try { localStorage.setItem(LS_SIDEBAR_ORDER, JSON.stringify(sidebarOrder)) } catch {}
  }, [sidebarOrder])
  useEffect(() => {
    try { localStorage.setItem('cops.globalFilesOpen', String(globalFilesOpen)) } catch {}
  }, [globalFilesOpen])

  // ── Cross-device sync: hydrate layout from server (source of truth) ─────────
  // localStorage already seeded state (no flash); server syncs on top.
  // Apply only present keys — an empty server state does not wipe anything.
  useEffect(() => {
    if (authState !== 'authed') return
    let cancelled = false
    ;(async () => {
      try {
        const { state } = await api.uiState()
        if (!cancelled && state && typeof state === 'object') {
          if (Array.isArray(state.open))
            setOpenIds((state.open as unknown[]).filter((x): x is string => typeof x === 'string'))
          if (typeof state.active === 'string' || state.active === null)
            setActiveId(state.active as string | null)
          if (Array.isArray(state.sidebarOrder))
            setSidebarOrder((state.sidebarOrder as unknown[]).filter((x): x is string => typeof x === 'string'))
          if (state.splitPairs && typeof state.splitPairs === 'object' && !Array.isArray(state.splitPairs))
            setSplitPairs(state.splitPairs as Record<string, string>)
          if (typeof state.splitWidth === 'number')
            setSplitWidth(Math.max(20, Math.min(80, state.splitWidth)))
          if (typeof state.globalFilesOpen === 'boolean')
            setGlobalFilesOpen(state.globalFilesOpen)
        }
      } catch {
        // no server state / offline — continue with local layout
      } finally {
        if (!cancelled) uiHydratedRef.current = true
      }
    })()
    return () => { cancelled = true }
  }, [authState])

  // ── Cross-device sync: debounced layout save to server ───────────────────────
  // Only AFTER hydration — otherwise a freshly opened device would overwrite the server
  // with stale localStorage. last-write-wins; conflicts are negligible for a single user.
  useEffect(() => {
    if (authState !== 'authed' || !uiHydratedRef.current) return
    if (uiSaveTimer.current) window.clearTimeout(uiSaveTimer.current)
    uiSaveTimer.current = window.setTimeout(() => {
      api.saveUiState({
        open: openIds,
        active: activeId,
        sidebarOrder,
        splitPairs,
        splitWidth,
        globalFilesOpen,
      }).catch(() => {})
    }, 800)
    return () => { if (uiSaveTimer.current) window.clearTimeout(uiSaveTimer.current) }
  }, [openIds, activeId, sidebarOrder, splitPairs, splitWidth, globalFilesOpen, authState])

  // Clean up openIds / splitPairs / sidebarOrder of dead projects after list load
  useEffect(() => {
    if (!projects.length) return
    const valid = new Set(projects.map(p => p.id))
    setOpenIds(prev => {
      const next = prev.filter(id => valid.has(id))
      return next.length === prev.length ? prev : next
    })
    setActiveId(prev => prev === GLOBAL_FILES_ID || prev === SCHEDULES_ID || prev === VAULT_ID || prev === TERMINAL_ID || prev === SETTINGS_ID || (prev && valid.has(prev)) ? prev : null)
    setSplitPairs(prev => {
      const next: Record<string, string> = {}
      let changed = false
      for (const [k, v] of Object.entries(prev)) {
        if (valid.has(k) && valid.has(v)) next[k] = v
        else changed = true
      }
      return changed ? next : prev
    })
    setSidebarOrder(prev => {
      const filtered = prev.filter(id => valid.has(id))
      const existingSet = new Set(filtered)
      const added = projects.filter(p => !existingSet.has(p.id)).map(p => p.id)
      if (filtered.length === prev.length && added.length === 0) return prev
      return [...added, ...filtered]
    })
  }, [projects])

  // Global SSE activity stream → unread indicators + live git-status refresh
  // ops:b2a081: also drives runningIds (working indicator) and replyReadyIds (attention badge).
  // Single EventSource = O(1) connections regardless of how many tabs are open.
  //
  // esRef holds the live EventSource so connectActivityStream can replace it on mobile resume
  // without needing authState to change (the original trigger).
  const esRef = useRef<EventSource | null>(null)

  const connectActivityStream = useCallback(() => {
    // Close any existing connection before opening a new one (avoids duplicate listeners).
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }

    const es = new EventSource('/api/activity-stream')
    esRef.current = es

    es.onmessage = (ev) => {
      let payload: { kind?: string; session_key?: string; text?: string; level?: string }
      try { payload = JSON.parse(ev.data) } catch { return }

      if (payload.kind === 'notification') {
        const note = payload as { text?: string; level?: string }
        if (note.text) showToast(note.text, (note.level as 'error' | 'info' | 'success') ?? 'info')
        return
      }

      const sk = payload.session_key
      if (!sk) return

      const findProj = () =>
        projectsRef.current.find(p => p.session_key === sk)

      if (payload.kind === 'run_start') {
        // Agent started a new turn — show working indicator; clear any stale attention badge
        const proj = findProj()
        if (!proj) return
        setRunningIds(prev => { const n = new Set(prev); n.add(proj.id); return n })
        setReplyReadyIds(prev => { if (!prev.has(proj.id)) return prev; const n = new Set(prev); n.delete(proj.id); return n })
        return
      }

      // run_end → agent may have written files → refresh project list
      // (git.dirty/unpushed in header and sidebar will become current)
      if (payload.kind === 'run_end') {
        loadProjects()
        const proj = findProj()
        if (!proj) return
        // Clear working indicator
        setRunningIds(prev => { if (!prev.has(proj.id)) return prev; const n = new Set(prev); n.delete(proj.id); return n })
        // Notify + attention cue when the project is not currently the active visible tab.
        // Notifications are project-level because all chats of a project share one session_key
        // on the bus — per-chat gating isn't distinguishable here (refine later if chats get distinct keys).
        const isBackground = document.visibilityState !== 'visible' || proj.id !== activeIdRef.current
        if (isBackground) {
          // Desktop/mobile notification (opt-in, gated by useNotifications internals)
          const outcome = (payload as { outcome?: string }).outcome ?? 'ok'
          notifyRunEndRef.current({
            projectId: proj.id,
            projectName: proj.name,
            outcome,
            onClick: () => handleSelectRef.current(proj.id),
          })
          // Attention cue: set title while the page is in the background; restore on next focus.
          if (document.visibilityState !== 'visible') {
            document.title = '● Cardloop'
          }
          // Mark project as attention-needed
          setReplyReadyIds(prev => {
            const next = new Set(prev)
            next.add(proj.id)
            return next
          })
        }
        return
      }

      // Only count meaningful events for unread
      if (payload.kind !== 'text' && payload.kind !== 'tool') return
      const proj = findProj()
      if (proj && proj.id === activeIdRef.current) return
      if (proj) incrementUnread(sk)
    }
    es.onerror = () => { /* EventSource will reconnect automatically */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- incrementUnread + showToast are stable (useCallback); loadProjects/projectsRef/activeIdRef are stable refs
  }, [loadProjects, showToast, incrementUnread])

  // Initial SSE connection — (re)created when auth state changes.
  useEffect(() => {
    if (authState !== 'authed') {
      // Logged out: close any open stream.
      esRef.current?.close()
      esRef.current = null
      return
    }
    connectActivityStream()
    return () => {
      esRef.current?.close()
      esRef.current = null
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- connectActivityStream is stable after auth
  }, [authState, connectActivityStream])

  // Mobile resume self-heal: iOS/Android kill the EventSource when the screen turns off.
  // On resume (visibilitychange→visible or network online event), reconnect if the socket
  // is closed or missing, then revalidate auth and refresh project list.
  useEffect(() => {
    if (authState !== 'authed') return

    const onResume = () => {
      if (document.visibilityState !== 'visible') return
      if (!esRef.current || esRef.current.readyState === EventSource.CLOSED) {
        connectActivityStream()
      }
      // Re-validate auth (drops to login screen on 401) and refresh project data.
      checkAuth()
      loadProjects()
    }

    document.addEventListener('visibilitychange', onResume)
    window.addEventListener('online', onResume)
    return () => {
      document.removeEventListener('visibilitychange', onResume)
      window.removeEventListener('online', onResume)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- connectActivityStream/checkAuth/loadProjects are stable useCallbacks
  }, [authState, connectActivityStream, checkAuth, loadProjects])

  // Live git-status refresh: poll every 15s + on window/tab focus
  useEffect(() => {
    if (authState !== 'authed') return

    const POLL_MS = 15_000
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') loadProjects()
    }, POLL_MS)

    const onFocus = () => loadProjects()
    const onVis = () => { if (document.visibilityState === 'visible') loadProjects() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVis)

    return () => {
      clearInterval(id)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [authState, loadProjects])

  // Attention cue: restore document title when the page becomes visible again.
  // Set to '● Cardloop' by the run_end SSE handler when a background run finishes.
  useEffect(() => {
    const originalTitle = document.title
    const onVisible = () => {
      if (document.visibilityState === 'visible' && document.title === '● Cardloop') {
        document.title = originalTitle === '● Cardloop' ? 'Cardloop' : originalTitle
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, []) // runs once — document.title at mount is the original title

  const clearUnread = useCallback((id: string) => {
    const proj = projectsRef.current.find(p => p.id === id)
    const sk = proj?.session_key ?? null
    if (sk) clearUnreadForSession(sk)
    // Also clear reply-ready indicator when the user switches to this tab
    setReplyReadyIds(prev => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
    // ops:b2a081 — notify backend that the operator has opened this tab
    // so the server-side awaiting flag clears (survives page refresh)
    api.projectSeen(id).catch(() => { /* non-critical — best effort */ })
  }, [clearUnreadForSession])

  // Open project (sidebar click) — add to openIds (if not there) + activate
  // Also close the mobile drawer after selection + navigate to project screen on mobile
  const handleSelect = useCallback((id: string) => {
    setOpenIds(prev => prev.includes(id) ? prev : [...prev, id])
    setActiveId(id)
    clearUnread(id)
    setDrawerOpen(false)
    setMobileScreen('project')
  }, [clearUnread])
  // Keep handleSelectRef current so the SSE handler (connectActivityStream) can call it
  // without being re-subscribed on every render (same pattern as activeIdRef).
  handleSelectRef.current = handleSelect

  // Sidebar context-menu "⚙ Settings": select the project + ask its ProjectView to
  // switch to the Settings tab. ProjectView watches settingsRequest.nonce.
  const handleOpenProjectSettings = useCallback((id: string) => {
    handleSelect(id)
    setSettingsRequest({ id, nonce: Date.now() })
  }, [handleSelect])

  // Drag-and-drop sidebar order. IMPORTANT: hook must be above any early returns
  // (return <LoginScreen> etc.), otherwise Rules of Hooks are violated → black screen.
  // If after refresh activeId === GLOBAL_FILES_ID but flag was cleared — restore it
  useEffect(() => {
    if (activeId === GLOBAL_FILES_ID) setGlobalFilesOpen(true)
  }, [activeId])

  const handleOpenGlobalFiles = useCallback(() => {
    setGlobalFilesOpen(true)
    setActiveId(GLOBAL_FILES_ID)
    setDrawerOpen(false)
    setMobileScreen('project')
  }, [])

  const handleCloseGlobalFiles = useCallback(() => {
    setGlobalFilesOpen(false)
    setActiveId(prev => prev === GLOBAL_FILES_ID ? (openIds[0] || null) : prev)
  }, [openIds])

  const handleOpenSchedules = useCallback(() => {
    setSchedulesOpen(true)
    setActiveId(SCHEDULES_ID)
    setDrawerOpen(false)
    setMobileScreen('project')
  }, [])

  const handleCloseSchedules = useCallback(() => {
    setSchedulesOpen(false)
    setActiveId(prev => prev === SCHEDULES_ID ? (openIds[0] || null) : prev)
  }, [openIds])

  const handleOpenVault = useCallback(() => {
    setVaultOpen(true)
    setActiveId(VAULT_ID)
    setDrawerOpen(false)
    setMobileScreen('project')
  }, [])

  const handleCloseVault = useCallback(() => {
    setVaultOpen(false)
    setActiveId(prev => prev === VAULT_ID ? (openIds[0] || null) : prev)
  }, [openIds])

  const handleOpenTerminal = useCallback(() => {
    setTerminalOpen(true)
    setActiveId(TERMINAL_ID)
    setDrawerOpen(false)
    setMobileScreen('project')
  }, [])

  const handleCloseTerminal = useCallback(() => {
    setTerminalOpen(false)
    setActiveId(prev => prev === TERMINAL_ID ? (openIds[0] || null) : prev)
  }, [openIds])

  const handleOpenSettings = useCallback(() => {
    setSettingsGlobalOpen(true)
    setActiveId(SETTINGS_ID)
    setDrawerOpen(false)
    setMobileScreen('project')
  }, [])

  const handleCloseSettings = useCallback(() => {
    setSettingsGlobalOpen(false)
    setActiveId(prev => prev === SETTINGS_ID ? (openIds[0] || null) : prev)
  }, [openIds])

  const handleSidebarReorder = useCallback((ids: string[]) => {
    setSidebarOrder(ids)
  }, [])

  // Activate tab (tab click)
  const handleTabActivate = useCallback((id: string) => {
    setActiveId(id)
    clearUnread(id)
    setMobileScreen('project')
  }, [clearUnread])

  // Close tab — remove from openIds; if it was active — the adjacent one becomes active
  const handleTabClose = useCallback((id: string) => {
    setSplitPairs(prev => { const { [id]: _, ...rest } = prev; return rest })
    setOpenIds(prev => {
      const idx = prev.indexOf(id)
      if (idx === -1) return prev
      const next = prev.filter(x => x !== id)
      // if the closed tab was active — switch to the adjacent one
      setActiveId(curActive => {
        if (curActive !== id) return curActive
        if (next.length === 0) {
          setMobileScreen('list')
          return null
        }
        // prefer the right neighbour, otherwise the left
        const newIdx = Math.min(idx, next.length - 1)
        return next[newIdx]
      })
      return next
    })
  }, [])

  // Split-view: create a second free chat alongside the active one
  const handleSplitCreate = useCallback(async (leftId: string) => {
    try {
      const res = await api.freeCreate()
      await loadProjects()
      // split partner is NOT added to openIds — managed via splitPairs
      setSplitPairs(prev => ({ ...prev, [leftId]: res.id }))
    } catch (e) {
      showToast(`Could not open split: ${e instanceof Error ? e.message : String(e)}`)
    }
  }, [loadProjects, showToast])

  const handleSplitClose = useCallback((leftId: string) => {
    setSplitPairs(prev => { const { [leftId]: _, ...rest } = prev; return rest })
  }, [])

  const onSplitDividerMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const parent = (e.currentTarget as HTMLElement).parentElement
    if (!parent) return
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    function onMove(ev: MouseEvent) {
      const rect = parent!.getBoundingClientRect()
      const pct = ((ev.clientX - rect.left) / rect.width) * 100
      setSplitWidth(Math.max(20, Math.min(80, pct)))
    }
    function onUp() {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [])

  // Create a new project, open it immediately + onboarding card will start.
  // Optional intent string → backend infers archetype, derives name/slug, scaffolds + seeds tasks.
  // The sidebar "+ New project" button calls this with a MouseEvent (blank create) — guarded by typeof.
  const [newProjectBusy, setNewProjectBusy] = useState(false)
  const [intentDraft, setIntentDraft] = useState('')
  const handleNewProject = useCallback(async (intent?: string) => {
    if (newProjectBusy) return
    const intentStr = typeof intent === 'string' ? intent.trim() : ''
    setNewProjectBusy(true)
    try {
      const res = await api.newProject(intentStr || undefined)
      await loadProjects()
      setOpenIds(prev => prev.includes(res.id) ? prev : [...prev, res.id])
      setActiveId(res.id)
      setIntentDraft('')
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast(`Could not create project: ${msg}`)
    } finally {
      setNewProjectBusy(false)
    }
  }, [loadProjects, newProjectBusy, showToast])

  // Create a new free chat (cwd=$HOME) and immediately open it as a tab
  const handleNewFree = useCallback(async () => {
    try {
      const res = await api.freeCreate()
      // Refresh project list and immediately open the new chat
      await loadProjects()
      setOpenIds(prev => prev.includes(res.id) ? prev : [...prev, res.id])
      setActiveId(res.id)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast(`Could not create free chat: ${msg}`)
    }
  }, [loadProjects, showToast])

  // Rename project (slug) — updates active project + open tabs
  const handleRenameSuccess = useCallback((oldId: string, newId: string) => {
    loadProjects()
    setOpenIds(prev => prev.map(id => id === oldId ? newId : id))
    setActiveId(prev => prev === oldId ? newId : prev)
    setSidebarOrder(prev => prev.map(id => id === oldId ? newId : id))
    setSplitPairs(prev => {
      const next: Record<string, string> = {}
      for (const [k, v] of Object.entries(prev)) {
        next[k === oldId ? newId : k] = v === oldId ? newId : v
      }
      return next
    })
  }, [loadProjects])

  // Tab rename — supported for free chats only
  const handleRenameTab = useCallback(async (id: string, label: string) => {
    const proj = projectsRef.current.find(p => p.id === id)
    if (!proj?.is_free) return
    try {
      await api.freeRename(id, label)
      loadProjects()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast(`Could not rename: ${msg}`)
    }
  }, [loadProjects, showToast])

  // Delete free chat — close the tab and remove from backend
  const handleDeleteFree = useCallback(async (id: string) => {
    try {
      await api.freeDelete(id)
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        showToast(t['app.free_chat_busy'])
        return
      }
      const msg = e instanceof Error ? e.message : String(e)
      showToast(`Could not delete: ${msg}`)
      return
    }
    // Close the tab locally (same logic as handleTabClose)
    setOpenIds(prev => {
      const idx = prev.indexOf(id)
      if (idx === -1) return prev
      const next = prev.filter(x => x !== id)
      setActiveId(curActive => {
        if (curActive !== id) return curActive
        if (next.length === 0) return null
        const newIdx = Math.min(idx, next.length - 1)
        return next[newIdx]
      })
      return next
    })
    loadProjects()
  }, [loadProjects, showToast])

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed(prev => {
      const next = !prev
      try { localStorage.setItem(LS_SIDEBAR_COLLAPSED, String(next)) } catch {}
      return next
    })
  }, [])

  // Close drawer when viewport is resized back to desktop (>1024px)
  useEffect(() => {
    function onResize() {
      if (window.innerWidth > 1024) setDrawerOpen(false)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  async function handleLogout() {
    try { await api.logout() } catch { /* ignore */ }
    setProjects([])
    setOpenIds([])
    setActiveId(null)
    resetUnread()
    setAuthState('unauthed')
  }

  if (authState === 'loading') {
    return (
      <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg)' }}>
        <Spinner label="Connecting..." />
      </div>
    )
  }

  if (authState === 'unauthed') {
    return <LoginScreen onLogin={() => setAuthState('authed')} />
  }

  // Sort: manual order from sidebarOrder (drag-and-drop), new items go to end
  const orderRank = new Map(sidebarOrder.map((id, i) => [id, i]))
  const sortedProjects = [...projects].sort((a, b) => {
    const ra = orderRank.get(a.id) ?? Infinity
    const rb = orderRank.get(b.id) ?? Infinity
    return ra - rb
  })

  // Open projects in their opening order (like in LS — don't shuffle on unread)
  const openProjects = openIds
    .map(id => projects.find(p => p.id === id))
    .filter((p): p is Project => !!p)

  const hasOpen = openProjects.length > 0

  return (
    <div className={`app-layout${sidebarCollapsed ? ' sidebar-collapsed' : ''} mobile-on-${mobileScreen}`}>
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
      {/* Backdrop for mobile drawer — tap to close */}
      <div
        className={`sidebar-backdrop${drawerOpen ? ' visible' : ''}`}
        onClick={() => setDrawerOpen(false)}
        aria-hidden="true"
      />
      <Sidebar
        projects={sortedProjects}
        selectedId={activeId}
        onSelect={handleSelect}
        onLogout={handleLogout}
        onDeleteFree={handleDeleteFree}
        loading={projectsLoading}
        unreadBySession={unreadBySession}
        replyReadyIds={replyReadyIds}
        collapsed={sidebarCollapsed}
        onToggleCollapse={toggleSidebar}
        onReorder={handleSidebarReorder}
        onNewProject={handleNewProject}
        newProjectBusy={newProjectBusy}
        drawerOpen={drawerOpen}
        onCloseDrawer={() => setDrawerOpen(false)}
        activeProjectId={activeId}
        onGoBack={() => setMobileScreen('project')}
        onProjectsReload={loadProjects}
        onOpenProjectSettings={handleOpenProjectSettings}
        theme={theme}
        onThemeChange={setTheme}
        onOpenTerminal={handleOpenTerminal}
        terminalActive={activeId === TERMINAL_ID}
        onOpenVault={handleOpenVault}
        vaultActive={activeId === VAULT_ID}
        onOpenGlobalFiles={handleOpenGlobalFiles}
        globalFilesActive={activeId === GLOBAL_FILES_ID}
        onOpenSchedules={handleOpenSchedules}
        schedulesActive={activeId === SCHEDULES_ID}
        onOpenSettingsGlobal={handleOpenSettings}
        settingsGlobalActive={activeId === SETTINGS_ID}
      />

      <div className="main-area">
        <ProjectTabBar
          projects={openProjects}
          activeId={activeId}
          unreadBySession={unreadBySession}
          replyReadyIds={replyReadyIds}
          runningIds={runningIds}
          onActivate={handleTabActivate}
          onClose={handleTabClose}
          onRename={handleRenameTab}
          onNewFree={handleNewFree}
          onReorderOpen={(newIds) => setOpenIds(newIds)}
          globalFilesOpen={globalFilesOpen}
          globalFilesActive={activeId === GLOBAL_FILES_ID}
          onOpenGlobalFiles={handleOpenGlobalFiles}
          onCloseGlobalFiles={handleCloseGlobalFiles}
          schedulesOpen={schedulesOpen}
          schedulesActive={activeId === SCHEDULES_ID}
          onOpenSchedules={handleOpenSchedules}
          onCloseSchedules={handleCloseSchedules}
          vaultOpen={vaultOpen}
          vaultActive={activeId === VAULT_ID}
          onOpenVault={handleOpenVault}
          onCloseVault={handleCloseVault}
          terminalOpen={terminalOpen}
          terminalActive={activeId === TERMINAL_ID}
          onOpenTerminal={handleOpenTerminal}
          onCloseTerminal={handleCloseTerminal}
          settingsGlobalOpen={settingsGlobalOpen}
          settingsGlobalActive={activeId === SETTINGS_ID}
          onOpenSettingsGlobal={handleOpenSettings}
          onCloseSettingsGlobal={handleCloseSettings}
          onToggleDrawer={() => setDrawerOpen(prev => !prev)}
          mobileScreen={mobileScreen}
          onGoToProjectList={() => setMobileScreen('list')}
        />

        {/* Global file browser — always mounted (display:none when inactive),
            otherwise the tree state is reset when switching tabs */}
        {globalFilesOpen && (
          <div
            className="main-content"
            style={{
              display: activeId === GLOBAL_FILES_ID ? 'flex' : 'none',
              padding: 0, overflow: 'hidden',
            }}
          >
            <GlobalFilesTab />
          </div>
        )}

        {/* Schedules tab — global, always mounted when open */}
        {schedulesOpen && (
          <div
            className="main-content"
            style={{
              display: activeId === SCHEDULES_ID ? 'flex' : 'none',
              flexDirection: 'column',
              padding: 0,
              overflow: 'hidden',
            }}
          >
            <SchedulesTab />
          </div>
        )}

        {/* Vault tab — global, always mounted when open */}
        {vaultOpen && (
          <div
            className="main-content"
            style={{
              display: activeId === VAULT_ID ? 'flex' : 'none',
              flexDirection: 'column',
              padding: 0,
              overflow: 'hidden',
            }}
          >
            <VaultTab />
          </div>
        )}

        {/* Terminal tab — global, always mounted when open */}
        {terminalOpen && (
          <div
            className="main-content"
            style={{
              display: activeId === TERMINAL_ID ? 'flex' : 'none',
              flexDirection: 'column',
              padding: 0,
              overflow: 'hidden',
            }}
          >
            <TerminalTab isActive={activeId === TERMINAL_ID} />
          </div>
        )}

        {/* Global settings tab — global, always mounted when open */}
        {settingsGlobalOpen && (
          <div
            className="main-content"
            style={{
              display: activeId === SETTINGS_ID ? 'flex' : 'none',
              flexDirection: 'column',
              padding: 0,
              overflow: 'hidden',
            }}
          >
            <GlobalSettingsTab />
          </div>
        )}

        {/* All open ProjectViews — always mounted, inactive ones are hidden */}
        {openProjects.map(p => {
          const splitId = splitPairs[p.id]
          const splitProject = splitId ? projects.find(x => x.id === splitId) : undefined
          const isActive = p.id === activeId && activeId !== GLOBAL_FILES_ID && activeId !== TERMINAL_ID
          return (
            <div
              key={p.id}
              className={`project-tab-slot${splitProject ? ' has-split' : ''}`}
              style={{ display: isActive ? 'flex' : 'none' }}
            >
              <div className={splitProject ? 'split-pane' : 'split-pane-full'} style={splitProject ? { flex: `0 0 ${splitWidth}%`, maxWidth: `${splitWidth}%` } : {}}>
                <ProjectView
                  project={p}
                  onProjectsReload={loadProjects}
                  onRenameSuccess={handleRenameSuccess}
                  onSplitCreate={p.is_free && !splitId ? () => handleSplitCreate(p.id) : undefined}
                  isActive={isActive}
                  openProjectIds={openIds}
                  onSwipeToProject={(id) => { handleTabActivate(id) }}
                  settingsRequest={settingsRequest}
                  models={models}
                />
              </div>
              {splitProject && (
                <>
                  <div className="free-split-divider" onMouseDown={onSplitDividerMouseDown} />
                  <div className="split-pane" style={{ flex: 1 }}>
                    <ProjectView
                      project={splitProject}
                      onProjectsReload={loadProjects}
                      onSplitClose={() => handleSplitClose(p.id)}
                      isActive={isActive}
                      models={models}
                    />
                  </div>
                </>
              )}
            </div>
          )
        })}

        {/* Welcome — only when no open projects and not in the global file browser, schedules, or vault */}
        {!hasOpen && activeId !== GLOBAL_FILES_ID && activeId !== SCHEDULES_ID && activeId !== VAULT_ID && activeId !== TERMINAL_ID && (
          <div className="main-content">
            <div className="welcome">
              <div className="welcome-icon">⚡</div>
              <h2>{t['app.welcome_title']}</h2>
              <p>{t['app.welcome_hint']}</p>
              <form
                className="welcome-intent"
                onSubmit={e => { e.preventDefault(); void handleNewProject(intentDraft) }}
              >
                <input
                  className="welcome-intent-input"
                  type="text"
                  placeholder={t['app.intent_placeholder']}
                  value={intentDraft}
                  onChange={e => setIntentDraft(e.target.value)}
                  disabled={newProjectBusy}
                  autoFocus
                />
                <button
                  className="welcome-intent-btn"
                  type="submit"
                  disabled={newProjectBusy}
                >
                  {newProjectBusy ? '⏳' : t['app.intent_create']}
                </button>
              </form>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

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
import { useToast, ToastContainer } from './components/Toast'
import { useUnreadTracker } from './hooks/useUnreadTracker'

const GLOBAL_FILES_ID = '__global__'

type AuthState = 'loading' | 'unauthed' | 'authed'

const LS_SIDEBAR_COLLAPSED = 'cops.sidebarCollapsed'
const LS_OPEN = 'cops.openProjects'
const LS_ACTIVE = 'cops.activeProject'
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
  const { toasts, showToast, dismiss } = useToast()
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [openIds, setOpenIds] = useState<string[]>(() => readStringList(LS_OPEN))
  const [activeId, setActiveId] = useState<string | null>(() => readString(LS_ACTIVE))
  const [sidebarOrder, setSidebarOrder] = useState<string[]>(() => readSidebarOrder())
  const { unreadBySession, incrementUnread, clearUnreadForSession, resetUnread } = useUnreadTracker()
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => readBool(LS_SIDEBAR_COLLAPSED, false))
  // Split-view: leftId → rightId (free chats only)
  const [splitPairs, setSplitPairs] = useState<Record<string, string>>(() => readSplitPairs())
  const [splitWidth, setSplitWidth] = useState<number>(() => readSplitWidth())
  // Global file browser (persisted in localStorage)
  const [globalFilesOpen, setGlobalFilesOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('cops.globalFilesOpen') === 'true' } catch { return false }
  })
  // Current active project — for SSE handler, no re-subscription on every select
  const activeIdRef = useRef<string | null>(null)
  const projectsRef = useRef<Project[]>([])
  // After first successful load, don't show "Loading..." on background polls
  const projectsLoadedRef = useRef(false)
  // Cross-device UI layout: server is the source of truth (data/ui_state.json).
  // localStorage serves as an instant cache (no flash); server syncs on top.
  const uiHydratedRef = useRef(false)
  const uiSaveTimer = useRef<number | null>(null)

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
              p.health.git?.branch === n.health.git?.branch &&
              p.health.git?.dirty === n.health.git?.dirty &&
              p.health.git?.unpushed === n.health.git?.unpushed
            )
          })
        return same ? prev : res.projects
      })
      projectsLoadedRef.current = true
    } catch {
      setProjects([])
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

  // Keep refs up-to-date (needed by SSE handler without re-subscription)
  useEffect(() => { activeIdRef.current = activeId }, [activeId])
  useEffect(() => { projectsRef.current = projects }, [projects])

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
    setActiveId(prev => prev === GLOBAL_FILES_ID || (prev && valid.has(prev)) ? prev : null)
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
  useEffect(() => {
    if (authState !== 'authed') return

    const es = new EventSource('/api/activity-stream')
    es.onmessage = (ev) => {
      let payload: { kind?: string; session_key?: string }
      try { payload = JSON.parse(ev.data) } catch { return }
      const sk = payload.session_key
      if (!sk) return

      // run_end → agent may have written files → refresh project list
      // (git.dirty/unpushed in header and sidebar will become current)
      if (payload.kind === 'run_end') {
        loadProjects()
        return
      }

      // Only count meaningful events for unread
      if (payload.kind !== 'text' && payload.kind !== 'tool') return
      const proj = projectsRef.current.find(p => p.tg_thread != null && String(p.tg_thread) === sk)
      if (proj && proj.id === activeIdRef.current) return
      incrementUnread(sk)
    }
    es.onerror = () => { /* EventSource will reconnect automatically */ }
    return () => { es.close() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- incrementUnread is stable (useCallback)
  }, [authState, loadProjects])

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

  const clearUnread = useCallback((id: string) => {
    const proj = projectsRef.current.find(p => p.id === id)
    const sk = proj?.tg_thread != null ? String(proj.tg_thread) : null
    if (!sk) return
    clearUnreadForSession(sk)
  }, [clearUnreadForSession])

  // Open project (sidebar click) — add to openIds (if not there) + activate
  const handleSelect = useCallback((id: string) => {
    setOpenIds(prev => prev.includes(id) ? prev : [...prev, id])
    setActiveId(id)
    clearUnread(id)
  }, [clearUnread])

  // Drag-and-drop sidebar order. IMPORTANT: hook must be above any early returns
  // (return <LoginScreen> etc.), otherwise Rules of Hooks are violated → black screen.
  // If after refresh activeId === GLOBAL_FILES_ID but flag was cleared — restore it
  useEffect(() => {
    if (activeId === GLOBAL_FILES_ID) setGlobalFilesOpen(true)
  }, [activeId])

  const handleOpenGlobalFiles = useCallback(() => {
    setGlobalFilesOpen(true)
    setActiveId(GLOBAL_FILES_ID)
  }, [])

  const handleCloseGlobalFiles = useCallback(() => {
    setGlobalFilesOpen(false)
    setActiveId(prev => prev === GLOBAL_FILES_ID ? (openIds[0] || null) : prev)
  }, [openIds])

  const handleSidebarReorder = useCallback((ids: string[]) => {
    setSidebarOrder(ids)
  }, [])

  // Activate tab (tab click)
  const handleTabActivate = useCallback((id: string) => {
    setActiveId(id)
    clearUnread(id)
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
        if (next.length === 0) return null
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

  // Create a new project (untitled-<ts>), open it immediately + onboarding card will start
  const [newProjectBusy, setNewProjectBusy] = useState(false)
  const handleNewProject = useCallback(async () => {
    if (newProjectBusy) return
    setNewProjectBusy(true)
    try {
      const res = await api.newProject()
      await loadProjects()
      setOpenIds(prev => prev.includes(res.id) ? prev : [...prev, res.id])
      setActiveId(res.id)
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
    <div className={`app-layout${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
      <Sidebar
        projects={sortedProjects.filter(p => !p.is_free)}
        selectedId={activeId}
        onSelect={handleSelect}
        onLogout={handleLogout}
        onDeleteFree={handleDeleteFree}
        loading={projectsLoading}
        unreadBySession={unreadBySession}
        collapsed={sidebarCollapsed}
        onToggleCollapse={toggleSidebar}
        onReorder={handleSidebarReorder}
        onNewProject={handleNewProject}
        newProjectBusy={newProjectBusy}
      />

      <div className="main-area">
        <ProjectTabBar
          projects={openProjects}
          activeId={activeId}
          unreadBySession={unreadBySession}
          onActivate={handleTabActivate}
          onClose={handleTabClose}
          onRename={handleRenameTab}
          onNewFree={handleNewFree}
          globalFilesOpen={globalFilesOpen}
          globalFilesActive={activeId === GLOBAL_FILES_ID}
          onOpenGlobalFiles={handleOpenGlobalFiles}
          onCloseGlobalFiles={handleCloseGlobalFiles}
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

        {/* All open ProjectViews — always mounted, inactive ones are hidden */}
        {openProjects.map(p => {
          const splitId = splitPairs[p.id]
          const splitProject = splitId ? projects.find(x => x.id === splitId) : undefined
          const isActive = p.id === activeId && activeId !== GLOBAL_FILES_ID
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
                    />
                  </div>
                </>
              )}
            </div>
          )
        })}

        {/* Welcome — only when no open projects and not in the global file browser */}
        {!hasOpen && activeId !== GLOBAL_FILES_ID && (
          <div className="main-content">
            <div className="welcome">
              <div className="welcome-icon">⚡</div>
              <h2>Claude-Ops</h2>
              <p>{t['app.welcome_hint']}</p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

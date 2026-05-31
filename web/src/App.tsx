import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from './api'
import { Project } from './types'
import { LoginScreen } from './components/LoginScreen'
import { Sidebar } from './components/Sidebar'
import { ProjectView } from './components/ProjectView'
import { ProjectTabBar } from './components/ProjectTabBar'
import { Spinner } from './components/Spinner'
import { GlobalFilesTab } from './tabs/GlobalFilesTab'

const GLOBAL_FILES_ID = '__global__'

type AuthState = 'loading' | 'unauthed' | 'authed'

const LS_UNREAD = 'cops.unreadBySession'
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

function readUnread(): Record<string, number> {
  try {
    const raw = localStorage.getItem(LS_UNREAD)
    if (!raw) return {}
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') return {}
    const out: Record<string, number> = {}
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'number' && v > 0) out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

function writeUnread(map: Record<string, number>) {
  try { localStorage.setItem(LS_UNREAD, JSON.stringify(map)) } catch {}
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
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [openIds, setOpenIds] = useState<string[]>(() => readStringList(LS_OPEN))
  const [activeId, setActiveId] = useState<string | null>(() => readString(LS_ACTIVE))
  const [sidebarOrder, setSidebarOrder] = useState<string[]>(() => readSidebarOrder())
  const [unreadBySession, setUnreadBySession] = useState<Record<string, number>>(() => readUnread())
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => readBool(LS_SIDEBAR_COLLAPSED, false))
  // Split-view: leftId → rightId (только для free-чатов)
  const [splitPairs, setSplitPairs] = useState<Record<string, string>>(() => readSplitPairs())
  const [splitWidth, setSplitWidth] = useState<number>(() => readSplitWidth())
  // Глобальный файловый браузер (персистируется в localStorage)
  const [globalFilesOpen, setGlobalFilesOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('cops.globalFilesOpen') === 'true' } catch { return false }
  })
  // Текущий активный проект — для SSE-обработчика, без перепересоздания подписки на каждом select
  const activeIdRef = useRef<string | null>(null)
  const projectsRef = useRef<Project[]>([])
  // После первой успешной загрузки не показываем "Загрузка..." при фоновых poll'ах
  const projectsLoadedRef = useRef(false)

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
    // Loading-флаг только при первой загрузке — иначе сайдбар мигает "Загрузка..."
    // при каждом фоновом poll'е (каждые 15с, на focus, на run_end)
    if (!projectsLoadedRef.current) setProjectsLoading(true)
    try {
      const res = await api.projects()
      // Стабильное сравнение: не обновляем стейт если данные не изменились
      // (предотвращает каскад эффектов openIds/sidebarOrder/activeId)
      setProjects(prev =>
        JSON.stringify(prev) === JSON.stringify(res.projects) ? prev : res.projects
      )
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

  // Поддерживаем refs в актуальном состоянии (нужны SSE-обработчику без перепересоздания подписки)
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

  // Чистим openIds / splitPairs / sidebarOrder от мёртвых проектов после загрузки списка
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

  // Глобальный SSE-стрим активности → unread-индикаторы + live-refresh git-статуса
  useEffect(() => {
    if (authState !== 'authed') return

    const es = new EventSource('/api/activity-stream')
    es.onmessage = (ev) => {
      let payload: { kind?: string; session_key?: string }
      try { payload = JSON.parse(ev.data) } catch { return }
      const sk = payload.session_key
      if (!sk) return

      // run_end → агент мог что-то записать в файлы → освежаем список проектов
      // (git.dirty/unpushed в шапке и сайдбаре станут актуальными)
      if (payload.kind === 'run_end') {
        loadProjects()
        return
      }

      // Учитываем только значимые события для unread
      if (payload.kind !== 'text' && payload.kind !== 'tool') return
      const proj = projectsRef.current.find(p => p.tg_thread != null && String(p.tg_thread) === sk)
      if (proj && proj.id === activeIdRef.current) return
      setUnreadBySession(prev => {
        const next = { ...prev, [sk]: (prev[sk] || 0) + 1 }
        writeUnread(next)
        return next
      })
    }
    es.onerror = () => { /* EventSource сам переподключится */ }
    return () => { es.close() }
  }, [authState, loadProjects])

  // Live-refresh git-статуса: polling каждые 15с + при возврате фокуса на окно/вкладку
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
    setUnreadBySession(prev => {
      if (!prev[sk]) return prev
      const next = { ...prev }
      delete next[sk]
      writeUnread(next)
      return next
    })
  }, [])

  // Открыть проект (клик в сайдбаре) — добавить в openIds (если нет) + активировать
  const handleSelect = useCallback((id: string) => {
    setOpenIds(prev => prev.includes(id) ? prev : [...prev, id])
    setActiveId(id)
    clearUnread(id)
  }, [clearUnread])

  // Drag-and-drop порядок сайдбара. ВАЖНО: hook — выше любых ранних return
  // (return <LoginScreen> и т.п.), иначе нарушаются Rules of Hooks → чёрный экран.
  // Если после refresh activeId === GLOBAL_FILES_ID но флаг сброшен — восстанавливаем
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

  // Активировать вкладку (клик по табу)
  const handleTabActivate = useCallback((id: string) => {
    setActiveId(id)
    clearUnread(id)
  }, [clearUnread])

  // Закрыть вкладку — убрать из openIds; если была активной — соседняя становится активной
  const handleTabClose = useCallback((id: string) => {
    setSplitPairs(prev => { const { [id]: _, ...rest } = prev; return rest })
    setOpenIds(prev => {
      const idx = prev.indexOf(id)
      if (idx === -1) return prev
      const next = prev.filter(x => x !== id)
      // если закрыли активную — переключаем на соседнюю
      setActiveId(curActive => {
        if (curActive !== id) return curActive
        if (next.length === 0) return null
        // приоритет: правый сосед, иначе левый
        const newIdx = Math.min(idx, next.length - 1)
        return next[newIdx]
      })
      return next
    })
  }, [])

  // Split-view: создать второй free-чат рядом с активным
  const handleSplitCreate = useCallback(async (leftId: string) => {
    try {
      const res = await api.freeCreate()
      await loadProjects()
      // партнёр НЕ добавляется в openIds — управляется через splitPairs
      setSplitPairs(prev => ({ ...prev, [leftId]: res.id }))
    } catch (e) {
      alert(`Не удалось открыть split: ${e instanceof Error ? e.message : String(e)}`)
    }
  }, [loadProjects])

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

  // Создать новый проект (untitled-<ts>) и сразу открыть его + запустится онбординг-карточка
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
      alert(`Не удалось создать проект: ${msg}`)
    } finally {
      setNewProjectBusy(false)
    }
  }, [loadProjects, newProjectBusy])

  // Создать новый свободный чат (cwd=$HOME) и сразу открыть его как вкладку
  const handleNewFree = useCallback(async () => {
    try {
      const res = await api.freeCreate()
      // Обновляем список проектов и сразу открываем новый чат
      await loadProjects()
      setOpenIds(prev => prev.includes(res.id) ? prev : [...prev, res.id])
      setActiveId(res.id)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      alert(`Не удалось создать свободный чат: ${msg}`)
    }
  }, [loadProjects])

  // Переименование вкладки — поддерживается только для free-чатов
  const handleRenameTab = useCallback(async (id: string, label: string) => {
    const proj = projectsRef.current.find(p => p.id === id)
    if (!proj?.is_free) return
    try {
      await api.freeRename(id, label)
      loadProjects()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      alert(`Не удалось переименовать: ${msg}`)
    }
  }, [loadProjects])

  // Удалить свободный чат — закрыть вкладку, убрать с бэка
  const handleDeleteFree = useCallback(async (id: string) => {
    try {
      await api.freeDelete(id)
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        alert('Свободный чат сейчас занят — сначала останови агента')
        return
      }
      const msg = e instanceof Error ? e.message : String(e)
      alert(`Не удалось удалить: ${msg}`)
      return
    }
    // Локально закрываем вкладку (та же логика, что handleTabClose)
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
  }, [loadProjects])

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
    setUnreadBySession({})
    writeUnread({})
    setAuthState('unauthed')
  }

  if (authState === 'loading') {
    return (
      <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg)' }}>
        <Spinner label="Соединение..." />
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

  // Открытые проекты в порядке их открытия (как в LS — не перетасовываем при unread)
  const openProjects = openIds
    .map(id => projects.find(p => p.id === id))
    .filter((p): p is Project => !!p)

  const hasOpen = openProjects.length > 0

  return (
    <div className={`app-layout${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
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

        {/* Глобальный файловый браузер — всегда примонтирован (display:none когда неактивен),
            иначе сбрасывается состояние дерева при переключении вкладок */}
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

        {/* Все открытые ProjectView — всегда примонтированы, скрываем неактивные */}
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
                  onSplitCreate={p.is_free && !splitId ? () => handleSplitCreate(p.id) : undefined}
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
                    />
                  </div>
                </>
              )}
            </div>
          )
        })}

        {/* Welcome — только когда нет открытых проектов и не в глобальном браузере */}
        {!hasOpen && activeId !== GLOBAL_FILES_ID && (
          <div className="main-content">
            <div className="welcome">
              <div className="welcome-icon">⚡</div>
              <h2>Claude-Ops</h2>
              <p>Выберите проект в левом сайдбаре для просмотра деталей</p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

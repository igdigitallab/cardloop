import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from './api'
import { Project } from './types'
import { LoginScreen } from './components/LoginScreen'
import { Sidebar } from './components/Sidebar'
import { ProjectView } from './components/ProjectView'
import { ProjectTabBar } from './components/ProjectTabBar'
import { Spinner } from './components/Spinner'

type AuthState = 'loading' | 'unauthed' | 'authed'

const LS_RECENT = 'cops.recentProjects'
const LS_UNREAD = 'cops.unreadBySession'
const LS_SIDEBAR_COLLAPSED = 'cops.sidebarCollapsed'
const LS_OPEN = 'cops.openProjects'
const LS_ACTIVE = 'cops.activeProject'
const RECENT_MAX = 50

function readRecent(): string[] {
  try {
    const raw = localStorage.getItem(LS_RECENT)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function writeRecent(ids: string[]) {
  try { localStorage.setItem(LS_RECENT, JSON.stringify(ids.slice(0, RECENT_MAX))) } catch {}
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

export default function App() {
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [openIds, setOpenIds] = useState<string[]>(() => readStringList(LS_OPEN))
  const [activeId, setActiveId] = useState<string | null>(() => readString(LS_ACTIVE))
  const [recentIds, setRecentIds] = useState<string[]>(() => readRecent())
  const [unreadBySession, setUnreadBySession] = useState<Record<string, number>>(() => readUnread())
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => readBool(LS_SIDEBAR_COLLAPSED, false))
  // Текущий активный проект — для SSE-обработчика, без перепересоздания подписки на каждом select
  const activeIdRef = useRef<string | null>(null)
  const projectsRef = useRef<Project[]>([])

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
    setProjectsLoading(true)
    try {
      const res = await api.projects()
      setProjects(res.projects)
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

  // Persist openIds + activeId
  useEffect(() => {
    try { localStorage.setItem(LS_OPEN, JSON.stringify(openIds)) } catch {}
  }, [openIds])
  useEffect(() => {
    try {
      if (activeId) localStorage.setItem(LS_ACTIVE, activeId)
      else localStorage.removeItem(LS_ACTIVE)
    } catch {}
  }, [activeId])

  // Чистим openIds от мёртвых проектов после загрузки списка
  useEffect(() => {
    if (!projects.length) return
    const valid = new Set(projects.map(p => p.id))
    setOpenIds(prev => {
      const next = prev.filter(id => valid.has(id))
      return next.length === prev.length ? prev : next
    })
    setActiveId(prev => (prev && valid.has(prev)) ? prev : null)
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
    setRecentIds(prev => {
      const next = [id, ...prev.filter(x => x !== id)].slice(0, RECENT_MAX)
      writeRecent(next)
      return next
    })
    clearUnread(id)
  }, [clearUnread])

  // Активировать вкладку (клик по табу)
  const handleTabActivate = useCallback((id: string) => {
    setActiveId(id)
    clearUnread(id)
  }, [clearUnread])

  // Закрыть вкладку — убрать из openIds; если была активной — соседняя становится активной
  const handleTabClose = useCallback((id: string) => {
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

  // Sort: recently opened first (in recency order), then the rest in original order
  const recentRank = new Map(recentIds.map((id, i) => [id, i]))
  const sortedProjects = [...projects].sort((a, b) => {
    const ra = recentRank.has(a.id) ? recentRank.get(a.id)! : Infinity
    const rb = recentRank.has(b.id) ? recentRank.get(b.id)! : Infinity
    if (ra !== rb) return ra - rb
    return 0
  })

  // Открытые проекты в порядке их открытия (как в LS — не перетасовываем при unread)
  const openProjects = openIds
    .map(id => projects.find(p => p.id === id))
    .filter((p): p is Project => !!p)

  const hasOpen = openProjects.length > 0

  return (
    <div className={`app-layout${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
      <Sidebar
        projects={sortedProjects}
        selectedId={activeId}
        onSelect={handleSelect}
        onLogout={handleLogout}
        loading={projectsLoading}
        unreadBySession={unreadBySession}
        collapsed={sidebarCollapsed}
        onToggleCollapse={toggleSidebar}
      />

      <div className="main-area">
        {hasOpen && (
          <ProjectTabBar
            projects={openProjects}
            activeId={activeId}
            unreadBySession={unreadBySession}
            onActivate={handleTabActivate}
            onClose={handleTabClose}
          />
        )}

        {hasOpen ? (
          // Рендерим ВСЕ открытые ProjectView, скрываем неактивные через display:none —
          // это сохраняет состояние чата/SSE при переключении между вкладками.
          openProjects.map(p => (
            <div
              key={p.id}
              className="project-tab-slot"
              style={{ display: p.id === activeId ? 'flex' : 'none' }}
            >
              <ProjectView project={p} onProjectsReload={loadProjects} />
            </div>
          ))
        ) : (
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

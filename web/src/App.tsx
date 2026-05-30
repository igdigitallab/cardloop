import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from './api'
import { Project } from './types'
import { LoginScreen } from './components/LoginScreen'
import { Sidebar } from './components/Sidebar'
import { ProjectView } from './components/ProjectView'
import { Spinner } from './components/Spinner'

type AuthState = 'loading' | 'unauthed' | 'authed'

const LS_RECENT = 'cops.recentProjects'
const LS_UNREAD = 'cops.unreadBySession'
const LS_SIDEBAR_COLLAPSED = 'cops.sidebarCollapsed'
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

export default function App() {
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [recentIds, setRecentIds] = useState<string[]>(() => readRecent())
  const [unreadBySession, setUnreadBySession] = useState<Record<string, number>>(() => readUnread())
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => readBool(LS_SIDEBAR_COLLAPSED, false))
  // Текущий выбранный проект — для SSE-обработчика, без перепересоздания подписки на каждом select
  const selectedIdRef = useRef<string | null>(null)
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
  useEffect(() => { selectedIdRef.current = selectedId }, [selectedId])
  useEffect(() => { projectsRef.current = projects }, [projects])

  // Глобальный SSE-стрим активности → unread-индикаторы в сайдбаре
  useEffect(() => {
    if (authState !== 'authed') return

    const es = new EventSource('/api/activity-stream')
    es.onmessage = (ev) => {
      let payload: { kind?: string; session_key?: string }
      try { payload = JSON.parse(ev.data) } catch { return }
      const sk = payload.session_key
      if (!sk) return
      // Учитываем только значимые события — игнорируем run_start/run_end (одиночные шумные точки)
      if (payload.kind !== 'text' && payload.kind !== 'tool') return
      // Если событие пришло по СЕЙЧАС открытому проекту — не помечаем как непрочитанное
      const proj = projectsRef.current.find(p => p.tg_thread != null && String(p.tg_thread) === sk)
      if (proj && proj.id === selectedIdRef.current) return
      setUnreadBySession(prev => {
        const next = { ...prev, [sk]: (prev[sk] || 0) + 1 }
        writeUnread(next)
        return next
      })
    }
    es.onerror = () => { /* EventSource сам переподключится */ }
    return () => { es.close() }
  }, [authState])

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id)
    setRecentIds(prev => {
      const next = [id, ...prev.filter(x => x !== id)].slice(0, RECENT_MAX)
      writeRecent(next)
      return next
    })
    // Сброс непрочитанных по этому проекту
    const proj = projectsRef.current.find(p => p.id === id)
    const sk = proj?.tg_thread != null ? String(proj.tg_thread) : null
    if (sk) {
      setUnreadBySession(prev => {
        if (!prev[sk]) return prev
        const next = { ...prev }
        delete next[sk]
        writeUnread(next)
        return next
      })
    }
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
    setSelectedId(null)
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

  const selectedProject = projects.find(p => p.id === selectedId) ?? null

  // Sort: recently opened first (in recency order), then the rest in original order
  const recentRank = new Map(recentIds.map((id, i) => [id, i]))
  const sortedProjects = [...projects].sort((a, b) => {
    const ra = recentRank.has(a.id) ? recentRank.get(a.id)! : Infinity
    const rb = recentRank.has(b.id) ? recentRank.get(b.id)! : Infinity
    if (ra !== rb) return ra - rb
    return 0 // stable: keep API order for non-recent
  })

  return (
    <div className={`app-layout${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
      <Sidebar
        projects={sortedProjects}
        selectedId={selectedId}
        onSelect={handleSelect}
        onLogout={handleLogout}
        loading={projectsLoading}
        unreadBySession={unreadBySession}
        collapsed={sidebarCollapsed}
        onToggleCollapse={toggleSidebar}
      />

      {selectedProject ? (
        <ProjectView key={selectedProject.id} project={selectedProject} onProjectsReload={loadProjects} />
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
  )
}

import { useEffect, useState, useCallback } from 'react'
import { api } from './api'
import { Project } from './types'
import { LoginScreen } from './components/LoginScreen'
import { Sidebar } from './components/Sidebar'
import { ProjectView } from './components/ProjectView'
import { Spinner } from './components/Spinner'

type AuthState = 'loading' | 'unauthed' | 'authed'

export default function App() {
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)

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

  async function handleLogout() {
    try { await api.logout() } catch { /* ignore */ }
    setProjects([])
    setSelectedId(null)
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

  return (
    <div className="app-layout">
      <Sidebar
        projects={projects}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onLogout={handleLogout}
        loading={projectsLoading}
      />

      {selectedProject ? (
        <ProjectView key={selectedProject.id} project={selectedProject} />
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

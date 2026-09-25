/**
 * The pop-out window: ONE project in its own OS window (see lib/popout.ts for what it
 * deliberately leaves out and why). Mounted by main.tsx instead of <App/> when the URL
 * carries ?popout=<projectId>.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Project } from '../types'
import { LoginScreen } from './LoginScreen'
import { ProjectView } from './ProjectView'
import { Spinner } from './Spinner'
import { UsageBadge } from './UsageBadge'
import { UpdatePill } from './UpdatePill'
import { ModulesProvider } from '../hooks/useModules'
import { useBuildWatch } from '../hooks/useBuildWatch'
import { isAppBusy } from '../lib/appBusy'
import {
  PopoutParams, WindowBounds, applyBounds, isMisplaced, isPopoutWindow, placementState, readBounds,
  requestPlacement, trackBounds,
} from '../lib/popout'

type AuthState = 'loading' | 'unauthed' | 'authed'

/** Same cadence as the main window's project poll (health / model / git chips). */
const PROJECT_POLL_MS = 15_000
/** A project absent from this many polls in a row is gone; one miss is a reload race and must
 *  not unmount the window (that would drop its chat stream and browser pane). */
const MISSES_BEFORE_GONE = 3

/** Plain HTTP (not localhost) = one shared 6-connection pool for every window of the browser. */
const PLAIN_HTTP = location.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(location.hostname)

export function PopoutApp({ params }: { params: PopoutParams }) {
  const { projectId } = params
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [project, setProject] = useState<Project | null>(null)
  const [missing, setMissing] = useState(false)
  const missesRef = useRef(0)
  // Hold the project's live stream only while the window can be seen: on a plain-HTTP origin
  // every open stream eats one of the 6 connections all windows share.
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible')
  const [models, setModels] = useState<{ value: string; label: string }[] | undefined>(undefined)
  // Remembered bounds this window failed to reach (Chrome clamped it to another monitor).
  const [misplaced, setMisplaced] = useState<WindowBounds | null>(null)
  const { updateReady, applyUpdate } = useBuildWatch(() => !isAppBusy())

  useEffect(() => {
    api.me()
      .then(res => setAuthState(res.authed ? 'authed' : 'unauthed'))
      .catch(() => setAuthState('unauthed'))
  }, [])

  const loadProject = useCallback(async () => {
    try {
      const res = await api.projects()
      const found = res.projects.find(p => p.id === projectId) ?? null
      if (!found) {
        missesRef.current += 1
        // Keep the last known project mounted until the miss repeats.
        if (missesRef.current >= MISSES_BEFORE_GONE) { setMissing(true); setProject(null) }
        return
      }
      missesRef.current = 0
      setMissing(false)
      // Keep the same object when nothing changed, so ProjectView does not re-run its effects
      // on every poll.
      setProject(prev => (prev && JSON.stringify(prev) === JSON.stringify(found) ? prev : found))
    } catch {
      // Transient failure: keep what is on screen.
    }
  }, [projectId])

  useEffect(() => {
    if (authState !== 'authed') return
    void loadProject()
    api.models().then(res => setModels(res.models)).catch(() => { /* static fallback */ })
    const id = window.setInterval(() => { void loadProject() }, PROJECT_POLL_MS)
    const onFocus = () => { void loadProject() }
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearInterval(id)
      window.removeEventListener('focus', onFocus)
    }
  }, [authState, loadProject])

  useEffect(() => {
    document.title = project ? `${project.name} · Cardloop` : 'Cardloop'
  }, [project])

  useEffect(() => {
    const onVis = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  // Return to the remembered monitor, and keep remembering where the operator puts it.
  useEffect(() => {
    // Only a real pop-out remembers its place: the same URL opened in an ordinary tab (a
    // bookmark) would otherwise record the tab's bounds for the next window.open.
    if (!isPopoutWindow()) return
    const saved = readBounds(projectId)
    const off = saved ? isMisplaced(saved, window.screenX, window.screenY) : false
    const stop = trackBounds(projectId, !off)
    if (saved && off) {
      void placementState().then(state => {
        if (state === 'granted') applyBounds(saved)
        else if (state === 'prompt') setMisplaced(saved)
      })
    }
    return stop
  }, [projectId])

  const moveToMonitor = useCallback(async () => {
    const target = misplaced
    setMisplaced(null)
    if (target && await requestPlacement()) applyBounds(target)
  }, [misplaced])

  if (authState === 'loading') {
    return <div className="popout-center"><Spinner /></div>
  }
  if (authState === 'unauthed') {
    return <LoginScreen onLogin={() => setAuthState('authed')} />
  }
  if (missing) {
    return (
      <div className="popout-center">
        <div>Project not found — it may have been archived or renamed.</div>
        <button className="btn btn-secondary btn-sm" onClick={() => window.close()}>Close window</button>
      </div>
    )
  }
  if (!project) {
    return <div className="popout-center"><Spinner /></div>
  }

  return (
    <ModulesProvider>
      <div className="app-layout popout-layout">
        {updateReady && <UpdatePill onApply={applyUpdate} />}
        <div className="main-area">
          <div className="project-tabbar popout-bar">
            <span className="popout-bar-title" title={project.cwd}>⧉ {project.name}</span>
            {misplaced && (
              <button
                className="popout-bar-btn"
                onClick={moveToMonitor}
                title="Chrome opened this window away from the monitor it was on. Allow the cockpit to manage windows on all your displays, and it returns there — now and on every next open."
              >
                🖥 Move to its monitor
              </button>
            )}
            <div className="ptab-spacer" />
            {PLAIN_HTTP && (
              <span
                className="popout-bar-warn"
                title="This cockpit is served over plain HTTP: the browser allows only 6 connections per host for ALL windows together, and each window with a chat holds one or two. With several pop-outs and running turns the cockpit can stop loading. Serve it over HTTPS (HTTP/2) to lift the limit."
              >⚠ HTTP/1.1</span>
            )}
            <UsageBadge />
          </div>
          <div className="project-tab-slot">
            <div className="split-pane-full">
              <ProjectView
                project={project}
                onProjectsReload={loadProject}
                isActive={visible}
                models={models}
                popout
                initialTab={params.tab ?? 'browser'}
              />
            </div>
          </div>
        </div>
      </div>
    </ModulesProvider>
  )
}

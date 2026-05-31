import { useCallback, useEffect, useRef, useState } from 'react'
import { Project, TabId } from '../types'
import { api } from '../api'
import { ProjectActivityProvider } from '../hooks/useProjectActivity'
import { OverviewTab } from '../tabs/OverviewTab'
import { ClaudeMdTab } from '../tabs/ClaudeMdTab'
import { LogsTab } from '../tabs/LogsTab'
import { BoardTab } from '../tabs/BoardTab'
import { ChatTab } from '../tabs/ChatTab'
import { FilesTab } from '../tabs/FilesTab'
import { MemoryTab } from '../tabs/MemoryTab'

interface Tab {
  id: TabId
  label: string
  disabled?: boolean
}

// Chat is no longer a tab — it lives in the permanent right panel
const TABS: Tab[] = [
  { id: 'overview',  label: 'Обзор' },
  { id: 'claude-md', label: 'CLAUDE.md' },
  { id: 'logs',      label: 'Логи' },
  { id: 'board',     label: 'Доска' },
  { id: 'files',     label: 'Файлы' },
  { id: 'memory',    label: 'Память' },
]

// localStorage keys
const LS_WIDTH    = 'cops.chatWidth'
const LS_COLLAPSED = 'cops.chatCollapsed'

const CHAT_MIN_PCT = 20
const CHAT_MAX_PCT = 70
const CHAT_DEFAULT_PCT = 45

function readLS(key: string, fallback: number): number {
  try {
    const v = localStorage.getItem(key)
    if (v === null) return fallback
    const n = parseFloat(v)
    return isNaN(n) ? fallback : n
  } catch {
    return fallback
  }
}

function readLSBool(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v === null) return fallback
    return v === 'true'
  } catch {
    return fallback
  }
}

interface Props {
  project: Project
  onProjectsReload: () => void
  onSplitCreate?: () => void   // показать кнопку ⊞ (только для левого free-чата)
  onSplitClose?: () => void    // показать кнопку ✕ Закрыть (только для правого)
}

type GitSyncState = 'idle' | 'busy' | 'ok' | 'err'

export function ProjectView({ project, onProjectsReload, onSplitCreate, onSplitClose }: Props) {
  const [activeTab, setActiveTab] = useState<TabId>('board')
  const git = project.health.git

  // ── Git sync (commit + push одной кнопкой) ────────────────────────────────
  const [syncState, setSyncState] = useState<GitSyncState>('idle')
  const [syncMsg, setSyncMsg] = useState<string>('')

  const gitDirty = (git?.dirty ?? 0) > 0
  const gitUnpushed = (git?.unpushed ?? 0) > 0
  const gitNeedsSync = gitDirty || gitUnpushed
  // Цвет точки: серый если git недоступен, жёлтый если есть что синхронизировать, зелёный если чисто
  const gitDotClass = !git ? 'gray' : gitNeedsSync ? 'yellow' : 'green'
  const gitDotTitle = !git
    ? 'Git недоступен'
    : gitNeedsSync
      ? `${gitDirty ? `${git!.dirty} изменено` : ''}${gitDirty && gitUnpushed ? ', ' : ''}${gitUnpushed ? `${git!.unpushed} не отправлено` : ''}`
      : 'Чисто, всё запушено'

  const onGitSync = useCallback(async () => {
    if (syncState === 'busy') return
    setSyncState('busy')
    setSyncMsg('')
    try {
      const res = await api.gitSync(project.id)
      const parts: string[] = []
      if (res.committed) parts.push(`коммит: ${res.message}`)
      if (res.pushed) parts.push('запушено')
      setSyncMsg(parts.join(' · ') || 'нечего синхронизировать')
      setSyncState('ok')
      onProjectsReload()
      setTimeout(() => setSyncState(s => (s === 'ok' ? 'idle' : s)), 3000)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setSyncMsg(msg.slice(0, 200))
      setSyncState('err')
      setTimeout(() => setSyncState(s => (s === 'err' ? 'idle' : s)), 6000)
    }
  }, [project.id, syncState, onProjectsReload])

  // ── Resize / collapse state (persisted in localStorage) ──────────────────
  const [chatWidth, setChatWidth] = useState<number>(() =>
    readLS(LS_WIDTH, CHAT_DEFAULT_PCT)
  )
  const [collapsed, setCollapsed] = useState<boolean>(() =>
    readLSBool(LS_COLLAPSED, false)
  )
  // Remember width before collapse so we can restore it
  const widthBeforeCollapse = useRef<number>(chatWidth)

  // Detect narrow viewport — disable resize below 900px
  const [narrow, setNarrow] = useState(() => window.innerWidth < 900)
  useEffect(() => {
    function onResize() { setNarrow(window.innerWidth < 900) }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Persist changes
  useEffect(() => {
    try { localStorage.setItem(LS_WIDTH, String(chatWidth)) } catch {}
  }, [chatWidth])
  useEffect(() => {
    try { localStorage.setItem(LS_COLLAPSED, String(collapsed)) } catch {}
  }, [collapsed])

  // ── Drag logic ────────────────────────────────────────────────────────────
  const containerRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  const onDividerMouseDown = useCallback((e: React.MouseEvent) => {
    if (narrow) return
    e.preventDefault()
    dragging.current = true
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    function onMouseMove(ev: MouseEvent) {
      if (!dragging.current || !containerRef.current) return
      const rect = containerRef.current.getBoundingClientRect()
      const totalW = rect.width
      if (totalW === 0) return
      // chatWidth = pct of the right part (from divider to right edge)
      const chatPx = rect.right - ev.clientX
      let pct = (chatPx / totalW) * 100
      pct = Math.max(CHAT_MIN_PCT, Math.min(CHAT_MAX_PCT, pct))
      setChatWidth(pct)
    }

    function onMouseUp() {
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', onMouseMove)
      document.removeEventListener('mouseup', onMouseUp)
    }

    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
  }, [narrow])

  // ── Collapse / expand ─────────────────────────────────────────────────────
  function toggleCollapse() {
    if (collapsed) {
      // Expand: restore previous width
      setChatWidth(widthBeforeCollapse.current)
      setCollapsed(false)
    } else {
      // Collapse: remember current width
      widthBeforeCollapse.current = chatWidth
      setCollapsed(true)
    }
  }

  // ── Inline styles for panels ──────────────────────────────────────────────
  // On narrow screens use CSS classes only (no inline flex-basis override)
  const leftStyle: React.CSSProperties = narrow || collapsed
    ? {}
    : { flex: `0 0 ${100 - chatWidth}%`, maxWidth: `${100 - chatWidth}%` }

  const chatStyle: React.CSSProperties = narrow
    ? {}
    : collapsed
      ? { flex: '0 0 0', overflow: 'hidden', minWidth: 0 }
      : { flex: `0 0 ${chatWidth}%`, maxWidth: `${chatWidth}%` }

  // Свободный чат — без левой панели табов, чат на всю ширину.
  if (project.is_free) {
    return (
      <ProjectActivityProvider projectId={project.id}>
        <div className="main-content project-free-layout">
          {(onSplitCreate || onSplitClose) && (
            <div className="free-chat-toolbar">
              <span className="free-chat-name">{project.name}</span>
              {onSplitCreate && (
                <button className="split-create-btn" onClick={onSplitCreate} title="Открыть второй чат рядом">
                  ⊞ Split
                </button>
              )}
              {onSplitClose && (
                <button className="split-close-btn" onClick={onSplitClose} title="Закрыть эту панель">
                  ✕ Закрыть
                </button>
              )}
            </div>
          )}
          <ChatTab project={project} onProjectsReload={onProjectsReload} />
        </div>
      </ProjectActivityProvider>
    )
  }

  return (
    <ProjectActivityProvider projectId={project.id}>
    <div className="main-content project-split-layout" ref={containerRef}>
      {/* LEFT: header + tabs + content */}
      <div className="project-left-pane" style={leftStyle}>
        <div className="project-header">
          <div className="project-header-top">
            <div className="project-header-icon">
              {project.name.charAt(0).toUpperCase()}
            </div>
            <div>
              <div className="project-title">{project.name}</div>
              <div className="project-meta-row">
                <span className="meta-chip">
                  <code>{project.cwd}</code>
                </span>
                <span className="meta-chip">{project.model}</span>
                {git && (
                  <span className="git-status">
                    <span className={`git-sync-dot ${gitDotClass}`} title={gitDotTitle} />
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                      style={{ opacity: 0.5 }}>
                      <circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/>
                      <path d="M6 21V9a9 9 0 0 0 9 9"/>
                    </svg>
                    <span className="git-branch">{git.branch}</span>
                    {git.dirty > 0 && (
                      <span className="git-dirty" title={`${git.dirty} изменённых файлов`}>
                        ~{git.dirty}
                      </span>
                    )}
                    {git.unpushed > 0 && (
                      <span className="git-unpushed" title={`${git.unpushed} не отправлено`}>
                        ↑{git.unpushed}
                      </span>
                    )}
                    {gitNeedsSync && (
                      <button
                        className={`git-sync-btn ${syncState}`}
                        onClick={onGitSync}
                        disabled={syncState === 'busy'}
                        title={gitDirty ? 'Закоммитить всё и запушить' : 'Запушить коммиты'}
                      >
                        {syncState === 'busy' ? '…' : '↑ Sync'}
                      </button>
                    )}
                    {syncState !== 'idle' && syncMsg && (
                      <span className={`git-sync-msg ${syncState}`} title={syncMsg}>
                        {syncState === 'ok' ? '✓ ' : syncState === 'err' ? '✗ ' : ''}{syncMsg}
                      </span>
                    )}
                  </span>
                )}
              </div>
            </div>
          </div>

          <nav className="tabs">
            {TABS.map(tab => (
              <button
                key={tab.id}
                className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
                disabled={tab.disabled}
                onClick={() => !tab.disabled && setActiveTab(tab.id)}
              >
                {tab.label}
                {tab.disabled && <span className="tab-soon">скоро</span>}
              </button>
            ))}
          </nav>
        </div>

        <div className="tab-content">
          {activeTab === 'overview'  && <OverviewTab project={project} />}
          {activeTab === 'claude-md' && <ClaudeMdTab projectId={project.id} />}
          {activeTab === 'logs'      && <LogsTab projectId={project.id} projectName={project.name} />}
          {activeTab === 'board'     && <BoardTab projectId={project.id} />}
          {activeTab === 'files'     && <FilesTab projectId={project.id} />}
          {activeTab === 'memory'    && <MemoryTab projectId={project.id} />}
        </div>
      </div>

      {/* DIVIDER — draggable handle (hidden on narrow screens and when collapsed) */}
      {!narrow && (
        <div
          className={`project-split-divider${collapsed ? ' divider-collapsed' : ''}`}
          onMouseDown={onDividerMouseDown}
          title={collapsed ? 'Развернуть чат' : 'Перетащить для изменения размера'}
          onClick={collapsed ? toggleCollapse : undefined}
        />
      )}

      {/* RIGHT: permanent chat panel */}
      <div className="project-chat-pane" style={chatStyle}>
        <div className="project-chat-pane-header">
          <span>💬 Чат по проекту</span>
          {!narrow && (
            <button
              className="chat-collapse-btn"
              onClick={toggleCollapse}
              title={collapsed ? 'Развернуть чат' : 'Свернуть чат'}
            >
              {collapsed ? '⟨' : '⟩'}
            </button>
          )}
        </div>
        <ChatTab project={project} onProjectsReload={onProjectsReload} />
      </div>

      {/* COLLAPSED STUB — narrow vertical button when chat is collapsed */}
      {!narrow && collapsed && (
        <button
          className="chat-collapsed-stub"
          onClick={toggleCollapse}
          title="Развернуть чат"
        >
          💬
        </button>
      )}
    </div>
    </ProjectActivityProvider>
  )
}

// DisabledTab reserved for future tabs that are not yet implemented
export function DisabledTab({ name, icon }: { name: string; icon: string }) {
  return (
    <div className="tab-placeholder">
      <div className="tab-placeholder-icon">{icon}</div>
      <h3>{name}</h3>
      <p>Эта функция появится в следующих фазах</p>
    </div>
  )
}

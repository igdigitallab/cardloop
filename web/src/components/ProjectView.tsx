import { useCallback, useEffect, useRef, useState } from 'react'
import { Project, ProjectStructureHealth, TabId } from '../types'
import { api } from '../api'
import { ProjectActivityProvider, useOnRunEnd, useProjectActivity } from '../hooks/useProjectActivity'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { Modal } from '../components/Modal'
import { TestResult } from '../types'
import { ClaudeMdTab } from '../tabs/ClaudeMdTab'
import { LogsTab } from '../tabs/LogsTab'
import { BoardTab } from '../tabs/BoardTab'
import { ChatTab } from '../tabs/ChatTab'
import { FilesTab } from '../tabs/FilesTab'
import { MemoryTab } from '../tabs/MemoryTab'
import { TimelineTab } from '../tabs/TimelineTab'
import { SettingsTab } from '../tabs/SettingsTab'
import { t } from '../i18n'

interface Tab {
  id: TabId
  label: string
  disabled?: boolean
}

// secrets tab removed (merged into Settings); overview tab removed (merged into Settings); 7 tabs remain
const TABS: Tab[] = [
  { id: 'claude-md', label: t['tab.claude_md'] },
  { id: 'logs',      label: t['tab.logs'] },
  { id: 'board',     label: t['tab.board'] },
  { id: 'files',     label: t['tab.files'] },
  { id: 'memory',    label: t['tab.memory'] },
  { id: 'timeline',  label: t['tab.timeline'] },
  { id: 'settings',  label: t['tab.settings'] },
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

const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$/

interface Props {
  project: Project
  onProjectsReload: () => void
  onRenameSuccess?: (oldId: string, newId: string) => void
  onSplitCreate?: () => void   // show ⊞ button (left free-chat only)
  onSplitClose?: () => void    // show ✕ Close button (right pane only)
  /** Passed to ChatTab for restoring running-state when switching back to the tab. */
  isActive?: boolean
}

type GitSyncState = 'idle' | 'busy' | 'ok' | 'err'

// ── Agent running indicator (must be inside ProjectActivityProvider) ──────────
function AgentRunningChip({ projectId }: { projectId: string }) {
  const [running, setRunning] = useState(false)

  // Seed state from backend on mount so a mid-run page refresh shows the chip
  useEffect(() => {
    api.projectRunning(projectId)
      .then(res => { if (res.running) setRunning(true) })
      .catch(() => { /* non-critical */ })
  }, [projectId])

  useProjectActivity(evt => {
    if (evt.kind === 'run_start') setRunning(true)
    else if (evt.kind === 'run_end') setRunning(false)
  })

  if (!running) return null
  return (
    <span
      className="agent-running-chip"
      title={t['header.agent_running']}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 11,
        color: 'var(--accent)',
        fontWeight: 600,
        animation: 'agent-pulse 1.4s ease-in-out infinite',
      }}
    >
      {t['header.agent_running']}
    </span>
  )
}

// ── Incidents chip (inside provider, uses project.incidents from prop) ────────
function IncidentsChip({ count, onNavigate }: { count: number; onNavigate: () => void }) {
  if (count <= 0) return null
  return (
    <button
      className="incidents-chip"
      onClick={onNavigate}
      title={`${t['header.incidents_chip']} ${count} active incident(s) — go to board`}
      style={{
        background: 'var(--red)',
        color: '#fff',
        border: 'none',
        borderRadius: 4,
        padding: '2px 7px',
        fontSize: 11,
        fontWeight: 700,
        cursor: 'pointer',
        lineHeight: 1.4,
      }}
    >
      {t['header.incidents_chip']} {count}
    </button>
  )
}

// ── Always-mounted: refresh health on run_end (single subscription, no tab dependency) ──
function HealthRunEndRefresher({ refresh }: { refresh: () => void }) {
  useOnRunEnd(refresh)
  return null
}

// ── Header test runner button ─────────────────────────────────────────────────
// Runs test_cmd on demand. Shows a summary (passed/failed + exit code);
// click on summary → modal with full output (which tests failed). No inline output
// in the button — otherwise failure details are invisible.
function HeaderTestRunner({ projectId }: { projectId: string }) {
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<TestResult | null>(null)
  const [showOutput, setShowOutput] = useState(false)

  async function run() {
    if (running) return
    setRunning(true); setResult(null); setShowOutput(false)
    try {
      const res = await api.runTests(projectId)
      setResult(res)
    } catch (e: unknown) {
      setResult({
        detected: false, ok: false, cmd: null, exit_code: null,
        output: 'Request failed: ' + (e instanceof Error ? e.message : String(e)),
      })
    } finally {
      setRunning(false)
    }
  }

  // Short summary of results
  let summary: { label: string; color: string } | null = null
  if (result) {
    if (!result.detected) {
      summary = { label: '? no tests found', color: 'var(--text-dim)' }
    } else if (result.timed_out) {
      summary = { label: '⏱ timeout', color: 'var(--red)' }
    } else if (result.ok) {
      summary = { label: `✓ passed (exit ${result.exit_code})`, color: 'var(--green)' }
    } else {
      summary = { label: `✗ failed (exit ${result.exit_code})`, color: 'var(--red)' }
    }
  }

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <button
        className="git-sync-btn"
        style={{ fontSize: 11, padding: '2px 8px' }}
        onClick={run}
        disabled={running}
        title={t['header.run_tests']}
      >
        {running ? t['header.running_tests'] : t['header.run_tests']}
      </button>
      {summary && (
        <button
          onClick={() => setShowOutput(true)}
          title="Show full output"
          style={{
            fontSize: 11, color: summary.color, fontWeight: 700, fontFamily: 'inherit',
            background: 'none', border: 'none', cursor: 'pointer', padding: 0,
            textDecoration: 'underline dotted',
          }}
        >
          {summary.label}
        </button>
      )}
      {showOutput && result && (
        <Modal onClose={() => setShowOutput(false)} className="test-output-modal">
          <h3 style={{ marginTop: 0 }}>Test results</h3>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>
            {result.cmd ? <code>{result.cmd}</code> : 'no tests detected'}
            {result.exit_code != null && ` · exit ${result.exit_code}`}
            {result.timed_out && ' · timeout'}
          </div>
          <pre style={{
            maxHeight: '60vh', overflow: 'auto', fontSize: 12, lineHeight: 1.45,
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            background: 'var(--bg-code, rgba(0,0,0,0.25))', padding: 10, borderRadius: 6,
          }}>
            {result.output || '(empty output)'}
          </pre>
        </Modal>
      )}
    </span>
  )
}

export function ProjectView({ project, onProjectsReload, onRenameSuccess, onSplitCreate, onSplitClose, isActive }: Props) {
  const [activeTab, setActiveTab] = useState<TabId>('board')
  // Mobile inner tab: null = show chat (default), TabId = show that inner tab
  const [mobileInnerTab, setMobileInnerTab] = useState<TabId | null>(null)
  const git = project.health.git

  // ── Rename state ──────────────────────────────────────────────────────────
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [renameError, setRenameError] = useState('')
  const renameInputRef = useRef<HTMLInputElement>(null)

  function startRename() {
    setRenameValue(project.id)
    setRenameError('')
    setRenaming(true)
  }

  function cancelRename() {
    setRenaming(false)
    setRenameError('')
  }

  async function commitRename() {
    const slug = renameValue.trim()
    if (!SLUG_RE.test(slug)) {
      setRenameError(t['project.rename_error_format'])
      return
    }
    if (slug === project.id) { cancelRename(); return }
    try {
      const res = await api.renameProject(project.id, slug)
      setRenaming(false)
      setRenameError('')
      onProjectsReload()
      onRenameSuccess?.(project.id, res.new_id)
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        setRenameError(t['project.rename_error_busy'])
      } else {
        setRenameError(e instanceof Error ? e.message : String(e))
      }
    }
  }

  // ── Structure health: single source of truth (no double-fetch) ────────────
  const [structHealth, setStructHealth] = useState<ProjectStructureHealth | null>(null)

  const refreshHealth = useCallback(() => {
    api.projectHealth(project.id)
      .then(h => setStructHealth(h))
      .catch(() => { /* endpoint may not exist yet */ })
  }, [project.id])

  useEffect(() => {
    refreshHealth()
  }, [refreshHealth])

  // ── Git sync (commit + push in one button) ───────────────────────────────
  const [syncState, setSyncState] = useState<GitSyncState>('idle')
  const [syncMsg, setSyncMsg] = useState<string>('')

  const gitDirty = (git?.dirty ?? 0) > 0
  const gitUnpushed = (git?.unpushed ?? 0) > 0
  const gitNeedsSync = gitDirty || gitUnpushed
  // Dot color: gray if git unavailable, yellow if sync needed, green if clean
  const gitDotClass = !git ? 'gray' : gitNeedsSync ? 'yellow' : 'green'
  const gitDotTitle = !git
    ? 'Git not available'
    : gitNeedsSync
      ? `${gitDirty ? `${git!.dirty} changed` : ''}${gitDirty && gitUnpushed ? ', ' : ''}${gitUnpushed ? `${git!.unpushed} unpushed` : ''}`
      : t['git.sync_clean']

  const onGitSync = useCallback(async () => {
    if (syncState === 'busy') return
    setSyncState('busy')
    setSyncMsg('')
    try {
      const res = await api.gitSync(project.id)
      const parts: string[] = []
      if (res.committed) parts.push(`commit: ${res.message}`)
      if (res.pushed) parts.push(t['git.sync_pushed'])
      setSyncMsg(parts.join(' · ') || t['git.sync_nothing'])
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

  // Detect narrow viewport — disable resize below 768px (matches CSS breakpoint)
  const [narrow, setNarrow] = useState(() => window.innerWidth < 768)
  useEffect(() => {
    function onResize() { setNarrow(window.innerWidth < 768) }
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

  // incidents count: project.incidents is defined in types.ts
  const incidentsCount = project.incidents ?? 0

  // Free chat — no left tab panel, chat at full width.
  if (project.is_free) {
    return (
      <ProjectActivityProvider projectId={project.id}>
        <div className="main-content project-free-layout">
          {(onSplitCreate || onSplitClose) && (
            <div className="free-chat-toolbar">
              <span className="free-chat-name">{project.name}</span>
              {onSplitCreate && (
                <button className="split-create-btn" onClick={onSplitCreate} title={t['split.open_second_chat']}>
                  ⊞ Split
                </button>
              )}
              {onSplitClose && (
                <button className="split-close-btn" onClick={onSplitClose} title={t['split.close_panel']}>
                  ✕ Close
                </button>
              )}
            </div>
          )}
          <ErrorBoundary label="Chat">
            <ChatTab project={project} onProjectsReload={onProjectsReload} isActive={isActive} />
          </ErrorBoundary>
        </div>
      </ProjectActivityProvider>
    )
  }

  // ── Mobile narrow branch (≤768px, non-free projects only) ──────────────────
  if (narrow && !project.is_free) {
    return (
      <ProjectActivityProvider projectId={project.id}>
        <div className="main-content mobile-project-layout">
          {/* Compact project header row */}
          <div className="mobile-project-header">
            <span className="mobile-project-title">{project.name}</span>
            {git && <span className={`git-sync-dot ${gitDotClass}`} title={gitDotTitle} />}
            <AgentRunningChip projectId={project.id} />
          </div>
          {/* Inner tab strip (secondary row) */}
          <nav className="mobile-inner-tabs" aria-label={t['tab.sections_aria']}>
            <button
              className={`mobile-inner-tab-btn ${mobileInnerTab === null ? 'active' : ''}`}
              onClick={() => setMobileInnerTab(null)}
            >
              💬 Chat
            </button>
            {TABS.map(tab => (
              <button
                key={tab.id}
                className={`mobile-inner-tab-btn ${mobileInnerTab === tab.id ? 'active' : ''}`}
                onClick={() => setMobileInnerTab(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </nav>
          {/* Content area: chat or inner tab */}
          <div className="mobile-project-content">
            {mobileInnerTab === null ? (
              <ErrorBoundary label="Chat">
                <ChatTab project={project} onProjectsReload={onProjectsReload} isActive={isActive} />
              </ErrorBoundary>
            ) : (
              <>
                {mobileInnerTab === 'claude-md' && <ErrorBoundary label="CLAUDE.md"><ClaudeMdTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'logs'      && <ErrorBoundary label="Logs"><LogsTab projectId={project.id} projectName={project.name} /></ErrorBoundary>}
                {mobileInnerTab === 'board'     && <ErrorBoundary label="Board"><BoardTab projectId={project.id} isActive={isActive} /></ErrorBoundary>}
                {mobileInnerTab === 'files'     && <ErrorBoundary label="Files"><FilesTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'memory'    && <ErrorBoundary label="Memory"><MemoryTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'timeline'  && <ErrorBoundary label="Activity"><TimelineTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'settings'  && <ErrorBoundary label="Settings"><SettingsTab projectId={project.id} project={project} health={structHealth} refreshHealth={refreshHealth} /></ErrorBoundary>}
              </>
            )}
          </div>
          <HealthRunEndRefresher refresh={refreshHealth} />
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
            <div style={{ minWidth: 0, flex: 1 }}>
              {renaming ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <input
                      ref={renameInputRef}
                      autoFocus
                      className="rename-input"
                      value={renameValue}
                      onChange={e => { setRenameValue(e.target.value); setRenameError('') }}
                      onKeyDown={e => {
                        if (e.key === 'Enter') { e.preventDefault(); commitRename() }
                        if (e.key === 'Escape') cancelRename()
                      }}
                      placeholder="new-slug"
                      style={{ fontSize: 18, fontWeight: 600, letterSpacing: '-0.4px', flex: 1, minWidth: 0 }}
                    />
                    <button className="rename-confirm-btn" onClick={commitRename} title={t['project.rename_apply']}>✓</button>
                    <button className="rename-cancel-btn" onClick={cancelRename} title={t['project.rename_cancel']}>✕</button>
                  </div>
                  {renameError && <div style={{ fontSize: 11, color: 'var(--red)' }}>{renameError}</div>}
                </div>
              ) : (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <div className="project-title">{project.name}</div>
                  <button
                    className="rename-edit-btn"
                    onClick={startRename}
                    title={t['project.rename_title']}
                  >✏️</button>
                </div>
              )}
              <div className="project-meta-row">
                <span className="meta-chip">
                  <code>{project.cwd}</code>
                </span>
                {structHealth && (
                  <button
                    className={`health-badge health-badge-${structHealth.color}`}
                    onClick={() => setActiveTab('settings')}
                    title={structHealth.items.find(i => !i.ok)?.label ?? t['project.health_all_ok']}
                  >
                    <span className={`git-sync-dot ${structHealth.color === 'red' ? 'yellow' : structHealth.color}`} />
                    health {structHealth.score}/{structHealth.total}
                  </button>
                )}
                {/* Incidents chip — shown when project has active incidents */}
                <IncidentsChip
                  count={incidentsCount}
                  onNavigate={() => setActiveTab('board')}
                />
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
                    {git.visibility && (
                      <span
                        className={`git-vis git-vis-${git.visibility}`}
                        title={git.visibility === 'private' ? 'Private GitHub repository' : 'Public (open) GitHub repository'}
                      >
                        {git.visibility === 'private' ? '🔒' : '🌐'}
                      </span>
                    )}
                    {git.dirty > 0 && (
                      <span className="git-dirty" title={`${git.dirty} changed file(s)`}>
                        ~{git.dirty}
                      </span>
                    )}
                    {git.unpushed > 0 && (
                      <span className="git-unpushed" title={`${git.unpushed} unpushed`}>
                        ↑{git.unpushed}
                      </span>
                    )}
                    {/* Sync button shown always (not gated on gitNeedsSync) */}
                    <button
                      className={`git-sync-btn ${syncState}`}
                      onClick={onGitSync}
                      disabled={syncState === 'busy'}
                      title={gitDirty ? t['git.commit_and_push'] : t['git.push']}
                    >
                      {syncState === 'busy' ? '…' : '↑ Sync'}
                    </button>
                    {syncState !== 'idle' && syncMsg && (
                      <span className={`git-sync-msg ${syncState}`} title={syncMsg}>
                        {syncState === 'ok' ? '✓ ' : syncState === 'err' ? '✗ ' : ''}{syncMsg}
                      </span>
                    )}
                  </span>
                )}
                {/* Agent running indicator + test runner — inside provider */}
                <AgentRunningChip projectId={project.id} />
                <HeaderTestRunner projectId={project.id} />
                {/* Always-mounted: single run_end→refreshHealth path */}
                <HealthRunEndRefresher refresh={refreshHealth} />
              </div>
            </div>
          </div>

          <nav className="tabs" role="tablist" aria-label={t['tab.sections_aria']}>
            {TABS.map(tab => (
              <button
                key={tab.id}
                role="tab"
                aria-selected={activeTab === tab.id}
                className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
                disabled={tab.disabled}
                onClick={() => !tab.disabled && setActiveTab(tab.id)}
              >
                {tab.label}
                {tab.disabled && <span className="tab-soon">{t['common.soon']}</span>}
              </button>
            ))}
          </nav>
        </div>

        <div className="tab-content">
          {activeTab === 'claude-md' && <ErrorBoundary label="CLAUDE.md"><ClaudeMdTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'logs'      && <ErrorBoundary label="Logs"><LogsTab projectId={project.id} projectName={project.name} /></ErrorBoundary>}
          {activeTab === 'board'     && <ErrorBoundary label="Board"><BoardTab projectId={project.id} isActive={isActive} /></ErrorBoundary>}
          {activeTab === 'files'     && <ErrorBoundary label="Files"><FilesTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'memory'    && <ErrorBoundary label="Memory"><MemoryTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'timeline'  && <ErrorBoundary label="Activity"><TimelineTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'settings'  && <ErrorBoundary label="Settings"><SettingsTab projectId={project.id} project={project} health={structHealth} refreshHealth={refreshHealth} /></ErrorBoundary>}
        </div>
      </div>

      {/* DIVIDER — draggable handle (hidden on narrow screens and when collapsed) */}
      {!narrow && (
        <div
          className={`project-split-divider${collapsed ? ' divider-collapsed' : ''}`}
          onMouseDown={onDividerMouseDown}
          title={collapsed ? t['split.expand_chat'] : t['split.drag_to_resize']}
          onClick={collapsed ? toggleCollapse : undefined}
        />
      )}

      {/* RIGHT: permanent chat panel */}
      <div className="project-chat-pane" style={chatStyle}>
        <div className="project-chat-pane-header">
          <span>💬 Project chat</span>
          {!narrow && (
            <button
              className="chat-collapse-btn"
              onClick={toggleCollapse}
              title={collapsed ? t['split.expand_chat'] : t['split.collapse_chat']}
              aria-label={collapsed ? t['split.expand_chat'] : t['split.collapse_chat']}
              aria-expanded={!collapsed}
            >
              {collapsed ? '⟨' : '⟩'}
            </button>
          )}
        </div>
        <ErrorBoundary label="Chat">
          <ChatTab project={project} onProjectsReload={onProjectsReload} isActive={isActive} />
        </ErrorBoundary>
      </div>

      {/* COLLAPSED STUB — narrow vertical button when chat is collapsed */}
      {!narrow && collapsed && (
        <button
          className="chat-collapsed-stub"
          onClick={toggleCollapse}
          title={t['split.expand_chat']}
        >
          💬
        </button>
      )}
    </div>
    </ProjectActivityProvider>
  )
}


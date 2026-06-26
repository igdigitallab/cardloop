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
import { SpecsTab } from '../tabs/SpecsTab'
import { t } from '../i18n'

interface Tab {
  id: TabId
  label: string
  disabled?: boolean
}

// secrets tab removed (merged into Settings); overview tab removed (merged into Settings); 8 tabs remain
const TABS: Tab[] = [
  { id: 'claude-md', label: t['tab.claude_md'] },
  { id: 'logs',      label: t['tab.logs'] },
  { id: 'board',     label: t['tab.board'] },
  { id: 'files',     label: t['tab.files'] },
  { id: 'memory',    label: t['tab.memory'] },
  { id: 'timeline',  label: t['tab.timeline'] },
  { id: 'settings',  label: t['tab.settings'] },
  { id: 'specs',     label: t['tab.specs'] },
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
  /** H3: All open project IDs in tab order (for swipe navigation) */
  openProjectIds?: string[]
  /** H3: Called when swipe switches to a different project */
  onSwipeToProject?: (id: string) => void
  /** Sidebar "⚙ Settings" request: when {id} matches this project, switch to the Settings tab.
   *  nonce changes on every request so repeats re-fire even for the already-open project. */
  settingsRequest?: { id: string; nonce: number } | null
  /** Live model registry from /api/models; undefined → ChatTab uses the static fallback. */
  models?: { value: string; label: string }[]
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

export function ProjectView({ project, onProjectsReload, onRenameSuccess, onSplitCreate, onSplitClose, isActive, openProjectIds, onSwipeToProject, settingsRequest, models }: Props) {
  const [activeTab, setActiveTab] = useState<TabId>('board')
  // Mobile inner tab: null = show chat (default), TabId = show that inner tab
  const [mobileInnerTab, setMobileInnerTab] = useState<TabId | null>(null)
  // spec-052 Phase 4a: a card the user chose to "Discuss" on the board → handed to the chat.
  const [discussCard, setDiscussCard] = useState<{ cardId: string; title: string } | null>(null)
  const git = project.health.git

  // Sidebar "⚙ Settings" request → switch to the Settings tab when it targets this project.
  // Keyed on nonce so a repeat request for the already-open project re-fires.
  useEffect(() => {
    if (settingsRequest && settingsRequest.id === project.id) {
      setActiveTab('settings')
      setMobileInnerTab('settings')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsRequest?.nonce])

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

  // Detect narrow viewport — disable resize at/below 768px (matches CSS breakpoint)
  const [narrow, setNarrow] = useState(() => window.innerWidth <= 768)
  useEffect(() => {
    function onResize() { setNarrow(window.innerWidth <= 768) }
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

  // ── H3: Swipe gesture to switch between open project chats ──────────────────
  // Hooks MUST be declared before any early return (Rules of Hooks).
  // SWIPE DIRECTION MAPPING (flip the +1/-1 to reverse):
  // Swipe RIGHT (finger moves right) → NEXT project tab (index + 1)
  // Swipe LEFT  (finger moves left)  → PREVIOUS project tab (index - 1)
  const SWIPE_H_THRESHOLD = 60   // minimum horizontal distance to trigger switch
  const SWIPE_V_MAX       = 30   // maximum vertical drift (to not conflict with scroll)

  const swipeStart = useRef<{ x: number; y: number; scroller: HTMLElement | null } | null>(null)
  const [swipeAnim, setSwipeAnim] = useState<'left' | 'right' | null>(null)
  const contentRef = useRef<HTMLDivElement>(null)

  // Row2 (section tabs) auto-collapse while reading: hidden when the chat feed scrolls
  // DOWN, revealed on scroll up / near top.
  const [navCollapsed, setNavCollapsed] = useState(false)
  const lastFeedScroll = useRef(0)
  const scrollAccum = useRef(0)
  const collapsedRef = useRef(false)      // mirrors navCollapsed for the scroll handler
  const collapseLockUntil = useRef(0)     // ignore scroll churn until this timestamp (ms)

  // Nearest ancestor that can ACTUALLY scroll horizontally — real overflow (not 1px
  // sub-pixel rounding) AND overflow-x is auto/scroll. The old check returned true for
  // any element a hair wider than its box (code blocks, tables, even rounding), which
  // silently disabled the swipe depending on WHERE the finger landed — the root cause
  // of "swipe doesn't switch on the first try". Now we only defer to an element that
  // genuinely scrolls, and only while it still has room to scroll (see handleTouchEnd).
  function hScrollableAncestor(el: EventTarget | null): HTMLElement | null {
    let node = el as HTMLElement | null
    while (node && node !== contentRef.current) {
      if (node.scrollWidth - node.clientWidth > 8) {
        const ox = getComputedStyle(node).overflowX
        if (ox === 'auto' || ox === 'scroll') return node
      }
      node = node.parentElement
    }
    return null
  }

  function handleTouchStart(e: React.TouchEvent) {
    if (mobileInnerTab !== null) return  // only on Chat tab
    const touch = e.touches[0]
    swipeStart.current = {
      x: touch.clientX,
      y: touch.clientY,
      scroller: hScrollableAncestor(e.target),
    }
  }

  function handleTouchMove(e: React.TouchEvent) {
    if (!swipeStart.current) return
    if (mobileInnerTab !== null) return
    const touch = e.touches[0]
    const dy = Math.abs(touch.clientY - swipeStart.current.y)
    // Vertical drift dominates → it's a scroll, not a horizontal swipe: cancel.
    if (dy > SWIPE_V_MAX) {
      swipeStart.current = null
    }
  }

  function handleTouchEnd(e: React.TouchEvent) {
    if (!swipeStart.current) return
    if (mobileInnerTab !== null) return
    const touch = e.changedTouches[0]
    const dx = touch.clientX - swipeStart.current.x
    const dy = Math.abs(touch.clientY - swipeStart.current.y)
    const scroller = swipeStart.current.scroller
    swipeStart.current = null

    if (Math.abs(dx) < SWIPE_H_THRESHOLD || dy > SWIPE_V_MAX) return
    // If the swipe began inside a genuinely h-scrollable element that still has room to
    // scroll in the swipe direction, let it consume the gesture instead of switching.
    if (scroller) {
      const atLeftEdge = scroller.scrollLeft <= 0
      const atRightEdge = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 1
      if (dx > 0 && !atLeftEdge) return   // swipe right → element still scrolls left
      if (dx < 0 && !atRightEdge) return  // swipe left  → element still scrolls right
    }
    if (!openProjectIds || !onSwipeToProject) return

    const currentIdx = openProjectIds.indexOf(project.id)
    if (currentIdx === -1) return

    // Swipe RIGHT → next (index + 1); Swipe LEFT → previous (index - 1)
    const delta = dx > 0 ? +1 : -1  // swipe right=+1, left=-1
    const nextIdx = currentIdx + delta
    if (nextIdx < 0 || nextIdx >= openProjectIds.length) return  // edge: no wrap

    const dir = dx > 0 ? 'right' : 'left'
    setSwipeAnim(dir)
    setTimeout(() => {
      setSwipeAnim(null)
      onSwipeToProject(openProjectIds[nextIdx])
    }, 180)  // matches CSS transition duration
  }

  // Collapse the top/section chrome while the chat feed scrolls DOWN, reveal on scroll
  // up / near top. `scroll` does not bubble → capture-phase listener keyed on .chat-feed.
  // Two guards kill the jitter: (1) direction is accumulated, only flipping past a 36px
  // deadzone; (2) after a flip we IGNORE scroll for ~320ms — collapsing grows the feed and
  // the browser clamps scrollTop near the bottom, which would otherwise read as a reverse
  // scroll and oscillate the state every frame during the height transition.
  function applyCollapse(next: boolean) {
    if (collapsedRef.current === next) return
    collapsedRef.current = next
    setNavCollapsed(next)
    collapseLockUntil.current = performance.now() + 320
  }
  useEffect(() => {
    if (!narrow) return
    function onScroll(e: Event) {
      const el = e.target as HTMLElement | null
      if (!el || !el.classList?.contains('chat-feed')) return
      const st = el.scrollTop
      // Absorb the reflow churn that our own collapse/expand triggers.
      if (performance.now() < collapseLockUntil.current) {
        lastFeedScroll.current = st
        scrollAccum.current = 0
        return
      }
      const dy = st - lastFeedScroll.current
      lastFeedScroll.current = st
      if (st < 48) { scrollAccum.current = 0; applyCollapse(false); return }  // near top → show
      if (dy === 0) return
      // Reset the accumulator whenever the scroll direction reverses.
      if ((dy > 0) !== (scrollAccum.current >= 0)) scrollAccum.current = 0
      scrollAccum.current += dy
      if (scrollAccum.current > 36) { scrollAccum.current = 0; applyCollapse(true) }
      else if (scrollAccum.current < -36) { scrollAccum.current = 0; applyCollapse(false) }
    }
    document.addEventListener('scroll', onScroll, true)
    return () => document.removeEventListener('scroll', onScroll, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [narrow])

  // Always reveal the chrome when switching section or project.
  useEffect(() => {
    collapsedRef.current = false
    collapseLockUntil.current = 0
    scrollAccum.current = 0
    setNavCollapsed(false)
  }, [mobileInnerTab, project.id])

  // Free chat — no left tab panel, chat at full width.
  if (project.is_free) {
    return (
      <ProjectActivityProvider projectId={project.id} active={isActive}>
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
            <ChatTab project={project} onProjectsReload={onProjectsReload} isActive={isActive} models={models} />
          </ErrorBoundary>
        </div>
      </ProjectActivityProvider>
    )
  }

  // ── Mobile narrow branch (≤768px, non-free projects only) ──────────────────
  if (narrow && !project.is_free) {
    return (
      <ProjectActivityProvider projectId={project.id} active={isActive}>
        <div className="main-content mobile-project-layout">
          {/* Inner tab strip — Row 2 (Row 1 = ProjectTabBar with back btn + usage badge) */}
          {/* Auto-collapse the section tabs ONLY on the Chat tab — chat has a scroll-up-to-reveal
              affordance (the .chat-feed scroll handler). Other tabs (Board/CLAUDE.md/Logs/…) scroll
              their own container with no reveal path, so a collapsed nav would strand the user with
              no way back to Chat. Always keep the nav expanded off the Chat tab. */}
          <nav className={`mobile-inner-tabs${navCollapsed && mobileInnerTab === null ? ' collapsed' : ''}`} aria-label={t['tab.sections_aria']}>
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
          {/* Content area: chat or inner tab — swipe handler on Chat tab only */}
          <div
            className={`mobile-project-content${swipeAnim ? ` swipe-anim-${swipeAnim}` : ''}`}
            ref={contentRef}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
            onTouchEnd={handleTouchEnd}
          >
            {mobileInnerTab === null ? (
              <ErrorBoundary label="Chat">
                <ChatTab project={project} onProjectsReload={onProjectsReload} isActive={isActive} chromeCollapsed={navCollapsed} onOpenCard={() => setMobileInnerTab('board')} discussCard={discussCard} onDiscussConsumed={() => setDiscussCard(null)} models={models} />
              </ErrorBoundary>
            ) : (
              <div className="tab-content">
                {mobileInnerTab === 'claude-md' && <ErrorBoundary label="CLAUDE.md"><ClaudeMdTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'logs'      && <ErrorBoundary label="Logs"><LogsTab projectId={project.id} projectName={project.name} /></ErrorBoundary>}
                {mobileInnerTab === 'board'     && <ErrorBoundary label="Board"><BoardTab projectId={project.id} isActive={isActive} onDiscuss={(c) => { setDiscussCard(c); setMobileInnerTab(null) }} /></ErrorBoundary>}
                {mobileInnerTab === 'files'     && <ErrorBoundary label="Files"><FilesTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'memory'    && <ErrorBoundary label="Memory"><MemoryTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'timeline'  && <ErrorBoundary label="Activity"><TimelineTab projectId={project.id} /></ErrorBoundary>}
                {mobileInnerTab === 'settings'  && <ErrorBoundary label="Settings"><SettingsTab projectId={project.id} project={project} health={structHealth} refreshHealth={refreshHealth} models={models} /></ErrorBoundary>}
                {mobileInnerTab === 'specs'     && <ErrorBoundary label="Specs"><SpecsTab projectId={project.id} /></ErrorBoundary>}
              </div>
            )}
          </div>
          <HealthRunEndRefresher refresh={refreshHealth} />
        </div>
      </ProjectActivityProvider>
    )
  }

  return (
    <ProjectActivityProvider projectId={project.id} active={isActive}>
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
          {activeTab === 'board'     && <ErrorBoundary label="Board"><BoardTab projectId={project.id} isActive={isActive} onDiscuss={setDiscussCard} /></ErrorBoundary>}
          {activeTab === 'files'     && <ErrorBoundary label="Files"><FilesTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'memory'    && <ErrorBoundary label="Memory"><MemoryTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'timeline'  && <ErrorBoundary label="Activity"><TimelineTab projectId={project.id} /></ErrorBoundary>}
          {activeTab === 'settings'  && <ErrorBoundary label="Settings"><SettingsTab projectId={project.id} project={project} health={structHealth} refreshHealth={refreshHealth} models={models} /></ErrorBoundary>}
          {activeTab === 'specs'     && <ErrorBoundary label="Specs"><SpecsTab projectId={project.id} /></ErrorBoundary>}
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

      {/* RIGHT: permanent chat panel — collapse button is now inside ChatTab's merged toolbar */}
      <div className="project-chat-pane" style={chatStyle}>
        <ErrorBoundary label="Chat">
          <ChatTab
            project={project}
            onProjectsReload={onProjectsReload}
            isActive={isActive}
            collapsed={collapsed}
            onToggleCollapse={toggleCollapse}
            onOpenCard={() => setActiveTab('board')}
            discussCard={discussCard}
            onDiscussConsumed={() => setDiscussCard(null)}
            models={models}
          />
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


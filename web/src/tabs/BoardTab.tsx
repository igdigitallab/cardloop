import { memo, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { ActivityEvent, Board, BoardColumn, GateResult, RichTool, RunResult, TaskCard, isIncidentCard } from '../types'
import { Spinner } from '../components/Spinner'
import { Modal, ModalHead } from '../components/Modal'
import { useOnRunEnd, useFocusRefresh, useProjectActivity } from '../hooks/useProjectActivity'
import { t } from '../i18n'
import { MODELS, modelLabel } from '../lib/models'

// ─── Live card run state ──────────────────────────────────────────────────────

interface CardRunState {
  cardId: string
  startedAt: number
  lastEventAt: number
  currentTool: RichTool | null
}

/** Formats M:SS duration. */
function fmtDuration(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${r.toString().padStart(2, '0')}`
}

// ─── CardLiveStrip — isolated ticker so board card list does NOT re-render/sec ─

interface CardLiveStripProps {
  run: CardRunState
}

/**
 * Compact live strip rendered inside a running board card.
 * Owns its own 1-second tick so re-renders are isolated to this element —
 * the parent card list does not re-render every second (same pattern as
 * ChatTab's RunStatusBar).
 */
const CardLiveStrip = memo(function CardLiveStrip({ run }: CardLiveStripProps) {
  const [tick, setTick] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const elapsedSec = (tick - run.startedAt) / 1000
  const silenceSec = (tick - run.lastEventAt) / 1000
  const lvl = silenceSec > 120 ? 'silence-red' : silenceSec > 30 ? 'silence-yellow' : 'silence-ok'

  const tool = run.currentTool
  let icon = '💭'
  let label: string
  if (tool) {
    icon = '🔧'
    const hint = toolHintBoard(tool)
    label = hint ? `${tool.name} · ${hint}` : tool.name
  } else {
    label = t['chat.status_card_running']
  }

  return (
    <div className={`card-live-strip ${lvl}`}>
      <span className="card-live-icon">{icon}</span>
      <span className="card-live-label">{label}</span>
      <span className="card-live-elapsed">· {fmtDuration(elapsedSec)}</span>
      {silenceSec > 30 && (
        <span className="card-live-silence">
          ⚠ {t['board.card_live_silence_warn']} {fmtDuration(silenceSec)}
          {silenceSec > 120 && ` · ${t['board.card_live_hung']}`}
        </span>
      )}
    </div>
  )
})

/** Short file/cmd hint for a tool — same logic as ChatTab's toolHint. */
function toolHintBoard(tool: RichTool): string {
  if (tool.kind === 'bash') {
    const cmd = tool.cmd.trim().split('\n')[0]
    return cmd.length > 45 ? cmd.slice(0, 45) + '…' : cmd
  }
  if (tool.kind === 'edit' || tool.kind === 'write' || tool.kind === 'read') {
    return tool.file.split('/').pop() || tool.file
  }
  if (tool.kind === 'search') {
    return tool.pattern.length > 35 ? tool.pattern.slice(0, 35) + '…' : tool.pattern
  }
  return ''
}

// ─── BoardDashboard — compact summary strip above the columns ─────────────────

interface BoardDashboardProps {
  board: Board
  run: CardRunState | null
}

/**
 * Compact project dashboard: column counts + live run indicator.
 * The live elapsed timer is isolated in a child memo component.
 */
const BoardDashboard = memo(function BoardDashboard({ board, run }: BoardDashboardProps) {
  const backlogCount = board.columns.find(c => c.key === 'backlog')?.cards.length ?? 0
  const reviewCount = board.columns.find(c => c.key === 'review')?.cards.length ?? 0
  const failedCount = board.columns.find(c => c.key === 'failed')?.cards.length ?? 0

  // Find the card text of the currently running card
  let runCardText: string | null = null
  if (run) {
    for (const col of board.columns) {
      const found = col.cards.find(c => c.id === run.cardId)
      if (found) { runCardText = found.text; break }
    }
  }

  return (
    <div className="board-dashboard">
      <span className="board-dashboard-counts">
        {backlogCount > 0 && (
          <span className="board-dashboard-pill">{t['board.dashboard_backlog']} <strong>{backlogCount}</strong></span>
        )}
        {reviewCount > 0 && (
          <span className="board-dashboard-pill board-dashboard-pill-review">{t['board.dashboard_review']} <strong>{reviewCount}</strong></span>
        )}
        {failedCount > 0 && (
          <span className="board-dashboard-pill board-dashboard-pill-failed">{t['board.dashboard_failed']} <strong>{failedCount}</strong></span>
        )}
      </span>
      {run && (
        <BoardDashboardRun run={run} cardText={runCardText} />
      )}
    </div>
  )
})

interface BoardDashboardRunProps {
  run: CardRunState
  cardText: string | null
}

/** Isolated ticker for the running card summary in the dashboard. */
const BoardDashboardRun = memo(function BoardDashboardRun({ run, cardText }: BoardDashboardRunProps) {
  const [tick, setTick] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const elapsedSec = (tick - run.startedAt) / 1000
  const label = cardText
    ? (cardText.length > 60 ? cardText.slice(0, 60) + '…' : cardText)
    : run.cardId

  return (
    <span className="board-dashboard-running">
      <span className="board-dashboard-running-icon">⚙</span>
      <span className="board-dashboard-running-label">{t['board.dashboard_running']}: {label}</span>
      <span className="board-dashboard-running-elapsed">· {fmtDuration(elapsedSec)}</span>
    </span>
  )
})

interface Props {
  projectId: string
  /** When false (project tab hidden via display:none), suspend polling to avoid wasted fetches. */
  isActive?: boolean
}

// Columns shown in the board column row.
// in_progress is intentionally excluded — it is never shown as a column or offered in toggles.
// Failed is also excluded — it lives in the collapsible tray above.
const ORDER = ['backlog', 'review']
// Arrows ←/→ move through "parking" columns, SKIPPING in_progress: the only
// way to run the agent is the 🤖 button (previously → from Backlog duplicated the robot).
const PARK_ORDER = ['backlog', 'review', 'failed']
const POLL_FAST_MS = 3000   // when there are cards in In Progress (agent is running)
const POLL_SLOW_MS = 10000  // background poll (TASKS.md edits from agent via chat, external edits)

const LS_BOARD_COLS = 'cops.boardVisibleCols'
const LS_FAILED_COLLAPSED = 'cops.board.failedCollapsed'
const DEFAULT_VISIBLE: string[] = ['backlog']  // default — Backlog only

function readVisibleCols(): Set<string> {
  try {
    const raw = localStorage.getItem(LS_BOARD_COLS)
    if (!raw) return new Set(DEFAULT_VISIBLE)
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr) || arr.length === 0) return new Set(DEFAULT_VISIBLE)
    return new Set(arr.filter((x): x is string => typeof x === 'string'))
  } catch {
    return new Set(DEFAULT_VISIBLE)
  }
}

function writeVisibleCols(s: Set<string>) {
  try { localStorage.setItem(LS_BOARD_COLS, JSON.stringify([...s])) } catch {}
}

function readFailedCollapsed(): boolean {
  try {
    const raw = localStorage.getItem(LS_FAILED_COLLAPSED)
    if (raw === null) return true  // default: collapsed
    return raw === 'true'
  } catch {
    return true
  }
}

function writeFailedCollapsed(v: boolean) {
  try { localStorage.setItem(LS_FAILED_COLLAPSED, String(v)) } catch {}
}

export function BoardTab({ projectId, isActive = true }: Props) {
  const [board, setBoard] = useState<Board | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [newText, setNewText] = useState('')
  const [showArchive, setShowArchive] = useState(false)
  const [archive, setArchive] = useState<string | null>(null)
  // Full-task editor modal: double-click on any card → single multi-line textarea + model picker
  const [taskEditModal, setTaskEditModal] = useState<{ id: string; text: string; model: string } | null>(null)

  // Card 5e1c0a: spec modal state
  const [specModal, setSpecModal] = useState<{ cardId: string; content: string; loading: boolean; saving: boolean } | null>(null)

  // Drag-and-drop
  const [dragCardId, setDragCardId] = useState<string | null>(null)
  const [dragOverCol, setDragOverCol] = useState<string | null>(null)

  // F1: card result modal state
  const [runResult, setRunResult] = useState<RunResult | null>(null)
  const [runResultLoading, setRunResultLoading] = useState(false)
  const [showRunModal, setShowRunModal] = useState(false)

  // C2-gate: apply/discard
  const [gateError, setGateError] = useState('')
  const [gateBusy, setGateBusy] = useState(false)
  // Confirmation modal for discard (irreversible)
  const [confirmDiscard, setConfirmDiscard] = useState<{ cardId: string } | null>(null)
  // Toast for gate messages
  const [gateToast, setGateToast] = useState<string>('')

  // Card defer-after-reset: track busy per card id
  const [cardDeferBusy, setCardDeferBusy] = useState<Set<string>>(new Set())
  // Card defer-after-reset: map card_id -> pending deferred record id (for the stateful toggle).
  // Populated from GET /api/deferred?status=pending, filtered to cards on THIS board.
  const [deferMap, setDeferMap] = useState<Record<string, string>>({})

  // Spec 009: quality gate — test check result before applying
  const [gateResult, setGateResult] = useState<GateResult | null>(null)
  const [gateChecking, setGateChecking] = useState(false)
  const [gateOutputOpen, setGateOutputOpen] = useState(false)

  // Visible columns (persisted in localStorage). Default — Backlog only.
  const [visibleCols, setVisibleCols] = useState<Set<string>>(() => readVisibleCols())

  // Failed tray collapse state (persisted in localStorage). Default — collapsed.
  const [failedCollapsed, setFailedCollapsed] = useState<boolean>(() => readFailedCollapsed())

  // Auto-reconcile settings popover (Task A).
  const [showReconcilePopover, setShowReconcilePopover] = useState(false)
  const [reconcileEnabled, setReconcileEnabled] = useState<boolean>(true)
  const [reconcileOnMatch, setReconcileOnMatch] = useState<'done' | 'review'>('done')
  const [reconcileLoading, setReconcileLoading] = useState(false)
  const reconcilePopoverRef = useRef<HTMLDivElement>(null)

  // Load reconcile settings on mount.
  useEffect(() => {
    api.settings().then(s => {
      const eff = s.effective
      if (typeof eff.board_reconcile_enabled === 'boolean') {
        setReconcileEnabled(eff.board_reconcile_enabled)
      }
      if (eff.board_reconcile_on_match === 'review' || eff.board_reconcile_on_match === 'done') {
        setReconcileOnMatch(eff.board_reconcile_on_match)
      }
    }).catch(() => {})
  }, [])

  // Close reconcile popover on outside click.
  useEffect(() => {
    if (!showReconcilePopover) return
    function handler(e: MouseEvent) {
      if (reconcilePopoverRef.current && !reconcilePopoverRef.current.contains(e.target as Node)) {
        setShowReconcilePopover(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showReconcilePopover])

  async function saveReconcileSetting(key: string, value: boolean | string) {
    setReconcileLoading(true)
    try {
      await api.saveSettings({ [key]: value })
    } catch { /* silently ignore — setting is best-effort */ }
    finally { setReconcileLoading(false) }
  }

  function toggleFailedCollapsed() {
    setFailedCollapsed(prev => {
      const next = !prev
      writeFailedCollapsed(next)
      return next
    })
  }

  // spec-036 Phase 2a: live run state — which card is currently being executed
  const [liveRun, setLiveRun] = useState<CardRunState | null>(null)

  // Subscribe to the activity bus and track which card is running
  useProjectActivity((evt: ActivityEvent) => {
    if (evt.kind === 'run_start') {
      setLiveRun({
        cardId: evt.run_id,
        startedAt: Date.now(),
        lastEventAt: Date.now(),
        currentTool: null,
      })
    } else if (evt.kind === 'tool') {
      setLiveRun(prev =>
        prev && prev.cardId === evt.run_id
          ? { ...prev, lastEventAt: Date.now(), currentTool: evt.tool }
          : prev
      )
    } else if (evt.kind === 'text') {
      setLiveRun(prev =>
        prev && prev.cardId === evt.run_id
          ? { ...prev, lastEventAt: Date.now(), currentTool: null }
          : prev
      )
    } else if (evt.kind === 'run_end') {
      // Clear live run only when the matching card finishes
      setLiveRun(prev => (prev && prev.cardId === evt.run_id ? null : prev))
    }
  })

  // Multi-select cards for batch sending to agent (sequential queue)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  function toggleSelect(cardId: string) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(cardId)) next.delete(cardId)
      else next.add(cardId)
      return next
    })
  }
  async function sendSelectedToAgent() {
    const ids = [...selected]
    if (!ids.length || busy) return
    setBusy(true); setError('')
    try {
      const r = await api.runBatch(projectId, ids)
      setSelected(new Set())
      const fresh = await api.tasks(projectId)
      setBoard(fresh)
      setGateToast(`🤖 Queued: ${r.queued}${r.started ? ` · started ${r.started}` : ' · waiting for project to free up'}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function toggleCol(key: string) {
    setVisibleCols(prev => {
      const next = new Set(prev)
      if (next.has(key)) {
        if (next.size <= 1) return prev  // cannot hide the last column
        next.delete(key)
      } else {
        next.add(key)
      }
      writeVisibleCols(next)
      return next
    })
  }

  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId

  // F1: are there cards in In Progress — speed up polling
  function hasInProgress(b: Board | null): boolean {
    if (!b) return false
    const col = b.columns.find(c => c.key === 'in_progress')
    return (col?.cards.length ?? 0) > 0
  }

  // Keep isActive in a ref so the polling closure always sees the current value
  const isActiveRef = useRef(isActive)
  isActiveRef.current = isActive

  // Polling: 3s while in_progress, 10s at rest; does not tick when tab is hidden or ProjectView inactive
  function schedulePoll(b: Board | null) {
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    const delay = hasInProgress(b) ? POLL_FAST_MS : POLL_SLOW_MS
    pollTimerRef.current = setTimeout(async () => {
      // Skip poll if project tab is hidden (display:none) or browser tab invisible
      if (!isActiveRef.current || document.visibilityState !== 'visible') {
        schedulePoll(b)  // wait for the next tick
        return
      }
      try {
        const fresh = await api.tasks(projectIdRef.current)
        setBoard(fresh)
        schedulePoll(fresh)
        refreshDeferMap()
      } catch {
        schedulePoll(b)
      }
    }, delay)
  }

  // Instant refresh (focus, visibility, run_end from bus)
  async function refreshNow() {
    try {
      const fresh = await api.tasks(projectIdRef.current)
      setBoard(fresh)
      schedulePoll(fresh)
      refreshDeferMap()
    } catch { /* silently ignore — next poll tick will retry */ }
  }

  // Keep the latest board in a ref so the deferred-map refresher can filter by
  // the current board's card ids without re-creating the callback every render.
  const boardRef = useRef<Board | null>(null)
  boardRef.current = board

  // Collect all card ids currently on the board (across every column).
  function collectCardIds(b: Board | null): Set<string> {
    const ids = new Set<string>()
    if (!b) return ids
    for (const col of b.columns) for (const c of col.cards) ids.add(c.id)
    return ids
  }

  // Refresh the card_id -> deferred-record-id map for pending "after reset" runs
  // belonging to cards on THIS board. Called on load/poll and after queue/cancel.
  async function refreshDeferMap() {
    try {
      const recs = await api.deferredList('?status=pending')
      const boardIds = collectCardIds(boardRef.current)
      const next: Record<string, string> = {}
      for (const r of recs as Array<{ id?: string; card_id?: string }>) {
        const cid = r.card_id
        const rid = r.id
        if (cid && rid && boardIds.has(cid)) next[cid] = rid
      }
      setDeferMap(next)
    } catch { /* silently ignore — next poll tick will retry */ }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setBoard(null)
    setShowArchive(false); setArchive(null)
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)

    setDeferMap({})  // reset stale map when switching projects
    api.tasks(projectId).then(b => {
      if (!cancelled) {
        setBoard(b)
        setLoading(false)
        schedulePoll(b)
        refreshDeferMap()
      }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })
    return () => {
      cancelled = true
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- schedulePoll uses refs only, stable
  }, [projectId])

  // Refresh on focus/visibility (shared hook)
  useFocusRefresh(refreshNow)
  // Refresh on run_end from the project bus — agent may have changed TASKS.md, card may have moved to Review/Failed
  useOnRunEnd(refreshNow)

  async function run(p: Promise<Board>) {
    setBusy(true); setError('')
    try {
      const b = await p
      setBoard(b)
      schedulePoll(b)
    } catch (e) {
      // F1: 409 = project is busy
      const status = (e as { status?: number })?.status
      if (status === 409) {
        setError('⏳ Project is busy (TG or another card) — try again later')
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setBusy(false)
    }
  }

  function addCard() {
    const raw = newText.trim()
    if (!raw) return
    setNewText('')
    // Multi-line input: first line = title, remaining lines = description.
    // Single-line input (no matter the length): send as-is — no character cap.
    // The backend stores the full text; long single-line tasks are NOT truncated.
    let title = raw
    let description: string | null = null
    const nlIdx = raw.indexOf('\n')
    if (nlIdx !== -1) {
      title = raw.slice(0, nlIdx).trim()
      description = raw.slice(nlIdx + 1).trim() || null
    }
    run(api.createTask(projectId, title, 'backlog', description))
  }

  function move(card: string, to: string) { run(api.moveTask(projectId, card, to)) }
  function del(card: string) { run(api.deleteTask(projectId, card)) }

  /** Save the task text + model override from the full-task editor modal and close it. */
  async function saveTaskEdit() {
    if (!taskEditModal) return
    const { id, text, model } = taskEditModal
    setTaskEditModal(null)
    const trimmed = text.trim()
    if (!trimmed) return
    // Pass model: '' means "clear override"; a non-empty value sets the override.
    run(api.updateTask(projectId, id, trimmed, undefined, model))
  }

  function toggleArchive() {
    if (showArchive) { setShowArchive(false); return }
    setShowArchive(true)
    if (archive === null) {
      api.tasksDone(projectId)
        .then(d => setArchive(d.content || '*Archive is empty*'))
        .catch(e => setArchive(`⚠ ${e.message || e}`))
    }
  }

  // F1: show card run result
  async function showResult(cardId: string) {
    setRunResultLoading(true)
    setShowRunModal(true)
    setRunResult(null)
    setGateError('')
    setGateResult(null)
    setGateOutputOpen(false)
    try {
      const r = await api.cardRun(projectId, cardId)
      setRunResult(r)
      // meta is stored in runResult.meta — gate buttons read from there
    } catch (e) {
      setRunResult({ content: `⚠ Load error: ${e instanceof Error ? e.message : String(e)}`, exists: false })
    } finally {
      setRunResultLoading(false)
    }
  }

  // Card 5e1c0a: open spec modal — fetch current content, then show editor
  async function openSpec(cardId: string) {
    setSpecModal({ cardId, content: '', loading: true, saving: false })
    try {
      const r = await api.getCardSpec(projectId, cardId)
      setSpecModal(prev => prev ? { ...prev, content: r.content, loading: false } : null)
    } catch (e) {
      setSpecModal(prev => prev ? { ...prev, content: `⚠ Load error: ${e instanceof Error ? e.message : String(e)}`, loading: false } : null)
    }
  }

  // Card 5e1c0a: save spec and refresh board (so has_spec indicator updates)
  async function saveSpec() {
    if (!specModal) return
    const { cardId, content } = specModal
    setSpecModal(prev => prev ? { ...prev, saving: true } : null)
    try {
      await api.putCardSpec(projectId, cardId, content)
      setSpecModal(null)
      // Refresh board so has_spec updates
      await refreshNow()
    } catch (e) {
      setSpecModal(prev => prev ? { ...prev, saving: false } : null)
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  // C2-gate: show toast and hide after 4 seconds
  function showToast(msg: string) {
    setGateToast(msg)
    setTimeout(() => setGateToast(''), 4000)
  }

  // C2-gate: apply changes (merge)
  async function applyCard(cardId: string) {
    setGateBusy(true)
    setGateError('')
    try {
      await api.applyCard(projectId, cardId)
      setShowRunModal(false)
      showToast(t['board.gate_applied_banner'])
      await refreshNow()
    } catch (e) {
      const status = (e as { status?: number })?.status
      const msg = e instanceof Error ? e.message : String(e)
      if (status === 409) {
        setGateError(t['board.gate_conflict'] + msg)
      } else {
        setGateError(msg)
      }
    } finally {
      setGateBusy(false)
    }
  }

  // C2-gate: discard changes
  async function discardCard(cardId: string) {
    setConfirmDiscard(null)
    setGateBusy(true)
    setGateError('')
    try {
      await api.discardCard(projectId, cardId)
      setShowRunModal(false)
      showToast(t['board.gate_discarded_banner'])
      await refreshNow()
    } catch (e) {
      setGateError(e instanceof Error ? e.message : String(e))
    } finally {
      setGateBusy(false)
    }
  }

  // Spec 009: quality gate — run tests in the card's worktree
  async function checkCard(cardId: string) {
    setGateChecking(true)
    setGateResult(null)
    setGateOutputOpen(false)
    setGateError('')
    try {
      const r = await api.checkCard(projectId, cardId)
      setGateResult(r)
    } catch (e) {
      setGateError(e instanceof Error ? e.message : String(e))
    } finally {
      setGateChecking(false)
    }
  }

  // Card ⏱ "after reset" toggle: queue when not queued, cancel when queued.
  // Idempotent — the per-card busy guard blocks a double-submit; once queued, a
  // second click cancels rather than creating a duplicate record.
  async function toggleCardDefer(card: TaskCard) {
    if (cardDeferBusy.has(card.id)) return
    const existingId = deferMap[card.id]
    setCardDeferBusy(prev => new Set(prev).add(card.id))
    try {
      if (existingId) {
        // Queued → cancel the pending deferred run.
        await api.deferredDelete(existingId)
        setDeferMap(prev => {
          const next = { ...prev }; delete next[card.id]; return next
        })
        showToast(t['board.card_defer_toast_cancelled'])
      } else {
        // Not queued → schedule (strict reset, fires only at the next boundary).
        if (!card.text.trim()) return
        const r = await api.deferredCreate({
          project: projectId,
          prompt: card.text,
          fire_on_reset: true,
          card_id: card.id,
        })
        setDeferMap(prev => ({ ...prev, [card.id]: r.id }))
        showToast(t['board.card_defer_toast_queued'])
      }
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e))
    } finally {
      setCardDeferBusy(prev => {
        const next = new Set(prev); next.delete(card.id); return next
      })
      // Reconcile with the server in case the record fired/cancelled elsewhere.
      refreshDeferMap()
    }
  }

  if (loading) return <Spinner label={t['board.loading']} />

  const cols = board?.columns ?? []
  const colByKey = (k: string): BoardColumn | undefined => cols.find(c => c.key === k)

  const visibleOrder = ORDER.filter(k => visibleCols.has(k))

  // Render a single card. Used by both the column loop and the failed tray.
  // Defined here (inside the component) so it closes over all state and handlers.
  function renderCard(
    card: TaskCard,
    { columnKey, parkIdx, isInProgress, canShowResult }: {
      columnKey: string
      parkIdx: number
      isInProgress: boolean
      canShowResult: boolean
    }
  ) {
    const isIncident = isIncidentCard(card)
    const isSel = selected.has(card.id)
    const isQueued = board?.queued?.includes(card.id) ?? false
    // spec-036: a card lights up (yellow border) when isInProgress (legacy column-based)
    // OR when the live activity bus identifies this card as the currently running one.
    const isLiveRunning = liveRun?.cardId === card.id
    const isRunning = isInProgress || isLiveRunning
    return (
      <div
        className={[
          'board-card',
          isRunning ? 'board-card-running' : '',
          dragCardId === card.id ? 'board-card-dragging' : '',
          isIncident ? 'board-card-incident' : '',
          isSel ? 'board-card-selected' : '',
          isQueued ? 'board-card-queued' : '',
        ].filter(Boolean).join(' ')}
        key={card.id}
        draggable={!isRunning}
        onDragStart={(e) => {
          setDragCardId(card.id)
          e.dataTransfer.effectAllowed = 'move'
          e.dataTransfer.setData('text/plain', card.id)
        }}
        onDragEnd={() => { setDragCardId(null); setDragOverCol(null) }}
      >
        {parkIdx >= 0 && (
          <input
            type="checkbox"
            className="board-card-check"
            checked={isSel}
            disabled={busy}
            title="Select for batch send to agent"
            onClick={e => e.stopPropagation()}
            onChange={() => toggleSelect(card.id)}
          />
        )}
        {isQueued && (
          <div className="board-card-selrow">
            <span className="board-card-queued-badge" title="Queued for agent run">⏳ queued</span>
          </div>
        )}
        <div
          className="board-card-text"
          onDoubleClick={() => !isRunning && setTaskEditModal({ id: card.id, text: card.text, model: card.model || '' })}
          title={card.text}
        >
          {isIncident && <span className="card-incident-icon" title={t['board.incident_title']}>⚠ </span>}
          {isRunning && <span className="card-running-icon" title={t['board.card_running_title']}>⚙ </span>}
          <span className="board-card-title">{card.text}</span>
          {card.model && (
            <span
              className="board-card-model-badge"
              title={t['board.card_model_badge_aria']}
              aria-label={t['board.card_model_badge_aria']}
            >{modelLabel(card.model)}</span>
          )}
          {/* Card 5e1c0a: persistent spec indicator — visible without hover */}
          {card.has_spec && (
            <span
              className="board-card-spec-dot"
              title={t['board.spec_indicator_aria']}
              aria-label={t['board.spec_indicator_aria']}
            >📋</span>
          )}
          {/* Defer-after-reset: visible badge when this card has a pending "after reset" run */}
          {deferMap[card.id] && (
            <span
              className="board-card-defer-badge"
              title={t['board.card_defer_title_queued']}
              aria-label={t['board.card_defer_aria_queued']}
            >{t['board.card_defer_badge']}</span>
          )}
        </div>
        {/* spec-036 Phase 2a: live activity strip — shown when this card is being executed */}
        {liveRun?.cardId === card.id && <CardLiveStrip run={liveRun} />}
        <div className="board-card-actions">
          {parkIdx >= 0 && (
            <>
              <button title={t['board.move_left']} aria-label={t['board.move_left_aria']} disabled={busy || parkIdx === 0}
                onClick={() => move(card.id, PARK_ORDER[parkIdx - 1])}>←</button>
              <button title={t['board.move_right']} aria-label={t['board.move_right_aria']} disabled={busy || parkIdx === PARK_ORDER.length - 1}
                onClick={() => move(card.id, PARK_ORDER[parkIdx + 1])}>→</button>
            </>
          )}
          {columnKey !== 'in_progress' && (
            <button
              title="🤖 Run by agent (→ In Progress)"
              aria-label={t['board.handoff_aria']}
              className="act-handoff"
              disabled={busy}
              onClick={() => move(card.id, 'in_progress')}
            >🤖</button>
          )}
          {/* Card 5e1c0a: hover-only spec action button */}
          <button
            title={t['board.spec_btn']}
            aria-label={t['board.spec_btn_aria']}
            className={`act-spec${card.has_spec ? ' has-spec' : ''}`}
            disabled={busy}
            onClick={() => openSpec(card.id)}
          >📋</button>
          {canShowResult && (
            <button
              title={t['board.show_result']}
              aria-label={t['board.show_result_aria']}
              className="act-result"
              disabled={busy}
              onClick={() => showResult(card.id)}
            >📄</button>
          )}
          {/* Defer-after-reset: stateful toggle. Not queued → schedule the card's
              prompt to run after the 5-hour window resets (strict reset boundary).
              Queued → active/highlighted; click cancels the pending run.
              Decoupled from the card state machine — the card stays where it is. */}
          {(() => {
            const queued = !!deferMap[card.id]
            return (
              <button
                title={queued ? t['board.card_defer_title_queued'] : t['board.card_defer_title']}
                aria-label={queued ? t['board.card_defer_aria_queued'] : t['board.card_defer_aria']}
                aria-pressed={queued}
                className={`act-defer${queued ? ' act-defer-active' : ''}`}
                disabled={busy || cardDeferBusy.has(card.id) || (!queued && !card.text.trim())}
                onClick={() => toggleCardDefer(card)}
              >⏱</button>
            )
          })()}
          <button title={t['board.archive']} aria-label={t['board.archive_aria']} className="act-done" disabled={busy}
            onClick={() => move(card.id, 'done')}>✓</button>
          <button title={t['board.delete']} aria-label={t['board.delete_aria']} className="act-del" disabled={busy}
            onClick={() => del(card.id)}>✕</button>
        </div>
      </div>
    )
  }

  return (
    <div className="board-wrap">
      {error && <div className="error-state" style={{ marginBottom: 10 }}>⚠ {error}</div>}

      {/* Failed tray — pinned at the TOP, only when the failed column has ≥1 card */}
      {(() => {
        const failedCol = colByKey('failed')
        if (!failedCol || failedCol.cards.length === 0) return null
        const trayLabel = failedCol.label || t['board.failed_tray_label']
        const isDragOver = dragOverCol === 'failed' && dragCardId !== null
        return (
          <div className="board-failed-tray">
            <button
              className={`board-failed-tray-header${failedCollapsed ? ' collapsed' : ''}`}
              aria-label={failedCollapsed ? t['board.failed_tray_expand'] : t['board.failed_tray_collapse']}
              onClick={toggleFailedCollapsed}
            >
              <span className="board-failed-tray-chevron">{failedCollapsed ? '▶' : '▼'}</span>
              <span className="board-failed-tray-title">🔴 {trayLabel} ({failedCol.cards.length})</span>
            </button>
            {!failedCollapsed && (
              <div
                className={`board-failed-tray-body${isDragOver ? ' board-col-drag-over' : ''}`}
                onDragOver={(e) => {
                  if (!dragCardId) return
                  e.preventDefault()
                  e.dataTransfer.dropEffect = 'move'
                  if (dragOverCol !== 'failed') setDragOverCol('failed')
                }}
                onDragLeave={(e) => {
                  if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOverCol(null)
                }}
                onDrop={(e) => {
                  e.preventDefault()
                  if (dragCardId) {
                    const fromCol = cols.find(c => c.cards.some(card => card.id === dragCardId))
                    if (fromCol?.key !== 'failed') move(dragCardId, 'failed')
                  }
                  setDragCardId(null)
                  setDragOverCol(null)
                }}
              >
                {failedCol.cards.map(card => {
                  const isSel = selected.has(card.id)
                  const isQueued = board?.queued?.includes(card.id) ?? false
                  return (
                    <div
                      key={card.id}
                      className={[
                        'board-failed-row',
                        isSel ? 'board-card-selected' : '',
                        isQueued ? 'board-card-queued' : '',
                      ].filter(Boolean).join(' ')}
                    >
                      <span className="board-failed-row-icon" title="Failed">🔴</span>
                      <span
                        className="board-failed-row-text"
                        title={card.text}
                      >{card.text}</span>
                      {deferMap[card.id] && (
                        <span
                          className="board-card-defer-badge"
                          title={t['board.card_defer_title_queued']}
                          aria-label={t['board.card_defer_aria_queued']}
                        >{t['board.card_defer_badge']}</span>
                      )}
                      <div className="board-failed-row-actions">
                        <button
                          title="🤖 Retry by agent (→ In Progress)"
                          aria-label="Retry card with agent"
                          className="act-handoff"
                          disabled={busy}
                          onClick={() => move(card.id, 'in_progress')}
                        >🤖</button>
                        <button
                          title="View result"
                          aria-label="View last run result"
                          className="act-result"
                          disabled={busy}
                          onClick={() => showResult(card.id)}
                        >📄</button>
                        <button
                          title="Move to Backlog"
                          aria-label="Move card to backlog"
                          disabled={busy}
                          onClick={() => move(card.id, 'backlog')}
                        >←</button>
                        {(() => {
                          const queued = !!deferMap[card.id]
                          return (
                            <button
                              title={queued ? t['board.card_defer_title_queued'] : t['board.card_defer_title']}
                              aria-label={queued ? t['board.card_defer_aria_queued'] : t['board.card_defer_aria']}
                              aria-pressed={queued}
                              className={`act-defer${queued ? ' act-defer-active' : ''}`}
                              disabled={busy || cardDeferBusy.has(card.id) || (!queued && !card.text.trim())}
                              onClick={() => toggleCardDefer(card)}
                            >⏱</button>
                          )
                        })()}
                        <button
                          title="Archive (mark done)"
                          aria-label="Archive card"
                          className="act-done"
                          disabled={busy}
                          onClick={() => move(card.id, 'done')}
                        >✓</button>
                        <button
                          title="Delete card"
                          aria-label="Delete card"
                          className="act-del"
                          disabled={busy}
                          onClick={() => del(card.id)}
                        >✕</button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      })()}

      {/* Column toggles — show/hide. If a hidden column has cards, highlight the counter. */}
      <div className="board-col-toggles">
        <span className="board-col-toggles-label">{t['board.columns_label']}</span>
        {ORDER.map(k => {
          const col = colByKey(k)
          const label = col?.label || k
          const count = col?.cards.length ?? 0
          const isOn = visibleCols.has(k)
          const hidden = !isOn && count > 0
          return (
            <button
              key={k}
              className={`board-col-toggle ${isOn ? 'on' : 'off'} ${hidden ? 'has-cards' : ''}`}
              onClick={() => toggleCol(k)}
              title={isOn ? `Hide "${label}"` : `Show "${label}"`}
            >
              {label}{count > 0 ? ` (${count})` : ''}
            </button>
          )
        })}

        {/* Right-side controls group: Archive + Reconcile gear */}
        <div className="board-col-toggles-right">
          {/* Task B: Archive (Done) button — moved up from footer */}
          <button className="board-archive-toggle board-archive-toggle-strip" onClick={toggleArchive}>
            {showArchive ? '▾' : '▸'} {t['board.archive_toggle']} · {board?.done_count ?? 0}
          </button>

          {/* Task A: Auto-reconcile gear button */}
          <div className="board-reconcile-wrap" ref={reconcilePopoverRef}>
            <button
              className={`board-col-toggle board-reconcile-gear ${reconcileEnabled ? 'on' : 'off'}`}
              onClick={() => setShowReconcilePopover(prev => !prev)}
              title={t['board.reconcile_gear_title']}
            >
              ⚙
            </button>
            {showReconcilePopover && (
              <div className="board-reconcile-popover">
                <div className="board-reconcile-row">
                  <label className="board-reconcile-label">{t['board.reconcile_enabled_label']}</label>
                  <input
                    type="checkbox"
                    checked={reconcileEnabled}
                    disabled={reconcileLoading}
                    onChange={e => {
                      const v = e.target.checked
                      setReconcileEnabled(v)
                      saveReconcileSetting('board_reconcile_enabled', v)
                    }}
                  />
                </div>
                {reconcileEnabled && (
                  <div className="board-reconcile-row">
                    <label className="board-reconcile-label">{t['board.reconcile_on_match_label']}</label>
                    <div className="board-reconcile-seg">
                      <button
                        className={`board-reconcile-seg-btn ${reconcileOnMatch === 'review' ? 'active' : ''}`}
                        disabled={reconcileLoading}
                        onClick={() => {
                          setReconcileOnMatch('review')
                          saveReconcileSetting('board_reconcile_on_match', 'review')
                        }}
                      >{t['board.reconcile_on_match_review']}</button>
                      <button
                        className={`board-reconcile-seg-btn ${reconcileOnMatch === 'done' ? 'active' : ''}`}
                        disabled={reconcileLoading}
                        onClick={() => {
                          setReconcileOnMatch('done')
                          saveReconcileSetting('board_reconcile_on_match', 'done')
                        }}
                      >{t['board.reconcile_on_match_done']}</button>
                    </div>
                  </div>
                )}
                <div className="board-reconcile-hint">{t['board.reconcile_hint']}</div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Archive panel — directly below the control strip for proximity to its button */}
      {showArchive && (
        <div className="board-archive">
          {archive === null
            ? <Spinner label={t['board.loading_archive']} />
            : <div className="markdown-wrap"><ReactMarkdown remarkPlugins={[remarkGfm]}>{archive}</ReactMarkdown></div>}
        </div>
      )}

      {/* Hint when TASKS.md not yet created — was in the footer, kept near top for visibility */}
      {!board?.exists && (
        <div className="board-hint board-hint-notexist">TASKS.md does not exist yet — will be created on the first task</div>
      )}

      {/* spec-036 Phase 2a: project dashboard summary */}
      {board && <BoardDashboard board={board} run={liveRun} />}

      <div className="board-columns">
        {visibleOrder.map(key => {
          const col = colByKey(key)
          if (!col) return null
          const parkIdx = PARK_ORDER.indexOf(key)   // -1 for in_progress → arrows hidden
          const isInProgress = key === 'in_progress'
          const canShowResult = key === 'review' || key === 'failed'
          return (
            <div className={`board-col board-col-${key}`} key={key}>
              <div className="board-col-head">
                <span className="board-col-label">{col.label}</span>
                <span className="board-col-count">{col.cards.length}</span>
                {/* F1: agent running indicator in column header */}
                {isInProgress && col.cards.length > 0 && (
                  <span className="board-col-running" title={t['board.agent_running']}>⚙</span>
                )}
              </div>

              <div
                className={`board-col-body${dragOverCol === key && dragCardId ? ' board-col-drag-over' : ''}`}
                onDragOver={(e) => {
                  if (!dragCardId) return
                  e.preventDefault()
                  e.dataTransfer.dropEffect = 'move'
                  if (dragOverCol !== key) setDragOverCol(key)
                }}
                onDragLeave={(e) => {
                  if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOverCol(null)
                }}
                onDrop={(e) => {
                  e.preventDefault()
                  if (dragCardId) {
                    const fromCol = cols.find(c => c.cards.some(card => card.id === dragCardId))
                    if (fromCol?.key !== key) move(dragCardId, key)
                  }
                  setDragCardId(null)
                  setDragOverCol(null)
                }}
              >
                {key === 'backlog' && (
                  <div className="board-add">
                    <textarea
                      placeholder={t['board.new_task_placeholder']}
                      value={newText}
                      onChange={e => setNewText(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); addCard() }
                      }}
                      rows={2}
                    />
                    <button className="btn-primary" disabled={busy || !newText.trim()}
                      onClick={addCard}>+ Add</button>
                  </div>
                )}

                {col.cards.map(card =>
                  renderCard(card, { columnKey: key, parkIdx, isInProgress, canShowResult })
                )}
              </div>
            </div>
          )
        })}
      </div>

      {selected.size > 0 && (
        <div className="board-batch-bar">
          <span className="board-batch-count">Selected: {selected.size}</span>
          <button className="btn-primary board-batch-send" disabled={busy} onClick={sendSelectedToAgent}>
            🤖 Send to agent ({selected.size}) — queue
          </button>
          <button className="board-batch-clear" disabled={busy} onClick={() => setSelected(new Set())}>Deselect all</button>
        </div>
      )}

      {/* F1: card result modal */}
      {showRunModal && (
        <Modal onClose={() => { setShowRunModal(false); setGateError(''); setGateResult(null); setGateOutputOpen(false) }}>
          <ModalHead title={t['board.result_modal_title']} onClose={() => { setShowRunModal(false); setGateError(''); setGateResult(null); setGateOutputOpen(false) }} />
          <div className="run-modal-body">
            {runResultLoading && <Spinner label={t['common.loading']} />}
            {!runResultLoading && runResult && !runResult.exists && (
              <div className="error-state">
                Sidecar not found — the card has not run yet or the result was deleted.
              </div>
            )}
            {!runResultLoading && runResult?.exists && (
              <div className="markdown-wrap">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{runResult.content}</ReactMarkdown>
              </div>
            )}
            {/* C2-gate: buttons / banner based on meta */}
            {!runResultLoading && runResult && (() => {
              const meta = runResult.meta
              if (!meta) return null
              if (meta.applied) {
                return <div className="gate-banner gate-banner-applied">{t['board.gate_applied_banner']}</div>
              }
              if (meta.discarded) {
                return <div className="gate-banner gate-banner-discarded">{t['board.gate_discarded_banner']}</div>
              }
              if (meta.mode === 'worktree' && meta.has_changes) {
                const applyClass = gateResult?.verdict === 'safe'
                  ? 'btn-primary gate-apply gate-apply-safe'
                  : gateResult?.verdict === 'risky'
                    ? 'btn-primary gate-apply gate-apply-risky'
                    : 'btn-primary gate-apply'
                return (
                  <div className="gate-actions">
                    {gateError && <div className="error-state gate-error">{gateError}</div>}

                    {/* Spec 009: quality gate — "Check" button + verdict */}
                    <div className="gate-check-row">
                      <button
                        className="btn-secondary gate-check"
                        aria-label={t['board.gate_check_aria']}
                        disabled={gateChecking || gateBusy}
                        onClick={() => checkCard(meta.card_id)}
                      >
                        {gateChecking ? t['board.gate_checking'] : t['board.gate_check']}
                      </button>
                      {gateResult && (
                        <span
                          className={`gate-verdict gate-verdict-${gateResult.verdict}`}
                          aria-live="polite"
                        >
                          {gateResult.verdict === 'safe' && t['board.gate_verdict_safe']}
                          {gateResult.verdict === 'risky' && t['board.gate_verdict_risky']}
                          {gateResult.verdict === 'unknown' && (
                            gateResult.reason === 'legacy'
                              ? t['board.gate_verdict_unknown_reason']
                              : t['board.gate_verdict_unknown']
                          )}
                        </span>
                      )}
                    </div>

                    {/* Collapsible test output (if risky) */}
                    {gateResult?.tests?.detected && gateResult.tests.output && (
                      <details
                        className="gate-output-details"
                        open={gateOutputOpen}
                        onToggle={e => setGateOutputOpen((e.target as HTMLDetailsElement).open)}
                      >
                        <summary className="gate-output-summary">{t['board.gate_output_toggle']}</summary>
                        <pre className="gate-output-pre">{gateResult.tests.output}</pre>
                      </details>
                    )}

                    <button
                      className={applyClass}
                      aria-label={t['board.gate_apply_aria']}
                      disabled={gateBusy || gateChecking}
                      onClick={() => {
                        const cardId = meta.card_id
                        applyCard(cardId)
                      }}
                    >{t['board.gate_apply']}</button>
                    <button
                      className="btn-danger gate-discard"
                      aria-label={t['board.gate_discard_aria']}
                      disabled={gateBusy || gateChecking}
                      onClick={() => setConfirmDiscard({ cardId: meta.card_id })}
                    >{t['board.gate_discard']}</button>
                  </div>
                )
              }
              if (meta.mode === 'worktree' && !meta.has_changes) {
                return <div className="gate-banner">{t['board.gate_no_changes_banner']}</div>
              }
              // legacy
              return <div className="gate-banner">{t['board.gate_legacy_banner']}</div>
            })()}
          </div>
        </Modal>
      )}

      {/* C2-gate: discard confirmation */}
      {confirmDiscard && (
        <Modal onClose={() => setConfirmDiscard(null)}>
          <ModalHead title={t['board.gate_confirm_title']} onClose={() => setConfirmDiscard(null)} />
          <div className="run-modal-body" role="dialog" aria-modal="true">
            <p>{t['board.gate_confirm_body']}</p>
            <div className="gate-actions">
              <button
                className="btn-danger"
                aria-label={t['board.gate_confirm_aria']}
                disabled={gateBusy}
                onClick={() => discardCard(confirmDiscard.cardId)}
              >{t['board.gate_confirm_yes']}</button>
              <button
                className="btn-secondary"
                onClick={() => setConfirmDiscard(null)}
              >{t['common.cancel']}</button>
            </div>
          </div>
        </Modal>
      )}

      {/* C2-gate: Toast */}
      {gateToast && (
        <div className="gate-toast" role="status" aria-live="polite">
          {gateToast}
          <button
            className="gate-toast-close"
            aria-label={t['toast.close_aria']}
            onClick={() => setGateToast('')}
          >✕</button>
        </div>
      )}

      {/* Full-task editor modal — double-click any card to edit the whole task text */}
      {taskEditModal && (
        <Modal onClose={() => setTaskEditModal(null)}>
          <ModalHead
            title={t['board.edit_task_modal_title']}
            onClose={() => setTaskEditModal(null)}
            extra={
              <button
                className="btn-primary"
                style={{ padding: '2px 10px', fontSize: 13 }}
                disabled={busy}
                onClick={saveTaskEdit}
              >{t['common.save']}</button>
            }
          />
          <div className="run-modal-body">
            <textarea
              className="board-desc-edit-input"
              value={taskEditModal.text}
              autoFocus
              rows={10}
              onChange={e => setTaskEditModal({ ...taskEditModal, text: e.target.value })}
              onKeyDown={e => {
                if (e.key === 'Escape') setTaskEditModal(null)
              }}
              placeholder={t['board.edit_task_placeholder']}
              style={{ width: '100%', resize: 'vertical', fontFamily: 'monospace', fontSize: 13 }}
            />
            {/* Card 43665f: per-card model override — (Default) means use board_card_model / sonnet */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10 }}>
              <label style={{ fontSize: 12, color: 'var(--text3)', flexShrink: 0 }}>
                {t['board.card_model_label']}
              </label>
              <select
                value={taskEditModal.model}
                onChange={e => setTaskEditModal({ ...taskEditModal, model: e.target.value })}
                style={{ fontSize: 12, padding: '2px 6px' }}
              >
                <option value="">{t['board.card_model_default']}</option>
                {MODELS.map(m => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>
          </div>
        </Modal>
      )}

      {/* Card 5e1c0a: spec modal — view/edit attached markdown doc */}
      {specModal && (
        <Modal onClose={() => !specModal.saving && setSpecModal(null)}>
          <ModalHead
            title={t['board.spec_modal_title']}
            onClose={() => !specModal.saving && setSpecModal(null)}
            extra={
              <button
                className="btn-primary"
                style={{ padding: '2px 10px', fontSize: 13 }}
                disabled={specModal.loading || specModal.saving}
                onClick={saveSpec}
              >{specModal.saving ? t['board.spec_saving'] : t['board.spec_save']}</button>
            }
          />
          <div className="run-modal-body">
            {specModal.loading
              ? <Spinner label={t['common.loading']} />
              : (
                <textarea
                  className="spec-modal-textarea"
                  value={specModal.content}
                  autoFocus
                  disabled={specModal.saving}
                  placeholder={t['board.spec_placeholder']}
                  onChange={e => setSpecModal(prev => prev ? { ...prev, content: e.target.value } : null)}
                  onKeyDown={e => {
                    if (e.key === 'Escape' && !specModal.saving) setSpecModal(null)
                  }}
                />
              )
            }
          </div>
        </Modal>
      )}
    </div>
  )
}

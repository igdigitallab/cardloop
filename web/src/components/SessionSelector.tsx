/**
 * Session selector dropdown.
 * Manages session switching for the chat panel.
 * The dropdown is portaled to document.body to escape any ancestor stacking context.
 *
 * spec-042: reset flow is fully delegated to the parent (ChatTab) via onRequestReset.
 *   The parent owns the unified reset-confirm modal with "New session + handoff" /
 *   "New session (blank)" choices. This component no longer shows its own reset modal.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import { SessionInfo } from '../types'
import { Modal, ModalHead } from './Modal'

interface Props {
  projectId: string
  onSessionChange: () => void
  /**
   * spec-042: called when the user clicks "New session" in the dropdown.
   * The parent opens the unified reset-confirm modal (handoff vs blank).
   * If not provided, the "New session" button is hidden (fallback for contexts
   * where reset is not applicable).
   */
  onRequestReset?: () => void
  /**
   * Bumped by the parent whenever the session is reset/rotated. Adding it to the load effect's
   * deps forces a refetch so the button stops showing the now-closed session (the list otherwise
   * only refreshes on mount or when the dropdown is opened).
   */
  reloadSignal?: number
}

/** Format ISO datetime as absolute clock string for session labels. */
function fmtSessionTime(iso: string): string {
  try {
    const d = new Date(iso)
    const now = new Date()
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    if (sameDay) {
      // Today: show time only, e.g. "14:32"
      return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    }
    const yesterday = new Date(now)
    yesterday.setDate(yesterday.getDate() - 1)
    const sameYesterday =
      d.getFullYear() === yesterday.getFullYear() &&
      d.getMonth() === yesterday.getMonth() &&
      d.getDate() === yesterday.getDate()
    if (sameYesterday) {
      return 'Yesterday ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    }
    // Older: short date + time
    return (
      d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
      ' ' +
      d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    )
  } catch {
    return ''
  }
}

/** Format a context token count as a compact string: 168000 → "168K", 1200000 → "1.2M". */
function fmtCtx(n: number): string {
  if (n < 1000) return `${n}`
  if (n < 1_000_000) return `${Math.round(n / 1000)}K`
  return `${(n / 1_000_000).toFixed(1)}M`
}

/** Anchor width used when clamping the dropdown to avoid right-edge overflow. */
const DROPDOWN_ANCHOR_WIDTH = 360

export function SessionSelector({ projectId, onSessionChange, onRequestReset, reloadSignal }: Props) {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Ref on the outer wrapper (button area) — used for outside-click detection
  const dropRef = useRef<HTMLDivElement>(null)
  // Ref on the toggle button — used to compute anchored position
  const btnRef = useRef<HTMLButtonElement>(null)
  // Ref on the portaled dropdown list — used for outside-click detection
  const dropdownRef = useRef<HTMLDivElement>(null)

  const [renameModal, setRenameModal] = useState<{ session: SessionInfo; value: string } | null>(null)

  // Computed desktop anchor style (null = mobile, let CSS bottom-sheet win)
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties | null>(null)

  const loadSessions = useCallback(async () => {
    try {
      const res = await api.sessions(projectId)
      setSessions(res.sessions)
    } catch {
      // non-critical
    }
  }, [projectId])

  useEffect(() => {
    loadSessions()
    setOpen(false)
    setError('')
  }, [projectId, loadSessions, reloadSignal])

  // Compute anchored position from the toggle button rect.
  // Same behaviour on mobile and desktop: the list drops DOWN from the button
  // (portaled to body, position:fixed). On mobile we clamp the width to the
  // viewport so a narrow screen never overflows. (Previously mobile returned
  // null and fell back to a bottom-sheet, which looked broken for short lists —
  // a 1-session list became a thin strip glued to the bottom edge.)
  function computeStyle(): React.CSSProperties | null {
    const btn = btnRef.current
    if (!btn) return null
    const rect = btn.getBoundingClientRect()
    const vw = window.innerWidth
    const isMobile = vw <= 768
    const width = isMobile ? Math.min(vw - 16, 440) : DROPDOWN_ANCHOR_WIDTH
    let left = rect.left
    if (left + width > vw) left = vw - width - 8
    if (left < 8) left = 8
    return {
      position: 'fixed',
      top: rect.bottom + 4,
      left,
      // Explicit width on mobile so the CSS bottom-sheet's left/right:8px can't
      // fight the inline anchor; desktop keeps its min/max-width from CSS.
      width: isMobile ? width : undefined,
      zIndex: 10001,
    }
  }

  // Recompute anchor whenever the dropdown opens or the window resizes/scrolls.
  useEffect(() => {
    if (!open) return
    setDropdownStyle(computeStyle())

    function update() {
      setDropdownStyle(computeStyle())
    }
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true) // capture scroll anywhere
    return () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // Outside-click: close only when the click is outside BOTH the selector wrapper
  // AND the portaled dropdown. Also close on Esc.
  useEffect(() => {
    if (!open) return

    function onMouseDown(e: MouseEvent) {
      const target = e.target as Node
      const insideSelector = dropRef.current?.contains(target) ?? false
      const insideDropdown = dropdownRef.current?.contains(target) ?? false
      if (!insideSelector && !insideDropdown) {
        setOpen(false)
      }
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }

    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const activeSession = sessions.find(s => s.is_active)
  const activeLabel = activeSession
    ? (activeSession.label || (activeSession.session_id.slice(0, 8) + '…'))
    : 'new'

  async function switchSession(action: 'new' | 'resume', session_id?: string) {
    setBusy(true)
    setError('')
    try {
      if (action === 'new') {
        await api.setSession(projectId, { action: 'new' })
      } else {
        await api.setSession(projectId, { action: 'resume', session_id: session_id! })
      }
      await loadSessions()
      onSessionChange()
      setOpen(false)
    } catch (err) {
      const e = err as { status?: number; message?: string }
      if (e?.status === 409) {
        setError('project is busy')
      } else {
        setError(e?.message || 'error')
      }
    } finally {
      setBusy(false)
    }
  }

  async function commitRename() {
    if (!renameModal) return
    const { session, value } = renameModal
    setRenameModal(null)
    try {
      await api.setSessionLabel(projectId, session.session_id, value.trim())
      await loadSessions()
      onSessionChange()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'rename error')
    }
  }

  /**
   * Render a human-readable label for a session entry (spec-042 session clarity).
   * Primary line: "Session · <start time>"
   * Secondary line (dimmed): first-message preview
   *
   * Note: the backend provides `last_used` (file mtime, used as session timestamp)
   * and `preview` (first human-readable message). `message_count` and a separate
   * `created` timestamp are NOT yet provided by the backend — using `last_used`
   * as the time indicator until the backend exposes those fields.
   */
  function renderSessionLabel(s: SessionInfo) {
    // Use `created` if the backend provides it (future), else fall back to `last_used`.
    const timeStr = fmtSessionTime(s.created ?? s.last_used)
    const msgs = typeof s.message_count === 'number' && s.message_count > 0
      ? ` · ${s.message_count} msgs`
      : ''
    const ctx = typeof s.context_tokens === 'number' && s.context_tokens > 0
      ? ` · ${fmtCtx(s.context_tokens)}`
      : ''
    const primaryLine = `Session · ${timeStr}${msgs}${ctx}`

    if (s.label) {
      // Named session: show user label as strong, then the standard "Session · time" sub-line
      return (
        <span className="session-item-label-block">
          <span className="session-item-label-primary"><strong>{s.label}</strong></span>
          <span className="session-item-label-secondary">{primaryLine} · {s.preview}</span>
        </span>
      )
    }

    return (
      <span className="session-item-label-block">
        <span className="session-item-label-primary">{primaryLine}</span>
        <span className="session-item-label-secondary">{s.preview}</span>
      </span>
    )
  }

  // The portaled dropdown element. On mobile dropdownStyle is null, so no
  // inline position/top/left — the CSS bottom-sheet rules take effect instead.
  const dropdown = (
    <div
      className="session-dropdown"
      role="listbox"
      ref={dropdownRef}
      style={dropdownStyle ?? undefined}
    >
      {onRequestReset && (
        <button
          className="session-dropdown-item session-new-item"
          onClick={() => { setOpen(false); onRequestReset() }}
          disabled={busy}
        >
          ➕ New session
        </button>
      )}
      {sessions.length > 0 && <div className="session-dropdown-sep" />}
      {sessions.map(s => (
        <div key={s.session_id} className="session-dropdown-row">
          <button
            className={`session-dropdown-item session-item-two-line${s.is_active ? ' active' : ''}`}
            onClick={() => switchSession('resume', s.session_id)}
            disabled={busy}
            title={s.label ? `${s.label}\n${s.preview}` : s.preview}
            role="option"
            aria-selected={s.is_active}
          >
            <span className="session-item-check">{s.is_active ? '✓' : ''}</span>
            <span className="session-item-preview">
              {renderSessionLabel(s)}
            </span>
          </button>
          <button
            className="session-rename-btn"
            onClick={e => { e.stopPropagation(); setRenameModal({ session: s, value: s.label || '' }); setOpen(false) }}
            disabled={busy}
            title="Rename session"
            aria-label="Rename session"
          >✎</button>
        </div>
      ))}
      {sessions.length === 0 && (
        <div className="session-dropdown-empty">no saved sessions</div>
      )}
    </div>
  )

  return (
    <div className="session-selector" ref={dropRef}>
      <button
        className="session-selector-btn"
        ref={btnRef}
        onClick={() => { setOpen(o => !o); if (!open) loadSessions() }}
        disabled={busy}
        title="Select session"
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="session-icon">◉</span>
        <span className="session-label">{activeLabel}</span>
        <span className="session-chevron">{open ? '▲' : '▼'}</span>
      </button>

      {error && <div className="session-error">{error}</div>}

      {/* Dropdown + backdrop portaled to document.body to escape ancestor stacking contexts.
          The backdrop is hidden on desktop via CSS (display:none default); on mobile (≤768px)
          it becomes display:block to dim content behind the bottom-sheet. */}
      {open && createPortal(
        <>
          <div className="session-dropdown-backdrop" onClick={() => setOpen(false)} />
          {dropdown}
        </>,
        document.body
      )}

      {/* Rename modal */}
      {renameModal && (
        <Modal onClose={() => setRenameModal(null)}>
          <ModalHead title="Rename session" onClose={() => setRenameModal(null)} />
          <div className="run-modal-body">
            <input
              className="rename-input"
              style={{ width: '100%', marginBottom: 12 }}
              autoFocus
              placeholder="Session name (empty — remove label)"
              value={renameModal.value}
              onChange={e => setRenameModal(m => m ? { ...m, value: e.target.value } : m)}
              onKeyDown={e => {
                if (e.key === 'Enter') commitRename()
                if (e.key === 'Escape') setRenameModal(null)
              }}
            />
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn-secondary" onClick={() => setRenameModal(null)}>Cancel</button>
              <button className="btn-primary" onClick={commitRename}>Save</button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}

/**
 * Session selector dropdown + confirm-reset modal.
 * Manages session switching for the chat panel.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { SessionInfo } from '../types'
import { useClickOutside } from '../hooks/useClickOutside'
import { Modal, ModalHead } from './Modal'

interface Props {
  projectId: string
  onSessionChange: () => void
  /** Called when the user wants to insert a "wrap-up prompt" into the chat input. */
  onInsertResetPrompt?: (text: string) => void
}

const DEFAULT_RESET_PROMPT =
  "Wrapping up the session. Before you go:\n" +
  "1. Review the card list in TASKS.md, mark completed ones (move to Done via my command or tell me).\n" +
  "2. Check for any junk temporary files in cwd (untitled, scratch, .bak) — suggest deleting them.\n" +
  "3. If there are uncommitted changes — a short description of what and why (commit message).\n" +
  "Don't write code, just check and report."

/** Format ISO datetime as relative time */
function relTime(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 2) return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h ago`
    const days = Math.floor(hrs / 24)
    return `${days}d ago`
  } catch {
    return ''
  }
}

export function SessionSelector({ projectId, onSessionChange, onInsertResetPrompt }: Props) {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dropRef = useRef<HTMLDivElement>(null)
  const [confirmReset, setConfirmReset] = useState(false)
  const [resetPromptText, setResetPromptText] = useState(DEFAULT_RESET_PROMPT)
  const [renameModal, setRenameModal] = useState<{ session: SessionInfo; value: string } | null>(null)

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
  }, [projectId, loadSessions])

  useClickOutside(dropRef, () => setOpen(false), open)

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

  function requestReset() {
    setResetPromptText(DEFAULT_RESET_PROMPT)
    setConfirmReset(true)
    setOpen(false)
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

  return (
    <div className="session-selector" ref={dropRef}>
      <button
        className="session-reset-btn"
        onClick={requestReset}
        disabled={busy}
        title="New session (no summary)"
      >↺</button>
      <button
        className="session-selector-btn"
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

      {open && (
        <div className="session-dropdown" role="listbox">
          <button
            className="session-dropdown-item session-new-item"
            onClick={requestReset}
            disabled={busy}
          >
            ➕ New session
          </button>
          {sessions.length > 0 && <div className="session-dropdown-sep" />}
          {sessions.map(s => (
            <div key={s.session_id} className="session-dropdown-row">
              <button
                className={`session-dropdown-item${s.is_active ? ' active' : ''}`}
                onClick={() => switchSession('resume', s.session_id)}
                disabled={busy}
                title={s.label ? `${s.label}\n— ${s.preview}` : s.preview}
                role="option"
                aria-selected={s.is_active}
              >
                <span className="session-item-check">{s.is_active ? '✓' : ''}</span>
                <span className="session-item-preview">
                  {s.label
                    ? <><strong>{s.label}</strong> <span className="session-item-sub">— {s.preview}</span></>
                    : s.preview}
                </span>
                <span className="session-item-time">{relTime(s.last_used)}</span>
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

      {/* Reset confirm modal */}
      {confirmReset && (
        <div className="reset-confirm-overlay" onClick={() => setConfirmReset(false)}>
          <div className="reset-confirm-modal" onClick={e => e.stopPropagation()}>
            <div className="reset-confirm-head">
              <span>New session</span>
              <button className="reset-confirm-close" onClick={() => setConfirmReset(false)}>✕</button>
            </div>
            <div className="reset-confirm-body">
              <p className="reset-confirm-hint">
                The current session context will be reset. Before closing, you can send the agent a "wrap-up" prompt (it will mark completed cards and check for junk):
              </p>
              <textarea
                className="reset-confirm-textarea"
                value={resetPromptText}
                onChange={e => setResetPromptText(e.target.value)}
                rows={7}
              />
              <div className="reset-confirm-actions">
                <button
                  className="reset-confirm-btn-cancel"
                  onClick={() => setConfirmReset(false)}
                  disabled={busy}
                >Cancel</button>
                <button
                  className="reset-confirm-btn-skip"
                  onClick={() => { setConfirmReset(false); switchSession('new') }}
                  disabled={busy}
                  title="Reset session without sending a prompt"
                >Just new session</button>
                <button
                  className="reset-confirm-btn-send"
                  onClick={() => {
                    if (onInsertResetPrompt) onInsertResetPrompt(resetPromptText)
                    setConfirmReset(false)
                  }}
                  disabled={busy || !onInsertResetPrompt}
                  title="Insert prompt into chat — send it, then click ↺ again for a new session"
                >📋 Insert into chat</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

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
  /** Вызывается когда юзер хочет вставить «промт-завершения» в чат-инпут. */
  onInsertResetPrompt?: (text: string) => void
}

const DEFAULT_RESET_PROMPT =
  "Заканчиваем сессию. Перед тем как уйти:\n" +
  "1. Просмотри список карточек в TASKS.md, отметь выполненные (передвинь в Done через мою команду или скажи мне).\n" +
  "2. Проверь нет ли мусорных временных файлов в cwd (untitled, scratch, .bak) — предложи удалить.\n" +
  "3. Если есть незакоммиченные правки — короткое описание что и зачем (commit-сообщение).\n" +
  "Не пиши код, просто проверь и доложи."

/** Format ISO datetime as relative time */
function relTime(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 2) return 'только что'
    if (mins < 60) return `${mins} мин назад`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs} ч назад`
    const days = Math.floor(hrs / 24)
    return `${days} дн назад`
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
    : 'новая'

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
        setError('проект занят')
      } else {
        setError(e?.message || 'ошибка')
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
      setError(err instanceof Error ? err.message : 'ошибка переименования')
    }
  }

  return (
    <div className="session-selector" ref={dropRef}>
      <button
        className="session-reset-btn"
        onClick={requestReset}
        disabled={busy}
        title="Новая сессия (с подтверждением)"
      >↺</button>
      <button
        className="session-selector-btn"
        onClick={() => { setOpen(o => !o); if (!open) loadSessions() }}
        disabled={busy}
        title="Выбрать сессию"
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
            ➕ Новая сессия
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
                title="Переименовать сессию"
                aria-label="Переименовать сессию"
              >✎</button>
            </div>
          ))}
          {sessions.length === 0 && (
            <div className="session-dropdown-empty">нет сохранённых сессий</div>
          )}
        </div>
      )}

      {/* Rename modal */}
      {renameModal && (
        <Modal onClose={() => setRenameModal(null)}>
          <ModalHead title="Переименовать сессию" onClose={() => setRenameModal(null)} />
          <div className="run-modal-body">
            <input
              className="rename-input"
              style={{ width: '100%', marginBottom: 12 }}
              autoFocus
              placeholder="Имя сессии (пусто — убрать лейбл)"
              value={renameModal.value}
              onChange={e => setRenameModal(m => m ? { ...m, value: e.target.value } : m)}
              onKeyDown={e => {
                if (e.key === 'Enter') commitRename()
                if (e.key === 'Escape') setRenameModal(null)
              }}
            />
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn-secondary" onClick={() => setRenameModal(null)}>Отмена</button>
              <button className="btn-primary" onClick={commitRename}>Сохранить</button>
            </div>
          </div>
        </Modal>
      )}

      {/* Reset confirm modal */}
      {confirmReset && (
        <div className="reset-confirm-overlay" onClick={() => setConfirmReset(false)}>
          <div className="reset-confirm-modal" onClick={e => e.stopPropagation()}>
            <div className="reset-confirm-head">
              <span>Новая сессия</span>
              <button className="reset-confirm-close" onClick={() => setConfirmReset(false)}>✕</button>
            </div>
            <div className="reset-confirm-body">
              <p className="reset-confirm-hint">
                Контекст текущей сессии сбросится. Перед закрытием можно отправить агенту промт-«завершение» (отметит сделанные карточки, проверит мусор):
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
                >Отмена</button>
                <button
                  className="reset-confirm-btn-skip"
                  onClick={() => { setConfirmReset(false); switchSession('new') }}
                  disabled={busy}
                  title="Сбросить сессию без отправки промта"
                >Просто новая сессия</button>
                <button
                  className="reset-confirm-btn-send"
                  onClick={() => {
                    if (onInsertResetPrompt) onInsertResetPrompt(resetPromptText)
                    setConfirmReset(false)
                  }}
                  disabled={busy || !onInsertResetPrompt}
                  title="Вставить промт в чат — отправь его, потом нажми ↺ ещё раз для новой сессии"
                >📋 Вставить в чат</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

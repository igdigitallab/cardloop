import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { Board, BoardColumn, GateResult, RunResult, TaskCard, isIncidentCard } from '../types'
import { Spinner } from '../components/Spinner'
import { Modal, ModalHead } from '../components/Modal'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

/** Возвращает бейдж авто-починки из description карточки, или null если не авто-чинилась. */
function getSelfHealBadge(card: TaskCard): string | null {
  const desc = card.description || ''
  const m = desc.match(/heal_badge=(.+)/)
  return m ? m[1].trim() : null
}

interface Props {
  projectId: string
  /** When false (project tab hidden via display:none), suspend polling to avoid wasted fetches. */
  isActive?: boolean
}

const ORDER = ['backlog', 'in_progress', 'review', 'failed']
// Стрелки ←/→ ходят по «парковочным» колонкам, ПРОПУСКАЯ in_progress: единственный
// способ запустить агента — кнопка 🤖 (раньше → из Backlog дублировала робота).
const PARK_ORDER = ['backlog', 'review', 'failed']
const POLL_FAST_MS = 3000   // когда есть карточки в In Progress (агент работает)
const POLL_SLOW_MS = 10000  // фоновый poll (правки TASKS.md от агента через чат, внешние правки)

const LS_BOARD_COLS = 'cops.boardVisibleCols'
const DEFAULT_VISIBLE: string[] = ['backlog']  // дефолт — только Backlog

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

export function BoardTab({ projectId, isActive = true }: Props) {
  const [board, setBoard] = useState<Board | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [newText, setNewText] = useState('')
  const [showArchive, setShowArchive] = useState(false)
  const [archive, setArchive] = useState<string | null>(null)
  // Инлайн-редактирование карточки: двойной клик → textarea
  const [editingCard, setEditingCard] = useState<{ id: string; text: string } | null>(null)

  // Description модалка: просмотр + редактирование описания карточки
  const [descModal, setDescModal] = useState<{ card: TaskCard } | null>(null)
  const [editingDesc, setEditingDesc] = useState<string | null>(null)  // null = read mode, string = edit mode

  // Drag-and-drop
  const [dragCardId, setDragCardId] = useState<string | null>(null)
  const [dragOverCol, setDragOverCol] = useState<string | null>(null)

  // F1: модалка результата карточки
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

  // Spec 009: quality gate — результат проверки тестов перед применением
  const [gateResult, setGateResult] = useState<GateResult | null>(null)
  const [gateChecking, setGateChecking] = useState(false)
  const [gateOutputOpen, setGateOutputOpen] = useState(false)

  // Видимые колонки (persist в localStorage). Дефолт — только Backlog.
  const [visibleCols, setVisibleCols] = useState<Set<string>>(() => readVisibleCols())

  function toggleCol(key: string) {
    setVisibleCols(prev => {
      const next = new Set(prev)
      if (next.has(key)) {
        if (next.size <= 1) return prev  // нельзя скрыть последнюю
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

  // F1: есть ли карточки в In Progress — частим polling
  function hasInProgress(b: Board | null): boolean {
    if (!b) return false
    const col = b.columns.find(c => c.key === 'in_progress')
    return (col?.cards.length ?? 0) > 0
  }

  // Keep isActive in a ref so the polling closure always sees the current value
  const isActiveRef = useRef(isActive)
  isActiveRef.current = isActive

  // Polling: 3с пока есть in_progress, 10с в покое; не тикает когда вкладка скрыта или ProjectView неактивен
  function schedulePoll(b: Board | null) {
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    const delay = hasInProgress(b) ? POLL_FAST_MS : POLL_SLOW_MS
    pollTimerRef.current = setTimeout(async () => {
      // Skip poll if project tab is hidden (display:none) or browser tab invisible
      if (!isActiveRef.current || document.visibilityState !== 'visible') {
        schedulePoll(b)  // ждём до следующего тика
        return
      }
      try {
        const fresh = await api.tasks(projectIdRef.current)
        setBoard(fresh)
        schedulePoll(fresh)
      } catch {
        schedulePoll(b)
      }
    }, delay)
  }

  // Мгновенный refresh (focus, visibility, run_end из шины)
  async function refreshNow() {
    try {
      const fresh = await api.tasks(projectIdRef.current)
      setBoard(fresh)
      schedulePoll(fresh)
    } catch { /* тихо игнорим — следующий poll-тик попробует */ }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setBoard(null)
    setShowArchive(false); setArchive(null)
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)

    api.tasks(projectId).then(b => {
      if (!cancelled) {
        setBoard(b)
        setLoading(false)
        schedulePoll(b)
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

  // Refresh на focus/visibility (общий хук)
  useFocusRefresh(refreshNow)
  // Refresh на run_end из общей шины проекта — агент мог изменить TASKS.md, карточка могла уйти Review/Failed
  useOnRunEnd(refreshNow)

  async function run(p: Promise<Board>) {
    setBusy(true); setError('')
    try {
      const b = await p
      setBoard(b)
      schedulePoll(b)
    } catch (e) {
      // F1: 409 = проект занят
      const status = (e as { status?: number })?.status
      if (status === 409) {
        setError('⏳ Проект занят (TG или другая карточка) — попробуй позже')
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
    // Авто-сплит: первая строка / первые 120 символов = title, остальное = description
    let title = raw
    let description: string | null = null
    const nlIdx = raw.indexOf('\n')
    if (nlIdx !== -1) {
      title = raw.slice(0, nlIdx).trim()
      description = raw.slice(nlIdx + 1).trim() || null
    } else if (raw.length > 120) {
      title = raw.slice(0, 120).trimEnd()
      description = raw.slice(120).trim() || null
    }
    run(api.createTask(projectId, title, 'backlog', description))
  }

  function move(card: string, to: string) { run(api.moveTask(projectId, card, to)) }
  function del(card: string) { run(api.deleteTask(projectId, card)) }

  async function saveCardEdit() {
    if (!editingCard) return
    const { id, text } = editingCard
    setEditingCard(null)
    const trimmed = text.trim()
    if (!trimmed) return
    run(api.updateTask(projectId, id, trimmed))
  }

  function openDescModal(card: TaskCard) {
    setDescModal({ card })
    setEditingDesc(null)
  }

  function closeDescModal() {
    setDescModal(null)
    setEditingDesc(null)
  }

  async function saveDescEdit() {
    if (!descModal || editingDesc === null) return
    const { card } = descModal
    const newDesc = editingDesc.trim() || null
    setEditingDesc(null)
    setBusy(true); setError('')
    try {
      const fresh = await api.updateTask(projectId, card.id, card.text, newDesc)
      setBoard(fresh)
      schedulePoll(fresh)
      // синхронизируем модалку с обновлёнными данными
      for (const col of fresh.columns) {
        const found = col.cards.find(c => c.id === card.id)
        if (found) { setDescModal({ card: found }); break }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function toggleArchive() {
    if (showArchive) { setShowArchive(false); return }
    setShowArchive(true)
    if (archive === null) {
      api.tasksDone(projectId)
        .then(d => setArchive(d.content || '*Архив пуст*'))
        .catch(e => setArchive(`⚠ ${e.message || e}`))
    }
  }

  // F1: показать результат выполнения карточки
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
      // мета хранится в runResult.meta — кнопки гейта читают оттуда
    } catch (e) {
      setRunResult({ content: `⚠ Ошибка загрузки: ${e instanceof Error ? e.message : String(e)}`, exists: false })
    } finally {
      setRunResultLoading(false)
    }
  }

  // C2-gate: показать toast и скрыть через 4 секунды
  function showToast(msg: string) {
    setGateToast(msg)
    setTimeout(() => setGateToast(''), 4000)
  }

  // C2-gate: применить изменения (merge)
  async function applyCard(cardId: string) {
    setGateBusy(true)
    setGateError('')
    try {
      await api.applyCard(projectId, cardId)
      setShowRunModal(false)
      showToast('✓ Изменения применены')
      await refreshNow()
    } catch (e) {
      const status = (e as { status?: number })?.status
      const msg = e instanceof Error ? e.message : String(e)
      if (status === 409) {
        setGateError('Конфликт merge: ' + msg)
      } else {
        setGateError(msg)
      }
    } finally {
      setGateBusy(false)
    }
  }

  // C2-gate: отменить изменения (discard)
  async function discardCard(cardId: string) {
    setConfirmDiscard(null)
    setGateBusy(true)
    setGateError('')
    try {
      await api.discardCard(projectId, cardId)
      setShowRunModal(false)
      showToast('Изменения карточки отменены')
      await refreshNow()
    } catch (e) {
      setGateError(e instanceof Error ? e.message : String(e))
    } finally {
      setGateBusy(false)
    }
  }

  // Spec 009: quality gate — прогнать тесты в worktree карточки
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

  if (loading) return <Spinner label="Загрузка доски..." />

  const cols = board?.columns ?? []
  const colByKey = (k: string): BoardColumn | undefined => cols.find(c => c.key === k)

  const visibleOrder = ORDER.filter(k => visibleCols.has(k))

  return (
    <div className="board-wrap">
      {error && <div className="error-state" style={{ marginBottom: 10 }}>⚠ {error}</div>}

      {/* Тогглы колонок — показать/скрыть. Если в скрытой колонке есть карточки, подсвечиваем счётчик. */}
      <div className="board-col-toggles">
        <span className="board-col-toggles-label">колонки:</span>
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
              title={isOn ? `Скрыть «${label}»` : `Показать «${label}»`}
            >
              {label}{count > 0 ? ` (${count})` : ''}
            </button>
          )
        })}
      </div>

      <div className="board-columns">
        {visibleOrder.map(key => {
          const col = colByKey(key)
          if (!col) return null
          const parkIdx = PARK_ORDER.indexOf(key)   // -1 для in_progress → стрелки скрыты
          const isInProgress = key === 'in_progress'
          const canShowResult = key === 'review' || key === 'failed'
          return (
            <div className={`board-col board-col-${key}`} key={key}>
              <div className="board-col-head">
                <span className="board-col-label">{col.label}</span>
                <span className="board-col-count">{col.cards.length}</span>
                {/* F1: индикатор работы агента в заголовке колонки */}
                {isInProgress && col.cards.length > 0 && (
                  <span className="board-col-running" title="Агент работает, авто-обновление...">⚙</span>
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
                      placeholder="Новая задача… (Enter — добавить)"
                      value={newText}
                      onChange={e => setNewText(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); addCard() }
                      }}
                      rows={2}
                    />
                    <button className="btn-primary" disabled={busy || !newText.trim()}
                      onClick={addCard}>+ Добавить</button>
                  </div>
                )}

                {col.cards.map(card => {
                  const isIncident = isIncidentCard(card)
                  const selfHealBadge = getSelfHealBadge(card)
                  return (
                  <div
                    className={[
                      'board-card',
                      isInProgress ? 'board-card-running' : '',
                      dragCardId === card.id ? 'board-card-dragging' : '',
                      isIncident ? 'board-card-incident' : '',
                      selfHealBadge ? 'board-card-self-heal' : '',
                    ].filter(Boolean).join(' ')}
                    key={card.id}
                    draggable={!isInProgress}
                    onDragStart={(e) => {
                      setDragCardId(card.id)
                      e.dataTransfer.effectAllowed = 'move'
                      e.dataTransfer.setData('text/plain', card.id)
                    }}
                    onDragEnd={() => { setDragCardId(null); setDragOverCol(null) }}
                  >
                    {editingCard?.id === card.id ? (
                      <textarea
                        className="board-card-edit-input"
                        value={editingCard.text}
                        autoFocus
                        rows={3}
                        onChange={e => setEditingCard({ id: card.id, text: e.target.value })}
                        onBlur={saveCardEdit}
                        onKeyDown={e => {
                          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveCardEdit() }
                          if (e.key === 'Escape') setEditingCard(null)
                        }}
                      />
                    ) : (
                      <div
                        className="board-card-text"
                        onDoubleClick={() => !isInProgress && setEditingCard({ id: card.id, text: card.text })}
                        title={isInProgress ? '' : 'Двойной клик — редактировать'}
                      >
                        {isIncident && <span className="card-incident-icon" title="Инцидент (источник: log/test)">⚠ </span>}
                        {isInProgress && <span className="card-running-icon" title="Выполняется агентом">⚙ </span>}
                        <span className="board-card-title">{card.text}</span>
                        {selfHealBadge && (
                          <span
                            className={`card-self-heal-badge ${selfHealBadge.includes('✓') ? 'badge-ok' : 'badge-fail'}`}
                            aria-label={t['board.self_heal_badge_aria']}
                            title={selfHealBadge}
                          >{selfHealBadge}</span>
                        )}
                        {card.description && (
                          <button
                            className="board-card-desc-btn"
                            title="Показать описание"
                            onClick={e => { e.stopPropagation(); openDescModal(card) }}
                          >📝</button>
                        )}
                      </div>
                    )}
                    <div className="board-card-actions">
                      {parkIdx >= 0 && (
                        <>
                          <button title="← переместить (без запуска)" aria-label="Переместить влево" disabled={busy || parkIdx === 0}
                            onClick={() => move(card.id, PARK_ORDER[parkIdx - 1])}>←</button>
                          <button title="переместить → (без запуска)" aria-label="Переместить вправо" disabled={busy || parkIdx === PARK_ORDER.length - 1}
                            onClick={() => move(card.id, PARK_ORDER[parkIdx + 1])}>→</button>
                        </>
                      )}
                      {col.key !== 'in_progress' && (
                        <button
                          title="🤖 Запустить агентом (→ In Progress)"
                          aria-label="Запустить агентом"
                          className="act-handoff"
                          disabled={busy}
                          onClick={() => move(card.id, 'in_progress')}
                        >🤖</button>
                      )}
                      {canShowResult && (
                        <button
                          title="Результат выполнения"
                          aria-label="Показать результат"
                          className="act-result"
                          disabled={busy}
                          onClick={() => showResult(card.id)}
                        >📄</button>
                      )}
                      <button title="✓ в Done (архив)" aria-label="Архивировать карточку" className="act-done" disabled={busy}
                        onClick={() => move(card.id, 'done')}>✓</button>
                      <button title="удалить" aria-label="Удалить карточку" className="act-del" disabled={busy}
                        onClick={() => del(card.id)}>✕</button>
                    </div>
                  </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>

      <div className="board-footer">
        <button className="board-archive-toggle" onClick={toggleArchive}>
          {showArchive ? '▾' : '▸'} Архив (Done) · {board?.done_count ?? 0}
        </button>
        {!board?.exists && (
          <span className="board-hint">TASKS.md ещё нет — создастся при первой задаче</span>
        )}
      </div>

      {showArchive && (
        <div className="board-archive">
          {archive === null
            ? <Spinner label="Загрузка архива..." />
            : <div className="markdown-wrap"><ReactMarkdown remarkPlugins={[remarkGfm]}>{archive}</ReactMarkdown></div>}
        </div>
      )}

      {/* F1: модалка результата карточки */}
      {showRunModal && (
        <Modal onClose={() => { setShowRunModal(false); setGateError(''); setGateResult(null); setGateOutputOpen(false) }}>
          <ModalHead title="Результат выполнения" onClose={() => { setShowRunModal(false); setGateError(''); setGateResult(null); setGateOutputOpen(false) }} />
          <div className="run-modal-body">
            {runResultLoading && <Spinner label="Загрузка..." />}
            {!runResultLoading && runResult && !runResult.exists && (
              <div className="error-state">
                Сайдкар не найден — карточка ещё не выполнялась или результат удалён.
              </div>
            )}
            {!runResultLoading && runResult?.exists && (
              <div className="markdown-wrap">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{runResult.content}</ReactMarkdown>
              </div>
            )}
            {/* C2-gate: кнопки / баннер по мета */}
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

                    {/* Spec 009: quality gate — кнопка «Проверить» + вердикт */}
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

                    {/* Сворачиваемый вывод тестов (если risky) */}
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

      {/* C2-gate: подтверждение discard */}
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
            aria-label="Закрыть уведомление"
            onClick={() => setGateToast('')}
          >✕</button>
        </div>
      )}

      {/* Description модалка */}
      {descModal && (
        <Modal onClose={closeDescModal}>
          <ModalHead
            title={
              <span style={{ fontWeight: 600, maxWidth: '92%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                {descModal.card.text}
              </span>
            }
            onClose={closeDescModal}
            extra={
              editingDesc === null ? (
                <button
                  className="run-modal-close"
                  title="Редактировать описание"
                  style={{ fontSize: 14 }}
                  onClick={() => setEditingDesc(descModal.card.description ?? '')}
                >✎</button>
              ) : (
                <button
                  className="btn-primary"
                  style={{ padding: '2px 10px', fontSize: 13 }}
                  disabled={busy}
                  onClick={saveDescEdit}
                >Сохранить</button>
              )
            }
          />
          <div className="run-modal-body">
            {editingDesc !== null ? (
              <textarea
                className="board-desc-edit-input"
                value={editingDesc}
                autoFocus
                rows={8}
                onChange={e => setEditingDesc(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Escape') setEditingDesc(null)
                }}
                placeholder="Описание задачи (markdown)…"
                style={{ width: '100%', resize: 'vertical', fontFamily: 'monospace', fontSize: 13 }}
              />
            ) : descModal.card.description ? (
              <div className="markdown-wrap">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{descModal.card.description}</ReactMarkdown>
              </div>
            ) : (
              <div style={{ color: 'var(--text-dim, #888)', fontStyle: 'italic' }}>
                Описание не задано. Нажмите ✎ чтобы добавить.
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}

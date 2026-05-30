import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { Board, BoardColumn, RunResult } from '../types'
import { Spinner } from '../components/Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'

interface Props {
  projectId: string
}

const ORDER = ['backlog', 'in_progress', 'review', 'failed']
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

export function BoardTab({ projectId }: Props) {
  const [board, setBoard] = useState<Board | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [newText, setNewText] = useState('')
  const [showArchive, setShowArchive] = useState(false)
  const [archive, setArchive] = useState<string | null>(null)

  // F1: модалка результата карточки
  const [runResult, setRunResult] = useState<RunResult | null>(null)
  const [runResultLoading, setRunResultLoading] = useState(false)
  const [showRunModal, setShowRunModal] = useState(false)

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

  // Polling: 3с пока есть in_progress, 10с в покое; не тикает когда вкладка скрыта
  function schedulePoll(b: Board | null) {
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    const delay = hasInProgress(b) ? POLL_FAST_MS : POLL_SLOW_MS
    pollTimerRef.current = setTimeout(async () => {
      if (document.visibilityState !== 'visible') {
        schedulePoll(b)  // ждём до следующего тика; обновим при visibility change
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
    } catch (e: any) {
      // F1: 409 = проект занят
      if (e.status === 409) {
        setError('⏳ Проект занят (TG или другая карточка) — попробуй позже')
      } else {
        setError(String(e.message || e))
      }
    } finally {
      setBusy(false)
    }
  }

  function addCard() {
    const t = newText.trim()
    if (!t) return
    setNewText('')
    run(api.createTask(projectId, t, 'backlog'))
  }

  function move(card: string, to: string) { run(api.moveTask(projectId, card, to)) }
  function del(card: string) { run(api.deleteTask(projectId, card)) }

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
    try {
      const r = await api.cardRun(projectId, cardId)
      setRunResult(r)
    } catch (e: any) {
      setRunResult({ content: `⚠ Ошибка загрузки: ${e.message || e}`, exists: false })
    } finally {
      setRunResultLoading(false)
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
          const idx = ORDER.indexOf(key)
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

              <div className="board-col-body">
                {col.cards.map(card => (
                  <div
                    className={`board-card${isInProgress ? ' board-card-running' : ''}`}
                    key={card.id}
                  >
                    <div className="board-card-text">
                      {/* F1: иконка на карточке в работе */}
                      {isInProgress && <span className="card-running-icon" title="Выполняется агентом">⚙ </span>}
                      {card.text}
                    </div>
                    <div className="board-card-actions">
                      <button title="← влево" disabled={busy || idx === 0}
                        onClick={() => move(card.id, ORDER[idx - 1])}>←</button>
                      <button title="вправо →" disabled={busy || idx === ORDER.length - 1}
                        onClick={() => move(card.id, ORDER[idx + 1])}>→</button>
                      {/* F1: кнопка результата для review/failed */}
                      {canShowResult && (
                        <button
                          title="Результат выполнения"
                          className="act-result"
                          disabled={busy}
                          onClick={() => showResult(card.id)}
                        >📄</button>
                      )}
                      <button title="✓ в Done (архив)" className="act-done" disabled={busy}
                        onClick={() => move(card.id, 'done')}>✓</button>
                      <button title="удалить" className="act-del" disabled={busy}
                        onClick={() => del(card.id)}>✕</button>
                    </div>
                  </div>
                ))}

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
        <div className="run-modal-overlay" onClick={() => setShowRunModal(false)}>
          <div className="run-modal" onClick={e => e.stopPropagation()}>
            <div className="run-modal-head">
              <span>Результат выполнения</span>
              <button className="run-modal-close" onClick={() => setShowRunModal(false)}>✕</button>
            </div>
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
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

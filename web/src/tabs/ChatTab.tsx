import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { PromptPicker } from '../components/PromptPicker'
import { SkillPicker } from '../components/SkillPicker'
import {
  ChatMessage,
  ChatSSEEvent,
  ChatToolCall,
  HistoryMessage,
  Project,
  RichTool,
  SessionContext,
  SessionInfo,
} from '../types'
import { useProjectActivity } from '../hooks/useProjectActivity'
import { useClickOutside } from '../hooks/useClickOutside'

interface Props {
  project: Project
  onProjectsReload: () => void
  /** Когда вкладка проекта становится видимой (false→true) — проверяем running-статус. */
  isActive?: boolean
}

type ModelKey = 'opus' | 'sonnet' | 'haiku'
const MODEL_OPTIONS: ModelKey[] = ['sonnet', 'opus', 'haiku']

/** Грубая оценка токенов: ~4 символа на токен (общепринятый эвристик для англ/русск). */
function estimateTokens(messages: ChatMessage[]): number {
  let total = 0
  for (const m of messages) {
    total += m.text.length
    for (const t of m.tools) {
      // короткий вес для tool — обычно компактные структуры
      total += JSON.stringify(t).length
    }
  }
  return Math.round(total / 4)
}

function formatTokens(n: number): string {
  if (n < 1000) return `${n}`
  if (n < 10000) return `${(n / 1000).toFixed(1)}K`
  return `${Math.round(n / 1000)}K`
}

/** Форматирует длительность: 0:05, 1:23, 12:45. */
function formatDuration(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${r.toString().padStart(2, '0')}`
}

/** Короткая подсказка для tool — что именно сейчас крутится. */
function toolHint(tool: RichTool): string {
  if (tool.kind === 'bash') {
    const cmd = tool.cmd.trim().split('\n')[0]
    return cmd.length > 50 ? cmd.slice(0, 50) + '…' : cmd
  }
  if (tool.kind === 'edit' || tool.kind === 'write' || tool.kind === 'read') {
    const fname = tool.file.split('/').pop() || tool.file
    return fname
  }
  if (tool.kind === 'search') {
    return tool.pattern.length > 40 ? tool.pattern.slice(0, 40) + '…' : tool.pattern
  }
  return ''
}

interface RunIndicator {
  startedAt: number
  lastEventAt: number
  currentTool: RichTool | null
  source: 'chat' | 'card'
}

interface Attachment {
  id: string
  name: string
  path?: string
  uploading: boolean
  error?: string
}

// Сегментация потокового ответа: на границе text↔tool открывается НОВОЕ ассистент-сообщение.
// Это сохраняет реальный порядок «текст → файл → текст → файл» как видно после reload
// (когда история парсится из SDK-транскрипта по отдельным assistant-блокам).
type StreamChunk =
  | { kind: 'text'; text: string }
  | { kind: 'tool'; tool: ChatToolCall }

function appendChunk(messages: ChatMessage[], chunk: StreamChunk): ChatMessage[] {
  const last = messages[messages.length - 1]
  const lastIsAsstStreaming = !!(last && last.role === 'assistant' && last.streaming)

  if (chunk.kind === 'text') {
    // Продолжаем текущий текстовый сегмент (нет инструментов в нём)
    if (lastIsAsstStreaming && last!.tools.length === 0) {
      return [...messages.slice(0, -1), { ...last!, text: last!.text + chunk.text }]
    }
    // Граница tool→text: закрываем прошлый сегмент, открываем новый текстовый
    const closed = lastIsAsstStreaming
      ? [...messages.slice(0, -1), { ...last!, streaming: false }]
      : messages
    return [...closed, { id: nextId(), role: 'assistant', text: chunk.text, tools: [], streaming: true }]
  }

  // tool
  if (lastIsAsstStreaming && last!.text === '') {
    // Первый или продолжение tool-only сегмента
    return [...messages.slice(0, -1), { ...last!, tools: [...last!.tools, chunk.tool] }]
  }
  // Граница text→tool: новый сегмент с инструментом
  const closed = lastIsAsstStreaming
    ? [...messages.slice(0, -1), { ...last!, streaming: false }]
    : messages
  return [...closed, { id: nextId(), role: 'assistant', text: '', tools: [chunk.tool], streaming: true }]
}

function finalizeStreaming(messages: ChatMessage[], err?: string): ChatMessage[] {
  const last = messages[messages.length - 1]
  if (last && last.role === 'assistant' && last.streaming) {
    const updated: ChatMessage = { ...last, streaming: false }
    if (err) updated.error = err
    return [...messages.slice(0, -1), updated]
  }
  return messages
}

let _msgCounter = 0
function nextId() { return `msg-${++_msgCounter}` }

function makeUserMsg(text: string): ChatMessage {
  return { id: nextId(), role: 'user', text, tools: [], streaming: false }
}

function makeAssistantMsg(): ChatMessage {
  return { id: nextId(), role: 'assistant', text: '', tools: [], streaming: true }
}

/** Parse a single SSE line: "data: {...}" → parsed object or null */
function parseLine(line: string): ChatSSEEvent | null {
  if (!line.startsWith('data: ')) return null
  try {
    return JSON.parse(line.slice(6)) as ChatSSEEvent
  } catch {
    return null
  }
}

/** Read a ReadableStream line-by-line, calling onLine for each line. */
async function readStream(
  body: ReadableStream<Uint8Array>,
  onLine: (line: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    while (true) {
      if (signal.aborted) break
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        if (part.startsWith('data: ') || part.startsWith(':')) onLine(part)
      }
    }
    // flush remaining buffer
    if (buf.startsWith('data: ') || buf.startsWith(':')) onLine(buf)
  } finally {
    reader.releaseLock()
  }
}

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

// ─── ToolBlock: rich terminal-style rendering of a single tool call ───────

function ToolBlock({ tool }: { tool: RichTool }) {
  const [expanded, setExpanded] = useState(false)

  if (tool.kind === 'bash') {
    return (
      <div className="chat-tool-row chat-tool-bash">
        <span className="chat-tool-icon">$</span>
        <div className="chat-tool-bash-body">
          <pre className="chat-tool-cmd">{tool.cmd}</pre>
          {tool.desc && <span className="chat-tool-desc">{tool.desc}</span>}
        </div>
      </div>
    )
  }

  if (tool.kind === 'edit') {
    const hasOldNew = 'old' in tool && 'new' in tool
    const count = 'count' in tool ? tool.count : undefined
    return (
      <div className="chat-tool-row chat-tool-edit">
        <span className="chat-tool-icon">✏</span>
        <div className="chat-tool-edit-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-file">{tool.file}</span>
            {count !== undefined && (
              <span className="chat-tool-desc">{count} правок</span>
            )}
            {'cell_type' in tool && tool.cell_type && (
              <span className="chat-tool-desc">cell: {tool.cell_type}</span>
            )}
            {hasOldNew && (
              <button
                className="chat-tool-expand-btn chat-tool-expand-inline"
                onClick={() => setExpanded(e => !e)}
              >{expanded ? '▲ скрыть' : '▼ diff'}</button>
            )}
          </div>
          {hasOldNew && expanded && (
            <div className="chat-tool-diff">
              {tool.old && (
                <pre className="chat-tool-diff-old">- {tool.old}</pre>
              )}
              {tool.new && (
                <pre className="chat-tool-diff-new">+ {tool.new}</pre>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  if (tool.kind === 'write') {
    return (
      <div className="chat-tool-row chat-tool-write">
        <span className="chat-tool-icon">📝</span>
        <div className="chat-tool-write-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-file">{tool.file}</span>
            {tool.preview && (
              <button
                className="chat-tool-expand-btn chat-tool-expand-inline"
                onClick={() => setExpanded(e => !e)}
              >{expanded ? '▲ скрыть' : '▼ содержимое'}</button>
            )}
          </div>
          {expanded && tool.preview && (
            <pre className="chat-tool-preview">{tool.preview}</pre>
          )}
        </div>
      </div>
    )
  }

  if (tool.kind === 'read') {
    return (
      <div className="chat-tool-row chat-tool-read">
        <span className="chat-tool-icon">📖</span>
        <span className="chat-tool-file">{tool.file}</span>
      </div>
    )
  }

  if (tool.kind === 'search') {
    return (
      <div className="chat-tool-row chat-tool-search">
        <span className="chat-tool-icon">🔍</span>
        <span className="chat-tool-name">{tool.name}</span>
        <span className="chat-tool-pattern">{tool.pattern}</span>
        {tool.path && <span className="chat-tool-desc">{tool.path}</span>}
      </div>
    )
  }

  // other / fallback
  return (
    <div className="chat-tool-row chat-tool-other">
      <span className="chat-tool-icon">⚙</span>
      <span className="chat-tool-name">{tool.name}</span>
      {tool.summary && <span className="chat-tool-input">{tool.summary}</span>}
    </div>
  )
}

// ─── Session Selector ─────────────────────────────────────────────────────

interface SessionSelectorProps {
  projectId: string
  onSessionChange: () => void
  /** Вызывается когда юзер хочет вставить «промт-завершения» в чат-инпут ДО сброса сессии (ops:a01372). */
  onInsertResetPrompt?: (text: string) => void
}

const DEFAULT_RESET_PROMPT =
  "Заканчиваем сессию. Перед тем как уйти:\n" +
  "1. Просмотри список карточек в TASKS.md, отметь выполненные (передвинь в Done через мою команду или скажи мне).\n" +
  "2. Проверь нет ли мусорных временных файлов в cwd (untitled, scratch, .bak) — предложи удалить.\n" +
  "3. Если есть незакоммиченные правки — короткое описание что и зачем (commit-сообщение).\n" +
  "Не пиши код, просто проверь и доложи."

function SessionSelector({ projectId, onSessionChange, onInsertResetPrompt }: SessionSelectorProps) {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dropRef = useRef<HTMLDivElement>(null)
  // Confirm-модалка перед /reset (ops:a01372) — даёт юзеру шанс отправить промт-завершение
  const [confirmReset, setConfirmReset] = useState(false)
  const [resetPromptText, setResetPromptText] = useState(DEFAULT_RESET_PROMPT)

  const loadSessions = useCallback(async () => {
    try {
      const res = await api.sessions(projectId)
      setSessions(res.sessions)
    } catch {
      // non-critical — silently ignore
    }
  }, [projectId])

  useEffect(() => {
    loadSessions()
    setOpen(false)
    setError('')
  }, [projectId, loadSessions])

  // Close dropdown on outside click
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
    } catch (err: any) {
      if (err?.status === 409) {
        setError('проект занят')
      } else {
        setError(err?.message || 'ошибка')
      }
    } finally {
      setBusy(false)
    }
  }

  async function renameSession(s: SessionInfo) {
    const next = window.prompt('Имя сессии (пусто — убрать лейбл):', s.label || '')
    if (next === null) return  // отмена
    try {
      await api.setSessionLabel(projectId, s.session_id, next.trim())
      await loadSessions()
      onSessionChange()  // лейбл активной мог измениться → обновить заголовок
    } catch (err: any) {
      setError(err?.message || 'ошибка переименования')
    }
  }

  function requestReset() {
    setResetPromptText(DEFAULT_RESET_PROMPT)
    setConfirmReset(true)
    setOpen(false)
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
      >
        <span className="session-icon">◉</span>
        <span className="session-label">{activeLabel}</span>
        <span className="session-chevron">{open ? '▲' : '▼'}</span>
      </button>

      {error && <div className="session-error">{error}</div>}

      {open && (
        <div className="session-dropdown">
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
                onClick={(e) => { e.stopPropagation(); renameSession(s) }}
                disabled={busy}
                title="Переименовать сессию"
              >✎</button>
            </div>
          ))}
          {sessions.length === 0 && (
            <div className="session-dropdown-empty">нет сохранённых сессий</div>
          )}
        </div>
      )}

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

// ─── SessionContextPanel ──────────────────────────────────────────────────

interface SessionContextPanelProps {
  projectId: string
  refreshKey: number  // increment to trigger reload
}

function SessionContextPanel({ projectId, refreshKey }: SessionContextPanelProps) {
  const [ctx, setCtx] = useState<SessionContext | null>(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    api.sessionContext(projectId).then(d => {
      setCtx(d)
      setLoading(false)
    }).catch(() => {
      setLoading(false)
    })
  }, [projectId])

  // Reload on mount, project change, or when refreshKey changes
  useEffect(() => {
    load()
  }, [load, refreshKey])

  const totalFiles = (ctx?.read.length ?? 0) + (ctx?.edited.length ?? 0)
  const hasData = totalFiles > 0 || (ctx?.commands.length ?? 0) > 0

  if (!ctx || (!hasData && !loading)) return null

  return (
    <div className="ctx-panel">
      <button
        className="ctx-panel-toggle"
        onClick={() => setOpen(o => !o)}
        title={open ? 'Свернуть контекст сессии' : 'Развернуть контекст сессии'}
      >
        <span className="ctx-panel-icon">📎</span>
        <span className="ctx-panel-label">
          Контекст: {totalFiles} файл{totalFiles === 1 ? '' : totalFiles >= 2 && totalFiles <= 4 ? 'а' : 'ов'}
          {ctx.commands.length > 0 && `, ${ctx.commands.length} команд`}
        </span>
        <span className="ctx-panel-chevron">{open ? '▲' : '▼'}</span>
        <button
          className="ctx-refresh-btn"
          onClick={e => { e.stopPropagation(); load() }}
          title="Обновить контекст"
          disabled={loading}
        >↺</button>
      </button>

      {open && (
        <div className="ctx-panel-body">
          {loading && <div className="ctx-loading">обновление…</div>}

          {ctx.read.length > 0 && (
            <div className="ctx-section">
              <div className="ctx-section-label">📖 Прочитано ({ctx.read.length})</div>
              <div className="ctx-list">
                {ctx.read.map((f, i) => (
                  <div key={i} className="ctx-item">{f}</div>
                ))}
              </div>
            </div>
          )}

          {ctx.edited.length > 0 && (
            <div className="ctx-section">
              <div className="ctx-section-label">✏️ Изменено ({ctx.edited.length})</div>
              <div className="ctx-list">
                {ctx.edited.map((f, i) => (
                  <div key={i} className="ctx-item ctx-item-edited">{f}</div>
                ))}
              </div>
            </div>
          )}

          {ctx.commands.length > 0 && (
            <div className="ctx-section">
              <div className="ctx-section-label">⚙ Команды ({ctx.commands.length})</div>
              <div className="ctx-list">
                {ctx.commands.map((c, i) => (
                  <div key={i} className="ctx-item ctx-item-cmd">{c}</div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── ChatTab ──────────────────────────────────────────────────────────────

export function ChatTab({ project, onProjectsReload, isActive }: Props) {
  const projectId = project.id
  const [messages, setMessages] = useState<ChatMessage[]>([])
  // Реальный размер контекста сессии (prompt-токены последнего хода), из бэкенда.
  // null = ещё не знаем (нет завершённых ходов) → бейдж не показываем.
  const [contextTokens, setContextTokens] = useState<number | null>(null)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  // Bump to trigger SessionContextPanel reload (after run_end)
  const [ctxRefreshKey, setCtxRefreshKey] = useState(0)
  const [changingModel, setChangingModel] = useState(false)
  // Единый индикатор активного прогона (для chat-POST и для card-run из шины).
  const [run, setRun] = useState<RunIndicator | null>(null)
  // Тикающее "сейчас" — обновляется каждую секунду, пока есть активный прогон.
  // Используется для расчёта elapsed/silence в статус-баре.
  const [tick, setTick] = useState<number>(Date.now())

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  // True while a card-run is being streamed into this chat via the activity bus
  const busActiveRef = useRef<boolean>(false)
  // Очередь сообщений: пока агент работает, новые жмут «Отправить» → встают сюда.
  // queueRef — источник истины (для рекурсивного запуска без stale-замыканий),
  // queueLen — только для UI.
  const queueRef = useRef<string[]>([])
  const [queueLen, setQueueLen] = useState<number>(0)
  // Ref на актуальную sendMessage — sendMessage сам себя дозапускает из finally.
  const sendMessageRef = useRef<((text?: string) => Promise<void>) | null>(null)
  // Stable ref so the activity-stream loop always sees the current streaming flag
  const streamingRef = useRef(false)

  // Прикреплённые файлы (до отправки)
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // Панель шаблонов промтов
  const [showPrompts, setShowPrompts] = useState(false)
  // Панель скиллов агента (глобальные + проектные)
  const [showSkills, setShowSkills] = useState(false)

  // Keep streamingRef in sync with the streaming state
  useEffect(() => {
    streamingRef.current = streaming
  }, [streaming])

  // Мгновенный скролл к последнему сообщению — без анимации (раздражает на длинной истории/стриминге)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
  }, [messages])

  // Тик каждую секунду, пока есть активный run — для перерисовки таймера/тишины в статус-баре.
  useEffect(() => {
    if (!run) return
    const id = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [run])

  // Маппинг истории сессии → сообщения ленты
  function histToMessages(items: HistoryMessage[]): ChatMessage[] {
    return items.map((m, i) => ({
      id: `hist-${i}`, role: m.role, text: m.text, tools: m.tools, streaming: false,
    }))
  }

  // Reset + загрузка истории активной сессии при смене проекта
  useEffect(() => {
    let cancelled = false
    abortRef.current?.abort()
    setMessages([])
    setInput('')
    setStreaming(false)
    setError('')
    setRun(null)
    queueRef.current = []
    setQueueLen(0)
    busActiveRef.current = false
    setContextTokens(null)
    setAttachments([])

    // Параллельно: история сессии + проверка активного прогона
    // Если агент работал до рефреша — восстанавливаем busActiveRef и run-статус,
    // чтобы последующие SSE-события text/tool не фильтровались
    Promise.all([
      api.sessionHistory(projectId),
      api.projectRunning(projectId).catch(() => ({ running: false })),
    ]).then(([histRes, runRes]) => {
      if (cancelled) return
      setMessages(histToMessages(histRes.messages))
      setContextTokens(histRes.context_tokens || null)
      if (runRes.running) {
        busActiveRef.current = true
        setRun({ startedAt: Date.now(), lastEventAt: Date.now(), currentTool: null, source: 'card' })
      }
    }).catch(() => { if (!cancelled) setMessages([]) })

    return () => { cancelled = true }
  }, [projectId])

  // Синхронизация run-индикатора с реальным состоянием бэкенда.
  // Зачем периодический поллинг, а не разовая проверка при возврате на вкладку:
  // — api_project_chat и TG-run НЕ публикуют события в шину (только card-run),
  //   поэтому шина может пропустить run_start/run_end и фронт рассинхронизируется
  //   (индикатор висит после реального завершения ИЛИ пропадает на работающем агенте).
  // — Полл /running каждые 5с пока вкладка активна — дешёвый источник истины,
  //   восстанавливает индикатор в обе стороны.
  // НЕ полим если идёт наш собственный chat-stream (streaming=true) — там состояние
  // ведётся через POST-SSE напрямую, перетирать бессмысленно.
  useEffect(() => {
    if (!isActive) return
    let cancelled = false

    async function sync() {
      if (cancelled || streamingRef.current) return
      try {
        const res = await api.projectRunning(projectId)
        if (cancelled) return
        if (res.running) {
          // Бэк работает, а у нас нет индикатора → восстанавливаем
          if (!busActiveRef.current) {
            busActiveRef.current = true
            const now = Date.now()
            setRun(r => r ?? { startedAt: now, lastEventAt: now, currentTool: null, source: 'card' })
          }
        } else {
          // Бэк свободен, а индикатор висит → шина пропустила run_end, гасим
          if (busActiveRef.current) {
            busActiveRef.current = false
            setRun(null)
            setMessages(prev => finalizeStreaming(prev))
          }
        }
      } catch { /* non-critical */ }
    }

    sync()  // первая проверка сразу при активации
    const id = setInterval(sync, 5000)
    return () => { cancelled = true; clearInterval(id) }
  }, [isActive, projectId])

  // Подписка на активность проекта (общий SSE через ProjectActivityProvider).
  // Card-run приходят сюда: render как обычные ассистент-сообщения.
  useProjectActivity(evt => {
    // Пропускаем, если идёт собственный POST-стрим пользователя — события не должны мешать
    if (streamingRef.current) return

    const now = Date.now()

    if (evt.kind === 'run_start') {
      const prefix = evt.source === 'card' ? '🗂 карточка: ' : evt.source === 'tg' ? '📱 TG: ' : ''
      const userMsg = makeUserMsg(prefix + evt.prompt)
      const assistantMsg = makeAssistantMsg()
      busActiveRef.current = true
      setMessages(prev => [...prev, userMsg, assistantMsg])
      setRun({ startedAt: now, lastEventAt: now, currentTool: null, source: 'card' })

    } else if (evt.kind === 'text') {
      if (!busActiveRef.current) return
      setRun(r => r ? { ...r, lastEventAt: now, currentTool: null } : r)
      setMessages(prev => appendChunk(prev, { kind: 'text', text: evt.text }))

    } else if (evt.kind === 'tool') {
      if (!busActiveRef.current) return
      const tool: ChatToolCall = evt.tool
      setRun(r => r ? { ...r, lastEventAt: now, currentTool: tool } : r)
      setMessages(prev => appendChunk(prev, { kind: 'tool', tool }))

    } else if (evt.kind === 'run_end') {
      if (!busActiveRef.current) return
      busActiveRef.current = false
      setRun(null)
      setMessages(prev => finalizeStreaming(prev))
      setCtxRefreshKey(k => k + 1)
    }
  })

  // Сессия переключена → грузим историю новой активной сессии (для «новой» придёт пусто)
  const handleSessionChange = useCallback(() => {
    abortRef.current?.abort()
    setMessages([])
    setStreaming(false)
    setError('')
    setRun(null)
    queueRef.current = []
    setQueueLen(0)
    busActiveRef.current = false
    setContextTokens(null)
    setAttachments([])
    setCtxRefreshKey(k => k + 1)
    api.sessionHistory(projectId)
      .then(res => { setMessages(histToMessages(res.messages)); setContextTokens(res.context_tokens || null) })
      .catch(() => setMessages([]))
  }, [projectId])

  async function uploadFile(file: File): Promise<string> {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`/api/projects/${projectId}/upload`, {
      method: 'POST', credentials: 'include', body: form,
    })
    if (!res.ok) throw new Error(await res.text().catch(() => res.statusText))
    const data = await res.json()
    return data.path as string
  }

  function addFiles(files: FileList | File[]) {
    Array.from(files).forEach(file => {
      const id = `att-${Date.now()}-${Math.random().toString(36).slice(2)}`
      setAttachments(prev => [...prev, { id, name: file.name, uploading: true }])
      uploadFile(file)
        .then(path => setAttachments(prev => prev.map(a => a.id === id ? { ...a, uploading: false, path } : a)))
        .catch(e => setAttachments(prev => prev.map(a => a.id === id ? { ...a, uploading: false, error: String(e?.message || e) } : a)))
    })
  }

  const sendMessage = useCallback(async (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    // файлы доступны только в прямых вызовах (не из очереди — там текст уже содержит пути)
    const readyFiles = overrideText === undefined ? attachments.filter(a => a.path) : []
    const effectiveText = text || (readyFiles.length > 0 ? 'Посмотри прикреплённые файлы.' : '')
    if (!effectiveText) return

    // Стрим активен И это пользовательский вызов (не дозапуск из очереди) → встаём в очередь
    if (streaming && overrideText === undefined) {
      const filePaths = readyFiles.map(a => `прикреплён файл: ${a.path}`)
      const fullText = filePaths.length > 0 ? `${effectiveText}\n\n${filePaths.join('\n')}` : effectiveText
      queueRef.current.push(fullText)
      setQueueLen(queueRef.current.length)
      setInput('')
      setAttachments([])
      return
    }

    const filePaths = readyFiles.map(a => `прикреплён файл: ${a.path}`)
    const fullPrompt = filePaths.length > 0 ? `${effectiveText}\n\n${filePaths.join('\n')}` : effectiveText

    if (overrideText === undefined) { setInput(''); setAttachments([]) }
    setError('')
    setStreaming(true)
    const startTs = Date.now()
    setRun({ startedAt: startTs, lastEventAt: startTs, currentTool: null, source: 'chat' })

    const userMsg = makeUserMsg(fullPrompt)
    const assistantMsg = makeAssistantMsg()

    setMessages(prev => [...prev, userMsg, assistantMsg])

    const ac = new AbortController()
    abortRef.current = ac

    try {
      const res = await fetch(`/api/projects/${projectId}/chat`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: fullPrompt }),
        signal: ac.signal,
      })

      if (!res.ok || !res.body) {
        const errText = await res.text().catch(() => res.statusText)
        throw new Error(errText)
      }

      await readStream(
        res.body,
        (line) => {
          const evt = parseLine(line)
          if (!evt) return

          // Обновляем run-индикатор (тишина/таймер/текущий инструмент)
          const now = Date.now()
          if (evt.type === 'text') {
            setRun(r => r ? { ...r, lastEventAt: now, currentTool: null } : r)
          } else if (evt.type === 'tool') {
            const { type: _t, ...toolFields } = evt as any
            setRun(r => r ? { ...r, lastEventAt: now, currentTool: toolFields as RichTool } : r)
          } else if (evt.type === 'result' || evt.type === 'done' || evt.type === 'error') {
            setRun(null)
          }
          // Реальный размер контекста приходит в result-событии (bot.py)
          if (evt.type === 'result' && typeof (evt as any).context_tokens === 'number' && (evt as any).context_tokens > 0) {
            setContextTokens((evt as any).context_tokens)
          }

          setMessages(prev => {
            switch (evt.type) {
              case 'text':
                return appendChunk(prev, { kind: 'text', text: evt.text })
              case 'tool': {
                const { type: _t, ...toolFields } = evt as any
                return appendChunk(prev, { kind: 'tool', tool: toolFields as ChatToolCall })
              }
              case 'result':
              case 'done':
                return finalizeStreaming(prev)
              case 'error':
                return finalizeStreaming(prev, evt.error)
              case 'rate_limit':
                return prev
              default:
                return prev
            }
          })
        },
        ac.signal,
      )

      // Ensure streaming flag is cleared after stream ends normally
      setMessages(prev => finalizeStreaming(prev))

    } catch (err: any) {
      if (err?.name === 'AbortError') return
      const msg = err?.message || String(err)
      setError(msg)
      setMessages(prev => finalizeStreaming(prev, msg))
    } finally {
      setStreaming(false)
      setRun(null)
      abortRef.current = null
      textareaRef.current?.focus()
      // Refresh context panel after any chat run completes
      setCtxRefreshKey(k => k + 1)
      // Освежаем проекты — git.dirty/unpushed могли измениться от агента
      onProjectsReload()
      // Если в очереди есть сообщения — отправляем следующее (через тик чтобы бэкенд успел снять замок)
      if (queueRef.current.length > 0) {
        const next = queueRef.current.shift()!
        setQueueLen(queueRef.current.length)
        setTimeout(() => { sendMessageRef.current?.(next) }, 150)
      }
    }
  }, [input, projectId, streaming, onProjectsReload])

  // Держим ref на актуальную sendMessage, чтобы дозапуск из finally работал без stale-замыканий
  useEffect(() => { sendMessageRef.current = sendMessage }, [sendMessage])

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData.files)
    if (files.length > 0) {
      e.preventDefault()
      addFiles(files)
    }
  }

  function handlePromptSelect(text: string) {
    setInput(text)
    setShowPrompts(false)
    setTimeout(() => {
      const ta = textareaRef.current
      if (!ta) return
      ta.focus()
      // Выделяем первую переменную [ПЕРЕМЕННАЯ] чтобы сразу вводить
      const match = text.match(/\[[^\]]+\]/)
      if (match && match.index !== undefined) {
        ta.setSelectionRange(match.index, match.index + match[0].length)
      }
    }, 0)
  }

  function handleSkillSelect(text: string) {
    // Скилл вставляется как «используй скилл <name>: » — нужно поставить курсор
    // в конец (после двоеточия), чтобы юзер сразу дописал задачу.
    setInput(text)
    setShowSkills(false)
    setTimeout(() => {
      const ta = textareaRef.current
      if (!ta) return
      ta.focus()
      const pos = text.length
      ta.setSelectionRange(pos, pos)
    }, 0)
  }

  const handleModelChange = useCallback(async (m: ModelKey) => {
    if (m === project.model) return
    setChangingModel(true)
    try {
      await api.setModel(projectId, m)
      onProjectsReload()
    } catch {
      // тихо игнорим — UI вернётся к старой модели после refetch
    } finally {
      setChangingModel(false)
    }
  }, [project.model, projectId, onProjectsReload])

  async function stopStream() {
    // Signal server to interrupt the running agent (best-effort)
    try {
      await api.stopChat(projectId)
    } catch {
      // non-critical — client abort follows regardless
    }
    abortRef.current?.abort()
    setStreaming(false)
    // При остановке очищаем очередь — иначе после прерывания текущего отправятся «забытые» сообщения
    queueRef.current = []
    setQueueLen(0)
  }

  return (
    <div className="chat-wrap">
      {/* Session selector bar + stats + model selector */}
      <div className="chat-session-bar">
        <SessionSelector
          projectId={projectId}
          onSessionChange={handleSessionChange}
          onInsertResetPrompt={(text) => {
            setInput(text)
            setTimeout(() => textareaRef.current?.focus(), 0)
          }}
        />
        {messages.length > 0 && (() => {
          // Честный размер: реальные prompt-токены последнего хода (из бэкенда).
          // Пока их нет (история без usage / до первого ответа) — грубая оценка со знаком ~.
          const real = contextTokens != null && contextTokens > 0
          const tokens = real ? contextTokens! : estimateTokens(messages)
          // Пороги по запросу: 🔴 200k, 🟡 120k. Каждый ход переотправляет весь контекст —
          // чем больше, тем дороже ре-якорь кэша (см. разбор расхода лимита).
          const lvl = tokens >= 200_000 ? 'high' : tokens >= 120_000 ? 'mid' : 'low'
          const lvlHint =
            lvl === 'high' ? ' · контекст раздут — /reset' :
            lvl === 'mid' ? ' · контекст растёт' : ''
          const title = real
            ? `Реальный размер контекста сессии: ${tokens.toLocaleString('ru')} токенов (весь промпт уходит в модель каждый ход). Базовый пол ~11–14K — системный промпт Claude Code + инструменты, остаётся даже после /reset. 🟡 от 120K · 🔴 от 200K.`
            : 'Грубая оценка (4 символа ≈ 1 токен) — точные токены появятся после первого ответа.'
          return (
            <span className={`chat-stats-inline lvl-${lvl}`} title={title}>
              💬 {messages.length} · {real ? '' : '~'}{formatTokens(tokens)}{lvlHint}
            </span>
          )
        })()}
        <div className="chat-model-selector" title="Модель применяется со следующего запроса">
          <span className="chat-model-label">🧠</span>
          <select
            className="chat-model-select"
            value={MODEL_OPTIONS.includes(project.model as ModelKey) ? project.model : 'sonnet'}
            onChange={e => handleModelChange(e.target.value as ModelKey)}
            disabled={changingModel || streaming}
          >
            {MODEL_OPTIONS.map(m => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Session context panel (Feature A) */}
      <SessionContextPanel projectId={projectId} refreshKey={ctxRefreshKey} />

      <div className="chat-feed">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <p>Начни чат с агентом по проекту.<br />Сессия общая с Telegram-топиком.</p>
          </div>
        )}

        {messages.map(msg => {
          // Пустой placeholder-assistant (text=='', tools=[]) НЕ рендерим — статус «работает»
          // показывается богатым status-bar внизу (chat-pulse), дубликат внутри чата лишний.
          const isEmpty = !msg.text && msg.tools.length === 0 && !msg.error
          if (isEmpty && msg.role === 'assistant') return null
          return (
            <div key={msg.id} className={`chat-msg chat-msg-${msg.role}`}>
              {/* Tool calls */}
              {msg.tools.length > 0 && (
                <div className="chat-tools">
                  {msg.tools.map((t, i) => (
                    <ToolBlock key={i} tool={t} />
                  ))}
                </div>
              )}

              {/* Message text */}
              {msg.text && (
                <div className="chat-msg-body markdown-wrap">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.text}</ReactMarkdown>
                </div>
              )}

              {/* Error */}
              {msg.error && (
                <div className="chat-msg-error">⚠ {msg.error}</div>
              )}
            </div>
          )
        })}

        <div ref={bottomRef} />
      </div>

      {/* Global error banner (fetch/auth failures) */}
      {error && !messages.some(m => m.error === error) && (
        <div className="error-state chat-error-banner">⚠ {error}</div>
      )}

      <div
        className={`chat-input-area${dragOver ? ' chat-input-drag-over' : ''}`}
        onDragOver={e => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false) }}
        onDrop={e => { e.preventDefault(); setDragOver(false); addFiles(Array.from(e.dataTransfer.files)) }}
      >
        <input
          ref={fileInputRef} type="file" multiple hidden
          onChange={e => { if (e.target.files) addFiles(e.target.files); e.target.value = '' }}
        />
        {attachments.length > 0 && (
          <div className="chat-attachments">
            {attachments.map(a => (
              <div key={a.id} className={`chat-att-chip${a.error ? ' att-error' : a.uploading ? ' att-uploading' : ''}`}>
                <span className="att-name" title={a.name}>{a.name}</span>
                {a.uploading && <span className="att-spinner">↻</span>}
                {a.error && <span className="att-err-icon" title={a.error}>⚠</span>}
                <button className="att-remove" onClick={() => setAttachments(prev => prev.filter(x => x.id !== a.id))} title="Убрать">✕</button>
              </div>
            ))}
          </div>
        )}
        {dragOver && <div className="chat-drop-hint">📎 Отпустите файлы здесь</div>}
        {run && (() => {
          const elapsedSec = (tick - run.startedAt) / 1000
          const silenceSec = (tick - run.lastEventAt) / 1000
          const lvl = silenceSec > 120 ? 'silence-red' : silenceSec > 30 ? 'silence-yellow' : 'silence-ok'
          const tool = run.currentTool
          let icon = '💭'
          let label: string
          if (tool) {
            icon = '🔧'
            const hint = toolHint(tool)
            label = hint ? `${tool.name} · ${hint}` : tool.name
          } else if (silenceSec < 3 && elapsedSec > 1) {
            icon = '✍'
            label = 'пишет ответ'
          } else {
            label = run.source === 'card' ? 'карточка работает' : 'агент думает'
          }
          // Кнопка «Стоп» показывается всегда когда run !== null:
          // backend `api.stopChat` → client.interrupt() прерывает ЛЮБОЙ source (chat/card/tg).
          // abortRef?.abort() безопасен даже если ref пуст (не-chat run) — fetch только для chat.
          // Раньше гейтилось `source === 'chat'` — кнопка пропадала после переключения вкладок,
          // потому что восстановление через api.projectRunning ставит source='card' (неизвестно
          // было ли это chat или card; ср. ops:13c785).
          const canStop = true
          return (
            <div className={`chat-status-bar ${lvl}`}>
              <span className="chat-status-icon">{icon}</span>
              <span className="chat-status-text">{label}</span>
              <span className="chat-status-time">· {formatDuration(elapsedSec)}</span>
              {silenceSec > 30 && (
                <span className="chat-status-silence">
                  ⚠ тишина {formatDuration(silenceSec)}
                  {silenceSec > 120 && ' · возможно завис'}
                </span>
              )}
              {queueLen > 0 && (
                <span className="chat-status-queue" title={`${queueLen} сообщ. в очереди, отправятся автоматически`}>
                  ⏭ в очереди: {queueLen}
                </span>
              )}
              {canStop && (
                <button className="chat-stop-btn" onClick={stopStream} title="Прервать стрим (очередь очистится)">✕ стоп</button>
              )}
            </div>
          )
        })()}
        {showPrompts && (
          <PromptPicker
            onSelect={handlePromptSelect}
            onClose={() => setShowPrompts(false)}
          />
        )}
        {showSkills && (
          <SkillPicker
            projectId={projectId}
            onSelect={handleSkillSelect}
            onClose={() => setShowSkills(false)}
          />
        )}
        <div className="chat-composer">
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            placeholder={streaming
              ? 'Агент работает — сообщение встанет в очередь, отправится после завершения…'
              : 'Сообщение агенту… (Enter — отправить, Shift+Enter — перенос)'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={3}
          />
          <div className="chat-toolbar">
            <div className="chat-toolbar-tools">
              <button
                className="chat-tool-btn"
                onClick={() => fileInputRef.current?.click()}
                title="Прикрепить файл (или перетащи / Ctrl+V)"
                aria-label="Прикрепить файл"
              >📎</button>
              <button
                className={`chat-tool-btn${showPrompts ? ' active' : ''}`}
                onClick={() => { setShowPrompts(s => !s); setShowSkills(false) }}
                title="Шаблоны промтов"
                aria-label="Шаблоны промтов"
              >📋</button>
              <button
                className={`chat-tool-btn${showSkills ? ' active' : ''}`}
                onClick={() => { setShowSkills(s => !s); setShowPrompts(false) }}
                title="Скиллы агента (глобальные + проекта)"
                aria-label="Скиллы агента"
              >🛠</button>
            </div>
            <button
              className="btn-primary chat-send-btn"
              disabled={!input.trim() && attachments.filter(a => a.path).length === 0}
              onClick={() => sendMessage()}
              title={streaming ? 'Поставить в очередь' : 'Отправить (Enter)'}
            >
              {streaming ? 'В очередь' : 'Отправить ↵'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

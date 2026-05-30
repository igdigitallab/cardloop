import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import {
  ActivityEvent,
  ChatMessage,
  ChatSSEEvent,
  ChatToolCall,
  HistoryMessage,
  RichTool,
  SessionContext,
  SessionInfo,
} from '../types'

interface Props {
  projectId: string
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

/** Parse an activity-stream line: "data: {...}" → ActivityEvent or null.
 *  Lines starting with ":" are heartbeat comments — return null (ignored). */
function parseActivityLine(line: string): ActivityEvent | null {
  if (line.startsWith(':')) return null  // heartbeat comment ": ping"
  if (!line.startsWith('data: ')) return null
  try {
    return JSON.parse(line.slice(6)) as ActivityEvent
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
          <span className="chat-tool-file">{tool.file}</span>
          {count !== undefined && (
            <span className="chat-tool-desc">{count} правок</span>
          )}
          {'cell_type' in tool && tool.cell_type && (
            <span className="chat-tool-desc">cell: {tool.cell_type}</span>
          )}
          {hasOldNew && (
            <button
              className="chat-tool-expand-btn"
              onClick={() => setExpanded(e => !e)}
            >{expanded ? '▲ скрыть' : '▼ diff'}</button>
          )}
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
          <span className="chat-tool-file">{tool.file}</span>
          {tool.preview && (
            <button
              className="chat-tool-expand-btn"
              onClick={() => setExpanded(e => !e)}
            >{expanded ? '▲ скрыть' : '▼ содержимое'}</button>
          )}
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
}

function SessionSelector({ projectId, onSessionChange }: SessionSelectorProps) {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dropRef = useRef<HTMLDivElement>(null)

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
  useEffect(() => {
    if (!open) return
    function handler(e: MouseEvent) {
      if (dropRef.current && !dropRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const activeSession = sessions.find(s => s.is_active)
  const activeLabel = activeSession
    ? activeSession.session_id.slice(0, 8) + '…'
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

  return (
    <div className="session-selector" ref={dropRef}>
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
            onClick={() => switchSession('new')}
            disabled={busy}
          >
            ➕ Новая сессия
          </button>
          {sessions.length > 0 && <div className="session-dropdown-sep" />}
          {sessions.map(s => (
            <button
              key={s.session_id}
              className={`session-dropdown-item${s.is_active ? ' active' : ''}`}
              onClick={() => switchSession('resume', s.session_id)}
              disabled={busy}
            >
              <span className="session-item-check">{s.is_active ? '✓' : ''}</span>
              <span className="session-item-preview">{s.preview}</span>
              <span className="session-item-time">{relTime(s.last_used)}</span>
            </button>
          ))}
          {sessions.length === 0 && (
            <div className="session-dropdown-empty">нет сохранённых сессий</div>
          )}
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

export function ChatTab({ projectId }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  // Bump to trigger SessionContextPanel reload (after run_end)
  const [ctxRefreshKey, setCtxRefreshKey] = useState(0)

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  // Tracks the id of the current card-assistant message being built by the bus
  const busAssistantIdRef = useRef<string | null>(null)
  // Stable ref so the activity-stream loop always sees the current streaming flag
  const streamingRef = useRef(false)

  // Keep streamingRef in sync with the streaming state
  useEffect(() => {
    streamingRef.current = streaming
  }, [streaming])

  // Auto-scroll to bottom when messages update
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

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
    busAssistantIdRef.current = null
    api.sessionHistory(projectId)
      .then(res => { if (!cancelled) setMessages(histToMessages(res.messages)) })
      .catch(() => { if (!cancelled) setMessages([]) })
    return () => { cancelled = true }
  }, [projectId])

  // ─── Activity-stream subscription ──────────────────────────────────────
  // Открываем постоянный SSE-поток к /activity-stream при монтировании / смене проекта.
  // Закрываем при unmount или смене проекта через AbortController.
  // Reconnect через ~2с при обрыве.
  useEffect(() => {
    const ac = new AbortController()
    let active = true

    async function connect() {
      while (active) {
        try {
          const res = await fetch(`/api/projects/${projectId}/activity-stream`, {
            credentials: 'include',
            signal: ac.signal,
          })
          if (!res.ok || !res.body) {
            // Ждём и повторяем при ошибке
            await new Promise(r => setTimeout(r, 2000))
            continue
          }
          await readStream(
            res.body,
            (line) => {
              const evt = parseActivityLine(line)
              if (!evt) return  // heartbeat или невалидный JSON

              // Пропускаем, если панель занята собственным POST-стримом пользователя.
              // Замок гарантирует, что события карточки и набранное сообщение
              // никогда не пересекаются, но флаг — дополнительная страховка.
              if (streamingRef.current) return

              if (evt.kind === 'run_start') {
                // Добавляем user-сообщение с промптом карточки
                const prefix = evt.source === 'card' ? '🗂 карточка: ' : ''
                const userMsg = makeUserMsg(prefix + evt.prompt)
                // Добавляем пустое assistant-сообщение в режиме streaming
                const assistantMsg = makeAssistantMsg()
                busAssistantIdRef.current = assistantMsg.id
                setMessages(prev => [...prev, userMsg, assistantMsg])

              } else if (evt.kind === 'text') {
                const aid = busAssistantIdRef.current
                if (!aid) return
                setMessages(prev => {
                  const msgs = [...prev]
                  const idx = msgs.findIndex(m => m.id === aid)
                  if (idx === -1) return msgs
                  const updated = { ...msgs[idx], text: msgs[idx].text + evt.text }
                  return [...msgs.slice(0, idx), updated, ...msgs.slice(idx + 1)]
                })

              } else if (evt.kind === 'tool') {
                const aid = busAssistantIdRef.current
                if (!aid) return
                const tool: ChatToolCall = evt.tool
                setMessages(prev => {
                  const msgs = [...prev]
                  const idx = msgs.findIndex(m => m.id === aid)
                  if (idx === -1) return msgs
                  const updated = { ...msgs[idx], tools: [...msgs[idx].tools, tool] }
                  return [...msgs.slice(0, idx), updated, ...msgs.slice(idx + 1)]
                })

              } else if (evt.kind === 'run_end') {
                const aid = busAssistantIdRef.current
                busAssistantIdRef.current = null
                if (!aid) return
                setMessages(prev => {
                  const msgs = [...prev]
                  const idx = msgs.findIndex(m => m.id === aid)
                  if (idx === -1) return msgs
                  const updated = { ...msgs[idx], streaming: false }
                  return [...msgs.slice(0, idx), updated, ...msgs.slice(idx + 1)]
                })
                // Refresh context panel after run completes
                setCtxRefreshKey(k => k + 1)
              }
            },
            ac.signal,
          )
        } catch (err: any) {
          if (!active || err?.name === 'AbortError') break
          // Ждём перед переподключением при случайном обрыве
          await new Promise(r => setTimeout(r, 2000))
        }
      }
    }

    connect()

    return () => {
      active = false
      ac.abort()
      // Сбрасываем текущее bus-сообщение при смене проекта / unmount
      busAssistantIdRef.current = null
    }
  }, [projectId])

  // Сессия переключена → грузим историю новой активной сессии (для «новой» придёт пусто)
  const handleSessionChange = useCallback(() => {
    abortRef.current?.abort()
    setMessages([])
    setStreaming(false)
    setError('')
    busAssistantIdRef.current = null
    api.sessionHistory(projectId)
      .then(res => setMessages(histToMessages(res.messages)))
      .catch(() => setMessages([]))
  }, [projectId])

  const sendMessage = useCallback(async () => {
    const text = input.trim()
    if (!text || streaming) return

    setInput('')
    setError('')
    setStreaming(true)

    const userMsg = makeUserMsg(text)
    const assistantMsg = makeAssistantMsg()

    setMessages(prev => [...prev, userMsg, assistantMsg])

    const ac = new AbortController()
    abortRef.current = ac

    try {
      const res = await fetch(`/api/projects/${projectId}/chat`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text }),
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

          setMessages(prev => {
            const msgs = [...prev]
            const last = msgs[msgs.length - 1]
            if (!last || last.id !== assistantMsg.id) return msgs

            switch (evt.type) {
              case 'text':
                return [...msgs.slice(0, -1), { ...last, text: last.text + evt.text }]

              case 'tool': {
                // evt is ChatEventTool which extends RichTool; strip 'type'
                const { type: _t, ...toolFields } = evt as any
                const tool: ChatToolCall = toolFields
                return [...msgs.slice(0, -1), { ...last, tools: [...last.tools, tool] }]
              }

              case 'result':
              case 'done':
                return [...msgs.slice(0, -1), { ...last, streaming: false }]

              case 'error':
                return [...msgs.slice(0, -1), {
                  ...last,
                  streaming: false,
                  error: evt.error,
                }]

              case 'rate_limit':
                return msgs

              default:
                return msgs
            }
          })
        },
        ac.signal,
      )

      // Ensure streaming flag is cleared after stream ends normally
      setMessages(prev => {
        const msgs = [...prev]
        const last = msgs[msgs.length - 1]
        if (last && last.id === assistantMsg.id && last.streaming) {
          return [...msgs.slice(0, -1), { ...last, streaming: false }]
        }
        return msgs
      })

    } catch (err: any) {
      if (err?.name === 'AbortError') return
      const msg = err?.message || String(err)
      setError(msg)
      setMessages(prev => {
        const msgs = [...prev]
        const last = msgs[msgs.length - 1]
        if (last && last.id === assistantMsg.id) {
          return [...msgs.slice(0, -1), { ...last, streaming: false, error: msg }]
        }
        return msgs
      })
    } finally {
      setStreaming(false)
      abortRef.current = null
      textareaRef.current?.focus()
      // Refresh context panel after any chat run completes
      setCtxRefreshKey(k => k + 1)
    }
  }, [input, projectId, streaming])

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  async function stopStream() {
    // Signal server to interrupt the running agent (best-effort)
    try {
      await api.stopChat(projectId)
    } catch {
      // non-critical — client abort follows regardless
    }
    abortRef.current?.abort()
    setStreaming(false)
  }

  return (
    <div className="chat-wrap">
      {/* Session selector bar */}
      <div className="chat-session-bar">
        <SessionSelector projectId={projectId} onSessionChange={handleSessionChange} />
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

        {messages.map(msg => (
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
            {msg.text ? (
              <div className="chat-msg-body markdown-wrap">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.text}</ReactMarkdown>
              </div>
            ) : msg.streaming ? (
              <div className="chat-msg-body chat-thinking">агент думает…</div>
            ) : null}

            {/* Error */}
            {msg.error && (
              <div className="chat-msg-error">⚠ {msg.error}</div>
            )}
          </div>
        ))}

        <div ref={bottomRef} />
      </div>

      {/* Global error banner (fetch/auth failures) */}
      {error && !messages.some(m => m.error === error) && (
        <div className="error-state chat-error-banner">⚠ {error}</div>
      )}

      <div className="chat-input-area">
        {streaming && (
          <div className="chat-status-bar">
            <span className="chat-status-text">агент думает…</span>
            <button className="chat-stop-btn" onClick={stopStream} title="Прервать стрим">✕ стоп</button>
          </div>
        )}
        <div className="chat-input-row">
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            placeholder={streaming ? 'Агент работает…' : 'Сообщение агенту… (Enter — отправить, Shift+Enter — перенос)'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={streaming}
            rows={3}
          />
          <button
            className="btn-primary chat-send-btn"
            disabled={streaming || !input.trim()}
            onClick={sendMessage}
          >
            Отправить
          </button>
        </div>
      </div>
    </div>
  )
}

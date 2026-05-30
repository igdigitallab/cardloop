import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { ChatMessage, ChatSSEEvent, ChatToolCall, SessionInfo, HistoryMessage } from '../types'

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

/** Read a ReadableStream line-by-line, calling onLine for each "data: ..." line. */
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
        if (part.startsWith('data: ')) onLine(part)
      }
    }
    // flush remaining buffer
    if (buf.startsWith('data: ')) onLine(buf)
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

// ─── ChatTab ──────────────────────────────────────────────────────────────

export function ChatTab({ projectId }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

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
    api.sessionHistory(projectId)
      .then(res => { if (!cancelled) setMessages(histToMessages(res.messages)) })
      .catch(() => { if (!cancelled) setMessages([]) })
    return () => { cancelled = true }
  }, [projectId])

  // Сессия переключена → грузим историю новой активной сессии (для «новой» придёт пусто)
  const handleSessionChange = useCallback(() => {
    abortRef.current?.abort()
    setMessages([])
    setStreaming(false)
    setError('')
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
                const tool: ChatToolCall = { name: evt.name, input: evt.input }
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
    }
  }, [input, projectId, streaming])

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  function stopStream() {
    abortRef.current?.abort()
    setStreaming(false)
  }

  return (
    <div className="chat-wrap">
      {/* Session selector bar */}
      <div className="chat-session-bar">
        <SessionSelector projectId={projectId} onSessionChange={handleSessionChange} />
      </div>

      <div className="chat-feed">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <p>Начни чат с агентом по проекту.<br />Сессия общая с Telegram-топиком.</p>
          </div>
        )}

        {messages.map(msg => (
          <div key={msg.id} className={`chat-msg chat-msg-${msg.role}`}>
            <div className="chat-msg-label">
              {msg.role === 'user' ? 'Вы' : 'Агент'}
              {msg.streaming && <span className="chat-streaming-dot" title="агент думает…">●</span>}
            </div>

            {/* Tool calls */}
            {msg.tools.length > 0 && (
              <div className="chat-tools">
                {msg.tools.map((t, i) => (
                  <div key={i} className="chat-tool-row">
                    <span className="chat-tool-icon">⚙</span>
                    <span className="chat-tool-name">{t.name}</span>
                    {t.input && <span className="chat-tool-input">{t.input}</span>}
                  </div>
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

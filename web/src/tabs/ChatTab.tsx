import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ChatMessage, ChatSSEEvent, ChatToolCall } from '../types'

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

  // Reset state when project changes
  useEffect(() => {
    abortRef.current?.abort()
    setMessages([])
    setInput('')
    setStreaming(false)
    setError('')
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
                // informational — не меняем UI
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
      // Mark last assistant msg as errored
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

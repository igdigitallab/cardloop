import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { PromptPicker } from '../components/PromptPicker'
import { SkillPicker } from '../components/SkillPicker'
import { ToolBlock } from '../components/ToolBlock'
import { SessionSelector } from '../components/SessionSelector'
import { SessionContextPanel } from '../components/SessionContextPanel'
import {
  ChatMessage,
  ChatToolCall,
  HistoryMessage,
  Project,
  RichTool,
} from '../types'
import { useProjectActivity } from '../hooks/useProjectActivity'
import { parseSseLine, readSseStream } from '../hooks/useChatStream'
import { MODELS } from '../lib/models'
import { t } from '../i18n'

interface Props {
  project: Project
  onProjectsReload: () => void
  /** When the project tab becomes visible (false→true) — check running status. */
  isActive?: boolean
}

type ModelKey = 'opus' | 'sonnet' | 'haiku'

/** Rough token estimate: ~4 characters per token (common heuristic for English/Russian). */
function estimateTokens(messages: ChatMessage[]): number {
  let total = 0
  for (const m of messages) {
    total += m.text.length
    for (const t of m.tools) {
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

/** Formats duration: 0:05, 1:23, 12:45. */
function formatDuration(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${r.toString().padStart(2, '0')}`
}

/** Short hint for tool — what is currently running. */
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

// Stream response segmentation: at the text↔tool boundary a NEW assistant message is opened.
type StreamChunk =
  | { kind: 'text'; text: string }
  | { kind: 'tool'; tool: ChatToolCall }

function appendChunk(messages: ChatMessage[], chunk: StreamChunk): ChatMessage[] {
  const last = messages[messages.length - 1]
  const lastIsAsstStreaming = !!(last && last.role === 'assistant' && last.streaming)

  if (chunk.kind === 'text') {
    if (lastIsAsstStreaming && last!.tools.length === 0) {
      return [...messages.slice(0, -1), { ...last!, text: last!.text + chunk.text }]
    }
    const closed = lastIsAsstStreaming
      ? [...messages.slice(0, -1), { ...last!, streaming: false }]
      : messages
    return [...closed, { id: nextId(), role: 'assistant', text: chunk.text, tools: [], streaming: true }]
  }

  // tool
  if (lastIsAsstStreaming && last!.text === '') {
    return [...messages.slice(0, -1), { ...last!, tools: [...last!.tools, chunk.tool] }]
  }
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

// ─── ChatTab ──────────────────────────────────────────────────────────────

/** True on touch devices — `pointer: coarse` or `ontouchstart` present. */
const isTouchDevice: boolean =
  typeof window !== 'undefined' &&
  (window.matchMedia?.('(pointer: coarse)').matches || 'ontouchstart' in window)

export function ChatTab({ project, onProjectsReload, isActive }: Props) {
  const projectId = project.id
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [contextTokens, setContextTokens] = useState<number | null>(null)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  const [ctxRefreshKey, setCtxRefreshKey] = useState(0)
  const [changingModel, setChangingModel] = useState(false)
  const [run, setRun] = useState<RunIndicator | null>(null)
  // Ticking "now" — only ticks while there is an active run, to avoid re-rendering the whole tab
  // on every second. The timer is localized: only the status bar reads `tick`.
  const [tick, setTick] = useState<number>(Date.now())

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const busActiveRef = useRef<boolean>(false)
  const queueRef = useRef<string[]>([])
  const [queueLen, setQueueLen] = useState<number>(0)
  const sendMessageRef = useRef<((text?: string) => Promise<void>) | null>(null)
  const streamingRef = useRef(false)

  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [showPrompts, setShowPrompts] = useState(false)
  const [showSkills, setShowSkills] = useState(false)

  useEffect(() => { streamingRef.current = streaming }, [streaming])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
  }, [messages])

  // Tick every second while there is an active run — localised to status bar only.
  useEffect(() => {
    if (!run) return
    const id = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [run])

  function histToMessages(items: HistoryMessage[]): ChatMessage[] {
    return items.map((m, i) => ({
      id: `hist-${i}`, role: m.role, text: m.text, tools: m.tools, streaming: false,
    }))
  }

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

  // Periodic poll of /running while tab is active (restores indicator after bus miss)
  useEffect(() => {
    if (!isActive) return
    let cancelled = false

    async function sync() {
      if (cancelled || streamingRef.current) return
      try {
        const res = await api.projectRunning(projectId)
        if (cancelled) return
        if (res.running) {
          if (!busActiveRef.current) {
            busActiveRef.current = true
            const now = Date.now()
            setRun(r => r ?? { startedAt: now, lastEventAt: now, currentTool: null, source: 'card' })
          }
        } else {
          if (busActiveRef.current) {
            busActiveRef.current = false
            setRun(null)
            setMessages(prev => finalizeStreaming(prev))
          }
        }
      } catch { /* non-critical */ }
    }

    sync()
    const id = setInterval(sync, 5000)
    return () => { cancelled = true; clearInterval(id) }
  }, [isActive, projectId])

  // Subscribe to project activity bus (card/TG runs)
  useProjectActivity(evt => {
    if (streamingRef.current) return

    const now = Date.now()

    if (evt.kind === 'run_start') {
      const prefix = evt.source === 'card' ? '🗂 card: ' : evt.source === 'tg' ? '📱 TG: ' : ''
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
    const readyFiles = overrideText === undefined ? attachments.filter(a => a.path) : []
    const effectiveText = text || (readyFiles.length > 0 ? t['chat.look_at_files'] : '')
    if (!effectiveText) return

    if (streaming && overrideText === undefined) {
      const filePaths = readyFiles.map(a => `attached file: ${a.path}`)
      const fullText = filePaths.length > 0 ? `${effectiveText}\n\n${filePaths.join('\n')}` : effectiveText
      queueRef.current.push(fullText)
      setQueueLen(queueRef.current.length)
      setInput('')
      setAttachments([])
      return
    }

    const filePaths = readyFiles.map(a => `attached file: ${a.path}`)
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

      await readSseStream(
        res.body,
        (line) => {
          const evt = parseSseLine(line)
          if (!evt) return

          const now = Date.now()
          if (evt.type === 'text') {
            setRun(r => r ? { ...r, lastEventAt: now, currentTool: null } : r)
          } else if (evt.type === 'tool') {
            const { type: _t, ...toolFields } = evt as unknown as Record<string, unknown>
            setRun(r => r ? { ...r, lastEventAt: now, currentTool: toolFields as unknown as RichTool } : r)
          } else if (evt.type === 'result' || evt.type === 'done' || evt.type === 'error') {
            setRun(null)
          }
          if (evt.type === 'result') {
            const evtAny = evt as unknown as Record<string, unknown>
            if (typeof evtAny.context_tokens === 'number' && (evtAny.context_tokens as number) > 0) {
              setContextTokens(evtAny.context_tokens as number)
            }
          }

          setMessages(prev => {
            switch (evt.type) {
              case 'text':
                return appendChunk(prev, { kind: 'text', text: evt.text })
              case 'tool': {
                const { type: _t, ...toolFields } = evt as unknown as Record<string, unknown>
                return appendChunk(prev, { kind: 'tool', tool: toolFields as unknown as ChatToolCall })
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

      setMessages(prev => finalizeStreaming(prev))

    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') return
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
      setMessages(prev => finalizeStreaming(prev, msg))
    } finally {
      setStreaming(false)
      setRun(null)
      abortRef.current = null
      textareaRef.current?.focus()
      setCtxRefreshKey(k => k + 1)
      onProjectsReload()
      if (queueRef.current.length > 0) {
        const next = queueRef.current.shift()!
        setQueueLen(queueRef.current.length)
        setTimeout(() => { sendMessageRef.current?.(next) }, 150)
      }
    }
  }, [input, projectId, streaming, onProjectsReload, attachments])

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
      const match = text.match(/\[[^\]]+\]/)
      if (match && match.index !== undefined) {
        ta.setSelectionRange(match.index, match.index + match[0].length)
      }
    }, 0)
  }

  function handleSkillSelect(text: string) {
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
      // silently ignore
    } finally {
      setChangingModel(false)
    }
  }, [project.model, projectId, onProjectsReload])

  async function stopStream() {
    try {
      await api.stopChat(projectId)
    } catch {
      // non-critical
    }
    abortRef.current?.abort()
    setStreaming(false)
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
          const real = contextTokens != null && contextTokens > 0
          const tokens = real ? contextTokens! : estimateTokens(messages)
          const lvl = tokens >= 200_000 ? 'high' : tokens >= 120_000 ? 'mid' : 'low'
          const lvlHint =
            lvl === 'high' ? ' · context bloated — /reset' :
            lvl === 'mid' ? ' · context growing' : ''
          const title = real
            ? `Actual session context size: ${tokens.toLocaleString('en')} tokens (full prompt is sent to the model each turn). Base floor ~11–14K — Claude Code system prompt + tools, remains even after /reset. 🟡 from 120K · 🔴 from 200K.`
            : t['chat.token_count_rough']
          return (
            <span className={`chat-stats-inline lvl-${lvl}`} title={title}>
              💬 {messages.length} · {real ? '' : '~'}{formatTokens(tokens)}{lvlHint}
            </span>
          )
        })()}
        <div className="chat-model-selector" title={t['chat.model_hint']}>
          <span className="chat-model-label">🧠</span>
          <select
            className="chat-model-select"
            value={MODELS.some(m => m.value === project.model) ? project.model : 'sonnet'}
            onChange={e => handleModelChange(e.target.value as ModelKey)}
            disabled={changingModel || streaming}
          >
            {MODELS.map(m => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Session context panel */}
      <SessionContextPanel projectId={projectId} refreshKey={ctxRefreshKey} />

      <div className="chat-feed">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <p>{t['chat.empty_hint']}<br />{t['chat.empty_session_hint']}</p>
          </div>
        )}

        {messages.map(msg => {
          const isEmpty = !msg.text && msg.tools.length === 0 && !msg.error
          if (isEmpty && msg.role === 'assistant') return null
          return (
            <div key={msg.id} className={`chat-msg chat-msg-${msg.role}`}>
              {msg.tools.length > 0 && (
                <div className="chat-tools">
                  {msg.tools.map((t, i) => (
                    <ToolBlock key={i} tool={t} />
                  ))}
                </div>
              )}
              {msg.text && (
                <div className="chat-msg-body markdown-wrap">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.text}</ReactMarkdown>
                </div>
              )}
              {msg.error && (
                <div className="chat-msg-error">⚠ {msg.error}</div>
              )}
            </div>
          )
        })}

        <div ref={bottomRef} />
      </div>

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
                <button className="att-remove" onClick={() => setAttachments(prev => prev.filter(x => x.id !== a.id))} title={t['chat.remove_file']} aria-label={t['chat.remove_file_aria']}>✕</button>
              </div>
            ))}
          </div>
        )}
        {dragOver && <div className="chat-drop-hint">📎 Drop files here</div>}
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
            label = t['chat.status_writing']
          } else {
            label = run.source === 'card' ? t['chat.status_card_running'] : t['chat.status_thinking']
          }
          return (
            <div className={`chat-status-bar ${lvl}`}>
              <span className="chat-status-icon">{icon}</span>
              <span className="chat-status-text">{label}</span>
              <span className="chat-status-time">· {formatDuration(elapsedSec)}</span>
              {silenceSec > 30 && (
                <span className="chat-status-silence">
                  ⚠ silence {formatDuration(silenceSec)}
                  {silenceSec > 120 && ' · possibly hung'}
                </span>
              )}
              {queueLen > 0 && (
                <span className="chat-status-queue" title={`${queueLen} message(s) queued, will send automatically`}>
                  ⏭ queued: {queueLen}
                </span>
              )}
              <button className="chat-stop-btn" onClick={stopStream} title={t['chat.stop_title']} aria-label={t['chat.stop_aria']}>{t['chat.stop_btn']}</button>
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
              ? t['chat.input_placeholder_busy']
              : isTouchDevice ? t['chat.input_placeholder_touch'] : t['chat.input_placeholder']}
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
                title={t['chat.attach_file_title']}
                aria-label={t['chat.attach_file_aria']}
              >📎</button>
              <button
                className={`chat-tool-btn${showPrompts ? ' active' : ''}`}
                onClick={() => { setShowPrompts(s => !s); setShowSkills(false) }}
                title={t['chat.prompts_title']}
                aria-label={t['chat.prompts_aria']}
              >📋</button>
              <button
                className={`chat-tool-btn${showSkills ? ' active' : ''}`}
                onClick={() => { setShowSkills(s => !s); setShowPrompts(false) }}
                title={t['chat.skills_title']}
                aria-label={t['chat.skills_aria']}
              >🛠</button>
            </div>
            <button
              className="btn-primary chat-send-btn"
              disabled={!input.trim() && attachments.filter(a => a.path).length === 0}
              onClick={() => sendMessage()}
              title={streaming ? t['chat.queue_title'] : t['chat.send_title']}
            >
              {streaming ? t['chat.queue'] : t['chat.send']}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

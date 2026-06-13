import React, { memo, useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { PromptPicker } from '../components/PromptPicker'
import { SkillPicker } from '../components/SkillPicker'
import { ToolBlock } from '../components/ToolBlock'
import { OptionPicker, parseOptionsBlock } from '../components/OptionPicker'
import { SessionSelector } from '../components/SessionSelector'
import { SessionContextPanel } from '../components/SessionContextPanel'
import {
  Chat,
  ChatMessage,
  ChatEventResult,
  ChatEventTextDelta,
  ChatToolCall,
  HistoryMessage,
  Project,
  RichTool,
  TurnMetrics,
  ActivityEventSubagent,
} from '../types'
import { useProjectActivity, useSeedCursor } from '../hooks/useProjectActivity'
import { parseSseLine, readSseStream } from '../hooks/useChatStream'
import { MODELS, modelLabel } from '../lib/models'
import { t } from '../i18n'

// ─── Spec-035: Sub-agent lane ─────────────────────────────────────────────────

/** Live state of a single sub-agent spawned during this turn. */
interface SubagentEntry {
  task_id: string
  description: string
  last_tool_name: string | null
  /** 'running' until a notification event arrives. */
  status: 'running' | 'completed' | 'failed'
}

/** Reduce a raw subagent bus/SSE event into a SubagentEntry state update. */
function applySubagentEvent(
  prev: SubagentEntry[],
  evt: ActivityEventSubagent,
): SubagentEntry[] {
  const { task_id, subtype, description, status, last_tool_name } = evt
  const existing = prev.find(e => e.task_id === task_id)
  if (subtype === 'started') {
    if (existing) return prev // idempotent
    return [...prev, {
      task_id,
      description: description ?? '',
      last_tool_name: null,
      status: 'running',
    }]
  }
  if (subtype === 'progress') {
    if (!existing) {
      // progress before started — create entry
      return [...prev, {
        task_id,
        description: description ?? '',
        last_tool_name: last_tool_name ?? null,
        status: 'running',
      }]
    }
    return prev.map(e => e.task_id !== task_id ? e : {
      ...e,
      last_tool_name: last_tool_name ?? e.last_tool_name,
      // update description if provided (progress events carry it)
      description: description ?? e.description,
    })
  }
  if (subtype === 'notification') {
    const terminal: SubagentEntry['status'] = status === 'completed' ? 'completed' : 'failed'
    if (!existing) {
      return [...prev, {
        task_id,
        description: description ?? '',
        last_tool_name: null,
        status: terminal,
      }]
    }
    return prev.map(e => e.task_id !== task_id ? e : { ...e, status: terminal })
  }
  return prev
}

/** Coerce a raw bus/SSE event object into ActivityEventSubagent, handling both shapes:
 *  - {kind:"subagent", ...}  (TG consumer path)
 *  - {type:"subagent", ...}  (chat path via live buffer, no kind field)
 */
function toSubagentEvent(raw: Record<string, unknown>): ActivityEventSubagent | null {
  const isSubagent = raw['kind'] === 'subagent' || raw['type'] === 'subagent'
  if (!isSubagent) return null
  return {
    kind: 'subagent',
    run_id: (raw['run_id'] as string | null) ?? null,
    type: raw['type'] as string | undefined,
    subtype: raw['subtype'] as ActivityEventSubagent['subtype'],
    task_id: (raw['task_id'] as string) ?? '',
    description: (raw['description'] as string | null) ?? null,
    status: (raw['status'] as string | null) ?? null,
    summary: (raw['summary'] as string | null) ?? null,
    last_tool_name: (raw['last_tool_name'] as string | null) ?? null,
    seq: raw['seq'] as number | undefined,
  }
}

interface Props {
  project: Project
  onProjectsReload: () => void
  /** When the project tab becomes visible (false→true) — check running status. */
  isActive?: boolean
}

type ModelKey = 'fable' | 'opus' | 'sonnet' | 'haiku'
type ThinkMode = 'max' | 'default' | 'min'

const THINK_MODES: { value: ThinkMode; labelKey: 'chat.think_mode_max' | 'chat.think_mode_default' | 'chat.think_mode_min' }[] = [
  { value: 'max',     labelKey: 'chat.think_mode_max' },
  { value: 'default', labelKey: 'chat.think_mode_default' },
  { value: 'min',     labelKey: 'chat.think_mode_min' },
]

function thinkModeStorageKey(projectId: string, chatId?: string) {
  // Spec-037: per-chat storage key; falls back to per-project for callers without a chat yet
  return chatId ? `cops.chat.thinkmode.${projectId}:${chatId}` : `cops.chat.thinkmode.${projectId}`
}

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

// ─── Spec-022 / Spec-033: Cost visibility constants ───────────────────────────
// Anthropic default ephemeral prompt-cache TTL = 5 min (not 60; 1-hour TTL is
// opt-in via cache_control.ttl:"1h" and this app does NOT set it).
const CACHE_TTL_MS = 5 * 60 * 1000  // Anthropic default ephemeral prompt-cache TTL = 5 min
const CACHE_WARM_PCT = 70   // ≥70% cache-hit → warm (♨️)
const CACHE_COLD_PCT = 30   // <30% cache-hit → cold (🧊)

/** Format HH:MM from a Date or ms timestamp. */
function fmtHHMM(ts: number): string {
  const d = new Date(ts)
  const h = d.getHours().toString().padStart(2, '0')
  const m = d.getMinutes().toString().padStart(2, '0')
  return `${h}:${m}`
}

/** Format a turn duration from milliseconds. E.g. "38s", "2m 41s". Returns null when ms is null. */
function fmtTurnDuration(ms: number | null | undefined): string | null {
  if (ms == null) return null
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  return r > 0 ? `${m}m ${r}s` : `${m}m`
}

/** Format MM:SS countdown from total seconds remaining. */
function fmtCountdown(totalSec: number): string {
  const s = Math.max(0, Math.floor(totalSec))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m.toString().padStart(2, '0')}:${r.toString().padStart(2, '0')}`
}

/** Format a gap (ms) as human-readable: "2m", "1h 5m", "3h". */
function fmtGap(ms: number): string {
  const totalMin = Math.round(ms / 60000)
  if (totalMin < 60) return `${totalMin}m`
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  return m > 0 ? `${h}h ${m}m` : `${h}h`
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

/**
 * Spec-029 §1: Append an incremental text delta to the in-progress assistant bubble.
 * Creates a new streaming assistant message if none is open yet.
 * Behaviour mirrors appendChunk for the `text` kind — deltas always go to a text-only bubble.
 */
function appendDelta(messages: ChatMessage[], delta: string): ChatMessage[] {
  const last = messages[messages.length - 1]
  const lastIsAsstStreaming = !!(last && last.role === 'assistant' && last.streaming)
  if (lastIsAsstStreaming && last!.tools.length === 0) {
    // Append delta to existing streaming text bubble
    return [...messages.slice(0, -1), { ...last!, text: last!.text + delta }]
  }
  if (lastIsAsstStreaming) {
    // Close the current tool-containing bubble and start a fresh text bubble
    return [
      ...messages.slice(0, -1),
      { ...last!, streaming: false },
      { id: nextId(), role: 'assistant', text: delta, tools: [], streaming: true },
    ]
  }
  // No open streaming bubble — create one
  return [...messages, { id: nextId(), role: 'assistant', text: delta, tools: [], streaming: true }]
}

/**
 * Spec-029 §1: Reconcile finalized text block with any accumulated delta text.
 * When the finalized {type:"text"} block arrives after deltas, the canonical text
 * replaces whatever was accumulated (ensuring exact match, no double-render).
 * Falls back to appendChunk if the last message is not a pure streaming text bubble
 * (e.g. no deltas arrived — first {type:"text"} in a non-delta session).
 */
function reconcileFinalText(messages: ChatMessage[], finalText: string): ChatMessage[] {
  const last = messages[messages.length - 1]
  if (last && last.role === 'assistant' && last.streaming && last.tools.length === 0) {
    // Replace accumulated delta text with the canonical final text — no double-render
    return [...messages.slice(0, -1), { ...last!, text: finalText }]
  }
  // No open text bubble (deltas never arrived, or last bubble has tools) — use normal append
  return appendChunk(messages, { kind: 'text', text: finalText })
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

/** Finalize streaming and attach per-turn metrics from the result event. */
function finalizeStreamingWithMetrics(
  messages: ChatMessage[],
  resultEvt: ChatEventResult,
  nowMs: number,
): ChatMessage[] {
  const last = messages[messages.length - 1]
  if (last && last.role === 'assistant' && last.streaming) {
    const metrics: TurnMetrics | undefined =
      resultEvt.cache_hit_pct != null && resultEvt.prompt_tokens != null
        ? {
            cache_hit_pct: resultEvt.cache_hit_pct ?? 0,
            prompt_tokens: resultEvt.prompt_tokens ?? 0,
            cache_read_tokens: resultEvt.cache_read_tokens ?? 0,
            fresh_tokens: resultEvt.fresh_tokens ?? 0,
            duration_ms: resultEvt.duration_ms ?? null,
            utilization: resultEvt.utilization ?? null,
          }
        : undefined
    const updated: ChatMessage = { ...last, streaming: false, ts: nowMs, metrics }
    return [...messages.slice(0, -1), updated]
  }
  return messages
}

let _msgCounter = 0
function nextId() { return `msg-${++_msgCounter}` }

function makeUserMsg(text: string): ChatMessage {
  return { id: nextId(), role: 'user', text, tools: [], streaming: false, ts: Date.now() }
}

function makeAssistantMsg(): ChatMessage {
  return { id: nextId(), role: 'assistant', text: '', tools: [], streaming: true }
}

// ─── CacheCountdownBadge ─────────────────────────────────────────────────────
// Isolated ticker so the parent ChatTab does NOT re-render on each second tick.

interface CacheCountdownBadgeProps {
  lastTurnEndMs: number | null
  lastCacheHitPct: number | null
  /** Last assistant turn metrics (derived from messages in parent, passed down to avoid re-computing). */
  lastAssistantMetrics: TurnMetrics | undefined
  /** Whether a run is currently active. */
  isRunning: boolean
}

// ─── Spec-038: inline image renderer + full-screen lightbox ──────────────────

/** Full-screen lightbox overlay. Closes on backdrop click, ✕ button, or Esc. */
function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  return (
    <div className="lightbox-overlay" onClick={onClose} role="dialog" aria-modal="true">
      <button
        className="lightbox-close"
        onClick={onClose}
        aria-label="Close image"
      >✕</button>
      {/* Stop propagation so clicking the image itself does not close */}
      <img
        className="lightbox-img"
        src={src}
        alt={alt}
        onClick={(e) => e.stopPropagation()}
      />
    </div>
  )
}

/** Custom img renderer for ReactMarkdown: shows a thumbnail; click opens Lightbox. */
function ChatImage({ src, alt }: React.ImgHTMLAttributes<HTMLImageElement>) {
  const [open, setOpen] = useState(false)
  if (!src) return null
  return (
    <>
      <img
        className="chat-msg-img"
        src={src}
        alt={alt ?? ''}
        loading="lazy"
        onClick={() => setOpen(true)}
      />
      {open && (
        <Lightbox src={src} alt={alt ?? ''} onClose={() => setOpen(false)} />
      )}
    </>
  )
}

const _mdComponents = { img: ChatImage }

const CacheCountdownBadge = memo(function CacheCountdownBadge({
  lastTurnEndMs,
  lastCacheHitPct,
  lastAssistantMetrics,
  isRunning,
}: CacheCountdownBadgeProps) {
  // Own tick state — only this small component re-renders every second.
  const [, setCacheTick] = useState<number>(Date.now())

  useEffect(() => {
    if (lastTurnEndMs === null) return
    const remaining = CACHE_TTL_MS - (Date.now() - lastTurnEndMs)
    if (remaining <= 0) return
    const id = setInterval(() => setCacheTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [lastTurnEndMs])

  let isWarm = false
  let remainingSec = 0
  if (isRunning) {
    isWarm = true
  } else if (lastTurnEndMs !== null) {
    remainingSec = Math.max(0, (CACHE_TTL_MS - (Date.now() - lastTurnEndMs)) / 1000)
    isWarm = remainingSec > 0
  }

  const effectiveCacheHitPct = lastAssistantMetrics?.cache_hit_pct ?? lastCacheHitPct
  if (!isRunning && effectiveCacheHitPct != null && effectiveCacheHitPct < CACHE_COLD_PCT) {
    isWarm = false
  }

  const cacheLabel = isRunning
    ? '♨️ running'
    : isWarm
      ? `♨️ ${fmtCountdown(remainingSec)}`
      : '⚪ cold'
  const cacheTip = isRunning
    ? 'Cache warm — agent is actively running and re-warming the prefix.'
    : isWarm
      ? `Cache warm — estimated ${fmtCountdown(remainingSec)} remaining in the 5-min window since last turn end. Actual warm/cold is confirmed by the measured cache-hit % of the last turn.`
      : 'Cache cold — next turn will re-read the full prompt at full price.'

  return (
    <span style={{
      color: isWarm ? 'var(--color-green, #22c55e)' : 'var(--color-muted, #9ca3af)',
      cursor: 'default',
    }} title={cacheTip}>
      {cacheLabel}
    </span>
  )
})

// ─── RunStatusBar ─────────────────────────────────────────────────────────────
// Isolated ticker so the parent ChatTab does NOT re-render on each second tick
// while a run is active.

interface RunStatusBarProps {
  run: RunIndicator
  serverStartedAt: number | null
  queueLen: number
  onStop: () => void
}

const RunStatusBar = memo(function RunStatusBar({
  run,
  serverStartedAt,
  queueLen,
  onStop,
}: RunStatusBarProps) {
  // Own tick state — only this small component re-renders every second.
  const [tick, setTick] = useState<number>(Date.now())

  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const timerBase = serverStartedAt ?? run.startedAt
  const elapsedSec = (tick - timerBase) / 1000
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
      <button className="chat-stop-btn" onClick={onStop} title={t['chat.stop_title']} aria-label={t['chat.stop_aria']}>{t['chat.stop_btn']}</button>
    </div>
  )
})

// ─── ChatTab ──────────────────────────────────────────────────────────────

/** True on touch devices — `pointer: coarse` or `ontouchstart` present. */
const isTouchDevice: boolean =
  typeof window !== 'undefined' &&
  (window.matchMedia?.('(pointer: coarse)').matches || 'ontouchstart' in window)

export function ChatTab({ project, onProjectsReload, isActive }: Props) {
  const projectId = project.id

  // ─── Spec-037: multi-chat tabs ────────────────────────────────────────────
  const [chats, setChats] = useState<Chat[]>([])
  const [activeChatId, setActiveChatId] = useState<string | null>(null)
  // Rename-in-place: null when not renaming, chat id when editing
  const [renamingChatId, setRenamingChatId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')

  // Load chats on project change
  useEffect(() => {
    let cancelled = false
    api.chats(projectId).then(res => {
      if (cancelled) return
      setChats(res.chats)
      setActiveChatId(res.active)
    }).catch(() => { /* non-critical — chat tabs unavailable */ })
    return () => { cancelled = true }
  }, [projectId])

  // The effective chat id: activeChatId from server (null until loaded = render nothing special)
  const effectiveChatId = activeChatId ?? ''

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [contextTokens, setContextTokens] = useState<number | null>(null)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  const [ctxRefreshKey, setCtxRefreshKey] = useState(0)
  const [changingModel, setChangingModel] = useState(false)
  // Thinking mode selector — persisted per-chat in localStorage; default = "default"
  // Spec-037: key is <projectId>:<chatId> so each chat has its own setting.
  const [thinkMode, setThinkMode] = useState<ThinkMode>(() => {
    try {
      const stored = localStorage.getItem(thinkModeStorageKey(projectId, effectiveChatId || undefined))
      if (stored === 'max' || stored === 'default' || stored === 'min') return stored
    } catch { /* localStorage unavailable */ }
    return 'default'
  })
  const [run, setRun] = useState<RunIndicator | null>(null)
  // Spec-035: server-authoritative turn start timestamp (epoch ms).
  // Set from /live started_at; null when not available (falls back to run.startedAt).
  const [serverStartedAt, setServerStartedAt] = useState<number | null>(null)
  // Spec-035: sub-agent lane — live state of spawned sub-agents in the current turn.
  const [subagents, setSubagents] = useState<SubagentEntry[]>([])
  // Ref for deduplication: "task_id:subtype" keys seen via the POST stream, so bus
  // does not double-render the same subagent event on the originating tab.
  const seenSubagentKeysRef = useRef<Set<string>>(new Set())
  // tick and cacheTick state removed — now owned by RunStatusBar and CacheCountdownBadge
  // child components to prevent the message list from re-rendering every second.

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const busActiveRef = useRef<boolean>(false)
  // Server-backed message queue: replaces the old client-only queueRef.
  // Survives page reload via GET /api/projects/{id}/chat/queue on mount.
  interface QueueItem { id: string; text: string; created_at: number }
  const [queueItems, setQueueItems] = useState<QueueItem[]>([])
  const [queueEditId, setQueueEditId] = useState<string | null>(null)
  const [queueEditText, setQueueEditText] = useState<string>('')
  const sendMessageRef = useRef<((text?: string) => Promise<void>) | null>(null)
  const streamingRef = useRef(false)

  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [showPrompts, setShowPrompts] = useState(false)
  const [showSkills, setShowSkills] = useState(false)
  const [showDefer, setShowDefer] = useState(false)
  const [deferMode, setDeferMode] = useState<'time' | 'reset'>('time')
  const [deferDatetime, setDeferDatetime] = useState('')
  const [deferSubmitting, setDeferSubmitting] = useState(false)
  const [deferToast, setDeferToast] = useState<string | null>(null)
  // Spec-021: context rotation UI state
  const [rotateToast, setRotateToast] = useState<string | null>(null)
  const [rotating, setRotating] = useState(false)
  // Context early-warning banner state.
  // contextWarnFromBackend: set true when the backend sends context_warn=true on a result event.
  // warnDismissedAtTokens: the token count when the user dismissed the banner (null = not dismissed).
  //   The banner re-appears if tokens climb into the escalation zone (≥175K) even after a dismiss.
  const [contextWarnFromBackend, setContextWarnFromBackend] = useState(false)
  const [warnDismissedAtTokens, setWarnDismissedAtTokens] = useState<number | null>(null)
  // Context-token value at the END of the previous completed turn — source for the growth delta.
  // null until a second turn arrives (no delta shown on the first turn).
  const [prevContextTokens, setPrevContextTokens] = useState<number | null>(null)
  // Spec-022/033: cache freshness countdown — unix ms when the last turn completed (null = never)
  const [lastTurnEndMs, setLastTurnEndMs] = useState<number | null>(null)
  // Spec-033: last known cache-hit % seeded from session history on reload (null = no data yet)
  const [lastCacheHitPct, setLastCacheHitPct] = useState<number | null>(null)

  // Spec-035 L2: seed the SSE reconnect cursor after /live hydration
  const seedCursor = useSeedCursor()

  // Re-load thinkMode from localStorage when projectId or activeChatId changes (per-chat key)
  useEffect(() => {
    try {
      const stored = localStorage.getItem(thinkModeStorageKey(projectId, effectiveChatId || undefined))
      if (stored === 'max' || stored === 'default' || stored === 'min') {
        setThinkMode(stored)
        return
      }
    } catch { /* localStorage unavailable */ }
    setThinkMode('default')
  }, [projectId, effectiveChatId])

  // Persist thinkMode to localStorage whenever it changes (per-chat key)
  const handleThinkModeChange = useCallback((mode: ThinkMode) => {
    setThinkMode(mode)
    try { localStorage.setItem(thinkModeStorageKey(projectId, effectiveChatId || undefined), mode) } catch { /* ignore */ }
  }, [projectId, effectiveChatId])

  useEffect(() => { streamingRef.current = streaming }, [streaming])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
  }, [messages])

  // D-05: Adjust chat-wrap height when the virtual keyboard appears on mobile.
  // Phase H (Chrome compression fix): use vv.height relative to vv itself, not
  // window.innerHeight. In standalone/installed PWA mode, window.innerHeight equals
  // the full screen height while vv.height already accounts for the keyboard.
  // We only shrink when the keyboard is unambiguously open (vv.height dropped by
  // more than 150px vs. the baseline captured at mount) to avoid false triggers
  // from address-bar show/hide in regular browser mode.
  useEffect(() => {
    if (!isTouchDevice) return
    const vv = window.visualViewport
    if (!vv) return
    // Baseline: the full available height at mount (before any keyboard)
    const baselineHeight = vv.height
    function onViewportResize() {
      const chatWrap = textareaRef.current?.closest('.chat-wrap') as HTMLElement | null
      if (!chatWrap) return
      const reduction = baselineHeight - vv!.height
      // Only shrink when the keyboard is clearly open (>150px reduction)
      chatWrap.style.height = reduction > 150
        ? `${vv!.height}px`
        : ''
    }
    vv.addEventListener('resize', onViewportResize)
    return () => vv.removeEventListener('resize', onViewportResize)
  }, [])

  // Tick timers removed from ChatTab — now owned by RunStatusBar and CacheCountdownBadge
  // child components. This prevents the message list from re-rendering every second.

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
    setServerStartedAt(null)
    setSubagents([])
    seenSubagentKeysRef.current = new Set()
    setQueueItems([])
    setQueueEditId(null)
    setQueueEditText('')
    busActiveRef.current = false
    setContextTokens(null)
    setPrevContextTokens(null)
    setAttachments([])
    setContextWarnFromBackend(false)
    setWarnDismissedAtTokens(null)
    setLastTurnEndMs(null)
    setLastCacheHitPct(null)

    Promise.all([
      api.sessionHistory(projectId),
      api.chatQueue(projectId).catch(() => ({ items: [] as Array<{ id: string; text: string; created_at: number }> })),
      // Spec-035 L3: /live replaces /running — returns running state + turn history + started_at
      api.projectLive(projectId).catch(() => ({ running: false, turn_id: null, started_at: null, model: null, cost_usd: null, cursor: 0, events: [] as Array<Record<string, unknown>> })),
    ]).then(([histRes, queueRes, liveRes]) => {
      if (cancelled) return
      setQueueItems(queueRes.items)
      setContextTokens(histRes.context_tokens || null)
      // Spec-033: seed cache freshness anchor from the persisted transcript data
      if (histRes.last_turn_at != null) setLastTurnEndMs(histRes.last_turn_at)
      if (histRes.last_cache_hit_pct != null) setLastCacheHitPct(histRes.last_cache_hit_pct)

      if (liveRes.running && liveRes.events.length > 0) {
        // ── Spec-035 L4: hydrate transcript from live buffer ──────────────────
        // Replay buffered events on top of session history to reconstruct the
        // in-flight turn. History is appended first; then the live events play.
        const histMsgs = histToMessages(histRes.messages)
        // Open a streaming assistant message for the ongoing turn
        const liveUserMsg = makeUserMsg('…')
        const liveAssistantMsg = makeAssistantMsg()
        let liveMsgs: ChatMessage[] = [...histMsgs, liveUserMsg, liveAssistantMsg]
        const liveSubagents: SubagentEntry[] = []
        for (const ev of liveRes.events) {
          const etype = ev['type'] as string | undefined
          if (etype === 'text') {
            liveMsgs = reconcileFinalText(liveMsgs, ev['text'] as string ?? '')
          } else if (etype === 'text_delta') {
            liveMsgs = appendDelta(liveMsgs, ev['text'] as string ?? '')
          } else if (etype === 'tool') {
            const { type: _t, seq: _s, ...toolFields } = ev
            liveMsgs = appendChunk(liveMsgs, { kind: 'tool', tool: toolFields as unknown as ChatToolCall })
          } else if (etype === 'subagent') {
            const sEvt = toSubagentEvent(ev)
            if (sEvt) {
              const updated = applySubagentEvent(liveSubagents, sEvt)
              liveSubagents.length = 0
              liveSubagents.push(...updated)
            }
          }
          // result/error/done would only appear if the turn already finished —
          // server sets running=true only for ongoing turns, so these are absent.
        }
        setMessages(liveMsgs)
        setSubagents([...liveSubagents])
        // Spec-035: server-authoritative timer — convert epoch seconds to ms
        const startMs = liveRes.started_at != null ? liveRes.started_at * 1000 : Date.now()
        setServerStartedAt(startMs)
        busActiveRef.current = true
        const now = Date.now()
        setRun({ startedAt: startMs, lastEventAt: now, currentTool: null, source: 'card' })
        // Seed the SSE cursor so the activity-stream subscription starts from where
        // the snapshot left off — no gap, no duplicates.
        seedCursor(liveRes.cursor)
      } else if (liveRes.running) {
        // Running but no buffered events yet (turn just started)
        const startMs = liveRes.started_at != null ? liveRes.started_at * 1000 : Date.now()
        setMessages(histToMessages(histRes.messages))
        setServerStartedAt(startMs)
        busActiveRef.current = true
        setRun({ startedAt: startMs, lastEventAt: Date.now(), currentTool: null, source: 'card' })
        seedCursor(liveRes.cursor)
      } else {
        setMessages(histToMessages(histRes.messages))
      }
    }).catch(() => { if (!cancelled) setMessages([]) })

    return () => { cancelled = true }
  // Spec-037: re-hydrate when the active chat changes (activeChatId drives all chat state)
  }, [projectId, effectiveChatId, seedCursor])

  // Periodic poll of /live while tab is active (restores indicator after bus miss).
  // Spec-035: uses /live (not /running) so we get started_at for the server-authoritative timer.
  useEffect(() => {
    if (!isActive) return
    let cancelled = false

    async function sync() {
      if (cancelled || streamingRef.current) return
      try {
        const res = await api.projectLive(projectId)
        if (cancelled) return
        if (res.running) {
          if (!busActiveRef.current) {
            busActiveRef.current = true
            // Spec-035: use server started_at to avoid re-stamping the timer on each poll
            const startMs = res.started_at != null ? res.started_at * 1000 : Date.now()
            setServerStartedAt(prev => prev ?? startMs)
            setRun(r => r ?? { startedAt: startMs, lastEventAt: Date.now(), currentTool: null, source: 'card' })
          }
        } else {
          if (busActiveRef.current) {
            busActiveRef.current = false
            setRun(null)
            setServerStartedAt(null)
            setSubagents([])
            seenSubagentKeysRef.current = new Set()
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

    } else if (evt.kind === 'subagent') {
      // Spec-035: sub-agent lane — process bus subagent events when not streaming on this tab.
      if (!busActiveRef.current) return
      const sEvt = evt as ActivityEventSubagent
      // Dedupe: skip if this (task_id, subtype) combination was already processed via POST stream.
      const dedupeKey = `${sEvt.task_id}:${sEvt.subtype}`
      if (seenSubagentKeysRef.current.has(dedupeKey)) return
      seenSubagentKeysRef.current.add(dedupeKey)
      setSubagents(prev => applySubagentEvent(prev, sEvt))

    } else if (evt.kind === 'run_end') {
      if (!busActiveRef.current) return
      busActiveRef.current = false
      setRun(null)
      setServerStartedAt(null)
      setSubagents([])
      seenSubagentKeysRef.current = new Set()
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
    setQueueItems([])
    setQueueEditId(null)
    busActiveRef.current = false
    setContextTokens(null)
    setPrevContextTokens(null)
    setAttachments([])
    setContextWarnFromBackend(false)
    setWarnDismissedAtTokens(null)
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
      setInput('')
      setAttachments([])
      // Enqueue server-side so the message survives a page reload.
      api.chatQueueAdd(projectId, fullText)
        .then(res => setQueueItems(prev => [...prev, res.item]))
        .catch(() => {/* queue full or network — silently drop */})
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
        // Spec-037: pass active chat_id so the backend writes session_id to the right chat entry
        body: JSON.stringify({ prompt: fullPrompt, think_mode: thinkMode, ...(effectiveChatId ? { chat_id: effectiveChatId } : {}) }),
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
          if (evt.type === 'text_delta' || evt.type === 'text') {
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
              // Snapshot the prior value before overwriting — feeds the growth delta badge.
              setContextTokens(prev => { setPrevContextTokens(prev); return evtAny.context_tokens as number })
            }
            // Thread context_warn from backend: if true, mark the banner as active and clear any
            // previous dismiss (a fresh backend signal means the operator should see it again).
            if ((evtAny as Record<string, unknown>).context_warn === true) {
              setContextWarnFromBackend(true)
              setWarnDismissedAtTokens(null)
            } else {
              setContextWarnFromBackend(false)
            }
            // Spec-022: reset cache freshness countdown on every completed turn
            setLastTurnEndMs(now)
          }
          // Spec-021: rotation event — session was cleared, reset context counter
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const evtRaw = evt as unknown as Record<string, any>
          if (evtRaw.type === 'rotation') {
            setContextTokens(0)
            setPrevContextTokens(null)
            const msg = typeof evtRaw.message === 'string' ? evtRaw.message : 'Session rotated'
            setRotateToast(msg)
            setTimeout(() => setRotateToast(null), 5000)
          }

          setMessages(prev => {
            switch (evt.type) {
              case 'text_delta':
                // Spec-029 §1: accumulate streaming delta into the in-progress bubble.
                // The finalized {type:"text"} block below is still the source of truth —
                // reconcileFinalText will overwrite with the canonical text on arrival.
                return appendDelta(prev, (evt as unknown as ChatEventTextDelta).text)
              case 'text':
                // Spec-029 §1: replace any accumulated delta text with the canonical final text.
                // Falls back to appendChunk when no delta was accumulated (non-streaming sessions).
                return reconcileFinalText(prev, evt.text)
              case 'tool': {
                const { type: _t, ...toolFields } = evt as unknown as Record<string, unknown>
                return appendChunk(prev, { kind: 'tool', tool: toolFields as unknown as ChatToolCall })
              }
              case 'result':
                // Spec-022: finalize with metrics from result event
                return finalizeStreamingWithMetrics(prev, evt as unknown as ChatEventResult, now)
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
      // Drain server-side queue: pop the first item and send it.
      setQueueItems(prev => {
        if (prev.length === 0) return prev
        const [first, ...rest] = prev
        // Delete from server (fire-and-forget — UI already updated optimistically)
        api.chatQueueDelete(projectId, first.id).catch(() => {/* non-critical */})
        setTimeout(() => { sendMessageRef.current?.(first.text) }, 150)
        return rest
      })
    }
  }, [input, projectId, streaming, onProjectsReload, attachments, thinkMode])

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
    // Clear server-side queue entries (fire-and-forget per item)
    setQueueItems(prev => {
      prev.forEach(item => api.chatQueueDelete(projectId, item.id).catch(() => {}))
      return []
    })
    setQueueEditId(null)
  }

  // Shared rotate handler — called from both the health-row button and the context warning banner.
  async function handleRotate() {
    setRotating(true)
    try {
      const res = await fetch(`/api/projects/${projectId}/rotate`, {
        method: 'POST', credentials: 'include',
      })
      const data = await res.json() as Record<string, unknown>
      if (!res.ok) {
        setRotateToast(`Rotate failed: ${data.error ?? res.statusText}`)
      } else if (data.rotated) {
        setContextTokens(0)
        setPrevContextTokens(null)
        setContextWarnFromBackend(false)
        setWarnDismissedAtTokens(null)
        setRotateToast('Session rotated — handoff saved, fresh start')
      } else {
        setRotateToast(`Not rotated: ${data.reason ?? 'unknown reason'}`)
      }
    } catch (e: unknown) {
      setRotateToast(e instanceof Error ? e.message : String(e))
    } finally {
      setRotating(false)
      setTimeout(() => setRotateToast(null), 5000)
    }
  }

  // ─── Spec-037: chat tab handlers ─────────────────────────────────────────

  async function handleSwitchChat(chatId: string) {
    if (chatId === activeChatId || streaming) return
    try {
      const res = await api.patchChat(projectId, chatId, { active: true })
      setActiveChatId(res.active)
      setChats(prev => prev.map(c => c.id === res.chat.id ? res.chat : c))
    } catch { /* non-critical */ }
  }

  async function handleCreateChat() {
    try {
      const newChat = await api.createChat(projectId)
      setChats(prev => [...prev, newChat])
      // Switch to newly created chat
      const res = await api.patchChat(projectId, newChat.id, { active: true })
      setActiveChatId(res.active)
    } catch { /* non-critical */ }
  }

  async function handleDeleteChat(chatId: string) {
    if (chats.length <= 1) return
    try {
      const res = await api.deleteChat(projectId, chatId)
      setChats(prev => prev.filter(c => c.id !== chatId))
      setActiveChatId(res.active)
    } catch { /* non-critical */ }
  }

  async function handleRenameChat(chatId: string, newName: string) {
    const name = newName.trim()
    if (!name) return
    try {
      const res = await api.patchChat(projectId, chatId, { name })
      setChats(prev => prev.map(c => c.id === chatId ? res.chat : c))
    } catch { /* non-critical */ }
    setRenamingChatId(null)
  }

  return (
    <div className="chat-wrap">
      {/* Spec-037: chat tabs strip — one tab per chat, + to create, dbl-click to rename */}
      {chats.length > 0 && (
        <div className="chat-named-tabs-strip">
          {chats.map(chat => {
            const isActive = chat.id === activeChatId
            const isRenaming = renamingChatId === chat.id
            return (
              <div
                key={chat.id}
                className={`chat-named-tab${isActive ? ' active' : ''}`}
                onClick={() => { if (!isRenaming) handleSwitchChat(chat.id) }}
                onDoubleClick={e => {
                  e.stopPropagation()
                  setRenamingChatId(chat.id)
                  setRenameValue(chat.name)
                }}
                title={chat.name}
              >
                {isRenaming ? (
                  <form
                    style={{ display: 'flex', alignItems: 'center', gap: 3 }}
                    onSubmit={e => { e.preventDefault(); handleRenameChat(chat.id, renameValue) }}
                    onClick={e => e.stopPropagation()}
                  >
                    <input
                      autoFocus
                      style={{
                        fontSize: 11, padding: '1px 4px', width: 90,
                        background: 'var(--bg, #111827)', color: 'var(--text, #f9fafb)',
                        border: '1px solid var(--color-primary, #6366f1)', borderRadius: 3,
                      }}
                      value={renameValue}
                      onChange={e => setRenameValue(e.target.value)}
                      placeholder={t['chat.tabs_rename_placeholder']}
                      onKeyDown={e => { if (e.key === 'Escape') setRenamingChatId(null) }}
                    />
                    <button
                      type="submit"
                      style={{ fontSize: 10, padding: '1px 4px', cursor: 'pointer',
                        background: 'var(--color-primary, #6366f1)', color: '#fff',
                        border: 'none', borderRadius: 3 }}
                    >{t['chat.tabs_rename_confirm']}</button>
                  </form>
                ) : (
                  <span className="chat-named-tab-label">{chat.name}</span>
                )}
                {/* Close button only on the active tab — keeps non-active tabs safe to tap on touch */}
                {!isRenaming && isActive && (
                  <button
                    className="chat-named-tab-close"
                    disabled={chats.length <= 1}
                    title={chats.length <= 1 ? t['chat.tabs_close_last'] : t['chat.tabs_close_aria']}
                    aria-label={t['chat.tabs_close_aria']}
                    onClick={e => {
                      e.stopPropagation()
                      if (chats.length > 1) handleDeleteChat(chat.id)
                    }}
                  >×</button>
                )}
              </div>
            )
          })}
          <button
            className="chat-named-tab-new"
            title={t['chat.tabs_new']}
            aria-label={t['chat.tabs_new_aria']}
            onClick={handleCreateChat}
          >+</button>
        </div>
      )}
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
        {/* Session health row — always visible when there are messages */}
        {messages.length > 0 && (() => {
          const real = contextTokens != null && contextTokens > 0
          const tokens = real ? contextTokens! : estimateTokens(messages)

          // Token color scale: yellow at 120K, red at 200K (consistent with progress bar)
          const tokenColor =
            tokens >= 200_000 ? 'var(--color-red, #ef4444)' :
            tokens >= 120_000 ? 'var(--color-yellow, #eab308)' :
            'var(--color-muted, #9ca3af)'

          // Progress bar fill fraction (0..1), capped at 1
          const fillFrac = Math.min(tokens / 200_000, 1)
          const barColor =
            fillFrac >= 1 ? 'var(--color-red, #ef4444)' :
            fillFrac >= 0.6 ? 'var(--color-yellow, #eab308)' :
            'var(--color-green, #22c55e)'

          // Spec-033: cache warm/cold indicator — rendered via CacheCountdownBadge child
          // component so the countdown ticker does not re-render the entire ChatTab (and
          // its message list) every second.
          const lastAssistantMetrics = [...messages].reverse().find(
            m => m.role === 'assistant' && m.metrics != null
          )?.metrics

          // Wrap & reset button prominence depends on token level
          const isProminent = tokens >= 120_000
          const wrapBtnStyle: React.CSSProperties = isProminent
            ? {
                fontSize: 11, padding: '1px 6px', cursor: rotating ? 'wait' : 'pointer',
                background: 'var(--bg-card)', border: `1px solid ${tokens >= 200_000 ? 'var(--color-red, #ef4444)' : 'var(--color-yellow, #eab308)'}`,
                borderRadius: 4,
                color: tokens >= 200_000 ? 'var(--color-red, #ef4444)' : 'var(--color-yellow, #eab308)',
                fontWeight: 600,
              }
            : {
                fontSize: 11, padding: '1px 6px', cursor: rotating ? 'wait' : 'pointer',
                background: 'transparent', border: '1px solid var(--border)',
                borderRadius: 4, color: 'var(--color-muted, #9ca3af)',
              }

          // Last turn utilization (null when not present)
          const utilization = lastAssistantMetrics?.utilization ?? null

          const tokenTip = real
            ? `Actual session context size: ${tokens.toLocaleString('en')} tokens (full prompt is sent to the model each turn). There is an incompressible base floor — Claude Code system prompt + tools + CLAUDE.md + memory — that remains even after /reset. Yellow from 120K · Red from 200K.`
            : t['chat.token_count_rough']

          // Context growth since the previous completed turn — only on real (server) numbers.
          // null when no prior turn exists (first turn / right after a reset).
          const deltaTokens = real && prevContextTokens != null ? tokens - prevContextTokens : null
          const deltaLabel = deltaTokens != null && deltaTokens !== 0
            ? `${deltaTokens > 0 ? '+' : '−'}${formatTokens(Math.abs(deltaTokens))}`
            : null

          return (
            <span className="chat-session-health" style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              fontSize: 12, whiteSpace: 'nowrap', flexShrink: 0,
            }}>
              {/* Message count */}
              <span style={{ color: 'var(--color-muted, #9ca3af)' }}>
                💬 {messages.length}
              </span>
              {/* Progress bar */}
              <span
                title={tokenTip}
                style={{
                  display: 'inline-block', width: 40, height: 5,
                  background: 'var(--border, #374151)', borderRadius: 3,
                  overflow: 'hidden', cursor: 'default', flexShrink: 0,
                }}
              >
                <span style={{
                  display: 'block', height: '100%',
                  width: `${Math.round(fillFrac * 100)}%`,
                  background: barColor,
                  borderRadius: 3,
                  transition: 'width 0.3s, background 0.3s',
                }} />
              </span>
              {/* Token count */}
              <span style={{ color: tokenColor, cursor: 'default' }} title={tokenTip}>
                {real ? '' : '~'}{formatTokens(tokens)}
              </span>
              {/* Growth delta since the previous turn */}
              {deltaLabel != null && (
                <span
                  className="chat-session-delta"
                  style={{ color: 'var(--color-muted, #9ca3af)', cursor: 'default', fontSize: 11 }}
                  title={`Context change since the previous turn: ${deltaLabel} tokens`}
                >
                  {deltaLabel}
                </span>
              )}
              {/* Cache countdown — rendered by CacheCountdownBadge (owns its own tick) */}
              {(lastTurnEndMs !== null || lastCacheHitPct != null || lastAssistantMetrics != null) && (
                <CacheCountdownBadge
                  lastTurnEndMs={lastTurnEndMs}
                  lastCacheHitPct={lastCacheHitPct}
                  lastAssistantMetrics={lastAssistantMetrics}
                  isRunning={run != null}
                />
              )}
              {/* Wrap & reset — always present */}
              <button
                className="btn btn-sm"
                style={wrapBtnStyle}
                disabled={rotating || streaming}
                title="Wrap & reset (summarize + fresh session)"
                onClick={handleRotate}
              >
                {rotating ? '…' : '♻ Wrap & reset'}
              </button>
              {/* Utilization — shown when available */}
              {utilization != null && (
                <span
                  className="chat-session-utilization"
                  style={{ color: 'var(--color-muted, #9ca3af)', cursor: 'default' }}
                  title={`Subscription utilization this turn: ${utilization}%`}
                >
                  ⏱ {utilization}%
                </span>
              )}
            </span>
          )
        })()}
        {/* Thinking mode selector — compact, per-project localStorage persistence.
            Disabled (greyed + tooltip) for fable model: thinking always runs high on Fable 5. */}
        {(() => {
          const isFable = project.model === 'fable' || project.model?.startsWith('fable')
          const selectorTitle = isFable
            ? t['chat.think_mode_fable_hint']
            : t['chat.think_mode_hint']
          return (
            <div
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 3,
                opacity: isFable ? 0.45 : 1,
                transition: 'opacity 0.2s',
              }}
              title={selectorTitle}
            >
              <span style={{ fontSize: 11, color: 'var(--color-muted, #9ca3af)', userSelect: 'none' }}>
                {t['chat.think_mode_label']}
              </span>
              {THINK_MODES.map(m => (
                <button
                  key={m.value}
                  style={{
                    fontSize: 11,
                    padding: '1px 6px',
                    cursor: isFable || streaming ? 'default' : 'pointer',
                    background: thinkMode === m.value
                      ? 'var(--color-primary, #6366f1)'
                      : 'transparent',
                    color: thinkMode === m.value
                      ? '#fff'
                      : 'var(--color-muted, #9ca3af)',
                    border: `1px solid ${thinkMode === m.value ? 'var(--color-primary, #6366f1)' : 'var(--border, #374151)'}`,
                    borderRadius: 4,
                    lineHeight: 1.4,
                    transition: 'background 0.15s, color 0.15s, border-color 0.15s',
                  }}
                  disabled={isFable || streaming}
                  onClick={() => { if (!isFable) handleThinkModeChange(m.value) }}
                  aria-label={`Set thinking mode to ${m.value}`}
                  aria-pressed={thinkMode === m.value}
                >
                  {t[m.labelKey]}
                </button>
              ))}
            </div>
          )
        })()}
        <div className="chat-model-selector" title={t['chat.model_hint']}>
          <span className="chat-model-label">🧠</span>
          <select
            className="chat-model-select"
            value={project.model}
            onChange={e => handleModelChange(e.target.value as ModelKey)}
            disabled={changingModel || streaming}
          >
            {/* Unknown stored alias: show it as-is instead of silently displaying
                (and on next change POST-ing) a different model. */}
            {!MODELS.some(m => m.value === project.model) && (
              <option value={project.model}>{modelLabel(project.model)}</option>
            )}
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

        {messages.map((msg, idx) => {
          const isEmpty = !msg.text && msg.tools.length === 0 && !msg.error
          if (isEmpty && msg.role === 'assistant') return null

          // Spec-022/033: cold-start divider — gap between prev assistant turn end and this user msg
          const prevMsg = idx > 0 ? messages[idx - 1] : null
          const showColdDivider = (
            msg.role === 'user' &&
            msg.ts != null &&
            prevMsg?.ts != null &&
            (msg.ts - prevMsg.ts) > CACHE_TTL_MS
          )

          // Option picker: parse ```options block from non-streaming assistant messages.
          // Only the LAST assistant message gets an interactive picker; older ones are static.
          const lastAssistantIdx = messages.reduceRight(
            (found, m, i) => (found === -1 && m.role === 'assistant' ? i : found),
            -1,
          )
          const isLastAssistant = msg.role === 'assistant' && idx === lastAssistantIdx
          const parsedOpts =
            msg.role === 'assistant' && msg.text && !msg.streaming
              ? parseOptionsBlock(msg.text)
              : null

          return (
            <div key={msg.id}>
              {showColdDivider && msg.ts != null && prevMsg!.ts != null && (
                <div style={{
                  display: 'flex', alignItems: 'center', margin: '8px 0', gap: 8,
                  color: 'var(--color-muted, #9ca3af)', fontSize: 11,
                }}>
                  <div style={{ flex: 1, height: 1, background: 'var(--border, #374151)' }} />
                  <span>⚪ paused {fmtGap(msg.ts - prevMsg!.ts)} · cache cold</span>
                  <div style={{ flex: 1, height: 1, background: 'var(--border, #374151)' }} />
                </div>
              )}
              <div className={`chat-msg chat-msg-${msg.role}`}>
                {/* Spec-022: timestamp on messages that have one (live-session only) */}
                {msg.ts != null && (
                  <div style={{
                    textAlign: 'right', fontSize: 10,
                    color: 'var(--color-muted, #9ca3af)',
                    marginBottom: 2, userSelect: 'none',
                  }}>
                    {fmtHHMM(msg.ts)}
                  </div>
                )}
                {msg.tools.length > 0 && (
                  <div className="chat-tools">
                    {msg.tools.map((t, i) => (
                      <ToolBlock key={i} tool={t} />
                    ))}
                  </div>
                )}
                {/* Option picker: when message ends with ```options block, split rendering */}
                {parsedOpts ? (
                  <>
                    {parsedOpts.prefix && (
                      <div className="chat-msg-body markdown-wrap">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={_mdComponents}>{parsedOpts.prefix}</ReactMarkdown>
                      </div>
                    )}
                    <OptionPicker
                      options={parsedOpts.options}
                      isActive={isLastAssistant && !run}
                      onSelect={(value) => sendMessage(value)}
                    />
                  </>
                ) : msg.text ? (
                  <div className="chat-msg-body markdown-wrap">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={_mdComponents}>{msg.text}</ReactMarkdown>
                  </div>
                ) : null}
                {msg.error && (
                  <div className="chat-msg-error">⚠ {msg.error}</div>
                )}
                {/* Spec-022: per-turn metric footer on assistant messages */}
                {msg.role === 'assistant' && msg.metrics && !msg.streaming && (() => {
                  const m = msg.metrics
                  const cacheEmoji = m.cache_hit_pct >= CACHE_WARM_PCT
                    ? '♨️'
                    : m.cache_hit_pct < CACHE_COLD_PCT
                    ? '🧊'
                    : ''
                  const durStr = fmtTurnDuration(m.duration_ms)
                  const ptK = m.prompt_tokens >= 1000
                    ? `${Math.round(m.prompt_tokens / 1000)}K`
                    : `${m.prompt_tokens}`
                  const parts: string[] = []
                  if (durStr) parts.push(`⏱ ${durStr}`)
                  parts.push(`${cacheEmoji ? cacheEmoji + ' ' : ''}cache ${m.cache_hit_pct}%`)
                  parts.push(`${ptK}`)
                  return (
                    <div
                      title="Facts from this turn's usage — cache-read is billed ~10%, fresh tokens at full price."
                      style={{
                        fontSize: 10, marginTop: 4,
                        color: 'var(--color-muted, #9ca3af)',
                        userSelect: 'none', whiteSpace: 'nowrap', overflow: 'hidden',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      {parts.join(' · ')}
                    </div>
                  )
                })()}
              </div>
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
        {/* Spec-035: sub-agent lane — rendered while a run is active and subagents are present */}
        {run && subagents.length > 0 && (
          <div style={{
            padding: '4px 8px',
            borderTop: '1px solid var(--border, #374151)',
            fontSize: 11,
            color: 'var(--color-muted, #9ca3af)',
            display: 'flex',
            flexDirection: 'column',
            gap: 2,
          }}>
            <span style={{ fontWeight: 600, marginBottom: 2 }}>{t['chat.subagent_lane_label']}</span>
            {subagents.map(sa => (
              <div key={sa.task_id} style={{
                paddingLeft: 12,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}>
                <span>{sa.status === 'completed' ? '✓' : sa.status === 'failed' ? '✗' : '⚙'}</span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {sa.description || sa.task_id}
                </span>
                {sa.last_tool_name && sa.status === 'running' && (
                  <span style={{ color: 'var(--color-muted, #9ca3af)', fontStyle: 'italic' }}>
                    ↳ [{sa.last_tool_name}]
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
        {/* Run status bar — rendered by RunStatusBar (owns its own tick) so the message
            list does not re-render every second while a run is active. */}
        {run && (
          <RunStatusBar
            run={run}
            serverStartedAt={serverStartedAt}
            queueLen={queueItems.length}
            onStop={stopStream}
          />
        )}
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
        {/* Context early-warning banner — shown above the composer when context approaches limits */}
        {(() => {
          const warnTokens = contextTokens != null && contextTokens > 0
            ? contextTokens
            : estimateTokens(messages)
          const WARN_THRESHOLD = 150_000
          const ESCALATE_THRESHOLD = 175_000
          const isEscalated = warnTokens >= ESCALATE_THRESHOLD
          const isInWarnZone = warnTokens >= WARN_THRESHOLD && !isEscalated
          // Trigger: backend flag OR token-count fallback
          const shouldWarn = contextWarnFromBackend || isInWarnZone || isEscalated
          if (!shouldWarn) return null
          // Dismiss gate: once dismissed, suppress unless we've escalated into the ≥175K zone
          if (warnDismissedAtTokens !== null && !isEscalated) return null
          const nK = Math.round(warnTokens / 1000)
          const bannerColor = isEscalated
            ? 'var(--color-red, #ef4444)'
            : 'var(--color-yellow, #eab308)'
          const bannerBg = isEscalated
            ? 'rgba(239,68,68,0.08)'
            : 'rgba(234,179,8,0.08)'
          const bannerText = isEscalated
            ? `⚠️ Context ${nK}K — auto-rotate backstop at 175K. Wrap now to avoid losing the session.`
            : `⚠️ Context ~${nK}K — consider wrapping the session before a large turn (auto-rotate backstop at 175K).`
          return (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '6px 10px',
              background: bannerBg,
              border: `1px solid ${bannerColor}`,
              borderRadius: 6,
              fontSize: 12,
              color: bannerColor,
              margin: '4px 0',
              flexShrink: 0,
            }}>
              <span style={{ flex: 1, lineHeight: 1.4 }}>{bannerText}</span>
              <button
                style={{
                  fontSize: 11, padding: '2px 7px', cursor: rotating ? 'wait' : 'pointer',
                  background: 'transparent', border: `1px solid ${bannerColor}`,
                  borderRadius: 4, color: bannerColor, fontWeight: 600, whiteSpace: 'nowrap',
                  flexShrink: 0,
                }}
                disabled={rotating || streaming}
                title="Wrap & reset (summarize + fresh session)"
                onClick={handleRotate}
              >
                {rotating ? '…' : '♻ Wrap & reset'}
              </button>
              <button
                style={{
                  fontSize: 13, padding: '0 4px', cursor: 'pointer',
                  background: 'transparent', border: 'none',
                  color: bannerColor, lineHeight: 1, flexShrink: 0,
                }}
                title="Dismiss warning"
                aria-label="Dismiss context warning"
                onClick={() => setWarnDismissedAtTokens(warnTokens)}
              >
                ✕
              </button>
            </div>
          )
        })()}
        {/* Server-backed message queue panel — visible when messages are queued while agent runs.
            Survives page reload via GET /api/projects/{id}/chat/queue hydration on mount. */}
        {queueItems.length > 0 && (
          <div style={{
            display: 'flex', flexDirection: 'column', gap: 4,
            padding: '6px 8px',
            background: 'var(--color-bg-alt, rgba(0,0,0,0.04))',
            border: '1px solid var(--color-border, #e5e7eb)',
            borderRadius: 6,
            margin: '4px 0',
            flexShrink: 0,
          }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-muted, #6b7280)', marginBottom: 2 }}>
              {t['chat.queue_panel_label']} ({queueItems.length})
            </span>
            {queueItems.map((item, idx) => (
              <div key={item.id} style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
                <span style={{ fontSize: 11, color: 'var(--color-muted, #9ca3af)', minWidth: 16, paddingTop: 2 }}>
                  {idx + 1}.
                </span>
                {queueEditId === item.id ? (
                  <>
                    <textarea
                      style={{
                        flex: 1, fontSize: 12, padding: '3px 6px',
                        border: '1px solid var(--color-primary, #6366f1)',
                        borderRadius: 4, resize: 'vertical', minHeight: 36,
                        background: 'var(--color-bg, #fff)', color: 'inherit',
                      }}
                      value={queueEditText}
                      onChange={e => setQueueEditText(e.target.value)}
                      aria-label={t['chat.queue_item_aria']}
                    />
                    <button
                      style={{ fontSize: 11, padding: '2px 7px', cursor: 'pointer',
                        background: 'var(--color-primary, #6366f1)', color: '#fff',
                        border: 'none', borderRadius: 4, whiteSpace: 'nowrap' }}
                      aria-label={t['chat.queue_save_aria']}
                      onClick={() => {
                        const trimmed = queueEditText.trim()
                        if (!trimmed) return
                        api.chatQueueEdit(projectId, item.id, trimmed)
                          .then(res => {
                            setQueueItems(prev => prev.map(q => q.id === item.id ? res.item : q))
                            setQueueEditId(null)
                          })
                          .catch(() => setQueueEditId(null))
                      }}
                    >{t['chat.queue_save_btn']}</button>
                    <button
                      style={{ fontSize: 11, padding: '2px 7px', cursor: 'pointer',
                        background: 'transparent', border: '1px solid var(--color-border, #d1d5db)',
                        borderRadius: 4, whiteSpace: 'nowrap' }}
                      aria-label={t['chat.queue_cancel_aria']}
                      onClick={() => setQueueEditId(null)}
                    >{t['chat.queue_cancel_btn']}</button>
                  </>
                ) : (
                  <>
                    <span style={{ flex: 1, fontSize: 12, wordBreak: 'break-word', paddingTop: 2 }}
                      aria-label={t['chat.queue_item_aria']}>
                      {item.text}
                    </span>
                    <button
                      style={{ fontSize: 11, padding: '2px 7px', cursor: 'pointer',
                        background: 'transparent', border: '1px solid var(--color-border, #d1d5db)',
                        borderRadius: 4, whiteSpace: 'nowrap', flexShrink: 0 }}
                      aria-label={t['chat.queue_edit_aria']}
                      onClick={() => { setQueueEditId(item.id); setQueueEditText(item.text) }}
                    >{t['chat.queue_edit_btn']}</button>
                    <button
                      style={{ fontSize: 11, padding: '2px 7px', cursor: 'pointer',
                        background: 'transparent', border: '1px solid var(--color-red, #ef4444)',
                        borderRadius: 4, color: 'var(--color-red, #ef4444)', whiteSpace: 'nowrap', flexShrink: 0 }}
                      aria-label={t['chat.queue_delete_aria']}
                      onClick={() => {
                        api.chatQueueDelete(projectId, item.id)
                          .then(() => setQueueItems(prev => prev.filter(q => q.id !== item.id)))
                          .catch(() => {/* already gone */})
                        if (queueEditId === item.id) setQueueEditId(null)
                      }}
                    >{t['chat.queue_delete_btn']}</button>
                  </>
                )}
              </div>
            ))}
          </div>
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
              <button
                className={`chat-tool-btn${showDefer ? ' active' : ''}`}
                onClick={() => {
                  setShowDefer(s => !s)
                  setShowPrompts(false)
                  setShowSkills(false)
                  // Default datetime = now + 30 min
                  if (!deferDatetime) {
                    const d = new Date(Date.now() + 30 * 60 * 1000)
                    const pad = (n: number) => String(n).padStart(2, '0')
                    setDeferDatetime(
                      `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
                    )
                  }
                }}
                title={t['chat.defer_title']}
                aria-label={t['chat.defer_aria']}
              >⏱</button>
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

      {/* Deferred Run Modal */}
      {showDefer && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}
          onClick={() => setShowDefer(false)}
        >
          <div style={{
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: 10, padding: 24, minWidth: 340, maxWidth: 480,
            boxShadow: '0 8px 32px rgba(0,0,0,0.25)',
          }}
            onClick={e => e.stopPropagation()}
          >
            <h3 style={{ margin: '0 0 16px', fontSize: 16 }}>{t['chat.defer_modal_title']}</h3>
            {/* Mode tabs */}
            <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
              <button
                className={`btn btn-sm ${deferMode === 'time' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setDeferMode('time')}
              >{t['chat.defer_mode_time']}</button>
              <button
                className={`btn btn-sm ${deferMode === 'reset' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setDeferMode('reset')}
              >{t['chat.defer_mode_reset']}</button>
            </div>
            {deferMode === 'time' && (
              <div style={{ marginBottom: 16 }}>
                <label style={{ fontSize: 13, display: 'block', marginBottom: 4 }}>{t['chat.defer_fire_at']}</label>
                <input
                  type="datetime-local"
                  value={deferDatetime}
                  onChange={e => setDeferDatetime(e.target.value)}
                  style={{ width: '100%', fontSize: 14, padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', boxSizing: 'border-box' }}
                />
              </div>
            )}
            {deferMode === 'reset' && (
              <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>
                {t['chat.defer_reset_hint']}
              </p>
            )}
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 16 }}>
              {t['chat.defer_prompt_preview']}: <em>{input.slice(0, 80) || '(empty)'}</em>
            </p>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary btn-sm" onClick={() => setShowDefer(false)}>
                {t['common.cancel']}
              </button>
              <button
                className="btn btn-primary btn-sm"
                disabled={deferSubmitting || !input.trim() || (deferMode === 'time' && !deferDatetime)}
                onClick={async () => {
                  setDeferSubmitting(true)
                  try {
                    const body: Record<string, unknown> = {
                      project: project.id,
                      prompt: input,
                    }
                    if (deferMode === 'reset') {
                      body.fire_on_reset = true
                    } else {
                      // Convert local datetime-local to ISO-8601 UTC
                      body.fire_at = new Date(deferDatetime).toISOString()
                    }
                    await api.deferredCreate(body)
                    setShowDefer(false)
                    setInput('')
                    setDeferToast(t['chat.defer_queued'])
                    setTimeout(() => setDeferToast(null), 4000)
                  } catch (e: unknown) {
                    setDeferToast(e instanceof Error ? e.message : String(e))
                    setTimeout(() => setDeferToast(null), 4000)
                  } finally {
                    setDeferSubmitting(false)
                  }
                }}
              >
                {deferSubmitting ? t['chat.defer_submitting'] : t['chat.defer_queue']}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Defer toast */}
      {deferToast && (
        <div style={{
          position: 'fixed', bottom: 24, right: 24,
          padding: '10px 18px', background: 'var(--bg-card)',
          border: '1px solid var(--border)', borderRadius: 8,
          boxShadow: '0 4px 16px rgba(0,0,0,0.15)', fontSize: 13, zIndex: 9999,
        }}>
          {deferToast}
        </div>
      )}

      {/* Spec-021: Rotation toast */}
      {rotateToast && (
        <div style={{
          position: 'fixed', bottom: deferToast ? 72 : 24, right: 24,
          padding: '10px 18px', background: 'var(--bg-card)',
          border: '1px solid var(--border)', borderRadius: 8,
          boxShadow: '0 4px 16px rgba(0,0,0,0.15)', fontSize: 13, zIndex: 9999,
        }}>
          ♻ {rotateToast}
        </div>
      )}
    </div>
  )
}

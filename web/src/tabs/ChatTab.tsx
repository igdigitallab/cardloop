import React, { memo, useCallback, useEffect, useRef, useState } from 'react'
import { Lightbox } from '../components/Lightbox'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { mdComponents } from '../components/markdown'
import { api } from '../api'
import { PromptPicker } from '../components/PromptPicker'
import { SkillPicker } from '../components/SkillPicker'
import { ToolBlock } from '../components/ToolBlock'
import { OptionPicker, parseOptionsBlock } from '../components/OptionPicker'
import { SessionSelector } from '../components/SessionSelector'
import { UsageBadge } from '../components/UsageBadge'
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
  ActivityEventCompact,
} from '../types'
import { useProjectActivity, useSeedCursor } from '../hooks/useProjectActivity'
import { parseSseLine, readSseStream } from '../hooks/useChatStream'
import { MODELS, modelLabel } from '../lib/models'
import { t } from '../i18n'
import { Modal, ModalHead } from '../components/Modal'
import { Paperclip, ClipboardList, Wrench, Clock, Square, Pencil, Trash2, File, Image, Flame, Snowflake } from 'lucide-react'

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
  /** Desktop-split only: whether the chat pane is collapsed. Passed only from the desktop-split render site. */
  collapsed?: boolean
  /** Desktop-split only: toggle function for collapsing/expanding the chat pane. Passed only from the desktop-split render site. */
  onToggleCollapse?: () => void
  /** Mobile only: when true, collapse the top session bar (Row3) — driven by ProjectView's scroll detection. */
  chromeCollapsed?: boolean
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

function draftStorageKey(projectId: string, chatId?: string) {
  // Per-chat draft key; falls back to per-project when chatId is not yet known.
  return chatId ? `cops.chat.draft.${projectId}:${chatId}` : `cops.chat.draft.${projectId}`
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

/**
 * Formats a context-window maximum as a short denominator: 1_000_000 → "1M",
 * 2_000_000 → "2M", 200_000 → "200K". Used for the "used / max" context label
 * so the window reads "196K / 1M" rather than "196K / 1000K".
 */
function formatMax(n: number): string {
  if (n >= 1_000_000) {
    const m = n / 1_000_000
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`
  }
  return formatTokens(n)
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

// ─── Spec-038: inline image/video renderer + full-screen lightbox ────────────

/** Returns true if the URL points to a video file (by extension). */
function _isVideoSrc(src: string): boolean {
  const ext = src.split('?')[0].split('.').pop()?.toLowerCase() ?? ''
  return ['mp4', 'webm', 'mov', 'ogg', 'ogv'].includes(ext)
}

/** Custom img renderer for ReactMarkdown: detects video by extension and renders
 *  either a thumbnail <img> or a <video> preview; click opens the Lightbox. */
function ChatImage({ src, alt }: React.ImgHTMLAttributes<HTMLImageElement>) {
  const [open, setOpen] = useState(false)
  if (!src) return null
  const isVideo = _isVideoSrc(src)
  return (
    <>
      {isVideo ? (
        <video
          className="chat-msg-video"
          src={src}
          controls
          preload="metadata"
          onClick={() => setOpen(true)}
        />
      ) : (
        <img
          className="chat-msg-img"
          src={src}
          alt={alt ?? ''}
          loading="lazy"
          onClick={() => setOpen(true)}
        />
      )}
      {open && (
        <Lightbox src={src} alt={alt ?? ''} video={isVideo} onClose={() => setOpen(false)} />
      )}
    </>
  )
}

const _mdComponents = { ...mdComponents, img: ChatImage }

// ─── MsgCopyButton ────────────────────────────────────────────────────────────
// Copies the full markdown text of a completed assistant message to the clipboard.
// Visible on hover of the parent .chat-msg container (CSS-driven opacity).

function MsgCopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked */ }
  }
  return (
    <button
      className={`msg-copy-btn${copied ? ' msg-copy-btn--ok' : ''}`}
      onClick={handleCopy}
      title="Copy message"
      aria-label="Copy message"
    >
      {copied ? '✓ Copied' : 'Copy'}
    </button>
  )
}

// ─── CacheCountdownBadge ─────────────────────────────────────────────────────
// Isolated ticker so the parent ChatTab does NOT re-render on each second tick.
// Restored from commit 6e286cb (flicker-safe memo pattern).

interface CacheCountdownBadgeProps {
  lastTurnEndMs: number | null
  lastCacheHitPct: number | null
  /** Last assistant turn metrics (derived from messages in parent, passed down to avoid re-computing). */
  lastAssistantMetrics: TurnMetrics | undefined
  /** Whether a run is currently active. */
  isRunning: boolean
}

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
      color: isWarm ? 'var(--green)' : 'var(--text2)',
      cursor: 'default',
      fontSize: 11,
      whiteSpace: 'nowrap',
    }} title={cacheTip}>
      {cacheLabel}
    </span>
  )
})

// ─── ThinkModeButton ─────────────────────────────────────────────────────────
// Compact button that shows "🧠 <level>" and opens a small popover to change
// the thinking mode. Replaces the full-width <select> to save session-bar width.

interface ThinkModeButtonProps {
  value: ThinkMode
  disabled: boolean
  isFable: boolean
  title: string
  onChange: (mode: ThinkMode) => void
}

const ThinkModeButton = memo(function ThinkModeButton({
  value,
  disabled,
  isFable,
  title,
  onChange,
}: ThinkModeButtonProps) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  // Close on outside click or Escape
  useEffect(() => {
    if (!open) return
    function handleOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handleOutside)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handleOutside)
      document.removeEventListener('keydown', handleKey)
    }
  }, [open])

  const currentMode = THINK_MODES.find(m => m.value === value)
  const currentLabel = currentMode ? t[currentMode.labelKey] : value

  return (
    <div className="chat-think-btn-wrap" ref={containerRef} title={title}>
      <button
        className="chat-think-btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => { if (!disabled) setOpen(v => !v) }}
        style={{ opacity: isFable ? 0.45 : 1 }}
      >
        🧠 {currentLabel}
      </button>
      {open && (
        <div className="chat-think-menu" role="listbox" aria-label={t['chat.think_mode_label']}>
          {THINK_MODES.map(m => (
            <div
              key={m.value}
              role="option"
              aria-selected={value === m.value}
              className={`chat-think-option${value === m.value ? ' selected' : ''}`}
              onMouseDown={e => {
                e.preventDefault()
                onChange(m.value)
                setOpen(false)
              }}
            >
              {t[m.labelKey]}
            </div>
          ))}
        </div>
      )}
    </div>
  )
})

// ─── ModelThinkButton ────────────────────────────────────────────────────────
// Mobile-only combined pill: "<Model> · <H|M|L>" that opens ONE popover to pick both
// the model and the thinking level. Think is folded INTO the model control (no
// separate think button on mobile). Desktop keeps the separate <select> + ThinkModeButton.
const THINK_TAG: Record<ThinkMode, string> = { max: 'H', default: 'M', min: 'L' }

const ModelThinkButton = memo(function ModelThinkButton({
  model, thinkValue, disabled, onModelChange, onThinkChange,
}: {
  model: string
  thinkValue: ThinkMode
  disabled: boolean
  onModelChange: (m: ModelKey) => void
  onThinkChange: (mode: ThinkMode) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const isFable = model === 'fable' || model.startsWith('fable')

  useEffect(() => {
    if (!open) return
    function onOut(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onOut)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onOut)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const tag = isFable ? '' : THINK_TAG[thinkValue]
  return (
    <div className="composer-modelthink" ref={ref}>
      <button
        className="composer-modelthink-btn"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => { if (!disabled) setOpen(o => !o) }}
      >
        {modelLabel(model)}{tag ? ` · ${tag}` : ''}
      </button>
      {open && (
        <div className="composer-modelthink-menu" role="listbox">
          <div className="composer-modelthink-sec">{t['chat.model_hint']}</div>
          {MODELS.map(m => (
            <div
              key={m.value}
              role="option"
              aria-selected={m.value === model}
              className={`chat-think-option${m.value === model ? ' selected' : ''}`}
              onMouseDown={e => { e.preventDefault(); onModelChange(m.value as ModelKey); setOpen(false) }}
            >
              {m.label}
            </div>
          ))}
          <div className="composer-modelthink-sec">{t['chat.think_mode_label']}</div>
          {THINK_MODES.map(m => (
            <div
              key={m.value}
              role="option"
              aria-selected={m.value === thinkValue}
              className={`chat-think-option${m.value === thinkValue ? ' selected' : ''}`}
              style={isFable ? { opacity: 0.4, pointerEvents: 'none' } : undefined}
              onMouseDown={e => { e.preventDefault(); if (isFable) return; onThinkChange(m.value); setOpen(false) }}
            >
              {t[m.labelKey]}
            </div>
          ))}
        </div>
      )}
    </div>
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
  let label: string
  if (tool) {
    const hint = toolHint(tool)
    label = hint ? `${tool.name} · ${hint}` : tool.name
  } else if (silenceSec < 3 && elapsedSec > 1) {
    label = t['chat.status_writing']
  } else {
    label = run.source === 'card' ? t['chat.status_card_running'] : t['chat.status_thinking']
  }

  return (
    <div className={`chat-status-bar ${lvl}`}>
      {/* Pulsing dot replaces emoji icon — color inherits from bar state via CSS */}
      <span className="chat-status-pulse" aria-hidden="true" />
      {/* flex:1 on text truncates long tool names instead of pushing siblings right */}
      <span className="chat-status-text">{label}</span>
      <span className="chat-status-time">{formatDuration(elapsedSec)}</span>
      {silenceSec > 30 && (
        <span className="chat-status-silence">
          ⚠ silence {formatDuration(silenceSec)}
          {silenceSec > 120 && ' · possibly hung'}
        </span>
      )}
      {queueLen > 0 && (
        <span className="chat-status-queue" title={`${queueLen} message(s) queued, will send automatically`}>
          ⏭ {queueLen}
        </span>
      )}
      {/* Stop is last child — sits at far right naturally, no margin-left:auto needed */}
      <button className="chat-stop-btn" onClick={onStop} title={t['chat.stop_title']} aria-label={t['chat.stop_aria']}><Square size={13} /> {t['chat.stop_btn']}</button>
    </div>
  )
})

// ─── ChatTab ──────────────────────────────────────────────────────────────

// Distance from the scroll container bottom (px) within which the user is considered "pinned".
const SCROLL_PIN_THRESHOLD = 80

/** True on touch devices — `pointer: coarse` or `ontouchstart` present. */
const isTouchDevice: boolean =
  typeof window !== 'undefined' &&
  (window.matchMedia?.('(pointer: coarse)').matches || 'ontouchstart' in window)

export function ChatTab({ project, onProjectsReload, isActive, collapsed, onToggleCollapse, chromeCollapsed }: Props) {
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
  const [pendingHandoff, setPendingHandoff] = useState<string | null>(null)
  const [contextTokens, setContextTokens] = useState<number | null>(null)
  const [contextWindow, setContextWindow] = useState<number>(1_000_000)
  // Narrow-viewport flag: on mobile the context/model/think cluster is rendered inside
  // the composer bar (.composer-meta); on desktop it stays in the top session bar.
  const [isMobile, setIsMobile] = useState<boolean>(
    () => typeof window !== 'undefined' && !!window.matchMedia?.('(max-width: 768px)').matches,
  )
  // Context-state popover (the 🔥/❄ icon in the composer bar) open flag.
  const [ctxOpen, setCtxOpen] = useState(false)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
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
  // feedRef: the scrollable .chat-feed container — used for stick-to-bottom logic.
  const feedRef = useRef<HTMLDivElement>(null)
  // pinnedRef: true when the user is scrolled within SCROLL_PIN_THRESHOLD of the bottom.
  // A ref (not state) so scroll-event updates don't trigger re-renders.
  const pinnedRef = useRef<boolean>(true)
  // showNewMsgPill: shows the "↓ New messages" button when unpinned and new content arrives.
  const [showNewMsgPill, setShowNewMsgPill] = useState<boolean>(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const busActiveRef = useRef<boolean>(false)
  // Server-backed message queue: replaces the old client-only queueRef.
  // Survives page reload via GET /api/projects/{id}/chat/queue on mount.
  interface QueueItem { id: string; text: string; created_at: number }
  const [queueItems, setQueueItems] = useState<QueueItem[]>([])
  const [queueEditId, setQueueEditId] = useState<string | null>(null)
  const [queueEditText, setQueueEditText] = useState<string>('')
  const streamingRef = useRef(false)
  // Spec-041 A3: always-current projectId for use in async drain callbacks.
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId
  // Track previous isActive to detect false→true reactivation transitions.
  const prevIsActiveRef = useRef<boolean>(isActive ?? false)

  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [showPrompts, setShowPrompts] = useState(false)
  const [showSkills, setShowSkills] = useState(false)
  const [deferToast, setDeferToast] = useState<string | null>(null)
  // One-click "after reset" button state
  const [deferAfterResetBusy, setDeferAfterResetBusy] = useState(false)
  // Pending deferred runs chip: count + modal + records
  const [pendingDeferred, setPendingDeferred] = useState<unknown[]>([])
  const [showPendingDeferred, setShowPendingDeferred] = useState(false)
  // Inline edit state for the deferred-runs management modal
  const [editingDeferredId, setEditingDeferredId] = useState<string | null>(null)
  const [editDeferredPrompt, setEditDeferredPrompt] = useState('')
  const [editDeferredMode, setEditDeferredMode] = useState<'time' | 'reset'>('time')
  const [editDeferredDatetime, setEditDeferredDatetime] = useState('')
  // Spec-021/039: manual reset + auto-compact UI state
  const [rotateToast, setRotateToast] = useState<string | null>(null)
  const [rotating, setRotating] = useState(false)
  // Which kind of reset is in progress — drives the progress indicator text.
  const [rotatingKind, setRotatingKind] = useState<'handoff' | 'blank' | null>(null)
  // spec-042: unified reset-confirm modal (replaces direct no-confirm handleRotate calls)
  const [resetModalOpen, setResetModalOpen] = useState(false)
  // Spec-039: toast shown when native auto-compact fires (kind:"compact" bus event)
  const [compactToast, setCompactToast] = useState(false)
  // Live compaction-in-progress indicator: true from compact event until first assistant output or run end.
  const [isCompacting, setIsCompacting] = useState(false)
  // Ref mirror so SSE/bus event closures can read the current value without stale closure issues.
  const isCompactingRef = useRef(false)
  // Fallback safety timer ref — clears the indicator after 120s if no resume event arrives.
  const compactFallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
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
  // Spec-043 C: fresh (non-cached) tokens from the last completed turn — indicates "expensive" portion.
  // null until first SSE result; 0 = fully warm; positive = portion billed at full price this turn.
  const [lastFreshTokens, setLastFreshTokens] = useState<number | null>(null)

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

  // Persist the chat input draft to localStorage on every change so a stream abort,
  // projects refresh, or accidental tab close doesn't wipe unsent text.
  // Cleared on successful send (see sendMessage below).
  useEffect(() => {
    try {
      const key = draftStorageKey(projectId, effectiveChatId || undefined)
      if (input) {
        localStorage.setItem(key, input)
      } else {
        localStorage.removeItem(key)
      }
    } catch { /* localStorage unavailable */ }
  }, [input, projectId, effectiveChatId])

  useEffect(() => { streamingRef.current = streaming }, [streaming])

  const errorRef = useRef('')
  useEffect(() => { errorRef.current = error }, [error])

  // Keep the ref in sync with the state so SSE/bus closures can read the live value.
  useEffect(() => { isCompactingRef.current = isCompacting }, [isCompacting])

  // Cleanup fallback timer on unmount to avoid ghost state after tab switch / unmount.
  useEffect(() => {
    return () => {
      if (compactFallbackTimerRef.current !== null) {
        clearTimeout(compactFallbackTimerRef.current)
        compactFallbackTimerRef.current = null
      }
    }
  }, [])

  // Load pending deferred runs for this project (for the queued chip).
  // Filters client-side by session_key === project.session_key.
  const refreshPendingDeferred = useCallback(async () => {
    if (!project.session_key) return
    try {
      const all = await api.deferredList('?status=pending')
      const sk = project.session_key
      setPendingDeferred((all as Array<Record<string, unknown>>).filter(r => r['session_key'] === sk))
    } catch {
      // Non-fatal — chip just shows stale data
    }
  }, [project.session_key])

  useEffect(() => {
    refreshPendingDeferred()
    const id = setInterval(refreshPendingDeferred, 45_000)
    return () => clearInterval(id)
  }, [refreshPendingDeferred])

  // Stick-to-bottom: auto-scroll only when the user is pinned (within SCROLL_PIN_THRESHOLD of bottom).
  // When unpinned (user scrolled up), new content does NOT jump the viewport — instead the pill
  // appears. The user clicking the pill re-pins and scrolls to bottom.

  // Scrolls the feed to the bottom and re-pins. Call on intentional actions (send, initial load).
  const scrollToBottom = useCallback(() => {
    const feed = feedRef.current
    if (!feed) return
    feed.scrollTop = feed.scrollHeight
    pinnedRef.current = true
    setShowNewMsgPill(false)
  }, [])

  // onScroll: update pinned state based on how close the user is to the bottom.
  const handleFeedScroll = useCallback(() => {
    const feed = feedRef.current
    if (!feed) return
    const distFromBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight
    const nowPinned = distFromBottom <= SCROLL_PIN_THRESHOLD
    pinnedRef.current = nowPinned
    if (nowPinned) setShowNewMsgPill(false)
  }, [])

  useEffect(() => {
    if (pinnedRef.current) {
      // User is pinned to bottom — auto-follow new content.
      bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
    } else {
      // User has scrolled up — show the pill so they know new content arrived.
      setShowNewMsgPill(true)
    }
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

  // Track the narrow-viewport breakpoint (must match the 768px CSS breakpoint) so the
  // context/model/think cluster renders in the composer bar on mobile, top bar on desktop.
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 768px)')
    const onChange = () => setIsMobile(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // Tick timers removed from ChatTab — now owned by RunStatusBar and CacheCountdownBadge
  // child components. This prevents the message list from re-rendering every second.

  function histToMessages(items: HistoryMessage[]): ChatMessage[] {
    return items.map((m, i) => ({
      id: `hist-${i}`, role: m.role, text: m.text, tools: m.tools, streaming: false,
    }))
  }

  // Fetch /live + history and rebuild the in-flight turn (or restore the final answer).
  // Callers supply isCancelled() so each call site manages its own cancellation token.
  // This is intentionally a one-shot fetch — no persistent connection added.
  const hydrateFromServer = useCallback((isCancelled: () => boolean) => {
    Promise.all([
      api.sessionHistory(projectId),
      api.chatQueue(projectId).catch(() => ({ items: [] as Array<{ id: string; text: string; created_at: number }> })),
      // Spec-035 L3: /live replaces /running — returns running state + turn history + started_at
      api.projectLive(projectId).catch(() => ({ running: false, turn_id: null, started_at: null, model: null, cost_usd: null, cursor: 0, events: [] as Array<Record<string, unknown>>, pending_handoff: null as string | null })),
    ]).then(([histRes, queueRes, liveRes]) => {
      if (isCancelled()) return
      setQueueItems(queueRes.items)
      setContextTokens(histRes.context_tokens != null ? histRes.context_tokens : null)
      if (histRes.context_window != null && histRes.context_window > 0) setContextWindow(histRes.context_window)
      // Spec-033: seed cache freshness anchor from the persisted transcript data
      if (histRes.last_turn_at != null) setLastTurnEndMs(histRes.last_turn_at)
      if (histRes.last_cache_hit_pct != null) setLastCacheHitPct(histRes.last_cache_hit_pct)

      setPendingHandoff(liveRes.pending_handoff ?? null)

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
      // Clear any stale error banner left over from a prior aborted stream so
      // the operator sees the recovered content, not an old error.
      setError('')
    }).catch(() => { if (!isCancelled()) { setMessages([]); setError('') } })
  }, [projectId, effectiveChatId, seedCursor]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false
    abortRef.current?.abort()
    setMessages([])
    // Restore any saved draft for this project+chat; fall back to empty string.
    try {
      const savedDraft = localStorage.getItem(draftStorageKey(projectId, effectiveChatId || undefined))
      setInput(savedDraft ?? '')
    } catch {
      setInput('')
    }
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
    setLastFreshTokens(null)
    // Re-pin on project/chat switch so initial load lands at the bottom.
    pinnedRef.current = true
    setShowNewMsgPill(false)

    hydrateFromServer(() => cancelled)

    return () => { cancelled = true }
  // Spec-037: re-hydrate when the active chat changes (activeChatId drives all chat state)
  }, [projectId, effectiveChatId, seedCursor])

  // Re-hydrate on tab reactivation (false→true transition).
  // Fixes missed SSE events while the tab was inactive: fetches /live + history and
  // rebuilds the in-flight turn (or restores the final answer) without a page refresh.
  // Guard: skip if a direct /chat stream is already rendering (streamingRef.current).
  // No setMessages([]) before fetch — swap after resolve to avoid a blank flash.
  useEffect(() => {
    const wasActive = prevIsActiveRef.current
    prevIsActiveRef.current = isActive ?? false

    // Only act on false→true transitions; skip initial mount (mount effect already hydrates).
    if (!isActive || wasActive) return
    // Direct /chat stream is rendering live — don't clobber it.
    if (streamingRef.current) return

    let cancelled = false
    hydrateFromServer(() => cancelled)
    return () => { cancelled = true }
  }, [isActive, hydrateFromServer])

  // Mobile resume: clear stale error banner + re-hydrate when screen turns back on.
  // The existing reactivation effect only fires on false→true isActive transitions, so
  // if the chat tab was already active when the screen went off, the banner persists.
  // This effect catches visibilitychange→visible and the network "online" event while
  // the tab is active, and re-hydrates as long as no live stream is in progress.
  useEffect(() => {
    if (!isActive) return

    const onResume = () => {
      if (document.visibilityState !== 'visible') return
      // Don't clobber an actively rendering /chat stream; but if an error banner is
      // showing the stream is frozen — still recover so the banner gets cleared.
      if (streamingRef.current && !errorRef.current) return
      let cancelled = false
      hydrateFromServer(() => cancelled)
      // Cancellation cleanup runs on the next resume event or unmount.
      return () => { cancelled = true }
    }

    document.addEventListener('visibilitychange', onResume)
    window.addEventListener('online', onResume)
    return () => {
      document.removeEventListener('visibilitychange', onResume)
      window.removeEventListener('online', onResume)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- hydrateFromServer is stable (useCallback); streamingRef is a ref
  }, [isActive, hydrateFromServer])

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
            // Spec-041 A2: drain queued message on poll-detected turn completion.
            drainQueue()
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
      // Compaction ended — first text output means the turn resumed.
      if (isCompactingRef.current) {
        if (compactFallbackTimerRef.current !== null) { clearTimeout(compactFallbackTimerRef.current); compactFallbackTimerRef.current = null }
        isCompactingRef.current = false
        setIsCompacting(false)
        setTimeout(() => setCompactToast(false), 4000)
      }
      setRun(r => r ? { ...r, lastEventAt: now, currentTool: null } : r)
      setMessages(prev => appendChunk(prev, { kind: 'text', text: evt.text }))

    } else if (evt.kind === 'tool') {
      if (!busActiveRef.current) return
      // Compaction ended — first tool call means the turn resumed.
      if (isCompactingRef.current) {
        if (compactFallbackTimerRef.current !== null) { clearTimeout(compactFallbackTimerRef.current); compactFallbackTimerRef.current = null }
        isCompactingRef.current = false
        setIsCompacting(false)
        setTimeout(() => setCompactToast(false), 4000)
      }
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
      // Spec-041 A2: drain queued message on bus-originated turn completion.
      drainQueue()
      // Compaction resolves when run ends (covers edge-case where no text/tool arrived).
      if (isCompactingRef.current) {
        if (compactFallbackTimerRef.current !== null) { clearTimeout(compactFallbackTimerRef.current); compactFallbackTimerRef.current = null }
        isCompactingRef.current = false
        setIsCompacting(false)
        setTimeout(() => setCompactToast(false), 4000)
      }

    } else if (evt.kind === 'compact') {
      // Spec-039: native CLI auto-compact fired — session is kept, context is smaller.
      // Show a persistent live indicator for the duration of compaction (can be 30–60s),
      // plus the bottom-right toast. The indicator clears when assistant output resumes.
      void (evt as ActivityEventCompact) // type assertion for exhaustiveness
      setCompactToast(true)
      setIsCompacting(true)
      isCompactingRef.current = true
      // Fallback safety: force-clear after 120s to prevent a stuck ghost indicator.
      if (compactFallbackTimerRef.current !== null) clearTimeout(compactFallbackTimerRef.current)
      compactFallbackTimerRef.current = setTimeout(() => {
        compactFallbackTimerRef.current = null
        setIsCompacting(false)
        isCompactingRef.current = false
        setCompactToast(false)
      }, 120_000)
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
    setLastCacheHitPct(null)
    setLastFreshTokens(null)
    api.sessionHistory(projectId)
      .then(res => { setMessages(histToMessages(res.messages)); setContextTokens(res.context_tokens != null ? res.context_tokens : null); if (res.context_window != null && res.context_window > 0) setContextWindow(res.context_window) })
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

  // Spec-041 A3: drainQueue no longer re-sends messages — the backend is the sole
  // deliverer.  This function only refreshes the queue display so the panel stays in
  // sync as the backend drains queued items.  Called from sendMessage finally, bus
  // run_end, and /live poll completion — same call sites as before.
  const drainQueue = useCallback(() => {
    const currentProjectId = projectIdRef.current
    api.chatQueue(currentProjectId)
      .then(res => {
        if (projectIdRef.current === currentProjectId) {
          setQueueItems(res.items)
        }
      })
      .catch(() => {/* non-critical — stale display is acceptable */})
  }, [])

  const sendMessage = useCallback(async (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    const readyFiles = overrideText === undefined ? attachments.filter(a => a.path) : []
    const effectiveText = text || (readyFiles.length > 0 ? t['chat.look_at_files'] : '')
    if (!effectiveText) return

    // Spec-041 A1: enqueue whenever a turn is active — either a direct stream
    // OR a bus/SSE/poll-adopted run — so the message isn't lost to a "busy" 409.
    if ((streaming || busActiveRef.current) && overrideText === undefined) {
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
    // Sending a message is an intentional action — re-pin to bottom regardless of scroll position.
    pinnedRef.current = true
    setShowNewMsgPill(false)
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
            // Spec-043 C fix: update on any numeric context_tokens (including 0) so a fresh
            // session with 0 tokens clears the stale large value instead of letting it linger.
            // The old guard (`> 0`) caused the "104k stale value" bug after rotate.
            if (typeof evtAny.context_tokens === 'number') {
              // Snapshot the prior value before overwriting — feeds the growth delta badge.
              setContextTokens(prev => { setPrevContextTokens(prev); return evtAny.context_tokens as number })
            }
            if (typeof evtAny.context_window === 'number' && (evtAny.context_window as number) > 0) {
              setContextWindow(evtAny.context_window as number)
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
            // Spec-043 C: update cache-hit % and fresh tokens from the SSE result so the
            // tooltip cost signal reflects the most recent completed turn without a page reload.
            if (typeof evtAny.cache_hit_pct === 'number') {
              setLastCacheHitPct(evtAny.cache_hit_pct as number)
            }
            if (typeof evtAny.fresh_tokens === 'number') {
              setLastFreshTokens(evtAny.fresh_tokens as number)
            }
          }
          // Spec-039: "rotation" SSE event is no longer emitted by the backend (auto-rotation
          // was removed). This block is a graceful no-op kept for backwards compatibility in
          // case an older server instance is running during a deploy transition.

          // Spec-041 A3: backend was busy — message was enqueued server-side instead of
          // starting a turn.  Stop streaming state, remove the optimistic bubbles that were
          // appended for this send, and refresh the queue display.  The backend drain loop
          // (or the lock-release drain) will deliver the message and emit run_start/run_end
          // on the activity bus so the tab re-renders the real turn.
          if (evt.type === 'queued') {
            setStreaming(false)
            setRun(null)
            // Remove the optimistic user + assistant bubbles added for this aborted send.
            setMessages(prev => prev.slice(0, -2))
            // Refresh queue display from server so the newly-enqueued item appears.
            const currentProjectId = projectIdRef.current
            api.chatQueue(currentProjectId)
              .then(res => { if (projectIdRef.current === currentProjectId) setQueueItems(res.items) })
              .catch(() => {/* non-critical */})
            return
          }

          // First assistant output (any kind) clears the live compaction indicator.
          if (isCompactingRef.current && (evt.type === 'text_delta' || evt.type === 'text' || evt.type === 'tool')) {
            if (compactFallbackTimerRef.current !== null) { clearTimeout(compactFallbackTimerRef.current); compactFallbackTimerRef.current = null }
            isCompactingRef.current = false
            setIsCompacting(false)
            setTimeout(() => setCompactToast(false), 4000)
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
      // Suppress intentional aborts regardless of how the browser names the error.
      // AbortError is the spec name; TypeError: "Failed to fetch" / "The user aborted a request"
      // can appear in Chrome/Firefox when the signal fires mid-stream.
      if (err instanceof Error && err.name === 'AbortError') return
      if (abortRef.current?.signal.aborted) return
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
      setMessages(prev => finalizeStreaming(prev, msg))
    } finally {
      setStreaming(false)
      setRun(null)
      abortRef.current = null
      textareaRef.current?.focus()
      onProjectsReload()
      // Spec-041 A2: drain via shared helper (also called from bus/poll paths).
      drainQueue()
      // Ensure compaction indicator is cleared when the turn ends (covers error/abort paths).
      if (isCompactingRef.current) {
        if (compactFallbackTimerRef.current !== null) { clearTimeout(compactFallbackTimerRef.current); compactFallbackTimerRef.current = null }
        isCompactingRef.current = false
        setIsCompacting(false)
        setTimeout(() => setCompactToast(false), 4000)
      }
    }
  }, [input, projectId, streaming, onProjectsReload, attachments, thinkMode])

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

  // Shared reset handler — called after the unified confirm modal resolves.
  // spec-042: accepts handoff flag forwarded to POST /api/projects/{id}/rotate {handoff}.
  // handoff=true  → backend builds a cheap haiku summary seeded into the next session.
  // handoff=false → blank reset (prior behaviour).
  async function handleRotate(handoff: boolean) {
    setResetModalOpen(false)
    setRotating(true)
    setRotatingKind(handoff ? 'handoff' : 'blank')
    // Immediately clear the stale session view so the UI is visibly responsive.
    setMessages([])
    setContextTokens(0)
    setPrevContextTokens(null)
    try {
      const data = await api.rotate(projectId, handoff)
      if (data.reset) {
        // Backend confirmed a real eviction and fresh session start.
        // Keep contextTokens(0) set optimistically above; also clear cost-signal state.
        setPrevContextTokens(null)
        setContextWarnFromBackend(false)
        setWarnDismissedAtTokens(null)
        setLastCacheHitPct(null)
        setLastFreshTokens(null)
        const toastMsg = handoff
          ? 'New session — prior context will be handed off'
          : t['chat.reset_done']
        setRotateToast(toastMsg)
        // Spec-043 C: drive the counter from the backend so the displayed value reflects
        // the actual (now-empty) new session rather than a client-side assumption.
        // One-shot fire-and-forget; rotCancelled is a local flag captured by the closure
        // (no cleanup needed — rotate is a singular non-repeating action per session).
        // eslint-disable-next-line prefer-const
        let _rotCancelled = false
        hydrateFromServer(() => _rotCancelled)
      } else {
        // reset:false — no active session was present.
        setRotateToast(t['chat.reset_no_session'])
      }
    } catch (e: unknown) {
      const reason = e instanceof Error ? e.message : String(e)
      setRotateToast(t['chat.reset_failed'].replace('{reason}', reason))
      // Restore history so the user can see the pre-reset messages again.
      let cancelled = false
      hydrateFromServer(() => cancelled)
    } finally {
      setRotating(false)
      setRotatingKind(null)
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
      {/* Spec-045: merged toolbar — chat tabs (left) + session controls + right cluster in ONE row.
          Layout: [tab…] [+]  [↺] [◉ session ▾]  ·(auto)·  [▬ ctx] [♨️ cache] [◆ model ▾] [🧠 think] [⟩]
          The ⟩ collapse button renders only when onToggleCollapse is provided (desktop-split). */}
      <div className={`chat-session-bar${isMobile && chromeCollapsed ? ' collapsed' : ''}`}>
        {/* Left: chat tabs inline */}
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
                      border: '1px solid var(--accent)', borderRadius: 3,
                    }}
                    value={renameValue}
                    onChange={e => setRenameValue(e.target.value)}
                    placeholder={t['chat.tabs_rename_placeholder']}
                    onKeyDown={e => { if (e.key === 'Escape') setRenamingChatId(null) }}
                  />
                  <button
                    type="submit"
                    style={{ fontSize: 10, padding: '1px 4px', cursor: 'pointer',
                      background: 'var(--accent)', color: '#fff',
                      border: 'none', borderRadius: 3 }}
                  >{t['chat.tabs_rename_confirm']}</button>
                </form>
              ) : (
                <span className="chat-named-tab-label">{chat.name}</span>
              )}
              {/* Close button only on the active tab */}
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
        {chats.length > 0 && (
          <button
            className="chat-named-tab-new"
            title={t['chat.tabs_new']}
            aria-label={t['chat.tabs_new_aria']}
            onClick={handleCreateChat}
          >+</button>
        )}
        {/* Separator between tabs and session controls */}
        {chats.length > 0 && <span className="chat-toolbar-sep" />}
        {/* Left group: single reset + session selector, grouped together. */}
        <div className="chat-session-left">
          {(() => {
            // Reset button prominence mirrors the context fill: amber at 75% of
            // the window, red at 90%. Computed here (not inside the health IIFE)
            // so the single ↺ can sit at the far left, before the selector.
            const realTokens = contextTokens != null && contextTokens > 0
              ? contextTokens
              : estimateTokens(messages)
            const warnAt = contextWindow * 0.75
            const critAt = contextWindow * 0.90
            const isProminent = realTokens >= warnAt
            const wrapBtnStyle: React.CSSProperties = isProminent
              ? {
                  fontSize: 13, lineHeight: 1, padding: '2px 7px', cursor: rotating ? 'wait' : 'pointer',
                  background: 'var(--bg-card)', border: `1px solid ${realTokens >= critAt ? 'var(--red)' : 'var(--yellow)'}`,
                  borderRadius: 4,
                  color: realTokens >= critAt ? 'var(--red)' : 'var(--yellow)',
                  fontWeight: 600,
                }
              : {
                  fontSize: 13, lineHeight: 1, padding: '2px 7px', cursor: rotating ? 'wait' : 'pointer',
                  background: 'transparent', border: '1px solid var(--border)',
                  borderRadius: 4, color: 'var(--text2)',
                }
            return (
              <button
                className="btn btn-sm chat-session-reset"
                style={wrapBtnStyle}
                disabled={rotating || streaming}
                title={t['chat.reset_session_tip']}
                onClick={() => setResetModalOpen(true)}
                aria-label={t['chat.reset_session_btn']}
              >
                {rotating ? '…' : '↺'}
              </button>
            )
          })()}
          <SessionSelector
            projectId={projectId}
            onSessionChange={handleSessionChange}
            onRequestReset={() => setResetModalOpen(true)}
          />
        </div>
        {/* Right group: context health + cache badge + model + think + collapse.
            margin-left:auto (on .chat-session-right) pushes it to the right edge. */}
        <div className="chat-session-right">
          {/* Session health — context "used / max" + progress bar + cache badge.
              Desktop only: on mobile this lives in the composer bar (.composer-meta). */}
          {!isMobile && messages.length > 0 && (() => {
            const real = contextTokens != null && contextTokens > 0
            const tokens = real ? contextTokens! : estimateTokens(messages)

            // Color scale relative to the real context window.
            const warnAt = contextWindow * 0.75
            const critAt = contextWindow * 0.90
            const tokenColor =
              tokens >= critAt ? 'var(--red)' :
              tokens >= warnAt ? 'var(--yellow)' :
              'var(--text2)'

            const fillFrac = Math.min(tokens / contextWindow, 1)
            const barColor =
              fillFrac >= 0.95 ? 'var(--red)' :
              fillFrac >= 0.75 ? 'var(--yellow)' :
              'var(--green)'

            const lastAssistantMetrics = [...messages].reverse().find(
              m => m.role === 'assistant' && m.metrics != null
            )?.metrics

            const utilization = lastAssistantMetrics?.utilization ?? null
            const deltaTokens = real && prevContextTokens != null ? tokens - prevContextTokens : null
            const deltaLabel = deltaTokens != null && deltaTokens !== 0
              ? `${deltaTokens > 0 ? '+' : '−'}${formatTokens(Math.abs(deltaTokens))}`
              : null

            const isCacheRunning = run != null
            let cacheIsWarm = false
            let cacheRemainingSec = 0
            if (isCacheRunning) {
              cacheIsWarm = true
            } else if (lastTurnEndMs !== null) {
              cacheRemainingSec = Math.max(0, (CACHE_TTL_MS - (Date.now() - lastTurnEndMs)) / 1000)
              cacheIsWarm = cacheRemainingSec > 0
            }
            const effectiveCacheHitPct = lastAssistantMetrics?.cache_hit_pct ?? lastCacheHitPct
            if (!isCacheRunning && effectiveCacheHitPct != null && effectiveCacheHitPct < CACHE_COLD_PCT) {
              cacheIsWarm = false
            }
            const hasCacheInfo = lastTurnEndMs !== null || lastCacheHitPct != null || lastAssistantMetrics != null
            const cacheLine = !hasCacheInfo
              ? null
              : isCacheRunning
                ? t['chat.session_bar_cache_running']
                : cacheIsWarm
                  ? t['chat.session_bar_cache_warm']
                      .replace('{remaining}', fmtCountdown(cacheRemainingSec))
                      .replace('{pct}', effectiveCacheHitPct != null ? `${Math.round(effectiveCacheHitPct)}%` : '—')
                  : t['chat.session_bar_cache_cold']

            const tokenTipLines: string[] = [
              real
                ? t['chat.session_bar_tip'].replace('{tokens}', tokens.toLocaleString('en'))
                : t['chat.token_count_rough'],
              t['chat.session_bar_messages'].replace('{n}', messages.length.toLocaleString('en')),
            ]
            if (deltaLabel != null) {
              tokenTipLines.push(t['chat.session_bar_delta'].replace('{delta}', deltaLabel))
            }
            if (cacheLine != null) {
              tokenTipLines.push(cacheLine)
            }
            const effectiveFreshTokens = lastAssistantMetrics?.fresh_tokens ?? lastFreshTokens
            if (effectiveFreshTokens != null && effectiveCacheHitPct != null && effectiveCacheHitPct > 0) {
              const freshK = formatTokens(effectiveFreshTokens)
              const hitPct = Math.round(effectiveCacheHitPct)
              tokenTipLines.push(
                `⚙ Cost: ~${freshK} fresh (×1.0) · ${hitPct}% cached (×0.10) — warm sessions are cheap`
              )
            } else if (effectiveFreshTokens != null && effectiveCacheHitPct === 0) {
              const freshK = formatTokens(effectiveFreshTokens)
              tokenTipLines.push(
                `⚙ Cost: ${freshK} fresh tokens — cache cold, this turn billed at full price`
              )
            }
            if (utilization != null) {
              tokenTipLines.push(t['chat.session_bar_util'].replace('{pct}', String(utilization)))
            }
            const tokenTip = tokenTipLines.join('\n')

            return (
              <span className="chat-session-health" style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                fontSize: 12, whiteSpace: 'nowrap', flexShrink: 0,
              }}>
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
                <span style={{ color: tokenColor, cursor: 'default' }} title={tokenTip}>
                  {real ? '' : '~'}{formatTokens(tokens)}
                  <span style={{ color: 'var(--text2)' }}> / {formatMax(contextWindow)}</span>
                </span>
                {(lastTurnEndMs !== null || lastCacheHitPct != null || lastAssistantMetrics != null) && (
                  <CacheCountdownBadge
                    lastTurnEndMs={lastTurnEndMs}
                    lastCacheHitPct={lastCacheHitPct}
                    lastAssistantMetrics={lastAssistantMetrics}
                    isRunning={run != null}
                  />
                )}
              </span>
            )
          })()}
          {/* Model selector — desktop only (mobile: in the composer bar). */}
          {!isMobile && (
            <div className="chat-model-selector" title={t['chat.model_hint']}>
              <span className="chat-model-label">◆</span>
              <select
                className="chat-model-select"
                value={project.model}
                onChange={e => handleModelChange(e.target.value as ModelKey)}
                disabled={changingModel || streaming}
              >
                {!MODELS.some(m => m.value === project.model) && (
                  <option value={project.model}>{modelLabel(project.model)}</option>
                )}
                {MODELS.map(m => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>
          )}
          {/* Thinking mode — compact button+popover. Disabled for fable. Desktop only. */}
          {!isMobile && (() => {
            const isFable = project.model === 'fable' || project.model?.startsWith('fable')
            const selectorTitle = isFable
              ? t['chat.think_mode_fable_hint']
              : t['chat.think_mode_hint']
            return (
              <ThinkModeButton
                value={thinkMode}
                disabled={isFable || streaming}
                isFable={isFable}
                title={selectorTitle}
                onChange={handleThinkModeChange}
              />
            )
          })()}
          {/* Collapse button — only when onToggleCollapse is provided (desktop-split site). */}
          {onToggleCollapse && (
            <button
              className="chat-collapse-btn"
              onClick={onToggleCollapse}
              title={collapsed ? t['split.expand_chat'] : t['split.collapse_chat']}
              aria-label={collapsed ? t['split.expand_chat'] : t['split.collapse_chat']}
              aria-expanded={!collapsed}
            >
              {collapsed ? '⟨' : '⟩'}
            </button>
          )}
        </div>
      </div>

      <div className="chat-feed" ref={feedRef} onScroll={handleFeedScroll} style={{ position: 'relative' }}>
        {rotating && (
          <div className="chat-empty">
            <div className="chat-status-bar" style={{ justifyContent: 'center', padding: '12px 20px', fontSize: 13 }}>
              <span className="att-spinner" style={{ fontSize: 18 }}>↻</span>
              <span style={{ fontWeight: 500 }}>
                {rotatingKind === 'handoff'
                  ? 'Compressing session & handing off context… this can take up to a minute'
                  : 'Starting a new session…'}
              </span>
            </div>
          </div>
        )}
        {!rotating && messages.length === 0 && !pendingHandoff && (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <p>{t['chat.empty_hint']}<br />{t['chat.empty_session_hint']}</p>
          </div>
        )}
        {!rotating && messages.length === 0 && pendingHandoff && (
          <div className="chat-handoff-card">
            <div className="chat-handoff-card-header">↩ Carried over from previous session</div>
            <div className="chat-handoff-card-body">{pendingHandoff}</div>
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
                  color: 'var(--text2)', fontSize: 11,
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
                    color: 'var(--text2)',
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
                {msg.error && (() => {
                  // Spec-039: detect 200K context-wall errors and render a prominent card
                  // with a one-click reset button.
                  //
                  // Detection condition: the error string contains any of the Anthropic API
                  // error codes for context overflow. The CLI forwards the API error text
                  // as `str(exc)`, which includes the error_code and message:
                  //   - "prompt_too_long" — official API error code
                  //   - "prompt is too long" — human-readable message variant
                  //   - "context_length_exceeded" — alternative code seen on some models
                  // Secondary heuristic: context ≥ 195K at the time of error (catches cases
                  // where the exact string is different but the wall is clearly the cause).
                  const errLow = msg.error.toLowerCase()
                  const isWallError = (
                    errLow.includes('prompt_too_long') ||
                    errLow.includes('prompt is too long') ||
                    errLow.includes('context_length_exceeded') ||
                    (contextTokens != null && contextTokens >= contextWindow * 0.95)
                  )
                  if (isWallError) {
                    return (
                      <div style={{
                        marginTop: 8, padding: '10px 14px',
                        background: 'rgba(239,68,68,0.08)',
                        border: '1px solid var(--red)',
                        borderRadius: 6, fontSize: 13,
                        color: 'var(--red)',
                        display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
                      }}>
                        <span style={{ flex: 1, minWidth: 0 }}>
                          🧱 {t['chat.wall_error_msg']}
                        </span>
                        <button
                          style={{
                            fontSize: 12, padding: '3px 10px',
                            cursor: rotating ? 'wait' : 'pointer',
                            background: 'var(--bg-card)',
                            border: '1px solid var(--red)',
                            borderRadius: 4,
                            color: 'var(--red)',
                            fontWeight: 600, whiteSpace: 'nowrap', flexShrink: 0,
                          }}
                          disabled={rotating || streaming}
                          onClick={() => setResetModalOpen(true)}
                        >
                          {rotating ? '…' : t['chat.wall_reset_btn']}
                        </button>
                      </div>
                    )
                  }
                  return <div className="chat-msg-error">⚠ {msg.error}</div>
                })()}
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
                        color: 'var(--text2)',
                        userSelect: 'none', whiteSpace: 'nowrap', overflow: 'hidden',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      {parts.join(' · ')}
                    </div>
                  )
                })()}
                {/* Copy-message button: visible on hover for completed assistant messages */}
                {msg.role === 'assistant' && !msg.streaming && msg.text && (
                  <MsgCopyButton text={msg.text} />
                )}
              </div>
            </div>
          )
        })}

        <div ref={bottomRef} />
        {/* Stick-to-bottom pill: visible only when user has scrolled up and new messages arrive. */}
        {showNewMsgPill && (
          <button
            className="chat-scroll-pill"
            onClick={scrollToBottom}
            aria-label={t['chat.scroll_to_bottom']}
          >
            {t['chat.scroll_to_bottom']}
          </button>
        )}
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
            {attachments.map(a => {
              // Pick an icon: image types get Image, everything else gets File
              const isImage = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(a.name)
              const AttIcon = isImage ? Image : File
              return (
                <div key={a.id} className={`chat-att-chip${a.error ? ' att-error' : a.uploading ? ' att-uploading' : ''}`}>
                  {/* File-type icon */}
                  <span className="att-icon"><AttIcon size={12} /></span>
                  <span className="att-name" title={a.name}>{a.name}</span>
                  {/* Upload progress affordance: spinner while uploading, error icon on failure */}
                  {a.uploading && <span className="att-spinner" aria-label="Uploading…" />}
                  {a.error && <span className="att-err-icon" title={a.error}>⚠</span>}
                  <button className="att-remove" onClick={() => setAttachments(prev => prev.filter(x => x.id !== a.id))} title={t['chat.remove_file']} aria-label={t['chat.remove_file_aria']}>✕</button>
                </div>
              )
            })}
          </div>
        )}
        {dragOver && <div className="chat-drop-hint">📎 Drop files here</div>}
        {/* Spec-035: sub-agent lane — rendered while a run is active and subagents are present */}
        {run && subagents.length > 0 && (
          <div style={{
            padding: '4px 8px',
            borderTop: '1px solid var(--border, #374151)',
            fontSize: 11,
            color: 'var(--text2)',
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
                  <span style={{ color: 'var(--text2)', fontStyle: 'italic' }}>
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
        {/* Live compaction indicator — shown while native auto-compact is running (can be 30–60s).
            Non-blocking inline row using existing .chat-status-bar + .att-spinner classes. */}
        {isCompacting && !run && (
          <div className="chat-status-bar" style={{ margin: '0 0 4px 0' }}>
            <span className="att-spinner" />
            <span>{t['chat.compacting_inprogress']}</span>
          </div>
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
        {/* Context early-warning banner — shown above the composer when context approaches limits.
            Spec-039: framing updated — no mention of auto-rotate (that is gone). The banner is
            ambient-only (dismissible); no popup/modal. Thresholds: amber ~150K, red ~190K. */}
        {(() => {
          const warnTokens = contextTokens != null && contextTokens > 0
            ? contextTokens
            : estimateTokens(messages)
          // Thresholds scale with the real context window: warn at 85%, escalate at 95%.
          const WARN_THRESHOLD = contextWindow * 0.85
          const ESCALATE_THRESHOLD = contextWindow * 0.95
          const isEscalated = warnTokens >= ESCALATE_THRESHOLD
          const isInWarnZone = warnTokens >= WARN_THRESHOLD && !isEscalated
          // Trigger: backend flag OR token-count fallback
          const shouldWarn = contextWarnFromBackend || isInWarnZone || isEscalated
          if (!shouldWarn) return null
          // Dismiss gate: once dismissed, suppress unless we've escalated into the ≥190K zone
          if (warnDismissedAtTokens !== null && !isEscalated) return null
          const nK = Math.round(warnTokens / 1000)
          const bannerColor = isEscalated
            ? 'var(--red)'
            : 'var(--yellow)'
          const bannerBg = isEscalated
            ? 'rgba(239,68,68,0.08)'
            : 'rgba(234,179,8,0.08)'
          // Spec-039: banner text no longer mentions auto-rotate. Uses i18n keys.
          const bannerText = isEscalated
            ? t['chat.ctx_warn_critical'].replace('{nK}', String(nK))
            : t['chat.ctx_warn_approaching'].replace('{nK}', String(nK))
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
                title={t['chat.reset_session_tip']}
                onClick={() => setResetModalOpen(true)}
              >
                {rotating ? '…' : t['chat.reset_session_btn']}
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
          <div className="chat-queue-panel">
            <span className="chat-queue-header">
              ⏭ {queueItems.length} <span className="chat-queue-header-label">{t['chat.queue_panel_label']}</span>
            </span>
            {queueItems.map((item, idx) => (
              <div key={item.id} className="chat-queue-row">
                <span className="chat-queue-idx">{idx + 1}.</span>
                {queueEditId === item.id ? (
                  <>
                    <textarea
                      className="chat-queue-edit-area"
                      value={queueEditText}
                      onChange={e => setQueueEditText(e.target.value)}
                      aria-label={t['chat.queue_item_aria']}
                    />
                    <button
                      className="chat-queue-action-btn chat-queue-save"
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
                      className="chat-queue-action-btn"
                      aria-label={t['chat.queue_cancel_aria']}
                      onClick={() => setQueueEditId(null)}
                    >{t['chat.queue_cancel_btn']}</button>
                  </>
                ) : (
                  <>
                    <span className="chat-queue-text" aria-label={t['chat.queue_item_aria']}>
                      {item.text}
                    </span>
                    <button
                      className="chat-queue-icon-btn"
                      aria-label={t['chat.queue_edit_aria']}
                      title={t['chat.queue_edit_btn']}
                      onClick={() => { setQueueEditId(item.id); setQueueEditText(item.text) }}
                    ><Pencil size={13} /></button>
                    <button
                      className="chat-queue-icon-btn chat-queue-icon-delete"
                      aria-label={t['chat.queue_delete_aria']}
                      title={t['chat.queue_delete_btn']}
                      onClick={() => {
                        api.chatQueueDelete(projectId, item.id)
                          .then(() => setQueueItems(prev => prev.filter(q => q.id !== item.id)))
                          .catch(() => {/* already gone */})
                        if (queueEditId === item.id) setQueueEditId(null)
                      }}
                    ><Trash2 size={13} /></button>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
        {/* Unified composer box: textarea on top, slim bottom bar with icons-left + Send-right */}
        <div className="chat-composer">
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            placeholder={rotating
              ? (rotatingKind === 'handoff' ? 'Compressing session…' : 'Starting new session…')
              : streaming
              ? t['chat.input_placeholder_busy']
              : isTouchDevice ? t['chat.input_placeholder_touch'] : t['chat.input_placeholder']}
            value={input}
            disabled={rotating}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={2}
          />
          <div className="chat-composer-bar">
            <div className="chat-toolbar-tools">
              <button
                className="chat-tool-btn"
                onClick={() => fileInputRef.current?.click()}
                title={t['chat.attach_file_title']}
                aria-label={t['chat.attach_file_aria']}
              ><Paperclip size={15} /></button>
              <button
                className={`chat-tool-btn${showPrompts ? ' active' : ''}`}
                onClick={() => { setShowPrompts(s => !s); setShowSkills(false) }}
                title={t['chat.prompts_title']}
                aria-label={t['chat.prompts_aria']}
              ><ClipboardList size={15} /></button>
              <button
                className={`chat-tool-btn${showSkills ? ' active' : ''}`}
                onClick={() => { setShowSkills(s => !s); setShowPrompts(false) }}
                title={t['chat.skills_title']}
                aria-label={t['chat.skills_aria']}
              ><Wrench size={15} /></button>
              <button
                className="chat-tool-btn"
                disabled={!input.trim() || deferAfterResetBusy}
                title={t['chat.defer_after_reset_title']}
                aria-label={t['chat.defer_aria']}
                onClick={async () => {
                  if (!input.trim()) return
                  setDeferAfterResetBusy(true)
                  try {
                    await api.deferredCreate({ project: project.id, prompt: input, fire_on_reset: true })
                    setInput('')
                    // Attempt to include reset time in toast
                    let toastMsg: string = t['chat.defer_after_reset_toast_plain']
                    try {
                      const usage = await api.usage()
                      const fiveH = usage.limits['five_hour']
                      if (fiveH?.resets_at) {
                        const d = new Date(fiveH.resets_at * 1000)
                        const hh = String(d.getHours()).padStart(2, '0')
                        const mm = String(d.getMinutes()).padStart(2, '0')
                        toastMsg = t['chat.defer_after_reset_toast'].replace('{time}', `${hh}:${mm}`)
                      }
                    } catch { /* usage unavailable — use plain message */ }
                    setDeferToast(toastMsg)
                    setTimeout(() => setDeferToast(null), 4000)
                    await refreshPendingDeferred()
                  } catch (e: unknown) {
                    setDeferToast(e instanceof Error ? e.message : String(e))
                    setTimeout(() => setDeferToast(null), 4000)
                  } finally {
                    setDeferAfterResetBusy(false)
                  }
                }}
              >{deferAfterResetBusy ? '…' : <Clock size={15} />}</button>
            </div>
            {/* Pending deferred runs chip — opens management modal */}
            {pendingDeferred.length > 0 && (
              <button
                className="btn btn-secondary btn-sm"
                title={t['chat.defer_pending_chip_title']}
                style={{ fontSize: 11, padding: '2px 6px', opacity: 0.85 }}
                onClick={() => setShowPendingDeferred(s => !s)}
              >
                <Clock size={13} /> {pendingDeferred.length}
              </button>
            )}
            {/* Mobile session-state cluster: context icon + rate-limit + model + think.
                On desktop these live in the top session bar (see !isMobile gates). */}
            {isMobile && (
              <div className="composer-meta">
                {(() => {
                  const real = contextTokens != null && contextTokens > 0
                  const tokens = real ? contextTokens! : estimateTokens(messages)
                  const fillFrac = Math.min(tokens / contextWindow, 1)
                  const color = fillFrac >= 0.90 ? 'var(--red)' : fillFrac >= 0.75 ? 'var(--yellow)' : 'var(--green)'
                  const warm = run != null || (lastTurnEndMs != null && (Date.now() - lastTurnEndMs) < CACHE_TTL_MS)
                  return (
                    <span className="composer-meta-ctx-wrap">
                      <button
                        type="button"
                        className="composer-meta-ctx"
                        style={{ color }}
                        title={`Context ${real ? '' : '~'}${formatTokens(tokens)} / ${formatMax(contextWindow)} · cache ${warm ? 'warm' : 'cold'}`}
                        onClick={() => setCtxOpen(o => !o)}
                      >
                        {warm ? <Flame size={15} /> : <Snowflake size={15} />}
                        <span className="composer-meta-ctx-num">{real ? '' : '~'}{formatTokens(tokens)}</span>
                      </button>
                      {ctxOpen && (
                        <div className="composer-meta-popover" onClick={() => setCtxOpen(false)}>
                          <div>Context: {real ? '' : '~'}{formatTokens(tokens)} / {formatMax(contextWindow)}</div>
                          <div style={{ color: 'var(--text2)' }}>Cache: {warm ? 'warm' : 'cold'}</div>
                        </div>
                      )}
                    </span>
                  )
                })()}
                <UsageBadge compact />
                <ModelThinkButton
                  model={project.model}
                  thinkValue={thinkMode}
                  disabled={changingModel || streaming}
                  onModelChange={handleModelChange}
                  onThinkChange={handleThinkModeChange}
                />
              </div>
            )}
            <button
              className="btn-primary chat-send-btn"
              disabled={rotating || (!input.trim() && attachments.filter(a => a.path).length === 0)}
              onClick={() => sendMessage()}
              title={streaming ? t['chat.queue_title'] : t['chat.send_title']}
            >
              {streaming ? t['chat.queue'] : t['chat.send']}
            </button>
          </div>
        </div>
      </div>

      {/* Deferred Runs Management Modal */}
      {showPendingDeferred && (
        <Modal onClose={() => { setShowPendingDeferred(false); setEditingDeferredId(null) }}>
          <ModalHead
            title={t['chat.defer_manage_title']}
            onClose={() => { setShowPendingDeferred(false); setEditingDeferredId(null) }}
          />
          <div className="run-modal-body">
            {pendingDeferred.length === 0 ? (
              <p style={{ color: 'var(--text-muted)', fontSize: 13, margin: 0 }}>
                {t['chat.defer_pending_no_items']}
              </p>
            ) : (
              <ul className="chat-defer-mgr-list">
                {(pendingDeferred as Array<Record<string, unknown>>).map(rec => {
                  const id = String(rec['id'])
                  const isEditing = editingDeferredId === id
                  const fireOnReset = Boolean(rec['fire_on_reset'])
                  const fireAt = rec['fire_at'] ? String(rec['fire_at']) : null
                  const waitReason = rec['reset_wait_reason'] ? String(rec['reset_wait_reason']) : null
                  const prompt = String(rec['prompt'] ?? '')

                  return (
                    <li key={id} className="chat-defer-mgr-row">
                      {isEditing ? (
                        // Edit mode
                        <div className="chat-defer-mgr-edit">
                          <textarea
                            className="chat-defer-mgr-edit-textarea"
                            value={editDeferredPrompt}
                            onChange={e => setEditDeferredPrompt(e.target.value)}
                            rows={4}
                          />
                          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                            <button
                              className={`btn btn-sm ${editDeferredMode === 'time' ? 'btn-primary' : 'btn-secondary'}`}
                              onClick={() => setEditDeferredMode('time')}
                            >{t['chat.defer_mode_time']}</button>
                            <button
                              className={`btn btn-sm ${editDeferredMode === 'reset' ? 'btn-primary' : 'btn-secondary'}`}
                              onClick={() => setEditDeferredMode('reset')}
                            >{t['chat.defer_mode_reset']}</button>
                          </div>
                          {editDeferredMode === 'time' && (
                            <input
                              type="datetime-local"
                              value={editDeferredDatetime}
                              onChange={e => setEditDeferredDatetime(e.target.value)}
                              style={{ marginTop: 6, width: '100%', fontSize: 13, padding: '5px 7px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', boxSizing: 'border-box' }}
                            />
                          )}
                          <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                            <button
                              className="btn btn-sm btn-primary"
                              onClick={async () => {
                                try {
                                  const body: Record<string, unknown> = { prompt: editDeferredPrompt }
                                  if (editDeferredMode === 'reset') {
                                    body['fire_on_reset'] = true
                                  } else {
                                    body['fire_at'] = new Date(editDeferredDatetime).toISOString()
                                  }
                                  await api.deferredUpdate(id, body)
                                  await refreshPendingDeferred()
                                  setEditingDeferredId(null)
                                } catch (e) {
                                  setDeferToast(e instanceof Error ? e.message : String(e))
                                  setTimeout(() => setDeferToast(null), 4000)
                                }
                              }}
                            >Save</button>
                            <button
                              className="btn btn-sm btn-secondary"
                              onClick={() => setEditingDeferredId(null)}
                            >Cancel</button>
                          </div>
                        </div>
                      ) : (
                        // View mode
                        <>
                          <div className="chat-defer-mgr-meta">
                            <span className="chat-defer-mgr-trigger">
                              {fireOnReset
                                ? <>↺ {t['chat.defer_mode_reset']}</>
                                : <>🕐 {fireAt ? new Date(fireAt).toLocaleString() : t['chat.defer_mode_time']}</>
                              }
                            </span>
                            {waitReason === 'usage_unavailable' && (
                              <span className="chat-defer-mgr-badge">
                                {t['chat.defer_waiting_usage']}
                              </span>
                            )}
                          </div>
                          <p className="chat-defer-mgr-prompt">{prompt}</p>
                          <div className="chat-defer-mgr-actions">
                            <button
                              className="btn btn-sm btn-secondary"
                              onClick={() => {
                                setEditingDeferredId(id)
                                setEditDeferredPrompt(prompt)
                                setEditDeferredMode(fireOnReset ? 'reset' : 'time')
                                // Pre-fill datetime-local from existing fire_at (strip seconds+ms for input compat)
                                if (fireAt) {
                                  const d = new Date(fireAt)
                                  const pad = (n: number) => String(n).padStart(2, '0')
                                  setEditDeferredDatetime(
                                    `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
                                  )
                                } else {
                                  setEditDeferredDatetime('')
                                }
                              }}
                            >{t['chat.defer_edit']}</button>
                            <button
                              className="btn btn-sm btn-secondary"
                              title={t['chat.defer_pending_cancel']}
                              onClick={async () => {
                                try {
                                  await api.deferredDelete(id)
                                  await refreshPendingDeferred()
                                } catch (e) {
                                  setDeferToast(e instanceof Error ? e.message : String(e))
                                  setTimeout(() => setDeferToast(null), 4000)
                                }
                              }}
                            >{t['chat.defer_pending_cancel']}</button>
                          </div>
                        </>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        </Modal>
      )}

      {/* Defer toast */}
      {deferToast && (
        <div style={{
          position: 'fixed', bottom: 24, right: 24,
          padding: '10px 18px', background: 'var(--bg-card)',
          border: '1px solid var(--border2)', borderRadius: 8,
          fontSize: 13, zIndex: 9999,
        }}>
          {deferToast}
        </div>
      )}

      {/* spec-042: Unified reset-confirm modal — two choices + cancel.
          Opened by every ↺ entry point (toolbar, wall-error button, context banner). */}
      {resetModalOpen && (
        <Modal onClose={() => setResetModalOpen(false)}>
          <ModalHead title="New session" onClose={() => setResetModalOpen(false)} />
          <div className="run-modal-body">
            <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--text2)', lineHeight: 1.5 }}>
              Choose how to start the next session:
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 20 }}>
              <button
                className="btn btn-primary"
                style={{ justifyContent: 'flex-start', textAlign: 'left', padding: '10px 14px' }}
                onClick={() => handleRotate(true)}
                disabled={rotating}
              >
                <strong>New session + handoff</strong>
                <span style={{ display: 'block', fontWeight: 400, fontSize: 11, marginTop: 2, opacity: 0.8 }}>
                  A compact summary of the prior session is built by a cheap model and seeded into the new one — no context is lost.
                </span>
              </button>
              <button
                className="btn btn-secondary"
                style={{ justifyContent: 'flex-start', textAlign: 'left', padding: '10px 14px' }}
                onClick={() => handleRotate(false)}
                disabled={rotating}
              >
                <strong>New session (blank)</strong>
                <span style={{ display: 'block', fontWeight: 400, fontSize: 11, marginTop: 2, opacity: 0.8 }}>
                  Fresh start with no prior context carried over.
                </span>
              </button>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={() => setResetModalOpen(false)}>Cancel</button>
            </div>
          </div>
        </Modal>
      )}

      {/* Spec-021/039: Manual reset toast */}
      {rotateToast && (
        <div style={{
          position: 'fixed', bottom: deferToast ? 72 : 24, right: 24,
          padding: '10px 18px', background: 'var(--bg-card)',
          border: '1px solid var(--border2)', borderRadius: 8,
          fontSize: 13, zIndex: 9999,
        }}>
          ↺ {rotateToast}
        </div>
      )}

      {/* Spec-039: Auto-compact toast — one slot, two states:
          - isCompacting=true  → in-progress: spinner + compacting text, neutral border
          - isCompacting=false → done: ✦ + done text, green border (auto-dismisses after 4s) */}
      {compactToast && (
        <div style={{
          position: 'fixed',
          bottom: deferToast ? 120 : rotateToast ? 72 : 24,
          right: 24,
          padding: '10px 18px', background: 'var(--bg-card)',
          border: isCompacting ? '1px solid var(--border2)' : '1px solid var(--green)',
          borderRadius: 8,
          fontSize: 13, zIndex: 9999,
          color: isCompacting ? 'var(--text2)' : 'var(--green)',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          {isCompacting
            ? <><span className="att-spinner" />{t['chat.compacting_inprogress']}</>
            : <>✦ {t['chat.compact_toast']}</>
          }
        </div>
      )}
    </div>
  )
}

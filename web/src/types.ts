export interface GitHealth {
  branch: string
  dirty: number
  unpushed: number
}

export interface ProjectHealth {
  git: GitHealth | null
}

export interface Project {
  id: string
  name: string
  cwd: string
  model: string
  tg_thread: number | null
  health: ProjectHealth
}

export interface ClaudeMd {
  path: string
  content: string
  exists: boolean
}

export interface Spec {
  name: string
  path: string
}

export interface SpecContent {
  name: string
  content: string
}

export interface TaskCard {
  id: string
  text: string
}

export interface BoardColumn {
  key: string
  label: string
  cards: TaskCard[]
}

export interface Board {
  columns: BoardColumn[]
  done_count: number
  exists: boolean
}

export interface RunResult {
  content: string
  exists: boolean
}

// ─── File Explorer ────────────────────────────────────────────────────────

export interface FileEntry {
  name: string
  type: 'dir' | 'file'
  size: number
}

export interface FileListing {
  path: string
  entries: FileEntry[]
}

export interface FileContent {
  path: string
  content: string
  lang: string
  size: number
  error?: string
}

export type TabId = 'overview' | 'readme' | 'claude-md' | 'specs' | 'activity' | 'chat' | 'board' | 'files'

// ─── C2: Session management ────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string
  last_used: string   // ISO datetime string
  preview: string
  is_active: boolean
}

// ─── C1: Chat SSE events ───────────────────────────────────────────────────

export interface ChatEventText {
  type: 'text'
  text: string
}

export interface ChatEventTool {
  type: 'tool'
  name: string
  input: string
}

export interface ChatEventResult {
  type: 'result'
}

export interface ChatEventError {
  type: 'error'
  error: string
}

export interface ChatEventDone {
  type: 'done'
}

export interface ChatEventRateLimit {
  type: 'rate_limit'
  status: string
}

export type ChatSSEEvent =
  | ChatEventText
  | ChatEventTool
  | ChatEventResult
  | ChatEventError
  | ChatEventDone
  | ChatEventRateLimit

// ─── Chat message (UI state) ───────────────────────────────────────────────

export interface ChatToolCall {
  name: string
  input: string
}

export interface HistoryMessage {
  role: 'user' | 'assistant'
  text: string
  tools: ChatToolCall[]
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  /** Accumulated text content */
  text: string
  /** Tool calls that happened during this turn */
  tools: ChatToolCall[]
  /** True while the SSE stream is still active for this message */
  streaming: boolean
  /** Error message if the turn ended with an error */
  error?: string
}

// ─── Activity bus events (server → client via /activity-stream) ────────────

export interface ActivityEventRunStart {
  kind: 'run_start'
  source: 'card' | string
  prompt: string
  run_id: string
}

export interface ActivityEventText {
  kind: 'text'
  text: string
  run_id: string
}

export interface ActivityEventTool {
  kind: 'tool'
  name: string
  input: string
  run_id: string
}

export interface ActivityEventRunEnd {
  kind: 'run_end'
  outcome: 'ok' | 'fail'
  run_id: string
}

export type ActivityEvent =
  | ActivityEventRunStart
  | ActivityEventText
  | ActivityEventTool
  | ActivityEventRunEnd

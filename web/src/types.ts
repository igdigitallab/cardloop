export interface GitHealth {
  branch: string
  dirty: number
  unpushed: number
}

export interface ProjectHealth {
  git: GitHealth | null
}

export interface ProjectStructureHealth {
  items: { key: string; label: string; ok: boolean; hint: string | null }[]
  score: number
  total: number
  color: 'green' | 'yellow' | 'red'
}

export interface Project {
  id: string
  name: string
  cwd: string
  model: string
  /** Для обычных проектов = number (chat:thread из TG). Для свободных чатов = их строковый id. */
  tg_thread: number | string | null
  health: ProjectHealth
  /** True для свободных чатов (без TG/git, cwd=$HOME). */
  is_free?: boolean
  /** Активные инциденты (err-карточки на доске вне Done). Бейдж 🚨 в сайдбаре. */
  incidents?: number
  /** Сконфигурированы ли источники для сканера ошибок. */
  log_cmd?: string | null
  test_cmd?: string | null
  /** Самолечение (Spec 010): агент авто-чинит новые инциденты в worktree. OFF по умолчанию. */
  self_heal?: boolean
  /** TG-уведомления о новых ошибках («упало»). OFF по умолчанию. */
  notify_on_error?: boolean
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
  description?: string | null
}

/** Карточка-инцидент = id начинается с 'err-'. UI подсвечивает её красной рамкой. */
export function isIncidentCard(card: TaskCard): boolean {
  return card.id.startsWith('err-')
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

// ─── Spec 009: quality gate result ───────────────────────────────────────────

export interface GateTestResult {
  detected: boolean
  ok: boolean
  cmd: string | null
  exit_code: number | null
  output: string
  timed_out: boolean
}

export interface GateResult {
  verdict: 'safe' | 'risky' | 'unknown'
  /** Reason for unknown verdict when no worktree (e.g. "legacy") */
  reason?: string
  tests: GateTestResult | null
  lint: null
}

// ─── C2-gate: worktree run meta ───────────────────────────────────────────

export interface RunMeta {
  card_id: string
  ts: string
  outcome: string
  mode: 'worktree' | 'legacy'
  branch: string | null
  base_branch: string | null
  wt_path: string | null
  has_changes: boolean
  applied: boolean
  discarded: boolean
}

export interface RunResult {
  content: string
  exists: boolean
  meta?: RunMeta | null
}

export interface TestResult {
  detected: boolean
  ok: boolean
  cmd: string | null
  exit_code: number | null
  timed_out?: boolean
  output: string
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

export type TabId = 'overview' | 'claude-md' | 'logs' | 'board' | 'files' | 'memory' | 'secrets' | 'timeline'

// ─── Timeline (Spec 008) ──────────────────────────────────────────────────────

export interface TimelineEvent {
  ts: number
  session_key: string
  kind: 'run_start' | 'tool' | 'text' | 'run_end' | string
  source?: 'card' | 'chat' | 'tg' | string
  run_id?: string
  /** run_start: prompt text */
  prompt?: string
  /** text: accumulated text */
  text?: string
  /** tool: rich tool data */
  tool?: import('./types').RichTool
  /** run_end: outcome */
  outcome?: 'ok' | 'fail'
}

// ─── Project Secrets (Spec 007) ───────────────────────────────────────────────

export interface ProjectSecrets {
  /** Names of keys stored — values are NEVER returned by the API. */
  keys: string[]
  exists: boolean
}

export interface ProjectLogs {
  lines: string[]
  configured: boolean
  cmd?: string | null
}

// ─── Session Context (Feature A) ──────────────────────────────────────────

export interface SessionContext {
  read: string[]
  edited: string[]
  commands: string[]
  session_id: string | null
}

// ─── Project Memory (Feature B) ───────────────────────────────────────────

export interface MemoryFile {
  name: string
  content: string
}

export interface ProjectMemory {
  files: MemoryFile[]
  exists: boolean
}

// ─── C2: Session management ────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string
  last_used: string   // ISO datetime string
  preview: string
  is_active: boolean
  label?: string | null
}

// ─── C1: Chat SSE events ───────────────────────────────────────────────────

export interface ChatEventText {
  type: 'text'
  text: string
}

// Rich tool call — kind discriminates rendering
export interface RichToolBash   { name: string; kind: 'bash';   cmd: string; desc?: string }
export interface RichToolEdit   { name: string; kind: 'edit';   file: string; old?: string; new?: string; count?: number; cell_type?: string }
export interface RichToolWrite  { name: string; kind: 'write';  file: string; preview: string }
export interface RichToolRead   { name: string; kind: 'read';   file: string }
export interface RichToolSearch { name: string; kind: 'search'; pattern: string; path?: string }
export interface RichToolOther  { name: string; kind: 'other';  summary: string }
export type RichTool = RichToolBash | RichToolEdit | RichToolWrite | RichToolRead | RichToolSearch | RichToolOther

export type ChatEventTool = RichTool & { type: 'tool' }

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

export type ChatToolCall = RichTool

export interface HistoryMessage {
  role: 'user' | 'assistant'
  text: string
  tools: RichTool[]
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

// ─── Prompt templates ─────────────────────────────────────────────────────

export interface Prompt {
  id: string
  title: string
  text: string
  category?: string
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
  run_id: string
  tool: RichTool
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

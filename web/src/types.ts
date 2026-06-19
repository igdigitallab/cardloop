export interface GitHealth {
  branch: string
  dirty: number
  unpushed: number
  /** GitHub visibility — private/public repo (null = not on GitHub / not yet determined) */
  visibility?: 'private' | 'public' | null
}

export interface ProjectHealth {
  git: GitHealth | null
}

export interface ProjectStructureHealth {
  items: { key: string; label: string; ok: boolean; hint: string | null; optional?: boolean }[]
  score: number
  total: number
  color: 'green' | 'yellow' | 'red'
}

export interface Project {
  id: string
  name: string
  cwd: string
  model: string
  /** Session key (e.g. "chat:thread" for projects, "free-<uuid>" for free chats). Backend `session_key`. */
  session_key: string
  health: ProjectHealth
  /** True for free chats (no TG/git, cwd=$HOME). */
  is_free?: boolean
  /** Active incidents (err-cards on the board outside Done). 🚨 badge in the sidebar. */
  incidents?: number
  /** Whether log/test sources are configured for the error scanner. */
  log_cmd?: string | null
  test_cmd?: string | null
  /** TG error notifications. OFF by default. */
  notify_on_error?: boolean
  /** Spec-024: virtual group label assigned to this project (null = ungrouped). */
  group?: string | null
  /** Spec-031: whether this project/free-chat is starred. */
  favorite?: boolean
  /** ops:b2a081 — true while an agent turn is in flight for this project. */
  running?: boolean
  /** ops:b2a081 — true when a turn just finished and the operator hasn't opened the tab yet. */
  awaiting?: boolean
}

// ─── Spec-024: Project Groups ────────────────────────────────────────────────

export interface ProjectGroups {
  /** Ordered list of group labels. */
  groups: string[]
  /** Map of project_id → group label. */
  assignments: Record<string, string>
}

export interface ClaudeMd {
  path: string
  content: string
  exists: boolean
}

export interface TaskCard {
  id: string
  text: string
  description?: string | null
  /** Card 43665f: optional per-card model override (opus/sonnet/haiku/fable). */
  model?: string | null
  /** Card 5e1c0a: true when a spec sidecar (data/card-specs/<id>.md) exists. */
  has_spec?: boolean
}

/** GET /api/projects/{id}/cards/{card}/spec response. */
export interface CardSpec {
  exists: boolean
  content: string
}

/** Incident card = id starts with 'err-'. UI highlights it with a red border. */
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
  /** card_ids queued for sequential agent execution */
  queued?: string[]
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

export type TabId = 'claude-md' | 'logs' | 'board' | 'files' | 'memory' | 'timeline' | 'settings'

// ─── Settings (card f2ba02) ───────────────────────────────────────────────────

// ─── Spec 017 Phase C: per-project sub-agent config ──────────────────────────
export interface AgentsConfig {
  executor_model?: string
  researcher_model?: string
  quick_model?: string
  conductor_prompt?: boolean
}

export interface ProjectSettings {
  git_enabled: boolean
  model: string | null
  notify_on_error: boolean
  log_cmd: string
  test_cmd: string
  agents_config: AgentsConfig
}

export interface GlobalSettingsEffective {
  scan_interval_sec: number
  default_model: string
  watchdog_stall_sec: number
  watchdog_max_sec: number
  // Board reconciler settings (Task A)
  board_reconcile_enabled?: boolean
  board_reconcile_on_match?: 'done' | 'review'
  /** Card 43665f: default model for board-card agent runs. '' = use sonnet. */
  board_card_model?: string
}

export interface GlobalSettings {
  stored: Record<string, unknown>
  effective: GlobalSettingsEffective
  spec: Record<string, { type: string; min: number | null; max: number | null }>
}

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

// ─── Spec-037: Multi-chat per project ─────────────────────────────────────

/** A single named chat thread within a project. */
export interface Chat {
  id: string
  name: string
  session_id: string | null
  created_at: number
}

export interface ChatsResponse {
  active: string
  chats: Chat[]
}

// ─── C2: Session management ────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string
  last_used: string   // ISO datetime string (file mtime — used as session timestamp)
  preview: string
  is_active: boolean
  label?: string | null
  /** ISO datetime of session creation — provided by backend when available (spec-042). */
  created?: string | null
  /** Total message count in the session — provided by backend when available (spec-042). */
  message_count?: number | null
}

// ─── C1: Chat SSE events ───────────────────────────────────────────────────

export interface ChatEventText {
  type: 'text'
  text: string
}

// Spec-029 §1: incremental text delta for live streaming preview.
// Deltas arrive before the finalised {type:"text"} block and are used to
// update the in-progress assistant bubble in real time. The finalised text
// block remains the source of truth; the UI reconciles on receipt.
export interface ChatEventTextDelta {
  type: 'text_delta'
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
  context_tokens?: number
  context_window?: number
  cache_read_tokens?: number | null
  fresh_tokens?: number | null
  prompt_tokens?: number | null
  cache_hit_pct?: number | null
  duration_ms?: number | null
  utilization?: number | null
  /** Early-warning flag: backend sets true when context is in the ≈150K–175K zone. */
  context_warn?: boolean
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

/** Spec-041 A3: backend was busy and enqueued the prompt instead of starting a turn. */
export interface ChatEventQueued {
  type: 'queued'
  item: { id: string; text: string; created_at: number }
}

export type ChatSSEEvent =
  | ChatEventText
  | ChatEventTextDelta
  | ChatEventTool
  | ChatEventResult
  | ChatEventError
  | ChatEventDone
  | ChatEventRateLimit
  | ChatEventQueued

// ─── Chat message (UI state) ───────────────────────────────────────────────

export type ChatToolCall = RichTool

export interface HistoryMessage {
  role: 'user' | 'assistant'
  text: string
  tools: RichTool[]
}

/** Response shape for GET /api/projects/{id}/session-history */
export interface SessionHistoryResponse {
  messages: HistoryMessage[]
  session_id: string | null
  context_tokens?: number
  context_window?: number
  /** Unix milliseconds of the last assistant turn (from transcript timestamp or file mtime). null if no assistant turn. */
  last_turn_at?: number | null
  /** Cache-hit % of the last assistant turn: round(cache_read / (cache_read + input_tokens) * 100). null if no usage. */
  last_cache_hit_pct?: number | null
}

/** Per-turn cost metrics stamped onto assistant messages when result event arrives. */
export interface TurnMetrics {
  cache_hit_pct: number
  prompt_tokens: number
  cache_read_tokens: number
  fresh_tokens: number
  duration_ms: number | null
  /** Subscription utilization % (0-100), null when not reported by this turn. */
  utilization?: number | null
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
  /** Unix timestamp (ms) when the message was created / result arrived */
  ts?: number
  /** Cost visibility metrics — stamped when result event arrives (assistant only) */
  metrics?: TurnMetrics
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

// ─── Spec-035 L4: sub-agent bus event ────────────────────────────────────────
// Arrives via /activity-stream from either:
//   - TG consumer (bot.py:1727): {kind:"subagent", run_id:null, type:"subagent", ...}
//   - Chat path (webapp.py L1 publish): {seq:N, type:"subagent", ...} (no kind field)
// We treat both: check kind==="subagent" OR type==="subagent".
export interface ActivityEventSubagent {
  kind: 'subagent'
  run_id: string | null
  // engine fields
  type?: string
  subtype: 'started' | 'progress' | 'notification'
  task_id: string
  description: string | null
  status: string | null
  summary: string | null
  last_tool_name: string | null
  // seq from live turn buffer (optional — may be absent on older TG-sourced events)
  seq?: number
}

/** Spec-035 L3: response from GET /api/projects/{id}/live */
export interface LiveTurnSnapshot {
  running: boolean
  turn_id: string | null
  /** Server wall-clock epoch (seconds, float) — the one timer source of truth. */
  started_at: number | null
  model: string | null
  cost_usd: number | null
  /** Latest seq in the buffer — subscribe activity-stream from here to avoid duplicates. */
  cursor: number
  /** Buffered events in chronological order (oldest to newest). */
  events: Array<Record<string, unknown>>
}

// ─── Spec-039: native auto-compact notification ────────────────────────────────
// Emitted by the PreCompact hook in bot.py when the CLI compacts in place.
// The session is kept — only a toast is shown in the cockpit.
export interface ActivityEventCompact {
  kind: 'compact'
  trigger: string
  project: string
}

export type ActivityEvent =
  | ActivityEventRunStart
  | ActivityEventText
  | ActivityEventTool
  | ActivityEventRunEnd
  | ActivityEventSubagent
  | ActivityEventCompact

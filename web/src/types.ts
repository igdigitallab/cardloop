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

export interface ProjectCapability {
  key: string
  label: string
  on: boolean
  hint: string
}
export interface ProjectStructureHealth {
  archetype: string
  capabilities: ProjectCapability[]
  security_warn: boolean
  security_hint: string | null
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

export type TabId = 'claude-md' | 'logs' | 'board' | 'files' | 'memory' | 'timeline' | 'settings' | 'specs' | 'browser'

// ─── Epic-lens: Spec list (GET /api/projects/{id}/epic-specs) ─────────────────

export interface EpicSpecCard {
  id: string
  text: string
  column?: string
}

export interface EpicSpec {
  spec_id: string
  title: string
  status: string
  name: string
  cards: {
    open: EpicSpecCard[]
    done: EpicSpecCard[]
  }
  done_count: number
  total: number
  progress: number
}

export interface EpicSpecsResp {
  specs: EpicSpec[]
}

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
  /** spec-051: resume policy after a rate-limit. ask (default) / always / never. */
  auto_resume_mode: 'ask' | 'always' | 'never'
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
  /** session_rotated / auto_rotated: why the session was rotated + the context at the time */
  trigger?: 'auto' | 'manual' | string
  context_tokens?: number
  threshold?: number
  handoff?: boolean
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
  /** Context token count (input + cache) from the last assistant turn in the session. */
  context_tokens?: number | null
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
  /** Absolute cost-management thresholds (decoupled from the window): warn-banner + auto-rotate. */
  context_warn_at?: number
  context_rotate_at?: number
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
  role: 'user' | 'assistant' | 'board'
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
  /** Spec-052: board event payload — present only when role === 'board' */
  board?: ActivityEventBoard
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
  /** The user prompt that started this turn — used to reconstruct the user bubble on
   *  hydration when the run_start was missed (queued / card / deferred runs). */
  prompt?: string
  /** Latest seq in the buffer — subscribe activity-stream from here to avoid duplicates. */
  cursor: number
  /** Buffered events in chronological order (oldest to newest). */
  events: Array<Record<string, unknown>>
  /** spec-052 Phase 7: recent actionable board strips, re-injected into the chat feed
   *  on hydration so they survive reload / inactive tab / mid-stream. */
  board_events?: ActivityEventBoard[]
  /** Pending handoff summary from the previous session (if any). */
  pending_handoff?: string | null
}

// ─── Spec-039: native auto-compact notification ────────────────────────────────
// Emitted by the PreCompact hook in bot.py when the CLI compacts in place.
// The session is kept — only a toast is shown in the cockpit.
export interface ActivityEventCompact {
  kind: 'compact'
  trigger: string
  project: string
}

// ─── Background-task monitors (card b6f5cc) ────────────────────────────────────
// Long-running shells / Monitor / Workflow tasks the agent started. Live updates arrive
// as {kind:"monitor", monitor: Monitor}; snapshot via GET /api/projects/{id}/monitors.
export interface Monitor {
  id: string
  kind: 'bash' | 'monitor' | 'workflow' | 'task' | string
  label: string
  status: 'running' | 'stopped' | 'done' | 'failed' | string
  started: number
  ts: number
  tail?: string
  agent?: string | null
  persistent?: boolean
  /** Set on a bus event when the monitor was dismissed/cleared — drop it from the list. */
  removed?: boolean
}

export interface ActivityEventMonitor {
  kind: 'monitor'
  monitor: Monitor
}

// Emitted by _maybe_auto_rotate when a session crosses CONTEXT_ROTATE_AT and is rotated
// WITH a handoff to cap re-bill cost. The session is reset; the next turn injects the handoff.
export interface ActivityEventAutoRotated {
  kind: 'auto_rotated'
  source: string
  context_tokens: number
  threshold: number
  handoff: boolean
}

// spec-051: rate-limit hit mid-run; project policy is "ask" → prompt operator
// to auto-resume when the window resets. Resolved via api.deferredConfirm.
export interface ActivityEventRateLimitPrompt {
  kind: 'rate_limit_prompt'
  deferred_id: string
  project: string
  session_key: string
  resets_at_display: string
  original_prompt_preview: string
}

// ─── Spec-052: board event surfaced in chat stream ────────────────────────────
// Emitted by the backend into each project's activity stream when a card changes
// state, an incident fires, a kanban run starts/ends, or a reconcile occurs.
export interface ActivityEventBoard {
  kind: 'board_event'
  event: 'incident' | 'moved' | 'run_start' | 'run_end' | 'reconcile'
  card_id: string
  title: string
  column_from: string | null
  column_to: string | null
  severity: 'info' | 'success' | 'error'
  summary: string
  ts: number
}

export type ActivityEvent =
  | ActivityEventRunStart
  | ActivityEventText
  | ActivityEventTool
  | ActivityEventRunEnd
  | ActivityEventSubagent
  | ActivityEventCompact
  | ActivityEventMonitor
  | ActivityEventRateLimitPrompt
  | ActivityEventBoard
  | ActivityEventAutoRotated

export interface VersionInfo {
  current: string
  latest: string | null
  behind: number
  update_available: boolean
  channel: string
  can_self_update: boolean
  reason: string | null
  update_status: { state: string; detail: string; ts: number } | null
}

// ─── Spec-065: module/extension registry ──────────────────────────────────────
export interface Module {
  id: string
  name: string
  description: string
  version: string
  provides: string[]
  enabled: boolean
  // spec-066: per-module config (the browser module carries the backend selection).
  config?: Record<string, unknown>
}

// ─── Spec-066: pluggable browser backends ─────────────────────────────────────
export interface BrowserConfig {
  backend: 'builtin' | 'cloakbrowser' | 'external-cdp'
  cdp_url: string
  manager_url: string
  default_profile: string
  per_project_profile: Record<string, string>
  agent_actions: 'read' | 'full'
  proxy?: string
  geoip?: boolean
  humanize?: boolean
  timezone?: string
  locale?: string
}

export interface CloakProfile {
  id: string
  name: string
  status: string
}

// GET /api/browser/backends response.
export interface BrowserBackends {
  current?: { backend: string; agent_actions: string }
  tiers: {
    builtin: { available: boolean }
    cloakbrowser: { installed: boolean; binary_ready: boolean; version: string | null }
    'external-cdp': { available: boolean }
  }
  manager: { configured: boolean; url: string | null; token_set: boolean }
  config: {
    backend: string
    cdp_url: string
    manager_url: string
    default_profile: string
    agent_actions: string
  }
  install_log?: string
  error?: string
}

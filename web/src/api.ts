const OPTS: RequestInit = { credentials: 'include' }

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...OPTS, ...init })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    // Attach parsed JSON body (if any) so callers can inspect error codes
    let body: Record<string, unknown> | null = null
    try { body = JSON.parse(text) } catch { /* ignore */ }
    throw Object.assign(new Error(text), { status: res.status, body })
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => apiFetch<{ ok: boolean }>('/api/health'),

  me: () => apiFetch<{ authed: boolean }>('/api/me'),

  // Live model registry — display labels resolved from /v1/models (subscription),
  // falling back to bundled static labels (web/src/lib/models.ts) when unavailable.
  models: () =>
    apiFetch<{ source: 'live' | 'static'; models: { value: string; label: string }[] }>('/api/models'),

  login: (password: string, totp?: string) =>
    apiFetch<{ ok: boolean }>('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(totp ? { password, totp } : { password }),
    }),

  logout: () =>
    apiFetch<{ ok: boolean }>('/api/logout', { method: 'POST' }),

  version: (check?: boolean) =>
    apiFetch<import('./types').VersionInfo>(`/api/version${check ? '?check=1' : ''}`),

  update: () =>
    apiFetch<{ status: string; error?: string }>('/api/update', { method: 'POST' }),

  projects: () =>
    apiFetch<{ projects: import('./types').Project[] }>('/api/projects'),

  newProject: (intent?: string, type?: string) =>
    apiFetch<{ id: string; name: string; session_key: string; cwd: string; started: boolean }>(
      '/api/projects/new',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intent: intent || '', type: type || '' }),
      }
    ),

  claudeMd: (id: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/claude-md`),

  saveClaudeMd: (id: string, content: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/claude-md`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  // Card 931573: global (home) agent-rules CLAUDE.md — view + edit. The id arg is ignored
  // (kept so the EditableMarkdown load/save signatures are satisfied unchanged).
  globalClaudeMd: (_id: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/global/claude-md`),

  saveGlobalClaudeMd: (_id: string, content: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/global/claude-md`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  projectLogs: (id: string) =>
    apiFetch<import('./types').ProjectLogs>(`/api/projects/${id}/logs`),

  // Card b6f5cc: background-task monitors (long-running shells / Monitor / Workflow tasks).
  monitors: (id: string) =>
    apiFetch<{ monitors: import('./types').Monitor[] }>(`/api/projects/${id}/monitors`),

  // Dismiss a lingering monitor row (does not kill the shell — read-only clear).
  dismissMonitor: (id: string, mid: string) =>
    apiFetch<{ ok: boolean; removed: boolean }>(`/api/projects/${id}/monitors/${mid}`, { method: 'DELETE' }),

  tasks: (id: string) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks`),

  createTask: (id: string, text: string, column?: string, description?: string | null) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, column, ...(description != null ? { description } : {}) }),
    }),

  moveTask: (id: string, card: string, to: string) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks/${card}/move`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to }),
    }),

  deleteTask: (id: string, card: string) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks/${card}`, {
      method: 'DELETE',
    }),

  updateTask: (
    id: string,
    card: string,
    text: string,
    description?: string | null,
    /** Card 43665f: per-card model override. undefined = don't touch; '' = clear. */
    model?: string | null,
  ) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks/${card}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        ...(description !== undefined ? { description } : {}),
        ...(model !== undefined ? { model } : {}),
      }),
    }),

  tasksDone: (id: string) =>
    apiFetch<{ content: string; exists: boolean }>(`/api/projects/${id}/tasks/done`),

  cardRun: (id: string, card: string) =>
    apiFetch<import('./types').RunResult>(`/api/projects/${id}/tasks/${card}/run`),

  // Card 5e1c0a: card spec sidecar
  getCardSpec: (id: string, card: string) =>
    apiFetch<import('./types').CardSpec>(`/api/projects/${id}/cards/${card}/spec`),

  putCardSpec: (id: string, card: string, content: string) =>
    apiFetch<import('./types').CardSpec>(`/api/projects/${id}/cards/${card}/spec`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  // Batch-send cards to agent → sequential queue (one at a time)
  runBatch: (id: string, cardIds: string[]) =>
    apiFetch<{ ok: boolean; queued: number; started: string | null }>(`/api/projects/${id}/cards/run-batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ card_ids: cardIds }),
    }),

  // Spec 009: quality gate — run tests in the card's worktree and get a verdict
  checkCard: (id: string, card: string) =>
    apiFetch<import('./types').GateResult>(
      `/api/projects/${id}/tasks/${card}/check`,
      { method: 'POST' }
    ),

  applyCard: (id: string, card: string) =>
    apiFetch<{ ok: boolean; applied: boolean; card_id: string }>(
      `/api/projects/${id}/tasks/${card}/apply`,
      { method: 'POST' }
    ),

  discardCard: (id: string, card: string) =>
    apiFetch<{ ok: boolean; discarded: boolean; card_id: string }>(
      `/api/projects/${id}/tasks/${card}/discard`,
      { method: 'POST' }
    ),

  files: (id: string, path: string) =>
    apiFetch<import('./types').FileListing>(
      `/api/projects/${id}/files?path=${encodeURIComponent(path)}`
    ),

  file: (id: string, path: string) =>
    apiFetch<import('./types').FileContent>(
      `/api/projects/${id}/file?path=${encodeURIComponent(path)}`
    ),

  // Spec-037: multi-chat per project
  chats: (id: string) =>
    apiFetch<import('./types').ChatsResponse>(`/api/projects/${id}/chats`),

  createChat: (id: string, name?: string) =>
    apiFetch<import('./types').Chat>(`/api/projects/${id}/chats`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(name ? { name } : {}),
    }),

  patchChat: (id: string, chatId: string, patch: { name?: string; active?: boolean }) =>
    apiFetch<{ active: string; chat: import('./types').Chat }>(
      `/api/projects/${id}/chats/${encodeURIComponent(chatId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }
    ),

  deleteChat: (id: string, chatId: string) =>
    apiFetch<{ ok: boolean; active: string }>(
      `/api/projects/${id}/chats/${encodeURIComponent(chatId)}`,
      { method: 'DELETE' }
    ),

  // C2: session management
  sessions: (id: string) =>
    apiFetch<{ sessions: import('./types').SessionInfo[] }>(`/api/projects/${id}/sessions`),

  // spec-042: rotate with optional handoff flag.
  // handoff=true → backend builds a cheap summary and seeds the next session.
  // handoff=false (or omitted) → blank reset (prior behaviour).
  rotate: (id: string, handoff: boolean) =>
    apiFetch<{ ok: boolean; reset: boolean; handoff: boolean }>(
      `/api/projects/${id}/rotate`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ handoff }),
      }
    ),

  setSession: (id: string, body: { action: 'new' } | { action: 'resume'; session_id: string }) =>
    apiFetch<{ active: string | null }>(`/api/projects/${id}/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // #2: manual label for any session (empty = remove)
  setSessionLabel: (id: string, sid: string, label: string) =>
    apiFetch<{ ok: boolean; session_id: string; label: string | null }>(
      `/api/projects/${id}/sessions/${encodeURIComponent(sid)}/label`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label }),
      }
    ),

  // #4: run project tests
  runTests: (id: string) =>
    apiFetch<import('./types').TestResult>(`/api/projects/${id}/test`, {
      method: 'POST',
    }),

  sessionHistory: (id: string, sessionId?: string) =>
    apiFetch<import('./types').SessionHistoryResponse>(
      `/api/projects/${id}/session-history${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`
    ),

  // C1-stop: interrupt the running agent on the server
  stopChat: (id: string) =>
    apiFetch<{ ok: boolean; stopped: boolean }>(`/api/projects/${id}/chat/stop`, {
      method: 'POST',
    }),

  // ops:b2a081 — mark project as seen (clears attention badge on background tabs)
  projectSeen: (id: string) =>
    apiFetch<{ ok: boolean; awaiting: boolean }>(`/api/projects/${id}/seen`, {
      method: 'POST',
    }),

  // Chat message queue — server-side persist; survives page reload
  chatQueue: (id: string) =>
    apiFetch<{ items: Array<{ id: string; text: string; created_at: number }> }>(
      `/api/projects/${id}/chat/queue`
    ),
  chatQueueAdd: (id: string, text: string) =>
    apiFetch<{ item: { id: string; text: string; created_at: number } }>(
      `/api/projects/${id}/chat/queue`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }
    ),
  chatQueueEdit: (id: string, msgId: string, text: string) =>
    apiFetch<{ item: { id: string; text: string; created_at: number } }>(
      `/api/projects/${id}/chat/queue/${encodeURIComponent(msgId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }
    ),
  chatQueueDelete: (id: string, msgId: string) =>
    apiFetch<{ ok: boolean }>(
      `/api/projects/${id}/chat/queue/${encodeURIComponent(msgId)}`,
      { method: 'DELETE' }
    ),

  // Check whether there is an active run (for restoring UI after refresh)
  projectRunning: (id: string) =>
    apiFetch<{ running: boolean }>(`/api/projects/${id}/running`),

  // Spec-035 L3: cold-open snapshot — current LiveTurn buffer (running state + event history)
  projectLive: (id: string) =>
    apiFetch<import('./types').LiveTurnSnapshot>(`/api/projects/${id}/live`),

  // Agent skills (global + project)
  projectSkills: (id: string) =>
    apiFetch<{
      global: { name: string; description: string }[]
      project: { name: string; description: string }[]
    }>(`/api/projects/${id}/skills`),

  // Incident scanner: manual trigger + count of active err-cards on the board
  scanErrors: (id: string) =>
    apiFetch<{ ok: boolean; scanned: number; added: number; updated: number; error?: string }>(
      `/api/projects/${id}/scan-errors`, { method: 'POST' }
    ),
  projectIncidents: (id: string) =>
    apiFetch<{ count: number; by_column: Record<string, number> }>(
      `/api/projects/${id}/incidents`
    ),

  // Feature B: project memory files
  memory: (id: string) =>
    apiFetch<import('./types').ProjectMemory>(`/api/projects/${id}/memory`),

  saveMemory: (id: string, name: string, content: string) =>
    apiFetch<import('./types').ProjectMemory>(`/api/projects/${id}/memory/${encodeURIComponent(name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  deleteMemory: (id: string, name: string) =>
    apiFetch<import('./types').ProjectMemory>(`/api/projects/${id}/memory/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),

  // Free chats (not bound to a project)
  freeCreate: (body?: { cwd?: string; model?: string; label?: string }) =>
    apiFetch<{ id: string; label: string; cwd: string; model: string; created_at: number }>(
      '/api/free',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      }
    ),

  freeRename: (id: string, label: string) =>
    apiFetch<{ ok: boolean; id: string; label: string }>(`/api/free/${id}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    }),

  freeDelete: (id: string) =>
    apiFetch<{ ok: boolean }>(`/api/free/${id}`, { method: 'DELETE' }),

  // Claude Code subscription limits (rate_limits SDK, updated passively)
  usage: () =>
    apiFetch<{
      limits: Record<string, { status: string; resets_at: number | null; utilization: number | null; ts: number }>
      now: number
    }>('/api/usage'),

  // Change project model (fable/opus/sonnet/haiku) — takes effect on the next request
  setModel: (id: string, model: 'fable' | 'opus' | 'sonnet' | 'haiku') =>
    apiFetch<{ ok: boolean; model: string; topics_updated: number }>(
      `/api/projects/${id}/model`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      }
    ),

  // Global file browser (from $HOME)
  globalFiles: (path: string) =>
    apiFetch<import('./types').FileListing>(
      `/api/global/files?path=${encodeURIComponent(path)}`
    ),

  globalFile: (path: string) =>
    apiFetch<import('./types').FileContent>(
      `/api/global/file?path=${encodeURIComponent(path)}`
    ),

  globalFileWrite: (path: string, content: string) =>
    apiFetch<{ ok: boolean; path: string }>(
      `/api/global/file?path=${encodeURIComponent(path)}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      }
    ),

  // Prompt templates
  prompts: () =>
    apiFetch<{ prompts: import('./types').Prompt[] }>('/api/prompts'),

  createPrompt: (body: { title: string; text: string; category?: string }) =>
    apiFetch<{ prompt: import('./types').Prompt }>('/api/prompts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  deletePrompt: (id: string) =>
    apiFetch<{ ok: boolean }>(`/api/prompts/${id}`, { method: 'DELETE' }),

  updatePrompt: (id: string, body: { title?: string; text?: string; category?: string }) =>
    apiFetch<{ prompt: import('./types').Prompt }>(`/api/prompts/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Project structure health check
  projectHealth: (id: string) =>
    apiFetch<import('./types').ProjectStructureHealth>(`/api/projects/${id}/health`),

  renameProject: (id: string, slug: string) =>
    apiFetch<{ ok: boolean; new_id: string; new_cwd: string; new_name?: string }>(
      `/api/projects/${id}/rename`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slug }) }
    ),

  auditProject: (id: string) =>
    apiFetch<{ ok: boolean; card_id: string }>(`/api/projects/${id}/audit`, { method: 'POST' }),

  upgradeProject: (id: string) =>
    apiFetch<{ ok: boolean; card_id: string }>(`/api/projects/${id}/upgrade`, { method: 'POST' }),

  // Spec 007: project secrets — values are NEVER returned, only key names
  secrets: (id: string) =>
    apiFetch<import('./types').ProjectSecrets>(`/api/projects/${id}/secrets`),

  setSecret: (id: string, key: string, value: string) =>
    apiFetch<import('./types').ProjectSecrets>(`/api/projects/${id}/secrets/${encodeURIComponent(key)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    }),

  deleteSecret: (id: string, key: string) =>
    apiFetch<import('./types').ProjectSecrets>(`/api/projects/${id}/secrets/${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),

  // Spec 008: Timeline — project bus event history (JSONL log)
  timeline: (id: string, opts?: { limit?: number; before?: number }) => {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set('limit', String(opts.limit))
    if (opts?.before != null) params.set('before', String(opts.before))
    const qs = params.toString()
    return apiFetch<{ events: import('./types').TimelineEvent[] }>(
      `/api/projects/${id}/timeline${qs ? `?${qs}` : ''}`
    )
  },

  // Git: commit (if dirty) + push in one button
  gitSync: (id: string, message?: string) =>
    apiFetch<{ ok: boolean; committed: boolean; pushed: boolean; message: string | null; log: string }>(
      `/api/projects/${id}/git/sync`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(message ? { message } : {}),
      }
    ),

  // Settings (card f2ba02): global + per-project
  settings: () =>
    apiFetch<import('./types').GlobalSettings>(`/api/settings`),

  saveSettings: (partial: Record<string, unknown>) =>
    apiFetch<{ ok: boolean; stored: Record<string, unknown> }>(`/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(partial),
    }),

  projectSettings: (id: string) =>
    apiFetch<import('./types').ProjectSettings>(`/api/projects/${id}/settings`),

  saveProjectSettings: (id: string, partial: Partial<import('./types').ProjectSettings>) =>
    apiFetch<{ ok: boolean; topics_updated: number; settings: import('./types').ProjectSettings }>(
      `/api/projects/${id}/settings`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(partial),
      }
    ),

  // TG error notifications — enable/disable per-project
  toggleNotifyOnError: (id: string, enabled: boolean) =>
    apiFetch<{ ok: boolean; notify_on_error: boolean; topics_updated: number }>(
      `/api/projects/${id}/notify-on-error`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }
    ),

  // spec-067: Autopilot — per-project mode
  setAutopilotMode: (id: string, mode: 'off' | 'propose' | 'auto') =>
    apiFetch<{ mode: 'off' | 'propose' | 'auto' }>(
      `/api/projects/${id}/autopilot`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      }
    ),

  // spec-067: Autopilot — global status + controls
  autopilotStatus: () =>
    apiFetch<import('./types').AutopilotStatus>('/api/autopilot/status'),

  setAutopilotGlobal: (enabled: boolean) =>
    apiFetch<import('./types').AutopilotStatus>('/api/autopilot/global', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),

  autopilotPause: () =>
    apiFetch<import('./types').AutopilotStatus>('/api/autopilot/pause', { method: 'POST' }),

  autopilotResume: () =>
    apiFetch<import('./types').AutopilotStatus>('/api/autopilot/resume', { method: 'POST' }),

  // Cross-device UI layout (open tabs/active/sidebar/split) — server source of truth
  uiState: () =>
    apiFetch<{ state: Record<string, unknown> }>('/api/ui-state'),

  saveUiState: (state: Record<string, unknown>) =>
    apiFetch<{ ok: boolean }>('/api/ui-state', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state }),
    }),

  // Spec-019: Schedules registry
  schedules: (qs = '') =>
    apiFetch<import('./tabs/SchedulesTab').SchedulesResponse>(`/api/schedules${qs}`),

  schedulesScan: () =>
    apiFetch<{ queued: boolean }>('/api/schedules/scan', { method: 'POST' }),

  schedulesInvestigate: (id: string) =>
    apiFetch<{ card_id: string }>(`/api/schedules/${encodeURIComponent(id)}/investigate`, { method: 'POST' }),

  // Spec-020: Deferred Runs
  deferredCreate: (body: Record<string, unknown>) =>
    apiFetch<{ id: string; status: string }>('/api/deferred', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  deferredList: (qs = '') =>
    apiFetch<unknown[]>(`/api/deferred${qs}`),

  deferredDelete: (id: string) =>
    apiFetch<{ cancelled: boolean }>(`/api/deferred/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // spec-051: resolve an awaiting_confirmation auto-resume record
  deferredConfirm: (id: string, body: { decision: 'yes' | 'no'; remember: boolean }) =>
    apiFetch<{ ok: boolean; status: string; remembered: string | null; noop?: boolean }>(
      `/api/deferred/${encodeURIComponent(id)}/confirm`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    ),

  deferredUpdate: (id: string, body: Record<string, unknown>) =>
    apiFetch<Record<string, unknown>>(`/api/deferred/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),

  // Spec-023: Project Archive
  archiveProject: (id: string) =>
    apiFetch<{ archived: boolean }>(`/api/projects/${id}/archive`, { method: 'POST' }),

  unarchiveProject: (id: string) =>
    apiFetch<{ archived: boolean }>(`/api/projects/${id}/unarchive`, { method: 'POST' }),

  archivedProjects: () =>
    apiFetch<{ projects: { id: string; name: string; cwd: string }[] }>('/api/projects/archived'),

  // Spec-025: Project Delete
  deletePrecheck: (id: string) =>
    apiFetch<{ is_git: boolean; uncommitted_count: number; unpushed_count: number; branch: string | null; has_remote: boolean }>(
      `/api/projects/${id}/delete-precheck`
    ),

  deleteProject: (id: string, confirmName: string) =>
    apiFetch<{ deleted: boolean; trash_path: string; purge_at: string }>(
      `/api/projects/${id}/delete`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_name: confirmName }),
      }
    ),

  trash: () =>
    apiFetch<{ trash: Array<{ entry: string; id: string; name: string; original_cwd: string; deleted_at: string; days_left: number }> }>(
      '/api/trash'
    ),

  restoreTrash: (entry: string) =>
    apiFetch<{ restored: boolean; cwd: string }>(
      `/api/trash/${encodeURIComponent(entry)}/restore`,
      { method: 'POST' }
    ),

  // Spec-024: Project Groups
  projectGroups: () =>
    apiFetch<import('./types').ProjectGroups>('/api/project-groups'),

  setProjectGroup: (id: string, group: string | null) =>
    apiFetch<{ ok: boolean }>(`/api/projects/${id}/group`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group }),
    }),

  manageGroups: (groups: string[]) =>
    apiFetch<{ ok: boolean }>('/api/project-groups', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ groups }),
    }),

  createGroup: (name: string) =>
    apiFetch<{ groups: string[]; assignments: Record<string, string> }>('/api/project-groups/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),

  renameGroup: (from: string, to: string) =>
    apiFetch<{ groups: string[]; assignments: Record<string, string> }>('/api/project-groups/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, to }),
    }),

  deleteGroup: (name: string) =>
    apiFetch<{ groups: string[]; assignments: Record<string, string> }>('/api/project-groups/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),

  reorderGroups: (order: string[]) =>
    apiFetch<{ groups: string[]; assignments: Record<string, string> }>('/api/project-groups/reorder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order }),
    }),

  // Spec-031: Favorites
  setFavorite: (id: string, favorite: boolean) =>
    apiFetch<{ ok: boolean }>(`/api/projects/${id}/favorite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favorite }),
    }),

  // Spec-026 Phase 2: TOTP 2FA
  totpStatus: () =>
    apiFetch<{ enabled: boolean }>('/api/auth/totp/status'),

  totpEnroll: () =>
    apiFetch<{ secret: string; otpauth_uri: string; recovery_codes: string[] }>('/api/auth/totp/enroll', {
      method: 'POST',
    }),

  totpActivate: (code: string) =>
    apiFetch<{ enabled: boolean }>('/api/auth/totp/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    }),

  totpDisable: () =>
    apiFetch<{ enabled: boolean }>('/api/auth/totp', { method: 'DELETE' }),

  // Spec-026 Phase 3: Global encrypted secret vault (names+categories only — no values)
  secretsList: () =>
    apiFetch<{ secrets: Array<{ name: string; category: string }> }>('/api/secrets'),

  secretReveal: (name: string) =>
    apiFetch<{ name: string; value: string; category: string; notes: string; updated_at: string }>(
      `/api/secrets/${encodeURIComponent(name)}`
    ),

  secretSet: (body: { name: string; value: string; category?: string; notes?: string }) =>
    apiFetch<{ name: string; category: string; ok: true }>('/api/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  secretDelete: (name: string) =>
    apiFetch<{ name: string; deleted: true }>(
      `/api/secrets/${encodeURIComponent(name)}`,
      { method: 'DELETE' }
    ),

  // Epic-lens: spec list + spec content (GET /api/projects/{id}/epic-specs)
  epicSpecs: (id: string) =>
    apiFetch<import('./types').EpicSpecsResp>(`/api/projects/${id}/epic-specs`),

  epicSpecContent: (id: string, name: string) =>
    apiFetch<{ name: string; content: string }>(`/api/projects/${id}/epic-specs/${encodeURIComponent(name)}`),

  // Spec-065: module/extension registry
  listModules: () =>
    apiFetch<{ modules: import('./types').Module[] }>('/api/modules'),

  setModule: (id: string, enabled: boolean) =>
    apiFetch<{ ok: boolean; module: import('./types').Module }>(`/api/modules/${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),

  // spec-066: persist a module's config block (e.g. the browser backend selection).
  setModuleConfig: (id: string, config: Record<string, unknown>) =>
    apiFetch<{ ok: boolean; module: import('./types').Module }>(`/api/modules/${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config }),
    }),

  // spec-066: pluggable browser backends + Cloak Manager.
  browserBackends: () =>
    apiFetch<import('./types').BrowserBackends>('/api/browser/backends'),

  installCloak: () =>
    apiFetch<{ ok: boolean; started: boolean }>('/api/browser/install-cloak', { method: 'POST' }),

  setManagerToken: (token: string) =>
    apiFetch<{ ok: boolean; token_set: boolean }>('/api/browser/manager-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    }),

  browserProfiles: () =>
    apiFetch<{ profiles: import('./types').CloakProfile[]; error?: string }>('/api/browser/profiles'),

  browserProfileAction: (id: string, action: 'launch' | 'stop') =>
    apiFetch<{ ok: boolean }>(`/api/browser/profiles/${encodeURIComponent(id)}/${action}`, { method: 'POST' }),
}

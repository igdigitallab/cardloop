const OPTS: RequestInit = { credentials: 'include' }

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...OPTS, ...init })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw Object.assign(new Error(text), { status: res.status })
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => apiFetch<{ ok: boolean }>('/api/health'),

  me: () => apiFetch<{ authed: boolean }>('/api/me'),

  login: (password: string) =>
    apiFetch<{ ok: boolean }>('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),

  logout: () =>
    apiFetch<{ ok: boolean }>('/api/logout', { method: 'POST' }),

  projects: () =>
    apiFetch<{ projects: import('./types').Project[] }>('/api/projects'),

  newProject: () =>
    apiFetch<{ ok: boolean; id: string; name: string; session_key: string; cwd: string }>(
      '/api/projects/new', { method: 'POST' }
    ),

  claudeMd: (id: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/claude-md`),

  saveClaudeMd: (id: string, content: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/claude-md`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  projectLogs: (id: string) =>
    apiFetch<import('./types').ProjectLogs>(`/api/projects/${id}/logs`),

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

  updateTask: (id: string, card: string, text: string, description?: string | null) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks/${card}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, ...(description !== undefined ? { description } : {}) }),
    }),

  tasksDone: (id: string) =>
    apiFetch<{ content: string; exists: boolean }>(`/api/projects/${id}/tasks/done`),

  cardRun: (id: string, card: string) =>
    apiFetch<import('./types').RunResult>(`/api/projects/${id}/tasks/${card}/run`),

  // Мульти-отправка карточек агенту → последовательная очередь (по одной)
  runBatch: (id: string, cardIds: string[]) =>
    apiFetch<{ ok: boolean; queued: number; started: string | null }>(`/api/projects/${id}/cards/run-batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ card_ids: cardIds }),
    }),

  // Spec 009: quality gate — прогнать тесты в worktree карточки и получить вердикт
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

  // C2: session management
  sessions: (id: string) =>
    apiFetch<{ sessions: import('./types').SessionInfo[] }>(`/api/projects/${id}/sessions`),

  setSession: (id: string, body: { action: 'new' } | { action: 'resume'; session_id: string }) =>
    apiFetch<{ active: string | null }>(`/api/projects/${id}/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // #2: ручной лейбл любой сессии (пустой — снять)
  setSessionLabel: (id: string, sid: string, label: string) =>
    apiFetch<{ ok: boolean; session_id: string; label: string | null }>(
      `/api/projects/${id}/sessions/${encodeURIComponent(sid)}/label`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label }),
      }
    ),

  // #4: запуск тестов проекта
  runTests: (id: string) =>
    apiFetch<import('./types').TestResult>(`/api/projects/${id}/test`, {
      method: 'POST',
    }),

  sessionHistory: (id: string, sessionId?: string) =>
    apiFetch<{ messages: import('./types').HistoryMessage[]; session_id: string | null; context_tokens?: number }>(
      `/api/projects/${id}/session-history${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`
    ),

  // C1-stop: interrupt the running agent on the server
  stopChat: (id: string) =>
    apiFetch<{ ok: boolean; stopped: boolean }>(`/api/projects/${id}/chat/stop`, {
      method: 'POST',
    }),

  // Проверить есть ли активный прогон (для восстановления UI после refresh)
  projectRunning: (id: string) =>
    apiFetch<{ running: boolean }>(`/api/projects/${id}/running`),

  // Скиллы агента (глобальные + проектные)
  projectSkills: (id: string) =>
    apiFetch<{
      global: { name: string; description: string }[]
      project: { name: string; description: string }[]
    }>(`/api/projects/${id}/skills`),

  // Сканер инцидентов: ручной запуск + счётчик активных err-карточек на доске
  scanErrors: (id: string) =>
    apiFetch<{ ok: boolean; scanned: number; added: number; updated: number; error?: string }>(
      `/api/projects/${id}/scan-errors`, { method: 'POST' }
    ),
  projectIncidents: (id: string) =>
    apiFetch<{ count: number; by_column: Record<string, number> }>(
      `/api/projects/${id}/incidents`
    ),

  // Feature A: session context (read/edited/commands)
  sessionContext: (id: string, sessionId?: string) =>
    apiFetch<import('./types').SessionContext>(
      `/api/projects/${id}/session-context${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`
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

  // Свободные чаты (без привязки к проекту)
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

  // Лимиты подписки Claude Code (rate_limits SDK, обновляются пассивно)
  usage: () =>
    apiFetch<{
      limits: Record<string, { status: string; resets_at: number | null; utilization: number | null; ts: number }>
      now: number
    }>('/api/usage'),

  // Сменить модель проекта (опус/сонет/хайку) — применится со следующего запроса
  setModel: (id: string, model: 'opus' | 'sonnet' | 'haiku') =>
    apiFetch<{ ok: boolean; model: string; topics_updated: number }>(
      `/api/projects/${id}/model`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      }
    ),

  // Глобальный файловый браузер (от $HOME)
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

  // Шаблоны промтов
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

  // Spec 008: Timeline — история событий шины проекта (JSONL-лог)
  timeline: (id: string, opts?: { limit?: number; before?: number }) => {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set('limit', String(opts.limit))
    if (opts?.before != null) params.set('before', String(opts.before))
    const qs = params.toString()
    return apiFetch<{ events: import('./types').TimelineEvent[] }>(
      `/api/projects/${id}/timeline${qs ? `?${qs}` : ''}`
    )
  },

  // Git: commit (если dirty) + push одной кнопкой
  gitSync: (id: string, message?: string) =>
    apiFetch<{ ok: boolean; committed: boolean; pushed: boolean; message: string | null; log: string }>(
      `/api/projects/${id}/git/sync`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(message ? { message } : {}),
      }
    ),

  // Настройки (карточка f2ba02): глобальные + per-project
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

  // Spec 010: самолечение — включить/выключить per-project
  toggleSelfHeal: (id: string, enabled: boolean) =>
    apiFetch<{ ok: boolean; self_heal: boolean; topics_updated: number }>(
      `/api/projects/${id}/self-heal`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }
    ),

  // TG-уведомления о новых ошибках («упало») — включить/выключить per-project
  toggleNotifyOnError: (id: string, enabled: boolean) =>
    apiFetch<{ ok: boolean; notify_on_error: boolean; topics_updated: number }>(
      `/api/projects/${id}/notify-on-error`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }
    ),
}

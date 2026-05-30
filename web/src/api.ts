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

  claudeMd: (id: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/claude-md`),

  saveClaudeMd: (id: string, content: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/claude-md`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  readme: (id: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/readme`),

  saveReadme: (id: string, content: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/readme`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  specs: (id: string) =>
    apiFetch<{ specs: import('./types').Spec[] }>(`/api/projects/${id}/specs`),

  spec: (id: string, name: string) =>
    apiFetch<import('./types').SpecContent>(`/api/projects/${id}/specs/${name}`),

  activity: (id: string) =>
    apiFetch<{ lines: string[] }>(`/api/projects/${id}/activity`),

  tasks: (id: string) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks`),

  createTask: (id: string, text: string, column?: string) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, column }),
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

  updateTask: (id: string, card: string, text: string) =>
    apiFetch<import('./types').Board>(`/api/projects/${id}/tasks/${card}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }),

  tasksDone: (id: string) =>
    apiFetch<{ content: string; exists: boolean }>(`/api/projects/${id}/tasks/done`),

  cardRun: (id: string, card: string) =>
    apiFetch<import('./types').RunResult>(`/api/projects/${id}/tasks/${card}/run`),

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

  // Feature A: session context (read/edited/commands)
  sessionContext: (id: string, sessionId?: string) =>
    apiFetch<import('./types').SessionContext>(
      `/api/projects/${id}/session-context${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`
    ),

  // Feature B: project memory files
  memory: (id: string) =>
    apiFetch<import('./types').ProjectMemory>(`/api/projects/${id}/memory`),

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
}

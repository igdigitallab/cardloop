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

  readme: (id: string) =>
    apiFetch<import('./types').ClaudeMd>(`/api/projects/${id}/readme`),

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

  tasksDone: (id: string) =>
    apiFetch<{ content: string; exists: boolean }>(`/api/projects/${id}/tasks/done`),
}

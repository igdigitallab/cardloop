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

export type TabId = 'overview' | 'readme' | 'claude-md' | 'specs' | 'activity' | 'chat' | 'board'

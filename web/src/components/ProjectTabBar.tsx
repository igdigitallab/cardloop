import { useState, useRef, useEffect } from 'react'
import { Project } from '../types'
import { UsageBadge } from './UsageBadge'

interface Props {
  projects: Project[]
  activeId: string | null
  unreadBySession: Record<string, number>
  onActivate: (id: string) => void
  onClose: (id: string) => void
  /** Переименование (поддерживается только для is_free вкладок) */
  onRename: (id: string, label: string) => void
  /** Создать новый свободный чат (кнопка «+» в полосе вкладок) */
  onNewFree: () => void
}

function TabItem({
  project, isActive, unread, onActivate, onClose, onRename,
}: {
  project: Project
  isActive: boolean
  unread: number
  onActivate: () => void
  onClose: () => void
  onRename: (label: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(project.name)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [editing])

  // Sync draft когда родительский label меняется (например, после внешнего rename)
  useEffect(() => { setDraft(project.name) }, [project.name])

  function commit() {
    const trimmed = draft.trim()
    if (trimmed && trimmed !== project.name) {
      onRename(trimmed)
    } else {
      setDraft(project.name)
    }
    setEditing(false)
  }

  function cancel() {
    setDraft(project.name)
    setEditing(false)
  }

  return (
    <div
      className={`ptab ${isActive ? 'active' : ''} ${project.is_free ? 'ptab-free' : ''}`}
      onClick={() => !editing && onActivate()}
      onDoubleClick={() => {
        // Переименовывать можно только free-чаты (у обычных имя = basename cwd)
        if (project.is_free) setEditing(true)
      }}
      title={editing ? '' : (project.is_free ? `${project.cwd} (двойной клик — переименовать)` : project.cwd)}
    >
      {editing ? (
        <input
          ref={inputRef}
          className="ptab-rename-input"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onClick={e => e.stopPropagation()}
          onKeyDown={e => {
            if (e.key === 'Enter') { e.preventDefault(); commit() }
            else if (e.key === 'Escape') { e.preventDefault(); cancel() }
          }}
          onBlur={commit}
        />
      ) : (
        <span className="ptab-name">{project.name}</span>
      )}
      {!editing && unread > 0 && !isActive && (
        <span className="ptab-unread" title={`${unread} новых`}>{unread > 99 ? '99+' : unread}</span>
      )}
      {!editing && (
        <button
          className="ptab-close"
          onClick={(e) => { e.stopPropagation(); onClose() }}
          title="Закрыть вкладку"
        >
          ✕
        </button>
      )}
    </div>
  )
}

export function ProjectTabBar({ projects, activeId, unreadBySession, onActivate, onClose, onRename, onNewFree }: Props) {
  return (
    <div className="project-tabbar">
      <div className="ptab-list">
        {projects.map(p => {
          const sk = p.tg_thread != null ? String(p.tg_thread) : null
          const unread = sk ? (unreadBySession[sk] || 0) : 0
          return (
            <TabItem
              key={p.id}
              project={p}
              isActive={p.id === activeId}
              unread={unread}
              onActivate={() => onActivate(p.id)}
              onClose={() => onClose(p.id)}
              onRename={(label) => onRename(p.id, label)}
            />
          )
        })}
        <button
          className="ptab-new"
          onClick={onNewFree}
          title="Новый свободный чат"
        >
          +
        </button>
      </div>
      <div className="ptab-spacer" />
      <UsageBadge />
    </div>
  )
}

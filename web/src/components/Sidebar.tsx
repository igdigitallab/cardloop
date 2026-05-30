import { useState } from 'react'
import { Project } from '../types'
import { HealthDot } from './HealthDot'

interface Props {
  projects: Project[]
  selectedId: string | null
  onSelect: (id: string) => void
  onLogout: () => void
  onDeleteFree: (id: string) => void
  loading: boolean
  unreadBySession: Record<string, number>
  collapsed: boolean
  onToggleCollapse: () => void
  onReorder: (ids: string[]) => void
}

function unreadFor(p: Project, map: Record<string, number>): number {
  if (p.tg_thread == null) return 0
  return map[String(p.tg_thread)] || 0
}

export function Sidebar({
  projects, selectedId, onSelect, onLogout, onDeleteFree, loading,
  unreadBySession, collapsed, onToggleCollapse, onReorder,
}: Props) {
  const [search, setSearch] = useState('')
  const [dragId, setDragId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)

  const filtered = projects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))

  if (collapsed) {
    return (
      <div className="sidebar sidebar-collapsed-mode">
        <button
          className="sidebar-toggle-btn collapsed"
          onClick={onToggleCollapse}
          title="Развернуть сайдбар"
        >
          ☰
        </button>
        <div className="projects-list-collapsed">
          {projects.map(p => {
            const unread = unreadFor(p, unreadBySession)
            const isActive = selectedId === p.id
            return (
              <button
                key={p.id}
                className={`project-icon-btn ${isActive ? 'active' : ''} ${p.is_free ? 'free' : ''}`}
                onClick={() => onSelect(p.id)}
                title={`${p.name}${unread ? ` (${unread} нов.)` : ''}`}
              >
                <span className="project-icon-letter">
                  {p.is_free ? '🏠' : p.name.charAt(0).toUpperCase()}
                </span>
                {unread > 0 && <span className="unread-dot-collapsed" />}
              </button>
            )
          })}
        </div>
      </div>
    )
  }

  function handleDragStart(e: React.DragEvent, id: string) {
    setDragId(id)
    e.dataTransfer.effectAllowed = 'move'
  }

  function handleDragOver(e: React.DragEvent, id: string) {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (id !== dragId) setDragOverId(id)
  }

  function handleDrop(e: React.DragEvent, targetId: string) {
    e.preventDefault()
    if (!dragId || dragId === targetId) {
      setDragId(null)
      setDragOverId(null)
      return
    }
    const ids = projects.map(p => p.id)
    const fromIdx = ids.indexOf(dragId)
    const toIdx = ids.indexOf(targetId)
    if (fromIdx !== -1 && toIdx !== -1) {
      const next = [...ids]
      next.splice(fromIdx, 1)
      next.splice(toIdx, 0, dragId)
      onReorder(next)
    }
    setDragId(null)
    setDragOverId(null)
  }

  function handleDragEnd() {
    setDragId(null)
    setDragOverId(null)
  }

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">⚡</div>
          <span className="sidebar-logo-text">Claude-Ops</span>
          <button
            className="sidebar-toggle-btn"
            onClick={onToggleCollapse}
            title="Свернуть сайдбар"
          >
            ⟨
          </button>
        </div>
        <input
          className="search-input"
          type="text"
          placeholder="Поиск проектов..."
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      <div className="projects-list">
        {loading ? (
          <div className="projects-empty">Загрузка...</div>
        ) : filtered.length === 0 ? (
          <div className="projects-empty">
            {search ? 'Ничего не найдено' : 'Нет проектов'}
          </div>
        ) : (
          filtered.map(p => {
            const unread = unreadFor(p, unreadBySession)
            const isDragging = dragId === p.id
            const isDragOver = dragOverId === p.id
            return (
              <div
                key={p.id}
                draggable
                onDragStart={e => handleDragStart(e, p.id)}
                onDragOver={e => handleDragOver(e, p.id)}
                onDragLeave={() => setDragOverId(null)}
                onDrop={e => handleDrop(e, p.id)}
                onDragEnd={handleDragEnd}
                className={[
                  'project-item',
                  p.is_free ? 'project-item-free' : '',
                  selectedId === p.id ? 'active' : '',
                  unread ? 'has-unread' : '',
                  isDragging ? 'sidebar-item-dragging' : '',
                  isDragOver ? 'sidebar-item-drag-over' : '',
                ].filter(Boolean).join(' ')}
                onClick={() => onSelect(p.id)}
                title={p.cwd}
              >
                {p.is_free
                  ? <span className="free-icon">🏠</span>
                  : <HealthDot health={p.health} />
                }
                <span className="project-name">{p.name}</span>
                {unread > 0 && (
                  <span className="unread-badge" title={`${unread} новых событий`}>
                    {unread > 99 ? '99+' : unread}
                  </span>
                )}
                {p.is_free && (
                  <button
                    className="free-delete-btn"
                    onClick={e => {
                      e.stopPropagation()
                      if (confirm(`Удалить свободный чат «${p.name}» (история стирается)?`)) {
                        onDeleteFree(p.id)
                      }
                    }}
                    title="Удалить свободный чат"
                  >✕</button>
                )}
              </div>
            )
          })
        )}
      </div>

      <div className="sidebar-footer">
        <button className="logout-btn" onClick={onLogout}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
            <polyline points="16 17 21 12 16 7"/>
            <line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
          Выйти
        </button>
      </div>
    </div>
  )
}

import { useState } from 'react'
import { Project } from '../types'
import { HealthDot } from './HealthDot'
import { ConfirmModal } from './ConfirmModal'
import { t } from '../i18n'

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
  onNewProject: () => void
  newProjectBusy: boolean
}

function unreadFor(p: Project, map: Record<string, number>): number {
  if (p.tg_thread == null) return 0
  return map[String(p.tg_thread)] || 0
}

export function Sidebar({
  projects, selectedId, onSelect, onLogout, onDeleteFree, loading,
  unreadBySession, collapsed, onToggleCollapse, onReorder,
  onNewProject, newProjectBusy,
}: Props) {
  const [search, setSearch] = useState('')
  const [dragId, setDragId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  // Confirm delete free chat
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null)

  const filtered = projects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))

  if (collapsed) {
    return (
      <div className="sidebar sidebar-collapsed-mode">
        <button
          className="sidebar-toggle-btn collapsed"
          onClick={onToggleCollapse}
          title={t['sidebar.expand']}
        >
          ☰
        </button>
        <button
          className="new-project-btn-collapsed"
          onClick={onNewProject}
          disabled={newProjectBusy}
          title={t['sidebar.new_project']}
        >
          {newProjectBusy ? '…' : '+'}
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
                title={`${p.name}${unread ? ` (${unread} new)` : ''}`}
              >
                <span className="project-icon-letter">
                  {p.is_free ? '🏠' : p.name.charAt(0).toUpperCase()}
                </span>
                {unread > 0 && <span className="unread-dot-collapsed" />}
                {(p.incidents ?? 0) > 0 && (
                  <span className="incidents-dot-collapsed" title={`${p.incidents} incident(s)`}>🚨</span>
                )}
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
            title={t['sidebar.collapse']}
          >
            ⟨
          </button>
        </div>
        <input
          className="search-input"
          type="text"
          placeholder={t['sidebar.search_placeholder']}
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      <button
        className="new-project-btn"
        onClick={onNewProject}
        disabled={newProjectBusy}
        title={t['sidebar.new_project_hint']}
      >
        {newProjectBusy ? '⏳ creating…' : `＋ ${t['sidebar.new_project']}`}
      </button>

      <div className="projects-list">
        {loading ? (
          <div className="projects-empty">{t['sidebar.loading']}</div>
        ) : filtered.length === 0 ? (
          <div className="projects-empty">
            {search ? t['sidebar.no_results'] : t['sidebar.empty']}
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
                {(p.incidents ?? 0) > 0 && (
                  <span
                    className="incidents-badge"
                    title={`${p.incidents} active incident(s) on the board`}
                  >
                    🚨 {p.incidents}
                  </span>
                )}
                {unread > 0 && (
                  <span className="unread-badge" title={`${unread} new event(s)`}>
                    {unread > 99 ? '99+' : unread}
                  </span>
                )}
                {p.is_free && (
                  <button
                    className="free-delete-btn"
                    onClick={e => {
                      e.stopPropagation()
                      setConfirmDelete({ id: p.id, name: p.name })
                    }}
                    title={t['sidebar.delete_free_chat']}
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
          Sign out
        </button>
      </div>

      {confirmDelete && (
        <ConfirmModal
          title={t['sidebar.delete_chat']}
          message={`Delete free chat "${confirmDelete.name}"? History will be erased.`}
          confirmLabel={t['common.delete']}
          danger
          onConfirm={() => { onDeleteFree(confirmDelete.id); setConfirmDelete(null) }}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </div>
  )
}

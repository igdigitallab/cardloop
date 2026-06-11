import { useRef, useState } from 'react'
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
  /** Project IDs where the agent finished a reply while the tab was not active */
  replyReadyIds?: Set<string>
  collapsed: boolean
  onToggleCollapse: () => void
  onReorder: (ids: string[]) => void
  onNewProject: () => void
  newProjectBusy: boolean
  /** Mobile/tablet off-canvas drawer: whether the sidebar is open */
  drawerOpen?: boolean
  /** Called when the drawer should close (e.g. close button inside) */
  onCloseDrawer?: () => void
}

function unreadFor(p: Project, map: Record<string, number>): number {
  if (p.tg_thread == null) return 0
  return map[String(p.tg_thread)] || 0
}

export function Sidebar({
  projects, selectedId, onSelect, onLogout, onDeleteFree, loading,
  unreadBySession, replyReadyIds, collapsed, onToggleCollapse, onReorder,
  onNewProject, newProjectBusy, drawerOpen,
}: Props) {
  const [search, setSearch] = useState('')
  // Confirm delete free chat
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null)

  // ── Pointer-events reorder (works on mouse + touch; closes board card f78394) ──
  // Replaces HTML5 DnD which is broken on iOS/Android.
  const [dragId, setDragId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)

  // Track pointer state without causing re-renders on every move
  const pointerState = useRef<{
    id: string
    startX: number
    startY: number
    moved: boolean
    pointerId: number
  } | null>(null)

  function handlePointerDown(e: React.PointerEvent, id: string) {
    // Only primary button (left click / single touch)
    if (e.button !== 0 && e.pointerType === 'mouse') return
    pointerState.current = {
      id,
      startX: e.clientX,
      startY: e.clientY,
      moved: false,
      pointerId: e.pointerId,
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }

  function handlePointerMove(e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    const dx = e.clientX - ps.startX
    const dy = e.clientY - ps.startY
    if (!ps.moved && Math.sqrt(dx * dx + dy * dy) > 5) {
      ps.moved = true
      setDragId(id)
    }
    if (!ps.moved) return

    // Find the element under the pointer to determine drag-over target
    const el = document.elementFromPoint(e.clientX, e.clientY)
    const itemEl = el?.closest('[data-project-id]') as HTMLElement | null
    const overId = itemEl?.dataset.projectId ?? null
    if (overId && overId !== id) {
      setDragOverId(overId)
    } else if (!overId) {
      setDragOverId(null)
    }
  }

  function handlePointerUp(e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)

    if (ps.moved && dragOverId && dragOverId !== id) {
      // Commit reorder
      const ids = projects.map(p => p.id)
      const fromIdx = ids.indexOf(id)
      const toIdx = ids.indexOf(dragOverId)
      if (fromIdx !== -1 && toIdx !== -1) {
        const next = [...ids]
        next.splice(fromIdx, 1)
        next.splice(toIdx, 0, id)
        onReorder(next)
      }
    } else if (!ps.moved) {
      // No drag movement → treat as a click/select
      onSelect(id)
    }

    pointerState.current = null
    setDragId(null)
    setDragOverId(null)
  }

  function handlePointerCancel(_e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    pointerState.current = null
    setDragId(null)
    setDragOverId(null)
  }

  const filtered = projects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))

  // CR-01 / Directive 2: Collapsed sidebar — narrow rail with only expand + new project buttons.
  // No icon list. Mobile hides the toggle entirely via CSS (sidebar-toggle-btn display:none at ≤768px).
  if (collapsed) {
    return (
      <div className={`sidebar sidebar-collapsed-mode${drawerOpen ? ' drawer-open' : ''}`}>
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
      </div>
    )
  }

  return (
    <div className={`sidebar${drawerOpen ? ' drawer-open' : ''}`}>
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
                data-project-id={p.id}
                /* Pointer events reorder — works on mouse AND touch */
                onPointerDown={e => handlePointerDown(e, p.id)}
                onPointerMove={e => handlePointerMove(e, p.id)}
                onPointerUp={e => handlePointerUp(e, p.id)}
                onPointerCancel={e => handlePointerCancel(e, p.id)}
                className={[
                  'project-item',
                  p.is_free ? 'project-item-free' : '',
                  selectedId === p.id ? 'active' : '',
                  unread ? 'has-unread' : '',
                  isDragging ? 'sidebar-item-dragging' : '',
                  isDragOver ? 'sidebar-item-drag-over' : '',
                ].filter(Boolean).join(' ')}
                title={p.cwd}
                style={{ touchAction: 'none' }}
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
                {replyReadyIds?.has(p.id) && selectedId !== p.id && (
                  <span className="reply-ready-badge" title="Agent reply is ready" />
                )}
                {p.is_free && (
                  <button
                    className="free-delete-btn"
                    onPointerDown={e => e.stopPropagation()}
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

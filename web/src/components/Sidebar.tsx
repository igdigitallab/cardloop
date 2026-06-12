import { useEffect, useRef, useState, useCallback } from 'react'
import { Project, ProjectGroups } from '../types'
import { api } from '../api'
import { HealthDot } from './HealthDot'
import { ConfirmModal } from './ConfirmModal'
import { t } from '../i18n'
import { useToast } from './Toast'

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
  /** H1: The current active project ID (for mobile back button) */
  activeProjectId?: string | null
  /** H1: Called when operator taps back — navigate back to the project screen */
  onGoBack?: () => void
  /** Called after archive/unarchive/group ops to trigger projects reload in parent */
  onProjectsReload?: () => void
}

function unreadFor(p: Project, map: Record<string, number>): number {
  if (p.tg_thread == null) return 0
  return map[String(p.tg_thread)] || 0
}

const LS_GROUP_PREFIX = 'cops.group.collapsed.'
const LS_ARCHIVED_COLLAPSED = 'cops.group.collapsed.__archived__'

function readCollapsed(key: string, def: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v === null) return def
    return v === 'true'
  } catch { return def }
}

function writeCollapsed(key: string, val: boolean) {
  try { localStorage.setItem(key, String(val)) } catch {}
}

export function Sidebar({
  projects, selectedId, onSelect, onLogout, onDeleteFree, loading,
  unreadBySession, replyReadyIds, collapsed, onToggleCollapse, onReorder,
  onNewProject, newProjectBusy, drawerOpen, activeProjectId, onGoBack,
  onProjectsReload,
}: Props) {
  const { showToast } = useToast()
  const [search, setSearch] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null)
  const [confirmArchive, setConfirmArchive] = useState<{ id: string; name: string } | null>(null)

  // Groups and archived state — managed locally
  const [groups, setGroups] = useState<ProjectGroups>({ groups: [], assignments: {} })
  const [archivedProjects, setArchivedProjects] = useState<{ id: string; name: string; cwd: string }[]>([])

  // Per-project context menu
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null)
  const [groupSubmenuId, setGroupSubmenuId] = useState<string | null>(null)

  // Section collapse state
  const [archivedCollapsed, setArchivedCollapsed] = useState(() => readCollapsed(LS_ARCHIVED_COLLAPSED, true))
  const [groupCollapsed, setGroupCollapsed] = useState<Record<string, boolean>>({})

  // Load groups and archived on mount and when projects change
  const loadGroups = useCallback(async () => {
    try {
      const data = await api.projectGroups()
      setGroups(data)
    } catch { /* ignore */ }
  }, [])

  const loadArchived = useCallback(async () => {
    try {
      const data = await api.archivedProjects()
      setArchivedProjects(data.projects)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    loadGroups()
    loadArchived()
  }, [loadGroups, loadArchived, projects])

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpenId) return
    function handle(e: MouseEvent) {
      const target = e.target as HTMLElement
      if (!target.closest('.project-menu-container')) {
        setMenuOpenId(null)
        setGroupSubmenuId(null)
      }
    }
    document.addEventListener('mousedown', handle)
    return () => document.removeEventListener('mousedown', handle)
  }, [menuOpenId])

  // ── Pointer-events reorder (works on mouse + touch) ──
  const [dragId, setDragId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  const pointerState = useRef<{
    id: string; startX: number; startY: number; moved: boolean; pointerId: number
  } | null>(null)

  function handlePointerDown(e: React.PointerEvent, id: string) {
    if (e.button !== 0 && e.pointerType === 'mouse') return
    pointerState.current = { id, startX: e.clientX, startY: e.clientY, moved: false, pointerId: e.pointerId }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }

  function handlePointerMove(e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    const dx = e.clientX - ps.startX
    const dy = e.clientY - ps.startY
    if (!ps.moved && Math.sqrt(dx * dx + dy * dy) > 5) { ps.moved = true; setDragId(id) }
    if (!ps.moved) return
    const el = document.elementFromPoint(e.clientX, e.clientY)
    const itemEl = el?.closest('[data-project-id]') as HTMLElement | null
    const overId = itemEl?.dataset.projectId ?? null
    if (overId && overId !== id) setDragOverId(overId)
    else if (!overId) setDragOverId(null)
  }

  function handlePointerUp(e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
    if (ps.moved && dragOverId && dragOverId !== id) {
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
      onSelect(id)
    }
    pointerState.current = null; setDragId(null); setDragOverId(null)
  }

  function handlePointerCancel(_e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    pointerState.current = null; setDragId(null); setDragOverId(null)
  }

  // Archive action
  async function doArchive(id: string) {
    try {
      await api.archiveProject(id)
      await loadArchived()
      if (onProjectsReload) onProjectsReload()
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        showToast(t['sidebar.archive_busy'], 'error')
      } else {
        showToast(t['common.error'], 'error')
      }
    }
  }

  // Restore action
  async function doRestore(id: string) {
    try {
      await api.unarchiveProject(id)
      await loadArchived()
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  // Set group action
  async function doSetGroup(id: string, group: string | null) {
    try {
      await api.setProjectGroup(id, group)
      await loadGroups()
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
    setMenuOpenId(null)
    setGroupSubmenuId(null)
  }

  // New group
  async function doNewGroup(id: string) {
    const label = window.prompt(t['sidebar.new_group'])
    if (!label?.trim()) return
    await doSetGroup(id, label.trim())
  }

  // Manage groups via prompt
  async function doManageGroups() {
    const current = groups.groups.join(', ')
    const input = window.prompt('Edit groups (comma-separated):', current)
    if (input === null) return
    const newGroups = input.split(',').map(s => s.trim()).filter(Boolean)
    try {
      await api.manageGroups(newGroups)
      await loadGroups()
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
    setMenuOpenId(null)
  }

  const nonFreeProjects = projects.filter(p => !p.is_free)
  const freeProjects = projects.filter(p => p.is_free)
  const filtered = nonFreeProjects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))
  const filteredFree = freeProjects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))

  // Group projects by their group assignment
  const grouped = new Map<string, Project[]>()
  for (const g of groups.groups) grouped.set(g, [])
  const ungrouped: Project[] = []
  for (const p of filtered) {
    const g = p.group && groups.groups.includes(p.group) ? p.group : null
    if (g) grouped.get(g)!.push(p)
    else ungrouped.push(p)
  }

  function toggleGroupCollapse(label: string) {
    setGroupCollapsed(prev => {
      const next = { ...prev, [label]: !(prev[label] ?? false) }
      writeCollapsed(LS_GROUP_PREFIX + label, next[label])
      return next
    })
  }

  function toggleArchivedCollapse() {
    setArchivedCollapsed(prev => {
      writeCollapsed(LS_ARCHIVED_COLLAPSED, !prev)
      return !prev
    })
  }

  function renderProjectItem(p: Project) {
    const unread = unreadFor(p, unreadBySession)
    const isDragging = dragId === p.id
    const isDragOver = dragOverId === p.id
    const menuOpen = menuOpenId === p.id
    const subOpen = groupSubmenuId === p.id

    return (
      <div
        key={p.id}
        data-project-id={p.id}
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
          <span className="incidents-badge" title={`${p.incidents} active incident(s) on the board`}>
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
            onClick={e => { e.stopPropagation(); setConfirmDelete({ id: p.id, name: p.name }) }}
            title={t['sidebar.delete_free_chat']}
          >✕</button>
        )}
        {!p.is_free && (
          <div className="project-menu-container" onPointerDown={e => e.stopPropagation()}>
            <button
              className="project-menu-btn"
              onClick={e => {
                e.stopPropagation()
                setMenuOpenId(menuOpen ? null : p.id)
                setGroupSubmenuId(null)
              }}
              title="More options"
            >⋮</button>
            {menuOpen && (
              <div className="project-menu-dropdown">
                <div
                  className="project-menu-item"
                  onMouseEnter={() => setGroupSubmenuId(p.id)}
                  onMouseLeave={() => setGroupSubmenuId(null)}
                >
                  {t['sidebar.move_to_group']} ▶
                  {subOpen && (
                    <div className="project-menu-submenu">
                      {groups.groups.map(g => (
                        <div
                          key={g}
                          className={`project-menu-item${p.group === g ? ' active' : ''}`}
                          onClick={() => doSetGroup(p.id, p.group === g ? null : g)}
                        >
                          {g}
                        </div>
                      ))}
                      <div className="project-menu-item" onClick={() => doNewGroup(p.id)}>
                        {t['sidebar.new_group']}
                      </div>
                    </div>
                  )}
                </div>
                {p.group && (
                  <div className="project-menu-item" onClick={() => doSetGroup(p.id, null)}>
                    {t['sidebar.remove_from_group']}
                  </div>
                )}
                <div className="project-menu-separator" />
                <div
                  className="project-menu-item danger"
                  onClick={e => {
                    e.stopPropagation()
                    setMenuOpenId(null)
                    setConfirmArchive({ id: p.id, name: p.name })
                  }}
                >
                  {t['sidebar.archive']}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    )
  }

  // CR-01: Collapsed sidebar
  if (collapsed) {
    return (
      <div className={`sidebar sidebar-collapsed-mode${drawerOpen ? ' drawer-open' : ''}`}>
        <button className="sidebar-toggle-btn collapsed" onClick={onToggleCollapse} title={t['sidebar.expand']}>☰</button>
        <button
          className="new-project-btn-collapsed"
          onClick={onNewProject}
          disabled={newProjectBusy}
          title={t['sidebar.new_project']}
        >{newProjectBusy ? '…' : '+'}</button>
      </div>
    )
  }

  const hasSearch = search.length > 0

  return (
    <div className={`sidebar${drawerOpen ? ' drawer-open' : ''}`}>
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">⚡</div>
          <span className="sidebar-logo-text">Claude-Ops</span>
          <button className="sidebar-toggle-btn" onClick={onToggleCollapse} title={t['sidebar.collapse']}>⟨</button>
        </div>
        {onGoBack && activeProjectId && activeProjectId !== '__global__' && activeProjectId !== '__schedules__' && (
          <button className="sidebar-back-btn" onClick={onGoBack} title={t['sidebar.back_to_project']} aria-label={t['sidebar.back_to_project']}>
            ✕ {t['sidebar.back_to_project']}
          </button>
        )}
        <input
          className="search-input"
          type="text"
          placeholder={t['sidebar.search_placeholder']}
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      <button className="new-project-btn" onClick={onNewProject} disabled={newProjectBusy} title={t['sidebar.new_project_hint']}>
        {newProjectBusy ? '⏳ creating…' : `＋ ${t['sidebar.new_project']}`}
      </button>

      <div className="projects-list">
        {loading ? (
          <div className="projects-empty">{t['sidebar.loading']}</div>
        ) : (
          <>
            {/* Named groups */}
            {!hasSearch && groups.groups.map(groupLabel => {
              const groupProjects = grouped.get(groupLabel) ?? []
              const isCollapsed = groupCollapsed[groupLabel] ?? false
              return (
                <div key={groupLabel} className="sidebar-group">
                  <div className="sidebar-group-header" onClick={() => toggleGroupCollapse(groupLabel)}>
                    <span className="sidebar-group-toggle">{isCollapsed ? '▶' : '▼'}</span>
                    <span className="sidebar-group-label">{groupLabel}</span>
                    <span className="sidebar-group-count">{groupProjects.length}</span>
                  </div>
                  {!isCollapsed && groupProjects.map(renderProjectItem)}
                </div>
              )
            })}

            {/* Ungrouped section (or all projects when searching) */}
            {hasSearch ? (
              <>
                {filtered.length === 0 && filteredFree.length === 0 ? (
                  <div className="projects-empty">{t['sidebar.no_results']}</div>
                ) : (
                  filtered.map(renderProjectItem)
                )}
              </>
            ) : (
              <>
                {ungrouped.length > 0 && groups.groups.length > 0 && (
                  <div className="sidebar-group-label-ungrouped">{t['sidebar.ungrouped']}</div>
                )}
                {ungrouped.map(renderProjectItem)}
                {filtered.length === 0 && groups.groups.length === 0 && (
                  <div className="projects-empty">{t['sidebar.empty']}</div>
                )}
              </>
            )}

            {/* Free chats */}
            {filteredFree.map(renderProjectItem)}

            {/* Manage groups button (only if there are groups) */}
            {!hasSearch && groups.groups.length > 0 && (
              <button className="sidebar-manage-groups-btn" onClick={doManageGroups}>
                {t['sidebar.manage_groups']}
              </button>
            )}

            {/* Archived section */}
            {!hasSearch && archivedProjects.length > 0 && (
              <div className="sidebar-group sidebar-archived-section">
                <div className="sidebar-group-header" onClick={toggleArchivedCollapse}>
                  <span className="sidebar-group-toggle">{archivedCollapsed ? '▶' : '▼'}</span>
                  <span className="sidebar-group-label">{t['sidebar.archived']}</span>
                  <span className="sidebar-group-count">{archivedProjects.length}</span>
                </div>
                {!archivedCollapsed && archivedProjects.map(ap => (
                  <div key={ap.id} className="project-item project-item-archived" title={ap.cwd}>
                    <span className="project-name">{ap.name}</span>
                    <button
                      className="sidebar-restore-btn"
                      onClick={e => { e.stopPropagation(); doRestore(ap.id) }}
                    >
                      {t['sidebar.restore']}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </>
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

      {confirmArchive && (
        <ConfirmModal
          title={t['sidebar.archive_confirm_title']}
          message={t['sidebar.archive_confirm_msg'].replace('{name}', confirmArchive.name)}
          confirmLabel={t['sidebar.archive']}
          onConfirm={() => { doArchive(confirmArchive.id); setConfirmArchive(null) }}
          onCancel={() => setConfirmArchive(null)}
        />
      )}
    </div>
  )
}

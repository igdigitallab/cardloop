import { useEffect, useRef, useState, useCallback } from 'react'
import { Project, ProjectGroups } from '../types'
import { api } from '../api'
import { HealthDot } from './HealthDot'
import { ConfirmModal } from './ConfirmModal'
import { Modal, ModalHead } from './Modal'
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
  replyReadyIds?: Set<string>
  collapsed: boolean
  onToggleCollapse: () => void
  onReorder: (ids: string[]) => void
  onNewProject: () => void
  newProjectBusy: boolean
  drawerOpen?: boolean
  onCloseDrawer?: () => void
  activeProjectId?: string | null
  onGoBack?: () => void
  onProjectsReload?: () => void
  /** Called when user right-clicks Open on a project — navigate to that project's settings tab */
  onOpenProjectSettings?: (id: string) => void
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

// ── Context menu types ────────────────────────────────────────────────────────
type CtxMenuTarget =
  | { kind: 'project'; id: string; group: string | null }
  | { kind: 'group'; label: string }

interface CtxMenuState {
  target: CtxMenuTarget
  x: number
  y: number
  submenuOpen: boolean
}

// ── Drag state ────────────────────────────────────────────────────────────────
type DragSubject =
  | { kind: 'project'; id: string }
  | { kind: 'group'; label: string }

export function Sidebar({
  projects, selectedId, onSelect, onLogout, onDeleteFree, loading,
  unreadBySession, replyReadyIds, collapsed, onToggleCollapse, onReorder,
  onNewProject, newProjectBusy, drawerOpen, activeProjectId, onGoBack,
  onProjectsReload, onOpenProjectSettings,
}: Props) {
  const { showToast } = useToast()
  const [search, setSearch] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null)
  const [confirmArchive, setConfirmArchive] = useState<{ id: string; name: string } | null>(null)

  const [groups, setGroups] = useState<ProjectGroups>({ groups: [], assignments: {} })
  const [archivedProjects, setArchivedProjects] = useState<{ id: string; name: string; cwd: string }[]>([])

  type Precheck = { is_git: boolean; uncommitted_count: number; unpushed_count: number; branch: string | null; has_remote: boolean }
  type TrashItem = { entry: string; id: string; name: string; original_cwd: string; deleted_at: string; days_left: number }
  const [hardDeleteTarget, setHardDeleteTarget] = useState<{ id: string; name: string } | null>(null)
  const [deletePrecheck, setDeletePrecheck] = useState<Precheck | null>(null)
  const [loadingPrecheck, setLoadingPrecheck] = useState(false)
  const [deleteNameInput, setDeleteNameInput] = useState('')
  const [deleteInProgress, setDeleteInProgress] = useState(false)
  const [trashItems, setTrashItems] = useState<TrashItem[]>([])
  const [trashCollapsed, setTrashCollapsed] = useState(true)

  const [archivedCollapsed, setArchivedCollapsed] = useState(() => readCollapsed(LS_ARCHIVED_COLLAPSED, true))
  const [groupCollapsed, setGroupCollapsed] = useState<Record<string, boolean>>({})

  // Inline rename state
  const [renamingGroup, setRenamingGroup] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const renameInputRef = useRef<HTMLInputElement>(null)

  // Confirm delete group modal
  const [confirmDeleteGroup, setConfirmDeleteGroup] = useState<string | null>(null)

  // Context menu state
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState | null>(null)
  const ctxMenuRef = useRef<HTMLDivElement>(null)

  // Drag state
  const [dragSubject, setDragSubject] = useState<DragSubject | null>(null)
  const [dragOverProjectId, setDragOverProjectId] = useState<string | null>(null)
  const [dragOverGroup, setDragOverGroup] = useState<string | null>(null)  // group label or '__ungrouped__'
  const [dragOverGroupHeader, setDragOverGroupHeader] = useState<string | null>(null)

  const pointerState = useRef<{
    id: string; kind: 'project' | 'group'
    startX: number; startY: number; moved: boolean; pointerId: number
    longPressTimer?: ReturnType<typeof setTimeout>
    group?: string | null  // current group for project
    label?: string  // group label for group drags
  } | null>(null)

  // Load data
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

  const loadTrash = useCallback(async () => {
    try {
      const data = await api.trash()
      setTrashItems(data.trash)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    loadGroups()
    loadArchived()
    loadTrash()
  }, [loadGroups, loadArchived, loadTrash, projects])

  // Focus rename input when rename starts
  useEffect(() => {
    if (renamingGroup !== null && renameInputRef.current) {
      renameInputRef.current.focus()
      renameInputRef.current.select()
    }
  }, [renamingGroup])

  // Close context menu on outside click or Escape
  useEffect(() => {
    if (!ctxMenu) return
    function handleMouseDown(e: MouseEvent) {
      if (ctxMenuRef.current && !ctxMenuRef.current.contains(e.target as Node)) {
        setCtxMenu(null)
      }
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setCtxMenu(null)
    }
    document.addEventListener('mousedown', handleMouseDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handleMouseDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [ctxMenu])

  // ── Context menu opener ───────────────────────────────────────────────────
  function openCtxMenu(e: React.MouseEvent | { clientX: number; clientY: number }, target: CtxMenuTarget) {
    setCtxMenu({ target, x: (e as React.MouseEvent).clientX, y: (e as React.MouseEvent).clientY, submenuOpen: false })
  }

  // ── Archive action ────────────────────────────────────────────────────────
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

  async function doRestore(id: string) {
    try {
      await api.unarchiveProject(id)
      await loadArchived()
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  async function openHardDelete(id: string, name: string) {
    setHardDeleteTarget({ id, name })
    setDeleteNameInput('')
    setDeletePrecheck(null)
    setLoadingPrecheck(true)
    try {
      const data = await api.deletePrecheck(id)
      setDeletePrecheck(data)
    } catch { /* show modal without precheck data */ }
    setLoadingPrecheck(false)
  }

  async function doHardDelete() {
    if (!hardDeleteTarget) return
    setDeleteInProgress(true)
    try {
      await api.deleteProject(hardDeleteTarget.id, deleteNameInput)
      setHardDeleteTarget(null)
      await loadArchived()
      await loadTrash()
      showToast(t['sidebar.deleted'], 'success')
      if (onProjectsReload) onProjectsReload()
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        showToast(t['sidebar.delete_busy'], 'error')
      } else if (status === 400) {
        const msg = (e as Error).message || ''
        if (msg.includes('path rejected')) {
          showToast(t['sidebar.delete_path_rejected'], 'error')
        } else {
          showToast(t['sidebar.delete_name_mismatch'], 'error')
        }
      } else {
        showToast(t['common.error'], 'error')
      }
    }
    setDeleteInProgress(false)
  }

  async function doRestoreTrash(entry: string) {
    try {
      await api.restoreTrash(entry)
      await loadArchived()
      await loadTrash()
      showToast(t['sidebar.trash_restored'], 'success')
      if (onProjectsReload) onProjectsReload()
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        showToast(t['sidebar.trash_collision'], 'error')
      } else {
        showToast(t['common.error'], 'error')
      }
    }
  }

  async function doSetGroup(id: string, group: string | null) {
    try {
      await api.setProjectGroup(id, group)
      await loadGroups()
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  // ── Inline rename group ────────────────────────────────────────────────────
  function startRenameGroup(label: string) {
    setRenamingGroup(label)
    setRenameValue(label)
    setCtxMenu(null)
  }

  async function commitRenameGroup() {
    if (!renamingGroup) return
    const newName = renameValue.trim()
    setRenamingGroup(null)
    if (!newName || newName === renamingGroup) return
    try {
      const data = await api.renameGroup(renamingGroup, newName)
      setGroups({ groups: data.groups, assignments: data.assignments })
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  function cancelRenameGroup() {
    setRenamingGroup(null)
    setRenameValue('')
  }

  // ── Create new group ───────────────────────────────────────────────────────
  async function doCreateGroup() {
    const tempName = `Group ${groups.groups.length + 1}`
    try {
      const data = await api.createGroup(tempName)
      setGroups({ groups: data.groups, assignments: data.assignments })
      // Immediately put into rename mode
      startRenameGroup(tempName)
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  // ── Delete group ───────────────────────────────────────────────────────────
  async function doDeleteGroup(label: string) {
    try {
      const data = await api.deleteGroup(label)
      setGroups({ groups: data.groups, assignments: data.assignments })
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
    setConfirmDeleteGroup(null)
  }

  // ── Reorder groups ─────────────────────────────────────────────────────────
  async function doReorderGroups(newOrder: string[]) {
    // Optimistic update
    setGroups(prev => ({ ...prev, groups: newOrder }))
    try {
      const data = await api.reorderGroups(newOrder)
      setGroups({ groups: data.groups, assignments: data.assignments })
    } catch {
      showToast(t['common.error'], 'error')
      loadGroups()
    }
  }

  // ── Pointer drag handlers ─────────────────────────────────────────────────

  function handleProjectPointerDown(e: React.PointerEvent, p: Project) {
    if (e.button !== 0 && e.pointerType === 'mouse') return
    // Long-press timer for touch context menu
    const timer = setTimeout(() => {
      if (pointerState.current && !pointerState.current.moved) {
        openCtxMenu({ clientX: e.clientX, clientY: e.clientY }, { kind: 'project', id: p.id, group: p.group ?? null })
        pointerState.current = null
        setDragSubject(null)
        ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
      }
    }, 500)
    pointerState.current = {
      id: p.id, kind: 'project',
      startX: e.clientX, startY: e.clientY, moved: false,
      pointerId: e.pointerId, longPressTimer: timer,
      group: p.group ?? null,
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }

  function handleProjectPointerMove(e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id || ps.kind !== 'project') return
    const dx = e.clientX - ps.startX
    const dy = e.clientY - ps.startY
    if (!ps.moved && Math.sqrt(dx * dx + dy * dy) > 6) {
      ps.moved = true
      if (ps.longPressTimer) clearTimeout(ps.longPressTimer)
      setDragSubject({ kind: 'project', id })
    }
    if (!ps.moved) return

    // Detect drop target — use elementsFromPoint to see through captured element
    const el = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null
    const projectEl = el?.closest('[data-project-id]') as HTMLElement | null
    const groupEl = el?.closest('[data-group]') as HTMLElement | null
    const ungroupedEl = el?.closest('[data-ungrouped-zone]') as HTMLElement | null

    if (projectEl && projectEl.dataset.projectId !== id) {
      setDragOverProjectId(projectEl.dataset.projectId ?? null)
      setDragOverGroup(null)
    } else if (ungroupedEl) {
      setDragOverProjectId(null)
      setDragOverGroup('__ungrouped__')
    } else if (groupEl && groupEl.dataset.group) {
      setDragOverProjectId(null)
      setDragOverGroup(groupEl.dataset.group)
    } else {
      setDragOverProjectId(null)
      setDragOverGroup(null)
    }
  }

  function handleProjectPointerUp(e: React.PointerEvent, p: Project) {
    const ps = pointerState.current
    if (!ps || ps.id !== p.id || ps.kind !== 'project') return
    if (ps.longPressTimer) clearTimeout(ps.longPressTimer)
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)

    if (ps.moved) {
      // Determine what we dropped onto
      if (dragOverGroup !== null) {
        // Drop onto a group zone or ungrouped zone
        const targetGroup = dragOverGroup === '__ungrouped__' ? null : dragOverGroup
        if (targetGroup !== (p.group ?? null)) {
          doSetGroup(p.id, targetGroup)
        }
      } else if (dragOverProjectId && dragOverProjectId !== p.id) {
        // Reorder within the same zone (same group or both ungrouped)
        const ids = projects.map(proj => proj.id)
        const fromIdx = ids.indexOf(p.id)
        const toIdx = ids.indexOf(dragOverProjectId)
        if (fromIdx !== -1 && toIdx !== -1) {
          const next = [...ids]
          next.splice(fromIdx, 1)
          next.splice(toIdx, 0, p.id)
          onReorder(next)
        }
      }
    } else {
      onSelect(p.id)
    }

    pointerState.current = null
    setDragSubject(null)
    setDragOverProjectId(null)
    setDragOverGroup(null)
  }

  function handleProjectPointerCancel(_e: React.PointerEvent, id: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== id) return
    if (ps.longPressTimer) clearTimeout(ps.longPressTimer)
    pointerState.current = null
    setDragSubject(null)
    setDragOverProjectId(null)
    setDragOverGroup(null)
  }

  // Group header drag
  function handleGroupPointerDown(e: React.PointerEvent, label: string) {
    if (e.button !== 0 && e.pointerType === 'mouse') return
    // Long-press for touch context menu on group header
    const timer = setTimeout(() => {
      if (pointerState.current && !pointerState.current.moved) {
        openCtxMenu({ clientX: e.clientX, clientY: e.clientY }, { kind: 'group', label })
        pointerState.current = null
        setDragSubject(null)
        ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
      }
    }, 500)
    pointerState.current = {
      id: label, kind: 'group',
      startX: e.clientX, startY: e.clientY, moved: false,
      pointerId: e.pointerId, longPressTimer: timer,
      label,
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }

  function handleGroupPointerMove(e: React.PointerEvent, label: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== label || ps.kind !== 'group') return
    const dx = e.clientX - ps.startX
    const dy = e.clientY - ps.startY
    if (!ps.moved && Math.sqrt(dx * dx + dy * dy) > 6) {
      ps.moved = true
      if (ps.longPressTimer) clearTimeout(ps.longPressTimer)
      setDragSubject({ kind: 'group', label })
    }
    if (!ps.moved) return

    const el = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null
    const headerEl = el?.closest('[data-group-header]') as HTMLElement | null
    if (headerEl && headerEl.dataset.groupHeader && headerEl.dataset.groupHeader !== label) {
      setDragOverGroupHeader(headerEl.dataset.groupHeader)
    } else {
      setDragOverGroupHeader(null)
    }
  }

  function handleGroupPointerUp(e: React.PointerEvent, label: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== label || ps.kind !== 'group') return
    if (ps.longPressTimer) clearTimeout(ps.longPressTimer)
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)

    if (ps.moved && dragOverGroupHeader && dragOverGroupHeader !== label) {
      const cur = groups.groups
      const fromIdx = cur.indexOf(label)
      const toIdx = cur.indexOf(dragOverGroupHeader)
      if (fromIdx !== -1 && toIdx !== -1) {
        const next = [...cur]
        next.splice(fromIdx, 1)
        next.splice(toIdx, 0, label)
        doReorderGroups(next)
      }
    } else if (!ps.moved) {
      // Click on header = toggle collapse
      toggleGroupCollapse(label)
    }

    pointerState.current = null
    setDragSubject(null)
    setDragOverGroupHeader(null)
  }

  function handleGroupPointerCancel(_e: React.PointerEvent, label: string) {
    const ps = pointerState.current
    if (!ps || ps.id !== label) return
    if (ps.longPressTimer) clearTimeout(ps.longPressTimer)
    pointerState.current = null
    setDragSubject(null)
    setDragOverGroupHeader(null)
  }

  // ── Data derived from state ───────────────────────────────────────────────
  const nonFreeProjects = projects.filter(p => !p.is_free)
  const freeProjects = projects.filter(p => p.is_free)
  const filtered = nonFreeProjects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))
  const filteredFree = freeProjects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))

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

  // ── Project item renderer ─────────────────────────────────────────────────
  function renderProjectItem(p: Project) {
    const unread = unreadFor(p, unreadBySession)
    const isDragging = dragSubject?.kind === 'project' && dragSubject.id === p.id
    const isDragOver = dragOverProjectId === p.id

    return (
      <div
        key={p.id}
        data-project-id={p.id}
        onPointerDown={e => handleProjectPointerDown(e, p)}
        onPointerMove={e => handleProjectPointerMove(e, p.id)}
        onPointerUp={e => handleProjectPointerUp(e, p)}
        onPointerCancel={e => handleProjectPointerCancel(e, p.id)}
        onContextMenu={e => {
          if (!p.is_free) {
            e.preventDefault()
            openCtxMenu(e, { kind: 'project', id: p.id, group: p.group ?? null })
          }
        }}
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
      </div>
    )
  }

  // ── Context menu renderer ─────────────────────────────────────────────────
  function renderCtxMenu() {
    if (!ctxMenu) return null
    const { target, x, y, submenuOpen } = ctxMenu

    // Clamp to viewport
    const menuW = 180
    const menuH = 220
    const cx = Math.min(x, window.innerWidth - menuW - 8)
    const cy = Math.min(y, window.innerHeight - menuH - 8)

    if (target.kind === 'project') {
      const pid = target.id
      const proj = projects.find(p => p.id === pid)
      return (
        <div
          ref={ctxMenuRef}
          className="ctx-menu"
          style={{ left: cx, top: cy }}
          onPointerDown={e => e.stopPropagation()}
        >
          <div className="ctx-menu-item" onClick={() => { onSelect(pid); setCtxMenu(null) }}>
            Open
          </div>
          {onOpenProjectSettings && (
            <div className="ctx-menu-item" onClick={() => { onOpenProjectSettings(pid); setCtxMenu(null) }}>
              ⚙ Settings
            </div>
          )}
          <div className="ctx-menu-separator" />
          <div
            className="ctx-menu-item"
            style={{ position: 'relative' }}
            onMouseEnter={() => setCtxMenu(prev => prev ? { ...prev, submenuOpen: true } : prev)}
            onMouseLeave={() => setCtxMenu(prev => prev ? { ...prev, submenuOpen: false } : prev)}
          >
            Move to group
            <span className="ctx-menu-arrow">▶</span>
            {submenuOpen && (
              <div className="ctx-submenu">
                {groups.groups.map(g => (
                  <div
                    key={g}
                    className={`ctx-menu-item${proj?.group === g ? ' disabled' : ''}`}
                    onClick={() => {
                      if (proj?.group !== g) doSetGroup(pid, g)
                      setCtxMenu(null)
                    }}
                  >
                    {proj?.group === g ? '✓ ' : ''}{g}
                  </div>
                ))}
                <div className="ctx-menu-separator" />
                <div
                  className="ctx-menu-item"
                  onClick={async () => {
                    setCtxMenu(null)
                    // Create a new group and immediately assign this project
                    const tempName = `Group ${groups.groups.length + 1}`
                    try {
                      const data = await api.createGroup(tempName)
                      setGroups({ groups: data.groups, assignments: data.assignments })
                      await doSetGroup(pid, tempName)
                      startRenameGroup(tempName)
                    } catch {
                      showToast(t['common.error'], 'error')
                    }
                  }}
                >
                  ➕ New group…
                </div>
              </div>
            )}
          </div>
          {target.group && (
            <div className="ctx-menu-item" onClick={() => { doSetGroup(pid, null); setCtxMenu(null) }}>
              Remove from group
            </div>
          )}
          <div className="ctx-menu-separator" />
          <div
            className="ctx-menu-item danger"
            onClick={() => {
              setCtxMenu(null)
              setConfirmArchive({ id: pid, name: proj?.name ?? pid })
            }}
          >
            🗄 Archive
          </div>
        </div>
      )
    }

    if (target.kind === 'group') {
      const label = target.label
      const isCollapsed = groupCollapsed[label] ?? false
      return (
        <div
          ref={ctxMenuRef}
          className="ctx-menu"
          style={{ left: cx, top: cy }}
          onPointerDown={e => e.stopPropagation()}
        >
          <div className="ctx-menu-item" onClick={() => { startRenameGroup(label) }}>
            ✏ Rename
          </div>
          <div className="ctx-menu-item" onClick={() => {
            setCtxMenu(null)
            // Create a new project in this group
            onNewProject()
            // Note: we can't pre-assign the group at creation time (no API for that),
            // but user can drag or context-menu to assign after creation.
          }}>
            ➕ New project in group
          </div>
          <div className="ctx-menu-item" onClick={() => { toggleGroupCollapse(label); setCtxMenu(null) }}>
            {isCollapsed ? 'Expand' : 'Collapse'}
          </div>
          <div className="ctx-menu-separator" />
          <div
            className="ctx-menu-item danger"
            onClick={() => { setCtxMenu(null); setConfirmDeleteGroup(label) }}
          >
            🗑 Delete group
          </div>
        </div>
      )
    }
    return null
  }

  // ── Collapsed sidebar ─────────────────────────────────────────────────────
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
        {onGoBack && activeProjectId && activeProjectId !== '__global__' && activeProjectId !== '__schedules__' && activeProjectId !== '__vault__' && (
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

      {/* ＋ New project + ＋ New group row */}
      <div className="sidebar-new-btns">
        <button className="new-project-btn" onClick={onNewProject} disabled={newProjectBusy} title={t['sidebar.new_project_hint']}>
          {newProjectBusy ? '⏳ creating…' : `＋ ${t['sidebar.new_project']}`}
        </button>
        <button className="new-group-btn" onClick={doCreateGroup} title="New group">
          ＋ group
        </button>
      </div>

      <div className="projects-list">
        {loading ? (
          <div className="projects-empty">{t['sidebar.loading']}</div>
        ) : (
          <>
            {/* Named groups */}
            {!hasSearch && groups.groups.map(groupLabel => {
              const groupProjects = grouped.get(groupLabel) ?? []
              const isCollapsed = groupCollapsed[groupLabel] ?? false
              const isGroupDragging = dragSubject?.kind === 'group' && dragSubject.label === groupLabel
              const isGroupDragOver = dragOverGroup === groupLabel
              const isGroupHeaderDragOver = dragOverGroupHeader === groupLabel

              return (
                <div
                  key={groupLabel}
                  className={[
                    'sidebar-group',
                    isGroupDragging ? 'group-dragging' : '',
                    isGroupDragOver ? 'group-drag-over' : '',
                  ].filter(Boolean).join(' ')}
                  data-group={groupLabel}
                >
                  <div
                    className={[
                      'sidebar-group-header',
                      isGroupHeaderDragOver ? 'group-header-drag-over' : '',
                    ].filter(Boolean).join(' ')}
                    data-group-header={groupLabel}
                    onPointerDown={e => handleGroupPointerDown(e, groupLabel)}
                    onPointerMove={e => handleGroupPointerMove(e, groupLabel)}
                    onPointerUp={e => handleGroupPointerUp(e, groupLabel)}
                    onPointerCancel={e => handleGroupPointerCancel(e, groupLabel)}
                    onContextMenu={e => { e.preventDefault(); openCtxMenu(e, { kind: 'group', label: groupLabel }) }}
                    onDoubleClick={() => startRenameGroup(groupLabel)}
                    style={{ touchAction: 'none' }}
                  >
                    <span className="sidebar-group-toggle">{isCollapsed ? '▶' : '▼'}</span>
                    {renamingGroup === groupLabel ? (
                      <input
                        ref={renameInputRef}
                        className="sidebar-group-rename-input"
                        value={renameValue}
                        onChange={e => setRenameValue(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') { e.preventDefault(); commitRenameGroup() }
                          if (e.key === 'Escape') cancelRenameGroup()
                        }}
                        onBlur={commitRenameGroup}
                        onClick={e => e.stopPropagation()}
                        onPointerDown={e => e.stopPropagation()}
                      />
                    ) : (
                      <span className="sidebar-group-label">{groupLabel}</span>
                    )}
                    <span className="sidebar-group-count">{groupProjects.length}</span>
                  </div>
                  {!isCollapsed && (
                    <>
                      {groupProjects.map(renderProjectItem)}
                      {groupProjects.length === 0 && (
                        <div className="sidebar-group-drop-hint">drop projects here</div>
                      )}
                    </>
                  )}
                </div>
              )
            })}

            {/* Ungrouped section or search results */}
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
                {groups.groups.length > 0 && (
                  <div
                    className="sidebar-group-label-ungrouped"
                    data-ungrouped-zone="1"
                  >
                    {t['sidebar.ungrouped']}
                  </div>
                )}
                <div
                  className={[
                    'sidebar-ungrouped-drop-zone',
                    dragOverGroup === '__ungrouped__' ? 'drop-active' : '',
                  ].filter(Boolean).join(' ')}
                  data-ungrouped-zone="1"
                >
                  {ungrouped.map(renderProjectItem)}
                  {ungrouped.length === 0 && groups.groups.length === 0 && (
                    <div className="projects-empty">{t['sidebar.empty']}</div>
                  )}
                </div>
              </>
            )}

            {/* Free chats */}
            {filteredFree.map(renderProjectItem)}

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
                    <div className="sidebar-archived-actions">
                      <button
                        className="sidebar-restore-btn"
                        onClick={e => { e.stopPropagation(); doRestore(ap.id) }}
                      >
                        {t['sidebar.restore']}
                      </button>
                      <button
                        className="sidebar-delete-btn"
                        onClick={e => { e.stopPropagation(); openHardDelete(ap.id, ap.name) }}
                        title={t['sidebar.delete_permanently']}
                      >
                        {t['sidebar.delete_permanently']}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Trash section */}
            {!hasSearch && trashItems.length > 0 && (
              <div className="sidebar-group sidebar-trash-section">
                <div className="sidebar-group-header" onClick={() => setTrashCollapsed(prev => !prev)}>
                  <span className="sidebar-group-toggle">{trashCollapsed ? '▶' : '▼'}</span>
                  <span className="sidebar-group-label">{t['sidebar.trash_section']}</span>
                  <span className="sidebar-group-count">{trashItems.length}</span>
                </div>
                {!trashCollapsed && trashItems.map(item => (
                  <div key={item.entry} className="project-item project-item-trash" title={item.original_cwd}>
                    <span className="project-name">{item.name}</span>
                    <span className="sidebar-trash-days">{t['sidebar.trash_days_left'].replace('{days}', String(item.days_left))}</span>
                    <button
                      className="sidebar-restore-btn"
                      onClick={e => { e.stopPropagation(); doRestoreTrash(item.entry) }}
                    >
                      {t['sidebar.trash_restore']}
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

      {/* Context menu portal */}
      {renderCtxMenu()}

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

      {confirmDeleteGroup && (
        <ConfirmModal
          title="Delete group"
          message={`Delete group "${confirmDeleteGroup}"? Its projects will become ungrouped.`}
          confirmLabel="Delete group"
          danger
          onConfirm={() => doDeleteGroup(confirmDeleteGroup)}
          onCancel={() => setConfirmDeleteGroup(null)}
        />
      )}

      {/* Spec-025: Hard delete modal */}
      {hardDeleteTarget && (
        <Modal onClose={() => { if (!deleteInProgress) setHardDeleteTarget(null) }}>
          <ModalHead title={t['sidebar.delete_confirm_title']} onClose={() => { if (!deleteInProgress) setHardDeleteTarget(null) }} />
          <div className="run-modal-body">
            {loadingPrecheck && (
              <p style={{ margin: '0 0 12px', color: 'var(--text2)', fontSize: 13 }}>Checking git status…</p>
            )}
            {!loadingPrecheck && deletePrecheck && (
              <div style={{ marginBottom: 12 }}>
                {!deletePrecheck.is_git && (
                  <p style={{ margin: '0 0 6px', color: 'var(--red)', fontSize: 13, lineHeight: 1.4 }}>
                    ⚠️ {t['sidebar.delete_warning_no_git']}
                  </p>
                )}
                {deletePrecheck.is_git && deletePrecheck.uncommitted_count > 0 && deletePrecheck.unpushed_count > 0 && (
                  <p style={{ margin: '0 0 6px', color: 'var(--red)', fontSize: 13, lineHeight: 1.4 }}>
                    ⚠️ {t['sidebar.delete_warning_git']
                      .replace('{uncommitted}', String(deletePrecheck.uncommitted_count))
                      .replace('{unpushed}', String(deletePrecheck.unpushed_count))}
                  </p>
                )}
                {deletePrecheck.is_git && deletePrecheck.uncommitted_count > 0 && deletePrecheck.unpushed_count === 0 && (
                  <p style={{ margin: '0 0 6px', color: 'var(--red)', fontSize: 13, lineHeight: 1.4 }}>
                    ⚠️ {t['sidebar.delete_warning_uncommitted']
                      .replace('{uncommitted}', String(deletePrecheck.uncommitted_count))}
                  </p>
                )}
                {deletePrecheck.is_git && deletePrecheck.uncommitted_count === 0 && deletePrecheck.unpushed_count > 0 && (
                  <p style={{ margin: '0 0 6px', color: 'var(--red)', fontSize: 13, lineHeight: 1.4 }}>
                    ⚠️ {t['sidebar.delete_warning_unpushed']
                      .replace('{unpushed}', String(deletePrecheck.unpushed_count))}
                  </p>
                )}
              </div>
            )}

            <p style={{ margin: '0 0 12px', fontSize: 13, lineHeight: 1.4, color: 'var(--text2)' }}>
              {t['sidebar.delete_trash_notice']}
            </p>

            <p style={{ margin: '0 0 6px', fontSize: 13, fontWeight: 500 }}>{t['sidebar.delete_type_name']}</p>
            <input
              className="doc-textarea"
              type="text"
              value={deleteNameInput}
              onChange={e => setDeleteNameInput(e.target.value)}
              placeholder={hardDeleteTarget.name}
              autoFocus
              disabled={deleteInProgress}
              style={{ marginBottom: 16 }}
            />

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                className="btn-secondary"
                onClick={() => setHardDeleteTarget(null)}
                disabled={deleteInProgress}
              >
                {t['common.cancel']}
              </button>
              <button
                className="btn-danger"
                onClick={doHardDelete}
                disabled={deleteNameInput !== hardDeleteTarget.name || deleteInProgress}
              >
                {deleteInProgress ? '…' : t['sidebar.delete_confirm_btn']}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}

import { useEffect, useRef, useState, useCallback } from 'react'
import { Project, ProjectGroups } from '../types'
import { api } from '../api'
import { HealthDot } from './HealthDot'
import { ConfirmModal } from './ConfirmModal'
import { Modal, ModalHead } from './Modal'
import { t } from '../i18n'
import { useToast } from './Toast'
import { ThemeToggle } from './ThemeToggle'
import { VersionBadge } from './VersionBadge'
import { ThemeValue } from '../hooks/useTheme'
import { ActionMenu, KebabButton, ActionMenuSection, ActionMenuItem } from './ActionMenu'

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
  /** Opens the new-project prompt dialog (caller shows the dialog, not blank-create). */
  onNewProject: (group?: string) => void
  newProjectBusy: boolean
  drawerOpen?: boolean
  onCloseDrawer?: () => void
  activeProjectId?: string | null
  onGoBack?: () => void
  onProjectsReload?: () => void
  /** Called when user right-clicks Open on a project — navigate to that project's settings tab */
  onOpenProjectSettings?: (id: string) => void
  /** Opens the rename-label dialog for a project (fires from the sidebar menu item). */
  onRenameProject?: (project: Project) => void
  theme?: ThemeValue
  onThemeChange?: (t: ThemeValue) => void
  // ── Global tool launchers (moved here from the top tab bar for mobile parity) ──
  onOpenTerminal?: () => void
  terminalActive?: boolean
  onOpenVault?: () => void
  vaultActive?: boolean
  onOpenUsage?: () => void
  usageActive?: boolean
  onOpenGlobalFiles?: () => void
  globalFilesActive?: boolean
  onOpenSchedules?: () => void
  schedulesActive?: boolean
  onOpenSettingsGlobal?: () => void
  settingsGlobalActive?: boolean
  /** Spec-074: opens the global search overlay (Cmd/Ctrl+K) */
  onOpenSearch?: () => void
}

function unreadFor(p: Project, map: Record<string, number>): number {
  if (!p.session_key) return 0
  return map[p.session_key] || 0
}

const LS_GROUP_PREFIX = 'cops.group.collapsed.'
const LS_ARCHIVED_COLLAPSED = 'cops.group.collapsed.__archived__'
const LS_FAVORITES_COLLAPSED = 'cops.group.collapsed.__favorites__'

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

// ── Context menu target types ─────────────────────────────────────────────────
type CtxMenuTarget =
  | { kind: 'project'; id: string; group: string | null }
  | { kind: 'group'; label: string }

interface ActionMenuState {
  target: CtxMenuTarget
  anchorRect: DOMRect
}

// ── Drag state ────────────────────────────────────────────────────────────────
type DragSubject =
  | { kind: 'project'; id: string }
  | { kind: 'group'; label: string }

export function Sidebar({
  projects, selectedId, onSelect, onLogout, onDeleteFree, loading,
  unreadBySession, replyReadyIds, collapsed, onToggleCollapse, onReorder,
  onNewProject, newProjectBusy, drawerOpen, activeProjectId, onGoBack,
  onProjectsReload, onOpenProjectSettings, onRenameProject,
  theme, onThemeChange,
  onOpenTerminal, terminalActive, onOpenVault, vaultActive,
  onOpenUsage, usageActive,
  onOpenGlobalFiles, globalFilesActive, onOpenSchedules, schedulesActive,
  onOpenSettingsGlobal, settingsGlobalActive, onOpenSearch,
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
  const [favoritesCollapsed, setFavoritesCollapsed] = useState(() => readCollapsed(LS_FAVORITES_COLLAPSED, false))
  const [groupCollapsed, setGroupCollapsed] = useState<Record<string, boolean>>({})

  // Inline rename state
  const [renamingGroup, setRenamingGroup] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const renameInputRef = useRef<HTMLInputElement>(null)

  // Confirm delete group modal
  const [confirmDeleteGroup, setConfirmDeleteGroup] = useState<string | null>(null)

  // Action menu state (replaces old ctxMenu)
  const [actionMenu, setActionMenu] = useState<ActionMenuState | null>(null)

  // Drag state
  const [dragSubject, setDragSubject] = useState<DragSubject | null>(null)
  const [dragOverProjectId, setDragOverProjectId] = useState<string | null>(null)
  const [dragOverGroup, setDragOverGroup] = useState<string | null>(null)  // group label or '__ungrouped__'
  const [dragOverGroupHeader, setDragOverGroupHeader] = useState<string | null>(null)

  // Refs mirroring drag-over state so doc-level handlers always read fresh values
  // without stale closure risk.
  const dragOverGroupRef = useRef<string | null>(null)
  const dragOverProjectIdRef = useRef<string | null>(null)
  const dragOverGroupHeaderRef = useRef<string | null>(null)
  // Mirror all three setters so we update ref and state together
  function setDragOverGroupSynced(v: string | null) {
    dragOverGroupRef.current = v
    setDragOverGroup(v)
  }
  function setDragOverProjectIdSynced(v: string | null) {
    dragOverProjectIdRef.current = v
    setDragOverProjectId(v)
  }
  function setDragOverGroupHeaderSynced(v: string | null) {
    dragOverGroupHeaderRef.current = v
    setDragOverGroupHeader(v)
  }

  // Single drag ref — replaces pointerState + touchLongPress + touchDragActive
  // https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events#determining_button_states
  const dragRef = useRef<{
    kind: 'project' | 'group'
    id: string
    group: string | null   // project's current group (null = ungrouped)
    startX: number
    startY: number
    moved: boolean
  } | null>(null)

  // Suppress the synthetic click that fires after a touch-pointerup when a drag occurred
  const suppressClick = useRef(false)

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

  // ── Action menu opener ────────────────────────────────────────────────────
  function openActionMenu(anchorRect: DOMRect, target: CtxMenuTarget) {
    setActionMenu({ target, anchorRect })
  }

  function closeActionMenu() {
    setActionMenu(null)
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

  async function doSetFavorite(id: string, favorite: boolean) {
    try {
      await api.setFavorite(id, favorite)
      if (onProjectsReload) onProjectsReload()
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  // ── Inline rename group ────────────────────────────────────────────────────
  // `renamingGroup` holds the FULL path of the folder being renamed; the input
  // edits only the LEAF segment (prefill = leaf). On commit we rebuild the full
  // path from the unchanged parent + the trimmed leaf (spec-061).
  function startRenameGroup(path: string) {
    setRenamingGroup(path)
    setRenameValue(folderLeaf(path))
    closeActionMenu()
  }

  async function commitRenameGroup() {
    if (!renamingGroup) return
    const path = renamingGroup
    const newLeaf = renameValue.trim()
    setRenamingGroup(null)
    if (!newLeaf) return
    if (newLeaf.includes('/')) {
      showToast(t['sidebar.folder_name_slash'], 'error')
      return
    }
    const parent = folderParent(path)
    const newPath = parent ? `${parent}/${newLeaf}` : newLeaf
    if (newPath === path) return
    try {
      const data = await api.renameGroup(path, newPath)
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
  // Pick a non-colliding top-level "Group N" name (groups.groups now counts
  // folders at all depths, so a naive length+1 can collide with an existing label).
  function nextTopLevelGroupName(): string {
    let n = groups.groups.length + 1
    while (groups.groups.includes(`Group ${n}`)) n += 1
    return `Group ${n}`
  }

  async function doCreateGroup() {
    const tempName = nextTopLevelGroupName()
    try {
      const data = await api.createGroup(tempName)
      setGroups({ groups: data.groups, assignments: data.assignments })
      // Immediately put into rename mode
      startRenameGroup(tempName)
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  // ── Create a subfolder under an existing folder (spec-061) ──────────────────
  async function doCreateSubfolder(parentPath: string) {
    // Pick a non-colliding "Folder N" leaf under the parent.
    let n = 1
    let leaf = `Folder ${n}`
    let childPath = `${parentPath}/${leaf}`
    while (groups.groups.includes(childPath)) {
      n += 1
      leaf = `Folder ${n}`
      childPath = `${parentPath}/${leaf}`
    }
    try {
      const data = await api.createGroup(childPath)
      setGroups({ groups: data.groups, assignments: data.assignments })
      // Make sure the parent is expanded so the new subfolder is visible.
      if (isGroupCollapsed(parentPath)) toggleGroupCollapse(parentPath)
      startRenameGroup(childPath)
    } catch {
      showToast(t['common.error'], 'error')
    }
  }

  // ── Move a folder (re-parent) via rename cascade (spec-061) ──────────────────
  // target === null → move to top level; otherwise nest under `target`.
  async function doMoveFolder(path: string, target: string | null) {
    const leaf = folderLeaf(path)
    const newPath = target ? `${target}/${leaf}` : leaf
    if (newPath === path) return
    try {
      const data = await api.renameGroup(path, newPath)
      setGroups({ groups: data.groups, assignments: data.assignments })
      if (onProjectsReload) onProjectsReload()
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

  // ── Shared drop-commit logic ──────────────────────────────────────────────
  function commitProjectDrop(
    p: Project,
    overGroup: string | null,
    overProjectId: string | null,
  ) {
    if (overGroup !== null) {
      const targetGroup = overGroup === '__ungrouped__' ? null : overGroup
      if (targetGroup !== (p.group ?? null)) {
        doSetGroup(p.id, targetGroup)
      }
    } else if (overProjectId && overProjectId !== p.id) {
      const targetProj = projects.find(proj => proj.id === overProjectId)
      const targetGroup = targetProj?.group ?? null
      if (targetGroup !== (p.group ?? null)) {
        doSetGroup(p.id, targetGroup)
      } else {
        const ids = projects.map(proj => proj.id)
        const fromIdx = ids.indexOf(p.id)
        const toIdx = ids.indexOf(overProjectId)
        if (fromIdx !== -1 && toIdx !== -1) {
          const next = [...ids]
          next.splice(fromIdx, 1)
          next.splice(toIdx, 0, p.id)
          onReorder(next)
        }
      }
    }
  }

  // Helper: given a client point, update dragOver state based on what's under it.
  function updateDragOverFromPoint(clientX: number, clientY: number, draggedId: string, draggedGroup: string | null) {
    const el = document.elementFromPoint(clientX, clientY) as HTMLElement | null
    const projectEl = el?.closest('[data-project-id]') as HTMLElement | null
    const groupEl = el?.closest('[data-group]') as HTMLElement | null
    const ungroupedEl = el?.closest('[data-ungrouped-zone]') as HTMLElement | null

    if (projectEl && projectEl.dataset.projectId !== draggedId) {
      const overProj = projects.find(proj => proj.id === projectEl.dataset.projectId)
      const overGroup = overProj?.group ?? null
      if (overGroup !== (draggedGroup ?? null)) {
        setDragOverProjectIdSynced(null)
        setDragOverGroupSynced(overGroup ?? '__ungrouped__')
      } else {
        setDragOverProjectIdSynced(projectEl.dataset.projectId ?? null)
        setDragOverGroupSynced(null)
      }
    } else if (ungroupedEl) {
      setDragOverProjectIdSynced(null)
      setDragOverGroupSynced('__ungrouped__')
    } else if (groupEl && groupEl.dataset.group) {
      setDragOverProjectIdSynced(null)
      setDragOverGroupSynced(groupEl.dataset.group)
    } else {
      setDragOverProjectIdSynced(null)
      setDragOverGroupSynced(null)
    }
  }

  // Helper: given a client point, update group-header drag-over state.
  function updateGroupDragOverFromPoint(clientX: number, clientY: number, draggedLabel: string) {
    const el = document.elementFromPoint(clientX, clientY) as HTMLElement | null
    const headerEl = el?.closest('[data-group-header]') as HTMLElement | null
    if (headerEl && headerEl.dataset.groupHeader && headerEl.dataset.groupHeader !== draggedLabel) {
      setDragOverGroupHeaderSynced(headerEl.dataset.groupHeader)
    } else {
      setDragOverGroupHeaderSynced(null)
    }
  }

  // ── Document-level pointer handlers ───────────────────────────────────────
  //
  // Defined as stable ref callbacks so add/removeEventListener always receives
  // the SAME function reference. They read drag-over state via refs (not
  // stale closure on state values).
  //
  // Pattern: https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events

  const onDocPointerMove = useRef((e: PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    if (!d.moved) {
      if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < 6) return
      d.moved = true
      if (d.kind === 'project') setDragSubject({ kind: 'project', id: d.id })
      else setDragSubject({ kind: 'group', label: d.id })
    }
    if (d.kind === 'project') updateDragOverFromPoint(e.clientX, e.clientY, d.id, d.group)
    else updateGroupDragOverFromPoint(e.clientX, e.clientY, d.id)
  }).current

  const cleanupDocListeners = useRef(() => {
    window.removeEventListener('pointermove', onDocPointerMove)
    window.removeEventListener('pointerup', onDocPointerUpFn.current)
    window.removeEventListener('pointercancel', onDocPointerCancelFn.current)
  })

  // onDocPointerUp reads projects (state) to commit drop — keep stable via ref
  // that captures the latest projects via a wrapper that calls projectsRef.current
  const projectsRef = useRef(projects)
  useEffect(() => { projectsRef.current = projects }, [projects])

  const groupsRef = useRef(groups)
  useEffect(() => { groupsRef.current = groups }, [groups])

  const onDocPointerUpFn = useRef((e: PointerEvent) => {
    const d = dragRef.current
    cleanupDocListeners.current()
    dragRef.current = null

    if (d?.moved) {
      suppressClick.current = true
      setTimeout(() => { suppressClick.current = false }, 50)

      if (d.kind === 'project') {
        const proj = projectsRef.current.find(p => p.id === d.id)
        if (proj) commitProjectDrop(proj, dragOverGroupRef.current, dragOverProjectIdRef.current)
      } else {
        // Group reorder
        const overHeader = dragOverGroupHeaderRef.current
        if (overHeader && overHeader !== d.id) {
          const cur = groupsRef.current.groups
          const fromIdx = cur.indexOf(d.id)
          const toIdx = cur.indexOf(overHeader)
          if (fromIdx !== -1 && toIdx !== -1) {
            const next = [...cur]
            next.splice(fromIdx, 1)
            next.splice(toIdx, 0, d.id)
            doReorderGroups(next)
          }
        }
      }
    }

    setDragSubject(null)
    setDragOverProjectIdSynced(null)
    setDragOverGroupSynced(null)
    setDragOverGroupHeaderSynced(null)
    void e  // silence unused-param lint
  })

  const onDocPointerCancelFn = useRef((_e: PointerEvent) => {
    cleanupDocListeners.current()
    dragRef.current = null
    setDragSubject(null)
    setDragOverProjectIdSynced(null)
    setDragOverGroupSynced(null)
    setDragOverGroupHeaderSynced(null)
  })

  // Keep cleanupDocListeners up-to-date with the stable handler refs
  useEffect(() => {
    cleanupDocListeners.current = () => {
      window.removeEventListener('pointermove', onDocPointerMove)
      window.removeEventListener('pointerup', onDocPointerUpFn.current)
      window.removeEventListener('pointercancel', onDocPointerCancelFn.current)
    }
  }, [onDocPointerMove])

  function attachDocListeners() {
    window.addEventListener('pointermove', onDocPointerMove)
    window.addEventListener('pointerup', onDocPointerUpFn.current)
    window.addEventListener('pointercancel', onDocPointerCancelFn.current)
  }

  // ── Project pointer-down handler ──────────────────────────────────────────
  // Drag-to-reorder is DESKTOP-ONLY (direct mouse drag on the row, no handle).
  // Touch has no reordering: taps select, vertical swipe scrolls the list.
  function handleProjectPointerDown(e: React.PointerEvent, p: Project) {
    if (e.pointerType !== 'mouse' || e.button !== 0) return
    const onExcluded = !!(e.target as HTMLElement).closest?.('.sidebar-kebab-btn, .fav-star-btn, .free-delete-btn')
    if (onExcluded) return

    dragRef.current = { kind: 'project', id: p.id, group: p.group ?? null, startX: e.clientX, startY: e.clientY, moved: false }
    attachDocListeners()
  }

  // ── Group header pointer-down handler ─────────────────────────────────────
  // Desktop-only group reordering via direct mouse drag on the header.
  function handleGroupPointerDown(e: React.PointerEvent, label: string) {
    if (e.pointerType !== 'mouse' || e.button !== 0) return

    dragRef.current = { kind: 'group', id: label, group: null, startX: e.clientX, startY: e.clientY, moved: false }
    attachDocListeners()
  }

  // ── Data derived from state ───────────────────────────────────────────────
  const freeProjects = projects.filter(p => p.is_free)
  const filteredFree = freeProjects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))

  // Favorites: all projects (real + free) that are starred (shown only when not searching)
  const favorites = projects.filter(p => p.favorite)

  // Grouping considers ALL projects (real + free) so free chats can be in groups
  const allFiltered = projects.filter(p => p.name.toLowerCase().includes(search.toLowerCase()))
  const grouped = new Map<string, Project[]>()
  for (const g of groups.groups) grouped.set(g, [])
  const ungrouped: Project[] = []
  for (const p of allFiltered) {
    const g = p.group && groups.groups.includes(p.group) ? p.group : null
    if (g) grouped.get(g)!.push(p)
    else if (!p.is_free) ungrouped.push(p)
    // is_free without group → shown in dedicated free section below
  }
  // Free chats without a group — shown in the dedicated bottom section
  const ungroupedFree = filteredFree.filter(p => !p.group || !groups.groups.includes(p.group ?? ''))

  // ── Folder tree derivation (spec-061: nested folders) ─────────────────────
  // Folders are full '/'-separated paths. groups.groups is a flat ORDERED list.
  // The tree is DERIVED: parent/child come from splitting paths on '/'.
  //
  // folderSet = every path in groups.groups PLUS all ancestor prefixes
  // (defensive — backend guarantees ancestors but we synthesize if missing).
  // folderOrder = stable order: first appearance in groups.groups, with any
  // synthesized ancestor inserted just before its first descendant.
  const folderOrder: string[] = []
  const folderSeen = new Set<string>()
  for (const path of groups.groups) {
    const segs = path.split('/')
    // Emit ancestor prefixes first so a synthesized parent precedes its child.
    for (let i = 1; i <= segs.length; i++) {
      const prefix = segs.slice(0, i).join('/')
      if (!folderSeen.has(prefix)) {
        folderSeen.add(prefix)
        folderOrder.push(prefix)
      }
    }
  }
  // folderSeen is the folderSet (every path + synthesized ancestors); folderOrder
  // is the same set in render order. We render via folderOrder, so folderSeen is
  // not referenced further — kept implicit in folderOrder.

  function folderDepth(path: string): number {
    return (path.match(/\//g) || []).length
  }
  function folderParent(path: string): string | null {
    const idx = path.lastIndexOf('/')
    return idx === -1 ? null : path.slice(0, idx)
  }
  function folderLeaf(path: string): string {
    return path.split('/').pop() ?? path
  }
  // Children of a folder (or top-level when parent === null), in first-appearance order.
  function childFolders(parentPath: string | null): string[] {
    return folderOrder.filter(p => folderParent(p) === parentPath)
  }
  // Projects whose assignment equals exactly this folder path.
  function projectsIn(path: string): Project[] {
    return grouped.get(path) ?? []
  }
  // Total projects in a folder's whole subtree (folder + all descendants) — count badge.
  function subtreeProjectCount(path: string): number {
    let n = (grouped.get(path)?.length ?? 0)
    const prefix = path + '/'
    for (const f of folderOrder) {
      if (f.startsWith(prefix)) n += (grouped.get(f)?.length ?? 0)
    }
    return n
  }
  // Is `candidate` the folder itself or a descendant of `path`? (for Move-folder targets)
  function isSelfOrDescendant(candidate: string, path: string): boolean {
    return candidate === path || candidate.startsWith(path + '/')
  }

  // Effective collapsed state for a folder path: the in-memory override wins,
  // otherwise fall back to localStorage (spec-061: persist collapse across reloads).
  function isGroupCollapsed(path: string): boolean {
    return groupCollapsed[path] ?? readCollapsed(LS_GROUP_PREFIX + path, false)
  }

  function toggleGroupCollapse(path: string) {
    const next = !isGroupCollapsed(path)
    writeCollapsed(LS_GROUP_PREFIX + path, next)
    setGroupCollapsed(prev => ({ ...prev, [path]: next }))
  }

  function toggleArchivedCollapse() {
    setArchivedCollapsed(prev => {
      writeCollapsed(LS_ARCHIVED_COLLAPSED, !prev)
      return !prev
    })
  }

  function toggleFavoritesCollapse() {
    setFavoritesCollapsed(prev => {
      writeCollapsed(LS_FAVORITES_COLLAPSED, !prev)
      return !prev
    })
  }

  // ── Action menu section builders ──────────────────────────────────────────

  function buildProjectMenuSections(p: Project): ActionMenuSection[] {
    const pid = p.id
    const isFree = p.is_free === true

    // --- "Move to group" submenu contents ---
    // Every folder path (full path shown), in tree order including synthesized
    // ancestors, so a project can be dropped into any folder level.
    const groupSubItems = folderOrder.map(g => ({
      label: g,
      checked: p.group === g,
      disabled: p.group === g,
      onClick: () => { if (p.group !== g) doSetGroup(pid, g) },
    }))

    const moveToGroupSubmenu: ActionMenuSection[] = [
      {
        items: [
          {
            label: 'No group',
            checked: !p.group,
            disabled: !p.group,
            onClick: () => doSetGroup(pid, null),
          },
          ...groupSubItems,
          {
            label: '+ New group…',
            onClick: async () => {
              const tempName = nextTopLevelGroupName()
              try {
                const data = await api.createGroup(tempName)
                setGroups({ groups: data.groups, assignments: data.assignments })
                await doSetGroup(pid, tempName)
                startRenameGroup(tempName)
              } catch {
                showToast(t['common.error'], 'error')
              }
            },
          },
        ],
      },
    ]

    // The drill-in item that navigates into the submenu
    const moveToGroupItem: ActionMenuItem = {
      label: 'Move to group',
      submenu: moveToGroupSubmenu,
    }

    // --- Free chat menu (short top-level) ---
    if (isFree) {
      return [
        {
          // Section A: primary action
          items: [
            { label: 'Open', onClick: () => onSelect(pid) },
          ],
        },
        {
          // Section B: favorite
          items: [
            {
              label: p.favorite ? t['sidebar.remove_from_favorites'] : t['sidebar.add_to_favorites'],
              onClick: () => doSetFavorite(pid, !p.favorite),
            },
            // Section C: move to group (drill-in)
            moveToGroupItem,
          ],
        },
        {
          // Section D: danger
          items: [
            {
              label: 'Delete free chat',
              icon: '🗑',
              danger: true,
              onClick: () => setConfirmDelete({ id: pid, name: p.name }),
            },
          ],
        },
      ]
    }

    // --- Normal project menu (short top-level) ---
    return [
      {
        // Section A: primary actions
        items: [
          { label: 'Open', onClick: () => onSelect(pid) },
          { label: t['sidebar.rename_project'], icon: '✏', onClick: () => onRenameProject?.(p) },
          ...(onOpenProjectSettings
            ? [{ label: 'Settings', icon: '⚙', onClick: () => onOpenProjectSettings(pid) }]
            : []),
        ],
      },
      {
        // Section B: favorite + move to group (drill-in)
        items: [
          {
            label: p.favorite ? t['sidebar.remove_from_favorites'] : t['sidebar.add_to_favorites'],
            onClick: () => doSetFavorite(pid, !p.favorite),
          },
          moveToGroupItem,
        ],
      },
      {
        // Section C: danger
        items: [
          {
            label: 'Archive',
            icon: '🗄',
            danger: true,
            onClick: () => setConfirmArchive({ id: pid, name: p.name }),
          },
        ],
      },
    ]
  }

  function buildGroupMenuSections(fullPath: string): ActionMenuSection[] {
    const isCollapsed = isGroupCollapsed(fullPath)

    // "Move folder to…" targets = Top level + every OTHER folder that is not
    // itself and not one of its descendants (can't move a folder into itself).
    const moveTargets: ActionMenuItem[] = [
      {
        label: t['sidebar.top_level'],
        checked: folderParent(fullPath) === null,
        disabled: folderParent(fullPath) === null,
        onClick: () => doMoveFolder(fullPath, null),
      },
      ...folderOrder
        .filter(f => !isSelfOrDescendant(f, fullPath))
        .map(f => ({
          label: f,
          checked: folderParent(fullPath) === f,
          disabled: folderParent(fullPath) === f,
          onClick: () => doMoveFolder(fullPath, f),
        })),
    ]

    return [
      {
        items: [
          { label: 'Rename', icon: '✏', onClick: () => startRenameGroup(fullPath) },
          { label: t['sidebar.new_subfolder'], icon: '📁', onClick: () => doCreateSubfolder(fullPath) },
          { label: t['sidebar.move_folder'], icon: '↪', submenu: [{ items: moveTargets }] },
          {
            label: 'New project in group',
            icon: '+',
            onClick: () => { onNewProject(fullPath) },
          },
          {
            label: isCollapsed ? 'Expand' : 'Collapse',
            onClick: () => toggleGroupCollapse(fullPath),
          },
        ],
      },
      {
        items: [
          {
            label: t['sidebar.delete_folder'],
            icon: '🗑',
            danger: true,
            onClick: () => setConfirmDeleteGroup(fullPath),
          },
        ],
      },
    ]
  }

  // ── Recursive folder renderer (spec-061: nested folders) ──────────────────
  // Renders one folder (header + body). The body holds child folders FIRST
  // (recursed) then this folder's own projects. Indentation is purely CSS —
  // it comes from the nested `.sidebar-group-body`, NOT per-depth padding here.
  function renderFolder(fullPath: string): JSX.Element {
    const isCollapsed = isGroupCollapsed(fullPath)
    const depth = folderDepth(fullPath)
    const leaf = folderLeaf(fullPath)
    const subtreeCount = subtreeProjectCount(fullPath)
    const isGroupDragging = dragSubject?.kind === 'group' && dragSubject.label === fullPath
    const isGroupDragOver = dragOverGroup === fullPath
    const isGroupHeaderDragOver = dragOverGroupHeader === fullPath
    const kids = childFolders(fullPath)
    const ownProjects = projectsIn(fullPath)

    return (
      <div
        key={fullPath}
        className={[
          'sidebar-group',
          isGroupDragging ? 'group-dragging' : '',
          isGroupDragOver ? 'group-drag-over' : '',
        ].filter(Boolean).join(' ')}
        data-group={fullPath}
        data-depth={depth}
      >
        <div
          className={[
            'sidebar-group-header',
            isGroupHeaderDragOver ? 'group-header-drag-over' : '',
          ].filter(Boolean).join(' ')}
          data-group-header={fullPath}
          onPointerDown={e => handleGroupPointerDown(e, fullPath)}
          onClick={e => {
            if (suppressClick.current) return
            // Only toggle collapse when click is NOT on an interactive child
            const target = e.target as HTMLElement
            if (target.closest('.sidebar-kebab-btn, .sidebar-group-rename-input')) return
            toggleGroupCollapse(fullPath)
          }}
          onContextMenu={e => {
            e.preventDefault()
            const rect = new DOMRect(e.clientX, e.clientY, 0, 0)
            openActionMenu(rect, { kind: 'group', label: fullPath })
          }}
          onDoubleClick={() => startRenameGroup(fullPath)}
        >
          <span className="sidebar-group-toggle">{isCollapsed ? '▶' : '▼'}</span>
          <span className="sidebar-group-folder-icon">{isCollapsed ? '📁' : '📂'}</span>
          {renamingGroup === fullPath ? (
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
            <span className="sidebar-group-label">{leaf}</span>
          )}
          <span className="sidebar-group-count">{subtreeCount}</span>
          {/* Kebab for folder — opens action menu */}
          <KebabButton
            label="Group actions"
            onClick={rect => openActionMenu(rect, { kind: 'group', label: fullPath })}
          />
        </div>
        {/* Body: nested child folders FIRST, then this folder's own projects. */}
        {!isCollapsed && (
          <div className="sidebar-group-body">
            {kids.map(child => renderFolder(child))}
            {ownProjects.map(renderProjectItem)}
            {kids.length === 0 && ownProjects.length === 0 && (
              <div className="sidebar-group-drop-hint">drop projects here</div>
            )}
          </div>
        )}
      </div>
    )
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
        onClick={() => { if (suppressClick.current) return; onSelect(p.id) }}
        onContextMenu={e => {
          // Desktop right-click: open action menu anchored to click position
          e.preventDefault()
          // Build a synthetic DOMRect from the click coordinates
          const rect = new DOMRect(e.clientX, e.clientY, 0, 0)
          openActionMenu(rect, { kind: 'project', id: p.id, group: p.group ?? null })
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
        {/* Favorite star — visibility:hidden when inactive to avoid layout shift */}
        <button
          className={`fav-star-btn${p.favorite ? ' fav-star-active' : ''}`}
          onPointerDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); doSetFavorite(p.id, !p.favorite) }}
          title={p.favorite ? t['sidebar.remove_from_favorites'] : t['sidebar.add_to_favorites']}
        >{p.favorite ? '⭐' : '☆'}</button>
        {p.is_free && (
          <button
            className="free-delete-btn"
            onPointerDown={e => e.stopPropagation()}
            onClick={e => { e.stopPropagation(); setConfirmDelete({ id: p.id, name: p.name }) }}
            title={t['sidebar.delete_free_chat']}
          >✕</button>
        )}
        {/* Explicit kebab button — the ONLY way to open the action menu on touch */}
        <KebabButton
          label="More actions"
          onClick={rect => openActionMenu(rect, { kind: 'project', id: p.id, group: p.group ?? null })}
        />
      </div>
    )
  }

  // ── Collapsed sidebar ─────────────────────────────────────────────────────
  if (collapsed) {
    return (
      <div className={`sidebar sidebar-collapsed-mode${drawerOpen ? ' drawer-open' : ''}`}>
        <button className="sidebar-toggle-btn collapsed" onClick={onToggleCollapse} title={t['sidebar.expand']}>☰</button>
      </div>
    )
  }

  const hasSearch = search.length > 0

  return (
    <div className={`sidebar${drawerOpen ? ' drawer-open' : ''}`}>
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">⚡</div>
          <span className="sidebar-logo-text">Cardloop</span>
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

      {/* New project + New group row */}
      <div className="sidebar-new-btns">
        <button className="new-project-btn" onClick={() => onNewProject()} disabled={newProjectBusy} title={t['sidebar.new_project_hint']}>
          {newProjectBusy ? '⏳ creating…' : `＋ ${t['sidebar.new_project']}`}
        </button>
        <button className="new-group-btn" onClick={doCreateGroup} title="New group">
          ＋ group
        </button>
      </div>

      {/* Global tools — Terminal / Vault / Files / Schedules / Settings.
          Moved here from the top tab bar so they are reachable on mobile
          (the sidebar is the off-canvas drawer on phones). */}
      <div className="sidebar-tools">
        <button className="sidebar-tool-btn"
                onClick={onOpenSearch} title="Search (Ctrl/Cmd+K)" aria-label="Search">🔍</button>
        <button className={`sidebar-tool-btn${terminalActive ? ' active' : ''}`}
                onClick={onOpenTerminal} title="Terminal" aria-label="Terminal">⌨</button>
        <button className={`sidebar-tool-btn${vaultActive ? ' active' : ''}`}
                onClick={onOpenVault} title="Vault" aria-label="Vault">🔐</button>
        <button className={`sidebar-tool-btn${usageActive ? ' active' : ''}`}
                onClick={onOpenUsage} title="Usage & cost" aria-label="Usage">📊</button>
        <button className={`sidebar-tool-btn${globalFilesActive ? ' active' : ''}`}
                onClick={onOpenGlobalFiles} title="Server files (~)" aria-label="Server files">📁</button>
        <button className={`sidebar-tool-btn${schedulesActive ? ' active' : ''}`}
                onClick={onOpenSchedules} title="Schedules" aria-label="Schedules">🗓</button>
        <button className={`sidebar-tool-btn${settingsGlobalActive ? ' active' : ''}`}
                onClick={onOpenSettingsGlobal} title="Global settings" aria-label="Global settings">⚙</button>
      </div>

      <div className="projects-list">
        {loading ? (
          <div className="projects-empty">{t['sidebar.loading']}</div>
        ) : (
          <>
            {/* Favorites section — pinned above groups, only when not searching */}
            {!hasSearch && favorites.length > 0 && (
              <div className="sidebar-group sidebar-favorites-section">
                <div className="sidebar-group-header" onClick={toggleFavoritesCollapse}>
                  <span className="sidebar-group-toggle">{favoritesCollapsed ? '▶' : '▼'}</span>
                  <span className="sidebar-group-label">{t['sidebar.favorites']}</span>
                  <span className="sidebar-group-count">{favorites.length}</span>
                </div>
                {!favoritesCollapsed && favorites.map(renderProjectItem)}
              </div>
            )}

            {/* Named folders — recursive tree (spec-061). Top level = folders
                with no parent, in first-appearance order. Each folder recurses
                into its child folders then its own projects. */}
            {!hasSearch && childFolders(null).map(top => renderFolder(top))}

            {/* Ungrouped section or search results */}
            {hasSearch ? (
              <>
                {allFiltered.length === 0 ? (
                  <div className="projects-empty">{t['sidebar.no_results']}</div>
                ) : (
                  allFiltered.map(renderProjectItem)
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

            {/* Free chats without a group — dedicated bottom section */}
            {ungroupedFree.map(renderProjectItem)}

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
        {theme !== undefined && onThemeChange && (
          <div className="sidebar-footer-theme">
            <span className="sidebar-footer-theme-label">Theme</span>
            <ThemeToggle theme={theme} onChange={onThemeChange} />
          </div>
        )}
        <VersionBadge />
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

      {/* Action menu — adaptive dropdown (desktop) or bottom sheet (mobile) */}
      {actionMenu && (
        <ActionMenu
          anchorRect={actionMenu.anchorRect}
          sections={(() => {
            const tgt = actionMenu.target
            if (tgt.kind === 'group') return buildGroupMenuSections(tgt.label)
            const proj = projects.find(p => p.id === tgt.id)
            if (!proj) return []
            return buildProjectMenuSections(proj)
          })()}
          onClose={closeActionMenu}
        />
      )}

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
          title={t['sidebar.delete_folder']}
          message={t['sidebar.delete_folder_confirm'].replace('{name}', folderLeaf(confirmDeleteGroup))}
          confirmLabel={t['sidebar.delete_folder']}
          danger
          onConfirm={() => doDeleteGroup(confirmDeleteGroup)}
          onCancel={() => setConfirmDeleteGroup(null)}
        />
      )}

      {/* Hard delete modal */}
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

import React, { useState, useRef, useEffect } from 'react'
import { Project } from '../types'
import { UsageBadge } from './UsageBadge'
import { t } from '../i18n'

interface Props {
  projects: Project[]
  activeId: string | null
  unreadBySession: Record<string, number>
  /** Project IDs where the agent finished a run while the tab was not active */
  replyReadyIds?: Set<string>
  /** Project IDs where an agent turn is currently in flight (working indicator) */
  runningIds?: Set<string>
  onActivate: (id: string) => void
  onClose: (id: string) => void
  onRename: (id: string, label: string) => void
  onNewFree: () => void
  /** Called when the user reorders tabs via drag-and-drop; receives the new ordered ID array */
  onReorderOpen?: (newIds: string[]) => void
  globalFilesOpen: boolean
  globalFilesActive: boolean
  onOpenGlobalFiles: () => void
  onCloseGlobalFiles: () => void
  schedulesOpen: boolean
  schedulesActive: boolean
  onOpenSchedules: () => void
  onCloseSchedules: () => void
  vaultOpen: boolean
  vaultActive: boolean
  onOpenVault: () => void
  onCloseVault: () => void
  terminalOpen: boolean
  terminalActive: boolean
  onOpenTerminal: () => void
  onCloseTerminal: () => void
  /** Toggles the mobile off-canvas sidebar drawer */
  onToggleDrawer?: () => void
  /** Current mobile navigation screen ('list' | 'project') */
  mobileScreen?: 'list' | 'project'
  /** Navigate back to the project list screen on mobile */
  onGoToProjectList?: () => void
}

function TabItem({
  project, isActive, unread, replyReady, isRunning, onActivate, onClose, onRename, activeRef,
  dragActive, dragOver,
  onPointerDown, onPointerMove, onPointerUp, onPointerCancel,
}: {
  project: Project
  isActive: boolean
  unread: number
  /** True when the agent finished a reply while this tab was not active */
  replyReady?: boolean
  /** True while an agent turn is in flight for this project */
  isRunning?: boolean
  onActivate: () => void
  onClose: () => void
  onRename: (label: string) => void
  activeRef?: React.RefObject<HTMLDivElement>
  /** True when this tab is the one being dragged */
  dragActive?: boolean
  /** 'before' | 'after' | null — drop indicator position relative to this tab */
  dragOver?: 'before' | 'after' | null
  onPointerDown?: (e: React.PointerEvent) => void
  onPointerMove?: (e: React.PointerEvent) => void
  onPointerUp?: (e: React.PointerEvent) => void
  onPointerCancel?: (e: React.PointerEvent) => void
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
      ref={activeRef}
      className={`ptab ${isActive ? 'active' : ''} ${project.is_free ? 'ptab-free' : ''}`}
      data-tab-id={project.id}
      data-drag-active={dragActive ? '' : undefined}
      data-drag-over={dragOver ?? undefined}
      onClick={() => !editing && onActivate()}
      onDoubleClick={() => {
        if (project.is_free) setEditing(true)
      }}
      title={editing ? '' : (project.is_free ? `${project.cwd} (double-click to rename)` : project.cwd)}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
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

      {!editing && isRunning && (
        <span className="ptab-working" title={t['tabbar.working_title']} aria-label={t['tabbar.working_title']} />
      )}
      {!editing && unread > 0 && !isActive && (
        <span className="ptab-unread" title={`${unread} new`}>{unread > 99 ? '99+' : unread}</span>
      )}
      {!editing && replyReady && !isActive && !isRunning && (
        <span className="ptab-reply-ready" title={t['tabbar.awaiting_title']} />
      )}
      {!editing && isActive && (
        <button
          className="ptab-close"
          onClick={(e) => { e.stopPropagation(); onClose() }}
          title="Close tab"
        >
          ✕
        </button>
      )}
      {!editing && (
        <span className="ptab-drag-handle" aria-label="Drag to reorder" title="Drag to reorder">
          <i /><i />
        </span>
      )}
    </div>
  )
}

export function ProjectTabBar({
  projects, activeId, unreadBySession, replyReadyIds, runningIds, onActivate, onClose, onRename, onNewFree,
  onReorderOpen,
  globalFilesOpen, globalFilesActive, onOpenGlobalFiles, onCloseGlobalFiles,
  schedulesOpen, schedulesActive, onOpenSchedules, onCloseSchedules,
  vaultOpen, vaultActive, onOpenVault, onCloseVault,
  terminalOpen, terminalActive, onOpenTerminal, onCloseTerminal,
  onToggleDrawer, mobileScreen, onGoToProjectList,
}: Props) {
  const activeTabRef = useRef<HTMLDivElement>(null)

  // H2: Open-tabs dropdown state + click-outside handling
  const [tabMenuOpen, setTabMenuOpen] = useState(false)
  const tabMenuRef = useRef<HTMLDivElement>(null)

  // ── Drag-and-drop reorder state ────────────────────────────────────────────
  const [dragId, setDragId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  const [dragOverSide, setDragOverSide] = useState<'before' | 'after' | null>(null)
  const dragPointerState = useRef<{
    id: string
    startX: number
    startY: number
    moved: boolean
    pointerId: number
  } | null>(null)

  function handleTabPointerDown(e: React.PointerEvent, id: string) {
    // Only primary button (mouse) or any pointer type (touch/pen)
    if (e.pointerType === 'mouse' && e.button !== 0) return
    // On touch/pen, only start a reorder drag from the dedicated grip handle —
    // plain swipes elsewhere must scroll the tab strip, taps must activate.
    if (e.pointerType !== 'mouse') {
      const onHandle = (e.target as HTMLElement).closest?.('.ptab-drag-handle')
      if (!onHandle) return
    }
    dragPointerState.current = {
      id,
      startX: e.clientX,
      startY: e.clientY,
      moved: false,
      pointerId: e.pointerId,
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }

  function handleTabPointerMove(e: React.PointerEvent, id: string) {
    const ps = dragPointerState.current
    if (!ps || ps.id !== id) return

    const dx = e.clientX - ps.startX
    const dy = e.clientY - ps.startY

    // Activate drag after 8px movement threshold
    if (!ps.moved && Math.sqrt(dx * dx + dy * dy) > 8) {
      ps.moved = true
      setDragId(id)
    }

    if (!ps.moved) return

    // Detect which tab we are hovering over using elementsFromPoint (works through pointer capture)
    const els = document.elementsFromPoint(e.clientX, e.clientY)
    const tabEl = els.find(el => el.hasAttribute('data-tab-id')) as HTMLElement | undefined

    if (tabEl && tabEl.getAttribute('data-tab-id') !== id) {
      const overId = tabEl.getAttribute('data-tab-id')!
      const rect = tabEl.getBoundingClientRect()
      const side: 'before' | 'after' = e.clientX < rect.left + rect.width / 2 ? 'before' : 'after'
      setDragOverId(overId)
      setDragOverSide(side)
    } else {
      setDragOverId(null)
      setDragOverSide(null)
    }
  }

  function handleTabPointerUp(e: React.PointerEvent, id: string) {
    const ps = dragPointerState.current
    if (!ps || ps.id !== id) return
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)

    if (ps.moved && dragOverId && dragOverId !== id && onReorderOpen) {
      const ids = projects.map(p => p.id)
      const fromIdx = ids.indexOf(id)
      const toIdx = ids.indexOf(dragOverId)
      if (fromIdx !== -1 && toIdx !== -1) {
        const next = [...ids]
        // Remove the dragged item first
        next.splice(fromIdx, 1)
        // After removal, the target index may have shifted down by 1 if fromIdx < toIdx
        const adjustedTo = fromIdx < toIdx ? toIdx - 1 : toIdx
        // Insert before or after the drop target
        const insertAt = dragOverSide === 'after' ? adjustedTo + 1 : adjustedTo
        next.splice(insertAt, 0, id)
        onReorderOpen(next)
      }
    }

    dragPointerState.current = null
    setDragId(null)
    setDragOverId(null)
    setDragOverSide(null)
  }

  function handleTabPointerCancel(_e: React.PointerEvent, id: string) {
    const ps = dragPointerState.current
    if (!ps || ps.id !== id) return
    dragPointerState.current = null
    setDragId(null)
    setDragOverId(null)
    setDragOverSide(null)
  }

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (tabMenuRef.current && !tabMenuRef.current.contains(e.target as Node)) {
        setTabMenuOpen(false)
      }
    }
    if (tabMenuOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [tabMenuOpen])

  // D5: auto-scroll active tab into view when activeId changes
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ behavior: 'smooth', inline: 'nearest' })
  }, [activeId])

  return (
    <div className="project-tabbar">
      {/* Hamburger — only visible on tablet/mobile (hidden on desktop via CSS) */}
      {/* On mobile project screen: acts as back-to-list. On desktop or list: opens drawer. */}
      <button
        className={`ptab-hamburger${mobileScreen === 'project' ? ' ptab-hamburger-back' : ''}`}
        onClick={mobileScreen === 'project' && onGoToProjectList ? onGoToProjectList : onToggleDrawer}
        title={mobileScreen === 'project' ? 'Back to project list' : 'Open sidebar'}
        aria-label={mobileScreen === 'project' ? 'Back to project list' : 'Open sidebar'}
      >
        {mobileScreen === 'project' ? '‹' : '☰'}
      </button>
      {/* H2: Open-tabs dropdown menu — mobile only. Lives OUTSIDE .ptab-list:
          the list has overflow-x:auto which would clip the absolute dropdown. */}
      {projects.length > 0 && (
        <div className="ptab-menu-wrap" ref={tabMenuRef}>
            <button
              className="ptab-menu-btn"
              onClick={() => setTabMenuOpen(s => !s)}
              title={t['tabbar.open_tabs_menu']}
              aria-label={t['tabbar.open_tabs_count']}
            >
              ▾ {projects.length}
            </button>
            {tabMenuOpen && (
              <div className="ptab-menu-dropdown" role="listbox">
                {projects.map(p => {
                  const sk = p.session_key ?? null
                  const unread = sk ? (unreadBySession[sk] || 0) : 0
                  const isActive = p.id === activeId
                  const hasIncidents = (p.incidents ?? 0) > 0
                  const isRunning = runningIds?.has(p.id)
                  return (
                    <button
                      key={p.id}
                      className={`ptab-menu-item${isActive ? ' active' : ''}`}
                      role="option"
                      aria-selected={isActive}
                      onClick={() => { onActivate(p.id); setTabMenuOpen(false) }}
                    >
                      <span className="ptab-menu-name">{p.name}</span>
                      {isRunning && (
                        <span className="ptab-menu-dot ptab-menu-dot-working" title={t['tabbar.working_title']} />
                      )}
                      {replyReadyIds?.has(p.id) && !isActive && !isRunning && (
                        <span className="ptab-menu-dot ptab-menu-dot-green" title={t['tabbar.awaiting_title']} />
                      )}
                      {hasIncidents && (
                        <span className="ptab-menu-badge" title={`${p.incidents} incident(s)`}>
                          🚨 {p.incidents}
                        </span>
                      )}
                      {unread > 0 && !isActive && (
                        <span className="ptab-unread">{unread > 99 ? '99+' : unread}</span>
                      )}
                    </button>
                  )
                })}
              </div>
            )}
        </div>
      )}
      <div className="ptab-list">
        {projects.map(p => {
          const sk = p.session_key ?? null
          const unread = sk ? (unreadBySession[sk] || 0) : 0
          const isActive = p.id === activeId
          return (
            <TabItem
              key={p.id}
              project={p}
              isActive={isActive}
              unread={unread}
              replyReady={replyReadyIds?.has(p.id)}
              isRunning={runningIds?.has(p.id)}
              onActivate={() => onActivate(p.id)}
              onClose={() => onClose(p.id)}
              onRename={(label) => onRename(p.id, label)}
              activeRef={isActive ? activeTabRef : undefined}
              dragActive={dragId === p.id}
              dragOver={dragOverId === p.id ? dragOverSide : null}
              onPointerDown={(e) => handleTabPointerDown(e, p.id)}
              onPointerMove={(e) => handleTabPointerMove(e, p.id)}
              onPointerUp={(e) => handleTabPointerUp(e, p.id)}
              onPointerCancel={(e) => handleTabPointerCancel(e, p.id)}
            />
          )
        })}
        {/* Server files special tab */}
        {globalFilesOpen && (
          <div
            className={`ptab ptab-global-files ${globalFilesActive ? 'active' : ''}`}
            onClick={onOpenGlobalFiles}
            title="Server files (~)"
          >
            <span className="ptab-name">📁 Files</span>
            {globalFilesActive && (
              <button
                className="ptab-close"
                onClick={e => { e.stopPropagation(); onCloseGlobalFiles() }}
                title="Close"
              >✕</button>
            )}
          </div>
        )}
        {/* Schedules special tab */}
        {schedulesOpen && (
          <div
            className={`ptab ptab-global-files ${schedulesActive ? 'active' : ''}`}
            onClick={onOpenSchedules}
            title="Schedules"
          >
            <span className="ptab-name">🗓 Schedules</span>
            {schedulesActive && (
              <button
                className="ptab-close"
                onClick={e => { e.stopPropagation(); onCloseSchedules() }}
                title="Close"
              >✕</button>
            )}
          </div>
        )}
        {/* Vault special tab */}
        {vaultOpen && (
          <div
            className={`ptab ptab-global-files ${vaultActive ? 'active' : ''}`}
            onClick={onOpenVault}
            title="Vault"
          >
            <span className="ptab-name">🔐 Vault</span>
            {vaultActive && (
              <button
                className="ptab-close"
                onClick={e => { e.stopPropagation(); onCloseVault() }}
                title="Close"
              >✕</button>
            )}
          </div>
        )}
        {/* Terminal special tab */}
        {terminalOpen && (
          <div
            className={`ptab ptab-global-files ${terminalActive ? 'active' : ''}`}
            onClick={onOpenTerminal}
            title="Terminal"
          >
            <span className="ptab-name">⌨ Terminal</span>
            {terminalActive && (
              <button
                className="ptab-close"
                onClick={e => { e.stopPropagation(); onCloseTerminal() }}
                title="Close"
              >✕</button>
            )}
          </div>
        )}
        <button
          className="ptab-new"
          onClick={onNewFree}
          title="New free chat"
        >
          +
        </button>
      </div>
      <div className="ptab-spacer" />
      {/* Terminal button */}
      <button
        className={`ptab-folder-btn${terminalActive ? ' active' : ''}`}
        onClick={onOpenTerminal}
        title="Terminal"
      >
        ⌨
      </button>
      {/* Vault button */}
      <button
        className={`ptab-folder-btn${vaultActive ? ' active' : ''}`}
        onClick={onOpenVault}
        title="Vault"
      >
        🔐
      </button>
      {/* Schedules button */}
      <button
        className={`ptab-folder-btn${schedulesActive ? ' active' : ''}`}
        onClick={onOpenSchedules}
        title="Schedules"
      >
        🗓
      </button>
      {/* Global file browser button */}
      <button
        className={`ptab-folder-btn${globalFilesActive ? ' active' : ''}`}
        onClick={onOpenGlobalFiles}
        title="Server files (~)"
      >
        📁
      </button>
      <UsageBadge />
    </div>
  )
}

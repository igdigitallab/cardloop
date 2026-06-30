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
  usageOpen: boolean
  usageActive: boolean
  onOpenUsage: () => void
  onCloseUsage: () => void
  terminalOpen: boolean
  terminalActive: boolean
  onOpenTerminal: () => void
  onCloseTerminal: () => void
  settingsGlobalOpen: boolean
  settingsGlobalActive: boolean
  onOpenSettingsGlobal: () => void
  onCloseSettingsGlobal: () => void
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

      {/* Single trailing slot: close on the active tab, otherwise ONE status indicator
          (priority running → reply-ready → unread) instead of three stacked badges. */}
      {!editing && (isActive ? (
        <button
          className="ptab-close"
          onClick={(e) => { e.stopPropagation(); onClose() }}
          title="Close tab"
        >
          ✕
        </button>
      ) : isRunning ? (
        <span className="ptab-working" title={t['tabbar.working_title']} aria-label={t['tabbar.working_title']} />
      ) : replyReady ? (
        <span className="ptab-reply-ready" title={t['tabbar.awaiting_title']} />
      ) : unread > 0 ? (
        <span className="ptab-unread" title={`${unread} new`}>{unread > 99 ? '99+' : unread}</span>
      ) : null)}
    </div>
  )
}

export function ProjectTabBar({
  projects, activeId, unreadBySession, replyReadyIds, runningIds, onActivate, onClose, onRename, onNewFree,
  onReorderOpen,
  globalFilesOpen, globalFilesActive, onOpenGlobalFiles, onCloseGlobalFiles,
  schedulesOpen, schedulesActive, onOpenSchedules, onCloseSchedules,
  vaultOpen, vaultActive, onOpenVault, onCloseVault,
  usageOpen, usageActive, onOpenUsage, onCloseUsage,
  terminalOpen, terminalActive, onOpenTerminal, onCloseTerminal,
  settingsGlobalOpen, settingsGlobalActive, onOpenSettingsGlobal, onCloseSettingsGlobal,
  onToggleDrawer, mobileScreen, onGoToProjectList,
}: Props) {
  const activeTabRef = useRef<HTMLDivElement>(null)

  // ── Drag-and-drop reorder state (desktop mouse only; touch reorder removed) ──
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
    // Never start a drag from interactive controls inside the tab (close button,
    // rename input) — otherwise pointer capture hijacks their click.
    if ((e.target as HTMLElement).closest?.('.ptab-close, .ptab-rename-input')) return
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
    // NOTE: do NOT setPointerCapture here. Capturing on pointerdown makes the
    // subsequent `click` event dispatch to the capture element (the tab), so the
    // close button's onClick never fires. Capture only once a drag truly starts.
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
      // Capture now (not on pointerdown) so taps/clicks still reach their target
      try { (e.currentTarget as HTMLElement).setPointerCapture(ps.pointerId) } catch { /* noop */ }
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
    // Only release if we actually captured (capture happens on drag start, not pointerdown)
    if (ps.moved) {
      try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId) } catch { /* noop */ }
    }

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
        {/* Usage dashboard special tab */}
        {usageOpen && (
          <div
            className={`ptab ptab-global-files ${usageActive ? 'active' : ''}`}
            onClick={onOpenUsage}
            title="Usage & cost"
          >
            <span className="ptab-name">📊 Usage</span>
            {usageActive && (
              <button
                className="ptab-close"
                onClick={e => { e.stopPropagation(); onCloseUsage() }}
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
        {/* Global settings special tab */}
        {settingsGlobalOpen && (
          <div
            className={`ptab ptab-global-files ${settingsGlobalActive ? 'active' : ''}`}
            onClick={onOpenSettingsGlobal}
            title="Global settings"
          >
            <span className="ptab-name">⚙ Settings</span>
            {settingsGlobalActive && (
              <button
                className="ptab-close"
                onClick={e => { e.stopPropagation(); onCloseSettingsGlobal() }}
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
      {/* Global tool launchers (Terminal / Vault / Schedules / Files / Settings)
          now live in the sidebar tools row — see Sidebar.tsx. Kept off the top bar
          to declutter it and to give the launchers mobile parity (sidebar = drawer). */}
      <UsageBadge onOpen={onOpenUsage} />
    </div>
  )
}

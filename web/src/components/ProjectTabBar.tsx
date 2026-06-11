import React, { useState, useRef, useEffect } from 'react'
import { Project } from '../types'
import { UsageBadge } from './UsageBadge'

interface Props {
  projects: Project[]
  activeId: string | null
  unreadBySession: Record<string, number>
  /** Project IDs where the agent finished a run while the tab was not active */
  replyReadyIds?: Set<string>
  onActivate: (id: string) => void
  onClose: (id: string) => void
  onRename: (id: string, label: string) => void
  onNewFree: () => void
  globalFilesOpen: boolean
  globalFilesActive: boolean
  onOpenGlobalFiles: () => void
  onCloseGlobalFiles: () => void
  schedulesOpen: boolean
  schedulesActive: boolean
  onOpenSchedules: () => void
  onCloseSchedules: () => void
  /** Toggles the mobile off-canvas sidebar drawer */
  onToggleDrawer?: () => void
}

function TabItem({
  project, isActive, unread, replyReady, onActivate, onClose, onRename, activeRef,
}: {
  project: Project
  isActive: boolean
  unread: number
  /** True when the agent finished a reply while this tab was not active */
  replyReady?: boolean
  onActivate: () => void
  onClose: () => void
  onRename: (label: string) => void
  activeRef?: React.RefObject<HTMLDivElement>
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
      onClick={() => !editing && onActivate()}
      onDoubleClick={() => {
        if (project.is_free) setEditing(true)
      }}
      title={editing ? '' : (project.is_free ? `${project.cwd} (double-click to rename)` : project.cwd)}
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
        <span className="ptab-unread" title={`${unread} new`}>{unread > 99 ? '99+' : unread}</span>
      )}
      {!editing && replyReady && !isActive && (
        <span className="ptab-reply-ready" title="Agent reply is ready" />
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
    </div>
  )
}

export function ProjectTabBar({
  projects, activeId, unreadBySession, replyReadyIds, onActivate, onClose, onRename, onNewFree,
  globalFilesOpen, globalFilesActive, onOpenGlobalFiles, onCloseGlobalFiles,
  schedulesOpen, schedulesActive, onOpenSchedules, onCloseSchedules,
  onToggleDrawer,
}: Props) {
  const activeTabRef = useRef<HTMLDivElement>(null)

  // D5: auto-scroll active tab into view when activeId changes
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ behavior: 'smooth', inline: 'nearest' })
  }, [activeId])

  return (
    <div className="project-tabbar">
      {/* Hamburger — only visible on tablet/mobile (hidden on desktop via CSS) */}
      <button
        className="ptab-hamburger"
        onClick={onToggleDrawer}
        title="Open sidebar"
        aria-label="Open sidebar"
      >
        ☰
      </button>
      <div className="ptab-list">
        {projects.map(p => {
          const sk = p.tg_thread != null ? String(p.tg_thread) : null
          const unread = sk ? (unreadBySession[sk] || 0) : 0
          const isActive = p.id === activeId
          return (
            <TabItem
              key={p.id}
              project={p}
              isActive={isActive}
              unread={unread}
              replyReady={replyReadyIds?.has(p.id)}
              onActivate={() => onActivate(p.id)}
              onClose={() => onClose(p.id)}
              onRename={(label) => onRename(p.id, label)}
              activeRef={isActive ? activeTabRef : undefined}
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
        <button
          className="ptab-new"
          onClick={onNewFree}
          title="New free chat"
        >
          +
        </button>
      </div>
      <div className="ptab-spacer" />
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

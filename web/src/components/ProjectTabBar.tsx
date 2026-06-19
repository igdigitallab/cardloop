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
  /** Toggles the mobile off-canvas sidebar drawer */
  onToggleDrawer?: () => void
  /** Current mobile navigation screen ('list' | 'project') */
  mobileScreen?: 'list' | 'project'
  /** Navigate back to the project list screen on mobile */
  onGoToProjectList?: () => void
}

function TabItem({
  project, isActive, unread, replyReady, isRunning, onActivate, onClose, onRename, activeRef,
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
    </div>
  )
}

export function ProjectTabBar({
  projects, activeId, unreadBySession, replyReadyIds, runningIds, onActivate, onClose, onRename, onNewFree,
  globalFilesOpen, globalFilesActive, onOpenGlobalFiles, onCloseGlobalFiles,
  schedulesOpen, schedulesActive, onOpenSchedules, onCloseSchedules,
  vaultOpen, vaultActive, onOpenVault, onCloseVault,
  onToggleDrawer, mobileScreen, onGoToProjectList,
}: Props) {
  const activeTabRef = useRef<HTMLDivElement>(null)

  // H2: Open-tabs dropdown state + click-outside handling
  const [tabMenuOpen, setTabMenuOpen] = useState(false)
  const tabMenuRef = useRef<HTMLDivElement>(null)

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
        <button
          className="ptab-new"
          onClick={onNewFree}
          title="New free chat"
        >
          +
        </button>
      </div>
      <div className="ptab-spacer" />
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

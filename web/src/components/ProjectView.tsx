import { useState } from 'react'
import { Project, TabId } from '../types'
import { OverviewTab } from '../tabs/OverviewTab'
import { ReadmeTab } from '../tabs/ReadmeTab'
import { ClaudeMdTab } from '../tabs/ClaudeMdTab'
import { SpecsTab } from '../tabs/SpecsTab'
import { ActivityTab } from '../tabs/ActivityTab'
import { BoardTab } from '../tabs/BoardTab'
import { ChatTab } from '../tabs/ChatTab'
import { FilesTab } from '../tabs/FilesTab'

interface Tab {
  id: TabId
  label: string
  disabled?: boolean
}

// Chat is no longer a tab — it lives in the permanent right panel
const TABS: Tab[] = [
  { id: 'overview',   label: 'Обзор' },
  { id: 'readme',     label: 'README' },
  { id: 'claude-md',  label: 'CLAUDE.md' },
  { id: 'specs',      label: 'Specs' },
  { id: 'activity',   label: 'Активность' },
  { id: 'board',      label: 'Доска' },
  { id: 'files',      label: 'Файлы' },
]

interface Props {
  project: Project
}

export function ProjectView({ project }: Props) {
  const [activeTab, setActiveTab] = useState<TabId>('overview')
  const git = project.health.git

  return (
    <div className="main-content project-split-layout">
      {/* LEFT: header + tabs + content (~55%) */}
      <div className="project-left-pane">
        <div className="project-header">
          <div className="project-header-top">
            <div className="project-header-icon">
              {project.name.charAt(0).toUpperCase()}
            </div>
            <div>
              <div className="project-title">{project.name}</div>
              <div className="project-meta-row">
                <span className="meta-chip">
                  <code>{project.cwd}</code>
                </span>
                <span className="meta-chip">{project.model}</span>
                {git && (
                  <span className="git-status">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                      style={{ opacity: 0.5 }}>
                      <circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/>
                      <path d="M6 21V9a9 9 0 0 0 9 9"/>
                    </svg>
                    <span className="git-branch">{git.branch}</span>
                    {git.dirty > 0 && (
                      <span className="git-dirty" title={`${git.dirty} изменённых файлов`}>
                        ~{git.dirty}
                      </span>
                    )}
                    {git.unpushed > 0 && (
                      <span className="git-unpushed" title={`${git.unpushed} не отправлено`}>
                        ↑{git.unpushed}
                      </span>
                    )}
                  </span>
                )}
              </div>
            </div>
          </div>

          <nav className="tabs">
            {TABS.map(tab => (
              <button
                key={tab.id}
                className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
                disabled={tab.disabled}
                onClick={() => !tab.disabled && setActiveTab(tab.id)}
              >
                {tab.label}
                {tab.disabled && <span className="tab-soon">скоро</span>}
              </button>
            ))}
          </nav>
        </div>

        <div className="tab-content">
          {activeTab === 'overview'  && <OverviewTab project={project} />}
          {activeTab === 'readme'    && <ReadmeTab projectId={project.id} />}
          {activeTab === 'claude-md' && <ClaudeMdTab projectId={project.id} />}
          {activeTab === 'specs'     && <SpecsTab projectId={project.id} />}
          {activeTab === 'activity'  && <ActivityTab projectId={project.id} />}
          {activeTab === 'board'     && <BoardTab projectId={project.id} />}
          {activeTab === 'files'     && <FilesTab projectId={project.id} />}
        </div>
      </div>

      {/* RIGHT: permanent chat panel (~45%) */}
      <div className="project-chat-pane">
        <div className="project-chat-pane-header">
          💬 Чат по проекту
        </div>
        <ChatTab projectId={project.id} />
      </div>
    </div>
  )
}

// DisabledTab reserved for future tabs that are not yet implemented
export function DisabledTab({ name, icon }: { name: string; icon: string }) {
  return (
    <div className="tab-placeholder">
      <div className="tab-placeholder-icon">{icon}</div>
      <h3>{name}</h3>
      <p>Эта функция появится в следующих фазах</p>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { Project, ProjectStructureHealth } from '../types'
import { api } from '../api'
import { ProjectStructureCardFull } from '../components/ProjectStructureCard'
import { t } from '../i18n'

const ONBOARDING_MARKERS = ['Заполнить во время онбординга', 'инициализируется']

function WelcomeBanner({ projectId }: { projectId: string }) {
  const [show, setShow] = useState(false)

  useEffect(() => {
    api.claudeMd(projectId)
      .then(d => {
        if (!d.content) return
        const matched = ONBOARDING_MARKERS.some(m => d.content.includes(m))
        setShow(matched)
      })
      .catch(() => { /* ignore */ })
  }, [projectId])

  if (!show) return null

  return (
    <div className="welcome-banner">
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{t['overview.initializing']}</div>
      <div style={{ fontSize: 13, color: 'var(--text2)', lineHeight: 1.5 }}>
        Claude задаёт вопросы в чате справа → ответь, чтобы оформить проект.
      </div>
    </div>
  )
}

interface Props {
  project: Project
  health: ProjectStructureHealth | null
  refreshHealth: () => void
}

export function OverviewTab({ project, health, refreshHealth }: Props) {
  const git = project.health.git

  return (
    <div>
      <WelcomeBanner projectId={project.id} />

      <div className="overview-grid">
        <div className="info-card">
          <div className="info-card-label">{t['overview.cwd']}</div>
          <div className="info-card-value mono">{project.cwd}</div>
        </div>

        <div className="info-card">
          <div className="info-card-label">{t['overview.tg_thread']}</div>
          <div className="info-card-value">
            {project.tg_thread !== null ? (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>#{project.tg_thread}</span>
            ) : (
              <span style={{ color: 'var(--text3)' }}>{t['overview.not_bound']}</span>
            )}
          </div>
        </div>
      </div>

      {git ? (
        <div className="git-card">
          <div className="git-card-header">{t['overview.git_state']}</div>
          <div className="git-stats">
            <div className="git-stat">
              <span className="git-stat-label">{t['overview.git_branch']}</span>
              <span className="git-stat-value" style={{ fontSize: 14, fontWeight: 500, color: 'var(--accent-h)' }}>
                {git.branch}
              </span>
            </div>
            <div className="git-stat">
              <span className="git-stat-label">{t['overview.git_changes']}</span>
              <span className={`git-stat-value ${git.dirty > 0 ? 'warn' : 'ok'}`}>
                {git.dirty}
              </span>
            </div>
            <div className="git-stat">
              <span className="git-stat-label">{t['overview.git_unpushed']}</span>
              <span className={`git-stat-value ${git.unpushed > 0 ? 'warn' : 'ok'}`}>
                {git.unpushed}
              </span>
            </div>
          </div>
        </div>
      ) : (
        <div className="no-content">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          {t['overview.git_unavailable']}
        </div>
      )}

      <ProjectStructureCardFull
        projectId={project.id}
        health={health}
        refreshHealth={refreshHealth}
      />
    </div>
  )
}

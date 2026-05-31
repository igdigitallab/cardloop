import { useEffect, useState } from 'react'
import { Project, TestResult } from '../types'
import { api } from '../api'
import { ProjectStructureCard } from '../components/ProjectStructureCard'

interface Props {
  project: Project
}

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
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Идёт инициализация</div>
      <div style={{ fontSize: 13, color: 'var(--text2)', lineHeight: 1.5 }}>
        Claude задаёт вопросы в чате справа → ответь, чтобы оформить проект.
      </div>
    </div>
  )
}

function TestRunner({ projectId }: { projectId: string }) {
  const [running, setRunning] = useState(false)
  const [res, setRes] = useState<TestResult | null>(null)
  const [err, setErr] = useState('')

  async function run() {
    setRunning(true); setErr(''); setRes(null)
    try {
      setRes(await api.runTests(projectId))
    } catch (e: unknown) {
      setErr(String(e instanceof Error ? e.message : e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="git-card test-card">
      <div className="test-card-header">
        <span className="git-card-header" style={{ margin: 0 }}>Тесты</span>
        <button className="doc-btn primary" onClick={run} disabled={running}>
          {running ? 'Запускаю…' : '▶ Запустить'}
        </button>
      </div>
      {err && <div className="error-state">⚠ {err}</div>}
      {res && !res.detected && (
        <div className="test-status dim">{res.output}</div>
      )}
      {res && res.detected && (
        <>
          <div className={`test-status ${res.ok ? 'ok' : 'fail'}`}>
            {res.ok ? '✓ прошли' : (res.timed_out ? '⏱ таймаут' : '✗ упали')}
            {' · '}<span className="mono">{res.cmd}</span>
            {res.exit_code != null && res.exit_code >= 0 ? ` · exit ${res.exit_code}` : ''}
          </div>
          <pre className="test-output">{res.output || '(пустой вывод)'}</pre>
        </>
      )}
    </div>
  )
}

export function OverviewTab({ project }: Props) {
  const git = project.health.git

  return (
    <div>
      <WelcomeBanner projectId={project.id} />

      <div className="overview-grid">
        <div className="info-card">
          <div className="info-card-label">Рабочая директория</div>
          <div className="info-card-value mono">{project.cwd}</div>
        </div>

        <div className="info-card">
          <div className="info-card-label">Модель</div>
          <div className="info-card-value">{project.model}</div>
        </div>

        <div className="info-card">
          <div className="info-card-label">Telegram тред</div>
          <div className="info-card-value">
            {project.tg_thread !== null ? (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>#{project.tg_thread}</span>
            ) : (
              <span style={{ color: 'var(--text3)' }}>не привязан</span>
            )}
          </div>
        </div>
      </div>

      {git ? (
        <div className="git-card">
          <div className="git-card-header">Git состояние</div>
          <div className="git-stats">
            <div className="git-stat">
              <span className="git-stat-label">Ветка</span>
              <span className="git-stat-value" style={{ fontSize: 14, fontWeight: 500, color: 'var(--accent-h)' }}>
                {git.branch}
              </span>
            </div>
            <div className="git-stat">
              <span className="git-stat-label">Изменений</span>
              <span className={`git-stat-value ${git.dirty > 0 ? 'warn' : 'ok'}`}>
                {git.dirty}
              </span>
            </div>
            <div className="git-stat">
              <span className="git-stat-label">Не отправлено</span>
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
          Git недоступен для этого проекта
        </div>
      )}

      <ProjectStructureCard projectId={project.id} />

      <TestRunner projectId={project.id} />
    </div>
  )
}

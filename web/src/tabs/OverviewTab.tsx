import { useEffect, useState } from 'react'
import { Project, TestResult } from '../types'
import { api } from '../api'
import { ProjectStructureCard } from '../components/ProjectStructureCard'
import { t } from '../i18n'

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
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{t['overview.initializing']}</div>
      <div style={{ fontSize: 13, color: 'var(--text2)', lineHeight: 1.5 }}>
        Claude задаёт вопросы в чате справа → ответь, чтобы оформить проект.
      </div>
    </div>
  )
}

function IncidentScanner({ project }: { project: Project }) {
  const [running, setRunning] = useState(false)
  const [res, setRes] = useState<{ ok: boolean; scanned: number; added: number; updated: number; error?: string } | null>(null)
  const [lastScan, setLastScan] = useState<string | null>(null)

  const hasSource = !!(project.log_cmd || project.test_cmd)

  async function scan() {
    setRunning(true)
    setRes(null)
    try {
      const r = await api.scanErrors(project.id)
      setRes(r)
      setLastScan(new Date().toLocaleTimeString('ru'))
    } catch (e: unknown) {
      setRes({ ok: false, scanned: 0, added: 0, updated: 0, error: String(e instanceof Error ? e.message : e) })
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="git-card test-card">
      <div className="test-card-header">
        <span className="git-card-header" style={{ margin: 0 }}>🩺 Сканер инцидентов</span>
        <button
          className="doc-btn primary"
          onClick={scan}
          disabled={running || !hasSource}
          title={hasSource ? t['overview.scan_hint_with_source'] : t['overview.scan_hint_no_source']}
        >
          {running ? t['overview.scanning'] : t['overview.scan']}
        </button>
      </div>
      {!hasSource && (
        <div className="test-status dim">
          Не настроены источники. Добавь <code>log_cmd</code> и/или <code>test_cmd</code> в <code>data/topics.json</code> для этого проекта.
          <br />
          Пример: <code>"log_cmd": "journalctl -u my-svc -n 300 --no-pager"</code>, <code>"test_cmd": "venv/bin/python -m pytest -q"</code>.
        </div>
      )}
      {hasSource && !res && !running && (
        <div className="test-status dim">
          Источники: {project.log_cmd ? '📜 logs' : ''}{project.log_cmd && project.test_cmd ? ' + ' : ''}{project.test_cmd ? '🧪 tests' : ''}.
          Авто-скан каждые 5 мин в фоне.
        </div>
      )}
      {res && (
        <div className={`test-status ${res.ok ? (res.added > 0 ? 'warn' : 'ok') : 'fail'}`}>
          {res.error ? `⚠ ${res.error}` : (
            <>
              {res.added > 0 ? `🚨 ${res.added} новых` : '✓ новых инцидентов нет'}
              {res.updated > 0 && ` · ↻ ${res.updated} обновлено`}
              {res.scanned > 0 && ` · просмотрено ${res.scanned} событий`}
              {lastScan && ` · в ${lastScan}`}
            </>
          )}
        </div>
      )}
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
        <span className="git-card-header" style={{ margin: 0 }}>{t['overview.tests']}</span>
        <button className="doc-btn primary" onClick={run} disabled={running}>
          {running ? t['overview.running_tests'] : t['overview.run_tests']}
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
          <div className="info-card-label">{t['overview.cwd']}</div>
          <div className="info-card-value mono">{project.cwd}</div>
        </div>

        <div className="info-card">
          <div className="info-card-label">{t['overview.model']}</div>
          <div className="info-card-value">{project.model}</div>
        </div>

        <div className="info-card">
          <div className="info-card-label">Telegram тред</div>
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
          <div className="git-card-header">Git состояние</div>
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
          Git недоступен для этого проекта
        </div>
      )}

      <ProjectStructureCard projectId={project.id} />

      <IncidentScanner project={project} />

      <TestRunner projectId={project.id} />
    </div>
  )
}

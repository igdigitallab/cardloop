import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../api'
import { Project, ProjectSettings, GlobalSettings, GlobalSettingsEffective, AgentsConfig, ProjectStructureHealth } from '../types'
import { Spinner } from '../components/Spinner'
import { SecretsTab } from './SecretsTab'
import { MODELS } from '../lib/models'
import { ProjectStructureCardFull } from '../components/ProjectStructureCard'
import { t } from '../i18n'

const ONBOARDING_MARKERS = ['Fill in during onboarding', 'initializing']

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
        Claude is asking questions in the chat panel → answer to set up the project.
      </div>
    </div>
  )
}

interface Props {
  projectId: string
  project: Project
  health: ProjectStructureHealth | null
  refreshHealth: () => void
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

// Label + control row with a hint
function Row({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, padding: '10px 0', borderTop: '1px solid var(--border)' }}>
      <span>
        <b style={{ fontSize: 13 }}>{title}</b>
        {hint && <span style={{ display: 'block', fontSize: 11, color: 'var(--text3)', fontWeight: 400, marginTop: 2, maxWidth: 430 }}>{hint}</span>}
      </span>
      <span style={{ flexShrink: 0, paddingTop: 2 }}>{children}</span>
    </div>
  )
}

export function SettingsTab({ projectId, project, health, refreshHealth }: Props) {
  const [proj, setProj] = useState<ProjectSettings | null>(null)
  const [glob, setGlob] = useState<GlobalSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [projMsg, setProjMsg] = useState('')
  const [globMsg, setGlobMsg] = useState('')
  const [savingProj, setSavingProj] = useState(false)
  const [savingGlob, setSavingGlob] = useState(false)
  const [archiving, setArchiving] = useState(false)
  const [archiveMsg, setArchiveMsg] = useState('')
  const [confirmArchiveLocal, setConfirmArchiveLocal] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setProj(null); setProjMsg(''); setGlobMsg('')
    Promise.all([api.projectSettings(projectId), api.settings()])
      .then(([p, g]) => { if (!cancelled) { setProj(p); setGlob(g); setLoading(false) } })
      .catch(e => { if (!cancelled) { setError(errMsg(e)); setLoading(false) } })
    return () => { cancelled = true }
  }, [projectId])

  async function saveProj() {
    if (!proj) return
    setSavingProj(true); setProjMsg('')
    try {
      // Clean agents_config: strip keys with empty/undefined model values before sending
      const rawCfg = proj.agents_config ?? {}
      const cleanCfg: AgentsConfig = {}
      if (rawCfg.executor_model)   cleanCfg.executor_model   = rawCfg.executor_model
      if (rawCfg.researcher_model) cleanCfg.researcher_model = rawCfg.researcher_model
      if (rawCfg.quick_model)      cleanCfg.quick_model      = rawCfg.quick_model
      if (rawCfg.conductor_prompt !== undefined) cleanCfg.conductor_prompt = rawCfg.conductor_prompt
      const r = await api.saveProjectSettings(projectId, { ...proj, agents_config: cleanCfg })
      setProj(r.settings); setProjMsg('Saved ✓')
    } catch (e) { setProjMsg('⚠ ' + errMsg(e)) }
    finally { setSavingProj(false) }
  }

  async function saveGlob() {
    if (!glob) return
    setSavingGlob(true); setGlobMsg('')
    const ef = glob.effective
    try {
      const r = await api.saveSettings({
        scan_interval_sec: ef.scan_interval_sec,
        default_model: ef.default_model || '',
        watchdog_stall_sec: ef.watchdog_stall_sec,
        watchdog_max_sec: ef.watchdog_max_sec,
        board_card_model: ef.board_card_model || '',
      })
      setGlobMsg(`Saved ✓ (${Object.keys(r.stored).length} override(s))`)
    } catch (e) { setGlobMsg('⚠ ' + errMsg(e)) }
    finally { setSavingGlob(false) }
  }

  if (loading) return <Spinner label="Loading settings…" />
  if (error) return <div className="error-state">⚠ {error}</div>
  if (!proj || !glob) return null

  const e = glob.effective
  const setE = (patch: Partial<GlobalSettingsEffective>) =>
    setGlob({ ...glob, effective: { ...e, ...patch } })

  const git = project.health.git

  return (
    <div style={{ maxWidth: 660, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 28, padding: '6px 4px 32px' }}>

      {/* ── Project info ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>Project info</h3>
        <WelcomeBanner projectId={projectId} />

        <div className="overview-grid" style={{ marginTop: 8 }}>
          <div className="info-card">
            <div className="info-card-label">{t['overview.cwd']}</div>
            <div className="info-card-value mono">{project.cwd}</div>
          </div>

          <div className="info-card">
            <div className="info-card-label">{t['overview.tg_thread']}</div>
            <div className="info-card-value">
              {project.session_key ? (
                <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{project.session_key}</span>
              ) : (
                <span style={{ color: 'var(--text3)' }}>{t['overview.not_bound']}</span>
              )}
            </div>
          </div>
        </div>

        {git ? (
          <div className="git-card" style={{ marginTop: 8 }}>
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
          <div className="no-content" style={{ marginTop: 8 }}>
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
          projectId={projectId}
          health={health}
          refreshHealth={refreshHealth}
        />
      </section>

      {/* ── Project settings ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>Project settings</h3>
        <p style={{ margin: '0 0 6px', fontSize: 12, color: 'var(--text3)' }}>
          Per-project only (stored in topics.json, hot-reloaded).
        </p>

        <Row title="Git sync"
             hint="Off — cockpit does not use git: cards run directly in the folder (no worktree branches), git-sync button is disabled, health does not require .git. All conversation history is preserved; existing .git is not physically touched.">
          <input type="checkbox" checked={proj.git_enabled}
                 onChange={ev => setProj({ ...proj, git_enabled: ev.target.checked })}
                 aria-label="Git sync" />
        </Row>

        <Row title="TG error notifications" hint="Ping in Telegram on new incidents in Failed.">
          <input type="checkbox" checked={proj.notify_on_error}
                 onChange={ev => setProj({ ...proj, notify_on_error: ev.target.checked })}
                 aria-label="TG error notifications" />
        </Row>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 0', borderTop: '1px solid var(--border)' }}>
          <b style={{ fontSize: 13 }}>log_cmd</b>
          <input type="text" className="doc-textarea"
                 style={{ height: 'auto', padding: '6px 8px', fontSize: 13, fontFamily: 'monospace' }}
                 placeholder="journalctl -u my-service -n 300 --no-pager"
                 value={proj.log_cmd} onChange={ev => setProj({ ...proj, log_cmd: ev.target.value })} />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 0', borderTop: '1px solid var(--border)' }}>
          <b style={{ fontSize: 13 }}>test_cmd</b>
          <input type="text" className="doc-textarea"
                 style={{ height: 'auto', padding: '6px 8px', fontSize: 13, fontFamily: 'monospace' }}
                 placeholder="venv/bin/python -m pytest -q"
                 value={proj.test_cmd} onChange={ev => setProj({ ...proj, test_cmd: ev.target.value })} />
        </div>

        <h4 style={{ margin: '14px 0 4px', fontSize: 13, fontWeight: 600, color: 'var(--text2)' }}>Sub-agents</h4>
        <p style={{ margin: '0 0 6px', fontSize: 11, color: 'var(--text3)' }}>
          Per-project model overrides for executor / researcher / quick agents. Empty = global default.
        </p>

        <Row title="Executor model" hint="Agent for code and infra runs (default: sonnet).">
          <select
            value={proj.agents_config?.executor_model ?? ''}
            onChange={ev => setProj({ ...proj, agents_config: { ...proj.agents_config, executor_model: ev.target.value || undefined } })}
          >
            <option value="">— global default —</option>
            {MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>

        <Row title="Researcher model" hint="Read-only research agent (default: sonnet).">
          <select
            value={proj.agents_config?.researcher_model ?? ''}
            onChange={ev => setProj({ ...proj, agents_config: { ...proj.agents_config, researcher_model: ev.target.value || undefined } })}
          >
            <option value="">— global default —</option>
            {MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>

        <Row title="Quick model" hint="Fast lookup agent (default: haiku).">
          <select
            value={proj.agents_config?.quick_model ?? ''}
            onChange={ev => setProj({ ...proj, agents_config: { ...proj.agents_config, quick_model: ev.target.value || undefined } })}
          >
            <option value="">— global default —</option>
            {MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>

        <Row title="Conductor prompt" hint="When off, fable sessions do not receive the orchestrator directive.">
          <input
            type="checkbox"
            checked={proj.agents_config?.conductor_prompt ?? true}
            onChange={ev => setProj({ ...proj, agents_config: { ...proj.agents_config, conductor_prompt: ev.target.checked } })}
            aria-label="Conductor prompt"
          />
        </Row>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
          <button className="doc-btn primary" onClick={saveProj} disabled={savingProj}>
            {savingProj ? 'Saving…' : 'Save'}
          </button>
          {projMsg && <span style={{ fontSize: 12, color: 'var(--text2)' }}>{projMsg}</span>}
        </div>
      </section>

      {/* ── Global settings ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>Global settings</h3>
        <p style={{ margin: '0 0 6px', fontSize: 12, color: 'var(--text3)' }}>
          Cockpit-wide (data/settings.json). Overrides env defaults at runtime.
        </p>

        <Row title="Incident scanner interval, sec" hint="30–3600">
          <input type="number" min={30} max={3600} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.scan_interval_sec}
                 onChange={ev => setE({ scan_interval_sec: Number(ev.target.value) })} />
        </Row>

        <Row title="Default model for new projects"
             hint="Applied when creating a new project. Existing projects are not affected.">
          <select value={e.default_model} onChange={ev => setE({ default_model: ev.target.value })}>
            {MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>

        <Row title={t['settings.board_card_model']} hint={t['settings.board_card_model_hint']}>
          <select
            value={e.board_card_model || ''}
            onChange={ev => setE({ board_card_model: ev.target.value })}
          >
            <option value="">{t['settings.board_card_model_default']}</option>
            {MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>

        <Row title="Watchdog: silence, sec" hint="No events longer than this → interrupt task. 30–7200">
          <input type="number" min={30} max={7200} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.watchdog_stall_sec}
                 onChange={ev => setE({ watchdog_stall_sec: Number(ev.target.value) })} />
        </Row>

        <Row title="Watchdog: task ceiling, sec" hint="Total task time limit. 60–14400">
          <input type="number" min={60} max={14400} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.watchdog_max_sec}
                 onChange={ev => setE({ watchdog_max_sec: Number(ev.target.value) })} />
        </Row>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
          <button className="doc-btn primary" onClick={saveGlob} disabled={savingGlob}>
            {savingGlob ? 'Saving…' : 'Save'}
          </button>
          {globMsg && <span style={{ fontSize: 12, color: 'var(--text2)' }}>{globMsg}</span>}
        </div>
      </section>

      {/* ── Project secrets ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>{'\u{1F511}'} Project secrets</h3>
        <p style={{ margin: '0 0 12px', fontSize: 12, color: 'var(--text3)' }}>
          Environment variables available to the agent when running tasks. Not committed to git.
        </p>
        <SecretsTab projectId={projectId} />
      </section>

      {/* ── Danger zone ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15, color: 'var(--red)' }}>Danger zone</h3>
        <p style={{ margin: '0 0 12px', fontSize: 12, color: 'var(--text3)' }}>
          Destructive actions. Archive moves the project out of the active list; it can be restored from the sidebar.
        </p>
        {archiveMsg && (
          <p style={{ margin: '0 0 8px', fontSize: 12, color: 'var(--text2)' }}>{archiveMsg}</p>
        )}
        {confirmArchiveLocal ? (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={{ fontSize: 13, color: 'var(--text2)' }}>Archive "{project.name}"?</span>
            <button
              className="btn-danger"
              onClick={async () => {
                setArchiving(true)
                setArchiveMsg('')
                try {
                  await api.archiveProject(projectId)
                  setArchiveMsg('Project archived. You can restore it from the sidebar.')
                } catch (e) { setArchiveMsg('⚠ ' + errMsg(e)) }
                setArchiving(false)
                setConfirmArchiveLocal(false)
              }}
              disabled={archiving}
            >
              {archiving ? 'Archiving…' : 'Confirm archive'}
            </button>
            <button className="btn-secondary" onClick={() => setConfirmArchiveLocal(false)} disabled={archiving}>
              Cancel
            </button>
          </div>
        ) : (
          <button
            className="btn-danger"
            style={{ fontSize: 13 }}
            onClick={() => setConfirmArchiveLocal(true)}
          >
            🗄 Archive project
          </button>
        )}
      </section>

    </div>
  )
}

import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../api'
import { ProjectSettings, GlobalSettings, GlobalSettingsEffective } from '../types'
import { Spinner } from '../components/Spinner'
import { SecretsTab } from './SecretsTab'
import { MODELS } from '../lib/models'

interface Props { projectId: string }

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

export function SettingsTab({ projectId }: Props) {
  const [proj, setProj] = useState<ProjectSettings | null>(null)
  const [glob, setGlob] = useState<GlobalSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [projMsg, setProjMsg] = useState('')
  const [globMsg, setGlobMsg] = useState('')
  const [savingProj, setSavingProj] = useState(false)
  const [savingGlob, setSavingGlob] = useState(false)

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
      const r = await api.saveProjectSettings(projectId, proj)
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

  return (
    <div style={{ maxWidth: 660, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 28, padding: '6px 4px 32px' }}>

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

    </div>
  )
}

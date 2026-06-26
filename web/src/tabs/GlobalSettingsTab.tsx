import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../api'
import { GlobalSettings, GlobalSettingsEffective } from '../types'
import { Spinner } from '../components/Spinner'
import { MODELS } from '../lib/models'
import { t } from '../i18n'

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

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

/** Global, project-independent cockpit settings (data/settings.json).
 *  Opened from the sidebar tools row — the same global-settings block that
 *  also appears inside a project's Settings tab, surfaced as its own entry. */
export function GlobalSettingsTab() {
  const [glob, setGlob] = useState<GlobalSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setMsg('')
    api.settings()
      .then(g => { if (!cancelled) { setGlob(g); setLoading(false) } })
      .catch(e => { if (!cancelled) { setError(errMsg(e)); setLoading(false) } })
    return () => { cancelled = true }
  }, [])

  async function save() {
    if (!glob) return
    setSaving(true); setMsg('')
    const ef = glob.effective
    try {
      const r = await api.saveSettings({
        scan_interval_sec: ef.scan_interval_sec,
        default_model: ef.default_model || '',
        watchdog_stall_sec: ef.watchdog_stall_sec,
        watchdog_max_sec: ef.watchdog_max_sec,
        board_card_model: ef.board_card_model || '',
      })
      setMsg(`Saved ✓ (${Object.keys(r.stored).length} override(s))`)
    } catch (e) { setMsg('⚠ ' + errMsg(e)) }
    finally { setSaving(false) }
  }

  if (loading) return <Spinner label="Loading settings…" />
  if (error) return <div className="error-state">⚠ {error}</div>
  if (!glob) return null

  const e = glob.effective
  const setE = (patch: Partial<GlobalSettingsEffective>) =>
    setGlob({ ...glob, effective: { ...e, ...patch } })

  return (
    <div style={{ maxWidth: 660, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 28, padding: '18px 16px 32px', overflowY: 'auto', width: '100%' }}>
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>{'⚙'} Global settings</h3>
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
          <button className="doc-btn primary" onClick={save} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
          {msg && <span style={{ fontSize: 12, color: 'var(--text2)' }}>{msg}</span>}
        </div>
      </section>
    </div>
  )
}

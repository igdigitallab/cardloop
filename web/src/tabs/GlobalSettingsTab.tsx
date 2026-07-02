import { useEffect, useState, useCallback, type ReactNode } from 'react'
import { api } from '../api'
import { GlobalSettings, GlobalSettingsEffective, AutopilotStatus, AutopilotDecision, DirectorResult } from '../types'
import { Spinner } from '../components/Spinner'
import { EditableMarkdown } from '../components/EditableMarkdown'
import { MODELS } from '../lib/models'
import { t } from '../i18n'
import { useNotifications } from '../hooks/useNotifications'
import { useModules } from '../hooks/useModules'
import { BrowserBackendSettings } from '../components/BrowserBackendSettings'

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function relTime(ts: number): string {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return s + 's ago'
  if (s < 3600) return Math.floor(s / 60) + 'm ago'
  if (s < 86400) return Math.floor(s / 3600) + 'h ago'
  return Math.floor(s / 86400) + 'd ago'
}

function formatAction(action: string): string {
  if (action === 'fix_failing_tests') return 'Would fix failing tests'
  if (action === 'run_backlog_card') return 'Would run a backlog card'
  if (action === 'scout') return 'Would propose improvement cards'
  if (action === 'none') return 'Nothing to do'
  return 'Would ' + action.replace(/_/g, ' ')
}

const PRIORITY_STYLE: Record<string, { bg: string; color: string }> = {
  P1: { bg: 'var(--red-subtle, #fef2f2)', color: 'var(--red, #b91c1c)' },
  P3: { bg: 'var(--surface2, #f3f4f6)', color: 'var(--text2, #374151)' },
  P4: { bg: 'var(--surface2, #f3f4f6)', color: 'var(--text3, #6b7280)' },
  P5: { bg: 'var(--surface2, #f3f4f6)', color: 'var(--text3, #6b7280)' },
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
  const [apStatus, setApStatus] = useState<AutopilotStatus | null>(null)
  const [apWorking, setApWorking] = useState(false)
  const [decisions, setDecisions] = useState<AutopilotDecision[]>([])
  const [tickWorking, setTickWorking] = useState(false)
  const [tickNote, setTickNote] = useState('')
  const [directorResults, setDirectorResults] = useState<Record<string, DirectorResult>>({})
  const [directorWorking, setDirectorWorking] = useState<Record<string, boolean>>({})
  const { permission, enabled, setEnabled, requestPermission } = useNotifications()
  const { modules, isEnabled: isModEnabled, setEnabled: setModEnabled } = useModules()
  const [pushTestMsg, setPushTestMsg] = useState('')
  const [pushTesting, setPushTesting] = useState(false)

  async function sendPushTest() {
    setPushTesting(true); setPushTestMsg('')
    try {
      const r = await api.pushTest()
      setPushTestMsg(
        r.sent > 0
          ? `Sent to ${r.sent} device${r.sent === 1 ? '' : 's'} — you should see it now.`
          : 'No device is subscribed yet. Turn the toggle on above and tap "Allow", then try again.',
      )
    } catch (e) {
      setPushTestMsg(errMsg(e))
    } finally {
      setPushTesting(false)
    }
  }

  const loadDecisions = useCallback(() => {
    void api.autopilotDecisions(20).then(setDecisions).catch(() => setDecisions([]))
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setMsg('')
    Promise.all([api.settings(), api.autopilotStatus().catch(() => null)])
      .then(([g, ap]) => {
        if (!cancelled) { setGlob(g); if (ap) setApStatus(ap); setLoading(false) }
      })
      .catch(e => { if (!cancelled) { setError(errMsg(e)); setLoading(false) } })
    loadDecisions()
    return () => { cancelled = true }
  }, [loadDecisions])

  async function toggleGlobalAutopilot() {
    if (!apStatus || apWorking) return
    setApWorking(true)
    try {
      const r = await api.setAutopilotGlobal(!apStatus.global_enabled)
      setApStatus(r)
    } catch { /* silently fail */ }
    finally { setApWorking(false) }
  }

  async function toggleAutopilotPause() {
    if (!apStatus || apWorking) return
    setApWorking(true)
    try {
      const r = apStatus.paused ? await api.autopilotResume() : await api.autopilotPause()
      setApStatus(r)
    } catch { /* silently fail */ }
    finally { setApWorking(false) }
  }

  async function runTick() {
    if (tickWorking) return
    setTickWorking(true)
    setTickNote('')
    try {
      const result = await api.autopilotTick()
      if (result.decisions?.length) setDecisions(result.decisions)
      else loadDecisions()
      if (!result.active) {
        setTickNote('Autopilot master is OFF — enable it above to let the shadow loop decide.')
      }
    } catch { loadDecisions() }
    finally { setTickWorking(false) }
  }

  async function runDirector(projectId: string) {
    if (directorWorking[projectId]) return
    setDirectorWorking(prev => ({ ...prev, [projectId]: true }))
    try {
      const result = await api.autopilotRunDirector(projectId)
      setDirectorResults(prev => ({ ...prev, [projectId]: result }))
    } catch {
      setDirectorResults(prev => ({
        ...prev,
        [projectId]: { ok: false, reason: 'Request failed', assessment: '', priority: 'P4', focus: '', proposed_cards: [], question_for_operator: null, cards_created: 0, notebook_note: '' },
      }))
    } finally {
      setDirectorWorking(prev => ({ ...prev, [projectId]: false }))
    }
  }

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

      {/* ── Autopilot master control (spec-067) ── */}
      {apStatus !== null && (
        <section>
          <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>Autopilot</h3>
          <p style={{ margin: '0 0 8px', fontSize: 12, color: 'var(--text3)' }}>
            Global master switch. Per-project modes are configured in each project's Settings tab.
          </p>

          <div style={{
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            padding: '10px 14px',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}>
            {/* Status line */}
            <div style={{ fontSize: 12, color: 'var(--text2)' }}>
              {!apStatus.global_enabled
                ? 'Disabled — per-project settings are inactive.'
                : apStatus.paused
                  ? `Enabled · paused · ${apStatus.active_runs} run${apStatus.active_runs === 1 ? '' : 's'} active`
                  : `Enabled · not paused · ${apStatus.active_runs} run${apStatus.active_runs === 1 ? '' : 's'} active`
              }
            </div>

            {/* Controls row */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              {/* Master on/off */}
              <div className="theme-toggle" aria-label="Global autopilot" style={{ opacity: apWorking ? 0.6 : 1 }}>
                <button
                  className={`theme-toggle-btn${!apStatus.global_enabled ? ' active' : ''}`}
                  onClick={() => { if (!apWorking) void toggleGlobalAutopilot() }}
                  disabled={apWorking}
                >
                  Disabled
                </button>
                <button
                  className={`theme-toggle-btn${apStatus.global_enabled ? ' active' : ''}`}
                  onClick={() => { if (!apWorking) void toggleGlobalAutopilot() }}
                  disabled={apWorking}
                >
                  Enabled
                </button>
              </div>

              {/* Pause / resume — only shown when globally enabled */}
              {apStatus.global_enabled && (
                <button
                  className="btn-secondary"
                  style={{ fontSize: 12, padding: '4px 10px', opacity: apWorking ? 0.6 : 1 }}
                  onClick={() => { if (!apWorking) void toggleAutopilotPause() }}
                  disabled={apWorking}
                >
                  {apStatus.paused ? 'Resume' : 'Pause'}
                </button>
              )}
            </div>
          </div>
        </section>
      )}

      {/* ── Autopilot shadow decisions (spec-067) ── */}
      {apStatus !== null && (
        <section>
          <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>What Autopilot would do</h3>
          <p style={{ margin: '0 0 8px', fontSize: 12, color: 'var(--text3)' }}>
            Shadow mode logs what the agent would do. It runs nothing.
          </p>

          <div style={{
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            padding: '10px 14px',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}>
            {/* Tick button row */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <button
                className="btn-secondary"
                style={{ fontSize: 12, padding: '4px 12px', opacity: tickWorking ? 0.6 : 1 }}
                onClick={() => { void runTick() }}
                disabled={tickWorking}
              >
                {tickWorking ? 'Running…' : 'Run a shadow tick now'}
              </button>
              {tickNote && (
                <span style={{ fontSize: 12, color: 'var(--text3)' }}>{tickNote}</span>
              )}
            </div>

            {/* Decision list */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {decisions.length === 0 ? (
                <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>
                  No decisions yet — enable the master switch and run a tick.
                </p>
              ) : (
                decisions.map((d, i) => {
                  const ps = PRIORITY_STYLE[d.priority] ?? PRIORITY_STYLE.P4
                  return (
                    <div
                      key={i}
                      style={{
                        border: '1px solid var(--border)',
                        borderRadius: 'var(--radius)',
                        padding: '7px 10px',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 3,
                      }}
                    >
                      {/* Line 1: priority pill + project + timestamp */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        <span style={{
                          fontSize: 10,
                          fontWeight: 600,
                          padding: '1px 5px',
                          borderRadius: 4,
                          background: ps.bg,
                          color: ps.color,
                          flexShrink: 0,
                        }}>
                          {d.priority}
                        </span>
                        <span style={{ fontSize: 13, fontWeight: 600, flexShrink: 0 }}>{d.project}</span>
                        <span style={{ fontSize: 11, color: 'var(--text3)', marginLeft: 'auto' }}>{relTime(d.ts)}</span>
                      </div>
                      {/* Line 2: action */}
                      <div style={{ fontSize: 12, color: 'var(--text2)' }}>{formatAction(d.action)}</div>
                      {/* Line 3: rationale */}
                      {d.rationale && (
                        <div
                          title={d.rationale}
                          style={{
                            fontSize: 11,
                            color: 'var(--text3)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {d.rationale}
                        </div>
                      )}
                    </div>
                  )
                })
              )}
            </div>
          </div>
        </section>
      )}

      {/* ── Director sub-panel (spec-067) ── */}
      {apStatus !== null && (() => {
        const enabledProjects = Object.entries(apStatus.per_project)
          .filter(([, mode]) => mode === 'propose' || mode === 'auto')
        return (
          <section>
            <h3 style={{ margin: '0 0 2px', fontSize: 15 }}>Director</h3>
            <p style={{ margin: '0 0 8px', fontSize: 12, color: 'var(--text3)' }}>
              The director reads the project and proposes a plan. It writes planning cards and asks you one question — it does not change code.
            </p>

            <div style={{
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius)',
              padding: '10px 14px',
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
            }}>
              {enabledProjects.length === 0 ? (
                <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>
                  No projects have Autopilot enabled. Set a project to Propose or Auto first.
                </p>
              ) : (
                enabledProjects.map(([projectId]) => {
                  const result = directorResults[projectId]
                  const working = !!directorWorking[projectId]
                  const ps = result ? (PRIORITY_STYLE[result.priority] ?? PRIORITY_STYLE.P4) : null
                  return (
                    <div key={projectId} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {/* Project row */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                        <span style={{ fontSize: 13, fontWeight: 600, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {projectId}
                        </span>
                        <button
                          className="btn-secondary"
                          style={{ fontSize: 12, padding: '4px 12px', flexShrink: 0, opacity: working ? 0.6 : 1 }}
                          onClick={() => { void runDirector(projectId) }}
                          disabled={working}
                        >
                          {working ? 'Thinking…' : 'Run director'}
                        </button>
                      </div>

                      {/* Result card */}
                      {result && (
                        <div style={{
                          border: '1px solid var(--border)',
                          borderRadius: 'var(--radius)',
                          padding: '10px 12px',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 8,
                          background: 'var(--surface1, var(--surface))',
                        }}>
                          {!result.ok ? (
                            <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>
                              {result.reason ?? 'Director returned an error.'}
                            </p>
                          ) : (
                            <>
                              {/* Priority pill + focus */}
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                                {ps && (
                                  <span style={{
                                    fontSize: 10,
                                    fontWeight: 600,
                                    padding: '1px 5px',
                                    borderRadius: 4,
                                    background: ps.bg,
                                    color: ps.color,
                                    flexShrink: 0,
                                  }}>
                                    {result.priority}
                                  </span>
                                )}
                                {result.focus && (
                                  <span style={{ fontSize: 12, color: 'var(--text2)', fontStyle: 'italic', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    {result.focus}
                                  </span>
                                )}
                              </div>

                              {/* Assessment */}
                              {result.assessment && (
                                <div>
                                  <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Assessment</span>
                                  <p style={{ fontSize: 12, color: 'var(--text2)', margin: '2px 0 0', lineHeight: 1.5 }}>{result.assessment}</p>
                                </div>
                              )}

                              {/* Proposed cards */}
                              {result.proposed_cards.length > 0 && (
                                <div>
                                  <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Proposed cards</span>
                                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
                                    {result.proposed_cards.map((card, idx) => (
                                      <div key={idx} style={{ fontSize: 12, lineHeight: 1.5 }}>
                                        <span style={{ fontWeight: 600, color: 'var(--text1, var(--text))' }}>{card.title}</span>
                                        {card.why && (
                                          <span style={{ color: 'var(--text3)', marginLeft: 6 }}>{card.why}</span>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                  <p style={{ fontSize: 12, color: 'var(--text3)', margin: '6px 0 0' }}>
                                    {result.cards_created} card{result.cards_created === 1 ? '' : 's'} added to the backlog.
                                  </p>
                                </div>
                              )}

                              {/* Question for operator */}
                              {result.question_for_operator && (
                                <div style={{
                                  border: '1px solid var(--border)',
                                  borderRadius: 'var(--radius)',
                                  padding: '8px 12px',
                                  fontSize: 13,
                                  color: 'var(--text2)',
                                  lineHeight: 1.5,
                                }}>
                                  {'❓ '}{result.question_for_operator}
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })
              )}
            </div>
          </section>
        )
      })()}

      {/* Browser notifications section */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>🔔 Notifications</h3>
        <p style={{ margin: '0 0 6px', fontSize: 12, color: 'var(--text3)' }}>
          Get notified when an agent run finishes — in the foreground, and in the background via
          Web Push once enabled. On a phone, install Cardloop to your home screen first, then turn
          this on and tap “Allow” when prompted.
        </p>

        <Row title={t['notify.settings_label']} hint={t['notify.settings_hint']}>
          {permission === 'unsupported' ? (
            <span style={{ fontSize: 12, color: 'var(--text3)' }}>Not supported in this browser</span>
          ) : permission === 'denied' ? (
            <span style={{ fontSize: 12, color: 'var(--text3)' }}>{t['notify.blocked_hint']}</span>
          ) : (
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
              <input
                type="checkbox"
                checked={enabled}
                onChange={async (ev) => {
                  if (ev.target.checked) {
                    await requestPermission()
                    setEnabled(true)
                  } else {
                    setEnabled(false)
                  }
                }}
              />
              {enabled ? 'On' : 'Off'}
            </label>
          )}
        </Row>
        {permission === 'granted' && enabled && (
          <div style={{ marginTop: 8 }}>
            <button
              className="btn-secondary"
              style={{ fontSize: 13, minHeight: 36 }}
              onClick={() => void sendPushTest()}
              disabled={pushTesting}
            >
              {pushTesting ? 'Sending…' : 'Send test notification'}
            </button>
            {pushTestMsg && (
              <div style={{ fontSize: 12, color: 'var(--text3)', marginTop: 6 }}>{pushTestMsg}</div>
            )}
          </div>
        )}
      </section>

      {/* Spec-065: module/extension registry */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>{t['extensions.section_title']}</h3>
        <p style={{ margin: '0 0 6px', fontSize: 12, color: 'var(--text3)' }}>
          {t['extensions.section_hint']}
        </p>
        {modules.length === 0 ? (
          <p style={{ fontSize: 12, color: 'var(--text3)', paddingTop: 8 }}>
            {t['extensions.empty']}
          </p>
        ) : (
          modules.map(mod => (
            <div key={mod.id}>
              <Row
                title={mod.name}
                hint={mod.description}
              >
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={isModEnabled(mod.id)}
                    onChange={ev => { void setModEnabled(mod.id, ev.target.checked) }}
                  />
                  {isModEnabled(mod.id) ? t['extensions.toggle_on'] : t['extensions.toggle_off']}
                </label>
              </Row>
              {/* spec-066: browser backend config, shown when the browser module is on. */}
              {mod.id === 'browser' && isModEnabled('browser') && <BrowserBackendSettings />}
            </div>
          ))
        )}
      </section>

      {/* Card 931573: global (home) agent-rules CLAUDE.md — view + edit on the server. */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>📋 Main CLAUDE.md</h3>
        <p style={{ margin: '0 0 8px', fontSize: 12, color: 'var(--text3)' }}>
          The global agent rules on the server (<code>$HOME/CLAUDE.md</code>) — routing &amp;
          cross-project conventions the agent reads for every project. Double-click or ✎ to edit,
          Ctrl+Enter to save.
        </p>
        <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 10, maxHeight: '60vh', overflow: 'auto' }}>
          <EditableMarkdown
            projectId=""
            load={api.globalClaudeMd}
            save={api.saveGlobalClaudeMd}
            spinnerLabel="Loading CLAUDE.md…"
            emptyLabel="No global CLAUDE.md yet"
          />
        </div>
      </section>
    </div>
  )
}

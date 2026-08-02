/**
 * spec-066 — Pluggable browser backends UI (Extensions → Browser).
 *
 * Lets the operator pick the browser backend (built-in Chromium / CloakBrowser
 * stealth / external CDP), one-click-install the free CloakBrowser tier, point at a
 * Cloak Manager + store its token in the safe, list/launch/stop/pick persistent
 * profiles, and set the agent-action safety gate. Degrades gracefully — a tier that
 * is unavailable is shown as such; the built-in default always works.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useModules } from '../hooks/useModules'
import type { BrowserBackends, BrowserConfig, CloakProfile } from '../types'

type Backend = BrowserConfig['backend']

function cfgOf(raw: Record<string, unknown> | undefined): BrowserConfig {
  const c = (raw || {}) as Partial<BrowserConfig>
  return {
    backend: (c.backend as Backend) || 'builtin',
    cdp_url: c.cdp_url || '',
    manager_url: c.manager_url || '',
    default_profile: c.default_profile || '',
    per_project_profile: (c.per_project_profile as Record<string, string>) || {},
    agent_actions: (c.agent_actions as 'read' | 'full') || 'read',
  }
}

const box: React.CSSProperties = {
  marginTop: 8, padding: 12, border: '1px solid var(--border)', borderRadius: 8,
  background: 'var(--bg2)', display: 'flex', flexDirection: 'column', gap: 12,
}
const label: React.CSSProperties = { fontSize: 12, fontWeight: 600, color: 'var(--text2)' }
const hint: React.CSSProperties = { fontSize: 11, color: 'var(--text3)', marginTop: 2 }
const input: React.CSSProperties = {
  flex: 1, fontSize: 12, padding: '5px 8px', borderRadius: 6,
  border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)',
}
const btn: React.CSSProperties = {
  fontSize: 12, padding: '5px 10px', borderRadius: 6, cursor: 'pointer',
  border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)',
}

export function BrowserBackendSettings() {
  const { modules, setConfig } = useModules()
  const browserMod = modules.find(m => m.id === 'browser')
  const cfg = cfgOf(browserMod?.config)

  const [backends, setBackends] = useState<BrowserBackends | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [installing, setInstalling] = useState(false)
  const [cdpUrl, setCdpUrl] = useState(cfg.cdp_url)
  const [managerUrl, setManagerUrl] = useState(cfg.manager_url)
  const [token, setToken] = useState('')
  const [profiles, setProfiles] = useState<CloakProfile[] | null>(null)
  const [profErr, setProfErr] = useState('')
  // Real projects (free chats excluded — they all share cwd=$HOME, so a per-cwd mapping
  // there would silently apply to every one of them at once).
  const [projects, setProjects] = useState<{ id: string; name: string; cwd: string }[]>([])
  const pollRef = useRef<number | null>(null)

  const loadBackends = useCallback(async () => {
    try {
      const b = await api.browserBackends()
      setBackends(b)
      // Sync local text fields with server truth (first load only respects edits-in-flight).
      setCdpUrl(prev => prev || b.config.cdp_url)
      setManagerUrl(prev => prev || b.config.manager_url)
      return b
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
      return null
    }
  }, [])

  useEffect(() => { void loadBackends() }, [loadBackends])
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current) }, [])

  const flash = (m: string) => { setMsg(m); window.setTimeout(() => setMsg(''), 2500) }

  const saveConfig = useCallback(async (patch: Partial<BrowserConfig>) => {
    setBusy(true)
    try {
      await setConfig('browser', patch as Record<string, unknown>)
      flash('Saved.')
    } catch (e) {
      flash(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [setConfig])

  const pickBackend = (backend: Backend) => { void saveConfig({ backend }) }

  const install = useCallback(async () => {
    setInstalling(true)
    setMsg('Installing CloakBrowser…')
    try {
      await api.installCloak()
    } catch (e) {
      setInstalling(false)
      flash(e instanceof Error ? e.message : String(e))
      return
    }
    // Poll backend availability until the tier reports installed (or ~2 min timeout).
    let ticks = 0
    pollRef.current = window.setInterval(async () => {
      ticks += 1
      const b = await loadBackends()
      if ((b && b.tiers.cloakbrowser.installed) || ticks > 40) {
        if (pollRef.current) window.clearInterval(pollRef.current)
        pollRef.current = null
        setInstalling(false)
        setMsg(b && b.tiers.cloakbrowser.installed ? 'CloakBrowser installed ✓' : 'Install finished — check the log.')
      }
    }, 3000)
  }, [loadBackends])

  const saveToken = useCallback(async () => {
    setBusy(true)
    try {
      const r = await api.setManagerToken(token)
      setToken('')
      flash(r.token_set ? 'Token stored in the safe ✓' : 'Token cleared.')
      await loadBackends()
    } catch (e) {
      flash(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }, [token, loadBackends])

  const loadProfiles = useCallback(async () => {
    setProfErr('')
    try {
      const r = await api.browserProfiles()
      if (r.error) setProfErr(r.error)
      setProfiles(r.profiles || [])
    } catch (e) {
      setProfErr(e instanceof Error ? e.message : String(e))
      setProfiles([])
    }
  }, [])

  // Project list for the per-project override table.
  useEffect(() => {
    api.projects()
      .then(r => setProjects((r.projects || [])
        .filter(pr => !pr.is_free && pr.cwd)
        .map(pr => ({ id: pr.id, name: pr.name, cwd: pr.cwd }))))
      .catch(() => { /* non-critical — the table just stays hidden */ })
  }, [])

  /**
   * Writes one project's override into per_project_profile (keyed by CWD).
   *  - '__inherit__' removes the key entirely → the project follows default_profile
   *  - '__none__' stores an empty string → an explicit opt-OUT that beats the default
   *  - a profile id pins the project to that profile
   * The whole map is sent because modules.set_config merges per top-level key, so a
   * partial map would not delete a removed entry.
   */
  const setProjectProfile = useCallback(async (cwd: string, value: string) => {
    const next: Record<string, string> = { ...cfg.per_project_profile }
    if (value === '__inherit__') delete next[cwd]
    else if (value === '__none__') next[cwd] = ''
    else next[cwd] = value
    await saveConfig({ per_project_profile: next })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.per_project_profile])

  const profileAction = useCallback(async (id: string, action: 'launch' | 'stop') => {
    try {
      await api.browserProfileAction(id, action)
      await loadProfiles()
    } catch (e) {
      setProfErr(e instanceof Error ? e.message : String(e))
    }
  }, [loadProfiles])

  const cloak = backends?.tiers.cloakbrowser
  const manager = backends?.manager

  return (
    <div style={box}>
      {/* Backend selector */}
      <div>
        <div style={label}>Backend</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
          <BackendRadio
            checked={cfg.backend === 'builtin'} onPick={() => pickBackend('builtin')}
            title="Built-in Chromium" sub="Default · works out of the box · no stealth" disabled={busy}
          />
          <BackendRadio
            checked={cfg.backend === 'cloakbrowser'} onPick={() => pickBackend('cloakbrowser')}
            title="CloakBrowser (stealth)"
            sub={cloak?.installed
              ? `Installed${cloak.binary_ready ? ' · binary ready' : ' · binary not downloaded'}${cloak.version ? ` · v${cloak.version}` : ''}`
              : 'Not installed — anti-detect, free tier (MIT)'}
            disabled={busy}
          />
          <BackendRadio
            checked={cfg.backend === 'external-cdp'} onPick={() => pickBackend('external-cdp')}
            title="External CDP" sub="Connect to any CDP browser or a Cloak Manager profile" disabled={busy}
          />
        </div>
      </div>

      {/* CloakBrowser install */}
      {cfg.backend === 'cloakbrowser' && !cloak?.installed && (
        <div>
          <button style={{ ...btn, borderColor: 'var(--accent)' }} disabled={installing} onClick={() => void install()}>
            {installing ? 'Installing…' : 'Install CloakBrowser (free)'}
          </button>
          <div style={hint}>
            Runs <code>pip install cloakbrowser</code> + the free Chromium binary, detached.
            Manual: <code>venv/bin/pip install cloakbrowser &amp;&amp; venv/bin/python -m cloakbrowser install</code>
          </div>
          {backends?.install_log && (
            <pre style={{ marginTop: 6, maxHeight: 120, overflow: 'auto', fontSize: 10, color: 'var(--text3)', background: 'var(--bg)', padding: 8, borderRadius: 6 }}>
              {backends.install_log.slice(-1200)}
            </pre>
          )}
        </div>
      )}

      {/* External CDP config — a STATIC endpoint only matters for this backend. */}
      {cfg.backend === 'external-cdp' && (
          <div>
            <div style={label}>Static CDP URL <span style={{ fontWeight: 400, color: 'var(--text3)' }}>(optional)</span></div>
            <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
              <input style={input} placeholder="http://host:9222" value={cdpUrl} onChange={e => setCdpUrl(e.target.value)} />
              <button style={btn} disabled={busy} onClick={() => void saveConfig({ cdp_url: cdpUrl })}>Save</button>
            </div>
            <div style={hint}>Direct endpoint (Browserless / Steel / chrome --remote-debugging-port). Leave empty to use a Cloak Manager profile.</div>
          </div>
      )}

      {/* Cloak Manager — deliberately NOT gated on the selected backend. A profile now
          overrides whichever backend is chosen, so it must be reachable from CloakBrowser
          and built-in too. Hiding it behind "External CDP" is exactly why a profile the
          operator had already created could never be attached to a project. */}
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10 }}>
            <div style={label}>Cloak Manager</div>
            <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
              <input style={input} placeholder="https://cloak.example.com" value={managerUrl} onChange={e => setManagerUrl(e.target.value)} />
              <button style={btn} disabled={busy} onClick={() => void saveConfig({ manager_url: managerUrl })}>Save</button>
            </div>
            <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
              <input style={input} type="password" placeholder={manager?.token_set ? '•••••• (token stored)' : 'Manager auth token'} value={token} onChange={e => setToken(e.target.value)} />
              <button style={btn} disabled={busy} onClick={() => void saveToken()}>Save token</button>
            </div>
            <div style={hint}>The token is stored in the encrypted safe, never in modules.json.</div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10 }}>
              <button style={btn} disabled={!manager?.configured} onClick={() => void loadProfiles()}>Load profiles</button>
              {!manager?.configured && <span style={hint}>Set the Manager URL first.</span>}
              {profErr && <span style={{ ...hint, color: 'var(--danger, #d44)' }}>{profErr}</span>}
            </div>
            {profiles && profiles.length === 0 && !profErr && <div style={hint}>No profiles found.</div>}
            {profiles && profiles.map(p => (
              <div key={p.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6, fontSize: 12 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: /run|live|start/i.test(p.status) ? '#3fb950' : 'var(--text3)' }} />
                <span style={{ flex: 1 }}>{p.name} <span style={{ color: 'var(--text3)' }}>({p.status})</span></span>
                {cfg.default_profile === p.id && <span style={{ color: 'var(--accent)', fontSize: 11 }}>● default</span>}
                <button style={btn} onClick={() => void profileAction(p.id, 'launch')}>Launch</button>
                <button style={btn} onClick={() => void profileAction(p.id, 'stop')}>Stop</button>
                <button style={{ ...btn, borderColor: 'var(--accent)' }}
                        onClick={() => void saveConfig({ default_profile: p.id })}>Use for all</button>
              </div>
            ))}
            {cfg.default_profile && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
                <span style={hint}>Every project uses the default profile unless overridden below.</span>
                <button style={btn} onClick={() => void saveConfig({ default_profile: '' })}>Clear default</button>
              </div>
            )}

            {/* Per-project override. The map is keyed by project CWD (that is what
                browser_backends.resolve receives), never by the display name. */}
            {profiles && profiles.length > 0 && projects.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div style={label}>Per project</div>
                <div style={hint}>
                  Projects sharing one profile share its logins &amp; cookies, but each gets its
                  own tab — so they do not fight over the same page.
                </div>
                {projects.map(pr => (
                  <div key={pr.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6, fontSize: 12 }}>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{pr.name}</span>
                    <select
                      style={{ ...input, flex: 'none', minWidth: 170 }}
                      value={pr.cwd in cfg.per_project_profile ? (cfg.per_project_profile[pr.cwd] || '__none__') : '__inherit__'}
                      onChange={e => void setProjectProfile(pr.cwd, e.target.value)}
                    >
                      <option value="__inherit__">
                        {cfg.default_profile ? 'Inherit default' : 'Inherit (no profile)'}
                      </option>
                      <option value="__none__">No profile</option>
                      {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </select>
                  </div>
                ))}
              </div>
            )}
          </div>

      {/* Agent action safety gate */}
      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10 }}>
        <div style={label}>Agent actions</div>
        <div style={{ display: 'flex', gap: 14, marginTop: 6, fontSize: 12 }}>
          <label style={{ display: 'flex', gap: 5, cursor: 'pointer' }}>
            <input type="radio" checked={cfg.agent_actions === 'read'} disabled={busy} onChange={() => void saveConfig({ agent_actions: 'read' })} />
            Read only
          </label>
          <label style={{ display: 'flex', gap: 5, cursor: 'pointer' }}>
            <input type="radio" checked={cfg.agent_actions === 'full'} disabled={busy} onChange={() => void saveConfig({ agent_actions: 'full' })} />
            Full (allow click / type)
          </label>
        </div>
        <div style={hint}>
          On a logged-in profile the agent acts as your identity. <b>Read only</b> (default) lets it
          navigate &amp; read; <b>Full</b> also allows it to click and type (submit/post).
        </div>
      </div>

      {msg && <div style={{ fontSize: 11, color: 'var(--text3)' }}>{msg}</div>}
    </div>
  )
}

function BackendRadio({ checked, onPick, title, sub, disabled }: {
  checked: boolean; onPick: () => void; title: string; sub: string; disabled?: boolean
}) {
  return (
    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: disabled ? 'default' : 'pointer' }}>
      <input type="radio" checked={checked} disabled={disabled} onChange={onPick} style={{ marginTop: 2 }} />
      <span>
        <span style={{ fontSize: 12, fontWeight: 600 }}>{title}</span>
        <span style={{ display: 'block', fontSize: 11, color: 'var(--text3)' }}>{sub}</span>
      </span>
    </label>
  )
}

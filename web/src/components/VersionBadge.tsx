import { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import type { VersionInfo } from '../types'

/**
 * Sidebar-footer version badge (spec-047 workstream A).
 * Shows the running version; when a newer version exists on origin it turns into
 * an Update affordance. Clicking Update (with confirm) POSTs /api/update — the
 * detached updater applies + restarts via restart-self.sh, and the existing
 * SSE reconnect-on-wake brings the cockpit back on the new version.
 */
export function VersionBadge() {
  const [info, setInfo] = useState<VersionInfo | null>(null)
  const [checking, setChecking] = useState(false)
  const [updating, setUpdating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (check?: boolean) => {
    try {
      if (check) setChecking(true)
      const v = await api.version(check)
      setInfo(v)
      if (v.update_status?.state === 'failed') setError(v.update_status.detail)
    } catch { /* offline — keep last known */ }
    finally { if (check) setChecking(false) }
  }, [])

  useEffect(() => { load() }, [load])

  async function onUpdate() {
    if (!info?.update_available) return
    const target = info.latest && info.latest !== info.current ? info.latest : `${info.behind} new commit(s)`
    if (!window.confirm(`Update Cardloop to ${target}?\n\nThe cockpit will restart and reconnect automatically.`)) return
    setError(null)
    try {
      const r = await api.update()
      if (r.status === 'updating') {
        setUpdating(true)   // SSE reconnect-on-wake returns us on the new version
      } else if (r.status === 'up_to_date') {
        load()
      }
    } catch (e) {
      const err = e as { body?: { error?: string }; message?: string }
      setError(err.body?.error || err.message || 'update failed')
    }
  }

  if (!info) return null

  if (updating) {
    return (
      <div className="version-badge version-badge--updating" title="Applying update and restarting">
        <span className="version-spinner" /> Updating… reconnecting
      </div>
    )
  }

  const ver = info.current.replace(/^v/, '')

  return (
    <div className="version-badge">
      <span className="version-badge-name" title={`channel: ${info.channel}`}>
        Cardloop&nbsp;<span className="version-badge-ver">v{ver}</span>
      </span>

      {info.update_available ? (
        <button className="version-update-btn" onClick={onUpdate} title={`Update to ${info.latest || 'latest'}`}>
          ⬆ Update{info.latest ? ` ${info.latest}` : ''}
        </button>
      ) : info.can_self_update ? (
        <button
          className="version-check-btn"
          onClick={() => load(true)}
          disabled={checking}
          title="Check for updates"
        >
          {checking ? '…' : 'up to date'}
        </button>
      ) : (
        <span className="version-check-btn version-check-btn--disabled" title={info.reason || ''}>
          {info.reason ? 'self-update off' : 'up to date'}
        </span>
      )}

      {error && <span className="version-error" title={error}>update failed</span>}
    </div>
  )
}

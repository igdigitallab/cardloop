import { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../api'
import type { VersionInfo } from '../types'

/**
 * Sidebar-footer version badge (spec-047 workstream A; daily-recheck spec-062).
 * Shows the running version; when a newer version exists on origin it turns into
 * an Update affordance. Clicking Update (with confirm) POSTs /api/update — the
 * detached updater applies + restarts via restart-self.sh, and the existing
 * SSE reconnect-on-wake brings the cockpit back on the new version.
 *
 * Discovery is pull-based and deliberately quiet: a forced network re-check runs
 * at most ONCE PER DAY (on mount and when the tab regains focus, gated by a
 * localStorage timestamp) so a busy dev day where master moves many times does
 * not nag the operator. The manual "up to date" pill always forces a fresh
 * check, ungated. When an un-acknowledged update first appears, a small accent
 * dot pulses on the Update button — noticeable without a toast.
 */

const DAY_MS = 24 * 60 * 60 * 1000
const LS_LAST_CHECK = 'cardloop:version:lastAutoCheck'  // wall-clock of last forced check
const LS_SEEN_TARGET = 'cardloop:version:seenTarget'     // update target the dot was last shown for

/** Stable signature of an available update, so the dot pulses once per target. */
function targetSig(info: VersionInfo): string {
  return info.latest || `behind:${info.behind}`
}

export function VersionBadge() {
  const [info, setInfo] = useState<VersionInfo | null>(null)
  const [checking, setChecking] = useState(false)
  const [updating, setUpdating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isNew, setIsNew] = useState(false)
  const startVerRef = useRef<string>('')   // version we were on when Update was clicked

  const load = useCallback(async (check?: boolean) => {
    try {
      if (check) setChecking(true)
      const v = await api.version(check)
      setInfo(v)
      if (v.update_status?.state === 'failed') setError(v.update_status.detail)
      // Pulse the dot only for an update we haven't acknowledged yet; persist
      // the seen target so a plain page reload doesn't re-pulse the same one.
      if (v.update_available) {
        setIsNew(localStorage.getItem(LS_SEEN_TARGET) !== targetSig(v))
      } else {
        setIsNew(false)
      }
    } catch { /* offline — keep last known */ }
    finally { if (check) setChecking(false) }
  }, [])

  // Force a fresh network check at most once per day; otherwise just read the
  // cheap local-git state. Stamps the timestamp before the request so two near
  // simultaneous triggers (mount + focus) can't double-fetch.
  const maybeDailyCheck = useCallback(() => {
    const last = Number(localStorage.getItem(LS_LAST_CHECK) || 0)
    if (Date.now() - last >= DAY_MS) {
      localStorage.setItem(LS_LAST_CHECK, String(Date.now()))
      load(true)
    } else {
      load(false)
    }
  }, [load])

  useEffect(() => {
    maybeDailyCheck()
    const onVis = () => { if (document.visibilityState === 'visible') maybeDailyCheck() }
    document.addEventListener('visibilitychange', onVis)
    const t = window.setInterval(maybeDailyCheck, DAY_MS)
    return () => { document.removeEventListener('visibilitychange', onVis); window.clearInterval(t) }
  }, [maybeDailyCheck])

  // While an update applies, poll for the outcome — the detached updater either
  // restarts the service onto a new build (version advances → hard reload) or
  // records a 'failed' status (surface it, stop the spinner). Without this the
  // "Updating…" spinner has no way to learn it finished and hangs forever.
  useEffect(() => {
    if (!updating) return
    const startedAt = Date.now()
    const startVer = startVerRef.current
    const TIMEOUT_MS = 4 * 60_000
    let timer = 0
    let stopped = false
    const poll = async () => {
      try {
        const v = await api.version()
        // A failure stamped at/after we started (5s slack) — not a stale one.
        if (v.update_status?.state === 'failed' && v.update_status.ts * 1000 >= startedAt - 5000) {
          setError(v.update_status.detail || 'update failed')
          setUpdating(false)
          return
        }
        // Version moved → the new build is live; reload onto fresh assets.
        if (v.current && startVer && v.current !== startVer) {
          window.location.reload()
          return
        }
      } catch { /* service is likely mid-restart — keep waiting */ }
      if (stopped) return
      if (Date.now() - startedAt >= TIMEOUT_MS) {
        setError('update is taking longer than expected — reload the page')
        setUpdating(false)
        return
      }
      timer = window.setTimeout(poll, 3000)
    }
    timer = window.setTimeout(poll, 3000)
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [updating])

  async function onUpdate() {
    if (!info?.update_available) return
    const target = info.latest && info.latest !== info.current ? info.latest : `${info.behind} new commit(s)`
    if (!window.confirm(`Update Cardloop to ${target}?\n\nThe cockpit will restart and reconnect automatically.`)) return
    // She's acting on this update — stop pulsing and remember we surfaced it.
    localStorage.setItem(LS_SEEN_TARGET, targetSig(info))
    setIsNew(false)
    setError(null)
    try {
      const r = await api.update()
      if (r.status === 'updating') {
        startVerRef.current = info.current
        setUpdating(true)   // the polling effect below watches for success/failure
      } else if (r.status === 'up_to_date') {
        load()
      }
    } catch (e) {
      const err = e as { body?: { error?: string }; message?: string }
      setError(err.body?.error || err.message || 'update failed')
    }
  }

  // Manual "Check for updates" — always forces a fresh fetch and resets the
  // daily timer so the next auto-check is measured from this point.
  function onManualCheck() {
    localStorage.setItem(LS_LAST_CHECK, String(Date.now()))
    load(true)
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
        <button
          className={`version-update-btn${isNew ? ' version-update-btn--new' : ''}`}
          onClick={onUpdate}
          title={`Update to ${info.latest || 'latest'}`}
        >
          {isNew && <span className="version-new-dot" aria-hidden="true" />}
          ⬆ Update{info.latest ? ` ${info.latest}` : ''}
        </button>
      ) : info.can_self_update ? (
        <button
          className="version-check-btn"
          onClick={onManualCheck}
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

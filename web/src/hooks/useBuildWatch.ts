import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Self-update onto a newly deployed frontend build.
 *
 * The cockpit is a long-lived PWA: the app on a phone stays open for hours and keeps
 * executing the JS it booted with, so a deploy is invisible to it. Reloading is not enough
 * either — workbox answers navigations from its precache, and the new service worker only
 * takes over once it has installed, so the FIRST reload after a deploy still hands back the
 * old index.html. That is the "reload twice" ritual; this hook automates it.
 *
 * Cost of not having it, measured: an operator spent an hour concluding a just-shipped
 * feature was broken while testing the previous day's bundle.
 */

/** How often to ask the server which bundle it serves (plus every visibility return). */
const POLL_MS = 5 * 60_000
/** Re-check this often whether it became safe to reload, once an update is pending. */
const IDLE_RECHECK_MS = 10_000
/** Give the new service worker this long to install + claim before reloading anyway. */
const SW_CLAIM_TIMEOUT_MS = 8_000

/** Filename of the bundle THIS page booted from, e.g. "index-DYlyWjZb.js". */
function bootedBundle(): string | null {
  const el = document.querySelector('script[type="module"][src*="assets/index-"]')
  const m = (el?.getAttribute('src') || '').match(/(index-[A-Za-z0-9_-]+\.js)/)
  return m ? m[1] : null
}

export interface BuildWatch {
  /** A newer build is served, but reloading right now would interrupt the operator. */
  updateReady: boolean
  /** Reload onto the new build (waits for the new service worker to claim the page). */
  applyUpdate: () => void
}

/**
 * @param canReloadNow returns false while a reload would cost the operator something —
 *   a streaming turn they are watching, or a draft in the composer. When it returns false
 *   the update is held and `updateReady` is surfaced instead; the hook keeps checking and
 *   applies it by itself as soon as the app goes idle.
 */
export function useBuildWatch(canReloadNow: () => boolean): BuildWatch {
  const [updateReady, setUpdateReady] = useState(false)
  const bootedRef = useRef<string | null>(null)
  const reloadingRef = useRef(false)
  const canReloadRef = useRef(canReloadNow)
  canReloadRef.current = canReloadNow
  if (bootedRef.current === null) bootedRef.current = bootedBundle()

  const applyUpdate = useCallback(() => {
    if (reloadingRef.current) return
    reloadingRef.current = true
    void (async () => {
      try {
        const reg = await navigator.serviceWorker?.getRegistration()
        if (reg) {
          // Wait for the new worker to claim this page (sw.ts calls skipWaiting +
          // clients.claim), otherwise the reload is served the precached old shell.
          const claimed = new Promise<void>(resolve => {
            const onChange = () => {
              navigator.serviceWorker.removeEventListener('controllerchange', onChange)
              resolve()
            }
            navigator.serviceWorker.addEventListener('controllerchange', onChange)
          })
          await reg.update().catch(() => { /* offline / 404 — reload anyway */ })
          await Promise.race([
            claimed,
            new Promise<void>(r => setTimeout(r, SW_CLAIM_TIMEOUT_MS)),
          ])
        }
      } catch { /* no service worker (plain http, private tab) — a plain reload suffices */ }
      window.location.reload()
    })()
  }, [])

  useEffect(() => {
    let cancelled = false

    const check = async () => {
      if (cancelled || reloadingRef.current) return
      if (document.visibilityState !== 'visible') return
      const booted = bootedRef.current
      if (!booted) return  // dev server / no hashed bundle — nothing to compare against
      try {
        const res = await fetch('/api/build', { credentials: 'include', cache: 'no-store' })
        if (!res.ok) return
        const data = await res.json() as { bundle?: string | null }
        if (cancelled || !data.bundle || data.bundle === booted) return
        if (canReloadRef.current()) applyUpdate()
        else setUpdateReady(true)
      } catch { /* offline or mid-restart — the next tick retries */ }
    }

    void check()
    const poll = setInterval(() => void check(), POLL_MS)
    const onVis = () => { if (document.visibilityState === 'visible') void check() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      cancelled = true
      clearInterval(poll)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [applyUpdate])

  // Held update: apply it the moment the app goes idle, so the operator never has to act.
  useEffect(() => {
    if (!updateReady) return
    const id = setInterval(() => {
      if (canReloadRef.current()) applyUpdate()
    }, IDLE_RECHECK_MS)
    return () => clearInterval(id)
  }, [updateReady, applyUpdate])

  return { updateReady, applyUpdate }
}

/**
 * useNotifications — browser Notification API + Web Push integration.
 *
 * Exposes:
 *   permission        — mirrors Notification.permission ('default'|'granted'|'denied'|'unsupported')
 *   enabled           — user opt-in, persisted in localStorage['cops.notify'] (default false)
 *   setEnabled        — toggle opt-in (does NOT request permission; caller must requestPermission first)
 *   requestPermission — calls Notification.requestPermission(), updates state
 *   notifyRunEnd      — fire a LOCAL notification for a completed agent run (app open)
 *
 * Web Push (spec-053 Phase B):
 *   When enabled && permission==='granted', subscribes to push via the service worker and
 *   registers the PushSubscription with the server (POST /api/push/subscribe).
 *   This enables background push notifications even when the app is fully closed.
 *
 * Design notes:
 *   - Opt-in by default (enabled=false) to avoid surprise prompts on first visit.
 *   - Guards against browsers without Notification API (older Safari, some PWA shells).
 *   - Push subscription is best-effort: failures are logged, never thrown to callers.
 *
 * References:
 *   https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API/Using_the_Notifications_API
 *   https://developer.mozilla.org/en-US/docs/Web/API/Push_API
 */
import { useState, useCallback, useEffect, useRef } from 'react'
import { t } from '../i18n'

const LS_KEY = 'cops.notify'

// Notification.permission type is 'default' | 'granted' | 'denied'.
// We add 'unsupported' for environments where the API is absent.
export type NotifyPermission = 'default' | 'granted' | 'denied' | 'unsupported'

export interface NotifyRunEndParams {
  projectId: string
  projectName: string
  outcome: string      // 'ok' | 'fail'
  onClick: () => void
}

export interface UseNotificationsResult {
  permission: NotifyPermission
  enabled: boolean
  setEnabled: (v: boolean) => void
  requestPermission: () => Promise<void>
  notifyRunEnd: (params: NotifyRunEndParams) => void
}

function isSupported(): boolean {
  return typeof Notification !== 'undefined'
}

function isPushSupported(): boolean {
  return (
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    isSupported()
  )
}

function readEnabled(): boolean {
  try {
    return localStorage.getItem(LS_KEY) === 'true'
  } catch {
    return false
  }
}

/**
 * Convert a URL-safe base64 string (no padding) to a Uint8Array.
 * Required to pass the VAPID public key as applicationServerKey to pushManager.subscribe.
 * Reference: https://developer.mozilla.org/en-US/docs/Web/API/PushManager/subscribe
 */
function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(b64)
  const buf = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) {
    buf[i] = raw.charCodeAt(i)
  }
  return buf
}

/** Subscribe to Web Push and register the subscription with the server. Best-effort. */
async function registerPushSubscription(): Promise<void> {
  if (!isPushSupported()) return
  try {
    const reg = await navigator.serviceWorker.ready
    if (!reg.pushManager) return

    // Fetch the VAPID public key from the server.
    const resp = await fetch('/api/push/vapid-public')
    if (!resp.ok) return
    const { key } = await resp.json() as { key?: string }
    if (!key) return

    // Subscribe (browser may reuse an existing subscription).
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    })

    // Send the PushSubscription to the server for storage.
    await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    })
  } catch (err) {
    // Non-fatal: push is a best-effort enhancement.
    console.warn('[push] subscribe failed:', err)
  }
}

export function useNotifications(): UseNotificationsResult {
  const [permission, setPermission] = useState<NotifyPermission>(() => {
    if (!isSupported()) return 'unsupported'
    return Notification.permission as NotifyPermission
  })

  const [enabled, setEnabledState] = useState<boolean>(() => readEnabled())

  // Track whether we have already attempted push subscription in this session
  // to avoid re-subscribing on every render when state changes.
  const pushAttemptedRef = useRef(false)

  const setEnabled = useCallback((v: boolean) => {
    try { localStorage.setItem(LS_KEY, String(v)) } catch { /* ignore */ }
    setEnabledState(v)
    // Reset so a re-enable triggers a fresh subscription attempt.
    if (!v) pushAttemptedRef.current = false
  }, [])

  const requestPermission = useCallback(async () => {
    if (!isSupported()) return
    const result = await Notification.requestPermission()
    setPermission(result as NotifyPermission)
  }, [])

  // Web Push subscription effect: fires when the user has opted in AND permission is granted.
  // Registers (or re-uses) the browser PushSubscription and sends it to the server.
  useEffect(() => {
    if (!enabled) return
    if (permission !== 'granted') return
    if (pushAttemptedRef.current) return
    pushAttemptedRef.current = true
    registerPushSubscription()
  }, [enabled, permission])

  const notifyRunEnd = useCallback(({ projectId, projectName, outcome, onClick }: NotifyRunEndParams) => {
    // Guard: must be opted in, permission granted, and API available.
    if (!enabled) return
    if (!isSupported()) return
    if (Notification.permission !== 'granted') return

    const isOk = outcome === 'ok'
    // Build title from i18n template — replace {projectName} placeholder.
    const titleTemplate = isOk ? t['notify.title_ok'] : t['notify.title_fail']
    const title = titleTemplate.replace('{projectName}', projectName)
    const body = isOk ? t['notify.body_ok'] : t['notify.body_fail']

    const n = new Notification(title, {
      body,
      // tag deduplicates: a second run_end for the same project replaces the previous notification.
      tag: projectId,
      icon: '/icons/icon-192.png',
      data: { projectId },
      // renotify: show the notification even if the tag already exists (new run = fresh alert).
      // Valid Web Notifications API option but absent from the DOM lib types — cast.
      renotify: true,
    } as NotificationOptions)

    n.onclick = () => {
      window.focus()
      onClick()
      n.close()
    }
  }, [enabled])

  return { permission, enabled, setEnabled, requestPermission, notifyRunEnd }
}

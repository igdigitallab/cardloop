/**
 * useNotifications — local browser Notification API integration.
 *
 * Exposes:
 *   permission        — mirrors Notification.permission ('default'|'granted'|'denied'|'unsupported')
 *   enabled           — user opt-in, persisted in localStorage['cops.notify'] (default false)
 *   setEnabled        — toggle opt-in (does NOT request permission; caller must requestPermission first)
 *   requestPermission — calls Notification.requestPermission(), updates state
 *   notifyRunEnd      — fire a notification for a completed agent run (respects enabled+permission guards)
 *
 * Design notes:
 *   - Opt-in by default (enabled=false) to avoid surprise prompts on first visit.
 *   - Guards against browsers without Notification API (older Safari, some PWA shells).
 *   - No direct App.tsx import; caller injects projectName + onClick closure.
 *
 * Reference: https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API/Using_the_Notifications_API
 */
import { useState, useCallback } from 'react'
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

function readEnabled(): boolean {
  try {
    return localStorage.getItem(LS_KEY) === 'true'
  } catch {
    return false
  }
}

export function useNotifications(): UseNotificationsResult {
  const [permission, setPermission] = useState<NotifyPermission>(() => {
    if (!isSupported()) return 'unsupported'
    return Notification.permission as NotifyPermission
  })

  const [enabled, setEnabledState] = useState<boolean>(() => readEnabled())

  const setEnabled = useCallback((v: boolean) => {
    try { localStorage.setItem(LS_KEY, String(v)) } catch { /* ignore */ }
    setEnabledState(v)
  }, [])

  const requestPermission = useCallback(async () => {
    if (!isSupported()) return
    const result = await Notification.requestPermission()
    setPermission(result as NotifyPermission)
  }, [])

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

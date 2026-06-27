/// <reference lib="webworker" />
// Custom Cardloop service worker — compiled by vite-plugin-pwa (injectManifest strategy).
// References: https://vite-pwa-org.netlify.app/guide/inject-manifest.html
//             https://developer.mozilla.org/en-US/docs/Web/API/Push_API
//             https://developer.mozilla.org/en-US/docs/Web/API/ServiceWorkerGlobalScope

import { precacheAndRoute, cleanupOutdatedCaches } from 'workbox-precaching'

declare const self: ServiceWorkerGlobalScope & { __WB_MANIFEST: Array<{ url: string; revision: string | null }> }

// Precache all build artifacts injected by workbox-build at build time.
// self.__WB_MANIFEST is replaced with the actual manifest entries by the plugin.
cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST || [])

// ── Lifecycle ────────────────────────────────────────────────────────────────

self.addEventListener('install', () => {
  // Skip waiting so the new SW activates immediately without waiting for old
  // tabs to close.
  self.skipWaiting()
})

self.addEventListener('activate', (event: ExtendableEvent) => {
  // Claim all open clients so the new SW controls them without a page reload.
  event.waitUntil(self.clients.claim())
})

// ── Web Push ─────────────────────────────────────────────────────────────────
//
// The server agent (spec-053 Phase B) will:
//   1. Expose POST /api/push/subscribe to save PushSubscription objects.
//   2. Send Web Push messages with JSON payload: { title, body, icon, tag, data }.
//
// `data.url`       — URL to open when the user clicks the notification.
// `data.projectId` — project to navigate to (sent via postMessage to open tabs).

interface PushPayload {
  title?: string
  body?: string
  icon?: string
  tag?: string
  data?: {
    url?: string
    projectId?: string
    [key: string]: unknown
  }
}

self.addEventListener('push', (event: PushEvent) => {
  let payload: PushPayload = {}

  if (event.data) {
    try {
      payload = event.data.json() as PushPayload
    } catch {
      // Fallback: treat the raw text as the notification body.
      payload = { body: event.data.text() }
    }
  }

  const title = payload.title || 'Cardloop'
  // Cast to `object` to include `renotify` and `badge` which are valid Web Push
  // notification options but not yet reflected in every DOM lib version.
  const options = {
    body: payload.body,
    icon: payload.icon || '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    tag: payload.tag ?? payload.data?.projectId,
    data: payload.data,
    // Show the notification even if one with the same tag is already displayed.
    renotify: Boolean(payload.tag ?? payload.data?.projectId),
  } as NotificationOptions

  // spec-053 Phase B dedup: if the app is open in any window, the in-page local
  // notification (useNotifications.notifyRunEnd) already handles the alert.
  // Suppress the SW push notification to avoid a double notification.
  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      if (clients.length > 0) {
        // At least one window is open — local notification handles it; skip SW notification.
        return
      }
      await self.registration.showNotification(title, options)
    })()
  )
})

self.addEventListener('notificationclick', (event: NotificationEvent) => {
  event.notification.close()

  const notificationData = event.notification.data as PushPayload['data'] | undefined
  const targetUrl = notificationData?.url || '/'
  const projectId = notificationData?.projectId

  event.waitUntil(
    (async () => {
      // Try to focus an already-open Cardloop tab rather than opening a new window.
      const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      for (const client of allClients) {
        if ('focus' in client) {
          await (client as WindowClient).focus()
          // Ask the tab to navigate to the relevant project.
          if (projectId) {
            client.postMessage({ type: 'notification-navigate', projectId })
          }
          return
        }
      }
      // No open tab — open a new window at the target URL.
      await self.clients.openWindow(targetUrl)
    })(),
  )
})

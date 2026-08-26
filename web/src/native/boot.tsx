import { Capacitor } from '@capacitor/core'
import { createRoot } from 'react-dom/client'
import { ServerSetup } from './ServerSetup'
import { NativeGate } from './NativeGate'
import { STORAGE_KEY, SETUP_QUERY_PARAM, APP_BUNDLE_HOST, AUTH_HANDOFF_PARAM } from './keys'

function go(url: string, token: string) {
  localStorage.setItem(STORAGE_KEY, url)
  // The passphrase is deliberately NOT persisted: the server sets an auth cookie on
  // first login and that cookie is what keeps the app signed in, so storing the
  // secret on the device would buy nothing and lose it to anyone holding the phone.
  window.location.replace(
    token ? `${url}#${AUTH_HANDOFF_PARAM}=${encodeURIComponent(token)}` : url
  )
}

/** Native (Capacitor) server gate. The app ships with no server baked in — it's
 *  self-hosted, so each install points at its own instance.
 *
 *  Returns true if the caller should mount <App/> normally right now.
 *  Returns false if this function took over rendering instead — a reachability probe
 *  is running or the picker is on screen. */
export function bootNative(rootEl: HTMLElement): boolean {
  if (!Capacitor.isNativePlatform()) return true

  // ⚠️ Only the app bundle's own origin hosts the picker. On a real server's origin
  // we are already where we wanted to be, so mount the app and stop.
  //
  // This check replaced an origin comparison against the SAVED url, which looked
  // equivalent and was not: localStorage is per-origin, so after the redirect the
  // saved value is NOT readable on the server's origin. "No saved value" then read as
  // "never configured" and the picker appeared a SECOND time, immediately after the
  // first one had just succeeded — and submitting it called location.replace() with a
  // URL differing only in its fragment, which does not navigate at all, so the button
  // sat on "Connecting..." forever. Observed on a real device, not theorised.
  if (window.location.hostname !== APP_BUNDLE_HOST) return true

  const params = new URLSearchParams(window.location.search)
  const forced = params.has(SETUP_QUERY_PARAM)
  if (forced) {
    // Drop the flag so a later reload of the bundle does not reopen the picker.
    params.delete(SETUP_QUERY_PARAM)
    const rest = params.toString()
    history.replaceState(null, '', `${window.location.pathname}${rest ? `?${rest}` : ''}`)
  }

  const saved = localStorage.getItem(STORAGE_KEY)
  createRoot(rootEl).render(
    saved && !forced
      ? <NativeGate saved={saved} onConnect={go} />
      : <ServerSetup initialUrl={saved ?? ''} onConnect={go} />
  )
  return false
}

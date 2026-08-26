import { Capacitor } from '@capacitor/core'
import { createRoot } from 'react-dom/client'
import { ServerSetup } from './ServerSetup'
import { NativeGate } from './NativeGate'
import { STORAGE_KEY, APP_ORIGIN_KEY, FORCE_SETUP_KEY, AUTH_HANDOFF_PARAM } from './keys'

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
 *  Returns false if this function took over rendering instead — a redirect is in
 *  flight, a reachability probe is running, or the picker is on screen. */
export function bootNative(rootEl: HTMLElement): boolean {
  if (!Capacitor.isNativePlatform()) return true

  const saved = localStorage.getItem(STORAGE_KEY)
  const forced = localStorage.getItem(FORCE_SETUP_KEY) === '1'

  if (saved && !forced) {
    // Both Capacitor's bridge (isNativePlatform() stays true) and this
    // localStorage key survive the redirect — confirmed live via CDP: on the
    // real server's own origin, window.Capacitor is still present and
    // STORAGE_KEY still reads back the URL we just navigated to. Without this
    // origin check, every load on the real server would see `saved` set and
    // call location.replace() on itself again — a same-URL reload loop that
    // never lets React mount (readyState reaches "complete" but #root stays
    // empty, observed live).
    try {
      if (new URL(saved).origin === window.location.origin) return true
    } catch {
      /* a corrupt saved value falls through to the picker below */
    }
  }

  // We are on the app bundle's own origin — record it so the server-side
  // "Change server" control knows where to send the WebView back to.
  localStorage.setItem(APP_ORIGIN_KEY, window.location.origin)
  localStorage.removeItem(FORCE_SETUP_KEY)

  createRoot(rootEl).render(
    saved
      ? <NativeGate saved={saved} forced={forced} onConnect={go} />
      : <ServerSetup onConnect={go} />
  )
  return false
}

import { Capacitor } from '@capacitor/core'
import { createRoot } from 'react-dom/client'
import { ServerSetup } from './ServerSetup'

const STORAGE_KEY = 'cops.native.serverUrl'

/** Native (Capacitor) first-run gate. The app ships with no server baked in —
 *  it's self-hosted, so each install points at its own instance, chosen once
 *  here via the server-setup screen and remembered in localStorage.
 *
 *  Returns true if the caller should mount <App/> normally right now.
 *  Returns false if this function took over rendering instead — either a
 *  same-WebView redirect to the saved server is in flight (after which the
 *  page reloads fresh from that server, same as the browser PWA), or the
 *  server-setup form is on screen waiting for input. Either way the caller
 *  must not mount anything else on top. */
export function bootNative(rootEl: HTMLElement): boolean {
  if (!Capacitor.isNativePlatform()) return true

  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved) {
    // Both Capacitor's bridge (isNativePlatform() stays true) and this
    // localStorage key survive the redirect — confirmed live via CDP: on the
    // real server's own origin, window.Capacitor is still present and
    // STORAGE_KEY still reads back the URL we just navigated to. Without this
    // origin check, every load on the real server would see `saved` set and
    // call location.replace() on itself again — a same-URL reload loop that
    // never lets React mount (readyState reaches "complete" but #root stays
    // empty, observed live).
    if (new URL(saved).origin === window.location.origin) return true
    window.location.replace(saved)
    return false
  }

  createRoot(rootEl).render(
    <ServerSetup
      onConnect={url => {
        localStorage.setItem(STORAGE_KEY, url)
        window.location.replace(url)
      }}
    />
  )
  return false
}

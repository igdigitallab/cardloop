import { Capacitor } from '@capacitor/core'
import { createRoot } from 'react-dom/client'
import { ServerSetup } from './ServerSetup'

const STORAGE_KEY = 'cops.native.serverUrl'

/** Native (Capacitor) first-run gate. The app ships with no server baked in —
 *  it's self-hosted, so each install points at its own instance, chosen once
 *  here and remembered via localStorage (scoped to the bundled app's own
 *  origin, separate from the real instance's origin).
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

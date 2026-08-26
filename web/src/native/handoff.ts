import { Capacitor } from '@capacitor/core'
import { AUTH_HANDOFF_PARAM, APP_BUNDLE_HOST, SETUP_QUERY_PARAM } from './keys'

/** Read (and immediately erase) a passphrase handed over from the native server
 *  picker, then sign in with it so the app lands logged in instead of showing the
 *  login form on every fresh install.
 *
 *  The handover rides in the URL **fragment**, not a query string: a fragment is
 *  never sent in the HTTP request, so the passphrase does not end up in the
 *  server's access log, in Cloudflare's, or in any proxy in between. It is stripped
 *  from the address before anything else runs, so a later share/screenshot of the
 *  URL cannot leak it either.
 *
 *  A failure here is deliberately silent: the login screen is the fallback and it
 *  already reports a wrong passphrase properly. */
export async function consumeAuthHandoff(): Promise<void> {
  const hash = window.location.hash
  if (!hash || !hash.includes(`${AUTH_HANDOFF_PARAM}=`)) return

  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const token = params.get(AUTH_HANDOFF_PARAM) || ''

  // Erase first, sign in second — if the request hangs or the app is killed
  // mid-flight, the secret must not be left sitting in the address bar.
  params.delete(AUTH_HANDOFF_PARAM)
  const rest = params.toString()
  history.replaceState(null, '', `${window.location.pathname}${window.location.search}${rest ? `#${rest}` : ''}`)

  if (!token) return
  try {
    await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: token }),
    })
  } catch {
    /* offline / rejected — the login screen takes over */
  }
}

/** Send the native WebView back to the app bundle with the server picker open.
 *
 *  The "open the picker" request travels as a QUERY FLAG, not a localStorage key:
 *  storage is per-origin, so a flag written here on the server's origin would be
 *  invisible to the bundle that has to act on it. */
export function requestServerChange(): void {
  window.location.replace(`https://${APP_BUNDLE_HOST}/?${SETUP_QUERY_PARAM}=1`)
}

/** True when this page is the native app currently showing a real server — i.e. when
 *  offering "Change server" makes sense at all. In a plain browser (or already on the
 *  bundle) there is nothing to change. */
export function canChangeServer(): boolean {
  return Capacitor.isNativePlatform() && window.location.hostname !== APP_BUNDLE_HOST
}

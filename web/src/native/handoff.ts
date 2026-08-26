import { AUTH_HANDOFF_PARAM, APP_ORIGIN_KEY, FORCE_SETUP_KEY, STORAGE_KEY } from './keys'

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
 *  Returns false when there is nothing to go back to (plain browser, or an install
 *  that never recorded its own origin), so the caller can hide the control. */
export function requestServerChange(): boolean {
  const appOrigin = localStorage.getItem(APP_ORIGIN_KEY)
  if (!appOrigin) return false
  localStorage.setItem(FORCE_SETUP_KEY, '1')
  window.location.replace(appOrigin)
  return true
}

/** True when this page is a native install that has an app origin to return to —
 *  i.e. when offering "Change server" makes sense at all. */
export function canChangeServer(): boolean {
  return Boolean(localStorage.getItem(APP_ORIGIN_KEY) && localStorage.getItem(STORAGE_KEY))
}

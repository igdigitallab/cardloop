import { useState, FormEvent } from 'react'
import { t } from '../i18n'
import { normalizeUrl } from './url'

const PROBE_TIMEOUT_MS = 8000

interface Props {
  /** Pre-filled when re-pointing an already-configured install (or recovering
   *  from a server that stopped answering) — empty on a genuine first run. */
  initialUrl?: string
  /** Shown above the form when we got here because the saved server is dead,
   *  rather than because the operator asked to change it. */
  initialError?: string
  onConnect: (url: string, token: string) => void
}

/** Server screen for the native (Capacitor) app: pick the self-hosted Cardloop
 *  instance, confirm it's reachable, then hand it to the caller which persists it
 *  and navigates the WebView there.
 *
 *  Used for BOTH the first run and every later change of server — an install that
 *  can only ever be pointed once is an install you have to delete and reinstall to
 *  move, which is exactly what happens when the instance moves to a new host. */
export function ServerSetup({ initialUrl = '', initialError = '', onConnect }: Props) {
  const [url, setUrl] = useState(initialUrl)
  const [token, setToken] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(initialError)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const normalized = normalizeUrl(url)
    if (!normalized) {
      setError(t['native.setup_error_invalid_url'])
      return
    }
    setLoading(true)
    setError('')
    try {
      // /api/health is unauthenticated on purpose, so this probe answers "is this a
      // Cardloop instance I can reach" without needing the passphrase first.
      // Bounded: an address that accepts the connection and then never answers would
      // otherwise leave this button on "Connecting..." with no way back.
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS)
      let res: Response
      try {
        res = await fetch(`${normalized}api/health`, { method: 'GET', signal: ctrl.signal })
      } finally {
        clearTimeout(timer)
      }
      if (!res.ok) throw new Error('unreachable')
      onConnect(normalized, token.trim())
    } catch {
      setError(t['native.setup_error_unreachable'])
      setLoading(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <div className="login-logo-icon">⚡</div>
          <span className="login-logo-text">Cardloop</span>
        </div>
        <h2>{initialUrl ? t['native.setup_title_change'] : t['native.setup_title']}</h2>
        <p className="login-sub">{t['native.setup_subtitle']}</p>

        {error && <div className="error-msg">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="server-url">{t['native.setup_url_label']}</label>
            <input
              id="server-url"
              type="url"
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder={t['native.setup_url_placeholder']}
              autoFocus
              autoCapitalize="none"
              autoCorrect="off"
              inputMode="url"
            />
          </div>

          <div className="form-group">
            <label htmlFor="server-token">{t['native.setup_token_label']}</label>
            <input
              id="server-token"
              type="password"
              value={token}
              onChange={e => setToken(e.target.value)}
              placeholder={t['native.setup_token_placeholder']}
              autoCapitalize="none"
              autoCorrect="off"
              autoComplete="current-password"
            />
            <p className="login-sub" style={{ marginTop: 6, fontSize: 12 }}>
              {t['native.setup_token_hint']}
            </p>
          </div>

          <button className="btn btn-primary" type="submit" disabled={loading || !url.trim()}>
            {loading ? t['native.setup_connecting'] : t['native.setup_connect']}
          </button>
        </form>
      </div>
    </div>
  )
}

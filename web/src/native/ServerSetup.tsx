import { useState, FormEvent } from 'react'
import { t } from '../i18n'

interface Props {
  onConnect: (url: string) => void
}

function normalizeUrl(raw: string): string | null {
  let value = raw.trim()
  if (!value) return null
  if (!/^https?:\/\//i.test(value)) value = `https://${value}`
  try {
    const u = new URL(value)
    u.hash = ''
    u.search = ''
    u.pathname = '/'
    return u.toString()
  } catch {
    return null
  }
}

/** First-run screen for the native (Capacitor) app: ask for the self-hosted
 *  Cardloop instance URL, confirm it's reachable, then hand it to the caller
 *  which persists it and navigates the WebView there. */
export function ServerSetup({ onConnect }: Props) {
  const [url, setUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

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
      const res = await fetch(`${normalized}api/health`, { method: 'GET' })
      if (!res.ok) throw new Error('unreachable')
      onConnect(normalized)
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
        <h2>{t['native.setup_title']}</h2>
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

          <button className="btn btn-primary" type="submit" disabled={loading || !url.trim()}>
            {loading ? t['native.setup_connecting'] : t['native.setup_connect']}
          </button>
        </form>
      </div>
    </div>
  )
}

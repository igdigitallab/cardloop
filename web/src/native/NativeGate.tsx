import { useEffect, useState } from 'react'
import { ServerSetup } from './ServerSetup'
import { t } from '../i18n'

const HEALTH_TIMEOUT_MS = 6000

/** Is the saved server actually answering? A saved-but-dead server is the whole
 *  reason this check exists: without it the WebView redirects into a void and the
 *  app has no UI left to fix itself with — the only way out being "clear app data",
 *  which is not something an operator should have to know. */
async function serverIsAlive(url: string): Promise<boolean> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS)
  try {
    // /api/health is unauthenticated on purpose, so this answers "can I reach a
    // Cardloop here" without needing a session first.
    const res = await fetch(`${url}api/health`, { method: 'GET', signal: ctrl.signal })
    return res.ok
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

interface Props {
  saved: string
  /** The operator asked for the picker — skip the probe and show it straight away. */
  forced: boolean
  onConnect: (url: string, token: string) => void
}

/** Decides between "redirect to the saved server" and "show the picker", doing the
 *  reachability probe asynchronously so a dead server degrades into a usable screen
 *  instead of a blank WebView. */
export function NativeGate({ saved, forced, onConnect }: Props) {
  const [state, setState] = useState<'checking' | 'setup'>(forced ? 'setup' : 'checking')
  const [error, setError] = useState('')

  useEffect(() => {
    if (forced) return
    let cancelled = false
    void (async () => {
      const alive = await serverIsAlive(saved)
      if (cancelled) return
      if (alive) {
        window.location.replace(saved)
        return
      }
      setError(t['native.setup_error_saved_unreachable'])
      setState('setup')
    })()
    return () => {
      cancelled = true
    }
  }, [saved, forced])

  if (state === 'checking') {
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="login-logo">
            <div className="login-logo-icon">⚡</div>
            <span className="login-logo-text">Cardloop</span>
          </div>
          <p className="login-sub">{t['native.setup_connecting']}</p>
        </div>
      </div>
    )
  }

  return <ServerSetup initialUrl={saved} initialError={error} onConnect={onConnect} />
}

import { useState, FormEvent } from 'react'
import { api } from '../api'
import { t } from '../i18n'

interface Props {
  onLogin: () => void
}

export function LoginScreen({ onLogin }: Props) {
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!password.trim()) return
    setLoading(true)
    setError('')
    try {
      await api.login(password)
      onLogin()
    } catch {
      setError(t['login.error_wrong_password'])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <div className="login-logo-icon">⚡</div>
          <span className="login-logo-text">Claude-Ops</span>
        </div>
        <h2>{t['login.title']}</h2>
        <p className="login-sub">{t['login.subtitle']}</p>

        {error && <div className="error-msg">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="password">{t['login.password_label']}</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="••••••••••••"
              autoFocus
              autoComplete="current-password"
            />
          </div>
          <button className="btn btn-primary" type="submit" disabled={loading || !password.trim()}>
            {loading ? t['login.submit_loading'] : t['login.submit']}
          </button>
        </form>
      </div>
    </div>
  )
}

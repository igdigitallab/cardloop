import { useState, FormEvent } from 'react'
import { api } from '../api'

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
      setError('Неверная парольная фраза')
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
        <h2>Вход</h2>
        <p className="login-sub">Панель управления проектами</p>

        {error && <div className="error-msg">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="password">Парольная фраза</label>
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
            {loading ? 'Проверяю...' : 'Войти'}
          </button>
        </form>
      </div>
    </div>
  )
}

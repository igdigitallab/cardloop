import { useState, FormEvent, useRef, useEffect } from 'react'
import { api } from '../api'
import { t } from '../i18n'

interface Props {
  onLogin: () => void
}

export function LoginScreen({ onLogin }: Props) {
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // 2FA two-step state
  const [totpMode, setTotpMode] = useState(false)
  const [totpCode, setTotpCode] = useState('')
  const totpRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (totpMode && totpRef.current) {
      totpRef.current.focus()
    }
  }, [totpMode])

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!password.trim()) return
    if (totpMode && !totpCode.trim()) return

    setLoading(true)
    setError('')
    try {
      await api.login(password, totpMode ? totpCode.trim() : undefined)
      onLogin()
    } catch (err: unknown) {
      const apiErr = err as { status?: number; body?: { error?: string } }
      if (apiErr.status === 401 && apiErr.body?.error === 'totp_required') {
        // Switch into 2FA step — keep password in state
        setTotpMode(true)
        setTotpCode('')
      } else if (apiErr.status === 401 && apiErr.body?.error === 'totp_invalid') {
        // Stay in 2FA step, show inline error
        setError(t['login.error_totp_invalid'])
        setTotpCode('')
      } else {
        setError(t['login.error_wrong_password'])
      }
    } finally {
      setLoading(false)
    }
  }

  function handleBackToPassword() {
    setTotpMode(false)
    setTotpCode('')
    setError('')
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <div className="login-logo-icon">⚡</div>
          <span className="login-logo-text">Cardloop</span>
        </div>
        <h2>{t['login.title']}</h2>
        <p className="login-sub">{totpMode ? t['login.subtitle_2fa'] : t['login.subtitle']}</p>

        {error && <div className="error-msg">{error}</div>}

        <form onSubmit={handleSubmit}>
          {!totpMode ? (
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
          ) : (
            <div className="form-group">
              <label htmlFor="totp-code">{t['login.totp_label']}</label>
              <input
                id="totp-code"
                ref={totpRef}
                type="text"
                inputMode="numeric"
                value={totpCode}
                onChange={e => setTotpCode(e.target.value.replace(/\D/g, '').slice(0, 8))}
                placeholder="000000"
                autoComplete="one-time-code"
                maxLength={8}
              />
              <p className="login-hint">{t['login.totp_recovery_hint']}</p>
            </div>
          )}

          <button className="btn btn-primary" type="submit" disabled={loading || (!totpMode && !password.trim()) || (totpMode && !totpCode.trim())}>
            {loading ? t['login.submit_loading'] : t['login.submit']}
          </button>

          {totpMode && (
            <button
              type="button"
              className="btn btn-secondary"
              style={{ marginTop: 8, width: '100%' }}
              onClick={handleBackToPassword}
              disabled={loading}
            >
              {t['login.back_to_password']}
            </button>
          )}
        </form>
      </div>
    </div>
  )
}

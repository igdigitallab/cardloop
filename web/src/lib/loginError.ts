import type { TKey } from '../i18n'

/**
 * Which message a failed sign-in shows.
 *
 * Every failure used to read "Wrong passphrase" — including a request that never reached the
 * server. The service worker serves the cached app shell offline, so a machine that dropped off
 * the VPN (an expired Tailscale key, 2026-11-21) still shows the login screen, and the operator
 * was told a correct passphrase was wrong. Only a plain 401 means the passphrase was wrong.
 * `totp_required` is not here: it switches the form into its 2FA step instead of showing text.
 */
export function loginErrorKey(err: unknown): TKey {
  const e = err as { status?: unknown; body?: { error?: unknown } } | null | undefined
  const status = typeof e?.status === 'number' ? e.status : undefined
  const code = e?.body?.error
  if (status === undefined) return 'login.error_unreachable'
  if (status === 401) return code === 'totp_invalid' ? 'login.error_totp_invalid' : 'login.error_wrong_password'
  if (status === 429) return 'login.error_rate_limited'
  if (status === 503 && code === '2fa_state_unreadable') return 'login.error_2fa_unreadable'
  return 'login.error_server'
}

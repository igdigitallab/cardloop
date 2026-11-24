// Login failure -> message key. Run: cd web && npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { loginErrorKey } from './loginError'

const http = (status: number, error?: string) =>
  Object.assign(new Error('x'), { status, body: error ? { error } : null })

test('a fetch that never reached the server is "unreachable", not a wrong passphrase', () => {
  // fetch() rejects with a TypeError and no status: offline, VPN down, TLS refused.
  assert.equal(loginErrorKey(new TypeError('Failed to fetch')), 'login.error_unreachable')
  assert.equal(loginErrorKey(undefined), 'login.error_unreachable')
})

test('only a plain 401 means the passphrase was wrong', () => {
  assert.equal(loginErrorKey(http(401, 'bad password')), 'login.error_wrong_password')
  assert.equal(loginErrorKey(http(401, 'totp_invalid')), 'login.error_totp_invalid')
})

test('throttling and server failures name themselves', () => {
  assert.equal(loginErrorKey(http(429, 'too many attempts, try later')), 'login.error_rate_limited')
  assert.equal(loginErrorKey(http(503, '2fa_state_unreadable')), 'login.error_2fa_unreadable')
  assert.equal(loginErrorKey(http(502)), 'login.error_server')
  assert.equal(loginErrorKey(http(500)), 'login.error_server')
  assert.equal(loginErrorKey(http(400, 'bad request')), 'login.error_server')
})

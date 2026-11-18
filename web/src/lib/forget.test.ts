// spec-098 — Forget view-model helpers + the capability helper. Run: cd web && npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  FORGET_CHANGED_MESSAGE, alsoDeletePayload, deletedChatSession, diaryDay, diaryLine, formatBytes,
  formatSpan, forgetErrorView, lineageKey, residueMessage, summaryLine, ForgetPlan,
} from './forget'
import { CapabilityRow, canDeleteSession, canForgetTurn } from './providers'

const at = (y: number, mo: number, d: number, h: number, mi: number) => new Date(y, mo - 1, d, h, mi).getTime() / 1000

test('diary days: ISO -> DD.MM, list line, empty -> null', () => {
  assert.equal(diaryDay('2026-11-17'), '17.11')
  assert.equal(diaryDay('weird'), 'weird')
  assert.equal(diaryLine(['2026-11-17', '2026-11-18']), 'Already in the diary: 17.11, 18.11')
  assert.equal(diaryLine([]), null)
  assert.equal(diaryLine(undefined), null)
})

test('time span: same day prints the date once, other day twice, instant once, unknown empty', () => {
  assert.equal(formatSpan(at(2026, 11, 17, 14, 3), at(2026, 11, 17, 15, 10)), '17.11 14:03 - 15:10')
  assert.equal(formatSpan(at(2026, 11, 17, 23, 50), at(2026, 11, 18, 0, 20)), '17.11 23:50 - 18.11 00:20')
  assert.equal(formatSpan(at(2026, 11, 17, 14, 3), at(2026, 11, 17, 14, 3)), '17.11 14:03')
  assert.equal(formatSpan(null, at(2026, 11, 17, 14, 3)), '17.11 14:03')
  assert.equal(formatSpan(null, null), '')
})

test('bytes and summary line', () => {
  assert.equal(formatBytes(0), '0 B')
  assert.equal(formatBytes(900), '900 B')
  assert.equal(formatBytes(834221), '815 KB')
  assert.equal(formatBytes(5 * 1024 * 1024), '5.0 MB')
  const p = { turns: 1, first_ts: null, last_ts: null, files: 12, bytes: 2048 } as ForgetPlan
  assert.equal(summaryLine(p), '1 turn · 12 files, 2 KB')
  assert.equal(summaryLine({ ...p, turns: 7, files: 0 }), '7 turns')
})

test('lineage ticks -> also_delete payload (plan order, stale keys ignored)', () => {
  const lineage = [
    { provider: 'claude', session_id: 'a', kind: 'rotate' },
    { provider: 'codex', session_id: 'b', kind: 'runtime_handoff' },
  ]
  assert.equal(lineageKey(lineage[1]), 'codex:b')
  assert.deepEqual(alsoDeletePayload(lineage, new Set()), [])
  assert.deepEqual(
    alsoDeletePayload(lineage, new Set(['codex:b', 'claude:a', 'grok:zzz'])),
    [{ provider: 'claude', session_id: 'a' }, { provider: 'codex', session_id: 'b' }],
  )
})

test('error mapping: changed -> reopen + replan; refusals show server text; fallbacks by code', () => {
  assert.deepEqual(forgetErrorView(409, 'changed', 'x'), { message: FORGET_CHANGED_MESSAGE, replan: true })
  for (const code of ['busy', 'subagents', 'writer', 'provider_in_use']) {
    assert.deepEqual(forgetErrorView(409, code, 'Sub-agents are still running'), { message: 'Sub-agents are still running', replan: false })
  }
  assert.equal(forgetErrorView(404, 'disabled', undefined).message, 'Deleting sessions is switched off on this server')
  assert.equal(forgetErrorView(404, 'not_found', '').message, 'This session no longer exists')
  assert.equal(forgetErrorView(500, 'failed', 'boom').message, 'boom')
  assert.equal(forgetErrorView(502, undefined, undefined).message, 'Delete failed (HTTP 502)')
  assert.equal(forgetErrorView(undefined, undefined, undefined).message, 'Delete failed')
})

test('residue toast: clean, hits capped at 3, truncated marker', () => {
  assert.equal(residueMessage(true, []), 'Deleted — no copies left')
  const h = ['a', 'b', 'c', 'd'].map(path => ({ path }))
  assert.equal(residueMessage(false, h), 'Deleted — still found in: a, b, c, …')
  assert.equal(residueMessage(false, h.slice(0, 2)), 'Deleted — still found in: a, b')
  assert.equal(residueMessage(false, h.slice(0, 1), true), 'Deleted — still found in: a, …')
})

test('session_deleted: matches by chat id or by any continuity id', () => {
  assert.equal(deletedChatSession({ session_id: 's', chat_ids: ['c1'] }, 'c1', []), true)
  assert.equal(deletedChatSession({ session_id: 's', chat_ids: ['c2'] }, 'c1', ['x', 's']), true)
  assert.equal(deletedChatSession({ session_id: 's', chat_ids: [] }, 'c1', ['x', null]), false)
  assert.equal(deletedChatSession({ chat_ids: ['c1'] }, null, []), false)
})

test('capability helper: registry flag decides; missing row/flag = no; no provider = claude', () => {
  const rows: CapabilityRow[] = [
    { provider: 'claude', capabilities: { delete_session: true, forget_turn: true } },
    { provider: 'codex', capabilities: { delete_session: true, forget_turn: false } },
    { provider: 'grok', capabilities: {} },
  ]
  assert.equal(canDeleteSession(rows), true)
  assert.equal(canDeleteSession(rows, 'codex'), true)
  assert.equal(canDeleteSession(rows, 'grok'), false)
  assert.equal(canForgetTurn(rows, 'codex'), false)
  assert.equal(canForgetTurn(rows, undefined), true)
  assert.equal(canDeleteSession([], 'claude'), false)
  assert.equal(canDeleteSession(null, 'claude'), false)
})

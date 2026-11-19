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
  // not clean with nothing found = the check did not run to the end: never shown as clean
  assert.equal(residueMessage(false, [], true), 'Deleted — the check for leftover copies could not finish')
  assert.equal(residueMessage(false, []), 'Deleted — the check for leftover copies could not finish')
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

// ── spec-098 P4: Forget from here ─────────────────────────────────────────────
import {
  chatForgottenAffectsMe, composerAfterForget, forgetAnchorFor, forgottenToast, ForgetFeedRow,
} from './forget'

const U = (uuid: string | undefined, ts: number | undefined, extra: Partial<ForgetFeedRow> = {}): ForgetFeedRow =>
  ({ role: 'user', uuid, ts, ...extra })
const A = (extra: Partial<ForgetFeedRow> = {}): ForgetFeedRow => ({ role: 'assistant', ...extra })
const BRK: ForgetFeedRow = { role: 'session_break' }
const MARK = 1_000_000

test('anchor: the user row before the assistant group, in the current session', () => {
  const feed = [U('u1', MARK + 1000), A(), U('u2', MARK + 5000), A(), A()]
  assert.equal(forgetAnchorFor(feed, 1, MARK), 'u1')
  assert.equal(forgetAnchorFor(feed, 3, MARK), 'u2')
})

test('anchor: null forget_from_ms or undefined means never', () => {
  const feed = [U('u1', MARK), A()]
  assert.equal(forgetAnchorFor(feed, 1, null), null)
  assert.equal(forgetAnchorFor(feed, 1, undefined), null)
})

test('anchor: user row older than the first mark (beyond 5 s slack) is refused, within slack is ok', () => {
  assert.equal(forgetAnchorFor([U('u1', MARK - 5000), A()], 1, MARK), 'u1')
  assert.equal(forgetAnchorFor([U('u1', MARK - 5001), A()], 1, MARK), null)
})

test('anchor: missing uuid or ts on the user row', () => {
  assert.equal(forgetAnchorFor([U(undefined, MARK), A()], 1, MARK), null)
  assert.equal(forgetAnchorFor([U('u1', undefined), A()], 1, MARK), null)
})

test('anchor: groups before the last session_break are not offered, groups after are', () => {
  const feed = [U('old', MARK), A(), BRK, U('new', MARK + 10), A()]
  assert.equal(forgetAnchorFor(feed, 1, MARK), null)
  assert.equal(forgetAnchorFor(feed, 4, MARK), 'new')
})

test('anchor: an assistant group right after a break with no user row has no anchor', () => {
  assert.equal(forgetAnchorFor([U('old', MARK), BRK, A()], 2, MARK), null)
})

test('anchor: streaming rows and non-assistant indexes', () => {
  assert.equal(forgetAnchorFor([U('u1', MARK), A({ streaming: true })], 1, MARK), null)
  assert.equal(forgetAnchorFor([U('u1', MARK), A(), A({ streaming: true })], 1, MARK), null)
  assert.equal(forgetAnchorFor([U('u1', MARK), A()], 0, MARK), null)
  assert.equal(forgetAnchorFor([U('u1', MARK), A()], 7, MARK), null)
})

test('anchor: board rows between user and assistant are skipped', () => {
  const feed = [U('u1', MARK), { role: 'board' }, A()]
  assert.equal(forgetAnchorFor(feed, 2, MARK), 'u1')
})

test('composer after forget: empty draft, non-empty draft, no prompt', () => {
  assert.equal(composerAfterForget('p', ''), 'p')
  assert.equal(composerAfterForget('p', '  \n'), 'p')
  assert.equal(composerAfterForget('p', 'draft'), 'p\n\ndraft')
  assert.equal(composerAfterForget('', 'draft'), 'draft')
})

test('forgotten toast and chat_forgotten matching', () => {
  assert.equal(forgottenToast(1, false), 'Forgot 1 turn')
  assert.equal(forgottenToast(3, false), 'Forgot 3 turns')
  assert.equal(forgottenToast(3, true), 'Forgot the whole session')
  assert.equal(chatForgottenAffectsMe({ chat_id: 'c1' }, 'c1', 's'), true)
  assert.equal(chatForgottenAffectsMe({ chat_id: 'c2', session_id: 's' }, 'c1', 's'), true)
  assert.equal(chatForgottenAffectsMe({ chat_id: 'c2', session_id: 'x' }, 'c1', 's'), false)
  assert.equal(chatForgottenAffectsMe({}, null, null), false)
})

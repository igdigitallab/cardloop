// Scroll-back paging bookkeeping (lib/historyPaging.ts).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  breakReason, breakRow, continuesAfterReset, nextOlder, olderAfterHydrate, sinceFor,
  type PrevOf,
} from './historyPaging'
import type { ChatMessage, PrevSession } from '../types'

const m = (idx: number, sid = 'S'): ChatMessage =>
  ({ id: `hist-${sid}-${idx}`, role: 'user', text: `q${idx}`, tools: [], streaming: false, idx, sid })
const range = (a: number, b: number, sid = 'S') => Array.from({ length: b - a }, (_, i) => m(a + i, sid))
const prev = (session_id: string, extra: Partial<PrevSession> = {}): PrevSession => ({
  session_id, trigger: 'auto', at: 1_790_000_000, context_tokens: 752_000, carried: true,
  handoff: 'summary', label: null, available: true, ...extra,
})
const none: PrevOf = () => undefined
const links = (map: Record<string, PrevSession | null>): PrevOf => sid => map[sid ?? '']

test('the next page is fetched above the loaded pages, or above the window when none', () => {
  assert.deepEqual(nextOlder([], 'S', 200, none), { kind: 'page', sid: 'S', before: 200 })
  assert.deepEqual(nextOlder(range(100, 200), 'S', 200, none), { kind: 'page', sid: 'S', before: 100 })
})

test('at a first message with nothing before it, or with unknown positions, there is nothing more', () => {
  assert.equal(nextOlder(range(0, 100), 'S', 100, links({ S: null })), null)
  assert.equal(nextOlder(range(0, 100), 'S', 100, none), null)
  assert.equal(nextOlder([], 'S', 0, links({ S: null })), null)
  assert.equal(nextOlder([], 'S', null, links({ S: prev('P') })), null)
})

test('at a first message the feed crosses into the session before it', () => {
  const p = prev('P')
  assert.deepEqual(nextOlder(range(0, 100), 'S', 100, links({ S: p })), { kind: 'prev', from: 'S', prev: p })
  // The window itself starts at the first message (a short session), or the chat was just reset.
  assert.deepEqual(nextOlder([], 'S', 0, links({ S: p })), { kind: 'prev', from: 'S', prev: p })
  assert.deepEqual(nextOlder([], null, 0, links({ '': p })), { kind: 'prev', from: null, prev: p })
})

test('the previous session pages back from its own positions, then crosses again', () => {
  const p = prev('P'), q = prev('Q')
  const older = [...range(40, 140, 'P'), breakRow('S', p), ...range(0, 50)]
  assert.deepEqual(nextOlder(older, 'S', 50, links({ S: p })), { kind: 'page', sid: 'P', before: 40 })
  const atStart = [...range(0, 140, 'P'), breakRow('S', p), ...range(0, 50)]
  assert.deepEqual(nextOlder(atStart, 'S', 50, links({ S: p, P: q })), { kind: 'prev', from: 'P', prev: q })
  assert.equal(nextOlder(atStart, 'S', 50, links({ S: p, P: null })), null)
})

test('an empty previous session is crossed, an unreadable one ends the feed', () => {
  const q = prev('Q')
  assert.deepEqual(nextOlder([breakRow('S', prev('P'))], 'S', 0, links({ P: q })),
    { kind: 'prev', from: 'P', prev: q })
  assert.equal(nextOlder([breakRow('S', prev('P', { available: false }))], 'S', 0, links({ P: q })), null)
})

test('a link back into a session already shown does not loop', () => {
  const older = [...range(0, 10, 'P'), breakRow('S', prev('P')), ...range(0, 5)]
  assert.equal(nextOlder(older, 'S', 5, links({ P: prev('S') })), null)
})

test('a hydrate asks for the window right after the loaded pages of its own session', () => {
  assert.equal(sinceFor([]), undefined)
  assert.equal(sinceFor(range(100, 200)), 200)
  assert.equal(sinceFor([...range(0, 10, 'P'), breakRow('S', prev('P'))]), 0)
  assert.equal(sinceFor([...range(0, 10, 'P'), breakRow('S', prev('P')), ...range(0, 30)]), 30)
})

test('pages stay when the window continues right after them', () => {
  const older = range(100, 200)
  assert.equal(olderAfterHydrate(older, 200, true), older)
  const crossed = [...range(0, 10, 'P'), breakRow('S', prev('P'))]
  assert.equal(olderAfterHydrate(crossed, 0, true), crossed)
})

test('pages go when the session changed, positions are gone, or a gap opened', () => {
  const older = range(100, 200)
  assert.deepEqual(olderAfterHydrate(older, 200, false), [])
  assert.deepEqual(olderAfterHydrate(older, null, true), [])
  assert.deepEqual(olderAfterHydrate(older, 260, true), [])
  // The window no longer starts at the first message: the earlier session is not adjacent any more.
  assert.deepEqual(olderAfterHydrate([...range(0, 10, 'P'), breakRow('S', prev('P'))], 40, true), [])
})

test('an overlapping window keeps only the pages above it, and the sessions above those', () => {
  const kept = olderAfterHydrate(range(100, 200), 150, true)
  assert.deepEqual(kept.map(x => x.idx), range(100, 150).map(x => x.idx))
  const head = [...range(0, 10, 'P'), breakRow('S', prev('P'))]
  const k2 = olderAfterHydrate([...head, ...range(0, 100)], 60, true)
  assert.deepEqual(k2.map(x => x.id), [...head, ...range(0, 60)].map(x => x.id))
  assert.deepEqual(olderAfterHydrate([...head, ...range(0, 100)], 0, true).map(x => x.id), head.map(x => x.id))
})

test('the first turn after a reset keeps the session shown above it', () => {
  const older = [...range(0, 10, 'P'), breakRow(null, prev('P'))]
  assert.equal(continuesAfterReset(older, null, prev('P')), true)
  assert.equal(continuesAfterReset(older, null, prev('OTHER')), false)
  assert.equal(continuesAfterReset(older, 'S', prev('P')), false)
  assert.equal(continuesAfterReset(older, null, null), false)
  assert.equal(continuesAfterReset(range(0, 10), null, prev('P')), false)
})

test('a break row says what reset the context', () => {
  assert.equal(breakReason(prev('P')), 'Auto-rotation at 752K tokens')
  assert.equal(breakReason(prev('P', { trigger: 'manual' })), 'Session reset by hand')
  assert.equal(breakReason(prev('P', { trigger: 'new' })), 'New session started from the session menu')
  assert.equal(breakReason(prev('P', { trigger: null })), 'Context reset')
  const row = breakRow('S', prev('P'))
  assert.equal(row.role, 'session_break')
  assert.equal(row.ts, 1_790_000_000_000)
  assert.equal(row.sessionBreak?.next, 'S')
})

// Scroll-back paging bookkeeping (lib/historyPaging.ts).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { olderAfterHydrate, olderCursor, sinceFor } from './historyPaging'
import type { ChatMessage } from '../types'

const m = (idx: number): ChatMessage => ({ id: `hist-${idx}`, role: 'user', text: `q${idx}`, tools: [], streaming: false, idx })
const range = (a: number, b: number) => Array.from({ length: b - a }, (_, i) => m(a + i))

test('the next page is fetched above the loaded pages, or above the window when none', () => {
  assert.equal(olderCursor([], 200), 200)
  assert.equal(olderCursor(range(100, 200), 200), 100)
})

test('nothing more to fetch once the first message is loaded or positions are unknown', () => {
  assert.equal(olderCursor(range(0, 100), 100), null)
  assert.equal(olderCursor([], 0), null)
  assert.equal(olderCursor([], null), null)
})

test('a hydrate asks for the window right after the loaded pages', () => {
  assert.equal(sinceFor([]), undefined)
  assert.equal(sinceFor(range(100, 200)), 200)
})

test('pages stay when the window continues right after them', () => {
  const older = range(100, 200)
  assert.equal(olderAfterHydrate(older, 200, true), older)
})

test('pages go when the session changed, positions are gone, or a gap opened', () => {
  const older = range(100, 200)
  assert.deepEqual(olderAfterHydrate(older, 200, false), [])
  assert.deepEqual(olderAfterHydrate(older, null, true), [])
  assert.deepEqual(olderAfterHydrate(older, 260, true), [])
})

test('an overlapping window keeps only the pages above it', () => {
  const kept = olderAfterHydrate(range(100, 200), 150, true)
  assert.deepEqual(kept.map(x => x.idx), range(100, 150).map(x => x.idx))
})

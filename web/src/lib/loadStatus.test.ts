// spec-094 — the load meter's pure logic (web/src/lib/loadStatus.ts): the one job that must not lie
// is telling "overloaded" from "not answering" from "signed out" from "feature off".
//
// Same harness as runtimeStatus.test.ts: Node's built-in runner, bundled with esbuild first because
// the module imports React and the api client.
//
//   cd web
//   npx esbuild src/lib/loadStatus.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/load-status-test/loadStatus.test.cjs --log-level=warning
//   node --test /tmp/load-status-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  INITIAL_LOAD, reduceLoad, viewOf, litSegments, verdict, DOWN_AFTER, OFF_AFTER, STALE_CLIENT_MS, STALE_SERVER_S,
  type LoadState,
} from './loadStatus'
import type { SystemLoad } from '../api'

const T0 = 1_790_000_000_000

function load(over: Partial<SystemLoad> = {}): SystemLoad {
  return {
    level: 'ok', score: 12, at: 1, age_s: 2, chats: { live: 3, max: 8 }, signals: [], top: [], host: {}, ...over,
  }
}
const ok = (d: SystemLoad, at = T0): LoadState => reduceLoad(INITIAL_LOAD, { type: 'ok', data: d, at })

test('a fresh reading shows its own level', () => {
  for (const level of ['ok', 'warn', 'crit'] as const) {
    assert.equal(viewOf(ok(load({ level })), T0 + 1000).kind, level)
  }
})

test('before the first answer: loading, never green', () => {
  assert.equal(viewOf(INITIAL_LOAD, T0).kind, 'loading')
  assert.equal(viewOf(ok(load({ warming_up: true, level: 'unknown' })), T0).kind, 'loading')
})

test('one 404 is a proxy hiccup, a run of them means the feature is off', () => {
  let s = ok(load())
  s = reduceLoad(s, { type: 'error', status: 404 })
  assert.notEqual(viewOf(s, T0 + 1000).kind, 'off')
  for (let i = 1; i < OFF_AFTER; i++) s = reduceLoad(s, { type: 'error', status: 404 })
  assert.equal(viewOf(s, T0 + 1000).kind, 'off')
  // ...and it comes back by itself when the endpoint answers again (no page reload)
  s = reduceLoad(s, { type: 'ok', data: load({ level: 'warn' }), at: T0 + 2000 })
  assert.equal(viewOf(s, T0 + 2500).kind, 'warn')
})

test('a 404 in the middle of a healthy run does not accumulate', () => {
  let s = ok(load())
  s = reduceLoad(s, { type: 'error', status: 404 })
  s = reduceLoad(s, { type: 'ok', data: load(), at: T0 + 10_000 })
  s = reduceLoad(s, { type: 'error', status: 404 })
  assert.notEqual(viewOf(s, T0 + 10_500).kind, 'off')
})

test('401 is "signed out", not "cockpit down", and does not count as a failure', () => {
  let s = ok(load())
  s = reduceLoad(s, { type: 'error', status: 401 })
  s = reduceLoad(s, { type: 'error', status: 401 })
  s = reduceLoad(s, { type: 'error', status: 401 })
  assert.equal(s.failures, 0)
  assert.equal(viewOf(s, T0 + 1000).kind, 'signedout')
})

test('timeouts and 5xx: one miss keeps the last reading, DOWN_AFTER misses say "not responding"', () => {
  let s = ok(load({ level: 'ok' }))
  s = reduceLoad(s, { type: 'error', status: null })            // an aborted (timed-out) fetch
  assert.equal(viewOf(s, T0 + 12_000).kind, 'ok')
  for (let i = 1; i < DOWN_AFTER; i++) s = reduceLoad(s, { type: 'error', status: 502 })
  const v = viewOf(s, T0 + 25_000)
  assert.equal(v.kind, 'down')
  assert.ok(v.data, 'the last reading stays available for the detail panel')
})

test('a green reading never outlives the server: old data goes stale, then down', () => {
  const s = ok(load({ level: 'ok' }))
  assert.equal(viewOf(s, T0 + STALE_CLIENT_MS - 1).kind, 'ok')
  assert.equal(viewOf(s, T0 + STALE_CLIENT_MS + 1).kind, 'stale')
})

test('the server reporting an old sampler is stale even when the poll itself succeeded', () => {
  assert.equal(viewOf(ok(load({ age_s: STALE_SERVER_S + 5 })), T0 + 1000).kind, 'stale')
})

test('recovery: one good answer clears down', () => {
  let s = ok(load())
  for (let i = 0; i < DOWN_AFTER; i++) s = reduceLoad(s, { type: 'error', status: null })
  assert.equal(viewOf(s, T0 + 30_000).kind, 'down')
  s = reduceLoad(s, { type: 'ok', data: load({ level: 'warn' }), at: T0 + 31_000 })
  assert.equal(viewOf(s, T0 + 31_500).kind, 'warn')
})

test('waking after a long pause is stale until the next poll lands — never the old green', () => {
  const s = ok(load(), T0)
  assert.equal(viewOf(s, T0 + 10 * 60_000).kind, 'stale')
  const fresh = reduceLoad(s, { type: 'ok', data: load(), at: T0 + 10 * 60_000 + 300 })
  assert.equal(viewOf(fresh, T0 + 10 * 60_000 + 500).kind, 'ok')
})

test('a failing server-side sampler is reported, not shown as green or "measuring"', () => {
  const v = viewOf(ok(load({ level: 'unknown', error: 'RuntimeError: boom', signals: [] })), T0 + 1)
  assert.equal(v.kind, 'unknown')
  assert.match(verdict(v), /failing/i)
})

test('nothing measurable on this host is "unknown", not green', () => {
  assert.equal(viewOf(ok(load({ level: 'unknown', score: 0 })), T0 + 1).kind, 'unknown')
})

test('LED segments: the lit count follows pressure and the zones match the verdict', () => {
  assert.equal(litSegments('ok', 3), 1)          // always at least one lit when there is a reading
  assert.equal(litSegments('ok', 49), 2)         // below the warn line: green only
  assert.equal(litSegments('warn', 50), 3)       // the warn line lights the first amber segment
  assert.equal(litSegments('warn', 80), 4)
  assert.equal(litSegments('crit', 100), 5)      // crit lights the red one
  assert.equal(litSegments('down', 100), 0)      // an unreachable server shows no reading at all
  assert.equal(litSegments('loading', 0), 0)
  assert.equal(litSegments('ok', 500), 5)        // clamped
})

test('verdict wording covers every state', () => {
  const kinds = ['off', 'loading', 'signedout', 'down', 'stale', 'unknown', 'ok', 'warn', 'crit'] as const
  for (const kind of kinds) assert.ok(verdict({ kind, data: null }).length > 0)
  assert.match(verdict({ kind: 'down', data: null }), /not responding/i)
})

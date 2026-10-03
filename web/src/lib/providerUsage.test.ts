// spec-095 — one shape for adapter usage (web/src/lib/providerUsage.ts).
//   cd web
//   npx esbuild src/lib/providerUsage.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/provider-usage-test/providerUsage.test.cjs --log-level=warning
//   node --test /tmp/provider-usage-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeProviderUsage } from './providerUsage'

test('codex shape: array by_model, cached_input / reasoning_output', () => {
  const u = normalizeProviderUsage({
    turns: 4, input: 100, output: 40, cached_input: 70, reasoning_output: 9, cost: null,
    subscription_cost_available: false,
    by_model: [
      { model: 'a', turns: 1, input: 10, output: 4, cached_input: 7, reasoning_output: 1 },
      { model: 'b', turns: 3, input: 90, output: 36, cached_input: 63, reasoning_output: 8 },
    ],
  })
  assert.deepEqual(u, {
    turns: 4, input: 100, output: 40, cached: 70, reasoning: 9, notionalUsd: null,
    byModel: [
      { model: 'b', turns: 3, input: 90, output: 36 },
      { model: 'a', turns: 1, input: 10, output: 4 },
    ],
  })
})

test('grok shape: by_model is a RECORD, fields are cached / reasoning, notional is API-equivalent', () => {
  const u = normalizeProviderUsage({
    turns: 3, input: 30, output: 12, cached: 20, reasoning: 5, limits: null, notional_usd: 1.25,
    by_model: { 'grok-build': { turns: 2, input: 20, output: 8 }, 'grok-fast': { turns: 1, input: 10, output: 4 } },
  })
  assert.equal(u?.cached, 20)
  assert.equal(u?.reasoning, 5)
  assert.equal(u?.notionalUsd, 1.25)
  assert.deepEqual(u?.byModel.map(r => [r.model, r.turns]), [['grok-build', 2], ['grok-fast', 1]])
})

test('a zero is a zero: the Codex-named field is only a fallback, never an override', () => {
  const u = normalizeProviderUsage({ turns: 1, cached: 0, cached_input: 99, reasoning: 0, reasoning_output: 77 })
  assert.equal(u?.cached, 0)
  assert.equal(u?.reasoning, 0)
})

test('missing / hostile numbers are 0 and a bad notional is null - never NaN on screen', () => {
  const u = normalizeProviderUsage({ turns: 'many', input: NaN, output: Infinity, cached: null, by_model: { m: { turns: '2' } }, notional_usd: 'free' })
  assert.deepEqual(u, {
    turns: 0, input: 0, output: 0, cached: 0, reasoning: 0, notionalUsd: null,
    byModel: [{ model: 'm', turns: 0, input: 0, output: 0 }],
  })
  assert.equal(normalizeProviderUsage({ notional_usd: Infinity })?.notionalUsd, null)
  assert.equal(normalizeProviderUsage({ notional_usd: 0 })?.notionalUsd, 0)
})

test('not an object = no data (no card is drawn); an object with no models = an empty table', () => {
  for (const bad of [null, undefined, 'x', 5, [], true]) assert.equal(normalizeProviderUsage(bad), null, String(bad))
  assert.deepEqual(normalizeProviderUsage({})?.byModel, [])
  assert.deepEqual(normalizeProviderUsage({ by_model: 'nope' })?.byModel, [])
  assert.deepEqual(normalizeProviderUsage({ by_model: [{ turns: 1 }] })?.byModel.map(r => r.model), ['unknown'])
})

test('model rows: most turns first, ties by name', () => {
  const u = normalizeProviderUsage({ by_model: { z: { turns: 1 }, a: { turns: 1 }, m: { turns: 5 } } })
  assert.deepEqual(u?.byModel.map(r => r.model), ['m', 'a', 'z'])
})

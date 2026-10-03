// spec-095 — the provider table (web/src/lib/providers.ts) and its helpers.
//
// Same harness as runtimeStatus.test.ts: Node's built-in runner over an esbuild bundle.
//   cd web
//   npx esbuild src/lib/providers.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/providers-test/providers.test.cjs --log-level=warning
//   node --test /tmp/providers-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  PROVIDERS, PROVIDER_IDS, boardModelProviders, continuityField, continuityId, continuityQuery,
  gatedProviders, hitThread, isAdapterProvider, isKnownProvider, normalizeProvider,
  projectModelField, providerLabel, providerReportsLimits, providerShort, providerSubscription,
  providerTag, providerUnavailableReason, selectableProviders, usageSectionVisible,
} from './providers'

test('table: Claude first and the only native harness; every other column is unique', () => {
  assert.deepEqual(PROVIDER_IDS, ['claude', 'codex', 'grok'])
  assert.deepEqual(PROVIDER_IDS.filter(id => !PROVIDERS[id].adapter), ['claude'])
  for (const col of ['label', 'continuityField', 'modelField'] as const) {
    const vals = PROVIDER_IDS.map(id => PROVIDERS[id][col])
    assert.equal(new Set(vals).size, vals.length, `${col} must be unique per provider`)
  }
  const tags = PROVIDER_IDS.map(id => PROVIDERS[id].tag).filter(Boolean)
  assert.equal(new Set(tags).size, tags.length, 'a fixed tag may belong to one provider only')
  assert.deepEqual([providerTag('claude'), providerTag('codex'), providerTag('grok')], [null, 'C', 'G'])
})

test('known: own properties only - Object.prototype names are not providers', () => {
  for (const id of PROVIDER_IDS) assert.equal(isKnownProvider(id), true)
  for (const bad of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf', 'Grok', 'CLAUDE', '', ' grok', null, undefined, 42, {}]) {
    assert.equal(isKnownProvider(bad), false, String(bad))
    assert.equal(isAdapterProvider(bad), false, `adapter ${String(bad)}`)
    assert.equal(providerTag(bad), null, `tag ${String(bad)}`)
    assert.equal(continuityField(bad), null, `field ${String(bad)}`)
    assert.equal(projectModelField(bad), null, `model field ${String(bad)}`)
  }
})

test('normalize: permissive for labels - an unknown name becomes the default, a known one stays', () => {
  assert.equal(normalizeProvider('grok'), 'grok')
  assert.equal(normalizeProvider('toString'), 'claude')
  assert.equal(normalizeProvider(undefined), 'claude')
})

test('labels: an unknown provider shows its own id - never passed off as Claude Code', () => {
  assert.equal(providerLabel('claude'), 'Claude Code')
  assert.equal(providerLabel('codex'), 'Codex')
  assert.equal(providerLabel('grok'), 'Grok')
  assert.equal(providerLabel('gemini'), 'gemini')
  assert.equal(providerLabel('constructor'), 'constructor')
  // absent / empty = a record that predates the provider field = the default provider
  assert.equal(providerLabel(undefined), 'Claude Code')
  assert.equal(providerLabel(''), 'Claude Code')
  assert.equal(providerShort('claude'), 'Claude')
  assert.equal(providerShort('gemini'), 'gemini')
  assert.equal(providerShort(null), 'Claude')
})

test('adapter + limits flags: Grok is an adapter that reports NO limits; unknown is muted', () => {
  assert.equal(isAdapterProvider('claude'), false)
  assert.equal(isAdapterProvider('codex'), true)
  assert.equal(isAdapterProvider('grok'), true)
  assert.equal(providerReportsLimits('claude'), true)
  assert.equal(providerReportsLimits('codex'), true)
  assert.equal(providerReportsLimits('grok'), false)
  assert.equal(providerReportsLimits('gemini'), false)
  assert.equal(providerSubscription('grok'), 'SuperGrok')
  assert.equal(providerSubscription('gemini'), 'subscription')
})

test('continuity: each provider reads ONLY its own resume field', () => {
  const chat = { provider: 'grok', session_id: 'CLAUDE-1', codex_thread_id: 'CODEX-1', grok_session_id: 'GROK-1' }
  assert.equal(continuityId(chat), 'GROK-1')
  assert.equal(continuityId(chat, 'codex'), 'CODEX-1')
  assert.equal(continuityId(chat, 'claude'), 'CLAUDE-1')
  // no id of its own = null, never another provider's id (that would resume a foreign thread)
  assert.equal(continuityId({ provider: 'grok', session_id: 'CLAUDE-1', codex_thread_id: 'CODEX-1' }), null)
  assert.equal(continuityId({ provider: 'codex', session_id: 'CLAUDE-1' }), null)
  // a record with no provider is Claude's
  assert.equal(continuityId({ session_id: 'CLAUDE-1' }), 'CLAUDE-1')
  assert.equal(continuityId({ provider: 'grok', grok_session_id: '' }), null)
  assert.equal(continuityId({ provider: 'gemini', session_id: 'X' }), null)
  assert.equal(continuityId(null), null)
})

test('history query: provider + the provider\'s own field name; Claude and junk send nothing', () => {
  assert.deepEqual(continuityQuery('claude', 'S1'), {})
  assert.deepEqual(continuityQuery('codex', 'T1'), { provider: 'codex', codex_thread_id: 'T1' })
  assert.deepEqual(continuityQuery('grok', 'G1'), { provider: 'grok', grok_session_id: 'G1' })
  assert.deepEqual(continuityQuery('gemini', 'X1'), {})
  assert.deepEqual(continuityQuery('grok', ''), {})
  assert.deepEqual(continuityQuery('grok', null), {})
  assert.deepEqual(continuityQuery(undefined, 'S1'), {})
})

test('search hit: which thread the peek opens, per provider', () => {
  assert.deepEqual(hitThread({ session_id: 'S1', uuid: 'u' }, 'claude'), { sessionId: 'S1' })
  assert.deepEqual(hitThread({ session_id: 'S1' }, undefined), { sessionId: 'S1' })
  assert.deepEqual(hitThread({ codex_thread_id: 'T1' }, 'codex'), { sessionId: 'T1', continuityId: 'T1' })
  assert.deepEqual(hitThread({ grok_session_id: 'G1' }, 'grok'), { sessionId: 'G1', continuityId: 'G1' })
  // a server that files Grok's id under session_id still opens the right thread
  assert.deepEqual(hitThread({ session_id: 'G2' }, 'grok'), { sessionId: 'G2', continuityId: 'G2' })
  // a Claude hit never grows a continuity id from a foreign field
  assert.deepEqual(hitThread({ session_id: 'S1', codex_thread_id: 'T9' }, 'claude'), { sessionId: 'S1' })
  assert.equal(hitThread({}, 'grok'), null)
  assert.equal(hitThread({ codex_thread_id: 'T1' }, 'grok'), null)
})

test('picker: Claude is always offered; an adapter only when the server lists it', () => {
  assert.deepEqual(selectableProviders([]), ['claude'])
  assert.deepEqual(selectableProviders([{ provider: 'claude' }, { provider: 'codex' }]), ['claude', 'codex'])
  assert.deepEqual(selectableProviders([{ provider: 'codex' }, { provider: 'grok' }]), ['claude', 'codex', 'grok'])
  // a record that already holds an unlisted adapter keeps it, so a <select> never renders blank
  assert.deepEqual(selectableProviders([], 'grok'), ['claude', 'grok'])
  assert.deepEqual(selectableProviders([{ provider: 'codex' }], 'grok'), ['claude', 'codex', 'grok'])
  assert.deepEqual(selectableProviders([{ provider: 'gemini' }]), ['claude'])
})

test('picker: an adapter is refused unless the registry says enabled AND available', () => {
  const ok = { provider: 'grok', enabled: true, available: true }
  assert.equal(providerUnavailableReason('grok', ok), '')
  assert.equal(providerUnavailableReason('grok', { ...ok, enabled: false }), 'Grok unavailable')
  assert.equal(providerUnavailableReason('grok', { ...ok, available: false }), 'Grok unavailable')
  assert.equal(providerUnavailableReason('grok', { ...ok, available: false, error: 'login expired' }), 'login expired')
  assert.equal(providerUnavailableReason('codex', undefined), 'Codex unavailable')
  assert.equal(providerUnavailableReason('codex', null), 'Codex unavailable')
  // the cockpit's own harness is never refused here - even with a bad row or none at all
  assert.equal(providerUnavailableReason('claude', undefined), '')
  assert.equal(providerUnavailableReason('claude', { provider: 'claude', enabled: false, available: false }), '')
})

test('usage filter: "all" shows every section, a provider shows only itself', () => {
  assert.equal(usageSectionVisible('all', 'claude'), true)
  assert.equal(usageSectionVisible('all', 'grok'), true)
  assert.equal(usageSectionVisible('grok', 'grok'), true)
  assert.equal(usageSectionVisible('grok', 'claude'), false)
  assert.equal(usageSectionVisible('codex', 'grok'), false)
  assert.equal(usageSectionVisible('claude', 'codex'), false)
})

test('settings: the privacy gate row shows while Grok is listed or still ON; Codex has none', () => {
  assert.deepEqual(gatedProviders([], {}), [])
  assert.deepEqual(gatedProviders([{ provider: 'codex' }], {}), [])
  assert.deepEqual(gatedProviders([{ provider: 'grok' }], {}), ['grok'])
  // server stopped listing Grok but the project still allows it: it must stay switch-off-able
  assert.deepEqual(gatedProviders([], { grok_allowed: true }), ['grok'])
  assert.deepEqual(gatedProviders([], { grok_allowed: false }), [])
  assert.deepEqual(gatedProviders([], { grok_allowed: 'yes' }), [])
  assert.equal(PROVIDERS.grok.gate?.field, 'grok_allowed')
  assert.equal(PROVIDERS.grok.gate?.recipient, 'xAI')
})

test('settings: a board-model row per listed adapter, or one the project already names a model for', () => {
  assert.deepEqual(boardModelProviders([], {}), [])
  assert.deepEqual(boardModelProviders([{ provider: 'codex' }, { provider: 'grok' }], {}), ['codex', 'grok'])
  assert.deepEqual(boardModelProviders([], { grok_model: '', codex_model: 'gpt' }), ['codex'])
  assert.deepEqual(boardModelProviders([{ provider: 'claude' }], {}), [])
})

test('settings: a default grok_model alone is NOT a Grok row (Grok switched off must leave Settings unchanged)', () => {
  // The server serves `grok_model: "grok-4.7"` for EVERY project, listed or not.
  assert.deepEqual(boardModelProviders([], { grok_model: 'grok-4.7', grok_allowed: false }), [])
  assert.deepEqual(boardModelProviders([{ provider: 'claude' }], { grok_model: 'grok-4.7', codex_model: '' }), [])
  // An opted-in project keeps its row (and so its way back) even if the server stopped listing Grok.
  assert.deepEqual(boardModelProviders([], { grok_model: 'grok-4.7', grok_allowed: true }), ['grok'])
  // Strictly the boolean true, like the server's own gate.
  assert.deepEqual(boardModelProviders([], { grok_model: 'grok-4.7', grok_allowed: 'true' }), [])
})

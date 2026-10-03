// A project that never chose a context-pack setting must still be able to save (spec-095 P5b: the
// Grok opt-in rides the same POST). web/src/lib/settingsPayload.ts.
//   cd web
//   npx esbuild src/lib/settingsPayload.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/settingspayload-test/settingsPayload.test.cjs --log-level=warning
//   node --test /tmp/settingspayload-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { projectSettingsPayload } from './settingsPayload'
import type { ProjectSettings } from '../types'

const base = { grok_model: 'grok-4.7', model: null } as unknown as ProjectSettings

test('an untouched null context_pack_enabled is not posted (the server would 400 the whole save)', () => {
  const body = projectSettingsPayload({ ...base, context_pack_enabled: null }, { context_pack_enabled: null })
  assert.equal('context_pack_enabled' in body, false)
  assert.equal(body.grok_model, 'grok-4.7')                 // the rest of the record is untouched
  assert.equal(body.model, null)                        // other nulls are the server's to read
  // no baseline yet (settings never loaded) behaves the same
  assert.equal('context_pack_enabled' in projectSettingsPayload({ ...base, context_pack_enabled: null }, null), false)
  assert.equal('context_pack_enabled' in projectSettingsPayload({ ...base }, null), false)
})

test('an explicit choice is always posted, true or false', () => {
  assert.equal(projectSettingsPayload({ ...base, context_pack_enabled: true }, { context_pack_enabled: null }).context_pack_enabled, true)
  assert.equal(projectSettingsPayload({ ...base, context_pack_enabled: false }, { context_pack_enabled: true }).context_pack_enabled, false)
})

test('going back to Inherit is posted as null so the server can answer, not silently dropped', () => {
  const body = projectSettingsPayload({ ...base, context_pack_enabled: null }, { context_pack_enabled: true })
  assert.equal('context_pack_enabled' in body, true)
  assert.equal(body.context_pack_enabled, null)
})

test('the input record is not mutated', () => {
  const input = { ...base, context_pack_enabled: null } as ProjectSettings
  projectSettingsPayload(input, { context_pack_enabled: null })
  assert.equal('context_pack_enabled' in input, true)
})

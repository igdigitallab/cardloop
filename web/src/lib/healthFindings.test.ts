// Unit tests for the project-health pill's pure logic (web/src/lib/healthFindings.ts).
//
// Same harness as runtimeStatus.test.ts: Node's built-in runner, esbuild (ships with vite) to bundle.
//
//   cd web
//   npx esbuild src/lib/healthFindings.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/health-findings-test/healthFindings.test.cjs --log-level=warning
//   node --test /tmp/health-findings-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ageLabel, findingKey, pillFindings, pillState } from './healthFindings'
import type { HealthFinding } from '../types'

function f(id: string, severity: 'warn' | 'crit' = 'warn', subject = ''): HealthFinding {
  return { id, severity, title: id, detail: 'd', fix_hint: 'h', subject }
}

test('a healthy project renders no pill at all', () => {
  assert.equal(pillState([]), null)
  assert.equal(pillState(null), null)
  assert.equal(pillState(undefined), null)
})

test('the pill counts findings and says warning-only is not critical', () => {
  const s = pillState([f('stale_work'), f('no_test_cmd')])!
  assert.equal(s.count, 2)
  assert.equal(s.crit, false)
  assert.equal(s.label, '⚠ 2')
  assert.equal(s.title, '2 issues')
})

test('any critical finding makes the pill critical and is named in the tooltip', () => {
  const s = pillState([f('stale_work'), f('memory_index_near_cap', 'crit', 'native')])!
  assert.equal(s.crit, true)
  assert.equal(s.title, '2 issues, 1 critical')
  assert.equal(pillState([f('x', 'crit')])!.title, '1 issue, 1 critical')
})

test('the .env exposed finding is not shown twice (its own header pill covers it)', () => {
  assert.equal(pillState([f('env_exposed', 'crit')]), null)
  const s = pillState([f('env_exposed', 'crit'), f('stale_work')])!
  assert.equal(s.count, 1)
  assert.equal(s.crit, false)
  assert.deepEqual(pillFindings([f('env_exposed'), f('stale_work')]).map(x => x.id), ['stale_work'])
})

test('several findings of one check get distinct keys', () => {
  const a = f('memory_index_near_cap', 'warn', 'native')
  const b = f('memory_index_near_cap', 'warn', 'curated')
  assert.notEqual(findingKey(a), findingKey(b))
})

test('ageLabel is coarse, never negative, and empty when unknown', () => {
  const now = 1_790_000_000
  assert.equal(ageLabel(null, now), '')
  assert.equal(ageLabel(undefined, now), '')
  assert.equal(ageLabel(now - 5, now), 'just now')
  assert.equal(ageLabel(now + 100, now), 'just now')
  assert.equal(ageLabel(now - 5 * 60, now), '5m ago')
  assert.equal(ageLabel(now - 3 * 3600, now), '3h ago')
  assert.equal(ageLabel(now - 2 * 86400 - 10, now), '2d ago')
})

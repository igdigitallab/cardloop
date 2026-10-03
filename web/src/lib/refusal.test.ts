// spec-095 — telling a deliberate server refusal from busy / stale-revision 409s
// (web/src/lib/refusal.ts).
//   cd web
//   npx esbuild src/lib/refusal.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/refusal-test/refusal.test.cjs --log-level=warning
//   node --test /tmp/refusal-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { errorTextFromBody, refusalReason } from './refusal'

const err = (status: number, body: unknown) => Object.assign(new Error(JSON.stringify(body)), { status, body })

test('the Grok privacy gate 409 is a refusal, in the server\'s own words', () => {
  assert.equal(refusalReason(err(409, { error: 'grok is not enabled for this project' })),
    'grok is not enabled for this project')
  // a project pinned to a backend refuses a chat move the same way
  assert.match(refusalReason(err(409, { error: "this project is pinned to the 'ollama' backend", project_pinned: 'ollama' })) || '', /pinned/)
})

test('busy, backend-down and stale-revision 409s are NOT refusals - their sites handle them', () => {
  assert.equal(refusalReason(err(409, { error: 'cannot change runtime: turn running', busy: true })), null)
  assert.equal(refusalReason(err(409, { error: 'down', backend_unavailable: true })), null)
  assert.equal(refusalReason(err(409, { error: 'runtime_revision mismatch', current_revision: 3 })), null)
  assert.equal(refusalReason(err(409, { error: 'revision stale', current_revision: 0 })), null)
  assert.equal(refusalReason(err(409, { error: 'project busy' })), null)
  assert.equal(refusalReason(err(409, { error: 'project is busy, cannot rename' })), null)
})

test('only a 409 with a sentence in its body can be a refusal', () => {
  assert.equal(refusalReason(err(400, { error: 'invalid thing' })), null)
  assert.equal(refusalReason(err(500, { error: 'boom' })), null)
  assert.equal(refusalReason(err(409, {})), null)
  assert.equal(refusalReason(err(409, { error: '' })), null)
  assert.equal(refusalReason(err(409, { error: 42 })), null)
  assert.equal(refusalReason(Object.assign(new Error('x'), { status: 409, body: null })), null)
  assert.equal(refusalReason(new Error('network down')), null)
  assert.equal(refusalReason(null), null)
  assert.equal(refusalReason(undefined), null)
})

test('error text: the sentence inside {"error":…}, anything else untouched', () => {
  assert.equal(errorTextFromBody('{"error":"grok is not enabled for this project"}'), 'grok is not enabled for this project')
  assert.equal(errorTextFromBody('{"ok":false}'), '{"ok":false}')
  assert.equal(errorTextFromBody('{"error":""}'), '{"error":""}')
  assert.equal(errorTextFromBody('{"error":{"code":1}}'), '{"error":{"code":1}}')
  assert.equal(errorTextFromBody('<html>502 Bad Gateway</html>'), '<html>502 Bad Gateway</html>')
  assert.equal(errorTextFromBody(''), '')
  assert.equal(errorTextFromBody('null'), 'null')
})

// Unit tests for how a chat message splits into body / file cards / option picker
// (web/src/lib/messageLayout.ts). Same harness as runtimeStatus.test.ts:
//
//   cd web
//   npx esbuild src/lib/messageLayout.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/message-layout-test/messageLayout.test.cjs --log-level=warning
//   node --test /tmp/message-layout-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { layoutMessage } from './messageLayout'

const PDF = '/api/projects/acme/media/1791912799_Statement.pdf'
const JPG = '/api/projects/acme/media/1791912799_License.jpg'

test('a reply that ends with a picker still turns its file lines into cards', () => {
  // Measured 2026-10-13: 135 of 260 agent replies that dropped a file also ended with an
  // ```options block; the picker branch rendered the file lines as plain text, glued into
  // one line by markdown, with no download link.
  const text = [
    'Upload these two:',
    '',
    `attached file: ${JPG}`,
    `attached file: ${PDF}`,
    '',
    'Then pick one:',
    '',
    '```options',
    'I will fix the domain myself',
    'Do it through the browser',
    '```',
  ].join('\n')
  const out = layoutMessage(text, 'assistant', false, 'acme')
  assert.deepEqual(out.files, [
    { url: JPG, name: 'License.jpg', kind: 'image' },
    { url: PDF, name: 'Statement.pdf', kind: 'file' },
  ])
  assert.deepEqual(out.options?.map(o => o.value), ['I will fix the domain myself', 'Do it through the browser'])
  assert.equal(out.body, 'Upload these two:\n\n\nThen pick one:')
  assert.ok(!out.body.includes('attached file'))
})

test('a reply without a picker keeps its cards and has no options', () => {
  const out = layoutMessage(`Sent again:\n\nattached file: ${PDF}\n`, 'assistant', false, 'acme')
  assert.deepEqual(out.files, [{ url: PDF, name: 'Statement.pdf', kind: 'file' }])
  assert.equal(out.options, null)
  assert.equal(out.body, 'Sent again:')
})

test('a picker with no file lines is unchanged', () => {
  const out = layoutMessage('Choose:\n\n```options\nA\nB\n```', 'assistant', false, 'acme')
  assert.deepEqual(out.files, [])
  assert.equal(out.body, 'Choose:')
  assert.deepEqual(out.options?.map(o => o.value), ['A', 'B'])
})

test('a streaming reply is shown as typed: no cards, no picker yet', () => {
  const text = `attached file: ${PDF}\n\n\`\`\`options\nA\n\`\`\``
  const out = layoutMessage(text, 'assistant', true, 'acme')
  assert.deepEqual(out, { body: text, files: [], options: null })
})

test('an operator upload maps its absolute path to the upload route; users get no picker', () => {
  const text = 'look at this\nattached file: /home/op/cardloop/data/inbox/1791912799_scan.png\n```options\nA\n```'
  const out = layoutMessage(text, 'user', false, 'acme')
  assert.deepEqual(out.files, [
    { url: '/api/projects/acme/upload/1791912799_scan.png', name: 'scan.png', kind: 'image' },
  ])
  assert.equal(out.options, null)
})

test('a file line indented by the model still becomes a card', () => {
  const out = layoutMessage(`Here:\n   attached file: ${PDF}`, 'assistant', false, 'acme')
  assert.deepEqual(out.files.map(f => f.url), [PDF])
  assert.equal(out.body, 'Here:')
})

test('a file line inside a fenced code block is example text, not an attachment', () => {
  const text = `The helper prints:\n\n\`\`\`\nattached file: ${PDF}\n\`\`\``
  const out = layoutMessage(text, 'assistant', false, 'acme')
  assert.deepEqual(out.files, [])
  assert.equal(out.body, text)
})

test('a line that only mentions the marker mid-sentence is left alone', () => {
  const text = `Full text: attached file: ${PDF}`
  const out = layoutMessage(text, 'assistant', false, 'acme')
  assert.deepEqual(out.files, [])
  assert.equal(out.body, text)
})

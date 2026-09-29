// Unit tests for draft persistence helpers (web/src/lib/filesDrafts.ts).
//
//   cd web
//   npx esbuild src/lib/filesDrafts.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/filesdrafts-test/filesDrafts.test.cjs --log-level=warning
//   node --test /tmp/filesdrafts-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { collectDrafts, MAX_DRAFT_CHARS, parseDrafts } from './filesDrafts'
import { EMPTY_TABS, tabsReducer, TabsAction } from './filesTabs'

const doc = (content: string, rev = 'r1') => ({ content, rev, editable: true, lang: 'md', size: content.length })
const run = (actions: TabsAction[]) => actions.reduce(tabsReducer, EMPTY_TABS)

test('only tabs with real unsaved edits are collected, with the rev they were typed against', () => {
  const s = run([
    { type: 'open', path: '/dirty' }, { type: 'loaded', path: '/dirty', doc: doc('one', 'r7') },
    { type: 'startEdit', path: '/dirty' }, { type: 'setDraft', path: '/dirty', text: 'two' },
    { type: 'open', path: '/clean-editing' }, { type: 'loaded', path: '/clean-editing', doc: doc('x') },
    { type: 'startEdit', path: '/clean-editing' },
    { type: 'open', path: '/viewing' }, { type: 'loaded', path: '/viewing', doc: doc('y') },
  ])
  assert.deepEqual(collectDrafts(s.tabs), { '/dirty': { draft: 'two', rev: 'r7' } })
})

test('a draft past the size cap is not stored', () => {
  const s = run([
    { type: 'open', path: '/big' }, { type: 'loaded', path: '/big', doc: doc('a') },
    { type: 'startEdit', path: '/big' }, { type: 'setDraft', path: '/big', text: 'x'.repeat(MAX_DRAFT_CHARS + 1) },
  ])
  assert.deepEqual(collectDrafts(s.tabs), {})
})

test('parseDrafts round-trips and drops garbage', () => {
  const ok = { '/a': { draft: 'd', rev: 'r' } }
  assert.deepEqual(parseDrafts(JSON.stringify(ok)), ok)
  assert.deepEqual(parseDrafts(null), {})
  assert.deepEqual(parseDrafts('{oops'), {})
  assert.deepEqual(parseDrafts(JSON.stringify({ rel: { draft: 'd', rev: 'r' }, '/b': { draft: 1, rev: 'r' }, '/c': null })), {})
})

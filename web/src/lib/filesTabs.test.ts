// Unit tests for the Files explorer's tab state (web/src/lib/filesTabs.ts).
//
//   cd web
//   npx esbuild src/lib/filesTabs.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/filestabs-test/filesTabs.test.cjs --log-level=warning
//   node --test /tmp/filestabs-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  EMPTY_TABS, DocSnapshot, isDirty, parsePersisted, tabsReducer, toPersisted, TabsAction, TabsState,
} from './filesTabs'

const doc = (content: string, rev = 'r1', extra: Partial<DocSnapshot> = {}): DocSnapshot =>
  ({ content, rev, editable: true, lang: 'md', size: content.length, ...extra })

function run(actions: TabsAction[], from: TabsState = EMPTY_TABS): TabsState {
  return actions.reduce(tabsReducer, from)
}
const tab = (s: TabsState, p: string) => s.tabs.find(t => t.path === p)!

test('open adds a loading tab and activates it; opening twice does not duplicate', () => {
  const s = run([{ type: 'open', path: '/a.md' }, { type: 'open', path: '/b.md' }, { type: 'open', path: '/a.md' }])
  assert.deepEqual(s.tabs.map(t => t.path), ['/a.md', '/b.md'])
  assert.equal(s.active, '/a.md')
  assert.equal(tab(s, '/b.md').status, 'loading')
})

test('closing the active tab activates the right neighbour, else the left, else nothing', () => {
  const base = run([{ type: 'open', path: '/a' }, { type: 'open', path: '/b' }, { type: 'open', path: '/c' }])
  assert.equal(run([{ type: 'activate', path: '/b' }, { type: 'close', path: '/b' }], base).active, '/c')
  assert.equal(run([{ type: 'close', path: '/c' }], base).active, '/b')
  assert.equal(run([{ type: 'activate', path: '/a' }, { type: 'close', path: '/b' }], base).active, '/a')
  const one = run([{ type: 'open', path: '/a' }, { type: 'close', path: '/a' }])
  assert.deepEqual(one, EMPTY_TABS)
})

test('a server refusal (binary/too large) becomes an error tab, not text', () => {
  const s = run([{ type: 'open', path: '/x' }, { type: 'loaded', path: '/x', doc: doc('', 'r', { error: 'binary file', editable: false }) }])
  assert.equal(tab(s, '/x').status, 'error')
  assert.equal(tab(s, '/x').error, 'binary file')
})

test('dirty is derived from draft vs content; cancel drops the draft', () => {
  let s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one') }, { type: 'startEdit', path: '/a' }])
  assert.equal(isDirty(tab(s, '/a')), false)
  s = run([{ type: 'setDraft', path: '/a', text: 'two' }], s)
  assert.equal(isDirty(tab(s, '/a')), true)
  s = run([{ type: 'setDraft', path: '/a', text: 'one' }], s)
  assert.equal(isDirty(tab(s, '/a')), false)
  s = run([{ type: 'setDraft', path: '/a', text: 'two' }, { type: 'cancelEdit', path: '/a' }], s)
  assert.equal(tab(s, '/a').draft, null)
})

test('startEdit is refused for a read-only or errored tab', () => {
  const ro = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('x', 'r', { editable: false }) }, { type: 'startEdit', path: '/a' }])
  assert.equal(tab(ro, '/a').draft, null)
})

test('typing while a save is in flight keeps the tab dirty and in edit mode', () => {
  let s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one') }, { type: 'startEdit', path: '/a' },
    { type: 'setDraft', path: '/a', text: 'two' }, { type: 'saveStart', path: '/a' }, { type: 'setDraft', path: '/a', text: 'two!' }])
  s = run([{ type: 'saveOk', path: '/a', saved: 'two', rev: 'r2' }], s)
  const t = tab(s, '/a')
  assert.equal(t.content, 'two')
  assert.equal(t.rev, 'r2')
  assert.equal(t.draft, 'two!')
  assert.equal(isDirty(t), true)
  assert.equal(t.saving, false)
})

test('a clean save leaves edit mode', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one') }, { type: 'startEdit', path: '/a' },
    { type: 'setDraft', path: '/a', text: 'two' }, { type: 'saveStart', path: '/a' }, { type: 'saveOk', path: '/a', saved: 'two', rev: 'r2' }])
  assert.equal(tab(s, '/a').draft, null)
})

test('a 409 keeps the draft and raises conflict', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one') }, { type: 'startEdit', path: '/a' },
    { type: 'setDraft', path: '/a', text: 'mine' }, { type: 'saveStart', path: '/a' }, { type: 'saveFail', path: '/a', message: 'changed on disk', conflict: true }])
  const t = tab(s, '/a')
  assert.equal(t.conflict, true)
  assert.equal(t.draft, 'mine')
  assert.equal(t.saving, false)
})

test('a refresh replaces a clean tab silently', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1') }, { type: 'refreshed', path: '/a', doc: doc('agent wrote', 'r2') }])
  assert.equal(tab(s, '/a').content, 'agent wrote')
  assert.equal(tab(s, '/a').diskChanged, false)
})

test('a refresh never overwrites unsaved edits: it only flags diskChanged', () => {
  let s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1') }, { type: 'startEdit', path: '/a' },
    { type: 'setDraft', path: '/a', text: 'mine' }, { type: 'refreshed', path: '/a', doc: doc('agent wrote', 'r2') }])
  assert.equal(tab(s, '/a').draft, 'mine')
  assert.equal(tab(s, '/a').content, 'one')
  assert.equal(tab(s, '/a').diskChanged, true)
  s = run([{ type: 'reload', path: '/a', doc: doc('agent wrote', 'r2') }], s)
  assert.equal(tab(s, '/a').draft, null)
  assert.equal(tab(s, '/a').content, 'agent wrote')
  assert.equal(tab(s, '/a').diskChanged, false)
})

test('an unchanged revision is a no-op (same object, no re-render)', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1') }])
  assert.equal(tabsReducer(s, { type: 'refreshed', path: '/a', doc: doc('one', 'r1') }), s)
})

test('a refresh of an editing-but-clean tab follows the disk', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1') }, { type: 'startEdit', path: '/a' },
    { type: 'refreshed', path: '/a', doc: doc('two', 'r2') }])
  assert.equal(tab(s, '/a').draft, 'two')
  assert.equal(isDirty(tab(s, '/a')), false)
})

test('actions on a path that was closed meanwhile are ignored', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'close', path: '/a' }])
  assert.equal(tabsReducer(s, { type: 'loaded', path: '/a', doc: doc('late') }), s)
})

test('persisted state round-trips and rejects garbage', () => {
  const s = run([{ type: 'open', path: '/a.md' }, { type: 'open', path: '/b.md' }])
  const p = toPersisted('/root', s)
  assert.deepEqual(parsePersisted(JSON.stringify(p)), p)
  assert.equal(parsePersisted(null), null)
  assert.equal(parsePersisted('{oops'), null)
  assert.equal(parsePersisted(JSON.stringify({ v: 2, root: '/', paths: [] })), null)
  const odd = parsePersisted(JSON.stringify({ v: 1, root: '/r', active: '/gone', paths: ['/a', 5, 'rel'] }))
  assert.deepEqual(odd, { v: 1, root: '/r', active: '/a', paths: ['/a'] })
})

test('a refresh that began before a save landed is dropped (it would roll the tab back)', () => {
  let s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1') }, { type: 'startEdit', path: '/a' },
    { type: 'setDraft', path: '/a', text: 'two' }])
  const genAtRefreshStart = tab(s, '/a').gen
  s = run([{ type: 'saveStart', path: '/a' }, { type: 'saveOk', path: '/a', saved: 'two', rev: 'r2' }], s)
  s = run([{ type: 'refreshed', path: '/a', doc: doc('one', 'r1'), gen: genAtRefreshStart }], s)
  assert.equal(tab(s, '/a').content, 'two')
  assert.equal(tab(s, '/a').rev, 'r2')
  // a refresh started after the save is honoured
  s = run([{ type: 'refreshed', path: '/a', doc: doc('agent', 'r3'), gen: tab(s, '/a').gen }], s)
  assert.equal(tab(s, '/a').content, 'agent')
})

test('a stored draft comes back as an edit in progress', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1') },
    { type: 'restoreDraft', path: '/a', draft: 'unsaved words', rev: 'r1' }])
  const t = tab(s, '/a')
  assert.equal(t.draft, 'unsaved words')
  assert.equal(isDirty(t), true)
  assert.equal(t.diskChanged, false)
})

test('a stored draft made against an older disk keeps the old rev, so the save conflicts', () => {
  const s = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('agent rewrote', 'r9') },
    { type: 'restoreDraft', path: '/a', draft: 'my old edit', rev: 'r1' }])
  const t = tab(s, '/a')
  assert.equal(t.rev, 'r1')
  assert.equal(t.content, 'agent rewrote')
  assert.equal(t.diskChanged, true)
})

test('a stored draft equal to the disk, or for a read-only tab, is not restored', () => {
  const same = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r2') },
    { type: 'restoreDraft', path: '/a', draft: 'one', rev: 'r1' }])
  assert.equal(tab(same, '/a').draft, null)
  const ro = run([{ type: 'open', path: '/a' }, { type: 'loaded', path: '/a', doc: doc('one', 'r1', { editable: false }) },
    { type: 'restoreDraft', path: '/a', draft: 'x', rev: 'r1' }])
  assert.equal(tab(ro, '/a').draft, null)
})

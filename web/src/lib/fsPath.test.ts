// Unit tests for the Files explorer's path helpers (web/src/lib/fsPath.ts).
//
//   cd web
//   npx esbuild src/lib/fsPath.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/fspath-test/fsPath.test.cjs --log-level=warning
//   node --test /tmp/fspath-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  ancestorsBetween, baseName, dirname, displayPath, formatAgo, isUnder, joinPath, looksLikePath, resolveRelative, tabLabels,
} from './fsPath'

test('dirname / baseName / joinPath handle the root', () => {
  assert.equal(dirname('/a/b/c.md'), '/a/b')
  assert.equal(dirname('/a'), '/')
  assert.equal(dirname('/'), '/')
  assert.equal(baseName('/a/b/c.md'), 'c.md')
  assert.equal(baseName('/'), '/')
  assert.equal(joinPath('/', 'x'), '/x')
  assert.equal(joinPath('/a/b', 'x'), '/a/b/x')
})

test('isUnder does not confuse sibling prefixes', () => {
  assert.equal(isUnder('/home/igor/proj/a', '/home/igor/proj'), true)
  assert.equal(isUnder('/home/igor/proj', '/home/igor/proj'), true)
  assert.equal(isUnder('/home/igor/project2/a', '/home/igor/proj'), false)
  assert.equal(isUnder('/tmp/x', '/'), true)
})

test('ancestorsBetween lists the folders to open, root excluded', () => {
  assert.deepEqual(ancestorsBetween('/h/p', '/h/p/docs/deep/a.md'), ['/h/p/docs', '/h/p/docs/deep'])
  assert.deepEqual(ancestorsBetween('/h/p', '/h/p/a.md'), [])
  assert.deepEqual(ancestorsBetween('/h/p', '/elsewhere/a.md'), [])
  assert.deepEqual(ancestorsBetween('/h/p', '/h/p'), [])
  assert.deepEqual(ancestorsBetween('/', '/tmp/a.md'), ['/tmp'])
})

test('displayPath shortens home', () => {
  assert.equal(displayPath('/home/igor', '/home/igor'), '~')
  assert.equal(displayPath('/home/igor/x/y', '/home/igor'), '~/x/y')
  assert.equal(displayPath('/tmp/x', '/home/igor'), '/tmp/x')
  assert.equal(displayPath('/home/igorx/y', '/home/igor'), '/home/igorx/y')
})

test('tabLabels adds the parent folder only for duplicate names', () => {
  assert.deepEqual(tabLabels(['/a/x/README.md', '/a/y/README.md', '/a/z/notes.md']), [
    { name: 'README.md', hint: 'x' },
    { name: 'README.md', hint: 'y' },
    { name: 'notes.md', hint: '' },
  ])
})

test('looksLikePath fires on pasted paths, not on prose', () => {
  for (const yes of ['/home/igor/x.md', '`/tmp/a.md`', '"/tmp/a b.md"', '(/tmp/a.md)', '~/x', '~', '$HOME/x',
    'file:///tmp/a.md', '/tmp/a.md:12', '  /tmp/a.md\nsecond line']) assert.equal(looksLikePath(yes), true, yes)
  for (const no of ['', 'hello world', '/ 5 apples', '//', 'see /tmp/a.md for details', 'a/b/c', 'C:\\x', 'x  /tmp/a']) {
    assert.equal(looksLikePath(no), false, no)
  }
})

test('resolveRelative walks . and .. and never climbs past the root', () => {
  assert.equal(resolveRelative('/a/b', 'c.png'), '/a/b/c.png')
  assert.equal(resolveRelative('/a/b', './img/c.png'), '/a/b/img/c.png')
  assert.equal(resolveRelative('/a/b', '../c.png'), '/a/c.png')
  assert.equal(resolveRelative('/a/b', '../../../../c.png'), '/c.png')
  assert.equal(resolveRelative('/a/b', 'x//y'), '/a/b/x/y')
})

test('formatAgo picks a readable unit', () => {
  const now = 1_000_000_000_000
  const at = (agoS: number) => formatAgo(now / 1000 - agoS, now)
  assert.equal(at(5), 'now')
  assert.equal(at(90), '2m')
  assert.equal(at(3 * 3600), '3h')
  assert.equal(at(2 * 86400), '2d')
  assert.equal(formatAgo(now / 1000 + 100, now), 'now') // clock skew never prints a negative
})

// Unit tests for spotting file paths in agent messages (web/src/lib/fileRefs.ts).
//
//   cd web
//   npx esbuild src/lib/fileRefs.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/filerefs-test/fileRefs.test.cjs --log-level=warning
//   node --test /tmp/filerefs-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { decodeFileRef, FILE_REF_PREFIX, looksLikeFileRef, remarkFileRefs, splitLoosePaths, stripLineSuffix } from './fileRefs'

test('inline code: real file references are linked', () => {
  for (const yes of ['/home/igor/x/report.md', '~/notes.txt', 'docs/plan.md', 'web/src/FileExplorer.tsx',
    'webapp.py', 'README.md', './a/b.json', '../x.png', '/tmp/out.pdf:12', 'webapp.py:120:5', 'a.py#L10-L20', 'Shot.PNG']) {
    assert.equal(looksLikeFileRef(yes), true, yes)
  }
})

test('inline code: commands, URLs, identifiers and prose are not', () => {
  for (const no of ['', 'npm run build', 'git push origin master', 'https://x.com/a.md', '/api/fs/raw', '/restart',
    'obj.method', 'e.g.', 'v1.2.3', 'fs_browser', '--flag.md', '@scope/pkg.json', 'a*.md', 'x = y.py',
    'cmd | tee out.log', 'file.', '.env', 'foo.bar', 'a'.repeat(400) + '.md']) {
    assert.equal(looksLikeFileRef(no), false, no)
  }
})

test('stripLineSuffix', () => {
  assert.equal(stripLineSuffix('a.py:12'), 'a.py')
  assert.equal(stripLineSuffix('a.py:12:3'), 'a.py')
  assert.equal(stripLineSuffix('a.py#L4-L9'), 'a.py')
  assert.equal(stripLineSuffix('a.py'), 'a.py')
})

test('loose text: only absolute-looking paths under the usual roots', () => {
  const pieces = splitLoosePaths('Saved to /home/igor/cardloop/docs/plan.md, and ~/notes/x.txt. Not and/or 1/2 or /api/foo.')
  assert.deepEqual(pieces.filter(p => p.path).map(p => p.path), ['/home/igor/cardloop/docs/plan.md', '~/notes/x.txt'])
  assert.equal(pieces.map(p => p.text).join(''), 'Saved to /home/igor/cardloop/docs/plan.md, and ~/notes/x.txt. Not and/or 1/2 or /api/foo.')
})

test('loose text: trailing punctuation and wrapping brackets stay outside the link', () => {
  const at = (s: string) => splitLoosePaths(s).find(p => p.path)?.path
  assert.equal(at('see (/tmp/a.md).'), '/tmp/a.md')
  assert.equal(at('done: /tmp/dir/a.md!'), '/tmp/dir/a.md')
  assert.equal(at('"/var/log/x.log"'), '/var/log/x.log')
  assert.equal(at('a.b/tmp/x'), undefined)     // not at a word start
  assert.equal(at('https://h.com/home/x/y'), undefined)
})

test('the remark plugin links prose paths but leaves code, links and existing links alone', () => {
  const tree = {
    type: 'root',
    children: [
      { type: 'paragraph', children: [
        { type: 'text', value: 'Wrote /tmp/report.md ok' },
        { type: 'inlineCode', value: '/tmp/keep.md' },
        { type: 'link', url: 'http://x', children: [{ type: 'text', value: '/tmp/in-link.md' }] },
        { type: 'strong', children: [{ type: 'text', value: '~/deep/bold.md' }] },
      ] },
      { type: 'code', value: '/tmp/block.md' },
    ],
  }
  remarkFileRefs()(tree as never)
  const p = (tree.children[0] as { children: { type: string; url?: string; value?: string; children?: { value?: string }[] }[] }).children
  const links = p.filter(n => n.type === 'link' && n.url?.startsWith(FILE_REF_PREFIX))
  assert.deepEqual(links.map(l => decodeURIComponent(l.url!.slice(FILE_REF_PREFIX.length))), ['/tmp/report.md'])
  assert.ok(p.some(n => n.type === 'inlineCode' && n.value === '/tmp/keep.md'))
  assert.ok(p.some(n => n.type === 'link' && n.url === 'http://x'))
  const strong = p.find(n => n.type === 'strong') as unknown as { children: { type: string; url?: string }[] }
  assert.ok(strong.children.some(n => n.type === 'link' && n.url?.startsWith(FILE_REF_PREFIX)))
  assert.equal((tree.children[1] as { value: string }).value, '/tmp/block.md')
})

test('no regex lookbehind anywhere in the detector (Safari < 16.4 cannot parse it)', () => {
  // The runner's cwd is web/ by hand and the repo root under pytest — look in both.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const fs = require('node:fs') as typeof import('node:fs')
  const file = ['src/lib/fileRefs.ts', 'web/src/lib/fileRefs.ts'].find(f => fs.existsSync(f))
  assert.ok(file, 'fileRefs.ts not found from ' + process.cwd())
  const code = fs.readFileSync(file as string, 'utf8').split('\n')
    .filter(l => !l.trim().startsWith('*') && !l.trim().startsWith('//')).join('\n')
  assert.equal(/\(\?<[=!]/.test(code), false)
})

test('decodeFileRef never throws on a malformed reference', () => {
  assert.equal(decodeFileRef(FILE_REF_PREFIX + encodeURIComponent('/tmp/a b.md')), '/tmp/a b.md')
  for (const bad of [FILE_REF_PREFIX + '%E0%A4', FILE_REF_PREFIX + '%ZZ', FILE_REF_PREFIX + '%', FILE_REF_PREFIX,
    FILE_REF_PREFIX + '%00', undefined, '', '#other', 'http://x']) {
    assert.equal(decodeFileRef(bad), null, String(bad))
  }
})

test('frameworks and bare file names', () => {
  for (const no of ['node.js', 'Next.js', 'Chart.js', 'socket.io']) assert.equal(looksLikeFileRef(no), false, no)
  assert.equal(looksLikeFileRef('src/node.js'), true)    // with a folder it is a real file
  for (const yes of ['Dockerfile', 'Makefile', '.gitignore', 'deploy/Dockerfile']) assert.equal(looksLikeFileRef(yes), true, yes)
  assert.equal(looksLikeFileRef('.env'), false)          // secrets are never linked
})

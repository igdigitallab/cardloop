// Unit tests for spotting file paths in agent messages (web/src/lib/fileRefs.ts).
//
//   cd web
//   npx esbuild src/lib/fileRefs.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/filerefs-test/fileRefs.test.cjs --log-level=warning
//   node --test /tmp/filerefs-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { FILE_REF_PREFIX, looksLikeFileRef, remarkFileRefs, splitLoosePaths, stripLineSuffix } from './fileRefs'

test('inline code: real file references are linked', () => {
  for (const yes of ['/home/igor/x/report.md', '~/notes.txt', 'docs/plan.md', 'web/src/FileExplorer.tsx',
    'webapp.py', 'README.md', './a/b.json', '../x.png', '/tmp/out.pdf:12', 'webapp.py:120:5', 'a.py#L10-L20', 'Shot.PNG']) {
    assert.equal(looksLikeFileRef(yes), true, yes)
  }
})

test('inline code: commands, URLs, identifiers and prose are not', () => {
  for (const no of ['', 'npm run build', 'git push origin master', 'https://x.com/a.md', '/api/fs/raw', '/restart',
    'obj.method', 'e.g.', 'v1.2.3', 'fs_browser', '--flag.md', '@scope/pkg.json', 'a*.md', 'x = y.py',
    'cmd | tee out.log', 'file.', '.gitignore', 'foo.bar', 'a'.repeat(400) + '.md']) {
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

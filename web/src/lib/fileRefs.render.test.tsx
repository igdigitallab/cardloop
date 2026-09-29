// The file-ref plugin through the REAL react-markdown / remark-gfm pipeline. The unit tests in
// fileRefs.test.ts hand-build a syntax tree, so they cannot see what the sanitiser, GFM or the
// renderer do to it — which is exactly where a malformed link once threatened to crash the chat.
//
//   cd web
//   npx esbuild src/lib/fileRefs.render.test.tsx --bundle --platform=node --format=cjs \
//     --outfile=/tmp/filerefs-render/fileRefs.render.test.cjs --log-level=warning
//   node --test /tmp/filerefs-render/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { decodeFileRef, remarkFileRefs } from './fileRefs'

// A stand-in for ChatTab's ChatLink: what it does with an href is decodeFileRef, nothing else.
const components = {
  a({ href, children }: { href?: string; children?: React.ReactNode }) {
    const path = decodeFileRef(href)
    return path ? <a data-open={path}>{children}</a> : <a href={href}>{children}</a>
  },
}

const render = (md: string) => renderToStaticMarkup(
  <ReactMarkdown remarkPlugins={[remarkGfm, remarkFileRefs]} components={components}>{md}</ReactMarkdown>)

test('a prose path becomes a link that survives the default url sanitiser', () => {
  assert.match(render('Wrote /tmp/out/report.md ok'), /<a data-open="\/tmp\/out\/report\.md">\/tmp\/out\/report\.md<\/a>/)
})

test('a malformed reference in a message renders instead of throwing', () => {
  for (const md of ['[x](#cardloop-file=%E0%A4)', '[x](#cardloop-file=%ZZ)', '[x](#cardloop-file=)']) {
    assert.doesNotThrow(() => render(md), md)
    assert.match(render(md), /<a[^>]*>x<\/a>/, md)
    assert.doesNotMatch(render(md), /data-open/, md)
  }
})

test('tables, lists, bold and code keep their structure around linked paths', () => {
  const html = render([
    '| file | note |', '|---|---|', '| /home/igor/a.md | see `docs/b.md` |', '',
    '- item /tmp/x/y.txt', '- **bold /var/log/z.log**', '', '```', '/tmp/in-a-block.md', '```',
  ].join('\n'))
  assert.match(html, /<table>/)
  assert.match(html, /<li>item <a data-open="\/tmp\/x\/y\.txt">/)
  assert.match(html, /<strong>bold <a data-open="\/var\/log\/z\.log">/)
  assert.doesNotMatch(html, /data-open="\/tmp\/in-a-block\.md"/)   // fenced code is left alone
})

test('the pipeline is stable across renders (streaming re-renders the same text)', () => {
  const md = 'Saved /tmp/a.md and /tmp/b.md.'
  assert.equal(render(md), render(md))
})

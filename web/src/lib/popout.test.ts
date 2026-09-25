// Unit tests for the pop-out window's pure logic (web/src/lib/popout.ts).
//
// Same harness as src/lib/runtimeStatus.test.ts: Node's built-in runner, no new dependency.
//
//   cd web
//   npx esbuild src/lib/popout.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/popout-test/popout.test.cjs --log-level=warning
//   node --test /tmp/popout-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  isMisplaced, openProjectWindow, parseBounds, parsePopoutParams, popoutKey, popoutUrl,
  popoutWindowName, windowFeatures,
} from './popout'

test('parsePopoutParams: only ?popout=<id> makes a pop-out', () => {
  assert.equal(parsePopoutParams(''), null)
  assert.equal(parsePopoutParams('?popout='), null)
  assert.equal(parsePopoutParams('?popout=%20'), null)
  assert.deepEqual(parsePopoutParams('?popout=cardloop'), { projectId: 'cardloop', tab: null })
  assert.deepEqual(parsePopoutParams('?popout=cardloop&tab=board'), { projectId: 'cardloop', tab: 'board' })
})

test('parsePopoutParams: an unknown tab is dropped, not trusted', () => {
  assert.deepEqual(parsePopoutParams('?popout=x&tab=secrets'), { projectId: 'x', tab: null })
  assert.deepEqual(parsePopoutParams('?popout=x&tab=claude-md'), { projectId: 'x', tab: null })
})

test('popoutUrl round-trips through parsePopoutParams', () => {
  const url = popoutUrl('my project/1', 'browser')
  assert.ok(url.startsWith('/?'))
  assert.deepEqual(parsePopoutParams(url.slice(1)), { projectId: 'my project/1', tab: 'browser' })
  assert.deepEqual(parsePopoutParams(popoutUrl('p').slice(1)), { projectId: 'p', tab: null })
})

test('popoutWindowName is stable per project and distinct across projects', () => {
  assert.equal(popoutWindowName('a'), popoutWindowName('a'))
  assert.notEqual(popoutWindowName('a'), popoutWindowName('b'))
  assert.match(popoutWindowName('weird id/..'), /^cardloop-popout-[A-Za-z0-9_-]+$/)
})

test('popoutKey never collides with the main window key', () => {
  assert.notEqual(popoutKey('cops.chatWidth'), 'cops.chatWidth')
})

test('parseBounds rejects garbage and windows too small to use', () => {
  assert.equal(parseBounds(null), null)
  assert.equal(parseBounds('not json'), null)
  assert.equal(parseBounds('{"left":0,"top":0,"width":"800","height":600}'), null)
  assert.equal(parseBounds('{"left":0,"top":0,"width":100,"height":600}'), null)
  assert.deepEqual(
    parseBounds('{"left":-1920,"top":10,"width":1900,"height":1000}'),
    { left: -1920, top: 10, width: 1900, height: 1000 },
  )
})

test('windowFeatures: a popup, sized; position only when remembered', () => {
  assert.equal(windowFeatures(null), 'popup,width=1400,height=900')
  assert.equal(
    windowFeatures({ left: 3840.4, top: -8, width: 1920, height: 1040 }),
    'popup,width=1920,height=1040,left=3840,top=-8',
  )
})

test('isMisplaced tolerates window-frame jitter but not another monitor', () => {
  const saved = { left: 3840, top: 0, width: 1920, height: 1040 }
  assert.equal(isMisplaced(saved, 3848, -8), false)
  assert.equal(isMisplaced(saved, 100, 100), true)
})

// ── openProjectWindow against a fake window.open ────────────────────────────────

interface FakeWin { location: { href: string }; focused: boolean; focus(): void }

function withFakeWindow(existing: Record<string, FakeWin>, fn: (calls: string[][]) => void): void {
  const calls: string[][] = []
  const g = globalThis as unknown as { window?: unknown; localStorage?: unknown }
  const prevWindow = g.window
  const prevLS = g.localStorage
  g.localStorage = { getItem: () => null, setItem: () => {} }
  g.window = {
    open(url: string, name: string, features: string) {
      calls.push([url, name, features])
      if (name === 'BLOCK') return null
      if (!existing[name]) {
        existing[name] = { location: { href: 'about:blank' }, focused: false, focus() { this.focused = true } }
      }
      return existing[name]
    },
  }
  try { fn(calls) } finally {
    g.window = prevWindow
    g.localStorage = prevLS
  }
}

test('openProjectWindow: first click opens by name and points the blank window at the pop-out', () => {
  const wins: Record<string, FakeWin> = {}
  withFakeWindow(wins, calls => {
    assert.equal(openProjectWindow('cardloop', 'browser'), true)
    assert.equal(calls.length, 1)
    assert.equal(calls[0][0], '', 'must open by name with an EMPTY url')
    const w = wins[popoutWindowName('cardloop')]
    assert.equal(w.location.href, popoutUrl('cardloop', 'browser'))
    assert.equal(w.focused, true)
  })
})

test('openProjectWindow: a second click focuses the open window WITHOUT reloading it', () => {
  // The trap: window.open(url, name) re-navigates an existing window, which would kill its
  // live browser stream and the turn it is showing.
  const name = popoutWindowName('cardloop')
  const live = popoutUrl('cardloop', 'board') + '#live'
  const wins: Record<string, FakeWin> = {
    [name]: { location: { href: `http://host${live}` }, focused: false, focus() { this.focused = true } },
  }
  withFakeWindow(wins, () => {
    assert.equal(openProjectWindow('cardloop', 'browser'), true)
    assert.equal(wins[name].location.href, `http://host${live}`, 'existing window must not be navigated')
    assert.equal(wins[name].focused, true)
  })
})

test('openProjectWindow: a blocked popup reports false', () => {
  withFakeWindow({}, () => {
    const g = globalThis as unknown as { window: { open: () => null } }
    g.window.open = () => null
    assert.equal(openProjectWindow('x'), false)
  })
})

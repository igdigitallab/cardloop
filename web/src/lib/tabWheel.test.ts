// Mouse wheel over the tab strip: which events move the strip, and by how much.
//
//   cd web
//   npx esbuild src/lib/tabWheel.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/tab-wheel-test/tabWheel.test.cjs --log-level=warning
//   node --test /tmp/tab-wheel-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { wheelScrollDelta, type WheelLike } from './tabWheel'

const ev = (o: Partial<WheelLike>): WheelLike => ({ deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, ...o })

test('a plain vertical wheel scrolls the strip by the same pixels', () => {
  assert.equal(wheelScrollDelta(ev({ deltaY: 100 }), 800), 100)
  assert.equal(wheelScrollDelta(ev({ deltaY: -100 }), 800), -100)
})

test('Ctrl+wheel is the browser zoom, never ours', () => {
  assert.equal(wheelScrollDelta(ev({ deltaY: 100, ctrlKey: true }), 800), null)
})

test('a trackpad swipe follows the dominant axis', () => {
  assert.equal(wheelScrollDelta(ev({ deltaX: 60, deltaY: 5 }), 800), 60)
  assert.equal(wheelScrollDelta(ev({ deltaX: 5, deltaY: -60 }), 800), -60)
})

test('Firefox line mode is scaled to pixels, page mode to the strip width', () => {
  assert.equal(wheelScrollDelta(ev({ deltaY: 3, deltaMode: 1 }), 800), 120)
  assert.equal(wheelScrollDelta(ev({ deltaY: 1, deltaMode: 2 }), 800), 800)
})

test('a zero-delta event is not handled', () => {
  assert.equal(wheelScrollDelta(ev({}), 800), null)
})

// Unit tests for the back-gesture layer stack (web/src/lib/backStack.ts).
//
//   cd web
//   npx esbuild src/lib/backStack.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/backstack-test/backStack.test.cjs --log-level=warning
//   node --test /tmp/backstack-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BackEnv, createBackStack } from './backStack'

/**
 * A tiny browser with the two properties that matter, both measured in headless Chromium:
 * history.back() runs LATER, and its target is fixed when it is CALLED — a pushState made
 * before it runs does not move it, so `back(); pushState()` in one task ends on the entry
 * BELOW the one just pushed. Entries are numbered; `at()` is the current one.
 */
function fakeWindow() {
  let length = 1
  let index = 0
  const queue: number[] = []
  const listeners = new Set<() => void>()
  const win: BackEnv & { entries(): number; at(): number; flush(): void; pressBack(): void } = {
    history: {
      pushState() { index++; length = index + 1 },   // drops any forward entries, like a browser
      back() { queue.push((queue.length ? queue[queue.length - 1] : index) - 1) },
    },
    addEventListener(_t, fn) { listeners.add(fn) },
    entries: () => length,
    at: () => index,
    flush() {
      while (queue.length) {
        const target = queue.shift() as number
        if (target < 0) continue                     // back() at the start of history is a no-op
        index = target
        for (const l of Array.from(listeners)) l()
      }
    },
    pressBack() { win.history.back(); win.flush() },
  }
  return win
}

test('a real Back closes only the top layer', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  const closed: string[] = []
  let closeBottom = () => {}
  let closeTop = () => {}
  closeBottom = s.push(() => { closed.push('bottom'); closeBottom() })
  closeTop = s.push(() => { closed.push('top'); closeTop() })
  win.pressBack()
  assert.deepEqual(closed, ['top'])
  assert.equal(s.depth(), 1)
  win.pressBack()
  assert.deepEqual(closed, ['top', 'bottom'])
  assert.equal(win.at(), 0) // every entry we pushed was given back
})

test('closing a layer from the UI does not dismiss the layer beneath it (the tab-flip bug)', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  let bottomDismissed = false
  s.push(() => { bottomDismissed = true })
  const closeModal = s.push(() => {})
  closeModal() // ✕ / Cancel / Escape
  win.flush()  // the browser delivers the popstate of that history.back()
  assert.equal(bottomDismissed, false)
  assert.equal(s.depth(), 1)
  assert.equal(win.at(), 1) // the modal's entry was consumed; the bottom layer's remains
})

test('after a UI close, the next real Back still reaches the layer beneath', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  let bottomDismissed = false
  let closeBottom = () => {}
  closeBottom = s.push(() => { bottomDismissed = true; closeBottom() })
  s.push(() => {})() // open and close a modal
  win.flush()
  win.pressBack()
  assert.equal(bottomDismissed, true)
})

test('closing after a Back does not pop history a second time', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  let close = () => {}
  close = s.push(() => close())
  win.pressBack()
  assert.equal(win.at(), 0)
  assert.equal(s.depth(), 0)
})

test('closing the LAST layer must not swallow the next real Back (async popstate)', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  s.push(() => {})()   // open and close; the popstate of our own back() is still in flight
  win.flush()          // ...and lands now, with no layer open
  let dismissed = false
  let close = () => {}
  close = s.push(() => { dismissed = true; close() })
  win.pressBack()
  assert.equal(dismissed, true)
})

test('an in-flight own popstate is not mistaken for a real Back by the next layer', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  s.push(() => {})()   // popstate still pending here
  let dismissed = false
  s.push(() => { dismissed = true })
  win.flush()          // the stale one arrives while the new layer is open
  assert.equal(dismissed, false)
})

test('closing one layer and opening another in the SAME tick leaves history and stack in step', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  s.push(() => {})()     // a layer open, then...
  const closeA = s.push(() => {})
  // ...one closes and another opens before the browser has run the back() (project switch, a
  // modal replacing a modal, a drawer closing as a dialog opens).
  closeA()
  let dismissedB = false
  let closeB = () => {}
  closeB = s.push(() => { dismissedB = true; closeB() })
  win.flush()
  assert.equal(win.at(), s.depth())              // one entry per open layer, no more, no fewer
  win.pressBack()                                // a real Back now reaches B, not the app
  assert.equal(dismissedB, true)
  assert.equal(win.at(), 0)
})

test('a layer opened and closed while a pop is in flight never pushes an entry', () => {
  const win = fakeWindow()
  const s = createBackStack(win)
  const closeA = s.push(() => {})
  closeA()                                       // pop in flight
  s.push(() => {})()                             // opened and closed before it lands
  win.flush()
  assert.equal(win.at(), 0)
  assert.equal(s.depth(), 0)
})

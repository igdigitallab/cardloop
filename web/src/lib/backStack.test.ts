// Unit tests for the back-gesture layer stack (web/src/lib/backStack.ts).
//
//   cd web
//   npx esbuild src/lib/backStack.test.ts --bundle --platform=node --format=cjs \
//     --outfile=/tmp/backstack-test/backStack.test.cjs --log-level=warning
//   node --test /tmp/backstack-test/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BackEnv, createBackStack } from './backStack'

/** A tiny browser: pushState grows the entry list; back() shrinks it and fires popstate LATER
 *  (like the real one — history.back() is asynchronous). */
function fakeWindow() {
  let entries = 1
  let pending = 0
  const listeners = new Set<() => void>()
  const win: BackEnv & { entries(): number; flush(): void; pressBack(): void } = {
    history: {
      pushState() { entries++ },
      back() { entries--; pending++ },
    },
    addEventListener(_t, fn) { listeners.add(fn) },
    entries: () => entries,
    flush() {
      while (pending > 0) { pending--; for (const l of Array.from(listeners)) l() }
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
  assert.equal(win.entries(), 1) // the history stack did not grow or leak
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
  assert.equal(win.entries(), 2) // the modal's entry was consumed; the bottom layer's remains
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
  assert.equal(win.entries(), 1)
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

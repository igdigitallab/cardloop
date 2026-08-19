// spec-082 D — unit tests for the QuickKeys sticky-Ctrl state machine and the
// emitted byte sequences (web/src/tabs/quickKeys.ts).
//
// This repo has no frontend test runner installed yet (no vitest/jest in
// web/package.json — verified before adding this file) and workstream D is
// not the place to bolt one on ("no new dependency", "no inventing a
// harness"). So this file is written against Node's own built-in test
// runner (`node:test` / `node:assert`, stable since Node 18), which needs no
// install at all. It exercises quickKeys.ts directly with zero DOM/React —
// the module under test has no such dependency either.
//
// Run it (after `tsc` is available — see web/README or CONTRIBUTING for the
// venv/node_modules setup already required to build this project):
//
//   cd web
//   npx tsc src/tabs/quickKeys.ts src/tabs/quickKeys.test.ts \
//     --outDir /tmp/quickkeys-test --module commonjs --target es2020 \
//     --moduleResolution node --esModuleInterop --skipLibCheck
//   node --test /tmp/quickkeys-test
//
// If/when a real frontend harness (vitest) is added to this project, this
// file's `describe`/`test`/`assert` calls are close enough to drop-in swap.

import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import {
  QUICK_KEY_ORDER,
  QUICK_KEY_SEQUENCES,
  ctrlByteFor,
  applyCtrlLatch,
} from './quickKeys'

describe('ctrlByteFor', () => {
  test('maps lowercase letters to their control byte', () => {
    assert.equal(ctrlByteFor('c'), '\x03') // Ctrl+C — interrupt
    assert.equal(ctrlByteFor('d'), '\x04') // Ctrl+D — EOF
    assert.equal(ctrlByteFor('a'), '\x01')
    assert.equal(ctrlByteFor('z'), '\x1a')
  })

  test('maps uppercase letters the same as lowercase', () => {
    assert.equal(ctrlByteFor('C'), ctrlByteFor('c'))
    assert.equal(ctrlByteFor('Z'), ctrlByteFor('z'))
  })

  test('returns null for non-letters and multi-char input', () => {
    assert.equal(ctrlByteFor('1'), null)
    assert.equal(ctrlByteFor(' '), null)
    assert.equal(ctrlByteFor('\r'), null)
    assert.equal(ctrlByteFor(''), null)
    assert.equal(ctrlByteFor('ab'), null)
    assert.equal(ctrlByteFor('\x1b[A'), null) // an arrow-key escape sequence
  })
})

describe('applyCtrlLatch', () => {
  test('passes data through unchanged when not armed', () => {
    const r = applyCtrlLatch(false, 'c')
    assert.equal(r.output, 'c')
    assert.equal(r.nextArmed, false)
  })

  test('transforms the next letter into its control byte when armed', () => {
    const r = applyCtrlLatch(true, 'c')
    assert.equal(r.output, '\x03')
    assert.equal(r.nextArmed, false)
  })

  test('de-latches after one key even when the key has no control byte', () => {
    const r = applyCtrlLatch(true, '\r')
    assert.equal(r.output, '\r') // passed through untouched
    assert.equal(r.nextArmed, false) // but the latch still releases
  })

  test('de-latches on multi-char input (paste / physical arrow key)', () => {
    const r = applyCtrlLatch(true, 'hello')
    assert.equal(r.output, 'hello')
    assert.equal(r.nextArmed, false)
  })

  test('a second armed keystroke never accumulates state — each call is independent', () => {
    const first = applyCtrlLatch(true, 'a')
    assert.equal(first.output, '\x01')
    // Using nextArmed from `first` (false) for a second, unrelated keystroke:
    const second = applyCtrlLatch(first.nextArmed, 'b')
    assert.equal(second.output, 'b') // NOT transformed — latch already released
  })
})

describe('QUICK_KEY_SEQUENCES', () => {
  test('every non-ctrl key in QUICK_KEY_ORDER has a byte sequence', () => {
    for (const id of QUICK_KEY_ORDER) {
      if (id === 'ctrl') continue
      assert.ok(
        typeof QUICK_KEY_SEQUENCES[id] === 'string' && QUICK_KEY_SEQUENCES[id]!.length > 0,
        `missing sequence for ${id}`,
      )
    }
  })

  test('ctrl itself has no sequence — it is a modifier, not a byte-emitting key', () => {
    assert.equal(QUICK_KEY_SEQUENCES.ctrl, undefined)
  })

  test('known sequences match real terminal escape codes', () => {
    assert.equal(QUICK_KEY_SEQUENCES.esc, '\x1b')
    assert.equal(QUICK_KEY_SEQUENCES.tab, '\t')
    assert.equal(QUICK_KEY_SEQUENCES.up, '\x1b[A')
    assert.equal(QUICK_KEY_SEQUENCES.down, '\x1b[B')
    assert.equal(QUICK_KEY_SEQUENCES.left, '\x1b[D')
    assert.equal(QUICK_KEY_SEQUENCES.right, '\x1b[C')
    assert.equal(QUICK_KEY_SEQUENCES['ctrl-c'], '\x03')
    assert.equal(QUICK_KEY_SEQUENCES.pipe, '|')
    assert.equal(QUICK_KEY_SEQUENCES.tilde, '~')
    assert.equal(QUICK_KEY_SEQUENCES.slash, '/')
    assert.equal(QUICK_KEY_SEQUENCES.dash, '-')
  })
})

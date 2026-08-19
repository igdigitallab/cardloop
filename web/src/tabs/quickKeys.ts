// spec-082 D — QuickKeys: mobile key bar for the PTY terminal.
//
// Pure, framework-free logic (no React, no DOM, no xterm import) so it can be
// unit-tested in isolation and reused verbatim by TerminalTab.tsx. Everything
// here operates on plain strings — the exact bytes written to the terminal's
// WebSocket input path, nothing else.

export type QuickKeyId =
  | 'esc'
  | 'tab'
  | 'ctrl'
  | 'up'
  | 'down'
  | 'left'
  | 'right'
  | 'ctrl-c'
  | 'pipe'
  | 'tilde'
  | 'slash'
  | 'dash'

// Left-to-right order the bar renders in.
export const QUICK_KEY_ORDER: QuickKeyId[] = [
  'esc',
  'tab',
  'ctrl',
  'up',
  'down',
  'left',
  'right',
  'ctrl-c',
  'pipe',
  'tilde',
  'slash',
  'dash',
]

// Bytes each key sends verbatim over the terminal's WebSocket input path.
// 'ctrl' is intentionally absent — it is a sticky modifier (see applyCtrlLatch
// below), not a byte-emitting key on its own.
export const QUICK_KEY_SEQUENCES: Partial<Record<QuickKeyId, string>> = {
  esc: '\x1b',
  tab: '\t',
  up: '\x1b[A',
  down: '\x1b[B',
  left: '\x1b[D',
  right: '\x1b[C',
  'ctrl-c': '\x03',
  pipe: '|',
  tilde: '~',
  slash: '/',
  dash: '-',
}

/**
 * Control-code byte for a single character (Ctrl+A..Ctrl+Z → 0x01..0x1A),
 * mirroring how a real terminal driver maps a held Ctrl key. Returns null
 * for anything that isn't a single ASCII letter — multi-char input (e.g. a
 * pasted string or an already-escaped sequence like an arrow key) has no
 * single well-defined "control" byte, so it is left untouched by the caller.
 */
export function ctrlByteFor(ch: string): string | null {
  if (ch.length !== 1) return null
  const code = ch.toUpperCase().charCodeAt(0)
  if (code >= 65 && code <= 90) return String.fromCharCode(code - 64)
  return null
}

export interface CtrlLatchResult {
  /** What to actually send to the terminal instead of the raw input. */
  output: string
  /** Armed state to carry forward after this keystroke. */
  nextArmed: boolean
}

/**
 * Sticky-Ctrl reducer: tap Ctrl, then the next keystroke is transformed into
 * its control code and the latch releases — "de-latches after one key" even
 * when that key isn't a letter (no way to get permanently stuck armed).
 */
export function applyCtrlLatch(armed: boolean, data: string): CtrlLatchResult {
  if (!armed) return { output: data, nextArmed: false }
  const ctrl = ctrlByteFor(data)
  return { output: ctrl ?? data, nextArmed: false }
}

/**
 * chime — a short "your turn" sound played when an agent run finishes.
 *
 * Synthesised with the Web Audio API instead of shipping an audio file: a pair
 * of decaying notes (sine fundamental + quieter triangle partial so it still
 * carries on a phone speaker), no asset to fetch, no binary in the repo.
 *
 * Autoplay policy: a browser only lets an AudioContext produce sound after a
 * user gesture, and iOS re-suspends the context after the app is backgrounded.
 * `primeAudio()` (called once at app mount) therefore installs PERSISTENT
 * gesture listeners that resume the context whenever it is found suspended —
 * not a one-shot unlock, which would go stale on the first background trip.
 *
 * Opt-out lives in localStorage['cops.sound'] (default ON) — read at play time
 * so the settings toggle takes effect without re-wiring the SSE handler.
 *
 * Debugging: `window.__cardloopChime()` plays it on demand from the console.
 */
import { readLSBool, writeLSBool } from './storage'

const LS_KEY = 'cops.sound'

/** Two run_end signals within this window produce one chime, not two.
 *  (The SSE bus and the chat stream both report the same turn ending.) */
const THROTTLE_MS = 1500

let ctx: AudioContext | null = null
let primed = false
let lastPlayedAt = 0

export function isSoundEnabled(): boolean {
  return readLSBool(LS_KEY, true)
}

export function setSoundEnabled(v: boolean): void {
  writeLSBool(LS_KEY, v)
}

function getCtx(): AudioContext | null {
  if (ctx) return ctx
  const Ctor: typeof AudioContext | undefined =
    window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctor) return null
  try {
    ctx = new Ctor()
  } catch {
    return null
  }
  return ctx
}

/** Create + resume the AudioContext on user gestures (autoplay unlock). */
export function primeAudio(): void {
  if (primed) return
  primed = true
  const unlock = () => {
    const c = getCtx()
    if (c && c.state !== 'running') c.resume().catch(() => { /* ignore */ })
  }
  // Persistent, passive listeners: cheap, and they re-unlock after iOS suspends
  // the context on a background trip.
  const opts = { capture: true, passive: true } as const
  window.addEventListener('pointerdown', unlock, opts)
  window.addEventListener('keydown', unlock, opts)
  window.addEventListener('touchstart', unlock, opts)
  ;(window as unknown as { __cardloopChime?: (k?: 'ok' | 'fail') => void }).__cardloopChime =
    (k?: 'ok' | 'fail') => playChime(k ?? 'ok', { force: true })
}

/** One decaying note: sine fundamental + a quieter triangle for presence. */
function note(c: AudioContext, freq: number, startAt: number, dur: number, peak: number): void {
  for (const [type, level] of [['sine', peak], ['triangle', peak * 0.25]] as const) {
    const osc = c.createOscillator()
    const gain = c.createGain()
    osc.type = type
    osc.frequency.setValueAtTime(freq, startAt)
    // exponentialRamp cannot touch 0 — ramp between tiny non-zero values instead.
    gain.gain.setValueAtTime(0.0001, startAt)
    gain.gain.exponentialRampToValueAtTime(level, startAt + 0.008)
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + dur)
    osc.connect(gain).connect(c.destination)
    osc.start(startAt)
    osc.stop(startAt + dur + 0.02)
  }
}

/** Schedule the two notes on a context that is known to be running. */
function schedule(c: AudioContext, kind: 'ok' | 'fail'): void {
  try {
    const t0 = c.currentTime + 0.02
    if (kind === 'ok') {
      // Ascending A5 → E6 — reads as "message arrived".
      note(c, 880, t0, 0.18, 0.42)
      note(c, 1318.5, t0 + 0.1, 0.42, 0.38)
    } else {
      // Same shape, descending — a softer "finished with an error".
      note(c, 659.3, t0, 0.18, 0.4)
      note(c, 440, t0 + 0.12, 0.5, 0.36)
    }
  } catch {
    /* audio is a nicety — never break the caller */
  }
}

/**
 * Play the completion chime. Silent no-op when the operator opted out, the
 * browser has no Web Audio, or the context is still locked by autoplay policy.
 * `force` plays regardless of the opt-out and the throttle (settings preview).
 */
export function playChime(kind: 'ok' | 'fail' = 'ok', opts?: { force?: boolean }): void {
  const force = opts?.force === true
  if (!force && !isSoundEnabled()) return
  const now = Date.now()
  if (!force && now - lastPlayedAt < THROTTLE_MS) return
  const c = getCtx()
  if (!c) return
  lastPlayedAt = now
  if (c.state === 'running') {
    schedule(c, kind)
    return
  }
  // Suspended: resume FIRST, then schedule — scheduling against a frozen clock
  // can otherwise place the notes in the past once the context starts running.
  c.resume().then(() => schedule(c, kind)).catch(() => { /* still locked — stay silent */ })
}

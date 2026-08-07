/**
 * chime — a short "your turn" sound played when an agent run finishes.
 *
 * Synthesised with the Web Audio API instead of shipping an audio file: two
 * decaying sine tones, no asset to fetch, no binary in the repo.
 *
 * Autoplay policy: a browser only lets an AudioContext produce sound after a
 * user gesture. `primeAudio()` (called once at app mount) attaches one-shot
 * gesture listeners that create and resume the context, so by the time the
 * operator has sent a message the context is already unlocked.
 *
 * Opt-out lives in localStorage['cops.sound'] (default ON) — read at play time
 * so the settings toggle takes effect without re-wiring the SSE handler.
 */
import { readLSBool, writeLSBool } from './storage'

const LS_KEY = 'cops.sound'

/** Two run_end events within this window produce one chime, not two. */
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

/** Create + resume the AudioContext on the first user gesture (iOS/Chrome autoplay unlock). */
export function primeAudio(): void {
  if (primed) return
  primed = true
  const unlock = () => {
    const c = getCtx()
    if (c && c.state === 'suspended') c.resume().catch(() => { /* ignore */ })
  }
  const opts = { once: true, capture: true, passive: true } as const
  window.addEventListener('pointerdown', unlock, opts)
  window.addEventListener('keydown', unlock, opts)
  window.addEventListener('touchstart', unlock, opts)
}

/** One decaying sine note. */
function note(c: AudioContext, freq: number, startAt: number, dur: number, peak: number): void {
  const osc = c.createOscillator()
  const gain = c.createGain()
  osc.type = 'sine'
  osc.frequency.setValueAtTime(freq, startAt)
  // exponentialRamp cannot touch 0 — ramp between tiny non-zero values instead.
  gain.gain.setValueAtTime(0.0001, startAt)
  gain.gain.exponentialRampToValueAtTime(peak, startAt + 0.012)
  gain.gain.exponentialRampToValueAtTime(0.0001, startAt + dur)
  osc.connect(gain).connect(c.destination)
  osc.start(startAt)
  osc.stop(startAt + dur + 0.02)
}

/**
 * Play the completion chime. Silent no-op when the operator opted out, the
 * browser has no Web Audio, or the context never got unlocked.
 * `force` plays regardless of the opt-out and the throttle (settings preview).
 *
 * ok   — ascending A5→E6, "message arrived"
 * fail — descending E5→A4, same shape, minor mood
 */
export function playChime(kind: 'ok' | 'fail' = 'ok', opts?: { force?: boolean }): void {
  const force = opts?.force === true
  if (!force && !isSoundEnabled()) return
  const now = Date.now()
  if (!force && now - lastPlayedAt < THROTTLE_MS) return
  const c = getCtx()
  if (!c) return
  if (c.state === 'suspended') c.resume().catch(() => { /* still locked — stay silent */ })
  try {
    const t0 = c.currentTime + 0.01
    if (kind === 'ok') {
      note(c, 880, t0, 0.16, 0.09)
      note(c, 1318.5, t0 + 0.1, 0.28, 0.075)
    } else {
      note(c, 659.3, t0, 0.16, 0.085)
      note(c, 440, t0 + 0.11, 0.34, 0.07)
    }
    lastPlayedAt = now
  } catch {
    /* audio is a nicety — never break the SSE handler */
  }
}

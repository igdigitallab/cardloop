/**
 * chime — a short "your turn" sound played when an agent run finishes.
 *
 * Synthesised with the Web Audio API instead of shipping an audio file: an
 * ascending three-note arpeggio, each note a sine fundamental plus a triangle
 * partial, run through a compressor so it lands LOUD without clipping.
 *
 * Loudness note: perceived volume follows RMS, not peak. A bare sine that
 * decays exponentially measures a healthy peak while carrying almost no energy
 * — the first version of this file peaked at -6 dBFS yet was barely audible.
 * Hence the sustain plateau in the envelope, the harmonics, and the makeup gain
 * after compression. `tools/measure_chime.py` renders this exact code in a real
 * browser and prints peak/RMS — re-run it after changing any constant here.
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

/** Makeup gain after the compressor — tuned so the render peaks just under 1.0. */
const MAKEUP = 1.2

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
  const w = window as unknown as {
    __cardloopChime?: (k?: 'ok' | 'fail') => void
    __cardloopChimeRender?: (c: BaseAudioContext, k?: 'ok' | 'fail') => void
  }
  w.__cardloopChime = (k?: 'ok' | 'fail') => playChime(k ?? 'ok', { force: true })
  // Offline-render hook: lets tools/measure_chime.py measure THIS code, not a copy.
  w.__cardloopChimeRender = (c: BaseAudioContext, k?: 'ok' | 'fail') => schedule(c, k ?? 'ok', 0.02)
}

/** Master chain: everything sums into a compressor, then a makeup gain. */
function bus(c: BaseAudioContext): AudioNode {
  const comp = c.createDynamicsCompressor()
  comp.threshold.value = -18
  comp.knee.value = 12
  comp.ratio.value = 10
  comp.attack.value = 0.003
  comp.release.value = 0.25
  const makeup = c.createGain()
  makeup.gain.value = MAKEUP
  comp.connect(makeup).connect(c.destination)
  return comp
}

/**
 * One note. The envelope is attack → sustain plateau → body decay → tail:
 * the plateau is what makes it read as a real chime rather than a faint tick.
 */
function note(
  c: BaseAudioContext, out: AudioNode,
  freq: number, at: number, dur: number, level: number,
): void {
  for (const [type, mul] of [['sine', 1], ['triangle', 0.45]] as const) {
    const osc = c.createOscillator()
    const gain = c.createGain()
    const peak = level * mul
    osc.type = type
    osc.frequency.setValueAtTime(freq, at)
    // exponentialRamp cannot touch 0 — ramp between tiny non-zero values instead.
    gain.gain.setValueAtTime(0.0002, at)
    gain.gain.exponentialRampToValueAtTime(peak, at + 0.006)
    gain.gain.setValueAtTime(peak, at + 0.05)
    gain.gain.exponentialRampToValueAtTime(peak * 0.6, at + dur * 0.4)
    gain.gain.exponentialRampToValueAtTime(0.0006, at + dur)
    osc.connect(gain).connect(out)
    osc.start(at)
    osc.stop(at + dur + 0.02)
  }
}

/** Schedule the motif on any context (live or offline). Exported for measurement. */
function schedule(c: BaseAudioContext, kind: 'ok' | 'fail', at?: number): void {
  try {
    const out = bus(c)
    const t0 = at ?? c.currentTime + 0.02
    if (kind === 'ok') {
      // Ascending C6–E6–G6 major triad, last note held — bright and unmistakable.
      note(c, out, 1046.5, t0, 0.34, 0.5)
      note(c, out, 1318.5, t0 + 0.11, 0.36, 0.5)
      note(c, out, 1568.0, t0 + 0.22, 0.85, 0.55)
      note(c, out, 2093.0, t0 + 0.22, 0.5, 0.16)  // octave sparkle on top
    } else {
      // Same weight, descending E5–C5–A4 — reads as "finished, but badly".
      note(c, out, 659.3, t0, 0.34, 0.5)
      note(c, out, 523.3, t0 + 0.12, 0.36, 0.5)
      note(c, out, 440.0, t0 + 0.24, 0.9, 0.55)
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

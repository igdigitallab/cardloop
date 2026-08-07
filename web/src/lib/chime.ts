/**
 * chime — the "your turn" sound played when an agent run finishes.
 *
 * Synthesised with the Web Audio API instead of shipping audio files: ten
 * presets, each built from oscillators (plus a noise buffer for the percussive
 * ones), so the whole sound library costs a few hundred bytes of code and no
 * network requests.
 *
 * Loudness note: perceived volume follows RMS, not peak. A bare sine that
 * decays exponentially measures a healthy peak while carrying almost no energy
 * — the first version of this file peaked at -6 dBFS yet was barely audible.
 * Hence the sustain plateau in the envelopes, the harmonics, and the compressor
 * with makeup gain. Every preset carries a `gain` trim that normalises it to
 * roughly the same loudness; `tools/measure_chime.py --all` renders the SHIPPED
 * code in a real browser and prints peak/RMS per preset. Re-run it after
 * touching any constant here — do not tune by ear alone.
 *
 * Autoplay policy: a browser only lets an AudioContext produce sound after a
 * user gesture, and iOS re-suspends the context after the app is backgrounded.
 * `primeAudio()` (called once at app mount) therefore installs PERSISTENT
 * gesture listeners that resume the context whenever it is found suspended —
 * not a one-shot unlock, which would go stale on the first background trip.
 *
 * Settings live in localStorage (per device, deliberately):
 *   cops.sound       — on/off       (default on)
 *   cops.soundName   — preset id    (default 'chime')
 *   cops.soundVolume — 10..100      (default 100)
 *
 * Debugging: `window.__cardloopChime('ok', 'bell')` plays a preset on demand.
 */
import { readLSBool, writeLSBool, readLSString, writeLSString, readLSNumber } from './storage'

const LS_ON = 'cops.sound'
const LS_NAME = 'cops.soundName'
const LS_VOLUME = 'cops.soundVolume'

/** Two run_end signals within this window produce one chime, not two.
 *  (The SSE bus and the chat stream both report the same turn ending.) */
const THROTTLE_MS = 1500

/** Post-compressor gain, tuned so a preset at trim 1.0 peaks just under 1.0. */
const MAKEUP = 1.7

export const DEFAULT_PRESET = 'chime'

let ctx: AudioContext | null = null
let noiseBuf: AudioBuffer | null = null
let primed = false
let lastPlayedAt = 0

// ── Settings ────────────────────────────────────────────────────────────────

export function isSoundEnabled(): boolean {
  return readLSBool(LS_ON, true)
}

export function setSoundEnabled(v: boolean): void {
  writeLSBool(LS_ON, v)
}

export function getPresetId(): string {
  const id = readLSString(LS_NAME)
  return id && PRESETS.some(p => p.id === id) ? id : DEFAULT_PRESET
}

export function setPresetId(id: string): void {
  writeLSString(LS_NAME, id)
}

/** Operator volume as a 0..1 multiplier (stored as 10..100). */
export function getVolume(): number {
  const pct = readLSNumber(LS_VOLUME, 100)
  return Math.min(100, Math.max(10, pct)) / 100
}

export function getVolumePct(): number {
  return Math.round(getVolume() * 100)
}

export function setVolumePct(pct: number): void {
  try { localStorage.setItem(LS_VOLUME, String(Math.min(100, Math.max(10, Math.round(pct))))) } catch { /* ignore */ }
}

// ── Context + autoplay unlock ───────────────────────────────────────────────

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
    __cardloopChime?: (k?: 'ok' | 'fail', preset?: string) => void
    __cardloopChimeRender?: (c: BaseAudioContext, k?: 'ok' | 'fail', preset?: string) => void
    __cardloopChimePresets?: string[]
  }
  w.__cardloopChime = (k, preset) => playChime(k ?? 'ok', { force: true, preset })
  // Offline-render hooks: let tools/measure_chime.py measure THIS code, not a copy.
  // Rendered at full volume so the measurement is independent of the operator's slider.
  w.__cardloopChimeRender = (c, k, preset) => schedule(c, k ?? 'ok', preset ?? getPresetId(), 0.02, 1)
  w.__cardloopChimePresets = PRESETS.map(p => p.id)
}

// ── Synthesis helpers ───────────────────────────────────────────────────────

/** Short white-noise buffer, built once, for the percussive presets. */
function getNoise(c: BaseAudioContext): AudioBuffer {
  if (noiseBuf && noiseBuf.sampleRate === c.sampleRate) return noiseBuf
  const buf = c.createBuffer(1, Math.floor(c.sampleRate * 0.5), c.sampleRate)
  const d = buf.getChannelData(0)
  // Deterministic pseudo-noise: no Math.random, so renders are reproducible.
  let seed = 22222
  for (let i = 0; i < d.length; i++) {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff
    d[i] = (seed / 0x3fffffff) - 1
  }
  noiseBuf = buf
  return buf
}

interface ToneOpts {
  type?: OscillatorType
  /** Sustain plateau length before the body decay — what makes a sound "present". */
  hold?: number
  /** Level at 40% of the duration, relative to peak (1 = flat, 0.2 = plucky). */
  curve?: number
  attack?: number
  detune?: number
}

/** One oscillator note with an attack → plateau → body → tail envelope. */
function tone(
  c: BaseAudioContext, out: AudioNode,
  freq: number, at: number, dur: number, level: number, o: ToneOpts = {},
): void {
  const osc = c.createOscillator()
  const gain = c.createGain()
  osc.type = o.type ?? 'sine'
  osc.frequency.setValueAtTime(freq, at)
  if (o.detune) osc.detune.setValueAtTime(o.detune, at)
  const attack = o.attack ?? 0.006
  const hold = o.hold ?? 0.05
  // exponentialRamp cannot touch 0 — ramp between tiny non-zero values instead.
  gain.gain.setValueAtTime(0.0002, at)
  gain.gain.exponentialRampToValueAtTime(level, at + attack)
  gain.gain.setValueAtTime(level, at + attack + hold)
  gain.gain.exponentialRampToValueAtTime(level * (o.curve ?? 0.6), at + dur * 0.4)
  gain.gain.exponentialRampToValueAtTime(0.0006, at + dur)
  osc.connect(gain).connect(out)
  osc.start(at)
  osc.stop(at + dur + 0.02)
}

/** A band-passed noise burst — the knock / click presets. */
function hit(
  c: BaseAudioContext, out: AudioNode,
  center: number, at: number, dur: number, level: number, q = 6,
): void {
  const src = c.createBufferSource()
  src.buffer = getNoise(c)
  const bp = c.createBiquadFilter()
  bp.type = 'bandpass'
  bp.frequency.value = center
  bp.Q.value = q
  const gain = c.createGain()
  gain.gain.setValueAtTime(level, at)
  gain.gain.exponentialRampToValueAtTime(0.0006, at + dur)
  src.connect(bp).connect(gain).connect(out)
  src.start(at)
  src.stop(at + dur + 0.02)
}

/** FM voice — inharmonic partials give a real bell/metal character. */
function fm(
  c: BaseAudioContext, out: AudioNode,
  carrier: number, ratio: number, index: number,
  at: number, dur: number, level: number,
): void {
  const car = c.createOscillator()
  const mod = c.createOscillator()
  const modGain = c.createGain()
  const gain = c.createGain()
  car.frequency.setValueAtTime(carrier, at)
  mod.frequency.setValueAtTime(carrier * ratio, at)
  modGain.gain.setValueAtTime(carrier * index, at)
  modGain.gain.exponentialRampToValueAtTime(carrier * index * 0.05, at + dur * 0.6)
  gain.gain.setValueAtTime(0.0002, at)
  gain.gain.exponentialRampToValueAtTime(level, at + 0.005)
  gain.gain.exponentialRampToValueAtTime(level * 0.35, at + dur * 0.35)
  gain.gain.exponentialRampToValueAtTime(0.0006, at + dur)
  mod.connect(modGain).connect(car.frequency)
  car.connect(gain).connect(out)
  mod.start(at); car.start(at)
  mod.stop(at + dur + 0.02); car.stop(at + dur + 0.02)
}

/** Play a note sequence; the 'fail' variant walks the same pitches backwards. */
function melody(
  c: BaseAudioContext, out: AudioNode, t0: number, kind: 'ok' | 'fail',
  notes: Array<{ f: number; at: number; dur: number; lvl: number }>,
  o: ToneOpts = {}, partial?: { mul: number; lvl: number },
): void {
  const freqs = notes.map(n => n.f)
  if (kind === 'fail') freqs.reverse()
  notes.forEach((n, i) => {
    tone(c, out, freqs[i], t0 + n.at, n.dur, n.lvl, o)
    if (partial) tone(c, out, freqs[i] * partial.mul, t0 + n.at, n.dur * 0.7, n.lvl * partial.lvl, o)
  })
}

// ── Preset catalog ──────────────────────────────────────────────────────────

export interface ChimePreset {
  id: string
  label: string
  /** Loudness trim, set from tools/measure_chime.py --all so presets match. */
  gain: number
  play: (c: BaseAudioContext, out: AudioNode, t0: number, kind: 'ok' | 'fail') => void
}

export const PRESETS: ChimePreset[] = [
  {
    id: 'chime', label: 'Chime — bright triad', gain: 1.11,
    play: (c, out, t0, kind) => melody(c, out, t0, kind, [
      { f: 1046.5, at: 0, dur: 0.34, lvl: 0.5 },
      { f: 1318.5, at: 0.11, dur: 0.36, lvl: 0.5 },
      { f: 1568.0, at: 0.22, dur: 0.85, lvl: 0.55 },
    ], { type: 'sine' }, { mul: 2, lvl: 0.3 }),
  },
  {
    id: 'ping', label: 'Ping — single bright note', gain: 1.23,
    play: (c, out, t0) => {
      tone(c, out, 1567.98, t0, 1.1, 0.6, { hold: 0.08, curve: 0.5 })
      tone(c, out, 3135.96, t0, 0.5, 0.14, { curve: 0.3 })
    },
  },
  {
    id: 'bell', label: 'Bell — metallic, long tail', gain: 1.1,
    play: (c, out, t0, kind) => {
      fm(c, out, kind === 'ok' ? 987.77 : 659.25, 1.41, 3.2, t0, 1.5, 0.55)
      fm(c, out, kind === 'ok' ? 1975.5 : 1318.5, 1.41, 1.8, t0 + 0.01, 0.9, 0.2)
    },
  },
  {
    id: 'marimba', label: 'Marimba — warm wooden', gain: 1.44,
    play: (c, out, t0, kind) => melody(c, out, t0, kind, [
      { f: 1174.66, at: 0, dur: 0.3, lvl: 0.6 },
      { f: 1567.98, at: 0.13, dur: 0.55, lvl: 0.6 },
    ], { type: 'sine', hold: 0.01, curve: 0.28 }, { mul: 4, lvl: 0.12 }),
  },
  {
    id: 'doorbell', label: 'Doorbell — two-tone ding-dong', gain: 1.3,
    play: (c, out, t0, kind) => melody(c, out, t0, kind, [
      { f: 659.25, at: 0, dur: 0.5, lvl: 0.55 },
      { f: 523.25, at: 0.26, dur: 1.1, lvl: 0.55 },
    ], { type: 'triangle', hold: 0.06, curve: 0.55 }, { mul: 2, lvl: 0.25 }),
  },
  {
    id: 'blip', label: 'Blip — short digital', gain: 1.27,
    play: (c, out, t0, kind) => melody(c, out, t0, kind, [
      { f: 880, at: 0, dur: 0.1, lvl: 0.4 },
      { f: 1318.5, at: 0.11, dur: 0.14, lvl: 0.4 },
    ], { type: 'square', hold: 0.03, curve: 0.7, attack: 0.003 }),
  },
  {
    id: 'knock', label: 'Knock — percussive tap', gain: 2.2,
    play: (c, out, t0) => {
      hit(c, out, 320, t0, 0.13, 0.9, 3)
      hit(c, out, 320, t0 + 0.16, 0.16, 0.9, 3)
      tone(c, out, 180, t0, 0.12, 0.3, { hold: 0.01, curve: 0.2 })
      tone(c, out, 180, t0 + 0.16, 0.14, 0.3, { hold: 0.01, curve: 0.2 })
    },
  },
  {
    id: 'arcade', label: 'Arcade — 8-bit run-up', gain: 0.94,
    play: (c, out, t0, kind) => melody(c, out, t0, kind, [
      { f: 783.99, at: 0, dur: 0.09, lvl: 0.34 },
      { f: 1046.5, at: 0.07, dur: 0.09, lvl: 0.34 },
      { f: 1318.5, at: 0.14, dur: 0.09, lvl: 0.34 },
      { f: 1567.98, at: 0.21, dur: 0.42, lvl: 0.38 },
    ], { type: 'square', hold: 0.02, curve: 0.6, attack: 0.003 }),
  },
  {
    id: 'pulse', label: 'Pulse — soft double beat', gain: 1.47,
    play: (c, out, t0) => {
      tone(c, out, 523.25, t0, 0.3, 0.55, { hold: 0.09, curve: 0.7 })
      tone(c, out, 523.25, t0 + 0.3, 0.4, 0.55, { hold: 0.09, curve: 0.7 })
    },
  },
  {
    id: 'alert', label: 'Alert — insistent triple beep', gain: 1.05,
    play: (c, out, t0) => {
      for (let i = 0; i < 3; i++) {
        tone(c, out, 1174.66, t0 + i * 0.19, 0.13, 0.45, { type: 'square', hold: 0.06, curve: 0.9, attack: 0.004 })
        tone(c, out, 2349.32, t0 + i * 0.19, 0.13, 0.1, { type: 'square', hold: 0.06, curve: 0.9, attack: 0.004 })
      }
    },
  },
]

export function presetLabel(id: string): string {
  return PRESETS.find(p => p.id === id)?.label ?? id
}

// ── Playback ────────────────────────────────────────────────────────────────

/** Preset → compressor → makeup×volume → speakers. */
function schedule(c: BaseAudioContext, kind: 'ok' | 'fail', presetId: string, at?: number, volume?: number): void {
  try {
    const preset = PRESETS.find(p => p.id === presetId) ?? PRESETS[0]
    const input = c.createGain()
    input.gain.value = preset.gain
    const comp = c.createDynamicsCompressor()
    comp.threshold.value = -26
    comp.knee.value = 12
    comp.ratio.value = 12
    comp.attack.value = 0.003
    comp.release.value = 0.25
    const makeup = c.createGain()
    makeup.gain.value = MAKEUP * (volume ?? getVolume())
    input.connect(comp).connect(makeup).connect(c.destination)
    preset.play(c, input, at ?? c.currentTime + 0.02, kind)
  } catch {
    /* audio is a nicety — never break the caller */
  }
}

/**
 * Play the completion chime. Silent no-op when the operator opted out, the
 * browser has no Web Audio, or the context is still locked by autoplay policy.
 * `force` plays regardless of the opt-out and the throttle (settings preview);
 * `preset` overrides the stored choice (also for previewing).
 */
export function playChime(
  kind: 'ok' | 'fail' = 'ok',
  opts?: { force?: boolean; preset?: string },
): void {
  const force = opts?.force === true
  if (!force && !isSoundEnabled()) return
  const now = Date.now()
  if (!force && now - lastPlayedAt < THROTTLE_MS) return
  const c = getCtx()
  if (!c) return
  lastPlayedAt = now
  const id = opts?.preset ?? getPresetId()
  if (c.state === 'running') {
    schedule(c, kind, id)
    return
  }
  // Suspended: resume FIRST, then schedule — scheduling against a frozen clock
  // can otherwise place the notes in the past once the context starts running.
  c.resume().then(() => schedule(c, kind, id)).catch(() => { /* still locked */ })
}

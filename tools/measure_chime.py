#!/usr/bin/env python3
"""Measure the completion chime's loudness in a real browser.

Perceived loudness follows RMS, not peak: a decaying sine can show a healthy
peak while being inaudible in practice. This renders the SHIPPED chime code
(via the window.__cardloopChimeRender hook installed by primeAudio) through an
OfflineAudioContext in headless Chromium and prints peak / RMS / dBFS, so a
tweak to the synthesis constants can be checked without guessing.

Usage:  venv/bin/python tools/measure_chime.py [ok|fail] [--all]
        --all measures every preset in the catalog, so their `gain` trims can be
        set to land on the same loudness instead of being guessed.
Requires: a running cockpit (WEB_PORT from .env) and `playwright install chromium`.
"""
import math
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent


def env(name: str, default: str = "") -> str:
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, default)


RENDER = """
async ([kind, preset]) => {
  if (typeof window.__cardloopChimeRender !== 'function') return { error: 'hook missing (stale bundle?)' }
  const oc = new OfflineAudioContext(1, 44100, 44100 * 3)
  window.__cardloopChimeRender(oc, kind, preset)
  const buf = await oc.startRendering()
  const d = buf.getChannelData(0)
  let peak = 0, sum = 0, voiced = 0
  for (let i = 0; i < d.length; i++) {
    const a = Math.abs(d[i])
    if (a > peak) peak = a
    if (a > 0.02) { voiced++; sum += d[i] * d[i] }
  }
  return { peak, rms: voiced ? Math.sqrt(sum / voiced) : 0, seconds: voiced / 44100 }
}
"""


def db(x: float) -> str:
    return "-inf" if x <= 0 else f"{20 * math.log10(x):+.1f} dBFS"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    every = "--all" in sys.argv
    kind = args[0] if args else "ok"
    port, password = env("WEB_PORT", "8787"), env("WEB_PASSWORD")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        field = page.query_selector("input[type=password]")
        if field and password:
            field.fill(password)
            field.press("Enter")
            page.wait_for_timeout(3000)

        presets = page.evaluate("() => window.__cardloopChimePresets || []") if every else [None]
        results = {(pid or "current"): page.evaluate(RENDER, [kind, pid]) for pid in presets}
        browser.close()

    failed = [k for k, v in results.items() if v.get("error")]
    if failed:
        print(f"FAILED: {results[failed[0]]['error']}")
        return 1

    print(f"{'preset':<10} {'peak':>7} {'dBFS':>8} {'rms':>7} {'dBFS':>8} {'len':>6}")
    clipping = False
    for name, r in results.items():
        clipping = clipping or r["peak"] > 0.99
        print(f"{name:<10} {r['peak']:>7.3f} {db(r['peak']):>8} {r['rms']:>7.3f} "
              f"{db(r['rms']):>8} {r['seconds']:>5.2f}s")
    print("\nrms is what the ear hears; keep presets within ~2 dB of each other.")
    if clipping:
        print("WARNING: clipping — lower that preset's `gain` in web/src/lib/chime.ts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

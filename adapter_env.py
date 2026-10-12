"""What an adapter engine (Codex, Grok) gets from the cockpit besides the prompt.

A Claude turn receives the project's environment (`secrets.env` + the cockpit media vars
COPS_PROJECT_ID / COPS_MEDIA_DIR) and, when the media vars are there, the repo's `tools/` first on
PATH plus the image/file hint in its system prompt (engine.run_engine). An adapter engine gets the
same two things from here, so `cockpit-img` / `cockpit-file` work in every engine.

Stdlib only at import time; the hint text is read from engine.py lazily (it is already loaded in
the cockpit process), so the two can never drift.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

TOOLS_DIR = str(Path(__file__).resolve().parent / "tools")


def media_on(env: "Mapping[str, str] | None") -> bool:
    return bool((env or {}).get("COPS_MEDIA_DIR"))


def with_tools_on_path(env: "Mapping[str, str] | None") -> dict:
    """A copy of `env` with the repo's tools/ FIRST on PATH when the media vars are present (the
    helpers live there; first, so a stale hand-copied helper elsewhere on PATH never wins)."""
    out = dict(env or {})
    if not media_on(out):
        return out
    current = out.get("PATH") or os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p and p != TOOLS_DIR]
    out["PATH"] = os.pathsep.join([TOOLS_DIR, *parts])
    return out


def media_hint(env: "Mapping[str, str] | None") -> str:
    """The image + file instructions a Claude turn gets, or "" without the media vars."""
    if not media_on(env):
        return ""
    try:
        from engine import FILES_PROMPT, IMAGES_PROMPT
    except Exception:  # noqa: BLE001 — a broken engine import must not take an adapter turn down
        return ""
    return IMAGES_PROMPT + "\n" + FILES_PROMPT

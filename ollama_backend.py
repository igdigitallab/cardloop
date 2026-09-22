"""ollama_backend.py — local inference as a BACKEND of the Claude harness (spec-092 P3).

Not a provider. Ollama 0.33+ serves a NATIVE Anthropic `/v1/messages` (real `tool_use`
blocks, real Anthropic SSE, a correct multi-step `tool_use` -> `tool_result` -> `end_turn`
round trip — measured, see docs/internal/specs/spec-092-ollama-probe.md), so pointing the
bundled CLI at it with `ANTHROPIC_BASE_URL` buys the ENTIRE agent loop — tools, hooks,
sub-agents, plan mode, ask mode — for an env dict, instead of a third engine with its own
tool set. That is why `runtime.OLLAMA_BACKEND` is a backend of `provider="claude"` and why
`validate_runtime_change` explicitly refuses `provider="ollama"`.

This module owns exactly three things and nothing else:
  * the feature flag and endpoint (env),
  * a LIVE availability probe (the box is real hardware that disappears — see below),
  * the model list that endpoint currently serves.

⚠️ Availability MUST be a live probe, never a config flag. The GPU is shared with ComfyUI
under automatic event-driven arbitration: `comfy-wake.path` fires `gpu-switch comfy` the
moment anyone queues an image on the studio host, which `docker stop`s ollama. It fired
twice in 15 minutes during the original probe, unattended. A config flag would advertise a
backend that is not there and the turn would die as a bare TCP refusal.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

BACKEND = "ollama"

# How long a successful probe is trusted. Short on purpose: the backend can vanish between
# two turns, and a stale "available" is worse than a slow menu — it produces a turn that
# dies mid-sentence instead of a row that is visibly greyed out.
_PROBE_TTL_SEC = 60.0
# A FAILED probe is cached for less: the box usually comes back on its own once ComfyUI
# releases the card, and making the operator wait a full minute to see it return would
# read as "still broken".
_PROBE_FAIL_TTL_SEC = 15.0
_PROBE_TIMEOUT_SEC = 3.0

_cache: dict = {"data": None, "ts": 0.0, "ok": False}


def ollama_enabled() -> bool:
    return (os.environ.get("OLLAMA_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    """The endpoint the CLI is pointed at. No trailing slash — the CLI appends `/v1/...`."""
    return (os.environ.get("OLLAMA_BASE_URL", "") or "http://127.0.0.1:11434").rstrip("/")


def auth_token() -> str:
    """Ollama ignores it, but the CLI refuses to start without SOME credential."""
    return (os.environ.get("OLLAMA_AUTH_TOKEN", "") or "local").strip() or "local"


def _fetch_tags(url: str) -> list[dict]:
    req = urllib.request.Request(f"{url}/api/tags", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SEC) as resp:  # noqa: S310 — operator-configured host
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    return [m for m in (payload.get("models") or []) if isinstance(m, dict)]


async def backend_info(*, force: bool = False) -> dict:
    """`{backend, enabled, available, models, base_url, error}` — the row the picker shows.

    Never raises: an unreachable box is a normal, frequent state, not an error condition of
    this function. `available=False` carries the reason so the greyed row can say why.
    """
    if not ollama_enabled():
        return {
            "backend": BACKEND, "enabled": False, "available": False, "models": [],
            "base_url": base_url(),
            "error": "local inference is off (set OLLAMA_ENABLED=true)",
        }
    now = time.time()
    cached = _cache["data"]
    ttl = _PROBE_TTL_SEC if _cache["ok"] else _PROBE_FAIL_TTL_SEC
    if not force and cached is not None and (now - _cache["ts"]) < ttl:
        return cached

    url = base_url()
    try:
        raw = await asyncio.to_thread(_fetch_tags, url)
        models = [
            {"value": name, "label": name}
            for name in (str(m.get("name") or "").strip() for m in raw)
            if name
        ]
        info = {
            "backend": BACKEND, "enabled": True, "available": bool(models), "models": models,
            "base_url": url,
            "error": None if models else "the endpoint answered but serves no models",
        }
        _cache.update(data=info, ts=now, ok=bool(models))
        return info
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        # The single most common case by far: the GPU was handed to ComfyUI and the
        # container is stopped, so the host still pings while the port refuses.
        info = {
            "backend": BACKEND, "enabled": True, "available": False, "models": [],
            "base_url": url,
            "error": f"{url} is not answering ({type(exc).__name__}) — the GPU may be held by another stack",
        }
        _cache.update(data=info, ts=now, ok=False)
        return info


def reset_cache() -> None:
    """Test hook / explicit refresh — the probe cache is process-wide."""
    _cache.update(data=None, ts=0.0, ok=False)

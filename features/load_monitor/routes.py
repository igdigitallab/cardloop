"""features/load_monitor/routes.py — GET /api/system-load (authenticated; /api/* is gated)."""
from __future__ import annotations

import time

from aiohttp import web

import engine as _engine
import load_monitor as _lm


async def api_system_load(req: web.Request) -> web.Response:
    snap = _lm.MONITOR.snapshot()
    if snap is None:                      # first seconds after start: say so instead of guessing
        return web.json_response({
            "level": "unknown", "score": 0, "at": None, "age_s": None, "signals": [], "top": [],
            "chats": {"live": 0, "max": _engine.LIVE_CLIENT_MAX}, "host": {}, "warming_up": True,
        })
    # age is computed here, on the server clock: the browser's clock may be minutes off.
    return web.json_response({**snap, "age_s": round(max(0.0, time.time() - snap["at"]), 1)})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/system-load", api_system_load)

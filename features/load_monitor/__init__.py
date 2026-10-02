"""features/load_monitor — spec-094: is this host overloaded, at a glance.

Entry point: register(app, ctx), called once from webapp startup via a deferred import.
Disabled (LOAD_MONITOR=0 or the Modules toggle) => complete no-op: no route, no loops. The
client treats the missing route (404) as "feature off" and hides the meter.

IRON RULE (spec-068): core (webapp/engine/board) MUST NOT import this package at module
top-level. The pure measuring code lives in the core module `load_monitor` (engine shares it).
"""
from __future__ import annotations

import load_monitor as _lm


def register(app, ctx: dict) -> None:  # type: ignore[type-arg]
    if not _lm.enabled():
        print("[load-monitor] disabled (LOAD_MONITOR=0 or module off)")
        return

    from features.load_monitor.loop import _heartbeat_loop, _sampler_loop
    from features.load_monitor.routes import add_routes
    from webapp import _spawn_bg, _STARTUP_BG_TASKS

    add_routes(app)
    _STARTUP_BG_TASKS.append(_spawn_bg(_heartbeat_loop()))
    _STARTUP_BG_TASKS.append(_spawn_bg(_sampler_loop(ctx)))
    print("[load-monitor] started (sample every 5s, GET /api/system-load)")

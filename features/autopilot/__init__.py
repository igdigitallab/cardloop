"""features/autopilot — Autopilot feature package (spec-067 / spec-068).

Entry point: register(app, ctx) — called once by webapp startup (deferred import).
When the "autopilot" module is disabled, register() is a no-op: no routes are
added and the background loop never starts.

IRON RULE (spec-068): core (webapp/engine/board) MUST NOT import this package at
module top-level.  The only allowed reference from core is a deferred import
INSIDE a function body (webapp's startup function).  This package freely imports
core — that direction is safe and cycle-free.
"""
from __future__ import annotations

import modules as _modules


def register(app, ctx: dict) -> None:  # type: ignore[type-arg]
    """Register autopilot routes and start the background loop if the module is enabled.

    *app* is the aiohttp Application.
    *ctx* is the runtime context dict passed through from bot.py.

    When modules.is_enabled("autopilot") is False this is a complete no-op,
    so the feature is fully dark: no routes, no background task.
    """
    if not _modules.is_enabled("autopilot"):
        return

    from features.autopilot.routes import add_routes
    from features.autopilot.loop import _autopilot_loop
    from webapp import _spawn_bg, _STARTUP_BG_TASKS
    import os

    add_routes(app)

    _STARTUP_BG_TASKS.append(_spawn_bg(_autopilot_loop(ctx)))
    print(
        f"[webapp] autopilot shadow loop started "
        f"(interval {os.environ.get('AUTOPILOT_TICK_SEC', '300')}s)"
    )

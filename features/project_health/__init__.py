"""features/project_health — "is this project quietly unwell?" (card [health]).

Entry point: register(app, ctx), called once from webapp startup via a deferred import.
Disabled module => complete no-op: no routes, no loop.

IRON RULE (spec-068): core (webapp/engine/board) MUST NOT import this package at module
top-level.  This package may freely import core.  (api_project_health imports `logic` inside
its function body to share the .env-exposed rule.)

What it is: a handful of read-only checks for real, actionable risks (memory index about to
be truncated, a heavy context floor, hanging work, project settings that execute code, ...).
What it is NOT: a score.  A healthy project shows nothing.
"""
from __future__ import annotations

import modules as _modules


def register(app, ctx: dict) -> None:  # type: ignore[type-arg]
    """Register the health-check endpoints and start the daily sweep when enabled."""
    if not _modules.is_enabled("project_health"):
        return

    from features.project_health.routes import add_routes
    from features.project_health.loop import _project_health_loop
    from features.project_health import logic as _logic
    from webapp import _spawn_bg, _STARTUP_BG_TASKS

    add_routes(app)
    if _logic.MODE != "off":
        _STARTUP_BG_TASKS.append(_spawn_bg(_project_health_loop(ctx)))
    print(
        f"[webapp] project health started (mode={_logic.MODE}, every {_logic.INTERVAL_SEC}s, "
        f"{len(_logic.check_ids())} checks)"
    )

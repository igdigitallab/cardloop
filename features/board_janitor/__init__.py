"""features/board_janitor — gives the Review column a way out (phase 1).

Entry point: register(app, ctx), called once from webapp startup via a deferred
import.  Disabled module => complete no-op: no routes, no loop.

IRON RULE (spec-068): core (webapp/engine/board) MUST NOT import this package at
module top-level.  This package may freely import core.
"""
from __future__ import annotations

import modules as _modules


def register(app, ctx: dict) -> None:  # type: ignore[type-arg]
    """Register the acceptance endpoints and start the sweep loop when enabled."""
    if not _modules.is_enabled("board_janitor"):
        return

    from features.board_janitor.routes import add_routes
    from features.board_janitor.loop import _board_janitor_loop
    from features.board_janitor import logic as _logic
    from webapp import _spawn_bg, _STARTUP_BG_TASKS

    add_routes(app)
    if _logic.MODE != "off":
        _STARTUP_BG_TASKS.append(_spawn_bg(_board_janitor_loop(ctx)))
    print(
        f"[webapp] board janitor started (mode={_logic.MODE}, "
        f"accept>{_logic.ACCEPT_AFTER_H}h, digest>{_logic.DIGEST_AFTER_H}h, "
        f"every {_logic.INTERVAL_SEC}s)"
    )

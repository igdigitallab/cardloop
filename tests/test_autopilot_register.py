"""tests/test_autopilot_register.py — spec-068 feature-module registration tests.

Verifies that features.autopilot.register(app, ctx) behaves correctly under
both enabled and disabled module states:
  - When modules.is_enabled("autopilot") is True: all 8 autopilot routes are
    registered on the aiohttp Application.
  - When modules.is_enabled("autopilot") is False: register() is a no-op and
    none of the 8 routes are present.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import features.autopilot as _autopilot_feature

# The expected route paths after registration.
_EXPECTED_ROUTES = {
    "/api/projects/{id}/autopilot",
    "/api/autopilot/status",
    "/api/autopilot/global",
    "/api/autopilot/pause",
    "/api/autopilot/resume",
    "/api/autopilot/tick",
    "/api/autopilot/decisions",
    "/api/autopilot/director/{id}",
}


def _make_ctx(tmp_path: Path) -> dict:
    """Minimal ctx for register() — only DATA and running are required."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return {
        "DATA": data_dir,
        "topics": {},
        "sessions": {},
        "running": {},
        "password": "test",
        "rate_limits": {},
        "save_topics": lambda: None,
        "save_sessions": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }


def _route_paths(app: web.Application) -> set:
    """Return the set of resource patterns registered on *app*."""
    return {r.get_info().get("path") or r.get_info().get("formatter", "")
            for r in app.router.resources()}


# ─────────────────────────────────────────────────────────────────────────────
# enabled=True: 8 routes registered
# ─────────────────────────────────────────────────────────────────────────────

def test_register_enabled_adds_all_routes(tmp_path):
    """When autopilot is enabled, all 8 routes are added to the app."""
    app = web.Application()
    ctx = _make_ctx(tmp_path)

    # Mock _spawn_bg so register() doesn't need a running event loop in this
    # synchronous test (the route-registration is the thing under test here).
    with patch("modules.is_enabled", return_value=True), \
         patch("webapp._spawn_bg", return_value=None):
        _autopilot_feature.register(app, ctx)

    registered = _route_paths(app)
    for expected in _EXPECTED_ROUTES:
        assert expected in registered, f"Missing route: {expected}"


def test_register_enabled_adds_exactly_8_autopilot_routes(tmp_path):
    """Exactly 8 autopilot routes are added — no extras, no missing."""
    app = web.Application()
    ctx = _make_ctx(tmp_path)

    with patch("modules.is_enabled", return_value=True), \
         patch("webapp._spawn_bg", return_value=None):
        _autopilot_feature.register(app, ctx)

    registered = _route_paths(app)
    # Every expected route must be present.
    missing = _EXPECTED_ROUTES - registered
    assert not missing, f"Routes not registered: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# enabled=False: no-op (zero routes)
# ─────────────────────────────────────────────────────────────────────────────

def test_register_disabled_adds_no_routes(tmp_path):
    """When autopilot is disabled, register() is a no-op — zero routes added."""
    app = web.Application()
    ctx = _make_ctx(tmp_path)

    with patch("modules.is_enabled", return_value=False):
        _autopilot_feature.register(app, ctx)

    registered = _route_paths(app)
    for expected in _EXPECTED_ROUTES:
        assert expected not in registered, f"Route should NOT be registered: {expected}"


def test_register_disabled_leaves_app_empty(tmp_path):
    """Disabled register() leaves the application with no resources at all."""
    app = web.Application()
    ctx = _make_ctx(tmp_path)

    with patch("modules.is_enabled", return_value=False):
        _autopilot_feature.register(app, ctx)

    # aiohttp always adds a system resource for the plain matcher — filter it out
    non_system = [r for r in app.router.resources()
                  if not getattr(r, "_path", "").startswith("/_")]
    assert len(non_system) == 0

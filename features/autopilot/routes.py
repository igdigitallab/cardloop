"""features/autopilot/routes.py — aiohttp endpoint handlers for the Autopilot feature.

All handlers that were previously defined in webapp.py under the
"API: autopilot" and "Autopilot DIRECTOR v1" comment blocks live here.

Import rule: this module imports from webapp/engine/board (feature→core is safe).
Core MUST NOT import this module at module top-level (IRON RULE — spec-068).
"""
from __future__ import annotations

from aiohttp import web

# ── core imports (feature → core is allowed) ─────────────────────────────────
from webapp import (
    _find_project_by_id,
    _collect_projects,
    _get_board_lock,
    _load_board,
    _save_board,
    _new_card_id,
    _bus_publish,
)

# ── sibling feature imports ───────────────────────────────────────────────────
from features.autopilot import logic as _autopilot
from features.autopilot.loop import _autopilot_test_signal
from features.autopilot.director import _run_director


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build status dict (used by multiple endpoints)
# ─────────────────────────────────────────────────────────────────────────────

def _autopilot_status(ctx: dict) -> dict:
    """Build the GET /api/autopilot/status response body."""
    data_dir = ctx["DATA"]
    state = _autopilot.load_state(data_dir)
    projects = _collect_projects(ctx)
    per_project = {
        p["id"]: _autopilot.get_project_mode(p)
        for p in projects
        if not p.get("is_free")
    }
    return {
        "global_enabled": bool(state.get("global_enabled")),
        "paused": bool(state.get("paused")),
        "daily_cap": _autopilot.DAILY_TOKEN_CAP,
        "tokens_today": int(state.get("tokens_today", 0)),
        "active_runs": int(state.get("active_runs", 0)),
        "max_concurrent": _autopilot.MAX_CONCURRENT,
        "rl_reserve": _autopilot.RL_RESERVE,
        "per_project": per_project,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Route handlers
# ─────────────────────────────────────────────────────────────────────────────

async def api_autopilot_set_project_mode(req: web.Request) -> web.Response:
    """PUT /api/projects/{id}/autopilot  {mode: "off"|"propose"|"auto"}
    Validate, persist to topics.json, return {mode: ...}.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    mode = str(body.get("mode", "")).strip().lower()
    if not _autopilot.valid_mode(mode):
        return web.json_response(
            {"error": f"mode must be one of: {list(_autopilot.MODES)}"},
            status=400,
        )
    # Persist to ALL topics entries with this cwd (same pattern as settings POST)
    cwd = project["cwd"]
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            if mode == _autopilot.DEFAULT_MODE:
                b.pop("autopilot", None)  # keep topics.json lean
            else:
                b["autopilot"] = mode
    save_fn = ctx.get("save_topics")
    if callable(save_fn):
        save_fn()
    return web.json_response({"mode": mode})


async def api_autopilot_status(req: web.Request) -> web.Response:
    """GET /api/autopilot/status — global autopilot state + per-project modes."""
    ctx = req.app["ctx"]
    return web.json_response(_autopilot_status(ctx))


async def api_autopilot_global(req: web.Request) -> web.Response:
    """POST /api/autopilot/global  {enabled: bool} — flip global_enabled flag."""
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return web.json_response({"error": "expected {enabled: bool}"}, status=400)
    data_dir = ctx["DATA"]
    state = _autopilot.load_state(data_dir)
    state["global_enabled"] = body["enabled"]
    _autopilot.save_state(data_dir, state)
    return web.json_response(_autopilot_status(ctx))


async def api_autopilot_pause(req: web.Request) -> web.Response:
    """POST /api/autopilot/pause — set paused=True."""
    ctx = req.app["ctx"]
    data_dir = ctx["DATA"]
    state = _autopilot.load_state(data_dir)
    state["paused"] = True
    _autopilot.save_state(data_dir, state)
    return web.json_response(_autopilot_status(ctx))


async def api_autopilot_resume(req: web.Request) -> web.Response:
    """POST /api/autopilot/resume — set paused=False."""
    ctx = req.app["ctx"]
    data_dir = ctx["DATA"]
    state = _autopilot.load_state(data_dir)
    state["paused"] = False
    _autopilot.save_state(data_dir, state)
    return web.json_response(_autopilot_status(ctx))


async def api_autopilot_tick(req: web.Request) -> web.Response:
    """POST /api/autopilot/tick — run one shadow tick immediately (manual trigger)."""
    from features.autopilot.loop import _autopilot_tick_once
    ctx = req.app["ctx"]
    data_dir = ctx["DATA"]
    state = _autopilot.load_state(data_dir)
    decisions = await _autopilot_tick_once(ctx)
    return web.json_response({
        "ran": True,
        "active": _autopilot.is_active(state),
        "decisions": decisions,
    })


async def api_autopilot_decisions(req: web.Request) -> web.Response:
    """GET /api/autopilot/decisions?limit=N — most-recent shadow decisions, newest first."""
    ctx = req.app["ctx"]
    try:
        limit = int(req.rel_url.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    limit = max(1, min(limit, 500))
    records = _autopilot.read_trajectory(ctx["DATA"], limit=limit)
    return web.json_response({"decisions": list(reversed(records))})


async def api_autopilot_director(req: web.Request) -> web.Response:
    """POST /api/autopilot/director/{id} — manually trigger a director plan run."""
    ctx = req.app["ctx"]
    pid = req.match_info.get("id", "")
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    result = await _run_director(ctx, project)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


# ─────────────────────────────────────────────────────────────────────────────
# Route registration helper
# ─────────────────────────────────────────────────────────────────────────────

def add_routes(app: web.Application) -> None:
    """Register all autopilot routes onto *app*."""
    app.router.add_put("/api/projects/{id}/autopilot", api_autopilot_set_project_mode)
    app.router.add_get("/api/autopilot/status", api_autopilot_status)
    app.router.add_post("/api/autopilot/global", api_autopilot_global)
    app.router.add_post("/api/autopilot/pause", api_autopilot_pause)
    app.router.add_post("/api/autopilot/resume", api_autopilot_resume)
    app.router.add_post("/api/autopilot/tick", api_autopilot_tick)
    app.router.add_get("/api/autopilot/decisions", api_autopilot_decisions)
    app.router.add_post("/api/autopilot/director/{id}", api_autopilot_director)

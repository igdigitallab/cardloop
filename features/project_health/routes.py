"""features/project_health/routes.py — HTTP surface for the project health check.

Import rule: feature -> core is safe; core never imports this module.
"""
from __future__ import annotations

import asyncio
import re

from aiohttp import web

from webapp import _find_project_by_id

from features.project_health import logic as _logic
from features.project_health import loop as _loop

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _truthy(v: "str | None") -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes")


async def api_project_health_check(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/health-check — findings for one project.

    Default: reuse a result younger than ~5 min (project open: one cheap GET, typically < 100 ms
    when it has to run).  ?fresh=1 always re-runs (the Tests button, the modal's Re-check).
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if project.get("is_free") or not project.get("cwd"):
        return web.json_response(_loop.empty_result(project))
    result = await _loop.check_project(ctx, project, fresh=_truthy(req.query.get("fresh")))
    return web.json_response(result)


async def api_project_health_ack(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/health-check/ack {check_id, sha256} — silence an acknowledged finding.

    The hash must be that of a project settings file AS IT IS NOW: acknowledging a stale hash
    would bless content nobody has seen.  The finding reappears as soon as the file changes.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad json"}, status=400)
    check_id = body.get("check_id")
    digest = body.get("sha256")
    if check_id not in _logic.ACKABLE_CHECKS:
        return web.json_response({"error": "this check cannot be acknowledged"}, status=400)
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        return web.json_response({"error": "sha256 must be 64 hex chars"}, status=400)
    current = await asyncio.get_running_loop().run_in_executor(
        None, _logic.settings_hashes, project.get("cwd") or "")
    if digest not in current.values():
        _loop.forget(project["id"])
        fresh = await _loop.check_project(ctx, project, fresh=True)
        return web.json_response({**fresh, "error": "the file changed since it was listed"}, status=409)
    _logic.ack_add(ctx["DATA"], project["id"], check_id, digest)
    _loop.forget(project["id"])
    result = await _loop.check_project(ctx, project, fresh=True)
    return web.json_response({"ok": True, **result})


async def api_health_check_fleet(req: web.Request) -> web.Response:
    """GET /api/health-check — what the last fleet sweep found (projects with findings only)."""
    return web.json_response(_loop.fleet_snapshot())


def add_routes(app) -> None:
    app.router.add_get("/api/projects/{id}/health-check", api_project_health_check)
    app.router.add_post("/api/projects/{id}/health-check/ack", api_project_health_ack)
    app.router.add_get("/api/health-check", api_health_check_fleet)

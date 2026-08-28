"""features/board_janitor/routes.py — HTTP surface for board acceptance.

Import rule: feature -> core is safe; core never imports this module.
"""
from __future__ import annotations

import time

from aiohttp import web

from webapp import (
    _find_project_by_id,
    _get_board_lock,
    _load_board,
    _save_board,
    _done_path,
    _done_archive_line,
    _pop_card,
    _valid_card_id,
    _board_payload_with_specs,
    _emit_board_event,
    _dismissed_add,
)

from features.board_janitor import logic as _logic
from features.board_janitor.loop import _janitor_tick_once


async def api_accept_review(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/cards/accept-review — archive Review cards in one go.

    Body: {"ids": ["abc123", ...]} for specific cards, or {"all": true} for the
    whole column.  This is the operator's manual counterpart to the janitor's
    objective gate: the digest tells them what is parked, this closes it without
    dragging cards one at a time.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd = project.get("cwd") or ""
    name = project.get("name") or project.get("id") or "project"
    if not cwd:
        return web.json_response({"error": "project has no cwd"}, status=400)

    try:
        body = await req.json()
    except Exception:
        body = {}
    take_all = bool(body.get("all"))
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return web.json_response({"error": "ids must be a list"}, status=400)
    ids = [str(i) for i in ids]
    if not take_all and not ids:
        return web.json_response({"error": "nothing to accept"}, status=400)
    for cid in ids:
        if not _valid_card_id(cid):
            return web.json_response({"error": "bad card id"}, status=400)

    accepted: list = []
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        review = list(cols.get("review") or [])
        targets = review if take_all else [c for c in review if c["id"] in ids]
        if not targets:
            return web.json_response({"error": "card not found"}, status=404)
        dp = _done_path(cwd)
        header = dp.read_text(encoding="utf-8") if dp.exists() else f"# Done — {name}\n"
        if not header.strip():
            header = f"# Done — {name}\n"
        stamp = time.strftime("%Y-%m-%d")
        lines = ""
        for card in targets:
            popped = _pop_card(cols, card["id"])
            if popped is None:
                continue
            # An error card leaving the board counts as dismissed, same as a manual
            # move to Done — otherwise the error scanner recreates it on the next sweep.
            if str(popped["id"]).startswith("err-"):
                _dismissed_add(str(popped["id"])[4:])
            lines += _done_archive_line(popped, stamp)
            accepted.append({"id": popped["id"], "text": popped.get("text", "")})
        if lines:
            dp.write_text(header.rstrip() + "\n" + lines, encoding="utf-8")
        _save_board(cwd, name, preamble, cols)

    session_key = project.get("session_key") or project.get("tg_thread", "")
    for c in accepted:
        _emit_board_event(
            session_key, event="moved", card_id=c["id"], title=c["text"],
            column_from="review", column_to="done", severity="info",
            summary="Accepted from Review",
        )
    payload = _board_payload_with_specs(cwd, ctx["DATA"])
    return web.json_response({**payload, "ok": True, "accepted": len(accepted)})


async def api_janitor_status(req: web.Request) -> web.Response:
    """GET /api/board/janitor — current policy plus the latest digest, if any."""
    ctx = req.app["ctx"]
    data = ctx["DATA"]
    latest = ""
    try:
        digests = sorted((data / "inbox").glob("board-digest-*.md"))
        if digests:
            latest = digests[-1].read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return web.json_response({
        "mode": _logic.MODE,
        "accept_after_h": _logic.ACCEPT_AFTER_H,
        "digest_after_h": _logic.DIGEST_AFTER_H,
        "interval_sec": _logic.INTERVAL_SEC,
        "digest": latest,
    })


async def api_janitor_run(req: web.Request) -> web.Response:
    """POST /api/board/janitor/run — run one sweep now (operator-triggered)."""
    summary = await _janitor_tick_once(req.app["ctx"])
    summary.pop("entries", None)
    return web.json_response({"ok": True, **summary})


async def api_autonomy_get(req: web.Request) -> web.Response:
    """GET /api/autonomy — is unattended activity currently allowed?"""
    return web.json_response({"paused": _logic.autonomy_paused(req.app["ctx"]["DATA"])})


async def api_autonomy_set(req: web.Request) -> web.Response:
    """POST /api/autonomy {"paused": bool} — the fleet-wide stop switch.

    One switch above every per-feature flag: when the operator wants everything
    unattended to stop, they should not have to remember how many loops exist.
    """
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if "paused" not in body or not isinstance(body["paused"], bool):
        return web.json_response({"error": "paused must be a boolean"}, status=400)
    state = _logic.set_autonomy_paused(req.app["ctx"]["DATA"], body["paused"])
    return web.json_response({"ok": True, **state})


def add_routes(app) -> None:
    app.router.add_get("/api/autonomy", api_autonomy_get)
    app.router.add_post("/api/autonomy", api_autonomy_set)
    app.router.add_post("/api/projects/{id}/cards/accept-review", api_accept_review)
    app.router.add_get("/api/board/janitor", api_janitor_status)
    app.router.add_post("/api/board/janitor/run", api_janitor_run)

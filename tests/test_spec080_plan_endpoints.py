"""
spec-080: webapp pending-plan store + HTTP decide endpoints + guards.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "plans").mkdir(parents=True)
    monkeypatch.setattr(_webapp, "_PLANS_DIR", data / "plans")
    monkeypatch.setattr(_webapp, "_plan_records", {})
    monkeypatch.setattr(_webapp, "_pending_plan_futures", {})
    monkeypatch.setattr(_webapp, "_plan_pending_by_session", {})
    monkeypatch.setattr(_webapp, "_last_turn_options", {})
    monkeypatch.setattr(_webapp, "_crash_state_dirty", False)
    monkeypatch.setattr(_webapp, "_CRASH_STATE_FILE", data / "crash-recovery-state.json")

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ctx = {
        "topics": {"1001:42": {"project": "proj", "cwd": str(project_dir), "model": "sonnet"}},
        "sessions": {}, "running": {}, "cwd_locks": {},
        "password": "pw", "DATA": data, "HERE": ROOT,
        "VAULT_PROJECTS": None, "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "save_handoff": lambda: None, "run_engine": None, "ptb_app": None,
        "rate_limits": {}, "pending_handoff": {}, "context_warned": set(),
        "live_clients": {}, "evict_live_client": None,
    }
    ctx["_auth_token"] = _derive_token("pw")
    monkeypatch.setattr(_webapp, "_WEBAPP_CTX", ctx)
    return ctx


def _make_app(ctx):
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/projects/{id}/plan/{plan_id}", _webapp.api_plan_get)
    app.router.add_post("/api/projects/{id}/plan/{plan_id}/decide", _webapp.api_plan_decide)
    app.router.add_post("/api/projects/{id}/rotate", _webapp.api_project_rotate)
    app.router.add_get("/api/health", _webapp.api_health)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_create_and_approve_resets_plan_mode(plan_env):
    ctx = plan_env
    _webapp._last_turn_options["1001:42"] = {"effort": "high", "ultracode": False,
                                             "plan_mode": True}
    plan_id, fut = _webapp.create_pending_plan(ctx, "1001:42", "chatA", "## The plan")
    assert _webapp._plan_pending_by_session["1001:42"] == plan_id
    rec = _webapp._read_plan_meta(plan_id)
    assert rec["status"] == "awaiting_approval"
    assert (_webapp._PLANS_DIR / f"{plan_id}.json").exists()

    assert _webapp.resolve_plan(ctx, plan_id, "approve") is True
    assert (await fut) == {"decision": "approve", "feedback": ""}
    assert _webapp._read_plan_meta(plan_id)["status"] == "approved"
    assert "1001:42" not in _webapp._plan_pending_by_session
    # C2: server-side mirror of the toggle auto-off — wake turns must not re-enter plan mode
    assert _webapp._last_turn_options["1001:42"]["plan_mode"] is False


async def test_resolve_is_idempotent(plan_env):
    ctx = plan_env
    plan_id, fut = _webapp.create_pending_plan(ctx, "1001:42", None, "p")
    assert _webapp.resolve_plan(ctx, plan_id, "reject", "needs work") is True
    assert (await fut)["feedback"] == "needs work"
    assert _webapp.resolve_plan(ctx, plan_id, "approve") is False  # already decided
    assert _webapp._read_plan_meta(plan_id)["status"] == "rejected"


async def test_decide_endpoint_approve_then_noop(aiohttp_client, plan_env):
    ctx = plan_env
    plan_id, fut = _webapp.create_pending_plan(ctx, "1001:42", None, "## P")
    client = await aiohttp_client(_make_app(ctx))

    r1 = await client.post(f"/api/projects/proj/plan/{plan_id}/decide",
                           headers=_auth(ctx), json={"decision": "approve"})
    assert r1.status == 200 and (await r1.json())["status"] == "approved"
    assert (await fut)["decision"] == "approve"

    r2 = await client.post(f"/api/projects/proj/plan/{plan_id}/decide",
                           headers=_auth(ctx), json={"decision": "reject"})
    assert (await r2.json()).get("noop") is True


async def test_decide_endpoint_validates(aiohttp_client, plan_env):
    ctx = plan_env
    client = await aiohttp_client(_make_app(ctx))
    r = await client.post("/api/projects/proj/plan/zzzz/decide",
                          headers=_auth(ctx), json={"decision": "approve"})
    assert r.status == 400
    plan_id, _ = _webapp.create_pending_plan(ctx, "1001:42", None, "p")
    r2 = await client.post(f"/api/projects/proj/plan/{plan_id}/decide",
                           headers=_auth(ctx), json={"decision": "maybe"})
    assert r2.status == 400


async def test_plan_get_endpoint(aiohttp_client, plan_env):
    ctx = plan_env
    plan_id, _ = _webapp.create_pending_plan(ctx, "1001:42", "chatB", "## Full body")
    client = await aiohttp_client(_make_app(ctx))
    r = await client.get(f"/api/projects/proj/plan/{plan_id}", headers=_auth(ctx))
    rec = await r.json()
    assert rec["plan_text"] == "## Full body" and rec["chat_id"] == "chatB"
    r404 = await client.get("/api/projects/proj/plan/00000000", headers=_auth(ctx))
    assert r404.status == 404


async def test_rotate_refuses_while_plan_pending(aiohttp_client, plan_env):
    ctx = plan_env
    _webapp.create_pending_plan(ctx, "1001:42", None, "p")
    client = await aiohttp_client(_make_app(ctx))
    r = await client.post("/api/projects/proj/rotate", headers=_auth(ctx))
    assert r.status == 409
    assert "awaiting approval" in (await r.json())["error"]
    r2 = await client.post("/api/projects/proj/rotate", headers=_auth(ctx),
                           json={"force": True})
    assert r2.status == 200


async def test_health_reports_plan_pending(aiohttp_client, plan_env):
    ctx = plan_env
    client = await aiohttp_client(_make_app(ctx))
    d0 = await (await client.get("/api/health?deep=1")).json()
    assert d0["plan_pending"] == 0
    _webapp.create_pending_plan(ctx, "1001:42", None, "p")
    d1 = await (await client.get("/api/health?deep=1")).json()
    assert d1["plan_pending"] == 1


async def test_bus_preview_is_bounded(plan_env, monkeypatch):
    ctx = plan_env
    events: list = []
    monkeypatch.setattr(_webapp, "_bus_publish",
                        lambda sk, ev, persist=True: events.append(ev))
    _webapp.create_pending_plan(ctx, "1001:42", None, "x" * 100_000)
    ready = [e for e in events if e.get("kind") == "plan_ready"]
    assert ready and len(ready[0]["plan_text_preview"]) <= _webapp._PLAN_PREVIEW_LIMIT

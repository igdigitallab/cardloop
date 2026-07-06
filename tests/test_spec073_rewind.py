"""
spec-073: POST /api/projects/{id}/rewind — file undo via SDK rewind_files.

Guards: 409 while a turn is running (the agent may be mid-write), 409 without a live
client (checkpoints belong to the connected CLI process), 400 without message_uuid.
Happy path calls entry.client.rewind_files(uuid) and publishes a board strip.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(tmp_path / "myproject"),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "live_clients": {},
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    (tmp_path / "myproject").mkdir(exist_ok=True)
    return ctx


@pytest.fixture
def rewind_app(fake_ctx):
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/rewind", _webapp.api_project_rewind)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _project_id(ctx):
    projects = _webapp._collect_projects(ctx)
    return next(p["id"] for p in projects if p.get("name") == "myproject")


def _session_key(ctx, pid):
    projects = _webapp._collect_projects(ctx)
    p = next(p for p in projects if p["id"] == pid)
    return p.get("session_key") or p.get("tg_thread", "")


@pytest.mark.asyncio
async def test_rewind_requires_uuid(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    r = await client.post(f"/api/projects/{pid}/rewind", json={}, headers=_auth(fake_ctx))
    assert r.status == 400


@pytest.mark.asyncio
async def test_rewind_409_while_running(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    fake_ctx["running"][_session_key(fake_ctx, pid)] = True
    r = await client.post(f"/api/projects/{pid}/rewind", json={"message_uuid": "u1"},
                          headers=_auth(fake_ctx))
    assert r.status == 409


@pytest.mark.asyncio
async def test_rewind_409_without_live_client(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    r = await client.post(f"/api/projects/{pid}/rewind", json={"message_uuid": "u1"},
                          headers=_auth(fake_ctx))
    assert r.status == 409
    body = await r.json()
    assert "live session" in body["error"]


@pytest.mark.asyncio
async def test_rewind_happy_path(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    entry = MagicMock()
    entry.client.rewind_files = AsyncMock()
    fake_ctx["live_clients"][_session_key(fake_ctx, pid)] = entry
    r = await client.post(f"/api/projects/{pid}/rewind", json={"message_uuid": "uuid-42"},
                          headers=_auth(fake_ctx))
    assert r.status == 200 and (await r.json())["ok"] is True
    entry.client.rewind_files.assert_awaited_once_with("uuid-42")

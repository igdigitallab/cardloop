"""
Tests for the dual-layer (chats.json + sessions) rotate fix.

Proves:
  1. After rotate on a project whose active chat has a session_id, BOTH the
     chats.json active chat session_id is None AND ctx["sessions"][key] is cleared.
  2. The "no active session" path returns reset:false when NEITHER layer has a session.
  3. handoff=true still stores the summary when session_id lives only in chats.json
     (mock _build_handoff).
  4. Existing tests: 409 busy still works; running lock is released after rotate.
  5. No active session in sessions but active chat has session_id → rotate still fires
     (sessions-only check was the original bug scenario).
"""
import sys
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _save_chats, _load_chats


# ─────────────────────────── helpers ────────────────────────────────────────

def _make_ctx(tmp_path, project_dir, *, run_engine=None):
    """Minimal ctx matching spec-042 test helpers."""
    password = "testpass"
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "cwd_locks": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "save_handoff": lambda: None,
        "run_engine": run_engine,
        "ptb_app": None,
        "rate_limits": {},
        "pending_handoff": {},
        "context_warned": set(),
        "live_clients": {},
        "evict_live_client": None,
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


def _make_rotate_app(ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/rotate", _webapp.api_project_rotate)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _seed_chats(ctx, project_id, active_chat_id, session_id):
    """Write a chats.json with one active chat holding the given session_id."""
    data = {
        project_id: {
            "active": active_chat_id,
            "chats": [
                {
                    "id": active_chat_id,
                    "name": "Main",
                    "session_id": session_id,
                    "created_at": 0,
                }
            ],
        }
    }
    _save_chats(ctx, data)
    return data


# ─────────────────────────── 1. Both layers cleared after rotate ─────────────

async def test_rotate_clears_both_layers(aiohttp_client, tmp_path):
    """Primary regression test: after rotate, chats.json active chat session_id is None
    AND ctx['sessions'][key] is cleared."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    old_sid = "old-session-abc"

    # Seed both layers with the old session
    ctx["sessions"][session_key] = old_sid
    _seed_chats(ctx, "myproject", "chat001", old_sid)

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True

    # Layer 1 (sessions) must be cleared
    assert ctx["sessions"].get(session_key) is None, (
        f"ctx['sessions'] must be None after rotate, got: {ctx['sessions'].get(session_key)}"
    )

    # Layer 2 (chats.json active chat) must be cleared
    chats = _load_chats(ctx)
    entry = chats.get("myproject", {})
    active_id = entry.get("active")
    active_chat = next((c for c in entry.get("chats", []) if c["id"] == active_id), None)
    assert active_chat is not None, "Active chat must still exist"
    assert active_chat.get("session_id") is None, (
        f"chats.json active chat session_id must be None after rotate, "
        f"got: {active_chat.get('session_id')!r}"
    )


async def test_rotate_clears_chat_layer_even_when_sessions_empty(aiohttp_client, tmp_path):
    """Bug scenario: sessions layer is already empty but chat layer has a session_id.
    Rotate must still fire AND clear the chat layer."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    old_sid = "ghost-session-xyz"

    # Only seed the chat layer — sessions is empty (post-restart scenario)
    # This is exactly the bug: the old code returned reset:false or left chat layer intact.
    _seed_chats(ctx, "myproject", "chat002", old_sid)
    assert ctx["sessions"].get(session_key) is None  # confirm sessions is empty

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True, (
        "rotate must fire when chat layer has a session, even if sessions dict is empty"
    )

    chats = _load_chats(ctx)
    active_chat = next(
        (c for c in chats.get("myproject", {}).get("chats", [])),
        None,
    )
    assert active_chat is not None
    assert active_chat.get("session_id") is None, (
        f"chats.json session_id must be None after rotate, got: {active_chat.get('session_id')!r}"
    )


# ─────────────────────────── 2. Neither layer has session → reset:false ──────

async def test_rotate_no_session_in_either_layer_returns_false(aiohttp_client, tmp_path):
    """When neither ctx['sessions'] nor chats.json active chat has a session_id,
    rotate returns reset:false (nothing to clear)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)

    # Seed chats.json with a chat that has NO session_id
    _seed_chats(ctx, "myproject", "chat003", None)
    assert ctx["sessions"].get("1001:42") is None

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is False, (
        f"Both layers empty → reset must be false, got: {data}"
    )


async def test_rotate_no_chats_no_sessions_returns_false(aiohttp_client, tmp_path):
    """No chats.json at all, no session in ctx — genuine empty state → reset:false."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["reset"] is False


# ─────────────────────────── 3. handoff=true reads sid from chat layer ───────

async def test_rotate_handoff_uses_chat_layer_sid(aiohttp_client, tmp_path):
    """handoff=true uses the effective_sid from the chat layer when sessions is empty."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    old_sid = "chat-layer-only-sid"

    # Only the chat layer has the session_id
    _seed_chats(ctx, "myproject", "chat004", old_sid)

    build_calls = []

    async def _fake_build(ctx, sk, cwd, sid):
        build_calls.append(sid)
        return f"Summary for {sid}"

    save_handoff_calls = []
    ctx["save_handoff"] = lambda: save_handoff_calls.append(True)

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_build_handoff", _fake_build):
        resp = await client.post(
            "/api/projects/myproject/rotate",
            json={"handoff": True},
            headers=_auth_headers(ctx),
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True
    assert data["handoff"] is True, f"handoff must be stored, got: {data}"
    assert build_calls == [old_sid], (
        f"_build_handoff must be called with the chat-layer sid, got: {build_calls}"
    )
    assert ctx["pending_handoff"].get(session_key) == f"Summary for {old_sid}"
    assert save_handoff_calls, "save_handoff must be called"

    # Both layers still cleared
    assert ctx["sessions"].get(session_key) is None
    chats = _load_chats(ctx)
    active = next(
        (c for c in chats.get("myproject", {}).get("chats", [])),
        None,
    )
    assert active is not None
    assert active.get("session_id") is None


# ─────────────────────────── 4. Running lock released after rotate ────────────

async def test_rotate_releases_running_lock(aiohttp_client, tmp_path):
    """After rotate completes (success or exception path), running[key] must be None."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "some-sid"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )

    assert resp.status == 200
    assert ctx["running"].get(session_key) is None, (
        f"running lock must be released after rotate, got: {ctx['running'].get(session_key)}"
    )


async def test_rotate_releases_running_lock_on_exception(tmp_path):
    """Even if _evict_live_client raises, the running lock is released (finally block)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "some-sid"

    async def _bad_evict(sk, ctx):
        raise RuntimeError("evict failed")

    ctx["evict_live_client"] = _bad_evict

    # Call directly (not via aiohttp_client) to avoid swallowing the exception
    class FakeMatchInfo(dict):
        def __getitem__(self, key):
            return "myproject"

    class FakeRequest:
        app = {"ctx": ctx}
        match_info = FakeMatchInfo()
        can_read_body = False

    resp = await _webapp.api_project_rotate(FakeRequest())
    # evict errors are caught internally — should still return 200
    assert resp.status == 200
    assert ctx["running"].get(session_key) is None, (
        "running lock must be released even when evict raises"
    )


# ─────────────────────────── 5. 409 busy guard still works ──────────────────

async def test_rotate_busy_returns_409(aiohttp_client, tmp_path):
    """409 when a run is already in progress (regression guard)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["running"]["1001:42"] = True
    ctx["sessions"]["1001:42"] = "active-sid"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 409


# ─────────────────────────── 6. Blank rotate (no handoff) clears both layers ─

async def test_rotate_blank_clears_both_layers(aiohttp_client, tmp_path):
    """POST /rotate with no body (blank reset) also clears both layers."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    old_sid = "blank-rotate-sid"

    ctx["sessions"][session_key] = old_sid
    _seed_chats(ctx, "myproject", "chat005", old_sid)

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True
    assert data["handoff"] is False

    # Both layers cleared
    assert ctx["sessions"].get(session_key) is None
    chats = _load_chats(ctx)
    active = next(
        (c for c in chats.get("myproject", {}).get("chats", [])),
        None,
    )
    assert active is not None
    assert active.get("session_id") is None

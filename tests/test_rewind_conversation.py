"""
POST /api/projects/{id}/rewind-conversation — conversation-side rewind (spec doc:
docs/internal/sdk-feature-audit/04-session-rewind.md), the counterpart to spec-073's
file-only /rewind.

Guards: 400 missing message_uuid, 409 while running, 400 for a Codex chat (no fork
mechanism), 400 with no active session, 400 when message_uuid can't be resolved to a
prior transcript entry. Happy path: drains the chat queue, clears buffered board
events, evicts the live client, calls ctx["rewind_conversation"], and writes the
returned session id back into chats.json + ctx["sessions"].
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _save_chats, _load_chats
from claude_agent_sdk import ProcessError


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir(exist_ok=True)
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
        "rewind_conversation": None,
        "rewind_refused_hint": None,
        "evict_live_client": None,
    }
    ctx["_auth_token"] = _derive_token("testpass")
    return ctx


@pytest.fixture
def rewind_app(fake_ctx):
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/rewind-conversation", _webapp.api_project_rewind_conversation)
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


def _seed_chat(ctx, project_id, chat_id, session_id, provider="claude"):
    data = {
        project_id: {
            "active": chat_id,
            "chats": [
                {
                    "id": chat_id,
                    "name": "Main",
                    "provider": provider,
                    "session_id": session_id if provider == "claude" else None,
                    "codex_thread_id": session_id if provider == "codex" else None,
                    "created_at": 0,
                }
            ],
        }
    }
    _save_chats(ctx, data)
    return data


def _msg(uuid):
    return SimpleNamespace(uuid=uuid)


@pytest.mark.asyncio
async def test_requires_message_uuid(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    r = await client.post(f"/api/projects/{pid}/rewind-conversation", json={}, headers=_auth(fake_ctx))
    assert r.status == 400


@pytest.mark.asyncio
async def test_409_while_running(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    fake_ctx["running"][_session_key(fake_ctx, pid)] = True
    r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                          json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
    assert r.status == 409


@pytest.mark.asyncio
async def test_400_codex_chat_rejected(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    _seed_chat(fake_ctx, pid, "chat1", "thread-1", provider="codex")
    r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                          json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
    assert r.status == 400
    body = await r.json()
    assert "Claude" in body["error"]


@pytest.mark.asyncio
async def test_400_no_active_session(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                          json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
    assert r.status == 400
    body = await r.json()
    assert "no active session" in body["error"]


@pytest.mark.asyncio
async def test_400_message_not_found(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")
    with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
        r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                              json={"message_uuid": "does-not-exist"}, headers=_auth(fake_ctx))
    assert r.status == 400
    body = await r.json()
    assert "not found" in body["error"]


@pytest.mark.asyncio
async def test_400_first_message_cannot_rewind(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")
    with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
        r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                              json={"message_uuid": "u1"}, headers=_auth(fake_ctx))
    assert r.status == 400
    body = await r.json()
    assert "/reset" in body["error"]


@pytest.mark.asyncio
async def test_happy_path_writes_back_new_session_id(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    sk = _session_key(fake_ctx, pid)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")
    fake_ctx["sessions"][sk] = "old-sid"
    fake_ctx["rewind_conversation"] = AsyncMock(return_value="new-forked-sid")
    fake_ctx["evict_live_client"] = AsyncMock()

    with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
        r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                              json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["session_id"] == "new-forked-sid"

    fake_ctx["rewind_conversation"].assert_awaited_once()
    _, kwargs = fake_ctx["rewind_conversation"].call_args
    assert kwargs["resume_session_id"] == "old-sid"
    assert kwargs["rewind_at_uuid"] == "u1"
    assert kwargs["rewind_drop_turn_uuid"] == "u2"

    # write-back: both layers now hold the NEW forked session id
    assert fake_ctx["sessions"][sk] == "new-forked-sid"
    chats = _load_chats(fake_ctx)
    active_chat = chats[pid]["chats"][0]
    assert active_chat["session_id"] == "new-forked-sid"

    # running slot released
    assert fake_ctx["running"].get(sk) is None


@pytest.mark.asyncio
async def test_evicts_live_client_before_rewind(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    sk = _session_key(fake_ctx, pid)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")

    calls = []

    async def _evict(session_key, ctx):
        calls.append("evict")

    async def _rewind(**kwargs):
        calls.append("rewind")
        return "new-sid"

    fake_ctx["evict_live_client"] = _evict
    fake_ctx["rewind_conversation"] = _rewind

    with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
        r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                              json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
    assert r.status == 200
    assert calls == ["evict", "rewind"]


@pytest.mark.asyncio
async def test_drains_pending_queue_and_returns_it(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    sk = _session_key(fake_ctx, pid)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")
    fake_ctx["rewind_conversation"] = AsyncMock(return_value="new-sid")
    _webapp._CHAT_QUEUE[sk] = [{"id": "q1", "text": "queued while the old turn was running"}]
    try:
        with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
            r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                                  json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
        assert r.status == 200
        body = await r.json()
        assert body["drained_queue"] == ["queued while the old turn was running"]
        assert sk not in _webapp._CHAT_QUEUE
    finally:
        _webapp._CHAT_QUEUE.pop(sk, None)


@pytest.mark.asyncio
async def test_clears_board_events_buffer(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    sk = _session_key(fake_ctx, pid)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")
    fake_ctx["rewind_conversation"] = AsyncMock(return_value="new-sid")
    import collections
    _webapp._recent_board_events[sk] = collections.deque([{"ts": 0, "kind": "board_event"}])
    try:
        with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
            r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                                  json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
        assert r.status == 200
        assert sk not in _webapp._recent_board_events
    finally:
        _webapp._recent_board_events.pop(sk, None)


@pytest.mark.asyncio
async def test_process_error_surfaces_hint_and_still_returns_drained_queue(aiohttp_client, fake_ctx, rewind_app):
    client = await aiohttp_client(rewind_app)
    pid = _project_id(fake_ctx)
    sk = _session_key(fake_ctx, pid)
    _seed_chat(fake_ctx, pid, "chat1", "old-sid")

    async def _refused(**kwargs):
        raise ProcessError("boom", exit_code=1, stderr="Resume rejected by --resume-drops-turn: reason")

    fake_ctx["rewind_conversation"] = _refused
    import engine as _engine
    fake_ctx["rewind_refused_hint"] = _engine._rewind_refused_hint
    _webapp._CHAT_QUEUE[sk] = [{"id": "q1", "text": "should not be lost"}]
    try:
        with patch.object(_webapp, "_get_session_messages", return_value=[_msg("u1"), _msg("u2")]):
            r = await client.post(f"/api/projects/{pid}/rewind-conversation",
                                  json={"message_uuid": "u2"}, headers=_auth(fake_ctx))
        assert r.status == 502
        body = await r.json()
        assert "rewind refused" in body["error"]
        assert body["drained_queue"] == ["should not be lost"]
        # running slot released even on failure
        assert fake_ctx["running"].get(sk) is None
    finally:
        _webapp._CHAT_QUEUE.pop(sk, None)

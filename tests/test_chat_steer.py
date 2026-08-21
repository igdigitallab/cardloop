"""
Tests for spec-086: mid-turn steering.

While a turn is running ON the session's live client, an operator message is injected
into the CLI's stdin (client.query) instead of parking in the chat queue — the same
steering behaviour as typing mid-turn in terminal Claude Code.

Covers:
- POST /chat while busy on a live-client turn → SSE type=steered, client.query called,
  queue untouched, 'steer' event published to the activity bus
- POST /chat while busy with the True placeholder / a fresh-client turn → still queued
- POST /api/projects/{id}/chat/steer → {"steered": true} on a steerable turn,
  falls back to the queue (201 + item) otherwise
- chat_id mismatch between the steer and the in-flight turn → queued, not injected
- STEER_MID_TURN=0 kills the feature → queued
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── Fakes ────────────────────────────────────────────


class _FakeClient:
    """Stands in for a connected ClaudeSDKClient: records query() calls."""

    def __init__(self):
        self.queries: list = []

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)


class _FakeEntry:
    """Stands in for engine._LiveEntry — only .client is read by the steer guard."""

    def __init__(self, client):
        self.client = client


# ─────────────────────────── Fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate chat queue, live turns and timeline writes between tests."""
    old_file = _webapp._CHAT_QUEUE_FILE
    old_queue = dict(_webapp._CHAT_QUEUE)
    old_turns = dict(_webapp._live_turns)
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = tmp_path / "chat-queue.json"
    _webapp._live_turns.clear()
    timeline: list = []
    monkeypatch.setattr(_webapp, "_timeline_append", lambda sk, ev: timeline.append((sk, ev)))
    yield
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old_queue)
    _webapp._CHAT_QUEUE_FILE = old_file
    _webapp._live_turns.clear()
    _webapp._live_turns.update(old_turns)


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
def chat_app(fake_ctx):
    from aiohttp import web

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "ok"}

    fake_ctx["run_engine"] = fake_engine

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    app.router.add_post("/api/projects/{id}/chat/steer", _webapp.api_chat_steer)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _arm_live_turn(fake_ctx, session_key="1001:42"):
    """Puts the session into 'running on the live client' state. Returns the fake client."""
    client = _FakeClient()
    fake_ctx["running"][session_key] = client
    fake_ctx["live_clients"][session_key] = _FakeEntry(client)
    return client


async def _read_sse_events(resp) -> list:
    body = await resp.read()
    events = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass
    return events


# ─────────────────────────── _try_steer_mid_turn guards ───────────────────────


@pytest.mark.asyncio
async def test_steer_helper_injects_on_live_client_turn(fake_ctx):
    client = _arm_live_turn(fake_ctx)
    ok = await _webapp._try_steer_mid_turn(fake_ctx, "1001:42", "go left", None)
    assert ok is True
    assert client.queries == ["go left"]


@pytest.mark.asyncio
async def test_steer_helper_rejects_true_placeholder(fake_ctx):
    """running=True (turn reserved, engine not started) must NOT be steered."""
    fake_ctx["running"]["1001:42"] = True
    fake_ctx["live_clients"]["1001:42"] = _FakeEntry(_FakeClient())
    ok = await _webapp._try_steer_mid_turn(fake_ctx, "1001:42", "go left", None)
    assert ok is False


@pytest.mark.asyncio
async def test_steer_helper_rejects_fresh_client_turn(fake_ctx):
    """A plan/ask gated turn runs on a FRESH client (running is not entry.client) → no steer."""
    fake_ctx["running"]["1001:42"] = _FakeClient()          # the gated turn's fresh client
    live = _FakeClient()
    fake_ctx["live_clients"]["1001:42"] = _FakeEntry(live)  # idle live client
    ok = await _webapp._try_steer_mid_turn(fake_ctx, "1001:42", "go left", None)
    assert ok is False
    assert live.queries == []


@pytest.mark.asyncio
async def test_steer_helper_rejects_chat_id_mismatch(fake_ctx):
    """The in-flight turn belongs to another chat of the same project → no cross-injection."""
    client = _arm_live_turn(fake_ctx)
    _webapp._live_turns["1001:42"] = {"chat_id": "aaaaaa", "seq": 0}
    ok = await _webapp._try_steer_mid_turn(fake_ctx, "1001:42", "go left", "bbbbbb")
    assert ok is False
    assert client.queries == []


@pytest.mark.asyncio
async def test_steer_helper_flag_off(fake_ctx, monkeypatch):
    monkeypatch.setattr(_webapp, "STEER_MID_TURN", 0)
    client = _arm_live_turn(fake_ctx)
    ok = await _webapp._try_steer_mid_turn(fake_ctx, "1001:42", "go left", None)
    assert ok is False
    assert client.queries == []


@pytest.mark.asyncio
async def test_steer_helper_query_failure_falls_back(fake_ctx):
    """A failing client.query must report non-steered so the caller queues instead."""
    client = _arm_live_turn(fake_ctx)

    async def _boom(prompt, session_id="default"):
        raise RuntimeError("transport closed")

    client.query = _boom
    ok = await _webapp._try_steer_mid_turn(fake_ctx, "1001:42", "go left", None)
    assert ok is False


# ─────────────────────────── POST /chat busy branch ───────────────────────────


@pytest.mark.asyncio
async def test_chat_busy_steers_into_live_client(aiohttp_client, fake_ctx, chat_app):
    """POST /chat while busy on a live-client turn → SSE type=steered, queue untouched."""
    session_key = "1001:42"
    client_obj = _arm_live_turn(fake_ctx, session_key)
    _webapp._chat_queue_init(fake_ctx)

    client = await aiohttp_client(chat_app)
    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "steer me"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 200
    events = await _read_sse_events(resp)
    assert [e for e in events if e.get("type") == "steered"], f"no steered frame: {events}"
    assert [e for e in events if e.get("type") == "queued"] == []
    assert client_obj.queries == ["steer me"]
    assert _webapp._chat_queue_get(session_key) == []


@pytest.mark.asyncio
async def test_chat_busy_placeholder_still_queues(aiohttp_client, fake_ctx, chat_app):
    """Regression: running=True placeholder keeps the pre-086 queue behaviour."""
    session_key = "1001:42"
    fake_ctx["running"][session_key] = True
    _webapp._chat_queue_init(fake_ctx)

    client = await aiohttp_client(chat_app)
    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "queue me"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 200
    events = await _read_sse_events(resp)
    assert [e for e in events if e.get("type") == "queued"], f"no queued frame: {events}"
    assert any(i["text"] == "queue me" for i in _webapp._chat_queue_get(session_key))


# ─────────────────────────── POST /chat/steer endpoint ────────────────────────


@pytest.mark.asyncio
async def test_steer_endpoint_steers(aiohttp_client, fake_ctx, chat_app):
    client_obj = _arm_live_turn(fake_ctx)
    _webapp._chat_queue_init(fake_ctx)

    client = await aiohttp_client(chat_app)
    resp = await client.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "focus on tests"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"steered": True}
    assert client_obj.queries == ["focus on tests"]
    assert _webapp._chat_queue_get("1001:42") == []


@pytest.mark.asyncio
async def test_steer_endpoint_falls_back_to_queue(aiohttp_client, fake_ctx, chat_app):
    """No live-client turn → the endpoint enqueues (201) so the caller can fire-and-forget."""
    fake_ctx["running"]["1001:42"] = True  # busy, but not steerable
    _webapp._chat_queue_init(fake_ctx)

    client = await aiohttp_client(chat_app)
    resp = await client.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "later please"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["steered"] is False
    assert body["item"]["text"] == "later please"
    assert any(i["text"] == "later please" for i in _webapp._chat_queue_get("1001:42"))


@pytest.mark.asyncio
async def test_steer_endpoint_publishes_bus_event(aiohttp_client, fake_ctx, chat_app, monkeypatch):
    """The steered message reaches the activity bus as a kind:'steer' event (single-writer canvas)."""
    published: list = []
    real_publish = _webapp._bus_publish

    def _spy(session_key, event, persist=True):
        published.append((session_key, event))
        return real_publish(session_key, event, persist=persist)

    monkeypatch.setattr(_webapp, "_bus_publish", _spy)
    _arm_live_turn(fake_ctx)
    _webapp._chat_queue_init(fake_ctx)

    client = await aiohttp_client(chat_app)
    resp = await client.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "check edge cases", "chat_id": "abc123"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 200
    steer_events = [ev for _, ev in published if ev.get("kind") == "steer"]
    assert len(steer_events) == 1
    assert steer_events[0]["text"] == "check edge cases"
    assert steer_events[0]["chat_id"] == "abc123"

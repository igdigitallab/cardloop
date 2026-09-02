"""
Tests for spec-089 §5: urgent send (interrupt-and-send for local CLI commands).

A local CLI command typed mid-turn (/goal clear, /clear, /compact, /model, /effort, /mcp)
only takes effect at a turn boundary — steer-injection weaves it into the CLI's own turn,
not a boundary, and the ordinary chat queue waits behind the current turn AND everything
already queued. A Stop-hook loop (/goal) never yields a boundary on its own, so both paths
can leave the command sitting for as long as the loop runs.

POST /api/projects/{id}/chat/steer with "urgent": true takes a third path: interrupt
whatever is running now (same effect as the Stop button — and recorded as the SAME
operator_stop timeline event) and enqueue at the HEAD of the chat queue, ahead of
whatever was already there, so it drains first.

Covers:
- urgent with a running interruptible client → interrupt() awaited once, operator_stop
  timeline record, item lands at queue index 0 ahead of pre-queued items, redrain
  triggered, response shape
- urgent during a drain-surfaced bg turn (no ctx["running"] client, but the live client is
  reachable) → the live client is interrupted the same way /chat/stop reaches it
- urgent while idle → no interrupt, no operator_stop record, still enqueued at head
- non-urgent body is untouched (no "urgent"/"interrupted" keys, no interrupt attempted)
- _chat_queue_enqueue(front=True) respects the max-depth cap and flushes to disk
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token

SESSION_KEY = "1001:42"


# ─────────────────────────── Fakes ────────────────────────────────────────────


class _FakeClient:
    """Stands in for a connected ClaudeSDKClient: records interrupt()/query() calls."""

    def __init__(self, raises: bool = False):
        self.interrupts = 0
        self.queries: list = []
        self._raises = raises

    async def interrupt(self):
        self.interrupts += 1
        if self._raises:
            raise RuntimeError("subprocess already gone")

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)


class _FakeEntry:
    """Stands in for engine._LiveEntry — only .client is read by the resolver/steer guard."""

    def __init__(self, client):
        self.client = client


# ─────────────────────────── Fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate chat queue, live turns, bg-turn registries and timeline writes between tests."""
    old_file = _webapp._CHAT_QUEUE_FILE
    old_queue = dict(_webapp._CHAT_QUEUE)
    old_turns = dict(_webapp._live_turns)
    old_opts = dict(_webapp._last_turn_options)
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = tmp_path / "chat-queue.json"
    _webapp._live_turns.clear()
    _webapp._last_turn_options.clear()
    _webapp._bg_run_ids.pop(SESSION_KEY, None)
    _webapp._bg_run_started.pop(SESSION_KEY, None)
    timeline: list = []
    monkeypatch.setattr(_webapp, "_timeline_append", lambda sk, ev: timeline.append((sk, ev)))
    yield timeline
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old_queue)
    _webapp._CHAT_QUEUE_FILE = old_file
    _webapp._live_turns.clear()
    _webapp._live_turns.update(old_turns)
    _webapp._last_turn_options.clear()
    _webapp._last_turn_options.update(old_opts)
    _webapp._bg_run_ids.pop(SESSION_KEY, None)
    _webapp._bg_run_started.pop(SESSION_KEY, None)


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    ctx = {
        "topics": {
            SESSION_KEY: {
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
    app.router.add_post("/api/projects/{id}/chat/stop", _webapp.api_project_chat_stop)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _spy_redrain(monkeypatch):
    """Replace _chat_queue_redrain_soon with a fast no-op spy (real one sleeps 0.4s)."""
    calls: list = []

    async def _fake(ctx, session_key, delay=0.4):
        calls.append((ctx, session_key))

    monkeypatch.setattr(_webapp, "_chat_queue_redrain_soon", _fake)
    return calls


# ─────────────────────────── _resolve_interruptible_client ────────────────────


def test_resolve_returns_live_client(fake_ctx):
    client = _FakeClient()
    fake_ctx["running"][SESSION_KEY] = client
    assert _webapp._resolve_interruptible_client(fake_ctx, SESSION_KEY) is client


def test_resolve_returns_none_for_true_placeholder(fake_ctx):
    fake_ctx["running"][SESSION_KEY] = True
    assert _webapp._resolve_interruptible_client(fake_ctx, SESSION_KEY) is None


def test_resolve_falls_back_to_live_client_during_bg_turn(fake_ctx):
    """No ctx['running'] entry, but a bg turn owns the CLI — reach the live client."""
    client = _FakeClient()
    fake_ctx["live_clients"][SESSION_KEY] = _FakeEntry(client)
    _webapp._bg_run_ids[SESSION_KEY] = "abc123"
    _webapp._bg_run_started[SESSION_KEY] = time.monotonic()
    assert _webapp._resolve_interruptible_client(fake_ctx, SESSION_KEY) is client


def test_resolve_returns_none_when_idle(fake_ctx):
    assert _webapp._resolve_interruptible_client(fake_ctx, SESSION_KEY) is None


# ─────────────────────────── urgent steer: running client ─────────────────────


@pytest.mark.asyncio
async def test_urgent_interrupts_running_client_and_queues_at_head(
    aiohttp_client, fake_ctx, chat_app, monkeypatch, reset_state
):
    client = _FakeClient()
    fake_ctx["running"][SESSION_KEY] = client
    _webapp._chat_queue_init(fake_ctx)
    # Two messages already queued (normal order) before the urgent send arrives.
    _webapp._chat_queue_enqueue(SESSION_KEY, "first", None, "myproject")
    _webapp._chat_queue_enqueue(SESSION_KEY, "second", None, "myproject")
    redrain_calls = _spy_redrain(monkeypatch)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "/goal clear", "urgent": True, "msg_id": "m1"},
        headers=_auth_headers(fake_ctx),
    )

    assert resp.status == 201
    body = await resp.json()
    assert body["steered"] is False
    assert body["urgent"] is True
    assert body["interrupted"] is True
    assert body["item"]["text"] == "/goal clear"

    assert client.interrupts == 1
    # operator_stop recorded — same event the Stop button records.
    assert (SESSION_KEY, {"kind": "operator_stop"}) in reset_state

    # Head of queue: urgent item first, pre-queued items preserved behind it in order.
    q = _webapp._chat_queue_get(SESSION_KEY)
    assert [i["text"] for i in q] == ["/goal clear", "first", "second"]

    assert redrain_calls == [(fake_ctx, SESSION_KEY)]


@pytest.mark.asyncio
async def test_urgent_carries_last_turn_options_for_fingerprint_stability(
    aiohttp_client, fake_ctx, chat_app, monkeypatch, reset_state
):
    """The drained item must carry effort/ultracode from the interrupted turn (mirrors
    _completion_wake_fire) so the drain does not evict the persistent client."""
    client = _FakeClient()
    fake_ctx["running"][SESSION_KEY] = client
    _webapp._last_turn_options[SESSION_KEY] = {"effort": "high", "ultracode": True}
    _webapp._chat_queue_init(fake_ctx)
    _spy_redrain(monkeypatch)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "/clear", "urgent": True},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["item"]["effort"] == "high"
    assert body["item"]["ultracode"] is True


# ─────────────────────────── urgent steer: bg turn lane ───────────────────────


@pytest.mark.asyncio
async def test_urgent_during_bg_turn_interrupts_live_client(
    aiohttp_client, fake_ctx, chat_app, monkeypatch, reset_state
):
    """No ctx['running'] client (the engine popped that slot at the operator turn's end),
    but a drain-surfaced bg turn owns the CLI — the live client must still be interrupted."""
    client = _FakeClient()
    fake_ctx["live_clients"][SESSION_KEY] = _FakeEntry(client)
    _webapp._bg_run_ids[SESSION_KEY] = "abc123"
    _webapp._bg_run_started[SESSION_KEY] = time.monotonic()
    _webapp._chat_queue_init(fake_ctx)
    _spy_redrain(monkeypatch)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "/goal clear", "urgent": True},
        headers=_auth_headers(fake_ctx),
    )

    assert resp.status == 201
    body = await resp.json()
    assert body["interrupted"] is True
    assert client.interrupts == 1
    assert (SESSION_KEY, {"kind": "operator_stop"}) in reset_state
    assert [i["text"] for i in _webapp._chat_queue_get(SESSION_KEY)] == ["/goal clear"]


# ─────────────────────────── urgent steer: idle ───────────────────────────────


@pytest.mark.asyncio
async def test_urgent_while_idle_does_not_interrupt_but_still_enqueues_at_head(
    aiohttp_client, fake_ctx, chat_app, monkeypatch, reset_state
):
    _webapp._chat_queue_init(fake_ctx)
    _spy_redrain(monkeypatch)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "/goal clear", "urgent": True},
        headers=_auth_headers(fake_ctx),
    )

    assert resp.status == 201
    body = await resp.json()
    assert body["interrupted"] is False
    # Nothing to interrupt → no operator_stop record.
    assert reset_state == []
    assert [i["text"] for i in _webapp._chat_queue_get(SESSION_KEY)] == ["/goal clear"]


@pytest.mark.asyncio
async def test_urgent_swallows_a_failing_interrupt(
    aiohttp_client, fake_ctx, chat_app, monkeypatch, reset_state
):
    """A dead subprocess raising from interrupt() must not surface as a 500 (same contract
    as /chat/stop) — the queued item still lands at the head."""
    client = _FakeClient(raises=True)
    fake_ctx["running"][SESSION_KEY] = client
    _webapp._chat_queue_init(fake_ctx)
    _spy_redrain(monkeypatch)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "/goal clear", "urgent": True},
        headers=_auth_headers(fake_ctx),
    )

    assert resp.status == 201
    body = await resp.json()
    assert body["interrupted"] is True  # a client WAS found and interrupt() WAS called
    assert client.interrupts == 1
    assert [i["text"] for i in _webapp._chat_queue_get(SESSION_KEY)] == ["/goal clear"]


# ─────────────────────────── non-urgent body is untouched ─────────────────────


@pytest.mark.asyncio
async def test_non_urgent_response_has_no_urgent_keys(
    aiohttp_client, fake_ctx, chat_app, reset_state
):
    """Regression pin: default (no 'urgent' field, or urgent:false) must behave exactly
    like pre-spec-089 — no interrupt attempted, no 'urgent'/'interrupted' in the body."""
    fake_ctx["running"][SESSION_KEY] = True  # busy, not steerable
    _webapp._chat_queue_init(fake_ctx)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "later please"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body == {"steered": False, "item": body["item"]}
    assert "urgent" not in body
    assert "interrupted" not in body
    assert reset_state == []  # no operator_stop — nothing was interrupted


@pytest.mark.asyncio
async def test_urgent_false_explicit_takes_the_normal_path(
    aiohttp_client, fake_ctx, chat_app, reset_state
):
    client = _FakeClient()
    fake_ctx["running"][SESSION_KEY] = client
    fake_ctx["live_clients"][SESSION_KEY] = _FakeEntry(client)
    _webapp._chat_queue_init(fake_ctx)

    http = await aiohttp_client(chat_app)
    resp = await http.post(
        "/api/projects/myproject/chat/steer",
        json={"text": "focus on tests", "urgent": False},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"steered": True}
    assert client.interrupts == 0
    assert client.queries == ["focus on tests"]


# ─────────────────────────── _chat_queue_enqueue(front=True) ──────────────────


def test_enqueue_front_inserts_at_head(fake_ctx):
    _webapp._chat_queue_init(fake_ctx)
    _webapp._chat_queue_enqueue(SESSION_KEY, "a")
    _webapp._chat_queue_enqueue(SESSION_KEY, "b")
    _webapp._chat_queue_enqueue(SESSION_KEY, "urgent", front=True)
    assert [i["text"] for i in _webapp._chat_queue_get(SESSION_KEY)] == ["urgent", "a", "b"]


def test_enqueue_front_respects_max_depth(fake_ctx):
    _webapp._chat_queue_init(fake_ctx)
    for i in range(_webapp._CHAT_QUEUE_MAX):
        assert _webapp._chat_queue_enqueue(SESSION_KEY, f"item{i}") is not None
    assert _webapp._chat_queue_enqueue(SESSION_KEY, "overflow", front=True) is None
    assert len(_webapp._chat_queue_get(SESSION_KEY)) == _webapp._CHAT_QUEUE_MAX
    assert _webapp._chat_queue_get(SESSION_KEY)[0]["text"] == "item0"


def test_enqueue_front_flushes_to_disk(fake_ctx):
    _webapp._chat_queue_init(fake_ctx)
    _webapp._chat_queue_enqueue(SESSION_KEY, "a")
    _webapp._chat_queue_enqueue(SESSION_KEY, "urgent", front=True)
    raw = json.loads(_webapp._CHAT_QUEUE_FILE.read_text(encoding="utf-8"))
    assert [i["text"] for i in raw[SESSION_KEY]] == ["urgent", "a"]

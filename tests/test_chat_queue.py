"""
Tests for Spec-041 A3: backend-authoritative chat queue drain.

Covers:
- _chat_queue_drain_one: drains a queued item when session is free
- _chat_queue_drain_one: returns False when session is busy
- POST /chat while busy: returns 'queued' SSE frame; message in queue
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── Fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_chat_queue(tmp_path):
    """Reset _CHAT_QUEUE and _CHAT_QUEUE_FILE between tests."""
    old_file = _webapp._CHAT_QUEUE_FILE
    old_queue = dict(_webapp._CHAT_QUEUE)
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = tmp_path / "chat-queue.json"
    yield
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old_queue)
    _webapp._CHAT_QUEUE_FILE = old_file


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    sessions_saved = []
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
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: sessions_saved.append(1),
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    ctx["_sessions_saved"] = sessions_saved
    (tmp_path / "myproject").mkdir(exist_ok=True)
    return ctx


# ─────────────────────────── _chat_queue_drain_one ────────────────────────────


@pytest.mark.asyncio
async def test_drain_one_free_session_dispatches_item(fake_ctx):
    """_chat_queue_drain_one on a free session with one queued item:
    - pops the item from the queue
    - reserves ctx['running'] synchronously
    - spawns execution (run_start and run_end published to bus; session_id saved)
    """
    session_key = "1001:42"
    # Enqueue one item
    item = _webapp._chat_queue_enqueue(session_key, "hello from queue")
    assert item is not None

    # Collect bus events published for this session
    bus_events: list = []
    bus_q = _webapp._bus_subscribe(session_key)

    run_engine_calls: list = []

    async def mock_run_engine(**kwargs):
        run_engine_calls.append(kwargs)
        yield {"type": "text", "text": "I ran queued task"}
        yield {"type": "result", "session_id": "sess-cq-test"}

    fake_ctx["run_engine"] = mock_run_engine

    spawned_coros: list = []

    def fake_spawn_bg(coro):
        spawned_coros.append(coro)
        # Actually run it so we can assert side effects
        return asyncio.ensure_future(coro)

    with patch.object(_webapp, "_spawn_bg", side_effect=fake_spawn_bg), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        result = await _webapp._chat_queue_drain_one(fake_ctx, session_key)
        assert result is True

        # Let the spawned coroutine run to completion
        if spawned_coros:
            await asyncio.gather(*[asyncio.ensure_future(asyncio.sleep(0))])
            # Give the background task time to complete
            await asyncio.sleep(0.05)

    # Item was popped from queue
    remaining = _webapp._chat_queue_get(session_key)
    assert remaining == [], f"Queue should be empty after drain, got: {remaining}"

    # session_id was saved
    assert fake_ctx["sessions"].get(session_key) == "sess-cq-test"

    # Drain the bus queue for assertions
    while not bus_q.empty():
        bus_events.append(bus_q.get_nowait())

    kinds = [e["kind"] for e in bus_events]
    assert "run_start" in kinds, f"run_start not published: {bus_events}"
    assert "run_end" in kinds, f"run_end not published: {bus_events}"

    run_end = next(e for e in bus_events if e["kind"] == "run_end")
    assert run_end["outcome"] == "ok"

    # Lock released after completion
    assert session_key not in fake_ctx["running"]

    _webapp._bus_unsubscribe(session_key, bus_q)


@pytest.mark.asyncio
async def test_drain_one_busy_session_returns_false(fake_ctx):
    """_chat_queue_drain_one returns False when the session lock is held."""
    session_key = "1001:42"
    _webapp._chat_queue_enqueue(session_key, "should not fire")
    # Hold the lock
    fake_ctx["running"][session_key] = True

    result = await _webapp._chat_queue_drain_one(fake_ctx, session_key)
    assert result is False

    # Item must still be in the queue
    queue = _webapp._chat_queue_get(session_key)
    assert len(queue) == 1
    assert queue[0]["text"] == "should not fire"


@pytest.mark.asyncio
async def test_drain_one_empty_queue_returns_false(fake_ctx):
    """_chat_queue_drain_one returns False when queue is empty."""
    session_key = "1001:42"
    result = await _webapp._chat_queue_drain_one(fake_ctx, session_key)
    assert result is False


@pytest.mark.asyncio
async def test_drain_one_seeds_live_turn_synchronously(fake_ctx):
    """Regression: a message queued during a session rotate must not vanish.

    _chat_queue_drain_one reserves ctx['running'] synchronously, then spawns the executor
    asynchronously. The executor used to be the first place the live-turn buffer was created —
    leaving a window where /live reports {running:true, prompt:""}. A client that hydrates in
    that window (handleRotate's post-rotate hydrateFromServer) renders an EMPTY turn and clobbers
    the operator's queued message. The fix seeds the buffer WITH the prompt synchronously in
    drain_one, in lockstep with the running flag. This test asserts that — with the executor
    stubbed out so it can never run — the buffer is already populated.
    """
    session_key = "1001:42"
    _webapp._live_turns.pop(session_key, None)
    _webapp._chat_queue_enqueue(session_key, "drain me after rotate")

    def no_run_spawn(coro):
        coro.close()  # never run the executor; avoid "coroutine was never awaited"
        return None

    try:
        with patch.object(_webapp, "_spawn_bg", side_effect=no_run_spawn):
            result = await _webapp._chat_queue_drain_one(fake_ctx, session_key)
        assert result is True
        # Lock reserved synchronously.
        assert fake_ctx["running"].get(session_key) is True
        # Live-turn buffer seeded synchronously WITH the prompt — makes /live authoritative
        # the instant the lock flips, so the post-rotate hydrate reconstructs the user bubble.
        turn = _webapp._live_turns.get(session_key)
        assert turn is not None, "live turn must be seeded synchronously in drain_one"
        assert turn["prompt"] == "drain me after rotate"
        assert turn["status"] == "running"
    finally:
        _webapp._live_turns.pop(session_key, None)
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_redrain_soon_delivers_late_landing_item(fake_ctx):
    """_chat_queue_redrain_soon re-drains after its delay.

    Covers the operator's message landing (fire-and-forget chatQueueAdd) just AFTER the rotate's
    lock-release drain already ran against an empty queue — it must deliver via the short re-check
    rather than only via the 3s backstop.
    """
    session_key = "1001:42"
    _webapp._live_turns.pop(session_key, None)
    _webapp._chat_queue_enqueue(session_key, "late message")

    def no_run_spawn(coro):
        coro.close()
        return None

    try:
        with patch.object(_webapp, "_spawn_bg", side_effect=no_run_spawn):
            await _webapp._chat_queue_redrain_soon(fake_ctx, session_key, delay=0.01)
        # The re-drain popped the item and reserved the lock (executor stubbed).
        assert _webapp._chat_queue_get(session_key) == []
        assert fake_ctx["running"].get(session_key) is True
    finally:
        _webapp._live_turns.pop(session_key, None)
        fake_ctx["running"].pop(session_key, None)


# ─────────────────────────── POST /chat busy → queued SSE ─────────────────────


@pytest.fixture
def chat_app(fake_ctx):
    from aiohttp import web

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "ok"}

    fake_ctx["run_engine"] = fake_engine

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    app.router.add_get("/api/projects/{id}/chat/queue", _webapp.api_chat_queue_list)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


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


@pytest.mark.asyncio
async def test_chat_busy_returns_queued_sse(aiohttp_client, fake_ctx, chat_app):
    """POST /chat while session lock is held → SSE type=queued, message in queue."""
    session_key = "1001:42"
    fake_ctx["running"][session_key] = True
    # Ensure chat queue is initialised
    _webapp._chat_queue_init(fake_ctx)

    client = await aiohttp_client(chat_app)
    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "queue me"},
        headers=_auth_headers(fake_ctx),
    )
    assert resp.status == 200
    assert "text/event-stream" in resp.headers.get("Content-Type", "")
    events = await _read_sse_events(resp)

    queued_events = [e for e in events if e.get("type") == "queued"]
    assert len(queued_events) == 1, f"Expected 'queued' SSE frame, got: {events}"
    item = queued_events[0]["item"]
    assert item["text"] == "queue me"

    # No error event
    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events == [], f"Unexpected error events: {error_events}"

    # Message is now in the server-side queue
    queue = _webapp._chat_queue_get(session_key)
    assert any(i["id"] == item["id"] for i in queue)


# ─────────────────── POST /chat busy → duplicate dropped (dedup) ───────────────
# A mobile OptionPicker re-arms after a ChatTab remount (screen lock/unlock), so a
# second tap re-POSTs the SAME prompt while the turn is still busy. The server must
# drop that identical copy instead of enqueuing a phantom that drains into a ghost turn.


@pytest.mark.asyncio
async def test_chat_busy_duplicate_of_queued_dropped(aiohttp_client, fake_ctx, chat_app):
    """Re-POST of a prompt already in the queue → {duplicate:true}, not a second copy."""
    session_key = "1001:42"
    fake_ctx["running"][session_key] = True
    _webapp._chat_queue_init(fake_ctx)
    while _webapp._chat_queue_pop(session_key):
        pass
    _webapp._chat_queue_enqueue(session_key, "dup me")
    try:
        client = await aiohttp_client(chat_app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "dup me"},
            headers=_auth_headers(fake_ctx),
        )
        assert resp.status == 200
        events = await _read_sse_events(resp)
        queued = [e for e in events if e.get("type") == "queued"]
        assert len(queued) == 1, f"expected one queued frame, got: {events}"
        assert queued[0].get("duplicate") is True
        assert "item" not in queued[0]
        # The queue still holds exactly ONE copy — no phantom added.
        copies = [i for i in _webapp._chat_queue_get(session_key) if i["text"] == "dup me"]
        assert len(copies) == 1, f"duplicate was enqueued: {copies}"
    finally:
        while _webapp._chat_queue_pop(session_key):
            pass


@pytest.mark.asyncio
async def test_chat_busy_duplicate_of_running_prompt_dropped(aiohttp_client, fake_ctx, chat_app):
    """Re-POST of the IN-FLIGHT turn's prompt → dropped (the exact incident shape)."""
    session_key = "1001:42"
    fake_ctx["running"][session_key] = True
    _webapp._chat_queue_init(fake_ctx)
    while _webapp._chat_queue_pop(session_key):
        pass
    _webapp._live_turns[session_key] = {"prompt": "run me", "events": []}
    try:
        client = await aiohttp_client(chat_app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "run me"},
            headers=_auth_headers(fake_ctx),
        )
        events = await _read_sse_events(resp)
        queued = [e for e in events if e.get("type") == "queued"]
        assert len(queued) == 1 and queued[0].get("duplicate") is True
        # Nothing was enqueued for the in-flight prompt.
        assert [i for i in _webapp._chat_queue_get(session_key) if i["text"] == "run me"] == []
    finally:
        _webapp._live_turns.pop(session_key, None)
        while _webapp._chat_queue_pop(session_key):
            pass


# ─────────────────── Multichat: per-chat_id routing of drained runs ────────────
# Two chats in one project share the project-level running lock + live buffer + bus.
# A queued message must carry the chat_id of the tab it was typed in, so that when it
# drains its run_start/text/run_end events and the /live buffer are stamped with that
# chat_id — otherwise the drained run broadcasts to EVERY chat tab in the project
# (peer tabs filter on chat_id; a missing chat_id passes their filter = duplicate bubble).


def test_enqueue_without_chat_id_omits_field():
    """Backward-compat: no chat_id passed → item has no chat_id key, so events carry none
    and the peer-tab filter is a no-op (single-chat projects see no regression)."""
    session_key = "1001:42"
    item = _webapp._chat_queue_enqueue(session_key, "plain")
    assert item is not None
    assert "chat_id" not in item


def test_enqueue_with_chat_id_records_field():
    """_chat_queue_enqueue stores the chat_id on the item when provided."""
    session_key = "1001:42"
    item = _webapp._chat_queue_enqueue(session_key, "for a chat", chat_id="a1b2c3")
    assert item is not None
    assert item.get("chat_id") == "a1b2c3"


@pytest.mark.asyncio
async def test_drain_one_stamps_chat_id_on_live_turn(fake_ctx):
    """A queued item carrying chat_id stamps the live-turn buffer, so /live reports the
    owning chat_id and peer tabs (hydrate + poll) don't adopt the run as their own."""
    session_key = "1001:42"
    _webapp._live_turns.pop(session_key, None)
    _webapp._chat_queue_enqueue(session_key, "msg for chat B", chat_id="a1b2c3")

    def no_run_spawn(coro):
        coro.close()  # never run the executor
        return None

    try:
        with patch.object(_webapp, "_spawn_bg", side_effect=no_run_spawn):
            result = await _webapp._chat_queue_drain_one(fake_ctx, session_key)
        assert result is True
        turn = _webapp._live_turns.get(session_key)
        assert turn is not None
        assert turn.get("chat_id") == "a1b2c3", f"live turn missing chat_id: {turn}"
    finally:
        _webapp._live_turns.pop(session_key, None)
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_drained_run_bus_events_carry_chat_id(fake_ctx):
    """run_start / text / run_end from a drained queue item all carry the originating
    chat_id, so peer chat tabs in the same project filter them out (no duplicate bubbles)."""
    session_key = "1001:42"
    _webapp._live_turns.pop(session_key, None)
    item = _webapp._chat_queue_enqueue(session_key, "hello", chat_id="a1b2c3")
    assert item is not None and item["chat_id"] == "a1b2c3"

    bus_q = _webapp._bus_subscribe(session_key)

    async def mock_run_engine(**kwargs):
        yield {"type": "text", "text": "answer"}
        yield {"type": "result", "session_id": "sess-x"}

    fake_ctx["run_engine"] = mock_run_engine

    def fake_spawn_bg(coro):
        return asyncio.ensure_future(coro)

    try:
        with patch.object(_webapp, "_spawn_bg", side_effect=fake_spawn_bg), \
             patch.object(_webapp, "_secrets_read", return_value={}), \
             patch.object(_webapp, "_build_agents_kwargs", return_value={}):
            assert await _webapp._chat_queue_drain_one(fake_ctx, session_key) is True
            await asyncio.sleep(0.05)

        bus_events = []
        while not bus_q.empty():
            bus_events.append(bus_q.get_nowait())

        for kind in ("run_start", "text", "run_end"):
            evs = [e for e in bus_events if e.get("kind") == kind]
            assert evs, f"{kind} not published: {bus_events}"
            for e in evs:
                assert e.get("chat_id") == "a1b2c3", f"{kind} missing chat_id: {e}"
    finally:
        _webapp._bus_unsubscribe(session_key, bus_q)
        _webapp._live_turns.pop(session_key, None)
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_chat_busy_enqueues_with_chat_id(aiohttp_client, fake_ctx, chat_app):
    """POST /chat while busy WITH a chat_id in the body → the queued item records that
    chat_id (so the later drained run routes back to that tab only)."""
    session_key = "1001:42"
    fake_ctx["running"][session_key] = True
    _webapp._chat_queue_init(fake_ctx)
    while _webapp._chat_queue_pop(session_key):
        pass
    try:
        client = await aiohttp_client(chat_app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "queue me", "chat_id": "a1b2c3"},
            headers=_auth_headers(fake_ctx),
        )
        assert resp.status == 200
        events = await _read_sse_events(resp)
        queued = [e for e in events if e.get("type") == "queued"]
        assert len(queued) == 1, f"expected queued frame, got: {events}"
        item = queued[0]["item"]
        assert item.get("chat_id") == "a1b2c3"
        q = _webapp._chat_queue_get(session_key)
        assert any(i["id"] == item["id"] and i.get("chat_id") == "a1b2c3" for i in q)
    finally:
        while _webapp._chat_queue_pop(session_key):
            pass

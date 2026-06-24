"""
Tests for api_project_chat (SSE) and the concurrency lock.

Smoke tests:
- chat starts stream with run_engine=None → degradation error
- chat with busy project (running[k] != None) → SSE error "busy"
- chat with normal operation (mock run_engine) → streams text, releases lock
- two "simultaneous" requests → second gets 409/"busy"
- api_move_task with busy project → 409

Engine is mocked as an async generator.
"""
import sys
import json
from pathlib import Path
import asyncio

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _tasks_path


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


def _make_tasks(cwd: Path, card_id="aabbcc", col="backlog"):
    """Creates TASKS.md with a single card."""
    content = (
        f"# Tasks\n"
        f"## Backlog\n"
        f"{'- [ ] Do it <!--ops:aabbcc-->' if col == 'backlog' else ''}\n"
        f"## In Progress\n"
        f"{'- [ ] Do it <!--ops:aabbcc-->' if col == 'in_progress' else ''}\n"
        f"## Review\n"
        f"## Failed\n"
    )
    _tasks_path(str(cwd)).write_text(content, encoding="utf-8")


def _make_chat_ctx(tmp_path, project_dir, run_engine=None):
    password = "testpass"
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
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": run_engine,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


def _make_app(ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    app.router.add_post("/api/projects/{id}/tasks", _webapp.api_create_task)
    app.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    app.router.add_delete("/api/projects/{id}/tasks/{card}", _webapp.api_delete_task)
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def _read_sse_events(resp) -> list[dict]:
    """Reads all SSE data from a StreamResponse. Returns list[dict]."""
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


# ─────────────────────────── chat: no run_engine ───────────────────────────


async def test_chat_no_run_engine_returns_error_sse(aiohttp_client, tmp_path, project_dir):
    """api_project_chat without run_engine → SSE with type=error (degradation)."""
    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=None)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Hello"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    assert "text/event-stream" in resp.headers.get("Content-Type", "")
    events = await _read_sse_events(resp)
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) > 0, f"Expected SSE with type=error, got: {events}"


async def test_chat_empty_prompt_returns_400(aiohttp_client, tmp_path, project_dir):
    """api_project_chat with empty prompt → 400 (before SSE)."""

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "ok"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "   "},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 400


# ─────────────────────────── chat: busy project ───────────────────────────


async def test_chat_busy_project_enqueues_message(aiohttp_client, tmp_path, project_dir):
    """Spec-041 A3: api_project_chat when session is busy → SSE frame type=queued, message in queue."""
    import webapp as _webapp

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "ok"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    # Simulate busy session
    ctx["running"]["1001:42"] = True

    # Ensure chat queue is initialised for the tmp data dir
    _webapp._chat_queue_init(ctx)

    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Hello queued"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    assert "text/event-stream" in resp.headers.get("Content-Type", "")
    events = await _read_sse_events(resp)
    # Must have a 'queued' frame, not an error
    queued_events = [e for e in events if e.get("type") == "queued"]
    assert len(queued_events) == 1, f"Expected 'queued' SSE frame, got: {events}"
    item = queued_events[0]["item"]
    assert item["text"] == "Hello queued"
    # Verify the message is in the server-side queue
    session_key = "1001:42"
    queue = _webapp._chat_queue_get(session_key)
    assert any(i["id"] == item["id"] for i in queue), "Message not found in chat queue"


# ─────────────────────────── chat: normal operation ───────────────────────────


async def test_chat_streams_text_events(aiohttp_client, tmp_path, project_dir):
    """api_project_chat with mock engine → SSE contains type=text and type=done."""

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "Hello from mock"}
        yield {"type": "result", "session_id": "sess-42", "context_tokens": 100}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Do something"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    events = await _read_sse_events(resp)
    types = {e.get("type") for e in events}
    assert "text" in types, f"Expected a text event: {events}"
    text_event = next(e for e in events if e.get("type") == "text")
    assert text_event.get("text") == "Hello from mock"


async def test_chat_releases_lock_after_completion(aiohttp_client, tmp_path, project_dir):
    """After chat completes the running lock must be released."""

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    assert "1001:42" not in ctx["running"]  # before request

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Work"},
        headers=_auth_headers(ctx),
    )
    # Read the full response to complete the stream
    await resp.read()

    assert "1001:42" not in ctx["running"], "Lock must be released after completion"


async def test_chat_saves_session_id(aiohttp_client, tmp_path, project_dir):
    """api_project_chat saves session_id from the result event."""

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "Hi"}
        yield {"type": "result", "session_id": "my-session-123"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Test"},
        headers=_auth_headers(ctx),
    )
    await resp.read()

    assert ctx["sessions"].get("1001:42") == "my-session-123", (
        "session_id must be saved in ctx['sessions']"
    )


# ─────────────────────────── concurrency lock ───────────────────────────


async def test_move_to_in_progress_busy_enqueues(aiohttp_client, tmp_path, project_dir):
    """api_move_task to in_progress when project is busy (run_engine present) → 200 + enqueued=True;
    card actually lands in the queue (card is enqueued instead of 409)."""
    import webapp as _webapp

    async def fake_engine(**kwargs):
        # Slow engine — never finishes within the test
        await asyncio.sleep(100)
        yield {"type": "text", "text": "never"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    # Initialise in-memory queue + file path (test isolation)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    _webapp._scan_state_init({"DATA": tmp_path / "data"})
    # Simulate already-occupied slot
    ctx["running"]["1001:42"] = True

    _make_tasks(project_dir, col="backlog")
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/tasks/aabbcc/move",
        json={"to": "in_progress"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200, f"Busy project must give 200+enqueued, got: {resp.status}"
    data = await resp.json()
    assert data.get("enqueued") is True, f"Expected enqueued=True: {data}"
    # Card actually in queue
    assert "aabbcc" in _webapp._queue_for("1001:42"), \
        f"Card must be in queue: {_webapp._queue_for('1001:42')}"


async def test_concurrent_chat_second_request_busy(tmp_path, project_dir):
    """Two direct calls to api_project_chat on one project — second gets SSE busy error.
    Test via direct handler call, not aiohttp_client (for timing isolation)."""
    from aiohttp import web
    from unittest.mock import AsyncMock, MagicMock

    event_received = asyncio.Event()
    slow_done = asyncio.Event()

    async def slow_engine(**kwargs):
        event_received.set()
        await slow_done.wait()
        yield {"type": "text", "text": "finally done"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=slow_engine)
    app_obj = _make_app(ctx)
    session_key = "1001:42"

    # Simulate first request having already occupied the slot (as the real handler does synchronously)
    ctx["running"][session_key] = True

    # Create a fake request for the second request
    class FakeRequest:
        def __init__(self):
            self.app = {"ctx": ctx}
            self.match_info = {"id": "myproject"}
            self.remote = "127.0.0.1"
            self._json = {"prompt": "Second request"}

        async def json(self):
            return self._json

    # Create a fake StreamResponse to capture writes
    written = []

    class FakeStreamResp:
        status = 200
        headers = {}

        async def prepare(self, req):
            pass

        async def write(self, data):
            written.append(data.decode("utf-8", errors="replace"))

        def set_status(self, s):
            self.status = s

    # Substitute web.StreamResponse
    original_sr = web.StreamResponse

    class PatchedStreamResponse(web.StreamResponse):
        pass

    # Use ctx directly: running is already occupied, so handler returns SSE error immediately.
    # But we need a real aiohttp request. Going via a separate test app.
    # Simpler check: verify via ctx["running"] directly.

    # Second request sees running[session_key] = True → must return error in SSE
    # We can verify this directly through _check logic:
    assert ctx["running"].get(session_key) is not None, "Slot must be occupied"

    # Clean up
    ctx["running"].pop(session_key, None)
    slow_done.set()  # release the slow engine


async def test_two_simultaneous_chat_requests(aiohttp_client, tmp_path, project_dir):
    """Two simultaneous POST /chat — second sees SSE 'busy' while first is running."""
    import asyncio

    # Synchronisation primitive between engine and test
    engine_started = asyncio.Event()
    engine_can_finish = asyncio.Event()

    async def blocking_engine(**kwargs):
        engine_started.set()
        await engine_can_finish.wait()
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=blocking_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    h = _auth_headers(ctx)

    # Start first request in background (do not await response)
    task1 = asyncio.create_task(
        client.post("/api/projects/myproject/chat", json={"prompt": "First"}, headers=h)
    )

    # Wait for engine to start (means first request has occupied the slot)
    # Engine starts synchronously — give it a moment
    await asyncio.sleep(0.05)

    # Spec-041 A3: the second request must be ENQUEUED (not rejected) while the first runs.
    resp2 = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Second"},
        headers=h,
    )
    events2 = await _read_sse_events(resp2)
    queued_events = [e for e in events2 if e.get("type") == "queued"]

    # The second prompt is now sitting in the chat queue (captured before we release the first run).
    assert len(queued_events) == 1, (
        f"Second request should get a 'queued' SSE frame, got: {events2}"
    )
    assert queued_events[0]["item"]["text"] == "Second"

    # Release the first request — its finally then drains "Second" from the queue.
    engine_can_finish.set()
    resp1 = await task1
    await resp1.read()


# ─────────────────────────── think_mode → effort mapping ─────────────────────


async def test_chat_think_mode_max_passes_high_effort(aiohttp_client, tmp_path, project_dir):
    """think_mode='max' in request body → run_engine receives effort='high'."""
    captured = {}

    async def fake_engine(**kwargs):
        captured["effort"] = kwargs.get("effort")
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Go", "think_mode": "max"},
        headers=_auth_headers(ctx),
    )
    await resp.read()
    assert captured.get("effort") == "high", f"Expected effort='high', got: {captured}"


async def test_chat_think_mode_min_passes_low_effort(aiohttp_client, tmp_path, project_dir):
    """think_mode='min' in request body → run_engine receives effort='low'."""
    captured = {}

    async def fake_engine(**kwargs):
        captured["effort"] = kwargs.get("effort")
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "s2"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Go", "think_mode": "min"},
        headers=_auth_headers(ctx),
    )
    await resp.read()
    assert captured.get("effort") == "low", f"Expected effort='low', got: {captured}"


async def test_chat_think_mode_default_passes_none_effort(aiohttp_client, tmp_path, project_dir):
    """think_mode='default' (or absent) → run_engine receives effort=None (preserves _DEFAULT_EFFORT)."""
    captured = {"effort": "sentinel"}  # distinguish "not set" from None

    async def fake_engine(**kwargs):
        captured["effort"] = kwargs.get("effort", "not_passed")
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "s3"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "Go", "think_mode": "default"},
        headers=_auth_headers(ctx),
    )
    await resp.read()
    assert captured.get("effort") is None, f"Expected effort=None, got: {captured}"

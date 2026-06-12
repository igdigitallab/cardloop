"""
Tests for spec-021: context rotation + fresh card sessions + cwd-lock.

All run_engine calls are mocked — no real Claude calls.
"""
import sys
import json
import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot as _bot
import webapp as _webapp
from webapp import _derive_token, CONTEXT_ROTATE_AT, CONTEXT_ROTATION, CONTEXT_WARN_AT


# ─────────────────────────── helpers ────────────────────────────────────────

def _make_ctx(tmp_path, project_dir, run_engine=None):
    """Minimal ctx for tests."""
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
        "cwd_locks": {},
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
        # Spec-021 Phase 4: pending handoff summaries
        "pending_handoff": {},
        # Context early-warn tracking set (shared by reference like pending_handoff)
        "context_warned": set(),
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


def _make_app(ctx, extra_routes=True):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    if extra_routes:
        app.router.add_post("/api/projects/{id}/rotate", _webapp.api_project_rotate)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def _read_sse(resp) -> list[dict]:
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


# ─────────────────────────── fixtures ───────────────────────────────────────

@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


# ─────────────────────────── Part 1: Auto rotation (api_project_chat) ───────

async def test_rotation_not_triggered_below_backstop(aiohttp_client, tmp_path, project_dir):
    """context_tokens=70000 < 175K backstop → rotation must NOT fire."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "sess-abc", "context_tokens": 70000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 175000), \
         patch.object(_webapp, "_QUEUE", {}):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Do something"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    types = [e.get("type") for e in events]
    assert "rotation" not in types, (
        f"70K is below 175K backstop — rotation must NOT fire, got: {types}"
    )


async def test_rotation_triggered_above_backstop(aiohttp_client, tmp_path, project_dir):
    """context_tokens=180000 > 175K backstop → rotation SSE event sent."""

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "sess-abc", "context_tokens": 180000}

    async def fake_rotation_engine(**kwargs):
        yield {"type": "text", "text": "Summary: working on X."}
        yield {"type": "result", "session_id": "sess-rotate", "context_tokens": 1000}

    async def dispatch_engine(**kwargs):
        model = kwargs.get("model", "sonnet")
        if model == "haiku":
            async for e in fake_rotation_engine(**kwargs):
                yield e
        else:
            async for e in fake_engine(**kwargs):
                yield e

    ctx = _make_ctx(tmp_path, project_dir, run_engine=dispatch_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 175000), \
         patch.object(_webapp, "_QUEUE", {}):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Do something"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    types = [e.get("type") for e in events]
    assert "rotation" in types, f"180K > 175K backstop — expected rotation event, got: {types}"
    rotation_evt = next(e for e in events if e.get("type") == "rotation")
    assert rotation_evt.get("tokens") == 180000


async def test_rotation_not_triggered_well_below_threshold(aiohttp_client, tmp_path, project_dir):
    """context_tokens=30000 << 175K backstop → no rotation event."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "sess-low", "context_tokens": 30000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 175000):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Test"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    types = [e.get("type") for e in events]
    assert "rotation" not in types, f"Should not have rotation event, got: {types}"


async def test_rotation_toggle_off(aiohttp_client, tmp_path, project_dir):
    """CONTEXT_ROTATION=False → no rotation even above 175K backstop."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "sess-x", "context_tokens": 180000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", False), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 175000):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Test"},
            headers=_auth_headers(ctx),
        )
        events = await _read_sse(resp)

    types = [e.get("type") for e in events]
    assert "rotation" not in types, f"Rotation disabled — should not fire, got: {types}"


async def test_rotation_session_cleared(tmp_path, project_dir):
    """After _do_session_rotation, sessions[key] is removed."""
    summary_text = "Task: build feature. Status: in progress."

    async def haiku_engine(**kwargs):
        yield {"type": "text", "text": summary_text}
        yield {"type": "result", "session_id": "haiku-sess", "context_tokens": 500}

    session_key = "1001:42"
    project = {"name": "myproject", "cwd": str(project_dir)}
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["sessions"][session_key] = "old-session-id"
    ctx["run_engine"] = haiku_engine

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        result = await _webapp._do_session_rotation(ctx, session_key, project, str(project_dir))

    assert result is not None, "Expected summary text"
    assert session_key not in ctx["sessions"], "Session key should be cleared after rotation"


async def test_rotation_handoff_file_written(tmp_path, project_dir):
    """After rotation, handoff file exists in cwd/.claude-ops/memory/."""
    summary_text = "Handoff content here."

    async def haiku_engine(**kwargs):
        yield {"type": "text", "text": summary_text}
        yield {"type": "result", "session_id": "haiku-sess", "context_tokens": 500}

    session_key = "1001:42"
    project = {"name": "myproject", "cwd": str(project_dir)}
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["sessions"][session_key] = "old-session-id"
    ctx["run_engine"] = haiku_engine

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        await _webapp._do_session_rotation(ctx, session_key, project, str(project_dir))

    handoff_path = project_dir / ".claude-ops" / "memory" / "session-handoff.md"
    assert handoff_path.exists(), "Handoff file should be created"
    content = handoff_path.read_text(encoding="utf-8")
    assert "type: handoff" in content
    assert summary_text in content


async def test_rotation_summary_uses_no_full_transcript_resume(tmp_path, project_dir):
    """Task 2: _do_session_rotation calls run_engine with resume_session_id=None (fresh haiku session).
    No full-transcript resume — the 175K context is NOT re-paid for the summary call."""
    captured = {}

    async def haiku_engine(**kwargs):
        captured["resume"] = kwargs.get("resume_session_id")
        yield {"type": "text", "text": "Summary: feature X in progress."}
        yield {"type": "result", "session_id": "haiku-fresh", "context_tokens": 500}

    session_key = "1001:42"
    project = {"name": "myproject", "cwd": str(project_dir)}
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["sessions"][session_key] = "fat-session-id"
    ctx["run_engine"] = haiku_engine

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        await _webapp._do_session_rotation(ctx, session_key, project, str(project_dir))

    assert captured.get("resume") is None, (
        f"Task 2: rotation summary must use resume_session_id=None (fresh session), "
        f"got: {captured.get('resume')!r}"
    )


async def test_rotation_failure_does_not_break_main_run(aiohttp_client, tmp_path, project_dir):
    """If _do_session_rotation throws, result event still arrives to client."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "working"}
        yield {"type": "result", "session_id": "sess-ok", "context_tokens": 180000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)

    # Make rotation always fail
    async def bad_rotation(*args, **kwargs):
        raise RuntimeError("rotation exploded")

    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 175000), \
         patch.object(_webapp, "_do_session_rotation", bad_rotation):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Test"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    # result event must still arrive
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) > 0, f"result event must arrive even when rotation fails, got: {events}"


# ─────────────────────────── Part 1: /rotate endpoint ───────────────────────

async def test_rotate_endpoint_no_session(aiohttp_client, tmp_path, project_dir):
    """POST /rotate with no active session → rotated=false."""
    ctx = _make_ctx(tmp_path, project_dir)
    # sessions is empty — no active session
    app = _make_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["rotated"] is False
    assert data.get("reason") == "no active session"


async def test_rotate_endpoint_busy(aiohttp_client, tmp_path, project_dir):
    """POST /rotate while project busy → 409."""
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["running"][session_key] = True  # project is busy
    ctx["sessions"][session_key] = "some-session"

    app = _make_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 409
    data = await resp.json()
    assert "busy" in data.get("error", "").lower()


async def test_rotate_endpoint_success(aiohttp_client, tmp_path, project_dir):
    """POST /rotate with active session → rotated=true."""
    async def haiku_engine(**kwargs):
        yield {"type": "text", "text": "Summary: X task in progress."}
        yield {"type": "result", "session_id": "h-sess", "context_tokens": 400}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=haiku_engine)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "existing-session-id"

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/rotate",
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        data = await resp.json()

    assert data["ok"] is True
    assert data["rotated"] is True
    assert "summary_preview" in data
    # Session should be cleared after rotation
    assert session_key not in ctx["sessions"]


# ─────────────────────────── Part 2: Fresh card sessions + cwd-lock ─────────

async def test_card_uses_fresh_session(tmp_path, project_dir):
    """_run_card is called with resume_session_id=None (fresh session)."""
    captured = {}

    async def fake_engine(**kwargs):
        captured["resume"] = kwargs.get("resume_session_id")
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "card-sess-new", "context_tokens": 100}

    session_key = "1001:42"
    project = {
        "name": "myproject",
        "cwd": str(project_dir),
        "tg_thread": session_key,
        "model": "sonnet",
    }
    card = {"id": "aabbcc", "text": "Build feature", "description": None}
    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    ctx["sessions"][session_key] = "old-shared-session-id"
    ctx["cwd_locks"] = {}

    # We call _run_card directly; it needs running lock set first (normally done by _start_card_run)
    ctx["running"][session_key] = True
    await _webapp._run_card(ctx, None, project, card, session_key, run_mode="legacy")

    assert captured.get("resume") is None, (
        f"Cards must start fresh (resume_session_id=None), got: {captured.get('resume')}"
    )


async def test_card_does_not_write_session(tmp_path, project_dir):
    """After _run_card, ctx['sessions'] is unchanged (card doesn't write session_id back)."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "card-sess-789", "context_tokens": 100}

    session_key = "1001:42"
    original_session = "shared-chat-session"
    project = {
        "name": "myproject",
        "cwd": str(project_dir),
        "tg_thread": session_key,
        "model": "sonnet",
    }
    card = {"id": "aabbcc", "text": "Build feature", "description": None}
    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    ctx["sessions"][session_key] = original_session
    ctx["cwd_locks"] = {}
    ctx["running"][session_key] = True

    await _webapp._run_card(ctx, None, project, card, session_key, run_mode="legacy")

    assert ctx["sessions"].get(session_key) == original_session, (
        f"Card must not overwrite shared session. Expected {original_session!r}, "
        f"got {ctx['sessions'].get(session_key)!r}"
    )


async def test_cwd_lock_blocks_concurrent_card(tmp_path, project_dir):
    """Two _run_card calls with same cwd: second is blocked by cwd-lock."""
    started = []
    finished = []

    async def slow_engine(**kwargs):
        started.append(1)
        await asyncio.sleep(0.05)
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "s1", "context_tokens": 100}
        finished.append(1)

    project_a = {
        "name": "myproject",
        "cwd": str(project_dir),
        "tg_thread": "1001:42",
        "model": "sonnet",
    }
    project_b = {
        "name": "myproject",
        "cwd": str(project_dir),  # same cwd
        "tg_thread": "1001:99",  # different session_key
        "model": "sonnet",
    }
    card_a = {"id": "aabb11", "text": "Card A", "description": None}
    card_b = {"id": "aabb22", "text": "Card B", "description": None}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=slow_engine)
    ctx["cwd_locks"] = {}
    ctx["running"]["1001:42"] = True
    ctx["running"]["1001:99"] = True

    # Launch both concurrently
    await asyncio.gather(
        _webapp._run_card(ctx, None, project_a, card_a, "1001:42", run_mode="legacy"),
        _webapp._run_card(ctx, None, project_b, card_b, "1001:99", run_mode="legacy"),
    )

    # Only one of the two should have actually started the engine (the cwd-lock blocks the second)
    assert len(started) == 1, (
        f"Expected only 1 card to run (cwd-lock should block the second), started={started}"
    )


async def test_cwd_lock_released_on_finish(tmp_path, project_dir):
    """After _run_card finishes, cwd-lock for that path is released."""
    async def fast_engine(**kwargs):
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "s1", "context_tokens": 50}

    session_key = "1001:42"
    project = {
        "name": "myproject",
        "cwd": str(project_dir),
        "tg_thread": session_key,
        "model": "sonnet",
    }
    card = {"id": "aabbcc", "text": "Task", "description": None}
    ctx = _make_ctx(tmp_path, project_dir, run_engine=fast_engine)
    ctx["cwd_locks"] = {}
    ctx["running"][session_key] = True

    await _webapp._run_card(ctx, None, project, card, session_key, run_mode="legacy")

    cwd_key = str(project_dir)
    assert not ctx["cwd_locks"].get(cwd_key), (
        f"cwd-lock must be released after _run_card finishes, got: {ctx['cwd_locks']}"
    )


# ─────────────────────────── TG-path hook (_maybe_rotate_tg in bot.py) ──────

_TG_KEY = "1001:42"


def _tg_binding(project_dir) -> dict:
    return {"project": "myproject", "cwd": str(project_dir), "model": "sonnet"}


async def _call_tg_hook(project_dir, context_tokens, tg_queue, rotation_stub, send_stub):
    """Invoke bot._maybe_rotate_tg with mocked rotation/send and a controlled queue."""
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 175000), \
         patch.object(_webapp, "_do_session_rotation", rotation_stub), \
         patch.object(_bot, "_TG_QUEUE", tg_queue), \
         patch.object(_bot, "send", send_stub):
        await _bot._maybe_rotate_tg(
            None, 1001, 42, _TG_KEY,
            _tg_binding(project_dir),
            {"context_tokens": context_tokens},
        )


async def test_tg_rotation_triggered_above_backstop(tmp_path, project_dir):
    """TG hook: 180K tokens (above 175K backstop), empty queue → _do_session_rotation called + TG notification sent."""
    calls = []

    async def rotation_stub(ctx, session_key, project, cwd):
        calls.append({"ctx": ctx, "session_key": session_key, "project": project, "cwd": cwd})
        return "summary text"

    send_stub = AsyncMock()
    await _call_tg_hook(project_dir, 180000, {}, rotation_stub, send_stub)

    assert len(calls) == 1, f"Rotation must be called once, got {len(calls)}"
    assert calls[0]["session_key"] == _TG_KEY
    assert calls[0]["cwd"] == str(project_dir)
    assert calls[0]["project"]["name"] == "myproject"
    # ctx must carry exactly what _do_session_rotation reads — incl. run_engine
    assert calls[0]["ctx"]["run_engine"] is _bot.run_engine
    assert "ptb_app" not in calls[0]["ctx"], "No ptb_app — TG notify goes via send(), not _notify_tg_rotation"
    send_stub.assert_awaited_once()


async def test_tg_rotation_skipped_when_queue_nonempty(tmp_path, project_dir):
    """TG hook: 180K tokens but _TG_QUEUE[k] has a pending message → rotation NOT called."""
    rotation_stub = AsyncMock(return_value="summary")
    send_stub = AsyncMock()
    queue = {_TG_KEY: [{"prompt": "queued message", "message_id": 7}]}

    await _call_tg_hook(project_dir, 180000, queue, rotation_stub, send_stub)

    rotation_stub.assert_not_awaited()
    send_stub.assert_not_awaited()


async def test_tg_rotation_skipped_below_threshold(tmp_path, project_dir):
    """TG hook: 30K tokens < threshold → rotation NOT called."""
    rotation_stub = AsyncMock(return_value="summary")
    send_stub = AsyncMock()

    await _call_tg_hook(project_dir, 30000, {}, rotation_stub, send_stub)

    rotation_stub.assert_not_awaited()
    send_stub.assert_not_awaited()


# ─────────────────────────── Part 3: Handoff auto-injection (Spec-021 Phase 4) ─

SESSION_KEY = "1001:42"


async def test_pending_handoff_set_after_rotation(tmp_path, project_dir):
    """_do_session_rotation stores summary in ctx['pending_handoff'][session_key]."""
    summary_text = "We were building feature X. Next step: add tests."

    async def haiku_engine(**kwargs):
        yield {"type": "text", "text": summary_text}
        yield {"type": "result", "session_id": "haiku-sess", "context_tokens": 500}

    project = {"name": "myproject", "cwd": str(project_dir)}
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["sessions"][SESSION_KEY] = "old-session-id"
    ctx["run_engine"] = haiku_engine

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        result = await _webapp._do_session_rotation(ctx, SESSION_KEY, project, str(project_dir))

    assert result == summary_text
    assert ctx["pending_handoff"].get(SESSION_KEY) == summary_text, (
        f"pending_handoff must be set after rotation, got: {ctx['pending_handoff']}"
    )


async def test_handoff_injected_into_next_chat_turn(aiohttp_client, tmp_path, project_dir):
    """A pending handoff is prepended to the prompt on the next fresh-session chat turn."""
    captured_prompts = []

    async def fake_engine(**kwargs):
        captured_prompts.append(kwargs.get("prompt", ""))
        yield {"type": "text", "text": "response"}
        yield {"type": "result", "session_id": "new-sess", "context_tokens": 100}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    # Simulate: rotation happened, pending handoff is waiting
    ctx["pending_handoff"][SESSION_KEY] = "Previous work: feature X was 80% done."
    # No active session — fresh turn (resume_session_id will be None)
    assert SESSION_KEY not in ctx["sessions"]

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Continue the work"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        await _read_sse(resp)

    assert len(captured_prompts) == 1
    injected = captured_prompts[0]
    assert "<prior-session-summary>" in injected, (
        f"Handoff preamble must be injected. Got prompt: {injected[:200]!r}"
    )
    assert "Previous work: feature X was 80% done." in injected
    assert "Continue the work" in injected


async def test_handoff_cleared_after_injection(aiohttp_client, tmp_path, project_dir):
    """After injection, pending_handoff entry is removed so it fires exactly once."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "sess-1", "context_tokens": 100}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    ctx["pending_handoff"][SESSION_KEY] = "Some summary."

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Hello"},
            headers=_auth_headers(ctx),
        )

    assert SESSION_KEY not in ctx["pending_handoff"], (
        "pending_handoff must be cleared after injection so it only fires once"
    )


async def test_handoff_not_injected_when_session_exists(aiohttp_client, tmp_path, project_dir):
    """If an active session already exists (not a fresh start), handoff is NOT injected."""
    captured_prompts = []

    async def fake_engine(**kwargs):
        captured_prompts.append(kwargs.get("prompt", ""))
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "existing-sess", "context_tokens": 100}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    # An active session exists (not post-rotation)
    ctx["sessions"][SESSION_KEY] = "ongoing-session-id"
    ctx["pending_handoff"][SESSION_KEY] = "Should not be injected."

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Next step"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        await _read_sse(resp)

    assert len(captured_prompts) == 1
    assert "<prior-session-summary>" not in captured_prompts[0], (
        "Handoff must NOT be injected when an active session exists"
    )
    # pending_handoff should still be there (not consumed)
    assert ctx["pending_handoff"].get(SESSION_KEY) == "Should not be injected."


async def test_card_run_not_affected_by_handoff(tmp_path, project_dir):
    """_run_card runs are unaffected by pending_handoff — cards are always fresh, no preamble."""
    captured_prompts = []

    async def fake_engine(**kwargs):
        captured_prompts.append(kwargs.get("prompt", ""))
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "card-sess", "context_tokens": 100}

    session_key = SESSION_KEY
    project = {
        "name": "myproject",
        "cwd": str(project_dir),
        "tg_thread": session_key,
        "model": "sonnet",
    }
    card = {"id": "aabbcc", "text": "Build widget", "description": None}
    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    ctx["cwd_locks"] = {}
    ctx["running"][session_key] = True
    # Simulate a pending handoff for this session
    ctx["pending_handoff"][session_key] = "Previous work summary."

    await _webapp._run_card(ctx, None, project, card, session_key, run_mode="legacy")

    assert len(captured_prompts) == 1
    assert "<prior-session-summary>" not in captured_prompts[0], (
        "Card runs must NOT receive the handoff preamble"
    )
    # pending_handoff should remain untouched by the card run
    assert ctx["pending_handoff"].get(session_key) == "Previous work summary.", (
        "Card run must not consume the pending_handoff"
    )


async def test_handoff_injection_failure_does_not_break_turn(aiohttp_client, tmp_path, project_dir):
    """If handoff injection throws an exception, the turn continues normally."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "response"}
        yield {"type": "result", "session_id": "sess-ok", "context_tokens": 100}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    # Install a broken pending_handoff that raises on pop
    broken_dict = {}

    class _BrokenDict(dict):
        def pop(self, key, default=None):
            raise RuntimeError("simulated injection error")

    ctx["pending_handoff"] = _BrokenDict({"1001:42": "summary"})

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Do work"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) > 0, (
        f"Turn must complete normally even if handoff injection fails, got: {events}"
    )


# ─────────────────────────── Part 4: Context early warning (CONTEXT_WARN_AT) ─

async def test_context_warn_fires_on_crossing(aiohttp_client, tmp_path, project_dir):
    """context_tokens at CONTEXT_WARN_AT → context_warn=True on result, session key tracked."""
    warn_at = 150000
    rotate_at = 175000

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "sess-w", "context_tokens": warn_at}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", rotate_at), \
         patch.object(_webapp, "CONTEXT_WARN_AT", warn_at), \
         patch.object(_webapp, "_QUEUE", {}), \
         patch.object(_webapp, "_notify_tg_context_warn", AsyncMock()):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Work"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert result_events[0].get("context_warn") is True, (
        f"Expected context_warn=True on first crossing, got: {result_events[0]}"
    )
    # The session key must be tracked to prevent re-firing.
    assert SESSION_KEY in ctx["context_warned"], (
        "session_key must be added to context_warned after the first crossing"
    )


async def test_context_warn_does_not_refire(aiohttp_client, tmp_path, project_dir):
    """Second turn still above CONTEXT_WARN_AT → context_warn NOT present (anti-spam)."""
    warn_at = 150000
    rotate_at = 175000

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "sess-w2", "context_tokens": warn_at + 1000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    # Pre-mark as already warned — simulates a session that already crossed the threshold.
    ctx["context_warned"].add(SESSION_KEY)

    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", rotate_at), \
         patch.object(_webapp, "CONTEXT_WARN_AT", warn_at), \
         patch.object(_webapp, "_QUEUE", {}), \
         patch.object(_webapp, "_notify_tg_context_warn", AsyncMock()):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "More work"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert "context_warn" not in result_events[0], (
        f"context_warn must be absent on a second above-threshold turn, got: {result_events[0]}"
    )


async def test_context_warn_absent_below_threshold(aiohttp_client, tmp_path, project_dir):
    """context_tokens well below CONTEXT_WARN_AT → context_warn absent from result."""
    warn_at = 150000
    rotate_at = 175000

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "sess-low2", "context_tokens": 50000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", rotate_at), \
         patch.object(_webapp, "CONTEXT_WARN_AT", warn_at), \
         patch.object(_webapp, "_QUEUE", {}):
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Test"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)

    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert "context_warn" not in result_events[0], (
        f"context_warn must be absent below threshold, got: {result_events[0]}"
    )
    assert SESSION_KEY not in ctx["context_warned"], (
        "context_warned must not be set when below threshold"
    )


async def test_context_warn_absent_at_or_above_rotate_at(aiohttp_client, tmp_path, project_dir):
    """context_tokens at/above CONTEXT_ROTATE_AT → rotation fires, warn must NOT also fire."""
    warn_at = 150000
    rotate_at = 175000

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "sess-over", "context_tokens": rotate_at + 1}

    async def fake_rotation_engine(**kwargs):
        yield {"type": "text", "text": "Summary."}
        yield {"type": "result", "session_id": "rot-sess", "context_tokens": 500}

    async def dispatch(**kwargs):
        if kwargs.get("model") == "haiku":
            async for e in fake_rotation_engine(**kwargs):
                yield e
        else:
            async for e in fake_engine(**kwargs):
                yield e

    ctx = _make_ctx(tmp_path, project_dir, run_engine=dispatch)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", rotate_at), \
         patch.object(_webapp, "CONTEXT_WARN_AT", warn_at), \
         patch.object(_webapp, "_QUEUE", {}), \
         patch.object(_webapp, "_notify_tg_context_warn", AsyncMock()) as mock_warn_notify:
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Do something"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        events = await _read_sse(resp)
        # Warn TG notify must NOT fire when tokens are at/above the rotation backstop.
        mock_warn_notify.assert_not_awaited()

    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert "context_warn" not in result_events[0], (
        f"context_warn must be absent when rotation fires (tokens >= CONTEXT_ROTATE_AT), got: {result_events[0]}"
    )


async def test_context_warn_cleared_after_rotation(tmp_path, project_dir):
    """After _do_session_rotation, context_warned entry for session_key is removed."""
    summary_text = "Task: X. Status: done."

    async def haiku_engine(**kwargs):
        yield {"type": "text", "text": summary_text}
        yield {"type": "result", "session_id": "haiku-r", "context_tokens": 400}

    ctx = _make_ctx(tmp_path, project_dir)
    ctx["sessions"][SESSION_KEY] = "old-sess"
    ctx["run_engine"] = haiku_engine
    # Simulate that the warn already fired before rotation.
    ctx["context_warned"].add(SESSION_KEY)

    with patch.object(_webapp, "CONTEXT_ROTATION", True):
        await _webapp._do_session_rotation(ctx, SESSION_KEY, project={"name": "myproject", "cwd": str(project_dir)}, cwd=str(project_dir))

    assert SESSION_KEY not in ctx["context_warned"], (
        "context_warned must be cleared after rotation so a fresh session can warn again"
    )


async def test_context_warn_cleared_after_web_reset(aiohttp_client, tmp_path, project_dir):
    """POST /session action=new clears context_warned so the fresh session can warn again."""
    ctx = _make_ctx(tmp_path, project_dir)
    # Simulate that the warn already fired.
    ctx["context_warned"].add(SESSION_KEY)
    # No active session / not busy.
    assert SESSION_KEY not in ctx["sessions"]
    assert ctx["running"].get(SESSION_KEY) is None

    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/session", _webapp.api_project_set_session)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/session",
        json={"action": "new"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    assert SESSION_KEY not in ctx["context_warned"], (
        "context_warned must be cleared after a web /session new reset"
    )

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

import webapp as _webapp
from webapp import _derive_token, CONTEXT_ROTATE_AT, CONTEXT_ROTATION


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

async def test_rotation_triggered_above_threshold(aiohttp_client, tmp_path, project_dir):
    """context_tokens=70000 > threshold → rotation SSE event sent."""
    session_id_store = {}

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "sess-abc", "context_tokens": 70000}

    async def fake_rotation_engine(**kwargs):
        # The haiku summary run — returns a summary
        yield {"type": "text", "text": "Summary: working on X."}
        yield {"type": "result", "session_id": "sess-rotate", "context_tokens": 1000}

    call_count = {"n": 0}

    async def dispatch_engine(**kwargs):
        call_count["n"] += 1
        model = kwargs.get("model", "sonnet")
        if model == "haiku":
            async for e in fake_rotation_engine(**kwargs):
                yield e
        else:
            async for e in fake_engine(**kwargs):
                yield e

    ctx = _make_ctx(tmp_path, project_dir, run_engine=dispatch_engine)
    # Ensure rotation is on, threshold correct, and queue is empty (test isolation)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 60000), \
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
    assert "rotation" in types, f"Expected rotation event, got: {types}"
    rotation_evt = next(e for e in events if e.get("type") == "rotation")
    assert rotation_evt.get("tokens") == 70000


async def test_rotation_not_triggered_below_threshold(aiohttp_client, tmp_path, project_dir):
    """context_tokens=30000 < threshold → no rotation event."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "sess-low", "context_tokens": 30000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 60000):
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
    """CONTEXT_ROTATION=False → no rotation even above threshold."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "sess-x", "context_tokens": 70000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    with patch.object(_webapp, "CONTEXT_ROTATION", False), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 60000):
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


async def test_rotation_failure_does_not_break_main_run(aiohttp_client, tmp_path, project_dir):
    """If _do_session_rotation throws, result event still arrives to client."""
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "working"}
        yield {"type": "result", "session_id": "sess-ok", "context_tokens": 70000}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)

    # Make rotation always fail
    async def bad_rotation(*args, **kwargs):
        raise RuntimeError("rotation exploded")

    with patch.object(_webapp, "CONTEXT_ROTATION", True), \
         patch.object(_webapp, "CONTEXT_ROTATE_AT", 60000), \
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

"""
Tests for spec-042: cheap handoff reset (producer + persistence).

Covers:
  1. save_handoff round-trip: write to HANDOFF_F, read back with _read.
  2. POST /rotate {handoff:true} with mocked _build_handoff stores summary in pending_handoff
     and returns {"handoff": true} in the response.
  3. POST /rotate {handoff:false} / no body: no handoff stored, existing behaviour preserved.
  4. POST /rotate {handoff:true} but no active session: no handoff, reset=false path unchanged.
  5. _build_handoff returns "" when session_id is None (edge case).
  6. Handoff pop in webapp consumer calls save_handoff (persistence after injection).
"""
import sys
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine as _engine
import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── helpers ────────────────────────────────────────

def _make_ctx(tmp_path, project_dir, *, run_engine=None):
    """Minimal ctx that mirrors what test_context_rotation.py uses."""
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


# ─────────────────────────── 1. save_handoff round-trip ─────────────────────

def test_save_handoff_round_trip(tmp_path):
    """save_handoff() writes pending_handoff to HANDOFF_F; _read() restores it."""
    handoff_f = tmp_path / "handoff.json"
    test_data = {"proj:42": "Previous work: feature X done.", "proj:99": "Other summary."}

    # Patch the module-level globals in engine so save_handoff writes to our tmp file
    with patch.object(_engine, "HANDOFF_F", handoff_f), \
         patch.object(_engine, "pending_handoff", test_data):
        _engine.save_handoff()

    assert handoff_f.exists(), "handoff.json must be created by save_handoff()"
    loaded = _engine._read(handoff_f, {})
    assert loaded == test_data, f"Round-trip mismatch: expected {test_data}, got {loaded}"


def test_save_handoff_empty_dict(tmp_path):
    """save_handoff() with empty dict produces a valid empty JSON object on disk."""
    handoff_f = tmp_path / "handoff.json"
    with patch.object(_engine, "HANDOFF_F", handoff_f), \
         patch.object(_engine, "pending_handoff", {}):
        _engine.save_handoff()

    loaded = _engine._read(handoff_f, None)
    assert loaded == {}, f"Empty pending_handoff should write {{}}, got {loaded}"


def test_handoff_f_loaded_on_start(tmp_path):
    """_read(HANDOFF_F, {}) behaviour: returns dict from file or {} if missing."""
    handoff_f = tmp_path / "handoff.json"
    # File absent → should return default {}
    result = _engine._read(handoff_f, {})
    assert result == {}

    # File present with content
    handoff_f.write_text(json.dumps({"k": "v"}))
    result = _engine._read(handoff_f, {})
    assert result == {"k": "v"}


# ─────────────────────────── 2. rotate {handoff:true} stores summary ─────────

async def test_rotate_handoff_true_stores_summary(aiohttp_client, tmp_path):
    """POST /rotate {handoff:true} with mocked _build_handoff stores summary."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "active-session-id"

    save_handoff_called = []

    def _fake_save_handoff():
        save_handoff_called.append(True)

    ctx["save_handoff"] = _fake_save_handoff

    async def _fake_build_handoff(ctx, sk, cwd, sid):
        return f"Summary for {sid}"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_build_handoff", _fake_build_handoff):
        resp = await client.post(
            "/api/projects/myproject/rotate",
            json={"handoff": True},
            headers=_auth_headers(ctx),
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True
    assert data["handoff"] is True, f"Expected handoff:true in response, got {data}"
    assert ctx["pending_handoff"].get(session_key) == "Summary for active-session-id", (
        f"pending_handoff must contain the summary, got: {ctx['pending_handoff']}"
    )
    assert save_handoff_called, "save_handoff must be called when summary is stored"
    # Session must be cleared
    assert ctx["sessions"].get(session_key) is None


async def test_rotate_handoff_true_empty_summary_not_stored(aiohttp_client, tmp_path):
    """If _build_handoff returns "", handoff is NOT stored and response has handoff:false."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "active-session-id"

    async def _fake_build_handoff_empty(ctx, sk, cwd, sid):
        return ""

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_build_handoff", _fake_build_handoff_empty):
        resp = await client.post(
            "/api/projects/myproject/rotate",
            json={"handoff": True},
            headers=_auth_headers(ctx),
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True
    assert data["handoff"] is False, f"Empty summary → handoff:false, got {data}"
    assert session_key not in ctx["pending_handoff"]


# ─────────────────────────── 3. rotate {handoff:false} / no body ─────────────

async def test_rotate_blank_no_body(aiohttp_client, tmp_path):
    """POST /rotate with no body → handoff:false, session cleared (original behaviour)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "some-session"

    build_called = []

    async def _fake_build_handoff(ctx, sk, cwd, sid):
        build_called.append(True)
        return "summary"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_build_handoff", _fake_build_handoff):
        resp = await client.post(
            "/api/projects/myproject/rotate",
            headers=_auth_headers(ctx),
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["reset"] is True
    assert data["handoff"] is False, f"No body → handoff must be false, got {data}"
    assert not build_called, "_build_handoff must NOT be called when handoff:false/absent"
    assert ctx["sessions"].get(session_key) is None


async def test_rotate_handoff_false_explicit(aiohttp_client, tmp_path):
    """POST /rotate {handoff:false} → same blank behaviour, no summary built."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["sessions"][session_key] = "some-session"

    build_called = []

    async def _fake_build(ctx, sk, cwd, sid):
        build_called.append(True)
        return "summary"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_build_handoff", _fake_build):
        resp = await client.post(
            "/api/projects/myproject/rotate",
            json={"handoff": False},
            headers=_auth_headers(ctx),
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["handoff"] is False
    assert not build_called


# ─────────────────────────── 4. rotate {handoff:true} no active session ──────

async def test_rotate_handoff_true_no_session(aiohttp_client, tmp_path):
    """POST /rotate {handoff:true} with no active session → reset=false, handoff not triggered."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    # sessions is empty

    build_called = []

    async def _fake_build(ctx, sk, cwd, sid):
        build_called.append(True)
        return "summary"

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
    assert data["reset"] is False  # no session → early return
    assert not build_called, "_build_handoff must not be called when no session"


# ─────────────────────────── 5. _build_handoff edge: no session_id ───────────

async def test_build_handoff_no_session_id(tmp_path):
    """_build_handoff returns '' when session_id is None (no transcript to read)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    result = await _webapp._build_handoff(ctx, "1001:42", str(project_dir), None)
    assert result == "", f"Expected '' for None session_id, got: {result!r}"


async def test_build_handoff_missing_transcript(tmp_path):
    """_build_handoff returns '' when the jsonl file doesn't exist."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    result = await _webapp._build_handoff(ctx, "1001:42", str(project_dir), "nonexistent-session-id")
    assert result == "", f"Expected '' for missing transcript, got: {result!r}"


# ─────────────────────────── 6. Consumer pop calls save_handoff ───────────────

async def test_handoff_injection_calls_save_handoff(aiohttp_client, tmp_path):
    """After handoff is injected (popped) in api_project_chat, save_handoff is called."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    save_called = []

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "response"}
        yield {"type": "result", "session_id": "new-sess", "context_tokens": 100}

    ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
    ctx["save_handoff"] = lambda: save_called.append(True)

    session_key = "1001:42"
    ctx["pending_handoff"][session_key] = "Summary from previous session."
    # No active session — fresh turn triggers injection
    assert session_key not in ctx["sessions"]

    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_QUEUE", {}):
        resp = await client.post(
            "/api/projects/myproject/chat",
            json={"prompt": "Continue"},
            headers=_auth_headers(ctx),
        )
        assert resp.status == 200
        await resp.read()

    assert save_called, "save_handoff must be called after injecting (popping) the handoff"
    assert session_key not in ctx["pending_handoff"], "pending_handoff entry must be cleared after injection"


# ─────────────────────────── 7. Existing rotate tests still pass ─────────────

async def test_rotate_existing_busy_409(aiohttp_client, tmp_path):
    """Existing test: busy project → 409 (regression guard)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    session_key = "1001:42"
    ctx["running"][session_key] = True
    ctx["sessions"][session_key] = "sess"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 409


async def test_rotate_response_includes_handoff_key_blank(aiohttp_client, tmp_path):
    """Blank rotate (no handoff) response always includes handoff:false key."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["sessions"]["1001:42"] = "sess"

    app = _make_rotate_app(ctx)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/myproject/rotate",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert "handoff" in data, f"Response must include 'handoff' key, got: {data}"
    assert data["handoff"] is False

"""
Tests for spec-039 Part 2: manual-reset eviction, graceful shutdown, PreCompact hook.
Tests for spec-039 shutdown regression fix: webapp.stop() + bounded teardown.

Covers:
- /reset (cmd_reset) evicts the live client
- api_project_rotate performs a real reset + evict and returns {ok, reset:true}
- SIGTERM handler saves sessions without self-killing (no os._exit, no kill)
- PreCompact hook emits an audit line and a bus event
- webapp.stop() cancels startup background loops promptly (regression: hang ≤12 s)
- webapp.stop() calls runner.cleanup() to release the TCP socket
- bounded teardown timeout backstop: _amain teardown never blocks past 12 s

All SDK calls are mocked — no real Claude subprocess is launched.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
import webapp as _webapp


# ─────────────────────────── shared helpers ─────────────────────────────────────────────────────

def _make_live_entry(session_key: str = "chat:99") -> bot._LiveEntry:
    """Build a fake _LiveEntry with a mock client that can be disconnected."""
    client = MagicMock()
    client.disconnect = AsyncMock()
    client.interrupt = AsyncMock()
    return bot._LiveEntry(
        client=client,
        fingerprint="fp",
        last_used=time.monotonic(),
        idle_task=None,
        session_key=session_key,
    )


def _make_ctx_with_live_client(session_key: str = "chat:99"):
    """Return (ctx, live_entry) with the entry pre-populated in ctx['live_clients']."""
    entry = _make_live_entry(session_key)
    ctx = {
        "live_clients": {session_key: entry},
        "running": {},
        "sessions": {session_key: "sess-abc"},
        "save_sessions": MagicMock(),
        "context_warned": set(),
        "evict_live_client": bot._evict_live_client,
    }
    return ctx, entry


# NOTE: the /reset (cmd_reset) eviction test was removed with the Telegram adapter
# (spec-040). The cockpit reset path is covered by test_api_project_rotate_resets_and_evicts below.

# ─────────────────────────── api_project_rotate does real reset+evict ──────────────────

@pytest.mark.asyncio
async def test_api_project_rotate_resets_and_evicts(tmp_path):
    """api_project_rotate must pop the session, evict the live client, and return
    {ok: true, reset: true} when there is an active session.
    """
    from aiohttp import web
    import webapp as _webapp
    from webapp import _derive_token

    session_key = "222:8"
    password = "testpass"

    ctx, entry = _make_ctx_with_live_client(session_key)
    ctx["password"] = password
    ctx["DATA"] = tmp_path / "data"
    ctx["topics"] = {
        session_key: {
            "project": "myproject",
            "cwd": str(tmp_path / "myproject"),
            "model": "sonnet",
            "tg_thread": session_key,
        }
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "myproject").mkdir(exist_ok=True)

    # Build a minimal aiohttp app for this endpoint only
    app = web.Application()
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/rotate", _webapp.api_project_rotate)

    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        # Authenticate with cookie
        resp = await client.post(
            "/api/projects/myproject/rotate",
            headers={"Cookie": f"ops_token={ctx['_auth_token']}"},
        )
        data = await resp.json()

    assert resp.status == 200, f"Expected 200, got {resp.status}: {data}"
    assert data.get("ok") is True, f"Expected ok=true: {data}"
    assert data.get("reset") is True, f"Expected reset=true: {data}"

    # Session must be removed
    assert session_key not in ctx["sessions"], "Session must be popped by rotate"
    ctx["save_sessions"].assert_called()

    # Live client must be evicted
    assert session_key not in ctx["live_clients"], "Live client must be evicted by rotate"
    entry.client.disconnect.assert_called_once()


# ─────────────────────────── Test 3: SIGTERM handler saves sessions, no self-kill ───────────────

@pytest.mark.asyncio
async def test_graceful_shutdown_saves_sessions_no_self_kill():
    """_graceful_shutdown must flush sessions.json and evict live clients.

    It must NOT call os._exit, sys.exit, os.kill, or any signal-based self-kill —
    systemd owns process termination (cgroup gotcha from GOTCHAS.md).
    """
    session_key = "333:9"
    entry = _make_live_entry(session_key)
    registry = {session_key: entry}

    save_called = []

    def _fake_save():
        save_called.append(True)

    with patch.object(engine, "save_sessions", _fake_save), \
         patch("os._exit", side_effect=AssertionError("os._exit must NOT be called")) as mock_exit, \
         patch("os.kill", side_effect=AssertionError("os.kill must NOT be called")) as mock_kill:

        await bot._graceful_shutdown(registry)

    # sessions.json must have been flushed
    assert save_called, "_graceful_shutdown must call save_sessions()"

    # Live client must be evicted and disconnected
    assert session_key not in registry, "Live client must be removed from registry"
    entry.client.disconnect.assert_called_once()

    # Verify os._exit / os.kill were NOT called (the patches raise if triggered — so
    # reaching here means they weren't called).
    assert not mock_exit.called, "os._exit must not be called in shutdown handler"
    assert not mock_kill.called, "os.kill must not be called in shutdown handler"


@pytest.mark.asyncio
async def test_graceful_shutdown_empty_registry():
    """_graceful_shutdown with an empty registry must not raise and must flush sessions."""
    save_called = []
    with patch.object(engine, "save_sessions", lambda: save_called.append(True)):
        await bot._graceful_shutdown({})
    assert save_called


@pytest.mark.asyncio
async def test_graceful_shutdown_tolerates_disconnect_failure():
    """A failing disconnect must not prevent other clients from being evicted."""
    entry_a = _make_live_entry("a:1")
    entry_a.client.disconnect = AsyncMock(side_effect=RuntimeError("disconnect failed"))
    entry_b = _make_live_entry("b:2")
    registry = {"a:1": entry_a, "b:2": entry_b}

    with patch.object(engine, "save_sessions", MagicMock()):
        # Must not raise even when disconnect fails on one entry
        await bot._graceful_shutdown(registry)

    # Both entries should be gone (evict removes the registry key before disconnect)
    # entry_b must also be evicted despite entry_a's failure
    assert "b:2" not in registry, "Healthy client must be evicted even when another fails"


# ─────────────────────────── Test 4: PreCompact hook emits audit + bus event ────────────────────

@pytest.mark.asyncio
async def test_pre_compact_hook_emits_audit_and_bus_event():
    """_make_pre_compact_hook must emit an audit line and call webapp._bus_publish
    with kind='compact' when the native auto-compact event fires.
    """
    project_name = "myproject"
    session_key = "chat:pre_compact"
    bus_events = []

    def _fake_bus_publish(sk, event, **kwargs):
        bus_events.append((sk, event))

    audit_lines = []

    def _fake_audit(project, kind, text):
        audit_lines.append((project, kind, text))

    hook_fn = engine._make_pre_compact_hook(project_name, session_key)

    # Simulate the SDK calling the hook with a PreCompact event
    fake_input = {"hook_event_name": "PreCompact", "trigger": "auto"}

    import webapp as _webapp
    with patch.object(engine, "audit", _fake_audit), \
         patch.object(_webapp, "_bus_publish", _fake_bus_publish), \
         patch.object(engine, "_bus_publish_cb", _fake_bus_publish):

        result = await hook_fn(fake_input, None, None)

    # Hook must return an empty dict (SyncHookJSONOutput, observe-only)
    assert result == {}, f"PreCompact hook must return empty dict; got {result}"

    # Audit line must be written
    assert len(audit_lines) == 1, f"Expected 1 audit line; got {audit_lines}"
    assert audit_lines[0][0] == project_name
    assert audit_lines[0][1] == "COMPACT"
    assert "auto" in audit_lines[0][2]

    # Bus event must be published
    assert len(bus_events) == 1, f"Expected 1 bus event; got {bus_events}"
    published_sk, published_event = bus_events[0]
    assert published_sk == session_key
    assert published_event["kind"] == "compact"
    assert published_event["trigger"] == "auto"
    assert published_event["project"] == project_name


@pytest.mark.asyncio
async def test_pre_compact_hook_never_raises():
    """PreCompact hook must never propagate exceptions — it's guarded end-to-end."""
    hook_fn = engine._make_pre_compact_hook("proj", "chat:x")

    # Pass a completely broken input — must still return {} without raising
    result = await hook_fn(None, None, None)
    assert result == {}


@pytest.mark.asyncio
async def test_pre_compact_hook_registered_in_opts(tmp_path):
    """run_engine must wire the PreCompact hook into opts.hooks alongside PostToolUse."""
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.__class__ = ResultMessage
    result_msg.session_id = "sess-compact"
    result_msg.total_cost_usd = None
    result_msg.api_error_status = None
    result_msg.duration_ms = None
    result_msg.duration_api_ms = None

    captured_opts = []

    def _fake_sdk_client(options):
        captured_opts.append(options)
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        async def _recv():
            yield result_msg

        client.receive_response = _recv
        return client

    with patch.object(engine, "PERSISTENT_CLIENT", False), \
         patch.object(engine, "ClaudeSDKClient", side_effect=_fake_sdk_client), \
         patch.object(engine, "audit", MagicMock()):

        async for _ in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="hello",
            session_key="chat:compact_test",
            model="sonnet",
            ctx={"running": {}, "live_clients": {}},
            ephemeral=False,
        ):
            pass

    assert len(captured_opts) == 1, "Expected exactly one ClaudeAgentOptions"
    hooks = captured_opts[0].hooks or {}
    assert "PreCompact" in hooks, (
        f"run_engine must register a PreCompact hook; got hooks keys: {list(hooks.keys())}"
    )
    assert "PostToolUse" in hooks, "PostToolUse hook must still be present"


# ─────────────────────────── Tests for shutdown regression fix ───────────────


@pytest.mark.asyncio
async def test_webapp_stop_cancels_startup_bg_tasks():
    """webapp.stop() must cancel all startup background tasks and return promptly.

    Regression: before the fix the 5 always-on background loops were never cancelled
    on shutdown, so asyncio.run(_amain()) blocked ~90 s until systemd SIGKILLed the
    process.  This test simulates a loop that would never exit on its own (swallows
    nothing but blocks on asyncio.sleep forever) and asserts that stop() completes
    within 2 s and that the task is done/cancelled afterwards.
    """
    # Save and reset the module-level state so this test is isolated.
    original_tasks = list(_webapp._STARTUP_BG_TASKS)
    original_runner = _webapp._runner
    _webapp._STARTUP_BG_TASKS.clear()
    _webapp._runner = None

    async def _never_ending_loop():
        """Simulates a background loop that only exits when cancelled."""
        while True:
            await asyncio.sleep(3600)  # effectively forever

    try:
        # Inject two fake startup tasks (simulates the 5 real loops)
        t1 = asyncio.create_task(_never_ending_loop())
        t2 = asyncio.create_task(_never_ending_loop())
        _webapp._STARTUP_BG_TASKS.extend([t1, t2])

        # stop() must return in well under 2 s (the loops sleep 3600 s each)
        start = asyncio.get_event_loop().time()
        await asyncio.wait_for(_webapp.stop(), timeout=2.0)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 2.0, f"webapp.stop() took too long: {elapsed:.2f}s"
        assert t1.done(), "Task 1 must be done (cancelled) after stop()"
        assert t2.done(), "Task 2 must be done (cancelled) after stop()"
        assert _webapp._STARTUP_BG_TASKS == [], "_STARTUP_BG_TASKS must be cleared after stop()"
    finally:
        # Restore original state
        _webapp._STARTUP_BG_TASKS.clear()
        _webapp._STARTUP_BG_TASKS.extend(original_tasks)
        _webapp._runner = original_runner
        # Ensure any leaked tasks are cancelled
        for t in [t for t in [t1, t2] if not t.done()]:  # type: ignore[possibly-undefined]
            t.cancel()


@pytest.mark.asyncio
async def test_webapp_stop_calls_runner_cleanup():
    """webapp.stop() must call runner.cleanup() to release the TCP socket."""
    original_tasks = list(_webapp._STARTUP_BG_TASKS)
    original_runner = _webapp._runner
    _webapp._STARTUP_BG_TASKS.clear()

    fake_runner = MagicMock()
    fake_runner.cleanup = AsyncMock()
    _webapp._runner = fake_runner

    try:
        await _webapp.stop()
        fake_runner.cleanup.assert_called_once()
        assert _webapp._runner is None, "_runner must be set to None after cleanup"
    finally:
        _webapp._STARTUP_BG_TASKS.clear()
        _webapp._STARTUP_BG_TASKS.extend(original_tasks)
        _webapp._runner = original_runner


@pytest.mark.asyncio
async def test_webapp_stop_tolerates_no_runner_no_tasks():
    """webapp.stop() must not raise when called before start() (no runner, no tasks)."""
    original_tasks = list(_webapp._STARTUP_BG_TASKS)
    original_runner = _webapp._runner
    _webapp._STARTUP_BG_TASKS.clear()
    _webapp._runner = None

    try:
        # Must complete without error in well under 1 s
        await asyncio.wait_for(_webapp.stop(), timeout=1.0)
    finally:
        _webapp._STARTUP_BG_TASKS.clear()
        _webapp._STARTUP_BG_TASKS.extend(original_tasks)
        _webapp._runner = original_runner


@pytest.mark.asyncio
async def test_webapp_stop_cancellation_swallowing_loop():
    """webapp.stop() must complete even if a loop body uses `except Exception` around awaits.

    Regression scenario: a loop that swallows all exceptions (including in Python 3.7
    where CancelledError was an Exception subclass) — stop() must still return promptly
    because the cancel() → gather() sequence forces the task to exit.
    """
    original_tasks = list(_webapp._STARTUP_BG_TASKS)
    original_runner = _webapp._runner
    _webapp._STARTUP_BG_TASKS.clear()
    _webapp._runner = None

    async def _bad_loop_swallows_everything():
        """Mimics a loop with except Exception that would swallow CancelledError on Py3.7.
        On Py3.8+ CancelledError is BaseException so it escapes; but we verify stop() handles
        both — the task.cancel() + gather() pair guarantees exit regardless."""
        while True:
            try:
                await asyncio.sleep(3600)
            except Exception:
                # On Python 3.7 this would swallow CancelledError — bad pattern.
                # On 3.8+ CancelledError is BaseException and escapes this catch.
                pass

    try:
        t = asyncio.create_task(_bad_loop_swallowing_everything := _bad_loop_swallows_everything())
        _webapp._STARTUP_BG_TASKS.append(t)

        start = asyncio.get_event_loop().time()
        await asyncio.wait_for(_webapp.stop(), timeout=2.0)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 2.0, f"stop() hung despite cancel(): {elapsed:.2f}s"
        assert t.done(), "Task must be done after stop()"
    finally:
        _webapp._STARTUP_BG_TASKS.clear()
        _webapp._STARTUP_BG_TASKS.extend(original_tasks)
        _webapp._runner = original_runner
        for t in [t] if not t.done() else []:  # type: ignore[possibly-undefined]
            t.cancel()

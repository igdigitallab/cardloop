"""
Tests for spec-028 Phases 0-2: persistent (long-lived) ClaudeSDKClient.

All tests exercise the PERSISTENT_CLIENT=1 code path by patching bot.PERSISTENT_CLIENT
directly.  Flag-OFF behaviour is covered by the existing 1074-test suite; these tests
add coverage for the NEW live-client branch only.

Mock strategy: mirrors test_spec017_orchestrator.py — a FakeLiveClient is used in place
of ClaudeSDKClient; we patch bot.ClaudeSDKClient and bot.PERSISTENT_CLIENT.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine


# ─────────────────────────── helpers ────────────────────────────────────────────────────────────

def _make_result_message():
    """Minimal fake ResultMessage (no cost, no session_id)."""
    from claude_agent_sdk import ResultMessage
    msg = MagicMock(spec=ResultMessage)
    msg.__class__ = ResultMessage
    msg.session_id = "sess-reuse"
    msg.total_cost_usd = None
    msg.api_error_status = None
    msg.duration_ms = None
    msg.duration_api_ms = None
    return msg


def _make_live_client(turn_messages_list):
    """Build a fake connected client that does NOT use async context manager.

    Supports multiple sequential turns: each call to .query() advances the turn index
    and receive_response() yields the corresponding messages.  This simulates the
    live-client pattern (query + receive_response called N times on one client).
    """
    client = MagicMock()
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    # connect() is called once by _get_or_create_live_client.
    client.connect = AsyncMock()

    turn_idx = [-1]  # mutable box so the closure can mutate it

    async def _query(prompt):
        turn_idx[0] += 1

    async def _receive():
        idx = turn_idx[0]
        msgs = turn_messages_list[idx] if idx < len(turn_messages_list) else []
        for m in msgs:
            yield m

    client.query = _query
    client.receive_response = _receive
    # The live-client path does NOT use __aenter__/__aexit__, but the fresh-client path does.
    # We intentionally skip adding them here to catch any accidental `async with` on live clients.
    return client


def _make_ctx(running=None, live_clients=None):
    """Minimal ctx dict for run_engine (Spec-028 Phase 1)."""
    return {
        "running": running if running is not None else {},
        "live_clients": live_clients if live_clients is not None else {},
    }


# ─────────────────────────── Test 1: reuse, no bleed ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_client_reused_across_turns(tmp_path):
    """Two sequential turns on flag-ON must reuse the SAME client instance (no reconnect).

    Turn 1 yields a text event; turn 2 yields a different text event.  After both turns
    the client's connect() must have been called exactly once and the events must contain
    no cross-contamination (turn 1 text not in turn 2 events and vice versa).
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    # Build two turns worth of messages.
    def _text_msg(text):
        msg = MagicMock(spec=AssistantMessage)
        msg.__class__ = AssistantMessage
        msg.parent_tool_use_id = None  # spec-071: real default — engine filters parented messages
        blk = MagicMock(spec=TextBlock)
        blk.__class__ = TextBlock
        blk.text = text
        msg.content = [blk]
        msg.usage = {}
        return msg

    turn1_msgs = [_text_msg("hello from turn 1"), _make_result_message()]
    turn2_msgs = [_text_msg("hello from turn 2"), _make_result_message()]

    live_client = _make_live_client([turn1_msgs, turn2_msgs])
    ctx = _make_ctx()

    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", return_value=live_client), \
         patch.object(engine, "audit", lambda *a: None):

        # Turn 1
        events1 = []
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="turn 1",
            session_key="chat:1",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            events1.append(ev)

        # Turn 2 — same session_key
        events2 = []
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="turn 2",
            session_key="chat:1",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            events2.append(ev)

    # connect() should have been called exactly once (one live-client creation).
    assert live_client.connect.call_count == 1, (
        f"Expected connect() called once; got {live_client.connect.call_count}"
    )

    # Turn 1 text should appear only in events1.
    text1 = [e["text"] for e in events1 if e.get("type") == "text"]
    text2 = [e["text"] for e in events2 if e.get("type") == "text"]
    assert "hello from turn 1" in text1, f"Turn 1 text missing from events1: {text1}"
    assert "hello from turn 2" not in text1, f"Turn 2 text leaked into events1: {text1}"
    assert "hello from turn 2" in text2, f"Turn 2 text missing from events2: {text2}"
    assert "hello from turn 1" not in text2, f"Turn 1 text leaked into events2: {text2}"

    # Entry stays alive in the registry (not evicted on normal completion).
    assert "chat:1" in ctx["live_clients"], "Live entry should still be in registry after normal turns"

    # Clean up the idle task to avoid asyncio warnings.
    entry = ctx["live_clients"].get("chat:1")
    if entry and entry.idle_task and not entry.idle_task.done():
        entry.idle_task.cancel()
        await asyncio.sleep(0)  # allow the task to observe cancellation


# ─────────────────────────── Test 2: running[] race / interrupt guard ───────────────────────────


@pytest.mark.asyncio
async def test_running_placeholder_and_interrupt_guard(tmp_path):
    """During the connect window running[k] may be the True placeholder.

    The watchdog uses hasattr(cl, 'interrupt') before calling interrupt().
    Assert:
    1. The True placeholder does NOT have an 'interrupt' attribute — the guard prevents AttributeError.
    2. After the real client is installed, interrupt reaches it.
    """
    # Guard: True placeholder must not have 'interrupt'.
    assert not hasattr(True, "interrupt"), "True should not have 'interrupt' — watchdog guard assumption broken"

    from claude_agent_sdk import AssistantMessage, ResultMessage

    result_msg = _make_result_message()

    live_client = _make_live_client([[result_msg]])

    running_dict = {}
    ctx = _make_ctx(running=running_dict)

    # Install the True placeholder as done by on_message / _drain_tg_queue.
    running_dict["chat:2"] = True

    # Assert guard: hasattr(True, 'interrupt') is False, so watchdog would NOT call True.interrupt().
    cl_placeholder = running_dict["chat:2"]
    assert not hasattr(cl_placeholder, "interrupt"), (
        "Watchdog guard: True placeholder must NOT have 'interrupt'"
    )

    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", return_value=live_client), \
         patch.object(engine, "audit", lambda *a: None):

        # run_engine should replace the True placeholder with the real client.
        async for _ in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="test",
            session_key="chat:2",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            pass

    # After the turn, the real client should be reachable via interrupt.
    # (In this test the turn finished normally so running is popped, but the live entry persists.)
    entry = ctx["live_clients"].get("chat:2")
    assert entry is not None, "Live entry should exist after a successful turn"
    assert hasattr(entry.client, "interrupt"), "Real client must have 'interrupt' attribute"
    # Calling interrupt on the real client should not raise.
    await entry.client.interrupt()
    assert entry.client.interrupt.called

    # Clean up idle task.
    if entry.idle_task and not entry.idle_task.done():
        entry.idle_task.cancel()
        await asyncio.sleep(0)


# ─────────────────────────── Test 3: dead-subprocess evict + fresh reconnect ────────────────────


@pytest.mark.asyncio
async def test_dead_client_evicted_and_fresh_on_next_turn(tmp_path):
    """A live client that raises on query() must be evicted; the next turn creates a fresh one."""
    from claude_agent_sdk import ResultMessage

    # First client raises on query — simulates a dead subprocess.
    dead_client = MagicMock()
    dead_client.connect = AsyncMock()
    dead_client.disconnect = AsyncMock()
    dead_client.interrupt = AsyncMock()

    async def _bad_query(prompt):
        raise RuntimeError("subprocess died")

    dead_client.query = _bad_query

    # Second client succeeds normally.
    result_msg = _make_result_message()
    fresh_client = _make_live_client([[result_msg]])

    # ClaudeSDKClient is called once per _get_or_create_live_client.
    clients_created = []

    def _factory(options):
        client = dead_client if len(clients_created) == 0 else fresh_client
        clients_created.append(client)
        return client

    ctx = _make_ctx()

    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", side_effect=_factory), \
         patch.object(engine, "audit", lambda *a: None):

        # Turn 1 — dead client, should yield an error event and evict.
        events1 = []
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="turn 1",
            session_key="chat:3",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            events1.append(ev)

        # The dead entry must be gone from the registry.
        assert "chat:3" not in ctx["live_clients"], (
            "Dead client must be evicted from registry after error"
        )

        # Turn 1 must have produced an error event.
        error_events = [e for e in events1 if e.get("type") == "error"]
        assert error_events, f"Expected error event from dead client, got: {events1}"
        assert "subprocess died" in str(error_events[0]["exc"])

        # Turn 2 — should create a fresh client and succeed.
        events2 = []
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="turn 2",
            session_key="chat:3",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            events2.append(ev)

    assert len(clients_created) == 2, (
        f"Expected 2 client creations (dead + fresh); got {len(clients_created)}"
    )
    # The second client should be the fresh one and connect() called on it.
    assert fresh_client.connect.call_count == 1, (
        f"Fresh client connect() should be called once; got {fresh_client.connect.call_count}"
    )
    # No error events on turn 2.
    error2 = [e for e in events2 if e.get("type") == "error"]
    assert not error2, f"Turn 2 (fresh client) must not yield error events: {error2}"

    # Clean up idle task on the fresh entry.
    entry = ctx["live_clients"].get("chat:3")
    if entry and entry.idle_task and not entry.idle_task.done():
        entry.idle_task.cancel()
        await asyncio.sleep(0)


# ─────────────────────────── Test 4: flag-OFF path unchanged ────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_uses_fresh_client_each_turn(tmp_path):
    """With PERSISTENT_CLIENT=0 (default) each turn uses a fresh `async with` client.

    _live_clients must stay empty regardless of how many turns are run.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage

    result_msg = _make_result_message()
    # A proper context-manager-style fake client for the `async with` path.
    fresh_client = MagicMock()
    fresh_client.connect = AsyncMock()
    fresh_client.disconnect = AsyncMock()
    fresh_client.interrupt = AsyncMock()
    fresh_client.query = AsyncMock()
    fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
    fresh_client.__aexit__ = AsyncMock(return_value=False)

    async def _receive():
        yield result_msg

    fresh_client.receive_response = _receive

    ctx = _make_ctx()

    with patch.object(engine, "PERSISTENT_CLIENT", False), \
         patch.object(engine, "ClaudeSDKClient", return_value=fresh_client), \
         patch.object(engine, "audit", lambda *a: None):

        async for _ in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="hello",
            session_key="chat:4",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            pass

    # No live entries should be created with flag OFF.
    assert len(ctx["live_clients"]) == 0, (
        f"Flag-OFF: live_clients must be empty; got {ctx['live_clients']}"
    )
    # The `async with` path uses __aenter__ / __aexit__.
    assert fresh_client.__aenter__.called, "Flag-OFF path must use async context manager"


# ─────────────────────────── Test 5: card/rotation always use fresh client ──────────────────────


@pytest.mark.asyncio
async def test_ephemeral_never_stores_live_client(tmp_path):
    """_run_card passes ephemeral=True (spec-039: _do_session_rotation removed).

    Even with flag ON no live entry should be stored for ephemeral session keys.
    """
    result_msg = _make_result_message()
    fresh_client = MagicMock()
    fresh_client.connect = AsyncMock()
    fresh_client.disconnect = AsyncMock()
    fresh_client.interrupt = AsyncMock()
    fresh_client.query = AsyncMock()
    fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
    fresh_client.__aexit__ = AsyncMock(return_value=False)

    async def _receive():
        yield result_msg

    fresh_client.receive_response = _receive

    ctx = _make_ctx()

    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", return_value=fresh_client), \
         patch.object(engine, "audit", lambda *a: None):

        async for _ in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="card task",
            session_key="card:abc123",
            model="sonnet",
            ctx=ctx,
            ephemeral=True,   # as _run_card would pass
        ):
            pass

    # Ephemeral call must NOT populate the live-client registry.
    assert "card:abc123" not in ctx["live_clients"], (
        "ephemeral=True must never store a live client in the registry"
    )
    # Must have used the `async with` path.
    assert fresh_client.__aenter__.called, "ephemeral=True must use async context manager path"


# ─────────────────────────── Test 6: model switch triggers eviction + reconnect ─────────────────


@pytest.mark.asyncio
async def test_model_switch_evicts_and_reconnects(tmp_path):
    """Changing the model (different fingerprint) must evict the old client and reconnect.

    We simulate a model switch by running two turns with different model values.
    The second turn should find a fingerprint mismatch, evict, and create a new entry.
    """
    from claude_agent_sdk import ResultMessage

    result_msg1 = _make_result_message()
    result_msg2 = _make_result_message()

    client_a = _make_live_client([[result_msg1]])
    client_b = _make_live_client([[result_msg2]])
    clients = [client_a, client_b]
    call_count = [0]

    def _factory(options):
        client = clients[call_count[0]]
        call_count[0] += 1
        return client

    ctx = _make_ctx()

    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", side_effect=_factory), \
         patch.object(engine, "audit", lambda *a: None):

        # Turn 1 with sonnet
        async for _ in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="hello",
            session_key="chat:5",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            pass

        # Verify client_a is in the registry.
        entry_before = ctx["live_clients"].get("chat:5")
        assert entry_before is not None, "Entry should exist after turn 1"
        assert entry_before.client is client_a

        # Turn 2 with haiku — fingerprint will differ (model field changed).
        async for _ in bot.run_engine(
            project_name="proj",
            cwd=str(tmp_path),
            prompt="hello",
            session_key="chat:5",
            model="haiku",
            ctx=ctx,
            ephemeral=False,
        ):
            pass

    # After the model switch, the registry should hold the new client (client_b).
    entry_after = ctx["live_clients"].get("chat:5")
    assert entry_after is not None, "Entry should exist after turn 2"
    assert entry_after.client is client_b, (
        "Model switch must evict old client and replace with fresh one"
    )
    assert call_count[0] == 2, f"Expected 2 client creations; got {call_count[0]}"

    # Disconnect must have been called on client_a (evicted).
    assert client_a.disconnect.called, "Old client must be disconnected on eviction"

    # Clean up.
    entry = ctx["live_clients"].get("chat:5")
    if entry and entry.idle_task and not entry.idle_task.done():
        entry.idle_task.cancel()
        await asyncio.sleep(0)

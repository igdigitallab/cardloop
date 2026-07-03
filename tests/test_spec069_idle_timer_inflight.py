"""
spec-069 Phase 1 (RC#1): the live-client idle/LRU eviction must NEVER kill a turn that is
still in-flight.

Before the fix, LIVE_CLIENT_TTL_SEC was armed once at turn start and never re-checked, so a
long orchestration that legitimately ran past the TTL got SIGTERMed mid-turn and died silently.
These tests exercise:
  - engine._schedule_idle_eviction — the idle timer defers eviction while a turn is running.
  - engine._get_or_create_live_client — LRU eviction skips in-flight sessions.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine


def _fake_client():
    c = MagicMock()
    c.connect = AsyncMock()
    c.disconnect = AsyncMock()
    return c


def _entry(session_key, last_used=0.0):
    return engine._LiveEntry(
        client=_fake_client(), fingerprint="fp", last_used=last_used,
        idle_task=None, session_key=session_key,
    )


@pytest.mark.asyncio
async def test_idle_eviction_deferred_while_turn_in_flight(monkeypatch):
    """TTL lapses repeatedly while the session's turn is running → must NOT evict."""
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 0.02)
    running = {"chat:1": True}                       # simulate an in-flight turn
    live = {"chat:1": _entry("chat:1")}
    ctx = {"running": running, "live_clients": live}
    client = live["chat:1"].client
    task = engine._schedule_idle_eviction("chat:1", ctx)
    live["chat:1"].idle_task = task
    try:
        await asyncio.sleep(0.12)                    # ~6 TTLs elapse mid-turn
        assert "chat:1" in live, "RC#1 regression: live client evicted mid-turn"
        assert not client.disconnect.called, "RC#1 regression: disconnect called mid-turn"
        # Turn ends → session goes idle → a subsequent TTL cycle evicts.
        running.pop("chat:1", None)
        await asyncio.sleep(0.08)
        assert "chat:1" not in live, "idle client not evicted after the turn ended"
        assert client.disconnect.called
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_idle_eviction_fires_when_never_in_flight(monkeypatch):
    """Baseline preserved: an idle client (never in `running`) is evicted after one TTL."""
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 0.02)
    live = {"chat:2": _entry("chat:2")}
    ctx = {"running": {}, "live_clients": live}
    task = engine._schedule_idle_eviction("chat:2", ctx)
    live["chat:2"].idle_task = task
    try:
        await asyncio.sleep(0.08)
        assert "chat:2" not in live, "idle client should have been evicted after TTL"
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_lru_skips_in_flight_sessions(monkeypatch):
    """LRU eviction must never disconnect a client whose turn is in-flight — it defers instead,
    then evicts the oldest IDLE client when one becomes available."""
    monkeypatch.setattr(engine, "PERSISTENT_CLIENT", True)
    monkeypatch.setattr(engine, "LIVE_CLIENT_MAX", 2)
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 999)          # keep idle timers dormant
    monkeypatch.setattr(engine, "_compute_fingerprint", lambda *a, **k: "fp")

    created = []

    def _new_client(*a, **k):
        c = _fake_client()
        created.append(c)
        return c

    monkeypatch.setattr(engine, "ClaudeSDKClient", _new_client)

    running = {"a": True, "b": True}                                 # both existing clients BUSY
    e_a, e_b = _entry("a", 1.0), _entry("b", 2.0)
    live = {"a": e_a, "b": e_b}
    ctx = {"running": running, "live_clients": live}
    opts = MagicMock()

    # Registry at MAX (2) and both busy → new session must NOT evict either (temp overflow).
    new_client = await engine._get_or_create_live_client(ctx, "c", opts, ephemeral=False)
    assert not e_a.client.disconnect.called, "busy client 'a' was LRU-evicted (RC#1 regression)"
    assert not e_b.client.disconnect.called, "busy client 'b' was LRU-evicted (RC#1 regression)"
    assert "c" in live and new_client is created[-1], "new client not created under overflow"

    # 'a' goes idle → forcing another create must LRU-evict the IDLE 'a', never the busy 'b'.
    running.pop("a", None)
    await engine._get_or_create_live_client(ctx, "d", opts, ephemeral=False)
    assert e_a.client.disconnect.called, "idle client 'a' should have been LRU-evicted"
    assert not e_b.client.disconnect.called, "busy client 'b' must be spared by LRU"

    for entry in list(live.values()):
        if entry.idle_task and not entry.idle_task.done():
            entry.idle_task.cancel()
    await asyncio.sleep(0)

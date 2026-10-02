"""
Idle-TTL eviction must actually reap the CLI subprocess.

Regression (2026-10-01, ops): 21 of 21 clients evicted by `idle TTL expired` stayed alive for
20-31 h (~450 MB each with their MCP children) while the 138 memory-guard evictions and the
4 fingerprint evictions all died cleanly. The cgroup sat at 97-100 %, so the memory guard kept
evicting the operator's real idle chats and every next message was a cold resume.

Cause: `_idle_waiter` awaits `_evict_live_client` from INSIDE the idle task, and the evictor
cancelled `entry.idle_task` - i.e. itself. The cancel lands on the first real suspension point
of `client.disconnect()`; the SDK's close() shield only defers anyio cancellation, so a raw
asyncio cancel skips the terminate/kill escalation. CancelledError is a BaseException, so
nothing was logged either.

The older tests use `AsyncMock` for disconnect, which never yields to the loop - the cancel had
nowhere to land and the bug was invisible. The client below yields before the child is reaped,
as the real SDK does.
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine


class _YieldingClient:
    """disconnect() suspends before the child is reaped, like the SDK's transport close."""

    def __init__(self):
        self.reaped = False

    async def disconnect(self):
        await asyncio.sleep(0)
        self.reaped = True


def _registry(key):
    client = _YieldingClient()
    entry = engine._LiveEntry(
        client=client, fingerprint="fp", last_used=0.0, idle_task=None, session_key=key,
    )
    return client, entry


@pytest.mark.asyncio
async def test_idle_ttl_eviction_reaps_the_subprocess(monkeypatch):
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 0.02)
    client, entry = _registry("chat:ttl")
    live = {"chat:ttl": entry}
    ctx = {"running": {}, "live_clients": live}
    entry.idle_task = engine._schedule_idle_eviction("chat:ttl", ctx)
    await asyncio.sleep(0.1)
    assert "chat:ttl" not in live
    assert client.reaped, "idle-TTL eviction abandoned disconnect() - the CLI child is leaked"


@pytest.mark.asyncio
async def test_slow_disconnect_outliving_the_wait_is_not_abandoned(monkeypatch):
    """The wait_for timeout is itself a raw cancel: the SDK escalation (EOF -> SIGTERM ->
    SIGKILL) can take ~20 s, so a disconnect slower than our wait must still run to the end."""
    monkeypatch.setattr(engine, "_DISCONNECT_WAIT_SEC", 0.01)

    class _Slow:
        reaped = False

        async def disconnect(self):
            await asyncio.sleep(0.08)
            self.reaped = True

    client = _Slow()
    entry = engine._LiveEntry(client=client, fingerprint="fp", last_used=0.0,
                              idle_task=None, session_key="chat:slow")
    ctx = {"running": {}, "live_clients": {"chat:slow": entry}}
    await engine._evict_live_client("chat:slow", ctx)      # returns after ~0.01 s
    assert not client.reaped
    await asyncio.sleep(0.2)
    assert client.reaped, "a disconnect slower than the wait was cancelled - CLI child leaked"
    assert not engine._disconnects_in_flight


@pytest.mark.asyncio
async def test_eviction_from_another_task_still_reaps_and_cancels_the_timer(monkeypatch):
    """The memory-guard / fingerprint shape: the evictor is NOT the idle task."""
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 60)
    client, entry = _registry("chat:guard")
    live = {"chat:guard": entry}
    ctx = {"running": {}, "live_clients": live}
    entry.idle_task = engine._schedule_idle_eviction("chat:guard", ctx)
    timer = entry.idle_task
    await engine._evict_live_client("chat:guard", ctx)
    await asyncio.sleep(0)
    assert client.reaped
    assert timer.done(), "the pending idle timer must be cancelled by an outside evictor"

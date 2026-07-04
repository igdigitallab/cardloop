"""
spec-069 f9d60d + 643ecf: eviction must not kill a client while its BACKGROUND sub-agents
are still running, and the monitor panel must not accumulate duplicate / stale rows.

RC#1 (test_spec069_idle_timer_inflight.py) proved eviction defers while the MAIN turn is
in-flight. But background Agent sub-agents are subprocesses of the same live client and
outlive the turn — when the turn ended (session left `running`) the client became
evict-eligible and disconnect() SIGTERMed the still-working sub-agents (exit 143, false
'failed' monitors, lost work). These tests exercise:
  - engine._schedule_idle_eviction / _get_or_create_live_client — defer while sub-agents live.
  - engine fingerprint-change path — reuse (defer option change) while sub-agents live.
  - webapp._has_live_agent_monitors — the liveness predicate engine consults.
  - webapp._monitor_update dedup-by-label + _monitors_clear_terminal_agents (643ecf).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp


def _fake_client():
    c = MagicMock()
    c.connect = AsyncMock()
    c.disconnect = AsyncMock()
    return c


def _entry(session_key, last_used=0.0, fingerprint="fp"):
    return engine._LiveEntry(
        client=_fake_client(), fingerprint=fingerprint, last_used=last_used,
        idle_task=None, session_key=session_key,
    )


# ─────────────────────────── engine eviction guards (f9d60d) ──────────────────────────────

@pytest.mark.asyncio
async def test_idle_eviction_deferred_while_subagents_live(monkeypatch):
    """Turn has ENDED (not in `running`), but background sub-agents are still working →
    the idle TTL must DEFER eviction; once they finish, a later TTL cycle evicts."""
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 0.02)
    subagents_live = {"chat:1"}
    monkeypatch.setattr(engine, "_has_live_subagents_cb", lambda k: k in subagents_live)
    running = {}  # the main turn is over — only sub-agents keep the client busy
    live = {"chat:1": _entry("chat:1")}
    ctx = {"running": running, "live_clients": live}
    client = live["chat:1"].client
    task = engine._schedule_idle_eviction("chat:1", ctx)
    live["chat:1"].idle_task = task
    try:
        await asyncio.sleep(0.12)  # several TTLs elapse while sub-agents run
        assert "chat:1" in live, "f9d60d regression: client evicted while sub-agents live"
        assert not client.disconnect.called, "f9d60d regression: disconnect SIGTERMs live sub-agents"
        subagents_live.discard("chat:1")  # sub-agents finish
        await asyncio.sleep(0.08)
        assert "chat:1" not in live, "client not evicted after sub-agents finished"
        assert client.disconnect.called
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_lru_skips_sessions_with_live_subagents(monkeypatch):
    """LRU must treat a session with running sub-agents as busy (never disconnect it),
    then evict it once the sub-agents finish."""
    monkeypatch.setattr(engine, "PERSISTENT_CLIENT", True)
    monkeypatch.setattr(engine, "LIVE_CLIENT_MAX", 2)
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 999)
    monkeypatch.setattr(engine, "_compute_fingerprint", lambda *a, **k: "fp")

    created = []

    def _new_client(*a, **k):
        c = _fake_client()
        created.append(c)
        return c

    monkeypatch.setattr(engine, "ClaudeSDKClient", _new_client)

    subagents_live = {"a", "b"}
    monkeypatch.setattr(engine, "_has_live_subagents_cb", lambda k: k in subagents_live)
    running = {}  # turns ended; only sub-agents hold the clients
    e_a, e_b = _entry("a", 1.0), _entry("b", 2.0)
    live = {"a": e_a, "b": e_b}
    ctx = {"running": running, "live_clients": live}
    opts = MagicMock()

    # Registry at MAX(2), both busy via sub-agents → new session must NOT evict either.
    await engine._get_or_create_live_client(ctx, "c", opts, ephemeral=False)
    assert not e_a.client.disconnect.called, "f9d60d regression: sub-agent-busy 'a' LRU-evicted"
    assert not e_b.client.disconnect.called, "f9d60d regression: sub-agent-busy 'b' LRU-evicted"

    # 'a' sub-agents finish → forcing another create must evict the now-idle 'a', never 'b'.
    subagents_live.discard("a")
    await engine._get_or_create_live_client(ctx, "d", opts, ephemeral=False)
    assert e_a.client.disconnect.called, "idle 'a' should be LRU-evicted after sub-agents done"
    assert not e_b.client.disconnect.called, "sub-agent-busy 'b' must be spared by LRU"

    for entry in list(live.values()):
        if entry.idle_task and not entry.idle_task.done():
            entry.idle_task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_fingerprint_change_deferred_while_subagents_live(monkeypatch):
    """A fingerprint (option) change must NOT evict a client whose sub-agents are live —
    it reuses the existing client, deferring the option change so the sub-agents survive."""
    monkeypatch.setattr(engine, "PERSISTENT_CLIENT", True)
    monkeypatch.setattr(engine, "LIVE_CLIENT_MAX", 10)
    monkeypatch.setattr(engine, "LIVE_CLIENT_TTL_SEC", 999)
    monkeypatch.setattr(engine, "_compute_fingerprint", lambda *a, **k: "new")  # request wants "new"
    monkeypatch.setattr(engine, "_has_live_subagents_cb", lambda k: True)       # sub-agents live

    e = _entry("s", fingerprint="old")  # existing client on the OLD fingerprint
    live = {"s": e}
    ctx = {"running": {}, "live_clients": live}
    client = await engine._get_or_create_live_client(ctx, "s", MagicMock(), ephemeral=False)
    assert not e.client.disconnect.called, "f9d60d regression: fingerprint change evicted live sub-agents"
    assert client is e.client, "should reuse existing client, deferring the option change"
    if e.idle_task and not e.idle_task.done():
        e.idle_task.cancel()
    await asyncio.sleep(0)


# ─────────────────────────── webapp monitor liveness + dedup (643ecf) ──────────────────────

@pytest.fixture
def clean_monitors(monkeypatch):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()
    yield
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()


def test_has_live_agent_monitors(clean_monitors):
    sk = "proj:1"
    assert webapp._has_live_agent_monitors(sk) is False
    webapp._monitor_update(sk, {"id": "ag1", "kind": "agent", "label": "L", "status": "running"})
    assert webapp._has_live_agent_monitors(sk) is True
    webapp._monitor_update(sk, {"id": "ag1", "status": "done"})
    assert webapp._has_live_agent_monitors(sk) is False


def test_non_agent_running_is_not_a_live_subagent(clean_monitors):
    """A running bg-bash / Workflow monitor must NOT hold the client — only real Agent sub-agents."""
    sk = "proj:1b"
    webapp._monitor_update(sk, {"id": "sh1", "kind": "task", "label": "bash", "status": "running"})
    assert webapp._has_live_agent_monitors(sk) is False


def test_agent_monitor_dedup_by_label(clean_monitors):
    """A relaunched sub-agent (new agentId, same label) drops the stale terminal duplicate."""
    sk = "proj:2"
    webapp._monitor_update(sk, {"id": "ag1", "kind": "agent", "label": "research X", "status": "running"})
    webapp._monitor_update(sk, {"id": "ag1", "status": "failed"})
    # Relaunch under a NEW id, SAME label.
    webapp._monitor_update(sk, {"id": "ag2", "kind": "agent", "label": "research X", "status": "running"})
    bucket = webapp._monitors[sk]
    assert "ag1" not in bucket, "643ecf: stale DONE/FAILED duplicate not removed on relaunch"
    assert "ag2" in bucket
    assert sum(1 for r in bucket.values() if r.get("label") == "research X") == 1


def test_dedup_keeps_a_still_running_same_label(clean_monitors):
    """Dedup only drops TERMINAL same-label rows — a still-running one must be left alone."""
    sk = "proj:2b"
    webapp._monitor_update(sk, {"id": "ag1", "kind": "agent", "label": "dup", "status": "running"})
    webapp._monitor_update(sk, {"id": "ag2", "kind": "agent", "label": "dup", "status": "running"})
    bucket = webapp._monitors[sk]
    assert "ag1" in bucket and "ag2" in bucket, "running same-label row wrongly removed"


def test_clear_terminal_agents(clean_monitors):
    """Turn-start cleanup drops terminal AGENT rows, keeps running agents + non-agent monitors."""
    sk = "proj:3"
    webapp._monitor_update(sk, {"id": "a_done", "kind": "agent", "label": "A", "status": "running"})
    webapp._monitor_update(sk, {"id": "a_done", "status": "done"})
    webapp._monitor_update(sk, {"id": "a_run", "kind": "agent", "label": "B", "status": "running"})
    webapp._monitor_update(sk, {"id": "sh1", "kind": "task", "label": "bash", "status": "failed"})
    webapp._monitors_clear_terminal_agents(sk)
    bucket = webapp._monitors.get(sk, {})
    assert "a_done" not in bucket, "terminal agent monitor not cleared"
    assert "a_run" in bucket, "running agent monitor wrongly cleared"
    assert "sh1" in bucket, "non-agent monitor wrongly cleared"

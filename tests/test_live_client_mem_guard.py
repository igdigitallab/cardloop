"""
Tests for the live-client MEMORY headroom guard (engine._enforce_memory_headroom).

Why this exists: LIVE_CLIENT_MAX bounds how MANY CLI subprocesses the registry holds, not
how much memory they use. On ops the cgroup reached MemoryMax twice with the registry legally
under the count cap (2026-08-26 13:43, 2026-08-27 09:11); the kernel OOM-killed a `claude`
child and systemd restarted the service mid-turn. The guard evicts idle clients before
connecting another one.

Mock strategy mirrors test_spec028_persistent_client.py: fake registry entries plus a patched
_evict_live_client, so no subprocess is ever spawned.
"""
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine


def _entry(session_key: str, last_used: float) -> engine._LiveEntry:
    return engine._LiveEntry(
        client=MagicMock(),
        fingerprint="fp",
        last_used=last_used,
        idle_task=None,
        session_key=session_key,
    )


def _registry(*keys: str) -> dict:
    """Registry whose entries were last used in the order given (first = oldest)."""
    now = time.monotonic()
    return {key: _entry(key, now - (len(keys) - i)) for i, key in enumerate(keys)}


@pytest.mark.asyncio
async def test_evicts_lru_idle_client_when_over_the_guard():
    registry = _registry("old", "new")
    evicted = []

    async def _fake_evict(session_key, ctx):
        evicted.append(session_key)
        registry.pop(session_key, None)

    # 0.90 → over the 0.75 guard; after the first eviction the reading drops below it.
    readings = iter([0.90, 0.60])

    with patch.object(engine, "LIVE_CLIENT_MEM_GUARD", 0.75), \
         patch.object(engine, "_cgroup_mem_fraction", lambda: next(readings)), \
         patch.object(engine, "_session_has_live_subagents", lambda _k: False), \
         patch.object(engine, "_evict_live_client", _fake_evict):
        await engine._enforce_memory_headroom(registry, None, {})

    assert evicted == ["old"], "must evict the least-recently-used idle client first"
    assert "new" in registry


@pytest.mark.asyncio
async def test_never_evicts_a_busy_client():
    """A client whose turn is in flight, or which still has live sub-agents, is untouchable —
    evicting it would disconnect() → SIGTERM live work."""
    registry = _registry("running-one", "with-subagents")
    evict = AsyncMock()

    with patch.object(engine, "LIVE_CLIENT_MEM_GUARD", 0.75), \
         patch.object(engine, "_cgroup_mem_fraction", lambda: 0.99), \
         patch.object(engine, "_session_has_live_subagents", lambda k: k == "with-subagents"), \
         patch.object(engine, "_evict_live_client", evict):
        await engine._enforce_memory_headroom(registry, None, {"running-one": object()})

    evict.assert_not_awaited()
    assert len(registry) == 2


@pytest.mark.asyncio
async def test_stops_when_a_reading_does_not_improve():
    """A disconnect frees memory asynchronously. One flat reading must end the loop rather
    than cascade into evicting the whole registry."""
    registry = _registry("a", "b", "c")
    evicted = []

    async def _fake_evict(session_key, ctx):
        evicted.append(session_key)
        registry.pop(session_key, None)

    with patch.object(engine, "LIVE_CLIENT_MEM_GUARD", 0.75), \
         patch.object(engine, "_cgroup_mem_fraction", lambda: 0.95), \
         patch.object(engine, "_session_has_live_subagents", lambda _k: False), \
         patch.object(engine, "_evict_live_client", _fake_evict):
        await engine._enforce_memory_headroom(registry, None, {})

    assert evicted == ["a"], "a non-improving reading stops the loop after one eviction"


@pytest.mark.asyncio
@pytest.mark.parametrize("guard,fraction", [(0.0, 0.99), (0.75, None)])
async def test_guard_is_inert_when_disabled_or_unmeasurable(guard, fraction):
    """LIVE_CLIENT_MEM_GUARD=0 disables it; an unmeasurable cgroup (no limit / not cgroup v2)
    returns None and must be treated as 'no signal', never as 0.0."""
    registry = _registry("a", "b")
    evict = AsyncMock()

    with patch.object(engine, "LIVE_CLIENT_MEM_GUARD", guard), \
         patch.object(engine, "_cgroup_mem_fraction", lambda: fraction), \
         patch.object(engine, "_evict_live_client", evict):
        await engine._enforce_memory_headroom(registry, None, {})

    evict.assert_not_awaited()
    assert len(registry) == 2


def test_cgroup_fraction_reads_a_real_v2_hierarchy(tmp_path, monkeypatch):
    """memory.current / memory.max, read through /proc/self/cgroup's 0:: line."""
    cgroup_root = tmp_path / "sys" / "fs" / "cgroup"
    unit = cgroup_root / "system.slice" / "cardloop.service"
    unit.mkdir(parents=True)
    (unit / "memory.max").write_text("10737418240\n")
    (unit / "memory.current").write_text("8589934592\n")
    proc_cgroup = tmp_path / "proc_self_cgroup"
    proc_cgroup.write_text("0::/system.slice/cardloop.service\n")

    real_open = open

    def _fake_open(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return real_open(proc_cgroup, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)
    monkeypatch.setattr(engine, "Path", lambda p="": tmp_path / str(p).lstrip("/")
                        if str(p).startswith("/sys/fs/cgroup") else Path(p))

    assert engine._cgroup_mem_fraction() == pytest.approx(0.8)


def test_cgroup_fraction_is_none_without_a_limit(tmp_path, monkeypatch):
    cgroup_root = tmp_path / "sys" / "fs" / "cgroup"
    unit = cgroup_root / "user.slice"
    unit.mkdir(parents=True)
    (unit / "memory.max").write_text("max\n")
    proc_cgroup = tmp_path / "proc_self_cgroup"
    proc_cgroup.write_text("0::/user.slice\n")

    real_open = open

    def _fake_open(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return real_open(proc_cgroup, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)
    monkeypatch.setattr(engine, "Path", lambda p="": tmp_path / str(p).lstrip("/")
                        if str(p).startswith("/sys/fs/cgroup") else Path(p))

    assert engine._cgroup_mem_fraction() is None

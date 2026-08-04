"""
Root-fix B1: boot-time reconcile of crash-recovery state.

Covers the two design bugs the adversarial review caught in the original plan:
  Bug #1 — records must be loaded INTO _monitors before flipping, or
           _monitor_update(only_existing=True) silently no-ops.
  Bug #2 — restored pending wakes must get an EXPLICIT _completion_wake_fire spawn:
           pre-populating _completion_wake_pending makes _schedule_completion_wake
           take its "window already open" branch, which never spawns the fire task.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


@pytest.fixture
def boot_env(tmp_path, monkeypatch):
    """Isolated module state + a crash-state file path under tmp."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(_webapp, "_CRASH_STATE_FILE", data / "crash-recovery-state.json")
    monkeypatch.setattr(_webapp, "_monitors", {})
    monkeypatch.setattr(_webapp, "_monitors_dismissed", {})
    monkeypatch.setattr(_webapp, "_completion_wake_pending", {})
    monkeypatch.setattr(_webapp, "_completion_wake_deferred_since", {})
    monkeypatch.setattr(_webapp, "_last_turn_options", {})
    monkeypatch.setattr(_webapp, "_crash_state_dirty", False)
    monkeypatch.setattr(_webapp, "_AUTO_CONTINUE_ON", True)

    ctx = {"DATA": data, "running": {}}
    monkeypatch.setattr(_webapp, "_WEBAPP_CTX", ctx)
    # No real projects — transcript resolution is exercised via monkeypatches per-test.
    monkeypatch.setattr(_webapp, "_collect_projects", lambda _ctx: [])

    fired: list = []

    async def _fake_fire(_ctx, session_key):
        fired.append(session_key)

    monkeypatch.setattr(_webapp, "_completion_wake_fire", _fake_fire)

    spawned: list = []

    def _fake_spawn(coro):
        spawned.append(coro)
        # Run the (fake) coroutine to completion synchronously via the loop
        return asyncio.get_event_loop().create_task(coro)

    monkeypatch.setattr(_webapp, "_spawn_bg", _fake_spawn)

    def _write_state(state: dict):
        (data / "crash-recovery-state.json").write_text(json.dumps(state))

    return ctx, fired, _write_state


async def test_orphaned_running_monitor_flips_failed_and_wakes(boot_env):
    """Bug #1 regression: a 'running' record from the previous life must be loaded into
    _monitors and flipped to failed — and the flip must schedule a completion wake."""
    ctx, fired, write_state = boot_env
    write_state({"monitors": {"k1": {
        "a1": {"id": "a1", "kind": "agent", "status": "running", "label": "worker",
               "started": 1.0},
    }}})

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)  # let spawned fire tasks run

    rec = _webapp._monitors["k1"]["a1"]
    assert rec["status"] == "failed"
    assert "service restarted" in rec["tail"]
    assert rec.get("crash_recovery") is True
    # The running→terminal transition scheduled the wake (window opened + fire spawned)
    assert fired == ["k1"]


async def test_terminal_records_do_not_wake(boot_env):
    ctx, fired, write_state = boot_env
    write_state({"monitors": {"k1": {
        "a1": {"id": "a1", "kind": "agent", "status": "done", "label": "worker"},
    }}})

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)

    assert _webapp._monitors["k1"]["a1"]["status"] == "done"
    assert fired == []


async def test_restored_pending_wake_gets_explicit_fire(boot_env):
    """Bug #2 regression: a session whose wake was pending at crash time must get an
    explicit fire spawn — the natural trigger sees 'window already open' and never spawns."""
    ctx, fired, write_state = boot_env
    write_state({
        "monitors": {},
        "wake_pending": {"k2": [{"id": "a9", "status": "done", "label": "finished-child"}]},
        "last_turn_options": {"k2": {"effort": "high", "ultracode": True}},
    })

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)

    assert _webapp._completion_wake_pending["k2"][0]["id"] == "a9"
    assert _webapp._last_turn_options["k2"]["effort"] == "high"
    assert fired == ["k2"]


async def test_flip_plus_restored_pending_fires_exactly_once(boot_env):
    """A session with BOTH a restored pending wake and a boot flip must not double-fire:
    the flip rides along in the restored window; the explicit spawn is the only fire."""
    ctx, fired, write_state = boot_env
    write_state({
        "monitors": {"k3": {
            "a1": {"id": "a1", "kind": "agent", "status": "running", "label": "w",
                   "started": 1.0},
        }},
        "wake_pending": {"k3": [{"id": "a0", "status": "done", "label": "earlier-child"}]},
    })

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)

    assert _webapp._monitors["k3"]["a1"]["status"] == "failed"
    # Flip appended into the restored window rather than opening a second one
    ids = [r["id"] for r in _webapp._completion_wake_pending["k3"]]
    assert "a0" in ids and "a1" in ids
    assert fired == ["k3"]


async def test_agent_with_transcript_uses_reconcile_path(boot_env, tmp_path, monkeypatch):
    """When the agent's transcript exists on disk, the boot reconcile must go through
    _reconcile_agent_monitor_from_parent (real terminal status) instead of blind-failing —
    and still mark the record as a crash-recovery verdict."""
    ctx, fired, write_state = boot_env
    sk = "k4"
    write_state({"monitors": {sk: {
        "a1": {"id": "a1", "kind": "agent", "status": "running", "label": "w", "started": 1.0},
    }}})

    monkeypatch.setattr(_webapp, "_collect_projects",
                        lambda _ctx: [{"session_key": sk, "cwd": str(tmp_path)}])
    sdk_dir = tmp_path / "sdk"
    (sdk_dir / "sid1" / "subagents").mkdir(parents=True)
    (sdk_dir / "sid1" / "subagents" / "agent-a1.jsonl").write_text("{}\n")
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda _cwd: sdk_dir)

    def _fake_reconcile(session_key, apath):
        # Simulates the transcript scan finding a real completion notification.
        _webapp._monitor_update(session_key, {"id": "a1", "status": "done"}, only_existing=True)

    monkeypatch.setattr(_webapp, "_reconcile_agent_monitor_from_parent", _fake_reconcile)

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)

    rec = _webapp._monitors[sk]["a1"]
    assert rec["status"] == "done"           # real status, not a blind "failed"
    assert rec.get("crash_recovery") is True  # but still flagged as post-crash verdict
    assert fired == [sk]


async def test_no_state_file_is_noop(boot_env):
    ctx, fired, _ = boot_env
    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)
    assert _webapp._monitors == {}
    assert fired == []

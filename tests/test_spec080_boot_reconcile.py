"""
spec-080: a plan awaiting approval cannot survive a restart — the boot reconcile must flip
the sidecar to 'orphaned', clear the pointer, publish plan_decided, and notify the operator.
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
    data = tmp_path / "data"
    (data / "plans").mkdir(parents=True)
    monkeypatch.setattr(_webapp, "_CRASH_STATE_FILE", data / "crash-recovery-state.json")
    monkeypatch.setattr(_webapp, "_PLANS_DIR", data / "plans")
    monkeypatch.setattr(_webapp, "_plan_records", {})
    monkeypatch.setattr(_webapp, "_pending_plan_futures", {})
    monkeypatch.setattr(_webapp, "_plan_pending_by_session", {})
    monkeypatch.setattr(_webapp, "_monitors", {})
    monkeypatch.setattr(_webapp, "_completion_wake_pending", {})
    monkeypatch.setattr(_webapp, "_last_turn_options", {})
    monkeypatch.setattr(_webapp, "_crash_state_dirty", False)
    monkeypatch.setattr(_webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(_webapp, "_collect_projects", lambda _ctx: [])

    ctx = {"DATA": data, "running": {}}
    monkeypatch.setattr(_webapp, "_WEBAPP_CTX", ctx)

    notes: list = []

    async def _fake_notify(_ctx, text):
        notes.append(text)

    monkeypatch.setattr(_webapp, "_notify_operator", _fake_notify)

    events: list = []
    monkeypatch.setattr(_webapp, "_bus_publish",
                        lambda sk, ev, persist=True: events.append((sk, ev)))

    spawned: list = []
    monkeypatch.setattr(_webapp, "_spawn_bg",
                        lambda coro: spawned.append(asyncio.get_event_loop().create_task(coro)))
    return ctx, notes, events, data


async def test_awaiting_plan_becomes_orphaned(boot_env):
    ctx, notes, events, data = boot_env
    sidecar = {"id": "aabbccdd", "session_key": "k1", "chat_id": "c1",
               "created_at": 1.0, "plan_text": "## p", "plan_file_path": None,
               "status": "awaiting_approval", "decided_at": None, "feedback": None}
    (data / "plans" / "aabbccdd.json").write_text(json.dumps(sidecar))
    (data / "crash-recovery-state.json").write_text(json.dumps({
        "monitors": {}, "wake_pending": {}, "last_turn_options": {},
        "plan_pending": {"k1": "aabbccdd"},
    }))

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)

    rec = json.loads((data / "plans" / "aabbccdd.json").read_text())
    assert rec["status"] == "orphaned"
    assert any("lost in a service restart" in n for n in notes)
    assert any(ev.get("kind") == "plan_decided" and ev.get("status") == "orphaned"
               for _sk, ev in events)
    # NOT restored as pending — the Future died with the old process
    assert _webapp._plan_pending_by_session == {}


async def test_already_decided_plan_untouched(boot_env):
    ctx, notes, events, data = boot_env
    sidecar = {"id": "aabbccdd", "session_key": "k1", "chat_id": None,
               "created_at": 1.0, "plan_text": "p", "plan_file_path": None,
               "status": "approved", "decided_at": 2.0, "feedback": None}
    (data / "plans" / "aabbccdd.json").write_text(json.dumps(sidecar))
    (data / "crash-recovery-state.json").write_text(json.dumps({
        "monitors": {}, "wake_pending": {}, "last_turn_options": {},
        "plan_pending": {"k1": "aabbccdd"},
    }))

    _webapp._monitors_reconcile_on_boot(ctx)
    await asyncio.sleep(0)

    assert json.loads((data / "plans" / "aabbccdd.json").read_text())["status"] == "approved"
    assert notes == []

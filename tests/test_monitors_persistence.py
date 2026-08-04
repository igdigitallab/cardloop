"""
Root-fix B1: crash-recovery state persistence (monitors + pending wakes).

The monitor registry and wake bookkeeping used to live in RAM only — an OOM kill erased
them and no wake ever fired after restart. These tests cover the snapshot save/load
round-trip, atomicity hygiene, and the dirty-flag hook on _monitor_update.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


@pytest.fixture
def crash_state(tmp_path, monkeypatch):
    """Point the crash-state file at tmp and isolate the module dicts."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(_webapp, "_CRASH_STATE_FILE", data / "crash-recovery-state.json")
    monkeypatch.setattr(_webapp, "_monitors", {})
    monkeypatch.setattr(_webapp, "_completion_wake_pending", {})
    monkeypatch.setattr(_webapp, "_last_turn_options", {})
    monkeypatch.setattr(_webapp, "_crash_state_dirty", False)
    return data


def test_round_trip(crash_state):
    _webapp._monitors["k1"] = {
        "a1": {"id": "a1", "kind": "agent", "status": "running", "label": "w", "started": 1.0},
    }
    _webapp._completion_wake_pending["k1"] = [{"id": "a2", "status": "done", "label": "x"}]
    _webapp._last_turn_options["k1"] = {"effort": "high", "ultracode": False}

    _webapp._save_crash_state()
    loaded = _webapp._load_crash_state()

    assert loaded["monitors"]["k1"]["a1"]["status"] == "running"
    assert loaded["wake_pending"]["k1"][0]["id"] == "a2"
    assert loaded["last_turn_options"]["k1"]["effort"] == "high"


def test_save_is_atomic_no_tmp_orphan(crash_state):
    _webapp._monitors["k1"] = {"a1": {"id": "a1", "status": "running"}}
    _webapp._save_crash_state()
    files = sorted(p.name for p in crash_state.iterdir())
    assert "crash-recovery-state.json" in files
    assert not any(n.endswith(".tmp") for n in files)
    # File is always valid JSON
    json.loads((crash_state / "crash-recovery-state.json").read_text())


def test_load_missing_or_corrupt_is_empty(crash_state):
    assert _webapp._load_crash_state() == {}
    (crash_state / "crash-recovery-state.json").write_text("{not json")
    assert _webapp._load_crash_state() == {}


def test_monitor_update_marks_dirty(crash_state):
    assert _webapp._crash_state_dirty is False
    _webapp._monitor_update("k1", {"id": "m1", "kind": "agent", "label": "w", "status": "running"})
    assert _webapp._crash_state_dirty is True
    assert _webapp._monitors["k1"]["m1"]["status"] == "running"


def test_monitor_update_stays_cheap_and_sync(crash_state):
    """Persistence must not block the hot drain path: _monitor_update itself never writes
    the file — only the flush loop does."""
    _webapp._monitor_update("k1", {"id": "m1", "kind": "agent", "label": "w", "status": "running"})
    assert not (_webapp._CRASH_STATE_FILE).exists()


def test_save_no_file_configured_is_noop(monkeypatch):
    monkeypatch.setattr(_webapp, "_CRASH_STATE_FILE", None)
    _webapp._save_crash_state()  # must not raise
    assert _webapp._load_crash_state() == {}

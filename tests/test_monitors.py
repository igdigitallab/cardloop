"""
Tests for background-task monitors (card b6f5cc).

Covers:
- engine._monitor_delta: detects background Bash shells, Monitor/Workflow tasks, polls, stops;
  ignores irrelevant tools; never raises on weird input.
- webapp._monitor_update: merges partial deltas, stamps timestamps, keeps running vs terminal,
  fans out a {kind:"monitor"} bus event; _monitors_clear drops a session.
- The PostToolUse hook routes a background-Bash result into the registry via the callback.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine as _engine
import webapp as _webapp
from engine import _monitor_delta, _make_post_tool_use_hook


# ─────────────────────────── _monitor_delta ───────────────────────────────────

def test_delta_background_bash_start():
    d = _monitor_delta("Bash", {"command": "npm run dev", "run_in_background": True},
                       {"stdout": "on :3000", "backgroundTaskId": "b1"}, None)
    assert d == {"id": "b1", "kind": "bash", "status": "running",
                 "label": "npm run dev", "tail": "on :3000", "agent": None}


def test_delta_foreground_bash_is_ignored():
    assert _monitor_delta("Bash", {"command": "ls", "run_in_background": False},
                          {"stdout": "a b c"}, None) is None
    # run_in_background True but no backgroundTaskId in response → nothing to track
    assert _monitor_delta("Bash", {"command": "x", "run_in_background": True},
                          {"stdout": "x"}, None) is None


def test_delta_monitor_and_workflow_tasks():
    m = _monitor_delta("Monitor", {"description": "watch CI"},
                       {"taskId": "t9", "persistent": True}, "Explore")
    assert m["id"] == "t9" and m["kind"] == "monitor" and m["persistent"] is True
    assert m["agent"] == "Explore" and m["label"] == "watch CI"

    w = _monitor_delta("Workflow", {"name": "review"}, {"taskId": "wf1", "workflowName": "review-changes"}, None)
    assert w["id"] == "wf1" and w["kind"] == "workflow" and w["label"] == "review-changes"


def test_delta_poll_updates_tail_only():
    # Still running → backgroundTaskId present in the response, status untouched.
    d = _monitor_delta("BashOutput", {"bash_id": "b1"},
                       {"stdout": "GET / 200", "backgroundTaskId": "b1"}, None)
    assert d == {"id": "b1", "tail": "GET / 200"}


def test_delta_poll_marks_done_when_finished():
    # Finished → response no longer carries backgroundTaskId → mark done.
    d = _monitor_delta("BashOutput", {"bash_id": "b1"}, {"stdout": "bye", "interrupted": False}, None)
    assert d == {"id": "b1", "tail": "bye", "status": "done"}


def test_monitor_tail_no_repr_on_empty():
    from engine import _monitor_tail
    # Empty bg-bash start must yield "" — NOT the dict repr (the bug we fixed live).
    assert _monitor_tail({"stdout": "", "stderr": "", "interrupted": False,
                          "backgroundTaskId": "b1"}) == ""
    # Multi-line output is preserved (rendered in <pre>).
    assert _monitor_tail({"stdout": "a\nb\nc"}) == "a\nb\nc"


def test_delta_stop_paths():
    assert _monitor_delta("KillShell", {"shell_id": "b1"}, {}, None) == {"id": "b1", "status": "stopped"}
    assert _monitor_delta("TaskStop", {"task_id": "t9"}, {}, None) == {"id": "t9", "status": "stopped"}


def test_delta_irrelevant_tool_and_bad_input():
    assert _monitor_delta("Read", {"file_path": "x"}, "contents", None) is None
    # tool_input not a dict, tool_response None — must not raise
    assert _monitor_delta("Bash", None, None, None) is None


# ─────────────────────────── webapp registry ──────────────────────────────────

def test_monitor_update_merges_and_clears():
    sk = "test:monitors"
    _webapp._monitors_clear(sk)
    try:
        _webapp._monitor_update(sk, _monitor_delta(
            "Bash", {"command": "tail -f log", "run_in_background": True},
            {"stdout": "line1", "backgroundTaskId": "b1"}, None))
        rec = _webapp._monitors[sk]["b1"]
        assert rec["status"] == "running" and rec["label"] == "tail -f log"
        started = rec["started"]

        # partial tail update preserves label + started, refreshes tail
        _webapp._monitor_update(sk, {"id": "b1", "tail": "line2"})
        rec = _webapp._monitors[sk]["b1"]
        assert rec["tail"] == "line2" and rec["label"] == "tail -f log" and rec["started"] == started

        # stop
        _webapp._monitor_update(sk, {"id": "b1", "status": "stopped"})
        assert _webapp._monitors[sk]["b1"]["status"] == "stopped"

        _webapp._monitors_clear(sk)
        assert sk not in _webapp._monitors
    finally:
        _webapp._monitors_clear(sk)


def test_monitor_update_only_existing_guard():
    """A completion signal for an unknown id (e.g. a sub-agent task) must NOT create a monitor."""
    sk = "test:monitors-guard"
    _webapp._monitors_clear(sk)
    try:
        _webapp._monitor_update(sk, {"id": "subagent-x", "status": "done"}, only_existing=True)
        assert sk not in _webapp._monitors or "subagent-x" not in _webapp._monitors.get(sk, {})
        # But an existing monitor IS flipped to done.
        _webapp._monitor_update(sk, {"id": "m1", "kind": "bash", "label": "x", "status": "running"})
        _webapp._monitor_update(sk, {"id": "m1", "status": "done"}, only_existing=True)
        assert _webapp._monitors[sk]["m1"]["status"] == "done"
    finally:
        _webapp._monitors_clear(sk)


def test_monitor_dismiss_removes_and_broadcasts():
    sk = "test:monitors-dismiss"
    _webapp._monitors_clear(sk)
    q = _webapp._bus_subscribe(sk)
    try:
        _webapp._monitor_update(sk, {"id": "d1", "kind": "bash", "label": "x", "status": "running"})
        q.get_nowait()  # drain the create event
        assert _webapp._monitor_dismiss(sk, "d1") is True
        evt = q.get_nowait()
        assert evt["kind"] == "monitor" and evt["monitor"] == {"id": "d1", "removed": True}
        assert "d1" not in _webapp._monitors.get(sk, {})
        # dismissing an unknown id is a no-op returning False
        assert _webapp._monitor_dismiss(sk, "nope") is False
    finally:
        _webapp._bus_unsubscribe(sk, q)
        _webapp._monitors_clear(sk)


def test_monitor_update_publishes_bus_event():
    sk = "test:monitors-bus"
    _webapp._monitors_clear(sk)
    q = _webapp._bus_subscribe(sk)
    try:
        _webapp._monitor_update(sk, {"id": "x1", "kind": "monitor", "label": "L", "status": "running"})
        evt = q.get_nowait()
        assert evt["kind"] == "monitor" and evt["monitor"]["id"] == "x1"
    finally:
        _webapp._bus_unsubscribe(sk, q)
        _webapp._monitors_clear(sk)


def test_hook_routes_background_bash_into_registry():
    sk = "test:hook-monitor"
    _webapp._monitors_clear(sk)
    _engine._register_webapp_callbacks(
        _webapp._timeline_append, _webapp._bus_publish, _webapp._monitor_update)
    hook = _make_post_tool_use_hook("proj", sk)
    hook_input = {
        "tool_name": "Bash",
        "tool_input": {"command": "npm run dev", "run_in_background": True},
        "tool_response": {"stdout": "ready", "backgroundTaskId": "bz"},
        "agent_type": None,
    }
    try:
        asyncio.run(hook(hook_input, "tu-1", None))
        assert _webapp._monitors[sk]["bz"]["status"] == "running"
        assert _webapp._monitors[sk]["bz"]["label"] == "npm run dev"
    finally:
        _webapp._monitors_clear(sk)


def test_monitor_dismiss_suppresses_readd():
    """A dismissed monitor must STAY gone even if its still-running bg-shell keeps emitting
    deltas (the 'monitor won't turn off' bug). Suppression lifts on session clear."""
    sk = "test:monitors-suppress"
    _webapp._monitors_clear(sk)
    try:
        _webapp._monitor_update(sk, {"id": "s1", "kind": "bash", "label": "x", "status": "running"})
        assert "s1" in _webapp._monitors[sk]
        assert _webapp._monitor_dismiss(sk, "s1") is True
        assert "s1" not in _webapp._monitors.get(sk, {})
        # a late delta for the SAME id (still-running bg-bash) must NOT resurrect the row
        _webapp._monitor_update(sk, {"id": "s1", "tail": "still going", "status": "running"})
        assert "s1" not in _webapp._monitors.get(sk, {}), "dismissed monitor must stay gone"
        # clearing the session lifts suppression so a fresh run with the same id can appear
        _webapp._monitors_clear(sk)
        _webapp._monitor_update(sk, {"id": "s1", "kind": "bash", "label": "x", "status": "running"})
        assert "s1" in _webapp._monitors[sk], "after clear, a new monitor with the same id is allowed"
    finally:
        _webapp._monitors_clear(sk)

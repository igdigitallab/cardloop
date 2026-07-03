"""
spec-069 Phase 3 (RC#3): a Workflow/Monitor task's monitor is registered under the tool's taskId,
but its completion TaskNotificationMessage carries a DIFFERENT internal task_id — so the flip never
matched and the monitor stuck "running" forever (verified live: id wpzrw3kpz vs notification
bubqfm5aq). Fix: carry tool_use_id (the stable shared key) and flip by it when the id doesn't match.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp


@pytest.fixture(autouse=True)
def _clean():
    webapp._monitors.clear()
    yield
    webapp._monitors.clear()


# ─────────────────────────── engine._monitor_delta carries tool_use_id ───────────────────────────

def test_workflow_delta_carries_tool_use_id():
    d = engine._monitor_delta("Workflow", {}, {"taskId": "w1", "workflowName": "wf"}, "orch", "toolu_A")
    assert d["id"] == "w1" and d["kind"] == "workflow" and d["tool_use_id"] == "toolu_A"


def test_monitor_tool_delta_carries_tool_use_id():
    d = engine._monitor_delta("Monitor", {"description": "watch"}, {"taskId": "m1"}, "orch", "toolu_B")
    assert d["id"] == "m1" and d["kind"] == "monitor" and d["tool_use_id"] == "toolu_B"


# ─────────────────────────── webapp._monitor_update flips by tool_use_id ─────────────────────────

def test_flips_by_tool_use_id_when_task_id_differs():
    # Register a workflow monitor under the TOOL's taskId, carrying its tool_use_id.
    webapp._monitor_update("s", {"id": "wpzrw3kpz", "kind": "workflow", "label": "demo",
                                 "tool_use_id": "toolu_X", "status": "running"})
    assert webapp._monitors["s"]["wpzrw3kpz"]["status"] == "running"
    # Completion notification: DIFFERENT id, SAME tool_use_id → must flip the registered monitor.
    webapp._monitor_update("s", {"id": "bubqfm5aq", "tool_use_id": "toolu_X", "status": "done"},
                           only_existing=True)
    assert webapp._monitors["s"]["wpzrw3kpz"]["status"] == "done", "monitor must flip via tool_use_id"
    assert "bubqfm5aq" not in webapp._monitors["s"], "notification id must not spawn a phantom entry"


def test_by_id_flip_still_works():
    # Regression: a bash monitor whose id DOES match flips normally (no tool_use_id involved).
    webapp._monitor_update("s", {"id": "bash1", "kind": "bash", "status": "running"})
    webapp._monitor_update("s", {"id": "bash1", "status": "done"}, only_existing=True)
    assert webapp._monitors["s"]["bash1"]["status"] == "done"


def test_only_existing_no_match_is_noop():
    # Unknown id + unknown tool_use_id under only_existing → no phantom monitor.
    webapp._monitor_update("s", {"id": "ghost", "tool_use_id": "nope", "status": "done"}, only_existing=True)
    assert webapp._monitors.get("s", {}) == {}

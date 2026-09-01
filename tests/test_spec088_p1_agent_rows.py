"""spec-088 P1 — one live monitor row per background sub-agent, fed from the SDK stream.

A Workflow spawns its agents itself: no Agent tool call → no PostToolUse row, and the transcript
sweeper's glob never reaches subagents/workflows/*/. The only per-agent signal is the SDK's
TaskStarted / TaskProgress system messages, which used to feed a client-only lane (wiped on
run_end) in-turn and were dropped outright between turns. Now they feed the durable monitors
registry in both paths, keyed by task_id so the terminal TaskNotification flips the row.
"""
import asyncio
import time

import pytest

import engine
import webapp
from claude_agent_sdk.types import (
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
)


def _started(task_id, task_type="local_agent", desc="read webapp.py", tool_use_id="tu-1"):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=task_id,
                              description=desc, uuid="u", session_id="sid",
                              tool_use_id=tool_use_id, task_type=task_type)


def _progress(task_id, tool="Bash"):
    return TaskProgressMessage(subtype="task_progress", data={}, task_id=task_id,
                               description="", usage={}, uuid="u", session_id="sid",
                               last_tool_name=tool)


def _notification(task_id, status="completed"):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=task_id,
                                   status=status, output_file="", summary="done",
                                   uuid="u", session_id="sid")


# ─────────────────────────── pure helper ───────────────────────────

def test_started_local_agent_creates_agent_row():
    d = engine._task_lifecycle_monitor_delta(_started("b1"))
    assert d == {"id": "b1", "kind": "agent", "status": "running",
                 "label": "read webapp.py", "stream": True}


def test_started_workflow_task_is_ignored():
    # The Workflow's own task already has a row from PostToolUse (keyed by the tool's taskId).
    assert engine._task_lifecycle_monitor_delta(_started("w1", task_type="local_workflow")) is None
    assert engine._task_lifecycle_monitor_delta(_started("s1", task_type="local_bash")) is None


def test_progress_is_a_tail_only_delta():
    assert engine._task_lifecycle_monitor_delta(_progress("b1", "WebSearch")) == {
        "id": "b1", "tail": "↳ WebSearch"}
    assert engine._task_lifecycle_monitor_delta(_progress("b1", None)) is None


# ─────────────────────────── between turns (drain) ───────────────────────────

class _FakeDrainClient:
    def __init__(self, messages):
        self._messages = messages
        self._blocker = asyncio.Event()

    async def receive_messages(self):
        for m in self._messages:
            yield m
        await self._blocker.wait()


@pytest.mark.asyncio
async def test_drain_feeds_agent_rows_and_flips_them(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_monitor_update_cb",
                        lambda sk, d, only_existing=False: calls.append((d, only_existing)))
    monkeypatch.setattr(engine, "_bg_run_cb", lambda *a, **k: None)
    msgs = [_started("b1"), _progress("b1", "Read"), _notification("b1")]
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    assert calls[0] == ({"id": "b1", "kind": "agent", "status": "running",
                         "label": "read webapp.py", "stream": True}, False)
    assert calls[1] == ({"id": "b1", "tail": "↳ Read"}, True)
    assert calls[2][0]["id"] == "b1" and calls[2][0]["status"] == "done" and calls[2][1] is True


# ─────────────────────────── registry behaviour ───────────────────────────

@pytest.fixture
def quiet_registry(monkeypatch):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_crash_state_mark_dirty", lambda: None)
    monkeypatch.setattr(webapp, "_schedule_completion_wake", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_monitors", {})
    return webapp._monitors


def test_stream_row_lifecycle_in_registry(quiet_registry):
    sk = "s"
    webapp._monitor_update(sk, engine._task_lifecycle_monitor_delta(_started("b1")))
    webapp._monitor_update(sk, engine._task_lifecycle_monitor_delta(_progress("b1", "Grep")),
                           only_existing=True)
    rec = quiet_registry[sk]["b1"]
    assert rec["kind"] == "agent" and rec["status"] == "running"
    assert rec["tail"] == "↳ Grep" and rec["stream"] is True
    webapp._monitor_update(sk, engine._notification_monitor_delta(_notification("b1")),
                           only_existing=True)
    assert quiet_registry[sk]["b1"]["status"] == "done"


def test_progress_never_creates_a_phantom_row(quiet_registry):
    webapp._monitor_update("s", engine._task_lifecycle_monitor_delta(_progress("ghost", "Bash")),
                           only_existing=True)
    assert "ghost" not in quiet_registry.get("s", {})


def test_stream_rows_go_stale_on_update_silence(quiet_registry, monkeypatch):
    """The sweeper must not glob for a transcript these rows do not have (it would flip them
    'failed'); silence on the stream is their staleness signal, on the agent threshold."""
    sk = "s"
    webapp._monitor_update(sk, engine._task_lifecycle_monitor_delta(_started("b1")))
    webapp._monitor_update(sk, engine._task_lifecycle_monitor_delta(_started("b2")))
    now = time.time()
    quiet_registry[sk]["b1"]["ts"] = now - webapp._MONITOR_STALE_SEC - 5   # silent too long
    quiet_registry[sk]["b2"]["ts"] = now - 30                              # fresh
    webapp._sweep_stale_monitors(now)
    assert quiet_registry[sk]["b1"]["status"] == "stopped"
    assert quiet_registry[sk]["b1"]["stale"] is True
    assert quiet_registry[sk]["b2"]["status"] == "running"


def test_workflow_rows_keep_the_long_threshold(quiet_registry):
    sk = "s"
    webapp._monitor_update(sk, {"id": "w1", "kind": "workflow", "status": "running", "label": "wf"})
    now = time.time()
    quiet_registry[sk]["w1"]["ts"] = now - webapp._MONITOR_STALE_SEC - 5  # past the AGENT threshold only
    webapp._sweep_stale_monitors(now)
    assert quiet_registry[sk]["w1"]["status"] == "running"
    quiet_registry[sk]["w1"]["ts"] = now - webapp._MONITOR_STALE_WF_SEC - 5
    webapp._sweep_stale_monitors(now)
    assert quiet_registry[sk]["w1"]["status"] == "stopped"

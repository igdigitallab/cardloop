"""
spec-069 Phase 3 (RC#3): transcript-based monitor reconciliation.

Completed background tasks (Workflow / run_in_background Bash) stay status="running" forever
because the SDK's TaskNotificationMessage carries an internal task_id that never matches the
monitor's registered id (the tool's taskId), and the tool_use_id flip in RC#3 is also inert
for Workflow completions. The real completion signal lives in the session .jsonl transcript as
a <task-notification> XML block whose <task-id> EXACTLY matches the monitor id.

_reconcile_monitors_from_transcript tails the last 64 KB of the transcript, parses every
task-notification block, and calls _monitor_update(..., only_existing=True) for each terminal
task-id found. Placement invariant: called AFTER _maybe_schedule_bg_continuation at both
turn-end sites to preserve the RC#2 re-wake guarantee.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp


# ─────────────────────────── fixtures ───────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state():
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()
    yield
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()


def _make_ctx(data_dir: Path, sessions: dict | None = None, projects: list | None = None) -> dict:
    """Minimal ctx dict sufficient for reconcile and project resolution."""
    return {
        "DATA": data_dir,
        "sessions": sessions or {},
        "topics": {},
        # A simple synchronous resolver: find project by id from a flat list.
        # _find_project_by_id reads ctx["topics"] + ctx["REGISTRY"] via _collect_projects;
        # patching it directly is simpler for unit tests.
        "_projects": projects or [],
    }


def _register_running(session_key: str, mid: str, kind: str = "workflow") -> None:
    """Seed a running monitor into the registry."""
    webapp._monitor_update(session_key, {"id": mid, "kind": kind, "status": "running"})


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _queue_op_line(task_id: str, status: str, tool_use_id: str = "toolu_test") -> str:
    """Build a queue-operation transcript line with a task-notification XML in content."""
    xml = (
        f"<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<status>{status}</status>\n"
        f"</task-notification>"
    )
    return json.dumps({"type": "queue-operation", "operation": "enqueue", "content": xml})


def _attachment_line(task_id: str, status: str, tool_use_id: str = "toolu_test") -> str:
    """Build an attachment transcript line with a task-notification XML in attachment.prompt."""
    xml = (
        f"<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<status>{status}</status>\n"
        f"</task-notification>"
    )
    return json.dumps({"type": "attachment", "attachment": {"type": "queued_command",
                                                              "commandMode": "task-notification",
                                                              "prompt": xml}})


# ─────────────────────────── core status-flip tests ─────────────────────────


def test_reconcile_completed_flips_to_done(tmp_path):
    """A 'completed' task-notification → monitor status becomes 'done'."""
    session_key = "sk:test"
    project_id = "myproj"
    mid = "WF1"
    cwd = str(tmp_path / "myproj")

    # Register a running monitor.
    _register_running(session_key, mid)
    assert webapp._monitors[session_key][mid]["status"] == "running"

    # Write a transcript with both line types carrying the task-notification.
    sid = "sess-abc123"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [
        _queue_op_line(mid, "completed"),
        _attachment_line(mid, "completed"),
    ])

    ctx = _make_ctx(tmp_path, projects=[{"id": project_id, "cwd": cwd}])

    # Patch helpers that read from disk/chats.json.
    with patch.object(webapp, "_find_project_by_id",
                      side_effect=lambda c, pid: {"id": project_id, "cwd": cwd} if pid == project_id else None), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    assert webapp._monitors[session_key][mid]["status"] == "done", \
        "completed task-notification must flip monitor to done"


def test_reconcile_failed_flips_to_failed(tmp_path):
    """A 'failed' task-notification → monitor status becomes 'failed'."""
    session_key = "sk:test2"
    project_id = "proj2"
    mid = "WF2"
    cwd = str(tmp_path / "proj2")

    _register_running(session_key, mid, kind="monitor")

    sid = "sess-xyz"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [_queue_op_line(mid, "failed")])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    assert webapp._monitors[session_key][mid]["status"] == "failed"


def test_reconcile_stopped_flips_to_stopped(tmp_path):
    """A 'stopped' task-notification → monitor status becomes 'stopped'."""
    session_key = "sk:test3"
    project_id = "proj3"
    mid = "WF3"
    cwd = str(tmp_path / "proj3")

    _register_running(session_key, mid)

    sid = "sess-stop"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [_attachment_line(mid, "stopped")])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    assert webapp._monitors[session_key][mid]["status"] == "stopped"


def test_reconcile_both_line_types_idempotent(tmp_path):
    """Processing both queue-operation and attachment lines for same task-id is idempotent."""
    session_key = "sk:idem"
    project_id = "proj_idem"
    mid = "WF_IDEM"
    cwd = str(tmp_path / "proj_idem")

    _register_running(session_key, mid)

    sid = "sess-idem"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    # Both lines carry 'completed' for the same id.
    _write_transcript(jsonl_path, [
        _queue_op_line(mid, "completed"),
        _attachment_line(mid, "completed"),
    ])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    # Should still be exactly one monitor entry, status done.
    assert webapp._monitors[session_key][mid]["status"] == "done"
    assert len(webapp._monitors[session_key]) == 1


# ─────────────────────────── only_existing: no phantom rows ─────────────────


def test_reconcile_unknown_task_id_no_phantom(tmp_path):
    """A task-id in the transcript that is NOT registered as a monitor must not create a row."""
    session_key = "sk:phantom"
    project_id = "proj_phantom"
    cwd = str(tmp_path / "proj_phantom")

    # NO monitors registered.
    assert webapp._monitors.get(session_key) is None

    sid = "sess-phantom"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [_queue_op_line("GHOST_ID", "completed")])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    assert webapp._monitors.get(session_key) is None or \
           "GHOST_ID" not in webapp._monitors.get(session_key, {}), \
        "unknown task-id must NOT create a phantom monitor row"


# ─────────────────────────── dismissed ids stay gone ────────────────────────


def test_reconcile_dismissed_id_not_recreated(tmp_path):
    """A dismissed monitor id in the transcript must NOT be re-created."""
    session_key = "sk:dismissed"
    project_id = "proj_dis"
    mid = "WF_DIS"
    cwd = str(tmp_path / "proj_dis")

    # Register and then dismiss.
    _register_running(session_key, mid)
    webapp._monitor_dismiss(session_key, mid)

    # Confirm it's gone and suppressed.
    assert webapp._monitors.get(session_key, {}).get(mid) is None
    assert mid in webapp._monitors_dismissed.get(session_key, set())

    sid = "sess-dis"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [_queue_op_line(mid, "completed")])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    assert webapp._monitors.get(session_key, {}).get(mid) is None, \
        "dismissed monitor must NOT be re-created by reconcile"


# ─────────────────────────── graceful no-ops ─────────────────────────────────


def test_reconcile_missing_file_is_noop(tmp_path):
    """Missing transcript file → silent return, no exception, no state change."""
    session_key = "sk:missing"
    project_id = "proj_miss"
    mid = "WF_MISS"
    cwd = str(tmp_path / "proj_miss")

    _register_running(session_key, mid)

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value="no-such-session"):
        # Must not raise.
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    # Monitor unchanged — no file means no flip.
    assert webapp._monitors[session_key][mid]["status"] == "running"


def test_reconcile_unknown_project_is_noop(tmp_path):
    """Unknown project_id → silent return, no exception."""
    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id", return_value=None):
        webapp._reconcile_monitors_from_transcript(ctx, "any_key", "nonexistent_id")
    # No crash is the assertion.


def test_reconcile_no_session_id_is_noop(tmp_path):
    """No resolvable session_id → silent return, no exception."""
    cwd = str(tmp_path / "proj_nosid")
    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": "p", "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=None):
        webapp._reconcile_monitors_from_transcript(ctx, "sk", "p")
    # No crash is the assertion.


def test_reconcile_unknown_status_ignored(tmp_path):
    """An unrecognised status value (e.g. 'pending') must not flip or crash."""
    session_key = "sk:unk"
    project_id = "proj_unk"
    mid = "WF_UNK"
    cwd = str(tmp_path / "proj_unk")

    _register_running(session_key, mid)

    sid = "sess-unk"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [_queue_op_line(mid, "pending")])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    # Status must not change — 'pending' is not a terminal value.
    assert webapp._monitors[session_key][mid]["status"] == "running"


# ─────────────────────────── RC#2 non-regression ────────────────────────────
# Verify that reconciling a monitor whose completion is NOT yet in the transcript
# (e.g. it is still running) leaves the monitor in "running" state so that
# _has_running_bg stays True and _maybe_schedule_bg_continuation can re-wake.


def test_reconcile_still_running_preserves_running_for_bg_wake(tmp_path):
    """Reconcile of a monitor whose task-id is NOT in the transcript leaves it running.

    This is the RC#2 non-regression proof: if the bg task is genuinely still running
    and has not yet written its completion to the transcript, reconcile must not flip
    it to any terminal state, so _has_running_bg stays True and the auto-continue
    scheduler can still re-wake the orchestrator.
    """
    session_key = "sk:rc2"
    project_id = "proj_rc2"
    mid = "WF_STILL_RUNNING"
    cwd = str(tmp_path / "proj_rc2")

    _register_running(session_key, mid, kind="workflow")
    assert webapp._has_running_bg(session_key) is True

    # Write a transcript that contains a DIFFERENT task-id (not mid).
    sid = "sess-rc2"
    jsonl_path = webapp._sdk_sessions_dir(cwd) / f"{sid}.jsonl"
    _write_transcript(jsonl_path, [_queue_op_line("OTHER_TASK", "completed")])

    ctx = _make_ctx(tmp_path)
    with patch.object(webapp, "_find_project_by_id",
                      return_value={"id": project_id, "cwd": cwd}), \
         patch.object(webapp, "_active_chat_session_id", return_value=sid):
        webapp._reconcile_monitors_from_transcript(ctx, session_key, project_id)

    # mid must still be running — reconcile must not have flipped it.
    assert webapp._monitors[session_key][mid]["status"] == "running", \
        "reconcile must not flip a still-running monitor that has no completion in transcript"
    # _has_running_bg must stay True so auto-continue can re-wake the orchestrator.
    assert webapp._has_running_bg(session_key) is True, \
        "_has_running_bg must remain True: RC#2 non-regression"

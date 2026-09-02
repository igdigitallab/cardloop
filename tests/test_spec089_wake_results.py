"""
spec-089 §2: the completion wake carries results, not just labels.

Before this, the wake prompt named only "<label> → <status>" for each finished background
task, forcing the orchestrator to spend 3-5 Bash calls grepping journal.jsonl / agent-*.jsonl
just to find out WHAT finished. Now the SDK's own output_file/summary ride the monitor record
from _notification_monitor_delta (engine.py) through _monitor_update into the wake prompt,
along with a bounded tail of the output file and, for a Workflow row, the run's journal
counts — so the model's first tool call after a wake is a targeted Read, or nothing.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp
from claude_agent_sdk.types import TaskNotificationMessage, TaskUpdatedMessage


def _notification(task_id, status="completed", output_file="", summary="", tool_use_id=None):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=task_id,
                                   status=status, output_file=output_file, summary=summary,
                                   uuid="u", session_id="sid", tool_use_id=tool_use_id)


def _updated(task_id, patch):
    return TaskUpdatedMessage(subtype="task_updated", data={}, task_id=task_id, patch=patch)


def _make_journal(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _ctx(running=None):
    return {"running": running or {}, "topics": {}, "REGISTRY": {}}


# ─────────────────────────── engine._notification_monitor_delta ────────────────────────────

def test_delta_from_notification_carries_output_file_and_summary():
    d = engine._notification_monitor_delta(
        _notification("t1", "completed", output_file="/tmp/x.output", summary="did the thing"))
    assert d["output_file"] == "/tmp/x.output"
    assert d["summary"] == "did the thing"


def test_delta_from_notification_omits_empty_output_file_and_summary():
    d = engine._notification_monitor_delta(_notification("t1", "completed"))
    assert "output_file" not in d
    assert "summary" not in d


def test_delta_from_notification_bounds_summary_to_600_chars():
    d = engine._notification_monitor_delta(_notification("t1", "completed", summary="x" * 1000))
    assert len(d["summary"]) == 600


def test_delta_from_task_updated_patch_carries_output_file_and_summary():
    d = engine._notification_monitor_delta(
        _updated("t2", {"status": "killed", "output_file": "/tmp/y.output",
                        "summary": "stopped early"}))
    assert d["output_file"] == "/tmp/y.output"
    assert d["summary"] == "stopped early"


def test_delta_from_task_updated_without_result_fields_omits_them():
    d = engine._notification_monitor_delta(_updated("t3", {"status": "killed"}))
    assert "output_file" not in d
    assert "summary" not in d


def test_delta_never_raises_when_patch_is_none_despite_a_terminal_status():
    # status set on the top-level field (not via patch) so the terminal check still passes,
    # exercising the new output_file/summary lookup against a non-dict patch.
    msg = TaskUpdatedMessage(subtype="task_updated", data={}, task_id="t4", patch=None,
                             status="killed")
    d = engine._notification_monitor_delta(msg)
    assert d is not None and d["status"] == "stopped"
    assert "output_file" not in d and "summary" not in d


# ─────────────────────────── webapp._monitor_update merges the new keys ────────────────────

@pytest.fixture(autouse=True)
def _clean_monitors():
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()
    yield
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()


def test_monitor_update_merges_output_file_and_summary(monkeypatch):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_crash_state_mark_dirty", lambda: None)
    monkeypatch.setattr(webapp, "_schedule_completion_wake", lambda *a, **k: None)
    webapp._monitor_update("s", {"id": "m1", "kind": "agent", "status": "running", "label": "w"})
    webapp._monitor_update("s", {"id": "m1", "status": "done", "output_file": "/tmp/o.txt",
                                  "summary": "finished"}, only_existing=True)
    rec = webapp._monitors["s"]["m1"]
    assert rec["output_file"] == "/tmp/o.txt"
    assert rec["summary"] == "finished"


# ─────────────────────────── webapp._file_tail_lines ───────────────────────────────────────

def test_file_tail_lines_missing_path_returns_empty(tmp_path):
    assert webapp._file_tail_lines(tmp_path / "nope.txt") == []


def test_file_tail_lines_empty_file_returns_empty(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("")
    assert webapp._file_tail_lines(p) == []


def test_file_tail_lines_fewer_than_n(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n")
    assert webapp._file_tail_lines(p) == ["a", "b", "c"]


def test_file_tail_lines_more_than_n_skips_blank_lines(tmp_path):
    p = tmp_path / "f.txt"
    lines = [f"line{i}" for i in range(10)]
    p.write_text("\n\n".join(lines) + "\n\n")  # blank lines sprinkled between + trailing
    assert webapp._file_tail_lines(p, n=5) == lines[-5:]


def test_file_tail_lines_truncates_long_lines(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x" * 1000 + "\n")
    result = webapp._file_tail_lines(p, max_chars=600)
    assert result == ["x" * 600]


def test_file_tail_lines_only_reads_the_tail_of_a_large_file(tmp_path):
    p = tmp_path / "big.txt"
    with open(p, "w") as fh:
        for i in range(20000):
            fh.write(f"line-{i}\n")
    result = webapp._file_tail_lines(p, n=5, max_bytes=65536)
    assert result[-1] == "line-19999"
    assert all("line-0" != ln for ln in result)  # nowhere near the 64 KB tail window


# ─────────────────────────── webapp._workflow_journal_summary ──────────────────────────────

def test_workflow_journal_summary_no_cwd_returns_none():
    assert webapp._workflow_journal_summary(None, {"id": "x"}) is None


def test_workflow_journal_summary_no_journals_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_sdk_sessions_dir", lambda cwd: tmp_path / "nope")
    assert webapp._workflow_journal_summary("/proj", {"id": "x"}) is None


def test_workflow_journal_summary_matches_by_record_id(tmp_path, monkeypatch):
    sdk_dir = tmp_path / "sdk"
    journal = sdk_dir / "sess1" / "subagents" / "workflows" / "wf_abc123" / "journal.jsonl"
    _make_journal(journal, [
        {"type": "started", "agentId": "a1"}, {"type": "started", "agentId": "a2"},
        {"type": "result", "agentId": "a1"}, {"type": "failed", "agentId": "a2"},
    ])
    monkeypatch.setattr(webapp, "_sdk_sessions_dir", lambda cwd: sdk_dir)
    result = webapp._workflow_journal_summary("/proj", {"id": "wf_abc123", "tool_use_id": "tu-x"})
    assert result is not None
    assert str(journal) in result
    assert "started=2" in result and "result=1" in result and "failed=1" in result


def test_workflow_journal_summary_matches_by_tool_use_id(tmp_path, monkeypatch):
    sdk_dir = tmp_path / "sdk"
    journal = sdk_dir / "sess1" / "subagents" / "workflows" / "wf_xyz789" / "journal.jsonl"
    _make_journal(journal, [{"type": "started", "agentId": "a1"}])
    monkeypatch.setattr(webapp, "_sdk_sessions_dir", lambda cwd: sdk_dir)
    result = webapp._workflow_journal_summary(
        "/proj", {"id": "toolTaskId1", "tool_use_id": "xyz789"})
    assert result is not None and str(journal) in result


def test_workflow_journal_summary_falls_back_to_newest_mtime_after_started(tmp_path, monkeypatch):
    sdk_dir = tmp_path / "sdk"
    old_journal = sdk_dir / "sess1" / "subagents" / "workflows" / "wf_old" / "journal.jsonl"
    new_journal = sdk_dir / "sess1" / "subagents" / "workflows" / "wf_new" / "journal.jsonl"
    _make_journal(old_journal, [{"type": "started", "agentId": "a1"}])
    _make_journal(new_journal, [{"type": "started", "agentId": "a1"}, {"type": "result", "agentId": "a1"}])
    started_ts = 1_000_000.0
    os.utime(old_journal, (started_ts - 100, started_ts - 100))  # before the row started
    os.utime(new_journal, (started_ts + 100, started_ts + 100))  # after — the real match
    monkeypatch.setattr(webapp, "_sdk_sessions_dir", lambda cwd: sdk_dir)
    result = webapp._workflow_journal_summary(
        "/proj", {"id": "no-match-here", "tool_use_id": "also-no-match", "started": started_ts})
    assert result is not None and str(new_journal) in result


def test_workflow_journal_summary_no_fallback_match_returns_none(tmp_path, monkeypatch):
    sdk_dir = tmp_path / "sdk"
    journal = sdk_dir / "sess1" / "subagents" / "workflows" / "wf_old" / "journal.jsonl"
    _make_journal(journal, [{"type": "started", "agentId": "a1"}])
    os.utime(journal, (1_000_000.0, 1_000_000.0))
    monkeypatch.setattr(webapp, "_sdk_sessions_dir", lambda cwd: sdk_dir)
    result = webapp._workflow_journal_summary(
        "/proj", {"id": "no-match", "tool_use_id": "no-match-2", "started": 2_000_000.0})
    assert result is None


# ─────────────────────────── wake prompt integration ───────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_wake_state():
    for d in (webapp._CHAT_QUEUE, webapp._bg_continue_count,
              webapp._completion_wake_pending, webapp._last_turn_options,
              webapp._completion_wake_deferred_since):
        d.clear()
    webapp._WEBAPP_CTX = None
    yield
    for d in (webapp._CHAT_QUEUE, webapp._bg_continue_count,
              webapp._completion_wake_pending, webapp._last_turn_options,
              webapp._completion_wake_deferred_since):
        d.clear()
    webapp._WEBAPP_CTX = None


@pytest.mark.asyncio
async def test_wake_prompt_contains_output_path_tail_and_journal_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())

    output_file = tmp_path / "task.output"
    output_file.write_text("line one\nline two\nline three\n")

    sdk_dir = tmp_path / "sdk"
    journal = sdk_dir / "sess1" / "subagents" / "workflows" / "wf_run1" / "journal.jsonl"
    _make_journal(journal, [{"type": "started", "agentId": "a1"}, {"type": "result", "agentId": "a1"}])
    monkeypatch.setattr(webapp, "_sdk_sessions_dir", lambda cwd: sdk_dir)
    monkeypatch.setattr(webapp, "_collect_projects",
                        lambda ctx: [{"id": "p1", "session_key": "s", "cwd": "/proj"}])

    webapp._completion_wake_pending["s"] = [
        {"id": "wf_run1", "kind": "workflow", "status": "done", "label": "research pass",
         "output_file": str(output_file), "summary": "did research"},
    ]
    await webapp._completion_wake_fire(_ctx(), "s")
    q = webapp._CHAT_QUEUE.get("s", [])
    assert len(q) == 1
    text = q[0]["text"]
    assert text.startswith(webapp._BG_CONTINUE_PREFIX)
    assert str(output_file) in text
    assert "did research" in text
    assert "line three" in text            # tail line made it in
    assert "started=1" in text and "result=1" in text  # journal counts made it in


@pytest.mark.asyncio
async def test_wake_prompt_omits_tail_block_when_output_file_missing(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())
    monkeypatch.setattr(webapp, "_collect_projects", lambda ctx: [])

    webapp._completion_wake_pending["s"] = [
        {"id": "m1", "kind": "agent", "status": "done", "label": "no output file recorded"},
    ]
    await webapp._completion_wake_fire(_ctx(), "s")
    text = webapp._CHAT_QUEUE["s"][0]["text"]
    assert text.startswith(webapp._BG_CONTINUE_PREFIX)
    assert "output:" not in text
    assert "last lines:" not in text
    assert "no output file recorded" in text


@pytest.mark.asyncio
async def test_wake_prompt_stays_under_byte_cap_with_eight_long_records(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())
    monkeypatch.setattr(webapp, "_collect_projects", lambda ctx: [])

    recs = []
    for i in range(8):
        out = tmp_path / f"out{i}.txt"
        out.write_text("\n".join(f"result line {i}-{j} " + "x" * 100 for j in range(80)))
        recs.append({
            "id": f"m{i}", "kind": "agent", "status": "done",
            "label": f"agent number {i} doing a very long labeled task " * 2,
            "output_file": str(out), "summary": "y" * 500,
        })
    webapp._completion_wake_pending["s"] = recs
    await webapp._completion_wake_fire(_ctx(), "s")
    text = webapp._CHAT_QUEUE["s"][0]["text"]
    assert text.startswith(webapp._BG_CONTINUE_PREFIX)
    assert len(text.encode("utf-8")) <= webapp._WAKE_PROMPT_BYTE_CAP
    # the cascade must have dropped the (huge) tails to make the cap
    assert "result line" not in text

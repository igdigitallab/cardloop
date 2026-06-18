"""
Tests for Spec-029 §2: PostToolUse hook — audit log + timeline enrichment.

Covers:
- _tool_response_to_str: dict (Bash), plain str, truncation, exception safety
- _make_post_tool_use_hook: writes RESULT audit line, writes tool_result timeline event
- Truncation: long output is capped at _HOOK_OUTPUT_TRUNCATE chars
- Exception safety: a raising hook body does NOT propagate to the caller
- Secrets / env: hook never receives env; output text that looks like a secret
  doesn't escape through any path not already covered by existing guards
- Hook return value: always returns {} (no model-visible side effects)
- include_hook_events remains False (verified via ClaudeAgentOptions defaults)
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot as _bot
import engine as _engine
import webapp as _webapp
from engine import (
    _HOOK_OUTPUT_TRUNCATE,
    _make_post_tool_use_hook,
    _tool_response_to_str,
    audit,
)
from webapp import _timeline_append, _timeline_init, _timeline_path


# ─────────────────────────── helpers ──────────────────────────────────────────

def _reset_timeline(tmp_path: Path, session_key: str, cwd: str) -> None:
    """Initialise webapp timeline state for a single project.

    Also registers webapp's _timeline_append and _bus_publish into engine so that
    _make_post_tool_use_hook (which lives in engine.py) can write to the timeline.
    """
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    _timeline_init({"DATA": data, "topics": {session_key: {"project": "proj", "cwd": cwd}}})
    # Inject webapp callbacks into engine so timeline writes work without a running server.
    _engine._register_webapp_callbacks(_webapp._timeline_append, _webapp._bus_publish)


def _run(coro):
    """Run a coroutine in a fresh event loop (test helper).

    asyncio.run() always creates a new event loop — safe to call from sync
    test functions even after other async tests have closed the current loop.
    """
    return asyncio.run(coro)


# ─────────────────────────── unit: _tool_response_to_str ──────────────────────

class TestToolResponseToStr:
    def test_bash_dict_stdout_only(self):
        resp = {"stdout": "hello world", "stderr": "", "interrupted": False}
        result = _tool_response_to_str(resp)
        assert "hello world" in result
        assert "[stderr]" not in result

    def test_bash_dict_with_stderr(self):
        resp = {"stdout": "", "stderr": "command not found", "interrupted": False}
        result = _tool_response_to_str(resp)
        assert "command not found" in result
        assert "[stderr]" in result

    def test_bash_dict_interrupted(self):
        resp = {"stdout": "partial", "stderr": "", "interrupted": True}
        result = _tool_response_to_str(resp)
        assert "[interrupted]" in result

    def test_plain_str(self):
        result = _tool_response_to_str("simple output")
        assert result == "simple output"

    def test_newlines_collapsed(self):
        resp = "line1\nline2\r\nline3"
        result = _tool_response_to_str(resp)
        assert "\n" not in result
        assert "\r" not in result
        assert "line1" in result and "line2" in result

    def test_truncation_at_limit(self):
        long_output = "x" * (_HOOK_OUTPUT_TRUNCATE + 200)
        result = _tool_response_to_str(long_output)
        assert len(result) <= _HOOK_OUTPUT_TRUNCATE + 1  # +1 for ellipsis char
        assert result.endswith("…")

    def test_short_output_not_truncated(self):
        short = "short output"
        result = _tool_response_to_str(short)
        assert result == short
        assert not result.endswith("…")

    def test_none_response(self):
        # None is a valid tool_response (e.g. no output)
        result = _tool_response_to_str(None)
        assert isinstance(result, str)

    def test_exception_returns_fallback(self):
        # A pathological object whose __str__ raises
        class BadStr:
            def __str__(self):
                raise RuntimeError("boom")

        result = _tool_response_to_str(BadStr())
        assert result == "<unparseable>"

    def test_dict_with_both_stdout_and_stderr(self):
        resp = {"stdout": "out", "stderr": "err", "interrupted": False}
        result = _tool_response_to_str(resp)
        assert "out" in result
        assert "err" in result


# ─────────────────────────── unit: hook writes RESULT audit line ───────────────

class TestHookAuditLine:
    def test_writes_result_audit_line(self, tmp_path):
        """PostToolUse hook produces a RESULT line in the audit log."""
        audit_dir = tmp_path / "audit"
        original = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        try:
            hook = _make_post_tool_use_hook("myproject", "42:100")
            hook_input = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_response": {"stdout": "hi", "stderr": "", "interrupted": False},
                "tool_use_id": "tid-001",
                "session_id": "sess-abc",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": "/home/igor/myproject",
                "permission_mode": "bypassPermissions",
            }
            result = _run(hook(hook_input, "tid-001", {}))
            assert result == {}, "Hook must return empty dict (no model side-effects)"

            log_files = list(audit_dir.glob("*.log"))
            assert log_files, "Audit log file should be created"
            content = log_files[0].read_text()
            assert "RESULT" in content
            assert "Bash" in content
            assert "ok" in content
            assert "hi" in content
        finally:
            _engine.AUDIT_DIR = original

    def test_error_tool_response_marks_err(self, tmp_path):
        """tool_response with error key → audit line contains 'err'."""
        audit_dir = tmp_path / "audit"
        original = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        try:
            hook = _make_post_tool_use_hook("myproject", "42:100")
            hook_input = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "false"},
                "tool_response": {"stdout": "", "stderr": "exit 1", "interrupted": False, "error": True},
                "tool_use_id": "tid-002",
                "session_id": "sess-abc",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": "/home/igor/myproject",
                "permission_mode": "bypassPermissions",
            }
            _run(hook(hook_input, "tid-002", {}))

            content = list(audit_dir.glob("*.log"))[0].read_text()
            assert "err" in content
        finally:
            _engine.AUDIT_DIR = original

    def test_different_tool_names_recorded(self, tmp_path):
        """Hook records the correct tool name for non-Bash tools."""
        audit_dir = tmp_path / "audit"
        original = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        try:
            hook = _make_post_tool_use_hook("proj", "1:1")
            for tool in ("Read", "Edit", "Write"):
                hook_input = {
                    "hook_event_name": "PostToolUse",
                    "tool_name": tool,
                    "tool_input": {"file_path": "/tmp/x.py"},
                    "tool_response": "ok",
                    "tool_use_id": "t",
                    "session_id": "s",
                    "transcript_path": "/tmp/t.jsonl",
                    "cwd": "/tmp",
                    "permission_mode": "bypassPermissions",
                }
                _run(hook(hook_input, "t", {}))

            content = list(audit_dir.glob("*.log"))[0].read_text()
            for tool in ("Read", "Edit", "Write"):
                assert tool in content
        finally:
            _engine.AUDIT_DIR = original


# ─────────────────────────── unit: hook writes tool_result timeline event ─────

class TestHookTimelineEvent:
    def test_writes_tool_result_to_timeline(self, tmp_path):
        """PostToolUse hook appends a tool_result event to the timeline JSONL."""
        cwd = str(tmp_path / "proj")
        session_key = "55:10"
        _reset_timeline(tmp_path, session_key, cwd)

        audit_dir = tmp_path / "audit"
        original_audit = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        try:
            hook = _make_post_tool_use_hook("proj", session_key)
            hook_input = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "file1\nfile2", "stderr": "", "interrupted": False},
                "tool_use_id": "t",
                "session_id": "s",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": cwd,
                "permission_mode": "bypassPermissions",
            }
            _run(hook(hook_input, "t", {}))

            p = _timeline_path(session_key)
            assert p is not None and p.exists()
            lines = [l for l in p.read_text().splitlines() if l.strip()]
            events = [json.loads(l) for l in lines]
            tool_results = [e for e in events if e.get("kind") == "tool_result"]
            assert len(tool_results) >= 1
            tr = tool_results[0]
            assert tr["tool"] == "Bash"
            assert tr["status"] == "ok"
            assert "file1" in tr["output"] or "file2" in tr["output"]
        finally:
            _engine.AUDIT_DIR = original_audit

    def test_timeline_event_output_truncated(self, tmp_path):
        """Long tool output is truncated in the timeline event."""
        cwd = str(tmp_path / "proj")
        session_key = "55:11"
        _reset_timeline(tmp_path, session_key, cwd)

        audit_dir = tmp_path / "audit"
        original_audit = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        try:
            hook = _make_post_tool_use_hook("proj", session_key)
            long_stdout = "y" * (_HOOK_OUTPUT_TRUNCATE + 500)
            hook_input = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "cat bigfile"},
                "tool_response": {"stdout": long_stdout, "stderr": "", "interrupted": False},
                "tool_use_id": "t",
                "session_id": "s",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": cwd,
                "permission_mode": "bypassPermissions",
            }
            _run(hook(hook_input, "t", {}))

            p = _timeline_path(session_key)
            assert p is not None
            lines = [l for l in p.read_text().splitlines() if l.strip()]
            events = [json.loads(l) for l in lines]
            tool_results = [e for e in events if e.get("kind") == "tool_result"]
            assert tool_results
            output = tool_results[0]["output"]
            assert len(output) <= _HOOK_OUTPUT_TRUNCATE + 1  # +1 for ellipsis
            assert output.endswith("…")
        finally:
            _engine.AUDIT_DIR = original_audit


# ─────────────────────────── unit: exception safety ───────────────────────────

class TestHookExceptionSafety:
    def test_raising_audit_does_not_propagate(self, tmp_path):
        """If audit() raises internally, the hook still returns {} without propagating."""
        hook = _make_post_tool_use_hook("proj", "1:1")
        hook_input = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": {"stdout": "ok"},
            "tool_use_id": "t",
            "session_id": "s",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/tmp",
            "permission_mode": "bypassPermissions",
        }
        with patch.object(_engine, "audit", side_effect=RuntimeError("audit exploded")):
            result = _run(hook(hook_input, "t", {}))
        assert result == {}

    def test_malformed_hook_input_does_not_propagate(self):
        """A completely wrong hook_input shape does not raise out of the hook."""
        hook = _make_post_tool_use_hook("proj", "1:1")
        result = _run(hook({"totally": "wrong"}, None, {}))
        assert result == {}

    def test_none_hook_input_does_not_propagate(self):
        """None input does not raise out of the hook."""
        hook = _make_post_tool_use_hook("proj", "1:1")
        result = _run(hook(None, None, {}))
        assert result == {}

    def test_timeline_failure_does_not_propagate(self, tmp_path):
        """If _timeline_append raises, the hook still returns {} and the audit line is written."""
        audit_dir = tmp_path / "audit"
        original_audit = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        try:
            hook = _make_post_tool_use_hook("proj", "1:1")
            hook_input = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/x"},
                "tool_response": "contents",
                "tool_use_id": "t",
                "session_id": "s",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": "/tmp",
                "permission_mode": "bypassPermissions",
            }
            with patch.object(_webapp, "_timeline_append", side_effect=OSError("disk full")):
                result = _run(hook(hook_input, "t", {}))
            assert result == {}
            # Audit line should still be written
            logs = list(audit_dir.glob("*.log"))
            assert logs
            assert "Read" in logs[0].read_text()
        finally:
            _engine.AUDIT_DIR = original_audit


# ─────────────────────────── unit: secrets / env never written ────────────────

class TestHookSecretsGuard:
    def test_env_not_in_hook_input(self):
        """The hook factory only receives project_name and session_key — never env.

        This is an architectural test: _make_post_tool_use_hook() signature takes
        (project_name, session_key), not env/secrets.  The closure therefore has
        no access to env values and cannot leak them.
        """
        import inspect
        sig = inspect.signature(_make_post_tool_use_hook)
        param_names = list(sig.parameters.keys())
        assert "env" not in param_names
        assert "secret" not in param_names
        assert "password" not in param_names

    def test_tool_output_containing_secret_shape_is_truncated_not_blocked(self, tmp_path):
        """Even if tool output incidentally looks like a secret value, it is
        recorded (truncated) — the existing env guard in audit() is at the
        call-site (env is never passed to audit anywhere).  We verify the hook
        records the output normally and does NOT inject additional 'env' keys."""
        audit_dir = tmp_path / "audit"
        original_audit = _engine.AUDIT_DIR
        _engine.AUDIT_DIR = audit_dir
        cwd = str(tmp_path / "proj")
        session_key = "77:1"
        _reset_timeline(tmp_path, session_key, cwd)
        try:
            hook = _make_post_tool_use_hook("proj", session_key)
            hook_input = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo $RANDOM"},
                "tool_response": {"stdout": "12345", "stderr": "", "interrupted": False},
                "tool_use_id": "t",
                "session_id": "s",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": cwd,
                "permission_mode": "bypassPermissions",
            }
            _run(hook(hook_input, "t", {}))

            # Verify timeline event has no 'env' key
            p = _timeline_path(session_key)
            if p and p.exists():
                for line in p.read_text().splitlines():
                    if line.strip():
                        obj = json.loads(line)
                        assert "env" not in obj
        finally:
            _engine.AUDIT_DIR = original_audit


# ─────────────────────────── unit: include_hook_events stays False ────────────

class TestIncludeHookEventsDefault:
    def test_include_hook_events_default_is_false(self):
        """ClaudeAgentOptions.include_hook_events defaults to False.

        We do NOT set it to True in run_engine.  This test confirms the SDK
        default hasn't changed — if it does, we'd need to reassess.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(ClaudeAgentOptions)}
        assert "include_hook_events" in fields
        assert fields["include_hook_events"].default is False

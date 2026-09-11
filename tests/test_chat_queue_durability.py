"""spec-092 P0b: the chat queue's persistence failures must be observable.

The queue is the only record of a message the cockpit has already acknowledged to the
operator: `_chat_queue_pop` removes an item and flushes BEFORE the run starts, so a failed
flush followed by a crash loses a message the operator watched being accepted. That failure
used to be swallowed whole — no log line, no return value, nothing to diagnose from.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp


def test_flush_reports_success_when_the_write_lands(tmp_path):
    """A healthy flush returns True and the bytes are really on disk."""
    target = tmp_path / "queue.json"
    with (
        patch.object(webapp, "_CHAT_QUEUE_FILE", target),
        patch.object(webapp, "_CHAT_QUEUE", {"proj:chat": [{"id": "1", "text": "hi"}]}),
    ):
        assert webapp._chat_queue_flush() is True
    assert "hi" in target.read_text(encoding="utf-8")


def test_flush_reports_failure_and_logs_instead_of_swallowing(tmp_path, capsys):
    """An I/O error must return False AND name the file in the log.

    Without this the in-memory queue and the disk copy diverge in total silence, which is how
    an accepted message disappears with nothing to explain it.
    """
    target = tmp_path / "nonexistent-dir" / "queue.json"  # parent missing -> write fails
    with (
        patch.object(webapp, "_CHAT_QUEUE_FILE", target),
        patch.object(webapp, "_CHAT_QUEUE", {"proj:chat": [{"id": "1", "text": "hi"}]}),
    ):
        assert webapp._chat_queue_flush() is False
    out = capsys.readouterr().out
    assert "[chat-queue]" in out and "FAILED to persist" in out
    assert str(target) in out


def test_flush_without_a_configured_file_is_not_a_silent_success(tmp_path):
    """No queue file configured is 'not durable', not 'persisted fine'."""
    with patch.object(webapp, "_CHAT_QUEUE_FILE", None):
        assert webapp._chat_queue_flush() is False

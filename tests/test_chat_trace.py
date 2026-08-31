"""
Tests for the chat message-lifecycle trace (spec-087).

The timeline JSONL records TURNS. It has no notion of an operator MESSAGE, so every path
that can swallow one before it becomes a turn — the duplicate guard, a full queue, a
refused steer, a drain that declined — left nothing on disk. "My message vanished" was
unfalsifiable: no record could say whether the server ever saw it.

_chat_trace writes one line per stage, joined by the client's msg_id. These tests pin the
two properties that make it worth having: every terminal outcome IS recorded (a stage that
drops a message must never be silent), and the writer never raises into a send path.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp

SESSION_KEY = "1001:42"


@pytest.fixture
def trace_file(tmp_path, monkeypatch):
    f = tmp_path / "chat-trace.jsonl"
    monkeypatch.setattr(_webapp, "_CHAT_TRACE_FILE", f, raising=False)
    return f


def _rows(f):
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


# ─────────────────────────── the writer ───────────────────────────────────────


def test_trace_writes_one_line_per_stage(trace_file):
    _webapp._chat_trace(SESSION_KEY, "recv", msg_id="m-1", text="hello", via="POST /chat")
    _webapp._chat_trace(SESSION_KEY, "queue", msg_id="m-1", text="hello", item_id="q1")
    rows = _rows(trace_file)
    assert [r["stage"] for r in rows] == ["recv", "queue"]
    assert all(r["msg_id"] == "m-1" for r in rows)
    assert all(r["session_key"] == SESSION_KEY for r in rows)


def test_trace_records_text_identity_without_storing_it_twice(trace_file):
    long_text = "x" * 500
    _webapp._chat_trace(SESSION_KEY, "recv", msg_id="m-2", text=long_text)
    r = _rows(trace_file)[0]
    assert r["len"] == 500
    assert len(r["sha8"]) == 8
    # Preview is bounded — the trace must not become a second copy of every conversation.
    assert len(r["preview"]) == _webapp._CHAT_TRACE_PREVIEW


def test_trace_matches_identical_texts_by_digest(trace_file):
    """The digest is what lets a drop be tied to the send it duplicated."""
    _webapp._chat_trace(SESSION_KEY, "recv", msg_id="m-a", text="same words")
    _webapp._chat_trace(SESSION_KEY, "dedup_drop", msg_id="m-b", text="same words")
    a, b = _rows(trace_file)
    assert a["sha8"] == b["sha8"] and a["msg_id"] != b["msg_id"]


def test_trace_omits_absent_fields(trace_file):
    _webapp._chat_trace(SESSION_KEY, "drain_blocked", why="running", depth=None)
    r = _rows(trace_file)[0]
    assert r["why"] == "running"
    assert "depth" not in r        # None extras are dropped, not written as null
    assert "msg_id" not in r
    assert "preview" not in r      # no text passed → no text fields at all


def test_trace_never_raises_into_a_send_path(trace_file, monkeypatch):
    """A broken trace must degrade to silence, never break the message it is describing."""
    class Unserialisable:
        pass
    _webapp._chat_trace(SESSION_KEY, "recv", msg_id="m-3", text="ok", bad=Unserialisable())
    # File may hold nothing for that call, but the call itself must not have raised.
    monkeypatch.setattr(_webapp, "_CHAT_TRACE_FILE", None, raising=False)
    _webapp._chat_trace(SESSION_KEY, "recv", msg_id="m-4", text="ok")   # no file → no-op


def test_trace_rotates_past_the_size_cap(trace_file, monkeypatch):
    monkeypatch.setattr(_webapp, "_CHAT_TRACE_MAX_SIZE", 200, raising=False)
    for i in range(40):
        _webapp._chat_trace(SESSION_KEY, "recv", msg_id=f"m-{i}", text="padding" * 5)
    assert trace_file.with_suffix(".jsonl.1").exists()


# ─────────────────────────── queue carries msg_id ─────────────────────────────


def test_queue_item_carries_msg_id_for_the_join():
    """Without this the drain's trace line cannot be tied back to the send."""
    _webapp._CHAT_QUEUE.pop(SESSION_KEY, None)
    try:
        item = _webapp._chat_queue_enqueue(SESSION_KEY, "later", None, "proj", msg_id="m-9")
        assert item["msg_id"] == "m-9"
        plain = _webapp._chat_queue_enqueue(SESSION_KEY, "other", None, "proj")
        assert "msg_id" not in plain      # absent id must not become an empty string
    finally:
        _webapp._CHAT_QUEUE.pop(SESSION_KEY, None)

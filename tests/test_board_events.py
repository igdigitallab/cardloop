"""
Tests for spec-052 Phase 2: board_event bus primitive (board changes surface in chat).

- webapp._emit_board_event publishes a well-formed board_event and truncates fields.
- engine._apply_reconcile_ops emits one reconcile board_event per applied op,
  ONLY when a session_key and the injected bus callback are present, and ONLY
  after the board write succeeds.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine
import webapp


# ─── webapp._emit_board_event ────────────────────────────────────────────────

def test_emit_board_event_shape_and_truncation(monkeypatch):
    captured = []
    monkeypatch.setattr(webapp, "_bus_publish", lambda sk, ev, persist=True: captured.append((sk, ev, persist)))

    webapp._emit_board_event(
        "chat:thread",
        event="moved",
        card_id="abc123",
        title="T" * 200,
        column_from="backlog",
        column_to="done",
        severity="success",
        summary="S" * 300,
    )

    assert len(captured) == 1
    sk, ev, persist = captured[0]
    assert sk == "chat:thread"
    assert persist is True  # written to the timeline JSONL (NOT the SSE reconnect buffer)
    assert ev["kind"] == "board_event"
    assert ev["event"] == "moved"
    assert ev["card_id"] == "abc123"
    assert ev["column_to"] == "done"
    assert ev["severity"] == "success"
    assert len(ev["title"]) == 120  # truncated
    assert len(ev["summary"]) == 240  # truncated
    assert isinstance(ev["ts"], float)


def test_emit_board_event_noop_without_session_key(monkeypatch):
    captured = []
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: captured.append(a))
    webapp._emit_board_event("", event="moved", card_id="x")
    assert captured == []  # no session_key → silently skipped


# ─── spec-052 Phase 7: recent-board-events buffer (survives reload/hydration) ────

@pytest.fixture
def _clear_board_buffer():
    webapp._recent_board_events.clear()
    yield
    webapp._recent_board_events.clear()


def test_actionable_events_buffered_for_hydration(monkeypatch, _clear_board_buffer):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    for ev in ("moved", "incident", "reconcile"):
        webapp._emit_board_event("chat:42", event=ev, card_id=f"c-{ev}", title=ev)
    got = webapp._recent_board_events_for("chat:42")
    assert [e["event"] for e in got] == ["moved", "incident", "reconcile"]
    assert all(e["kind"] == "board_event" for e in got)


def test_transient_events_not_buffered(monkeypatch, _clear_board_buffer):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    webapp._emit_board_event("chat:42", event="run_start", card_id="r1")
    webapp._emit_board_event("chat:42", event="run_end", card_id="r1")
    # run_start/run_end drive the banner, not the notification feed → not buffered
    assert webapp._recent_board_events_for("chat:42") == []


def test_buffer_recency_window(monkeypatch, _clear_board_buffer):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    webapp._emit_board_event("chat:42", event="moved", card_id="old")
    # Backdate the buffered event beyond the retention window.
    webapp._recent_board_events["chat:42"][0]["ts"] -= webapp._BOARD_EVENTS_RETAIN_SEC + 60
    assert webapp._recent_board_events_for("chat:42") == []


def test_buffer_is_per_session(monkeypatch, _clear_board_buffer):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    webapp._emit_board_event("chat:A", event="moved", card_id="a1")
    webapp._emit_board_event("chat:B", event="moved", card_id="b1")
    assert [e["card_id"] for e in webapp._recent_board_events_for("chat:A")] == ["a1"]
    assert [e["card_id"] for e in webapp._recent_board_events_for("chat:B")] == ["b1"]


def test_board_events_cleared_on_reset(monkeypatch, _clear_board_buffer):
    # Reset/rotate must drop the old session's buffered strips so they don't
    # re-hydrate into the fresh chat (the reported "stale card after Reset Session" bug).
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    webapp._emit_board_event("chat:A", event="moved", card_id="a1")
    webapp._emit_board_event("chat:B", event="moved", card_id="b1")
    webapp._board_events_clear("chat:A")
    assert webapp._recent_board_events_for("chat:A") == []
    # Other sessions are untouched.
    assert [e["card_id"] for e in webapp._recent_board_events_for("chat:B")] == ["b1"]
    # Clearing an unknown session is a no-op (never raises).
    webapp._board_events_clear("chat:unknown")


# ─── engine._apply_reconcile_ops board_event emission ────────────────────────

FIXTURE = """\
# Tasks

## Backlog
- [ ] Existing card <!--ops:exist1-->

## In Progress
- [~] Active card <!--ops:inprog1-->

## Review

## Failed
"""


@pytest.mark.asyncio
async def test_reconcile_emits_board_event_per_op(tmp_path, monkeypatch):
    (tmp_path / "TASKS.md").write_text(FIXTURE, encoding="utf-8")
    cwd = str(tmp_path)

    captured = []
    monkeypatch.setattr(engine, "_bus_publish_cb", lambda sk, ev: captured.append((sk, ev)))

    ops = [
        {"op": "create", "text": "Fresh card", "column": "backlog"},
        {"op": "move", "id": "inprog1", "to": "done"},
    ]
    await engine._apply_reconcile_ops(cwd, "proj", ops, on_match="done", session_key="chat:thread")

    kinds = [ev["event"] for _, ev in captured]
    assert kinds == ["reconcile", "reconcile"]
    assert all(ev["kind"] == "board_event" for _, ev in captured)
    assert all(sk == "chat:thread" for sk, _ in captured)
    # the move→done op is announced as a success ("auto-closed")
    move_ev = next(ev for _, ev in captured if ev["column_to"] == "done")
    assert move_ev["severity"] == "success"
    assert "closed" in move_ev["summary"].lower()


@pytest.mark.asyncio
async def test_reconcile_no_board_event_without_session_key(tmp_path, monkeypatch):
    (tmp_path / "TASKS.md").write_text(FIXTURE, encoding="utf-8")
    cwd = str(tmp_path)

    captured = []
    monkeypatch.setattr(engine, "_bus_publish_cb", lambda sk, ev: captured.append((sk, ev)))

    ops = [{"op": "create", "text": "No-announce card", "column": "backlog"}]
    # session_key omitted → no board_event, but the op still applies to the board
    await engine._apply_reconcile_ops(cwd, "proj", ops, on_match="done")

    assert captured == []
    assert "No-announce card" in (tmp_path / "TASKS.md").read_text(encoding="utf-8")

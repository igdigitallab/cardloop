"""
spec-063 Stage 2a: autonomous background runs (drain-surfaced CLI turns) as first-class runs.

_bg_run_event(session_key, phase, text) — start creates a bg-marked live turn and publishes
kind:run_start source:'bg'; text appends seq-tagged events; end finishes the turn and
publishes run_end with a push preview. A concurrently starting operator turn must never be
clobbered (buffer guard).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp


@pytest.fixture(autouse=True)
def _clean():
    for d in (webapp._bg_run_ids, webapp._bg_run_buffered, webapp._bg_run_preview):
        d.clear()
    webapp._live_turns.pop("bg-s", None)
    webapp._live_seq.pop("bg-s", None)
    webapp._WEBAPP_CTX = {"running": {}}
    yield
    for d in (webapp._bg_run_ids, webapp._bg_run_buffered, webapp._bg_run_preview):
        d.clear()
    webapp._live_turns.pop("bg-s", None)
    webapp._live_seq.pop("bg-s", None)
    webapp._WEBAPP_CTX = None


def _collect_bus(monkeypatch):
    published = []
    monkeypatch.setattr(webapp, "_bus_publish",
                        lambda sk, e, persist=True: published.append((sk, e, persist)))
    monkeypatch.setattr(webapp, "_timeline_append", lambda sk, e: None)
    return published


def test_bg_run_lifecycle_publishes_and_buffers(monkeypatch):
    published = _collect_bus(monkeypatch)
    sk = "bg-s"
    webapp._bg_run_event(sk, "start")
    webapp._bg_run_event(sk, "text", "hello from the night shift")
    webapp._bg_run_event(sk, "end")

    kinds = [(e.get("kind"), e.get("type")) for _, e, _ in published]
    assert kinds[0] == ("run_start", None)
    assert kinds[1] == (None, "text")
    assert kinds[2] == ("run_end", None)
    start, text, end = (e for _, e, _ in published)
    assert start["source"] == "bg" and end["source"] == "bg"
    assert "seq" in text, "bg text must be seq-tagged (live-buffer parity)"
    assert end["preview"].startswith("hello from the night")
    # live turn was created bg-marked and finished
    turn = webapp._live_turns[sk]
    assert turn.get("bg") is True and turn["status"] == "done"


def test_bg_run_never_clobbers_operator_turn(monkeypatch):
    published = _collect_bus(monkeypatch)
    sk = "bg-s"
    # An operator turn is running (both signals: running flag + open non-bg live turn).
    webapp._WEBAPP_CTX = {"running": {sk: True}}
    op_turn = webapp._live_turn_create(sk, "opus", "operator prompt")
    webapp._bg_run_event(sk, "start")
    webapp._bg_run_event(sk, "text", "late bg text")
    webapp._bg_run_event(sk, "end")
    # Operator's live turn is untouched (no bg flag, same object, still running).
    assert webapp._live_turns[sk] is op_turn
    assert webapp._live_turns[sk].get("bg") is None
    assert webapp._live_turns[sk]["status"] == "running"
    # bg text still reached the bus (un-buffered fallback), marked bg.
    bg_text = next(e for _, e, _ in published if e.get("type") == "text")
    assert bg_text.get("bg") is True and "seq" not in bg_text

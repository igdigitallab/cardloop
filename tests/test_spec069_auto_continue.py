"""
spec-069 Phase 2: auto-continue on pending background work (RC#2 re-wake).

When the orchestrator's turn ends but background children are still running, a synthetic
continuation is enqueued into the chat queue (safe reuse of the tested drain path — NO concurrent
receive on the live client). Flag-gated (AUTO_CONTINUE_ON_BG, default off), bounded per episode.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp


@pytest.fixture(autouse=True)
def _clean_state():
    webapp._monitors.clear()
    webapp._CHAT_QUEUE.clear()
    webapp._bg_continue_count.clear()
    yield
    webapp._monitors.clear()
    webapp._CHAT_QUEUE.clear()
    webapp._bg_continue_count.clear()


def _running_monitor(session_key, kind="task", mid="m1"):
    webapp._monitors[session_key] = {mid: {"id": mid, "kind": kind, "status": "running"}}


# ─────────────────────────── _has_running_bg ────────────────────────────────────────────────────

def test_has_running_bg_true_for_task():
    _running_monitor("s", kind="task")
    assert webapp._has_running_bg("s") is True


def test_has_running_bg_excludes_raw_bash():
    _running_monitor("s", kind="bash")            # bg-bash lingers as running — must NOT count
    assert webapp._has_running_bg("s") is False


def test_has_running_bg_false_when_terminal():
    webapp._monitors["s"] = {"m1": {"id": "m1", "kind": "task", "status": "done"}}
    assert webapp._has_running_bg("s") is False


# ─────────────────────────── _maybe_schedule_bg_continuation ─────────────────────────────────────

def test_schedule_noop_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", False)
    _running_monitor("s")
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._maybe_schedule_bg_continuation({}, "s", None, None)
    assert spawned == [], "must not schedule anything when disabled"
    assert "s" not in webapp._bg_continue_count


def test_schedule_fires_when_bg_running(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    _running_monitor("s")
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._maybe_schedule_bg_continuation({}, "s", None, None)
    assert len(spawned) == 1, "must schedule exactly one continuation"
    assert webapp._bg_continue_count["s"] == 1


def test_schedule_resets_budget_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    webapp._bg_continue_count["s"] = 3            # a prior episode
    # no running monitors → episode over
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._maybe_schedule_bg_continuation({}, "s", None, None)
    assert spawned == []
    assert "s" not in webapp._bg_continue_count, "budget must reset once children are done"


def test_schedule_respects_budget_cap(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_MAX", 2)
    _running_monitor("s")
    webapp._bg_continue_count["s"] = 2            # already at cap
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._maybe_schedule_bg_continuation({}, "s", None, None)
    assert spawned == [], "must not re-wake past the budget cap"
    assert webapp._bg_continue_count["s"] == 2


# ─────────────────────────── _bg_continuation_after_grace ────────────────────────────────────────

@pytest.mark.asyncio
async def test_continuation_enqueues_after_grace(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_GRACE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())
    _running_monitor("s")
    await webapp._bg_continuation_after_grace({}, "s", None, "pid", 1)
    q = webapp._CHAT_QUEUE.get("s", [])
    assert len(q) == 1 and q[0]["text"] == webapp._BG_CONTINUE_PROMPT
    webapp._chat_queue_drain_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_continuation_skips_when_bg_cleared_during_grace(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_GRACE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())
    # bg finished before the grace elapsed → nothing pending
    await webapp._bg_continuation_after_grace({}, "s", None, "pid", 1)
    assert webapp._CHAT_QUEUE.get("s", []) == [], "no continuation when children already done"


@pytest.mark.asyncio
async def test_continuation_dedups_existing(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_GRACE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())
    _running_monitor("s")
    webapp._CHAT_QUEUE["s"] = [{"id": "x", "text": webapp._BG_CONTINUE_PROMPT, "created_at": 0}]
    await webapp._bg_continuation_after_grace({}, "s", None, "pid", 2)
    assert len(webapp._CHAT_QUEUE["s"]) == 1, "must not stack duplicate continuations"

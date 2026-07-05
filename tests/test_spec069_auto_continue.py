"""
spec-069 Phase 2 v2 (spec-071): completion-driven auto-continue.

v1 was a blind turn-end poll (grace loop, 5 attempts, budget never reset — went permanently
deaf). v2 fires from _monitor_update's running→terminal transition: _schedule_completion_wake
opens a debounce window, _completion_wake_fire enqueues ONE continuation naming the finished
children — only when the session is idle — reusing the operator's last per-turn options so the
live-client fingerprint stays stable. The budget resets on operator turns and on rotate.
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
    for d in (webapp._monitors, webapp._CHAT_QUEUE, webapp._bg_continue_count,
              webapp._completion_wake_pending, webapp._last_turn_options):
        d.clear()
    webapp._WEBAPP_CTX = None
    yield
    for d in (webapp._monitors, webapp._CHAT_QUEUE, webapp._bg_continue_count,
              webapp._completion_wake_pending, webapp._last_turn_options):
        d.clear()
    webapp._WEBAPP_CTX = None


def _running_monitor(session_key, kind="task", mid="m1"):
    webapp._monitors[session_key] = {mid: {"id": mid, "kind": kind, "status": "running"}}


def _terminal_rec(mid="m1", kind="agent", status="done", label="researcher"):
    return {"id": mid, "kind": kind, "status": status, "label": label}


def _ctx(running=None):
    return {"running": running or {}, "topics": {}, "REGISTRY": {}}


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


# ─────────────────────────── _schedule_completion_wake ──────────────────────────────────────────

def test_wake_noop_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", False)
    webapp._WEBAPP_CTX = _ctx()
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._schedule_completion_wake("s", _terminal_rec())
    assert spawned == [] and "s" not in webapp._completion_wake_pending


def test_wake_noop_without_ctx(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._schedule_completion_wake("s", _terminal_rec())
    assert spawned == []


def test_wake_debounces_bursts(monkeypatch):
    """A burst of completions joins ONE open window — a single fire task is spawned."""
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    webapp._WEBAPP_CTX = _ctx()
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    webapp._schedule_completion_wake("s", _terminal_rec("m1"))
    webapp._schedule_completion_wake("s", _terminal_rec("m2"))
    webapp._schedule_completion_wake("s", _terminal_rec("m3"))
    assert len(spawned) == 1, "burst must collapse into one wake"
    assert len(webapp._completion_wake_pending["s"]) == 3


def test_monitor_update_terminal_transition_triggers_wake(monkeypatch):
    """The wake is wired to the running→terminal transition inside _monitor_update."""
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    webapp._WEBAPP_CTX = _ctx()
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    _running_monitor("s", kind="agent")
    webapp._monitor_update("s", {"id": "m1", "status": "done"}, only_existing=True)
    assert len(spawned) == 1
    # A tail-only refresh on an already-terminal monitor must NOT re-trigger.
    webapp._monitor_update("s", {"id": "m1", "tail": "x"}, only_existing=True)
    assert len(spawned) == 1


def test_monitor_update_bash_completion_does_not_wake(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    webapp._WEBAPP_CTX = _ctx()
    spawned = []
    monkeypatch.setattr(webapp, "_spawn_bg", lambda c: (spawned.append(c), c.close()))
    _running_monitor("s", kind="bash")
    webapp._monitor_update("s", {"id": "m1", "status": "done"}, only_existing=True)
    assert spawned == []


# ─────────────────────────── _completion_wake_fire ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fire_enqueues_one_continuation(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_chat_queue_drain_one", AsyncMock())
    webapp._completion_wake_pending["s"] = [_terminal_rec(label="seo research")]
    webapp._last_turn_options["s"] = {"effort": "xhigh", "ultracode": True}
    await webapp._completion_wake_fire(_ctx(), "s")
    q = webapp._CHAT_QUEUE.get("s", [])
    assert len(q) == 1
    assert q[0]["text"].startswith(webapp._BG_CONTINUE_PREFIX)
    assert "seo research" in q[0]["text"] and "done" in q[0]["text"]
    # spec-071: the synthetic turn reuses the operator's options (fingerprint stability).
    assert q[0]["effort"] == "xhigh" and q[0]["ultracode"] is True
    assert webapp._bg_continue_count["s"] == 1
    webapp._chat_queue_drain_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_skips_when_session_running(monkeypatch):
    """An active turn sees completions natively — no synthetic wake."""
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    webapp._completion_wake_pending["s"] = [_terminal_rec()]
    await webapp._completion_wake_fire(_ctx(running={"s": True}), "s")
    assert webapp._CHAT_QUEUE.get("s", []) == []
    assert "s" not in webapp._bg_continue_count


@pytest.mark.asyncio
async def test_fire_respects_budget_cap(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_MAX", 2)
    webapp._bg_continue_count["s"] = 2            # already at cap
    webapp._completion_wake_pending["s"] = [_terminal_rec()]
    await webapp._completion_wake_fire(_ctx(), "s")
    assert webapp._CHAT_QUEUE.get("s", []) == [], "must not re-wake past the budget cap"


@pytest.mark.asyncio
async def test_fire_dedups_queued_continuation(monkeypatch):
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_ON", True)
    monkeypatch.setattr(webapp, "_AUTO_CONTINUE_DEBOUNCE_SEC", 0.0)
    webapp._CHAT_QUEUE["s"] = [{"id": "x", "text": webapp._BG_CONTINUE_PREFIX + " earlier",
                                "created_at": 0}]
    webapp._completion_wake_pending["s"] = [_terminal_rec()]
    await webapp._completion_wake_fire(_ctx(), "s")
    assert len(webapp._CHAT_QUEUE["s"]) == 1, "must not stack duplicate continuations"


# ─────────────────────────── episode reset ───────────────────────────────────────────────────────

def test_turn_end_resets_budget_when_nothing_pending():
    webapp._bg_continue_count["s"] = 3            # a prior (possibly exhausted) episode
    webapp._maybe_schedule_bg_continuation(_ctx(), "s", None, None)
    assert "s" not in webapp._bg_continue_count, "budget must reset once children are done"


def test_turn_end_keeps_budget_while_bg_running():
    _running_monitor("s")
    webapp._bg_continue_count["s"] = 2
    webapp._maybe_schedule_bg_continuation(_ctx(), "s", None, None)
    assert webapp._bg_continue_count["s"] == 2


def test_bg_continue_reset():
    webapp._bg_continue_count["s"] = 3
    webapp._bg_continue_reset("s")
    assert "s" not in webapp._bg_continue_count

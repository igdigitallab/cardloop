"""spec-088 P0 — the deterministic bugs behind "the chat shows nothing while agents work".

Incident 2026-09-01 (session free-f91f596f): (1) a turn started inside the previous turn's
5-minute retain window lost its live buffer when the old drop timer fired; (2) a model-issued
TaskStop woke the orchestrator as if work had finished; (3) /api/health?deep=1 could not see a
CLI-autonomous turn, so a deploy restarted the service under a live orchestration; (4) a bg
run only opened on the first TEXT of an autonomous turn, so minutes of tool calls were invisible.
"""
import asyncio

import pytest

import engine
import webapp
from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock


# ─────────────────────────── (1) live-turn drop is bound to its turn ───────────────────────────

def test_live_turn_drop_spares_a_newer_turn():
    sk = "spec088-drop"
    webapp._live_turns.pop(sk, None)
    webapp._live_seq.pop(sk, None)
    old = webapp._live_turn_create(sk, "opus", "first")
    webapp._live_turn_finish(sk, "done")
    new = webapp._live_turn_create(sk, "opus", "second")  # started inside the retain window
    webapp._live_turn_drop(sk, old)  # the OLD turn's deferred cleanup fires
    assert webapp._live_turns.get(sk) is new, "the old timer must not drop the new turn"
    tagged = webapp._live_turn_append(sk, {"kind": "steer", "text": "follow-up"})
    assert "seq" in tagged, "events of the surviving turn keep their seq"
    webapp._live_turn_drop(sk, new)
    assert sk not in webapp._live_turns
    webapp._live_seq.pop(sk, None)


def test_live_turn_drop_without_turn_keeps_legacy_semantics():
    sk = "spec088-drop-legacy"
    webapp._live_turns.pop(sk, None)
    webapp._live_turn_create(sk, "opus", "x")
    webapp._live_turn_drop(sk)
    assert sk not in webapp._live_turns
    webapp._live_seq.pop(sk, None)


@pytest.mark.asyncio
async def test_live_turn_finish_schedules_drop_bound_to_that_turn(monkeypatch):
    sk = "spec088-drop-sched"
    webapp._live_turns.pop(sk, None)
    calls = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_later", lambda delay, fn, *a: calls.append((fn, a)))
    old = webapp._live_turn_create(sk, "opus", "first")
    webapp._live_turn_finish(sk, "done")
    assert calls and calls[0][0] is webapp._live_turn_drop
    assert calls[0][1] == (sk, old), "the drop must carry the turn it was scheduled for"
    webapp._live_turns.pop(sk, None)
    webapp._live_seq.pop(sk, None)


# ─────────────────────────── (2) 'stopped' is not a completion ───────────────────────────

@pytest.fixture
def wake_capture(monkeypatch):
    scheduled = []
    monkeypatch.setattr(webapp, "_schedule_completion_wake",
                        lambda sk, rec: scheduled.append((sk, dict(rec))))
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_crash_state_mark_dirty", lambda: None)
    return scheduled


def _fresh(sk):
    webapp._monitors.pop(sk, None)


def test_plain_stopped_does_not_wake(wake_capture):
    sk = "spec088-wake-stopped"
    _fresh(sk)
    webapp._monitor_update(sk, {"id": "w1", "kind": "workflow", "status": "running", "label": "wf"})
    webapp._monitor_update(sk, {"id": "w1", "status": "stopped"})  # model TaskStop / operator
    assert wake_capture == []
    _fresh(sk)


def test_done_and_failed_still_wake(wake_capture):
    sk = "spec088-wake-done"
    _fresh(sk)
    webapp._monitor_update(sk, {"id": "a1", "kind": "agent", "status": "running", "label": "r"})
    webapp._monitor_update(sk, {"id": "a1", "status": "done"})
    webapp._monitor_update(sk, {"id": "a2", "kind": "agent", "status": "running", "label": "r"})
    webapp._monitor_update(sk, {"id": "a2", "status": "failed"})
    assert [r["id"] for _, r in wake_capture] == ["a1", "a2"]
    _fresh(sk)


def test_crash_recovery_and_stale_stopped_wake(wake_capture):
    sk = "spec088-wake-crash"
    _fresh(sk)
    webapp._monitor_update(sk, {"id": "w1", "kind": "workflow", "status": "running", "label": "wf"})
    webapp._monitor_update(sk, {"id": "w1", "status": "stopped", "crash_recovery": True})
    webapp._monitor_update(sk, {"id": "a1", "kind": "agent", "status": "running", "label": "r"})
    webapp._monitor_update(sk, {"id": "a1", "status": "stopped", "stale": True})
    assert [r["id"] for _, r in wake_capture] == ["w1", "a1"]
    _fresh(sk)


# ─────────────────────────── (3) health sees CLI-autonomous turns ───────────────────────────

@pytest.mark.asyncio
async def test_health_deep_reports_bg_turns(aiohttp_client, monkeypatch):
    app = webapp.web.Application()
    app["ctx"] = {"running": {}}
    app.router.add_get("/api/health", webapp.api_health)
    client = await aiohttp_client(app)
    monkeypatch.setattr(webapp, "_bg_run_ids", {"s1": "abc123", "s2": "def456"})
    monkeypatch.setattr(webapp, "_bg_run_started", {"s1": webapp.time.monotonic(),
                                                    "s2": webapp.time.monotonic() - 10 ** 6})
    data = await (await client.get("/api/health?deep=1")).json()
    assert data["bg_turns"] == 1, "a stale bg marker must not count; a live one must"
    assert data["running"] == 0


# ─────────────────────────── (4) bg run opens on the first assistant message ───────────────────

class _FakeDrainClient:
    def __init__(self, messages):
        self._messages = messages
        self._blocker = asyncio.Event()

    async def receive_messages(self):
        for m in self._messages:
            yield m
        await self._blocker.wait()


@pytest.mark.asyncio
async def test_drain_opens_bg_run_on_tool_only_assistant_message(monkeypatch):
    bg_events = []
    monkeypatch.setattr(engine, "_bg_run_cb",
                        lambda sk, phase, text=None: bg_events.append((phase, text)))
    msgs = [
        AssistantMessage(content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"})],
                         model="m"),
        AssistantMessage(content=[TextBlock(text="done looking")], model="m"),
    ]
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    assert bg_events[0] == ("start", None), "the run must open before the first text arrives"
    assert ("text", "done looking") in bg_events
    assert bg_events[-1] == ("end", None)

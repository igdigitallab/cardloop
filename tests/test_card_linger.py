"""
Root-fix A4: ephemeral (card) runs must linger for still-open deferring background tasks
(local_agent / local_workflow) instead of letting the `async with` exit disconnect —
which SIGTERMs the children mid-work. The linger is a no-op when nothing is open.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
from claude_agent_sdk.types import (
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
)


def _task_started(task_id: str, task_type: str = "local_agent") -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=task_id, description="bg child",
        uuid="u1", session_id="s1", task_type=task_type)


def _task_done(task_id: str) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id, status="completed",
        output_file=None, summary="done", uuid="u2", session_id="s1")


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s1")


def _make_client(events: dict, *, response_msgs, late_msgs):
    """Fake ClaudeSDKClient: receive_response yields the turn (ends at ResultMessage),
    receive_messages yields the post-result stream the linger reads."""

    class FakeClient:
        def __init__(self, options):
            events["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            for m in response_msgs:
                yield m

        async def receive_messages(self):
            events.setdefault("linger_reads", 0)
            for m in late_msgs:
                events["linger_reads"] += 1
                yield m
            # Keep the stream open like the real SDK — the linger must exit via its own
            # empty-set check, not via StopAsyncIteration.
            await asyncio.sleep(3600)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events["disconnected"] = True
            return False

    return FakeClient


async def _run(events, response_msgs, late_msgs, monitor_updates):
    def _mu(session_key, delta, only_existing=False):
        monitor_updates.append(dict(delta))

    with patch.object(engine, "ClaudeSDKClient",
                      _make_client(events, response_msgs=response_msgs, late_msgs=late_msgs)), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None), \
         patch.object(engine, "_monitor_update_cb", _mu):
        out = []
        async for ev in bot.run_engine(project_name="t", cwd="/tmp", prompt="hi",
                                       session_key="c:t", model="sonnet"):
            out.append(ev)
        return out


async def test_no_deferring_tasks_no_linger(tmp_path):
    """Common case: nothing open at turn end → receive_messages is never touched."""
    events: dict = {}
    updates: list = []
    await _run(events, [_result()], [_task_done("never")], updates)
    assert events.get("disconnected") is True
    assert "linger_reads" not in events  # linger loop never engaged


async def test_linger_waits_for_open_deferring_task(tmp_path):
    """A local_agent started but not finished at ResultMessage → the run lingers, consumes
    the late terminal notification, flips the monitor, and only then disconnects."""
    events: dict = {}
    updates: list = []
    await _run(events,
               [_task_started("a1"), _result()],
               [_task_done("a1")],
               updates)
    assert events.get("disconnected") is True
    assert events.get("linger_reads") == 1
    assert any(u.get("id") == "a1" and u.get("status") == "done" for u in updates)


async def test_linger_ignores_non_deferring_types(tmp_path):
    """Background shells (task_type not in DEFERRING_TASK_TYPES) must not trigger a linger —
    they run indefinitely by SDK design and would hang every card for the full cap."""
    events: dict = {}
    updates: list = []
    await _run(events,
               [_task_started("sh1", task_type="local_shell"), _result()],
               [_task_done("sh1")],
               updates)
    assert events.get("disconnected") is True
    assert "linger_reads" not in events


async def test_linger_timeout_flips_monitor_failed(tmp_path, monkeypatch):
    """Cap reached with the child still open → the monitor is flipped failed IMMEDIATELY
    (so the completion wake hears it now, not via the 15-min staleness sweep)."""
    monkeypatch.setattr(engine, "CARD_LINGER_MAX_SEC", 0)  # expire instantly
    events: dict = {}
    updates: list = []
    await _run(events,
               [_task_started("a2"), _result()],
               [],  # nothing ever arrives
               updates)
    assert events.get("disconnected") is True
    assert any(u.get("id") == "a2" and u.get("status") == "failed"
               and "card run ended" in u.get("tail", "") for u in updates)


async def test_notification_during_turn_clears_before_linger(tmp_path):
    """A task that starts AND finishes inside the turn leaves nothing to linger for."""
    events: dict = {}
    updates: list = []
    await _run(events,
               [_task_started("a3"), _task_done("a3"), _result()],
               [],
               updates)
    assert events.get("disconnected") is True
    assert "linger_reads" not in events
    assert any(u.get("id") == "a3" and u.get("status") == "done" for u in updates)

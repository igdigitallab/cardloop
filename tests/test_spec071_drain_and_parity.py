"""
spec-071: between-turns stream drain, sub-agent chat-lane filter, terminal-status unification,
session-monotonic live seq, widened eviction guard, queue-item per-turn options.

Background (diagnosis 2026-07-05): with PERSISTENT_CLIENT=1 nothing consumed the SDK's bounded
message buffer between turns — the CLI stalled (~1 tool round / 10 min), completions arrived
only on the next operator send, and forwarded sub-agent messages chopped the chat canvas
mid-word. These tests pin the structural fixes.
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolUseBlock,
)


def _notification(task_id, status, tool_use_id=None):
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id, status=status,
        output_file="", summary="done", uuid="u", session_id="sid",
        tool_use_id=tool_use_id,
    )


def _updated(task_id, patch_status):
    return TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id=task_id, patch={"status": patch_status},
    )


def _result(session_id="sid"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id=session_id)


# ─────────────────────────── _notification_monitor_delta ────────────────────────────────────────

def test_delta_from_notification_completed():
    d = engine._notification_monitor_delta(_notification("t1", "completed", tool_use_id="tu"))
    assert d == {"id": "t1", "tool_use_id": "tu", "status": "done"}


def test_delta_from_notification_killed_maps_to_stopped():
    # The old in-turn map lacked 'killed' — those flips were silently lost.
    d = engine._notification_monitor_delta(_notification("t1", "killed"))
    assert d and d["status"] == "stopped"


def test_delta_from_task_updated_patch_status():
    # Per SDK docs a terminal state can arrive ONLY as task_updated (e.g. TaskStop → killed).
    d = engine._notification_monitor_delta(_updated("t2", "killed"))
    assert d and d["id"] == "t2" and d["status"] == "stopped"


def test_delta_ignores_non_terminal():
    assert engine._notification_monitor_delta(_updated("t3", "running")) is None


# ─────────────────────────── between-turns drain ─────────────────────────────────────────────────

class _FakeDrainClient:
    """Yields the scripted messages, then blocks forever (like a real idle stream)."""

    def __init__(self, messages):
        self._messages = messages
        self._blocker = asyncio.Event()

    async def receive_messages(self):
        for m in self._messages:
            yield m
        await self._blocker.wait()


@pytest.mark.asyncio
async def test_drain_flips_monitors_and_surfaces_autonomous_turn(monkeypatch):
    flips, published = [], []
    monkeypatch.setattr(engine, "_monitor_update_cb",
                        lambda sk, d, only_existing=False: flips.append((sk, d)))
    monkeypatch.setattr(engine, "_bus_publish_cb", lambda sk, e: published.append((sk, e)))
    msgs = [
        _notification("a1", "completed"),
        _updated("w1", "killed"),
        AssistantMessage(content=[TextBlock(text="sub-agent noise")], model="m",
                         parent_tool_use_id="tu1"),
        AssistantMessage(content=[TextBlock(text="background reply")], model="m"),
        _result(),
    ]
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    assert entry.drain_task is not None
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    assert entry.drain_task is None

    assert ("s", {"id": "a1", "tool_use_id": None, "status": "done"}) in flips
    assert ("s", {"id": "w1", "tool_use_id": None, "status": "stopped"}) in flips
    kinds = [e.get("kind") for _, e in published]
    assert kinds == ["bg_text", "bg_turn_end"], "autonomous turn must surface, sub-agent noise must not"
    assert published[0][1]["text"] == "background reply"


@pytest.mark.asyncio
async def test_drain_start_is_idempotent_and_flag_gated(monkeypatch):
    entry = engine._LiveEntry(client=_FakeDrainClient([]), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    t1 = entry.drain_task
    engine._start_drain(entry, None)
    assert entry.drain_task is t1, "second start must not spawn a duplicate reader"
    await engine._stop_drain(entry)

    monkeypatch.setattr(engine, "LIVE_CLIENT_DRAIN", False)
    entry2 = engine._LiveEntry(client=_FakeDrainClient([]), fingerprint="f",
                               last_used=0.0, idle_task=None, session_key="s2")
    engine._start_drain(entry2, None)
    assert entry2.drain_task is None, "flag off → no drain"


# ─────────────────────────── chat-lane sub-agent filter ─────────────────────────────────────────

class _FakeTurnClient:
    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        for m in self._messages:
            yield m


@pytest.mark.asyncio
async def test_run_engine_filters_parented_subagent_messages(tmp_path):
    """Forwarded sub-agent AssistantMessages (parent_tool_use_id set) must never become
    text/tool events in the main chat lane — they chopped the streamed answer mid-word."""
    msgs = [
        AssistantMessage(content=[ToolUseBlock(id="x", name="Bash", input={"command": "ls"}),
                                  TextBlock(text="sub-agent text")],
                         model="m", parent_tool_use_id="tu9",
                         usage={"input_tokens": 999_999}),
        AssistantMessage(content=[TextBlock(text="orchestrator answer")], model="m",
                         usage={"input_tokens": 7}),
        _result(),
    ]
    events = []
    with patch.object(engine, "ClaudeSDKClient", return_value=_FakeTurnClient(msgs)):
        async for ev in engine.run_engine(
            project_name="t", cwd=str(tmp_path), prompt="hi",
            session_key="chat:spec071", model="opus", ctx=None,
        ):
            events.append(ev)

    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["orchestrator answer"]
    assert not [e for e in events if e["type"] == "tool"], "sub-agent tool must not leak"
    result = next(e for e in events if e["type"] == "result")
    assert result["context_tokens"] == 7, "sub-agent usage must not inflate context tracking"


# ─────────────────────────── session-monotonic live seq ─────────────────────────────────────────

def test_live_seq_survives_turn_boundaries_and_drop():
    sk = "seq-test"
    webapp._live_turns.pop(sk, None)
    webapp._live_seq.pop(sk, None)
    webapp._live_turn_create(sk, "opus", "one")
    for i in range(5):
        webapp._live_turn_append(sk, {"type": "text_delta", "text": str(i)})
    webapp._live_turn_finish(sk, "done")
    webapp._live_turn_drop(sk)  # retention cleanup — used to reset seq to 0
    turn2 = webapp._live_turn_create(sk, "opus", "two")
    assert turn2["seq"] == 5, "seq must continue across turns (grow-only client cursor)"
    tagged = webapp._live_turn_append(sk, {"type": "text", "text": "x"})
    assert tagged["seq"] == 5
    webapp._live_turns.pop(sk, None)
    webapp._live_seq.pop(sk, None)


# ─────────────────────────── widened eviction guard ──────────────────────────────────────────────

def test_has_live_agent_monitors_counts_workflow_and_monitor_kinds():
    sk = "guard-test"
    try:
        for kind, expected in (("agent", True), ("workflow", True), ("monitor", True),
                               ("bash", False), ("task", False)):
            webapp._monitors[sk] = {"m": {"id": "m", "kind": kind, "status": "running"}}
            assert webapp._has_live_agent_monitors(sk) is expected, kind
        webapp._monitors[sk] = {"m": {"id": "m", "kind": "workflow", "status": "done"}}
        assert webapp._has_live_agent_monitors(sk) is False
    finally:
        webapp._monitors.pop(sk, None)


# ─────────────────────────── queue items carry per-turn options ──────────────────────────────────

def test_chat_queue_enqueue_carries_effort_and_ultracode():
    sk = "q-test"
    try:
        item = webapp._chat_queue_enqueue(sk, "hello", None, "pid",
                                          effort="xhigh", ultracode=True)
        assert item["effort"] == "xhigh" and item["ultracode"] is True
        plain = webapp._chat_queue_enqueue(sk, "plain", None, "pid")
        assert "effort" not in plain and "ultracode" not in plain
    finally:
        webapp._CHAT_QUEUE.pop(sk, None)
        webapp._chat_queue_flush()


# ─────────────────────────── glob-based agent reconcile (card sessions) ─────────────────────────

def test_reconcile_agent_monitor_from_parent_flips_card_session_zombie(tmp_path):
    """An agent spawned in an isolated (card) session completes there — the active-chat
    reconcile never scans that transcript. The path-derived scan must flip it."""
    sk = "card-test"
    agent_id = "a071389dbe85bfe96"
    sdk_dir = tmp_path / "proj-slug"
    subagents = sdk_dir / "sess-1" / "subagents"
    subagents.mkdir(parents=True)
    agent_path = subagents / f"agent-{agent_id}.jsonl"
    agent_path.write_text("{}\n", encoding="utf-8")
    parent = sdk_dir / "sess-1.jsonl"
    xml = (f"<task-notification><task-id>{agent_id}</task-id>"
           f"<status>completed</status></task-notification>")
    parent.write_text(json.dumps({"type": "queue-operation", "operation": "enqueue",
                                  "content": xml}) + "\n", encoding="utf-8")
    try:
        webapp._monitors[sk] = {agent_id: {"id": agent_id, "kind": "agent",
                                           "status": "running", "label": "audit"}}
        webapp._reconcile_agent_monitor_from_parent(sk, agent_path)
        assert webapp._monitors[sk][agent_id]["status"] == "done"
    finally:
        webapp._monitors.pop(sk, None)

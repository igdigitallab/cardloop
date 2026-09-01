"""
Tests for spec-085 Phase 3: sub-agent text forwarding (_subagent_text_events).

With the SDK's forward_subagent_text on, a sub-agent's finalized TextBlocks arrive as
AssistantMessages tagged with the spawning Agent tool_use id. The engine helper maps
them to the task_id from TaskStartedMessage.tool_use_id and emits capped
{"type":"subagent","subtype":"text"} lane events.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine as _engine
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)


def _sub_msg(text_blocks, parent="tu-1"):
    return AssistantMessage(
        content=[TextBlock(text=t) for t in text_blocks],
        model="sonnet",
        parent_tool_use_id=parent,
    )


def test_forwards_mapped_text():
    tool_to_task = {"tu-1": "task-A"}
    counts: dict = {}
    events = _engine._subagent_text_events(_sub_msg(["hello from sub"]), tool_to_task, counts)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "subagent" and ev["subtype"] == "text"
    assert ev["task_id"] == "task-A"
    assert ev["text"] == "hello from sub"
    assert counts["task-A"] == 1


def test_unmapped_parent_dropped():
    events = _engine._subagent_text_events(_sub_msg(["orphan"]), {}, {})
    assert events == []


def test_non_assistant_message_dropped():
    class NotAssistant:
        parent_tool_use_id = "tu-1"

    events = _engine._subagent_text_events(NotAssistant(), {"tu-1": "task-A"}, {})
    assert events == []


def test_flag_off_disables(monkeypatch):
    monkeypatch.setattr(_engine, "FORWARD_SUBAGENT_TEXT", 0)
    events = _engine._subagent_text_events(_sub_msg(["hi"]), {"tu-1": "task-A"}, {})
    assert events == []


def test_char_cap_truncates():
    long = "x" * (_engine.SUBAGENT_TEXT_MAX_CHARS + 500)
    events = _engine._subagent_text_events(_sub_msg([long]), {"tu-1": "task-A"}, {})
    assert len(events[0]["text"]) == _engine.SUBAGENT_TEXT_MAX_CHARS


def test_block_cap_per_task():
    tool_to_task = {"tu-1": "task-A"}
    counts: dict = {}
    cap = _engine.SUBAGENT_TEXT_MAX_BLOCKS
    total = 0
    # Stream more blocks than the cap across several messages — the tail is dropped.
    for _ in range(cap + 10):
        total += len(_engine._subagent_text_events(_sub_msg(["block"]), tool_to_task, counts))
    assert total == cap
    assert counts["task-A"] == cap
    # A DIFFERENT task still has its own budget.
    tool_to_task["tu-2"] = "task-B"
    events = _engine._subagent_text_events(_sub_msg(["other"], parent="tu-2"), tool_to_task, counts)
    assert len(events) == 1 and events[0]["task_id"] == "task-B"


def test_thinking_and_tool_blocks_skipped():
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="pondering...", signature="sig"),
            ToolUseBlock(id="t1", name="Bash", input={}),
            TextBlock(text="only me"),
        ],
        model="sonnet",
        parent_tool_use_id="tu-1",
    )
    events = _engine._subagent_text_events(msg, {"tu-1": "task-A"}, {})
    assert [e["text"] for e in events] == ["only me"]


def test_whitespace_text_skipped():
    events = _engine._subagent_text_events(_sub_msg(["   \n  "]), {"tu-1": "task-A"}, {})
    assert events == []


def test_options_wire_forward_subagent_text():
    """The main-turn ClaudeAgentOptions must carry forward_subagent_text=bool(flag)."""
    src = (ROOT / "engine.py").read_text()
    assert "forward_subagent_text=bool(FORWARD_SUBAGENT_TEXT)" in src


# ─────────── task_type on the lane events (phantom-row fix, 2026-08-23) ───────────
#
# The CLI reports a sub-agent's OWN tool executions as tasks too, with the shell command as
# their description. The cockpit had no way to tell those apart, so one running agent drew
# three rows in the lane (the ⚙ chip read 2/3) titled "sleep 20 && echo slept-1" — observed
# live in the browser. Progress and notification messages carry no task_type of their own, so
# the engine learns it from the started message and stamps it on the rest.

def test_started_event_carries_task_type():
    """Only a started message knows the type — it must reach the client."""
    import inspect
    src = inspect.getsource(_engine.run_engine)
    assert '"task_type": getattr(msg, "task_type", None),' in src, \
        "started events must forward the CLI's task_type"


def test_progress_and_notification_are_stamped_from_the_started_map():
    """Progress/notification have no task_type field — they get it from the per-turn map."""
    import inspect
    src = inspect.getsource(_engine.run_engine)
    assert src.count('"task_type": _sub_task_types.get(msg.task_id) or None,') == 2, \
        "progress AND notification must be stamped from the task_id -> task_type map"
    assert "_sub_task_types[msg.task_id] = getattr(msg, \"task_type\", None) or \"\"" in src, \
        "the map must be filled from the started message"
    assert "_sub_task_types: dict = {}" in src, \
        "the map is per-turn state, declared inside the message loop"


def test_webapp_forwards_task_type_to_the_client():
    from pathlib import Path
    import webapp as _webapp
    src = Path(_webapp.__file__).read_text(encoding="utf-8")
    assert '"task_type": event.get("task_type"),' in src, \
        "the SSE/bus payload must carry task_type or the lane cannot filter"


# ─────────────── cross-session peer messages (live surfacing) ────────────────
#
# Another Claude Code session can write to this one; the CLI replays that as a user turn
# carrying `origin`. The engine loop used to ignore UserMessage entirely, so the delivery was
# invisible until the operator reloaded and the history feed matched the raw envelope.

class _Origin(dict):
    """MessageOrigin is a TypedDict — a plain dict at runtime."""


class _UserMsg:
    def __init__(self, content, origin=None):
        self.content = content
        self.origin = origin
        self.parent_tool_use_id = None


def test_peer_message_uses_the_decoded_body():
    ev = _engine._peer_message_event(_UserMsg(
        "Another Claude session sent a message: <agent-message from=\"auditor\">raw envelope",
        _Origin(kind="peer", name="auditor", body="audit done, 3 findings", fromSession="sess-9"),
    ))
    assert ev is not None
    assert ev["type"] == "peer_message" and ev["kind"] == "peer"
    assert ev["text"] == "audit done, 3 findings", "the SDK's decoded body, not the envelope"
    assert ev["sender"] == "auditor" and ev["from_session"] == "sess-9"


def test_peer_message_falls_back_to_content_without_a_body():
    ev = _engine._peer_message_event(_UserMsg("plain delivery", _Origin(kind="channel", server="slack")))
    assert ev is not None and ev["text"] == "plain delivery" and ev["sender"] == "slack"


def test_ordinary_traffic_never_produces_a_peer_message():
    """Tool results carry no origin; the operator's own turn is 'human'."""
    assert _engine._peer_message_event(_UserMsg("tool result", None)) is None
    assert _engine._peer_message_event(_UserMsg("my prompt", _Origin(kind="human"))) is None
    assert _engine._peer_message_event(_UserMsg("", _Origin(kind="peer"))) is None


def test_noise_kinds_are_skipped_but_unknown_kinds_pass():
    """The SDK says to treat unrecognized kinds as 'not human' — so the filter is a denylist."""
    for noisy in ("auto-continuation", "task-notification", "observer-activity"):
        assert _engine._peer_message_event(_UserMsg("x", _Origin(kind=noisy, body="x"))) is None
    ev = _engine._peer_message_event(_UserMsg("x", _Origin(kind="some-future-kind", body="hello")))
    assert ev is not None and ev["kind"] == "some-future-kind"


def test_peer_message_text_is_capped():
    ev = _engine._peer_message_event(_UserMsg("", _Origin(kind="peer", body="z" * 20000)))
    assert ev is not None and len(ev["text"]) == _engine.PEER_MESSAGE_MAX_CHARS


# ─── docs/internal/sdk-feature-audit/02-subagent-output.md: TaskNotificationMessage.output_file ───
#
# output_file is a REQUIRED field on TaskNotificationMessage — the CLI unconditionally names an
# on-disk file alongside every task-completion notification. engine.py discarded it; surface it
# on the "subagent"/"notification" event so the cockpit can offer the raw output even when the
# sub-agent forgot to cockpit-file its own report.

class _FakeTurnClient:
    """A ClaudeSDKClient stand-in that replays a fixed message list for one turn."""

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


def _notification(task_id, output_file, tool_use_id=None):
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id, status="completed",
        output_file=output_file, summary="done", uuid="u", session_id="sid",
        tool_use_id=tool_use_id,
    )


def _started(task_id, tool_use_id=None):
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=task_id, description="do X",
        uuid="u", session_id="sid", tool_use_id=tool_use_id, task_type="local_agent",
    )


def _result(session_id="sid"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id=session_id)


@pytest.mark.asyncio
async def test_notification_event_carries_output_file(tmp_path):
    msgs = [
        _started("t1", tool_use_id="tu1"),
        _notification("t1", "/tmp/agent-t1-report.jsonl", tool_use_id="tu1"),
        _result(),
    ]
    events = []
    with patch.object(_engine, "ClaudeSDKClient", return_value=_FakeTurnClient(msgs)):
        async for ev in _engine.run_engine(
            project_name="t", cwd=str(tmp_path), prompt="hi",
            session_key="chat:output-file-test", model="opus", ctx=None,
        ):
            events.append(ev)

    notif = next(e for e in events if e["type"] == "subagent" and e["subtype"] == "notification")
    assert notif["output_file"] == "/tmp/agent-t1-report.jsonl"


def test_notification_event_output_file_key_uses_defensive_getattr():
    """SDK guarantees the field, but the branch must not crash if a future SDK drops it."""
    import inspect
    src = inspect.getsource(_engine.run_engine)
    assert '"output_file": getattr(msg, "output_file", None),' in src


def test_webapp_forwards_output_file_to_the_client():
    import webapp as _webapp
    src = Path(_webapp.__file__).read_text(encoding="utf-8")
    assert '"output_file": event.get("output_file"),' in src, \
        "the SSE/bus payload must carry output_file or the cockpit can't offer the raw output"

"""
Tests for spec-085 Phase 3: sub-agent text forwarding (_subagent_text_events).

With the SDK's forward_subagent_text on, a sub-agent's finalized TextBlocks arrive as
AssistantMessages tagged with the spawning Agent tool_use id. The engine helper maps
them to the task_id from TaskStartedMessage.tool_use_id and emits capped
{"type":"subagent","subtype":"text"} lane events.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine as _engine
from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock, ToolUseBlock


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

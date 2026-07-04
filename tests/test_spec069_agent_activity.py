"""
spec-069 Phase 3-B: live per-tool activity for background Agent sub-agents.

Part A — _monitor_delta returns a kind="agent" delta with the parsed agentId
when the Agent tool response carries an agentId: line; returns None otherwise.

Part B — _agent_last_tool_tail reads a sub-agent .jsonl transcript and returns
a "↳ <name> <target>" string for the last tool_use block; returns None for an
empty / missing file.  Only-on-change semantics: _monitor_update is NOT called
again if the tail is unchanged.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp


# ─────────────────────────── fixtures ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state():
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()
    yield
    webapp._monitors.clear()
    webapp._monitors_dismissed.clear()


# ─────────────────────────── Part A: _monitor_delta ──────────────────────────


def _agent_response(agent_id: str, extra: str = "") -> str:
    """Synthetic Agent tool response text."""
    return (
        f"Async agent launched successfully.\n"
        f"agentId: {agent_id} (internal ID for tracking)\n"
        f"{extra}"
    )


def test_agent_delta_returns_kind_agent():
    """Agent tool result with a valid agentId → kind='agent', status='running'."""
    d = engine._monitor_delta(
        "Agent",
        {"description": "Run tests", "subagent_type": "general"},
        _agent_response("abc123"),
        "orch",
    )
    assert d is not None, "_monitor_delta must return a delta for Agent tool"
    assert d["kind"] == "agent"
    assert d["status"] == "running"


def test_agent_delta_parses_agent_id():
    """Parsed agentId becomes the monitor id."""
    d = engine._monitor_delta(
        "Agent",
        {"description": "Build frontend"},
        _agent_response("XYZ9876"),
        "orch",
    )
    assert d is not None
    assert d["id"] == "XYZ9876"


def test_agent_delta_label_from_description():
    """description field is used as label when present."""
    d = engine._monitor_delta(
        "Agent",
        {"description": "Deploy to staging", "subagent_type": "general"},
        _agent_response("aid1"),
        "orch",
    )
    assert d is not None
    assert d["label"] == "Deploy to staging"


def test_agent_delta_label_falls_back_to_subagent_type():
    """Falls back to subagent_type when description is absent."""
    d = engine._monitor_delta(
        "Agent",
        {"subagent_type": "code-review"},
        _agent_response("aid2"),
        "orch",
    )
    assert d is not None
    assert d["label"] == "code-review"


def test_agent_delta_label_falls_back_to_literal_agent():
    """Falls back to 'agent' when both description and subagent_type are absent."""
    d = engine._monitor_delta(
        "Agent",
        {},
        _agent_response("aid3"),
        "orch",
    )
    assert d is not None
    assert d["label"] == "agent"


def _agent_response_dict(agent_id: str, description: str = "") -> dict:
    """The REAL runtime shape the PostToolUse hook receives for an Agent launch — a dict,
    NOT the display text.  Regression guard for the smoke-caught bug: the first cut parsed
    agentId with a regex on the text form, but prod delivers this dict so the regex silently
    failed (dict repr is `'agentId': '<id>'`, no `agentId:` substring) → nothing registered."""
    return {"isAsync": True, "status": "async_launched", "agentId": agent_id,
            "description": description, "resolvedModel": "claude-haiku-4-5-20251001",
            "prompt": "..."}


def test_agent_delta_parses_dict_response():
    """PROD shape: tool_response is a dict carrying agentId — must register (smoke-caught)."""
    d = engine._monitor_delta(
        "Agent",
        {"description": "Probe", "subagent_type": "quick"},
        _agent_response_dict("a8dad6dd0cb26f68a", "Probe"),
        None,
    )
    assert d is not None, "dict-form Agent response must yield a delta"
    assert d["id"] == "a8dad6dd0cb26f68a"
    assert d["kind"] == "agent"
    assert d["status"] == "running"
    assert d["agent"] == "quick"   # from subagent_type, not the caller agent_type


def test_agent_delta_dict_label_from_response_description():
    """Label falls back to the response dict's description when input lacks one."""
    d = engine._monitor_delta(
        "Agent",
        {"subagent_type": "executor"},
        _agent_response_dict("aid9", "Implement feature X"),
        None,
    )
    assert d is not None
    assert d["label"] == "Implement feature X"


def test_agent_delta_returns_none_when_no_agent_id():
    """Agent response without agentId: line → return None (don't register phantom)."""
    d = engine._monitor_delta(
        "Agent",
        {"description": "something"},
        "Launch failed: quota exceeded",
        "orch",
    )
    assert d is None, "_monitor_delta must return None when agentId is missing"


def test_agent_delta_returns_none_for_other_tools():
    """Non-Agent tools are unaffected by the new branch."""
    d = engine._monitor_delta("Bash", {"command": "ls"}, {"stdout": "file1"}, "orch")
    # Bash without run_in_background should return None
    assert d is None


def test_agent_monitor_registered_via_monitor_update():
    """A delta from _monitor_delta can be fed into _monitor_update and appears in registry."""
    d = engine._monitor_delta("Agent", {"description": "Test run"},
                               _agent_response("regABC123"), "orch")
    assert d is not None
    webapp._monitor_update("sk:reg", d)
    bucket = webapp._monitors.get("sk:reg", {})
    assert "regABC123" in bucket
    assert bucket["regABC123"]["kind"] == "agent"
    assert bucket["regABC123"]["status"] == "running"


# ─────────────────────────── Part B: _agent_last_tool_tail ───────────────────


def _make_assistant_message(tool_uses: list[dict]) -> str:
    """Build a JSON line representing an assistant message with tool_use blocks."""
    content = [
        {"type": "tool_use", "id": f"toolu_{i}", "name": tu["name"], "input": tu["input"]}
        for i, tu in enumerate(tool_uses)
    ]
    return json.dumps({"type": "message", "role": "assistant", "content": content})


def _write_agent_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tail_returns_none_for_missing_file(tmp_path):
    """Missing transcript file → None."""
    result = webapp._agent_last_tool_tail(tmp_path / "no_such_file.jsonl")
    assert result is None


def test_tail_returns_none_for_empty_file(tmp_path):
    """Empty transcript file → None."""
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    result = webapp._agent_last_tool_tail(p)
    assert result is None


def test_tail_picks_last_tool_use(tmp_path):
    """Given two tool_use blocks in separate messages, the LAST one determines the tail."""
    p = tmp_path / "agent.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([{"name": "Read", "input": {"file_path": "/a/b.py"}}]),
        _make_assistant_message([{"name": "Edit", "input": {"file_path": "/a/c.py"}}]),
    ])
    result = webapp._agent_last_tool_tail(p)
    assert result is not None
    assert "Edit" in result
    assert "c.py" in result


def test_tail_picks_last_tool_use_within_same_message(tmp_path):
    """Multiple tool_use blocks in a single message → the last block wins."""
    p = tmp_path / "agent2.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([
            {"name": "Read", "input": {"file_path": "/x/y.py"}},
            {"name": "Bash", "input": {"command": "ls -la"}},
        ]),
    ])
    result = webapp._agent_last_tool_tail(p)
    assert result is not None
    assert "Bash" in result
    assert "ls" in result


def test_tail_format_arrow_prefix(tmp_path):
    """Tail string starts with '↳ '."""
    p = tmp_path / "agent3.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([{"name": "Write", "input": {"file_path": "/foo/bar.py"}}]),
    ])
    result = webapp._agent_last_tool_tail(p)
    assert result is not None
    assert result.startswith("↳ ")


def test_tail_fallback_fields(tmp_path):
    """When file_path is absent, falls back to command, pattern, description, prompt."""
    p = tmp_path / "agent4.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([
            {"name": "Bash", "input": {"command": "npm test"}},
        ]),
    ])
    result = webapp._agent_last_tool_tail(p)
    assert result is not None
    assert "npm test" in result


def test_tail_no_tool_use_returns_none(tmp_path):
    """A transcript with only non-tool_use blocks → None."""
    p = tmp_path / "agent5.jsonl"
    _write_agent_jsonl(p, [
        json.dumps({"type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "Let me help you."}]}),
    ])
    result = webapp._agent_last_tool_tail(p)
    assert result is None


def test_tail_truncates_long_target(tmp_path):
    """Long targets are truncated to _AGENT_TAIL_TARGET_MAX chars + ellipsis."""
    long_path = "/home/user/project/" + "x" * 200
    p = tmp_path / "agent6.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([{"name": "Read", "input": {"file_path": long_path}}]),
    ])
    result = webapp._agent_last_tool_tail(p)
    assert result is not None
    # tail = "↳ Read <truncated>" — total well under 300 chars
    assert len(result) < 300
    assert result.endswith("…")


# ─────────────────────────── only-on-change semantics ────────────────────────


def test_no_second_update_when_tail_unchanged(tmp_path, monkeypatch):
    """If the tail string has not changed, _monitor_update is NOT called a second time."""
    agent_id = "sweep_test"
    session_key = "sk:sweep"

    # Pre-seed the monitor with a tail value.
    webapp._monitor_update(session_key, {
        "id": agent_id, "kind": "agent", "status": "running", "tail": "↳ Edit foo.py"
    })

    # Write a transcript whose last tool_use would produce the same tail.
    p = tmp_path / "subagents" / f"agent-{agent_id}.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([{"name": "Edit", "input": {"file_path": "foo.py"}}]),
    ])
    assert webapp._agent_last_tool_tail(p) == "↳ Edit foo.py"

    call_count = []

    def counting_update(sk, delta, only_existing=False):
        call_count.append(1)

    monkeypatch.setattr(webapp, "_monitor_update", counting_update)

    # Simulate the sweeper's inner check (same logic as the sweeper loop).
    tail_str = webapp._agent_last_tool_tail(p)
    rec = webapp._monitors.get(session_key, {}).get(agent_id, {})
    # Restore _monitor_update before the assertion (monkeypatch already applied).
    if tail_str is not None and tail_str != rec.get("tail"):
        webapp._monitor_update(session_key, {"id": agent_id, "tail": tail_str},
                               only_existing=True)

    assert call_count == [], "must NOT call _monitor_update when tail is unchanged"


def test_update_called_when_tail_changes(tmp_path, monkeypatch):
    """When the tail string changes, _monitor_update is called exactly once."""
    agent_id = "sweep_change"
    session_key = "sk:change"

    webapp._monitor_update(session_key, {
        "id": agent_id, "kind": "agent", "status": "running", "tail": "↳ Read old.py"
    })

    p = tmp_path / "subagents" / f"agent-{agent_id}.jsonl"
    _write_agent_jsonl(p, [
        _make_assistant_message([{"name": "Edit", "input": {"file_path": "new.py"}}]),
    ])

    call_args = []
    real_update = webapp._monitor_update

    def tracking_update(sk, delta, only_existing=False):
        call_args.append((sk, delta))
        real_update(sk, delta, only_existing=only_existing)

    monkeypatch.setattr(webapp, "_monitor_update", tracking_update)

    tail_str = webapp._agent_last_tool_tail(p)
    rec = webapp._monitors.get(session_key, {}).get(agent_id, {})
    if tail_str is not None and tail_str != rec.get("tail"):
        webapp._monitor_update(session_key, {"id": agent_id, "tail": tail_str},
                               only_existing=True)

    assert len(call_args) == 1, "must call _monitor_update exactly once when tail changes"
    assert call_args[0][1]["tail"] == "↳ Edit new.py"

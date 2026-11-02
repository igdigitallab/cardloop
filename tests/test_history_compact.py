"""
A native compaction in the chat feed.

The CLI writes a compaction as a `compact_boundary` system line, then the summary as a `type:user`
line flagged `isCompactSummary` (~13K chars, "This session is being continued…"), then — for a
manual `/compact` — the command echo and `<local-command-stdout>Compacted</local-command-stdout>`
as plain user lines. Before this, the summary and the stdout rendered as two bubbles the operator
never typed, and the summary was taken for the operator's last instruction. The rows below are the
shape recorded from CLI 2.1.316 (`/compact` through the Agent SDK), with the content shortened.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import history_index as hix
import webapp as _webapp

SUMMARY = ("This session is being continued from a previous conversation that ran out of context. "
           "The summary below covers the earlier portion of the conversation.\n\n"
           "<system-reminder>\n<tools-used-before-summary></tools-used-before-summary>\n"
           "</system-reminder>\nSummary:\n1. Primary Request and Intent:\n"
           "   - The user asked me to remember the codeword PELICAN-42.\n")


def _line(i, typ, content, **extra):
    msg = {"role": typ, "content": content}
    return {"type": typ, "uuid": f"x{i}", "timestamp": f"2026-11-02T16:10:{i:02d}Z",
            "message": msg, **extra}


def _transcript(after_compact=True):
    rows = [
        _line(1, "user", "Remember the codeword PELICAN-42. Reply with one word: ok."),
        _line(2, "assistant", [{"type": "text", "text": "ok"}]),
        _line(3, "user", "What is 17*3? Reply with the number only."),
        _line(4, "assistant", [{"type": "text", "text": "51"}]),
        {"type": "system", "subtype": "compact_boundary", "isMeta": False, "uuid": "x5",
         "timestamp": "2026-11-02T16:10:05Z", "logicalParentUuid": "x4",
         "compactMetadata": {"trigger": "manual", "preTokens": 16675}},
        _line(6, "user", SUMMARY, isCompactSummary=True, isVisibleInTranscriptOnly=True),
        _line(7, "user", "<local-command-caveat>The command below was run directly in Claude Code"
                         "</local-command-caveat>", isMeta=True),
        _line(8, "user", "<command-name>/compact</command-name>\n<command-message>compact"
                         "</command-message>\n<command-args></command-args>"),
        _line(9, "user", "<local-command-stdout>Compacted </local-command-stdout>"),
    ]
    if after_compact:
        rows += [_line(10, "user", "What was the codeword? Reply with it only."),
                 _line(11, "assistant", [{"type": "text", "text": "PELICAN-42"}])]
    return rows


def _write(path: Path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache():
    hix._CACHE.clear()
    yield
    hix._CACHE.clear()


def test_the_summary_is_a_compact_row_and_the_cli_stdout_is_not_an_operator_bubble(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, _transcript())
    for read in (_webapp._session_history, _webapp._session_history_scan):
        feed = read(p, limit=50)
        assert [m["role"] for m in feed] == ["user", "assistant", "user", "assistant",
                                             "compact", "user", "assistant"], read.__name__
        compact = feed[4]
        assert compact["text"].startswith("This session is being continued")
        assert "PELICAN-42" in compact["text"]
        assert "<system-reminder>" not in compact["text"]
        assert compact["uuid"] == "x6"
        assert not any("local-command-stdout" in (m["text"] or "") for m in feed)
        assert not any((m["text"] or "").startswith("This session is being continued")
                       for m in feed if m["role"] == "user")


def test_a_compaction_is_never_taken_for_the_operators_last_instruction(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, _transcript(after_compact=False))          # /compact was the last thing that happened
    _ts, user, agent = _webapp._transcript_tail(p, True)
    assert user["text"] == "What is 17*3? Reply with the number only."
    assert agent["text"] == "51"


def test_a_summary_line_is_not_a_sessions_first_prompt(tmp_path):
    p = tmp_path / "s.jsonl"
    rows = _transcript()
    _write(p, rows[4:])                                   # a session whose transcript opens at the summary
    first = hix.first_user_line(p, _webapp._history_line_msg)
    assert first is not None and first["uuid"] != "x6"     # not the summary (x6)
    assert not first["message"]["content"].startswith("This session is being continued")


def test_an_ordinary_local_command_stdout_is_left_alone(tmp_path):
    """Only the fixed `/compact` echo is dropped — other local-command output is not this fix."""
    p = tmp_path / "s.jsonl"
    _write(p, [_line(1, "user", "<local-command-stdout>Set model to opus</local-command-stdout>")])
    assert [m["role"] for m in _webapp._session_history_scan(p, limit=10)] == ["user"]


@pytest.mark.parametrize("env,expected", [
    ({}, 1_000_000),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500k"}, 500_000),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400K"}, 400_000),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"}, 300_000),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1M"}, 1_000_000),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "2M"}, 1_000_000),       # never past the model's own
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "auto"}, 1_000_000),     # not a size: the model default
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500k", "CONTEXT_WINDOW": "800000"}, 800_000),
])
def test_the_meter_window_follows_the_clis_auto_compact_window(env, expected):
    assert _webapp._context_window_from_env(env) == expected

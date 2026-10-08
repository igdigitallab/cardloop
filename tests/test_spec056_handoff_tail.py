"""
Tests for spec-056: recency-weighted handoff — deterministic tail assembly.

Covers:
  1. When history has a final user message and a final assistant message, the
     fact_lines in the assembled handoff contain the two new deterministic lines:
       "Last instruction (operator): ..."
       "Where we stopped (agent's last message):\n..."
  2. With empty history the two lines are absent and no exception is raised.
  3. Long user/assistant texts pass whole up to their caps; over a cap the head and the END
     survive and only the middle is elided (the open question sits at the end).
  4. Interleaved history: only the LAST user and LAST assistant messages are used.
"""
import sys
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


# ─────────────────────────── helpers ────────────────────────────────────────

def _make_ctx(tmp_path):
    """Minimal ctx for _build_handoff_inner calls."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return {
        "topics": {},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "reconcile_board": None,
    }


def _fake_jsonl(tmp_path: Path, session_id: str) -> Path:
    """Write a minimal non-empty .jsonl so _build_handoff_inner passes the early guards.

    The actual parsing is mocked out via _session_history / _session_context patches,
    so the content only needs to be non-empty (stat.st_size > 0).
    """
    sessions_dir = tmp_path / ".claude" / "projects" / str(tmp_path).replace("/", "-")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    jsonl = sessions_dir / f"{session_id}.jsonl"
    jsonl.write_text('{"type":"dummy"}\n')
    return jsonl


async def _run_build_handoff_inner(
    tmp_path: Path,
    session_id: str,
    history: list[dict],
) -> str:
    """Run _build_handoff_inner with mocked model calls and filesystem helpers."""
    ctx = _make_ctx(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir(exist_ok=True)
    cwd = str(project_dir)

    jsonl_path = _fake_jsonl(tmp_path, session_id)
    sessions_dir = jsonl_path.parent

    async def _noop_haiku(prompt, opts):
        return "narrative placeholder"

    with (
        patch.object(_webapp, "_sdk_sessions_dir", return_value=sessions_dir),
        patch.object(_webapp, "_session_context", return_value={"edited": [], "commands": []}),
        patch.object(_webapp, "_session_history", return_value=history),
        patch.object(_webapp, "_haiku_summarize", side_effect=_noop_haiku),
        patch("subprocess.run", return_value=MagicMock(returncode=1)),  # no git
        patch.dict("sys.modules", {"board": MagicMock(board_summary=lambda cwd: "")}),
    ):
        result = await _webapp._build_handoff_inner(ctx, "key:1", cwd, session_id)

    return result


# ─────────────────────────── 1. Deterministic lines present ─────────────────

async def test_deterministic_tail_lines_present(tmp_path):
    """Last instruction and Where we stopped appear in the assembled handoff."""
    history = [
        {"role": "user",      "text": "Please implement the login page.", "tools": []},
        {"role": "assistant", "text": "Done. Login page is at web/Login.tsx.", "tools": []},
        {"role": "user",      "text": "Now add the logout button.",         "tools": []},
        {"role": "assistant", "text": "Logout button added in Header.tsx.",  "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-001", history)

    assert "Last instruction (operator): Now add the logout button." in result, (
        f"Expected last user line in result. Got:\n{result}"
    )
    assert "Where we stopped (agent's last message):" in result, (
        f"Expected agent stop line in result. Got:\n{result}"
    )
    assert "Logout button added in Header.tsx." in result, (
        f"Expected last assistant text in result. Got:\n{result}"
    )


# ─────────────────────────── 2. Empty history → lines absent, no exception ──

async def test_deterministic_tail_empty_history(tmp_path):
    """Empty history: neither deterministic fact line appears; no exception raised."""
    result = await _run_build_handoff_inner(tmp_path, "sess-002", [])

    assert "Last instruction (operator):" not in result, (
        f"Empty history must not produce Last instruction line. Got:\n{result}"
    )
    assert "Where we stopped (agent's last message):" not in result, (
        f"Empty history must not produce Where we stopped line. Got:\n{result}"
    )


# ─────────────────────────── 3. Verbatim caps ───────────────────────────────

async def test_long_last_agent_message_passes_whole(tmp_path):
    """A long status report reaches the next session whole, closing question included.

    Regression: the 1200-char cap cut a report mid-table (2026-10-08) and the question that
    was waiting on the operator never reached the new session."""
    report = ("| item | fix |\n|---|---|\n" + "| row | detail |\n" * 300
              + "Pick one: (1) fix all ten fields, (2) hand over, (3) show texts first?")
    assert len(report) > 5000
    history = [
        {"role": "user",      "text": "are we ready?", "tools": []},
        {"role": "assistant", "text": report,          "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-003", history)

    assert "Where we stopped (agent's last message):\n" + report in result


async def test_over_cap_keeps_head_and_end(tmp_path):
    """Over the cap only the middle goes: the start and the very end both survive."""
    cap_u = _webapp._HANDOFF_LAST_USER_CHARS
    cap_a = _webapp._HANDOFF_LAST_AGENT_CHARS
    long_user = "HEADU" + "u" * (cap_u * 2) + "ENDU"
    long_asst = "HEADA" + "a" * (cap_a * 2) + "QUESTION?"
    history = [
        {"role": "user",      "text": long_user, "tools": []},
        {"role": "assistant", "text": long_asst, "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-003b", history)

    assert "Last instruction (operator): HEADU" in result
    assert "ENDU" in result
    assert "Where we stopped (agent's last message):\nHEADA" in result
    assert result.rstrip().endswith("QUESTION?")
    assert f"[… {len(long_asst) - cap_a} characters omitted …]" in result
    # Bounded: neither block may carry the whole oversized text.
    assert "a" * (cap_a + 1) not in result
    assert "u" * (cap_u + 1) not in result


async def test_unanswered_last_instruction_is_flagged(tmp_path):
    """When the operator spoke last, the agent's message is labelled as an earlier reply."""
    history = [
        {"role": "user",      "text": "Fix the login page.",       "tools": []},
        {"role": "assistant", "text": "Login page fixed.",         "tools": []},
        {"role": "user",      "text": "Now ship it to prod.",      "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-003c", history)

    assert "Last instruction (operator): Now ship it to prod." in result
    assert "never answered this instruction" in result
    assert "reply to an EARLIER turn" in result


async def test_answered_last_instruction_is_not_flagged(tmp_path):
    history = [
        {"role": "user",      "text": "Now ship it to prod.", "tools": []},
        {"role": "assistant", "text": "Shipped.",             "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-003d", history)

    assert "never answered" not in result


# ─────────────────────────── 4. Only LAST user/assistant taken ───────────────

async def test_deterministic_tail_uses_last_entries(tmp_path):
    """When multiple user and assistant turns exist, only the last of each is used."""
    history = [
        {"role": "user",      "text": "First user message.",   "tools": []},
        {"role": "assistant", "text": "First assistant reply.", "tools": []},
        {"role": "user",      "text": "Second user message.",  "tools": []},
        {"role": "assistant", "text": "Second assistant reply, the final one.", "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-004", history)

    assert "Second user message." in result, "Last user message must appear"
    assert "Second assistant reply, the final one." in result, "Last assistant message must appear"
    # Earlier messages must NOT appear as the deterministic lines (they may appear
    # in the narrative, but the fact-line prefix guards the check)
    assert "Last instruction (operator): First user message." not in result

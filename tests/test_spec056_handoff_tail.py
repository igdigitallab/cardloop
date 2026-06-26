"""
Tests for spec-056: recency-weighted handoff — deterministic tail assembly.

Covers:
  1. When history has a final user message and a final assistant message, the
     fact_lines in the assembled handoff contain the two new deterministic lines:
       "Last instruction (operator): ..."
       "Where we stopped (agent's last message):\n..."
  2. With empty history the two lines are absent and no exception is raised.
  3. Long user/assistant texts are truncated to the spec limits (1000 / 1200 chars).
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


# ─────────────────────────── 3. Truncation limits ────────────────────────────

async def test_deterministic_tail_truncation(tmp_path):
    """User text truncated at 1000 chars; assistant text truncated at 1200 chars."""
    long_user = "U" * 1500
    long_asst = "A" * 1800
    history = [
        {"role": "user",      "text": long_user, "tools": []},
        {"role": "assistant", "text": long_asst, "tools": []},
    ]

    result = await _run_build_handoff_inner(tmp_path, "sess-003", history)

    # Exact truncation: spec says ~1000 and ~1200
    assert "U" * 1001 not in result, "User text must be truncated to 1000 chars"
    assert "U" * 1000 in result,     "User text up to 1000 chars must appear"
    assert "A" * 1201 not in result, "Assistant text must be truncated to 1200 chars"
    assert "A" * 1200 in result,     "Assistant text up to 1200 chars must appear"


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

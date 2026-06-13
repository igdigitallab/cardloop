"""
Tests for spec-034 Phase 1: Board-Centric OS (L0, L1, L2).

L0: board.py extracted primitives + board_summary
L1: _build_board_append injects board protocol + card snapshot into system_prompt
L2: reconcile_board background task applies haiku-suggested ops to the board
"""
import asyncio
import json
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── ensure project root is on sys.path ───────────────────────────────────────
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ─── imports ──────────────────────────────────────────────────────────────────
import board as board_module
from board import (
    BOARD_COLUMNS,
    _load_board,
    _save_board,
    _tasks_path,
    board_summary,
)
import bot as bot_module
from bot import _build_board_append, reconcile_board, _apply_reconcile_ops


# ═══════════════════════════════════════════════════════════════════════════════
# L0 — board_summary tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBoardSummary:
    def test_no_tasks_md_returns_empty_string(self, tmp_path):
        """board_summary returns '' when TASKS.md does not exist."""
        result = board_summary(str(tmp_path))
        assert result == ""

    def test_empty_board_returns_board_is_empty(self, tmp_path):
        """board_summary returns 'Board is empty.' when TASKS.md exists but has no open cards."""
        tasks = tmp_path / "TASKS.md"
        tasks.write_text("# Tasks\n\n## Backlog\n\n## In Progress\n\n## Review\n", encoding="utf-8")
        result = board_summary(str(tmp_path))
        assert result == "Board is empty."

    def test_shows_open_cards(self, tmp_path):
        """board_summary shows backlog/in_progress/review cards."""
        tasks = tmp_path / "TASKS.md"
        tasks.write_text(
            "# Tasks\n\n"
            "## Backlog\n- [ ] Fix login bug <!--ops:abc123-->\n\n"
            "## In Progress\n- [~] Refactor auth <!--ops:def456-->\n\n"
            "## Review\n- [?] Add unit tests <!--ops:ghi789-->\n\n"
            "## Failed\n- [!] Old attempt <!--ops:fail01-->\n",
            encoding="utf-8",
        )
        result = board_summary(str(tmp_path))
        assert "abc123" in result
        assert "Fix login bug" in result
        assert "def456" in result
        assert "ghi789" in result
        # failed cards must NOT appear
        assert "fail01" not in result

    def test_done_cards_not_shown(self, tmp_path):
        """board_summary does not show cards from DONE.md."""
        tasks = tmp_path / "TASKS.md"
        tasks.write_text("# Tasks\n\n## Backlog\n- [ ] Active task <!--ops:aaa111-->\n", encoding="utf-8")
        done = tmp_path / "DONE.md"
        done.write_text("- [x] Old done task <!--ops:ddd999-->\n", encoding="utf-8")
        result = board_summary(str(tmp_path))
        assert "ddd999" not in result
        assert "aaa111" in result

    def test_groups_by_column(self, tmp_path):
        """board_summary groups cards under column headers."""
        tasks = tmp_path / "TASKS.md"
        tasks.write_text(
            "## Backlog\n- [ ] Task A <!--ops:aaa111-->\n\n"
            "## In Progress\n- [~] Task B <!--ops:bbb222-->\n",
            encoding="utf-8",
        )
        result = board_summary(str(tmp_path))
        # Backlog header should appear before In Progress header
        assert result.index("Backlog") < result.index("In Progress")

    def test_truncates_at_40_cards(self, tmp_path):
        """board_summary caps output at ~40 cards."""
        lines = ["## Backlog"]
        for i in range(50):
            lines.append(f"- [ ] Card number {i:03d} <!--ops:{i:06x}-->")
        (tmp_path / "TASKS.md").write_text("\n".join(lines), encoding="utf-8")
        result = board_summary(str(tmp_path))
        # Should mention truncation
        assert "truncated" in result.lower() or result.count("- [") <= 40


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — _build_board_append / board context injection
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildBoardAppend:
    def test_no_tasks_md_returns_empty(self, tmp_path):
        """_build_board_append returns '' when TASKS.md is absent."""
        result = _build_board_append(str(tmp_path))
        assert result == ""

    def test_with_tasks_md_contains_protocol_header(self, tmp_path):
        """_build_board_append includes the board protocol header text."""
        (tmp_path / "TASKS.md").write_text(
            "## Backlog\n- [ ] Deploy service <!--ops:abc123-->\n",
            encoding="utf-8",
        )
        result = _build_board_append(str(tmp_path))
        assert "Board protocol" in result

    def test_with_known_card_id_in_append(self, tmp_path):
        """When TASKS.md has a card with id 'abc123', that id appears in the board append."""
        (tmp_path / "TASKS.md").write_text(
            "## Backlog\n- [ ] Fix the auth bug <!--ops:abc123-->\n",
            encoding="utf-8",
        )
        result = _build_board_append(str(tmp_path))
        assert "abc123" in result
        assert "Board protocol" in result  # protocol header present

    def test_empty_board_returns_nonempty_append(self, tmp_path):
        """Even with an empty board, _build_board_append returns content (not '') if TASKS.md exists."""
        (tmp_path / "TASKS.md").write_text(
            "## Backlog\n\n## In Progress\n",
            encoding="utf-8",
        )
        result = _build_board_append(str(tmp_path))
        # TASKS.md exists → inject protocol block even if no open cards
        assert "Board protocol" in result or "Board is empty" in result


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — reconcile_board unit tests (haiku call mocked)
# ═══════════════════════════════════════════════════════════════════════════════

FIXTURE_TASKS_MD = """\
# Tasks

## Backlog
- [ ] Existing backlog card <!--ops:exist1-->

## In Progress
- [~] Active work card <!--ops:inprog1-->

## Review

## Failed
"""


def _write_fixture_board(tmp_path: Path) -> Path:
    """Write FIXTURE_TASKS_MD to tmp_path/TASKS.md and return tmp_path."""
    (tmp_path / "TASKS.md").write_text(FIXTURE_TASKS_MD, encoding="utf-8")
    return tmp_path


def _make_haiku_mock(json_response: str):
    """Create an async-generator mock for _sdk_query.

    claude_agent_sdk.query is an async generator function (not a regular async def).
    The mock must also be an async generator function so that
    `async for msg in _sdk_query(...)` works without a preceding `await`.
    """
    # Import the real types so isinstance checks in reconcile_board pass.
    from claude_agent_sdk import AssistantMessage, TextBlock  # type: ignore

    fake_text_block = MagicMock(spec=TextBlock)
    fake_text_block.text = json_response

    fake_msg = MagicMock(spec=AssistantMessage)
    fake_msg.content = [fake_text_block]

    # Must be an async generator function (uses `yield`), NOT `async def` + `return`.
    async def _fake_sdk_query(**kwargs):
        yield fake_msg

    return _fake_sdk_query


@pytest.mark.asyncio
async def test_reconcile_creates_card_on_completed_work(tmp_path, monkeypatch):
    """Scenario (a): agent completed work → haiku returns create op → card created on board."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)

    create_op = json.dumps([{"op": "create", "text": "New feature done", "column": "review"}])

    monkeypatch.setattr(bot_module, "_sdk_query", _make_haiku_mock(create_op))
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Please add the new feature",
        agent_summary="I have implemented the new feature successfully.",
    )

    # Verify card was written to board
    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    assert "New feature done" in content


@pytest.mark.asyncio
async def test_reconcile_no_ops_on_question(tmp_path, monkeypatch):
    """Scenario (b): pure question → haiku returns [] → board unchanged."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)
    original_content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")

    empty_ops = "[]"
    monkeypatch.setattr(bot_module, "_sdk_query", _make_haiku_mock(empty_ops))
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="What is the status of the project?",
        agent_summary="The project is going well, currently working on auth.",
    )

    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    assert content == original_content


@pytest.mark.asyncio
async def test_reconcile_disabled_by_env(tmp_path, monkeypatch):
    """Scenario (c): BOARD_RECONCILE=0 → no haiku call, board unchanged."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)
    original_content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    monkeypatch.setenv("BOARD_RECONCILE", "0")

    mock_sdk = AsyncMock(side_effect=Exception("should not be called"))
    monkeypatch.setattr(bot_module, "_sdk_query", mock_sdk)
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Do some work",
        agent_summary="Done.",
    )

    # Board unchanged, haiku was never called
    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    assert content == original_content
    mock_sdk.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_malformed_json_noop(tmp_path, monkeypatch):
    """Scenario (d): haiku returns invalid JSON → no-op, no crash, board unchanged."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)
    original_content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")

    bad_json = "this is not json at all!"
    monkeypatch.setattr(bot_module, "_sdk_query", _make_haiku_mock(bad_json))
    # Must not raise
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Do work",
        agent_summary="Done.",
    )

    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    assert content == original_content


@pytest.mark.asyncio
async def test_reconcile_no_tasks_md_skips(tmp_path, monkeypatch):
    """reconcile_board skips entirely when TASKS.md is absent."""
    cwd = str(tmp_path)  # no TASKS.md

    mock_sdk = AsyncMock(side_effect=Exception("should not be called"))
    monkeypatch.setattr(bot_module, "_sdk_query", mock_sdk)
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Do work",
        agent_summary="Done.",
    )

    mock_sdk.assert_not_called()
    assert not (tmp_path / "TASKS.md").exists()


@pytest.mark.asyncio
async def test_reconcile_move_card_to_done(tmp_path, monkeypatch):
    """reconcile_board moves an existing card to done (archived in DONE.md)."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)

    move_op = json.dumps([{"op": "move", "id": "inprog1", "to": "done"}])
    monkeypatch.setattr(bot_module, "_sdk_query", _make_haiku_mock(move_op))
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Finish the active work card",
        agent_summary="I have completed the active work card.",
    )

    tasks_content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    done_content = (tmp_path / "DONE.md").read_text(encoding="utf-8") if (tmp_path / "DONE.md").exists() else ""
    # The card should be removed from TASKS.md
    assert "inprog1" not in tasks_content
    # And appear in DONE.md
    assert "inprog1" in done_content


@pytest.mark.asyncio
async def test_reconcile_dedupe_create(tmp_path, monkeypatch):
    """reconcile_board skips creating a card whose title matches an existing open card."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)

    # "Existing backlog card" already exists with id exist1
    dup_op = json.dumps([{"op": "create", "text": "Existing backlog card", "column": "backlog"}])
    monkeypatch.setattr(bot_module, "_sdk_query", _make_haiku_mock(dup_op))
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Add the existing backlog card",
        agent_summary="Done.",
    )

    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    # Only the original card should be present (not duplicated)
    assert content.count("exist1") == 1
    # Title should appear exactly once (no duplicate)
    assert content.count("Existing backlog card") == 1


@pytest.mark.asyncio
async def test_reconcile_caps_at_5_ops(tmp_path, monkeypatch):
    """reconcile_board applies at most 5 ops per turn."""
    _write_fixture_board(tmp_path)
    cwd = str(tmp_path)

    # 10 create ops
    many_ops = json.dumps([
        {"op": "create", "text": f"New card {i}", "column": "backlog"}
        for i in range(10)
    ])
    monkeypatch.setattr(bot_module, "_sdk_query", _make_haiku_mock(many_ops))
    await reconcile_board(
        cwd=cwd,
        name="test-project",
        user_msg="Create many cards",
        agent_summary="Done.",
    )

    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    # At most 5 "New card" entries should be created
    created_count = len(re.findall(r"New card \d+", content))
    assert created_count <= 5

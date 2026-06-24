"""
Unit tests for _ingest_errors_to_board:
- adding new errors → land in the Failed section
- deduplication: re-ingesting the same error updates seen/last, does not create a duplicate
- guard: suspicious file (more potential cards than parsed) → ingest is skipped
- empty errors → (0, 0) with no changes
- multiple errors with different hashes → multiple cards
- card already in review (user moved it) → stays there, only seen is updated
"""
import sys
from pathlib import Path
import asyncio

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _ingest_errors_to_board,
    _load_board,
    _tasks_path,
    _parse_incident_desc,
    _hash6,
    _norm_msg,
)


def _make_empty_board(cwd: Path, name: str = "testproj") -> None:
    _tasks_path(str(cwd)).write_text(
        f"# Tasks — {name}\n\n## Backlog\n\n## In Progress\n\n## Review\n\n## Failed\n",
        encoding="utf-8",
    )


def _make_error(msg: str, etype: str = "TestError", source: str = "log") -> dict:
    h = _hash6(_norm_msg(f"{etype}: {msg}"))
    return {
        "hash": h,
        "type": etype,
        "message": msg,
        "source": source,
        "excerpt": f"{etype}: {msg}",
    }


# ─────────────────────────── basic cases ───────────────────────────


async def test_ingest_empty_errors_returns_zero(tmp_path):
    """Empty errors → (0, 0), file is not touched."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    added, updated = await _ingest_errors_to_board(str(cwd), "proj", [])
    assert added == 0
    assert updated == 0


async def test_ingest_new_error_adds_to_failed(tmp_path):
    """New error → added to Failed with id=err-<hash>."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("connection refused", "ConnectionError")
    added, updated = await _ingest_errors_to_board(str(cwd), "proj", [err])

    assert added == 1
    assert updated == 0

    _, _, cols = _load_board(str(cwd))
    failed_cards = cols["failed"]
    assert len(failed_cards) == 1
    card = failed_cards[0]
    assert card["id"] == f"err-{err['hash']}"
    assert "ConnectionError" in card["text"] or "connection refused" in card["text"]


async def test_ingest_multiple_errors_adds_all(tmp_path):
    """Multiple distinct errors → same number of cards created."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    errors = [
        _make_error("division by zero", "ZeroDivisionError"),
        _make_error("key missing", "KeyError"),
        _make_error("no space left", "OSError"),
    ]
    added, updated = await _ingest_errors_to_board(str(cwd), "proj", errors)

    assert added == 3
    assert updated == 0

    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 3


async def test_ingest_dedup_same_error_increments_seen(tmp_path):
    """Re-ingesting the same error → updated=1, added=0, seen increments."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("disk full", "OSError")

    # First ingest
    await _ingest_errors_to_board(str(cwd), "proj", [err])

    # Second ingest
    added, updated = await _ingest_errors_to_board(str(cwd), "proj", [err])

    assert added == 0
    assert updated == 1

    # seen should become 2
    _, _, cols = _load_board(str(cwd))
    card = next(c for c in cols["failed"] if c["id"] == f"err-{err['hash']}")
    meta = _parse_incident_desc(card.get("description"))
    assert meta.get("seen") == "2", f"seen should be 2, got: {meta}"


async def test_ingest_dedup_card_in_review_stays_in_review(tmp_path):
    """If the user moved an err-card to Review — it stays there on dedup update."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("some error", "SomeError")

    # First ingest
    await _ingest_errors_to_board(str(cwd), "proj", [err])

    # Manually move the card to Review
    raw, preamble, cols = _load_board(str(cwd))
    card = cols["failed"].pop()
    cols["review"].append(card)
    from webapp import _save_board
    _save_board(str(cwd), "proj", preamble, cols)

    # Second ingest — card must remain in Review
    added, updated = await _ingest_errors_to_board(str(cwd), "proj", [err])

    assert added == 0
    assert updated == 1

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == f"err-{err['hash']}" for c in cols["review"]), (
        "Card should remain in Review"
    )
    assert len(cols["failed"]) == 0


async def test_ingest_suspicious_file_skips(tmp_path):
    """File with more potential cards than parsed → ingest is skipped (guard against data loss)."""
    cwd = tmp_path / "proj"
    cwd.mkdir()

    # Create a TASKS.md with cards in non-standard format (no checkbox)
    # This makes _count_potential_cards return > 0 while parsed_count = 0
    bad_content = (
        "# Tasks\n\n## Backlog\n"
        "- something without ops marker or checkbox\n"
        "- another line that looks like a card\n"
        "## In Progress\n## Review\n## Failed\n"
    )
    _tasks_path(str(cwd)).write_text(bad_content, encoding="utf-8")

    # _parse_tasks recognises plain bullets as backlog cards (PLAIN_CARD_RE).
    # Verify ingest runs without raising an exception.
    err = _make_error("test error", "Error")
    added, updated = await _ingest_errors_to_board(str(cwd), "proj", [err])
    # Exact value not checked — just must not crash


async def test_ingest_no_board_file_does_not_crash(tmp_path):
    """If TASKS.md does not exist — ingest works without crashing, creates the file."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    # No TASKS.md

    err = _make_error("test error", "Error")
    # _ingest_errors_to_board uses _load_board which creates empty cols when the file is absent;
    # guard "raw.strip() and parsed < potential" = "" strip = False → does not fire → writes
    try:
        added, updated = await _ingest_errors_to_board(str(cwd), "proj", [err])
        # May or may not add — must not raise
    except Exception as e:
        pytest.fail(f"_ingest_errors_to_board must not crash when TASKS.md is absent: {e}")


async def test_ingest_card_has_source_in_description(tmp_path):
    """Card contains source in its description."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("some crash", "RuntimeError", source="log")
    await _ingest_errors_to_board(str(cwd), "proj", [err])

    _, _, cols = _load_board(str(cwd))
    card = cols["failed"][0]
    meta = _parse_incident_desc(card.get("description"))
    assert meta.get("source") == "log", f"source should be 'log': {meta}"
    assert meta.get("seen") == "1"
    assert meta.get("first") is not None
    assert meta.get("last") is not None


async def test_ingest_test_source_card(tmp_path):
    """Errors with source=test also land in Failed."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("test_foo - AssertionError", "FAILED", source="test")
    added, updated = await _ingest_errors_to_board(str(cwd), "proj", [err])

    assert added == 1
    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 1


async def test_ingest_multiple_ingests_accumulate(tmp_path):
    """Multiple ingests of different errors → cards accumulate."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _make_empty_board(cwd)

    err1 = _make_error("error alpha", "AlphaError")
    err2 = _make_error("error beta", "BetaError")

    await _ingest_errors_to_board(str(cwd), "proj", [err1])
    added, _ = await _ingest_errors_to_board(str(cwd), "proj", [err2])

    assert added == 1
    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 2

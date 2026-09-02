"""Tests for tools/daily-journal.py — the Haiku-digest daily activity journal.

Every function under test is pure (given data in, text/objects out) or takes a
tmp_path fixture for filesystem-shaped behavior (the frontmatter guard, the
index upsert). No network access and no model call anywhere in this file —
that is exercised manually via `--dry-run` / a single verification run, never
in the automated suite.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "daily_journal", Path(__file__).resolve().parent.parent / "tools" / "daily-journal.py")
dj = importlib.util.module_from_spec(_SPEC)
# Register in sys.modules BEFORE exec: the module's @dataclass definitions need
# their module resolvable via sys.modules[cls.__module__] during class creation
# (same gotcha as tests/test_doctor.py's "doctor" module).
sys.modules["daily_journal"] = dj
_SPEC.loader.exec_module(dj)  # type: ignore[union-attr]


# ─────────────────────────────────────────────────────────────────────────────
# LA-day -> UTC bounds, including the two 2026 DST transitions
# ─────────────────────────────────────────────────────────────────────────────

def test_la_day_bounds_utc_ordinary_day_is_24h():
    start, end = dj.la_day_bounds_utc(date(2026, 6, 15), "America/Los_Angeles")
    assert start.tzinfo is not None and end.tzinfo is not None
    assert (end - start) == timedelta(hours=24)
    assert start == datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)  # PDT = UTC-7


def test_la_day_bounds_utc_spring_forward_is_23h():
    # 2026-03-08: America/Los_Angeles springs forward 2:00am -> 3:00am.
    start, end = dj.la_day_bounds_utc(date(2026, 3, 8), "America/Los_Angeles")
    assert (end - start) == timedelta(hours=23)
    assert start == datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)   # still PST (-8)
    assert end == datetime(2026, 3, 9, 7, 0, tzinfo=timezone.utc)     # now PDT (-7)


def test_la_day_bounds_utc_fall_back_is_25h():
    # 2026-11-01: America/Los_Angeles falls back 2:00am -> 1:00am.
    start, end = dj.la_day_bounds_utc(date(2026, 11, 1), "America/Los_Angeles")
    assert (end - start) == timedelta(hours=25)
    assert start == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)  # still PDT (-7)
    assert end == datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)    # now PST (-8)


def test_la_day_bounds_utc_matches_zoneinfo_ground_truth():
    """Cross-check against a from-scratch zoneinfo computation, not our own code."""
    tz = ZoneInfo("America/Los_Angeles")
    for day in (date(2026, 1, 15), date(2026, 3, 8), date(2026, 7, 4), date(2026, 11, 1)):
        expected_start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
        expected_end = (datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
        start, end = dj.la_day_bounds_utc(day, "America/Los_Angeles")
        assert start == expected_start
        assert end == expected_end


def test_compute_target_days_default_is_yesterday():
    yesterday = date(2026, 8, 31)
    assert dj.compute_target_days(None, None, yesterday) == [yesterday]


def test_compute_target_days_explicit_date_wins():
    yesterday = date(2026, 8, 31)
    assert dj.compute_target_days("2026-01-01", 7, yesterday) == [date(2026, 1, 1)]


def test_compute_target_days_backfill_n_ends_at_yesterday():
    yesterday = date(2026, 8, 31)
    got = dj.compute_target_days(None, 3, yesterday)
    assert got == [date(2026, 8, 31), date(2026, 8, 30), date(2026, 8, 29)]


# ─────────────────────────────────────────────────────────────────────────────
# Slugs — the two different schemes must never be conflated
# ─────────────────────────────────────────────────────────────────────────────

def test_sdk_session_slug_replaces_every_non_alnum():
    assert dj.sdk_session_slug("/home/youruser/line_vpn_bot") == "-home-youruser-line-vpn-bot"


def test_timeline_slug_replaces_only_slashes():
    assert dj.timeline_slug("/home/youruser/line_vpn_bot") == "-home-youruser-line_vpn_bot"


# ─────────────────────────────────────────────────────────────────────────────
# Transcript line filtering — a 6-line fixture covering every noise class
# ─────────────────────────────────────────────────────────────────────────────

def _iso(offset_min: int) -> str:
    base = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=offset_min)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


_SIX_LINE_FIXTURE = [
    # 1. Real operator prompt.
    {"type": "user", "timestamp": _iso(0),
     "message": {"content": "Deploy the new build"}},
    # 2. Real assistant reply to it.
    {"type": "assistant", "timestamp": _iso(1),
     "message": {"content": [{"type": "text", "text": "Deployed successfully, commit abc123"}]}},
    # 3. isMeta harness noise (e.g. an image-downscale notice) — never operator-typed.
    {"type": "user", "timestamp": _iso(2), "isMeta": True,
     "message": {"content": "[Image: original 828x8240, displayed at 201x2000.]"}},
    # 4. A tool_result content list — not a human reply.
    {"type": "user", "timestamp": _iso(3),
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file written"}]}},
    # 5. A user line that is PURELY an SDK service block — nothing human survives.
    {"type": "user", "timestamp": _iso(4),
     "message": {"content": "<task-notification>Sub-agent finished</task-notification>"}},
    # 6. A real follow-up with an embedded service block — the human text survives.
    {"type": "user", "timestamp": _iso(5),
     "message": {"content": "<system-reminder>ctx note</system-reminder>Now also update the docs"}},
]


def test_turns_from_lines_six_line_fixture():
    start_ms = dj.iso_to_epoch_ms(_iso(0))
    end_ms = dj.iso_to_epoch_ms(_iso(10))
    turns = dj.turns_from_lines(_SIX_LINE_FIXTURE, start_ms, end_ms)

    assert len(turns) == 2
    assert turns[0].prompt == "Deploy the new build"
    assert turns[0].reply == "Deployed successfully, commit abc123"
    assert turns[1].prompt == "Now also update the docs"
    assert turns[1].reply == ""  # nothing followed it in the window
    # oldest -> newest
    assert turns[0].ts_ms < turns[1].ts_ms


def test_turns_from_lines_respects_the_time_window():
    start_ms = dj.iso_to_epoch_ms(_iso(0))
    end_ms = dj.iso_to_epoch_ms(_iso(1))  # excludes line 6 (offset 5) and its reply source
    turns = dj.turns_from_lines(_SIX_LINE_FIXTURE, start_ms, end_ms)
    assert len(turns) == 1
    assert turns[0].prompt == "Deploy the new build"


@pytest.mark.parametrize("obj", [
    {"type": "user", "isMeta": True, "message": {"content": "[Image: ...]"}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
    {"type": "user", "message": {"content": "<task-notification>done</task-notification>"}},
    {"type": "user", "message": {"content": "[auto-continue] wake up"}},
    {"type": "user", "message": {"content": "<agent-message from=x>hi</agent-message>"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    {"type": "user", "message": {"content": None}},
])
def test_extract_operator_line_rejects_every_noise_class(obj):
    assert dj.extract_operator_line(obj) is None


def test_extract_operator_line_accepts_a_plain_string():
    obj = {"type": "user", "message": {"content": "hello there"}}
    assert dj.extract_operator_line(obj) == "hello there"


def test_extract_operator_line_accepts_text_only_block_list():
    obj = {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}
    assert dj.extract_operator_line(obj) == "hello"


def test_extract_operator_line_accepts_a_queued_command_steer():
    obj = {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "also do X"}}
    assert dj.extract_operator_line(obj) == "also do X"


def test_extract_assistant_text_joins_text_blocks_only():
    obj = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read"},
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two"},
    ]}}
    assert dj.extract_assistant_text(obj) == "part one\npart two"


# ─────────────────────────────────────────────────────────────────────────────
# Digest truncation — oldest rows dropped first, budget respected
# ─────────────────────────────────────────────────────────────────────────────

def test_truncate_blocks_keeps_everything_under_budget():
    blocks = ["a" * 10, "b" * 10, "c" * 10]
    kept, dropped = dj.truncate_blocks(blocks, 1000)
    assert kept == blocks
    assert dropped == 0


def test_truncate_blocks_drops_oldest_first():
    blocks = ["OLDEST", "MIDDLE", "NEWEST"]  # oldest -> newest, as the caller must order them
    # Budget only large enough for the last block plus a bit of slack.
    kept, dropped = dj.truncate_blocks(blocks, len("NEWEST") + 2)
    assert kept == ["NEWEST"]
    assert dropped == 2


def test_truncate_blocks_empty_input():
    kept, dropped = dj.truncate_blocks([], 100)
    assert kept == []
    assert dropped == 0


def test_truncate_blocks_zero_budget_drops_everything():
    kept, dropped = dj.truncate_blocks(["x", "y"], 0)
    assert kept == []
    assert dropped == 2


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter marker guard — never overwrite a hand-written vault note
# ─────────────────────────────────────────────────────────────────────────────

def test_is_generated_note_true_for_our_frontmatter():
    text = (
        "---\n"
        "date: 2026-08-30\n"
        "generated: cardloop-daily-journal\n"
        "source: cardloop\n"
        "---\n"
        "# 2026-08-30 (Sunday)\n"
    )
    assert dj.is_generated_note(text) is True


def test_is_generated_note_false_for_hand_written_note():
    text = "# 2026-08-30\n\n## 18:04\nLadno, delai final cut.\n"
    assert dj.is_generated_note(text) is False


def test_is_generated_note_not_fooled_by_prose_mention():
    # The marker string appears in the BODY, not in a leading frontmatter block.
    text = (
        "# Some note\n\n"
        "I wonder if `generated: cardloop-daily-journal` would even work here.\n"
    )
    assert dj.is_generated_note(text) is False


def test_is_generated_note_false_for_empty_text():
    assert dj.is_generated_note("") is False


def test_choose_note_path_no_existing_file_uses_primary(tmp_path: Path):
    path, used_fallback = dj.choose_note_path(tmp_path, date(2026, 8, 30))
    assert path == tmp_path / "2026-08-30.md"
    assert used_fallback is False


def test_choose_note_path_regenerating_our_own_note_uses_primary(tmp_path: Path):
    primary = tmp_path / "2026-08-30.md"
    primary.write_text(
        "---\ndate: 2026-08-30\ngenerated: cardloop-daily-journal\nsource: cardloop\n---\n# old\n",
        encoding="utf-8",
    )
    path, used_fallback = dj.choose_note_path(tmp_path, date(2026, 8, 30))
    assert path == primary
    assert used_fallback is False


def test_choose_note_path_never_overwrites_a_hand_written_note(tmp_path: Path):
    primary = tmp_path / "2026-08-30.md"
    primary.write_text("# 2026-08-30\n\n## 18:04\nHand-written note.\n", encoding="utf-8")
    original = primary.read_text(encoding="utf-8")

    path, used_fallback = dj.choose_note_path(tmp_path, date(2026, 8, 30))

    assert used_fallback is True
    assert path == tmp_path / "2026-08-30-cardloop.md"
    assert path != primary
    # The hand-written file itself must be untouched by merely choosing a path.
    assert primary.read_text(encoding="utf-8") == original


# ─────────────────────────────────────────────────────────────────────────────
# Index line insertion — newest-first, idempotent
# ─────────────────────────────────────────────────────────────────────────────

def test_upsert_index_creates_header_on_first_write():
    line = dj.render_index_line(date(2026, 8, 30), "busiest: cardloop", 5, 2)
    out = dj.upsert_index("", date(2026, 8, 30), line)
    assert line in out
    assert out.index("# Journal index") < out.index(line)


def test_upsert_index_is_newest_first():
    text = ""
    line_a = dj.render_index_line(date(2026, 8, 28), "day A", 1, 1)
    line_b = dj.render_index_line(date(2026, 8, 29), "day B", 2, 2)
    line_c = dj.render_index_line(date(2026, 8, 30), "day C", 3, 3)
    text = dj.upsert_index(text, date(2026, 8, 28), line_a)
    text = dj.upsert_index(text, date(2026, 8, 29), line_b)
    text = dj.upsert_index(text, date(2026, 8, 30), line_c)
    assert text.index(line_c) < text.index(line_b) < text.index(line_a)


def test_upsert_index_is_idempotent():
    line = dj.render_index_line(date(2026, 8, 30), "busiest: cardloop", 5, 2)
    once = dj.upsert_index("", date(2026, 8, 30), line)
    twice = dj.upsert_index(once, date(2026, 8, 30), line)
    assert once == twice
    assert twice.count("[[2026-08-30]]") == 1


def test_upsert_index_regeneration_replaces_not_duplicates():
    old_line = dj.render_index_line(date(2026, 8, 30), "old summary", 1, 1)
    new_line = dj.render_index_line(date(2026, 8, 30), "new summary", 9, 4)
    text = dj.upsert_index("", date(2026, 8, 30), old_line)
    text = dj.upsert_index(text, date(2026, 8, 30), new_line)
    assert text.count("[[2026-08-30]]") == 1
    assert old_line not in text
    assert new_line in text


def test_upsert_index_preserves_a_custom_header():
    text = dj.upsert_index("# My Journal\n\nCustom preamble.\n",
                            date(2026, 8, 30), "- [[2026-08-30]] — x · 1 sessions · 1 projects")
    assert text.startswith("# My Journal\n\nCustom preamble.\n")
    assert "- [[2026-08-30]]" in text


# ─────────────────────────────────────────────────────────────────────────────
# compute_numbers / build_index_summary — deterministic aggregation, no model
# ─────────────────────────────────────────────────────────────────────────────

def _project_day(label, turns=None, commits=None):
    return dj.ProjectDay(
        project_id=label, label=label, cwd=f"/home/youruser/{label}", kind="project",
        turns=turns or [], commits=commits or [], session_count=1 if turns else 0,
    )


def test_compute_numbers_empty_day():
    g = {"projects": {}, "total_ledger": dj.LedgerStats()}
    numbers = dj.compute_numbers(g, "America/Los_Angeles")
    assert numbers.sessions == 0
    assert numbers.turns == 0
    assert numbers.active_minutes == 0
    assert numbers.first_activity == ""
    assert numbers.last_activity == ""
    assert numbers.commits == 0
    assert numbers.projects_count == 0


def test_compute_numbers_aggregates_turns_and_commits():
    t1 = dj.Turn(ts_ms=dj.iso_to_epoch_ms(_iso(0)), prompt="a")
    t2 = dj.Turn(ts_ms=dj.iso_to_epoch_ms(_iso(30)), prompt="b")
    pd1 = _project_day("alpha", turns=[t1], commits=[{"hash": "abc123", "time": "10:00", "subject": "x"}])
    pd2 = _project_day("beta", turns=[t2])
    g = {"projects": {"alpha": pd1, "beta": pd2}, "total_ledger": dj.LedgerStats()}
    numbers = dj.compute_numbers(g, "America/Los_Angeles")
    assert numbers.turns == 2
    assert numbers.sessions == 2
    assert numbers.commits == 1
    assert numbers.projects_count == 2
    assert numbers.first_activity != ""
    assert numbers.last_activity != ""


def test_build_index_summary_no_activity():
    assert dj.build_index_summary({}) == "no recorded activity"


def test_build_index_summary_names_busiest_project():
    pd1 = _project_day("alpha", turns=[dj.Turn(ts_ms=1, prompt="a")] * 5)
    pd2 = _project_day("beta", turns=[dj.Turn(ts_ms=1, prompt="b")])
    summary = dj.build_index_summary({"alpha": pd1, "beta": pd2})
    assert "alpha" in summary

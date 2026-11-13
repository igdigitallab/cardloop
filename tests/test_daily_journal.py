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


# ─────────────────────────────────────────────────────────────────────────────
# Busy days keep their morning; a failed model call still yields a note
# ─────────────────────────────────────────────────────────────────────────────

def test_render_digest_keeps_every_turn_of_a_busy_day():
    # 160 turns at the per-row clip ceiling — a real busy day (2026-09-16 had
    # 158). The old 40k budget dropped the oldest 128 of them.
    base = dj.iso_to_epoch_ms(_iso(0))
    turns = [
        dj.Turn(ts_ms=base + i * 60_000, prompt=f"P{i:03d}" + "p" * (dj.PROMPT_SNIPPET_CHARS - 4),
                reply="r" * dj.REPLY_SNIPPET_CHARS)
        for i in range(160)
    ]
    g = {"day": date(2026, 9, 16), "projects": {"alpha": _project_day("alpha", turns=turns)},
         "total_ledger": dj.LedgerStats(), "vault_specs_touched": []}
    digest, _ = dj.render_digest(g, "America/Los_Angeles")
    assert "omitted" not in digest
    assert "P000" in digest and "P159" in digest


def _empty_day(day):
    return {"day": day, "projects": {}, "total_ledger": dj.LedgerStats(),
            "vault_specs_touched": []}


def test_model_failure_still_writes_a_note_and_flags_the_ping(tmp_path, monkeypatch):
    day = date(2026, 9, 12)
    monkeypatch.setattr(dj, "gather_day", lambda d, tz, data_dir: _empty_day(d))

    def boom(*a, **k):
        raise RuntimeError("Not logged in · Please run /login")
    monkeypatch.setattr(dj, "call_model", boom)
    pings = []
    monkeypatch.setattr(dj, "notify_telegram",
                        lambda d, n, body, path, root, warning="": pings.append(warning))

    args = dj.build_arg_parser().parse_args(["--notify-telegram"])
    ok = dj._process_one_day(day, "America/Los_Angeles", tmp_path, tmp_path,
                             tmp_path / "_index.md", args, "en")

    assert ok is False  # the cron's exit code must stay red
    assert (tmp_path / "2026-09-12.md").exists()
    assert len(pings) == 1 and "Not logged in" in pings[0]


def test_model_success_reports_ok_with_no_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(dj, "gather_day", lambda d, tz, data_dir: _empty_day(d))
    monkeypatch.setattr(dj, "call_model", lambda *a, **k: "## Day\n- did things")
    pings = []
    monkeypatch.setattr(dj, "notify_telegram",
                        lambda d, n, body, path, root, warning="": pings.append(warning))

    args = dj.build_arg_parser().parse_args(["--notify-telegram"])
    assert dj._process_one_day(date(2026, 9, 14), "America/Los_Angeles", tmp_path, tmp_path,
                               tmp_path / "_index.md", args, "en") is True
    assert pings == [""]


def test_telegram_message_carries_the_warning_escaped():
    numbers = dj.compute_numbers({"projects": {}, "total_ledger": dj.LedgerStats()},
                                 "America/Los_Angeles")
    msg = dj.build_telegram_message(date(2026, 9, 12), numbers, "", Path("/tmp/x.md"), "",
                                    warning="model call failed (<login>)")
    assert "model call failed (&lt;login&gt;)" in msg
    assert "⚠" in dj.build_telegram_message(date(2026, 9, 12), numbers, "",
                                                 Path("/tmp/x.md"), "", warning="w")
    assert "⚠" not in dj.build_telegram_message(date(2026, 9, 12), numbers, "",
                                                     Path("/tmp/x.md"), "")


_NOTE = """## Day at a glance
- **alpha**: shipped the thing
- beta: fixed a bug

## Numbers
| sessions | 3 |

## Open threads
- **alpha**: deploy still pending
"""


def test_extract_open_threads_reads_the_last_section():
    assert dj.extract_open_threads(_NOTE) == ["**alpha**: deploy still pending"]


def test_extract_open_threads_empty_when_numbers_is_last():
    note = _NOTE.split("## Open threads")[0]
    assert dj.extract_open_threads(note) == []
    assert dj.extract_glance(note) == ["**alpha**: shipped the thing", "beta: fixed a bug"]


def test_telegram_message_has_done_and_open():
    numbers = dj.compute_numbers({"projects": {}, "total_ledger": dj.LedgerStats()},
                                 "America/Los_Angeles")
    msg = dj.build_telegram_message(date(2026, 9, 17), numbers, _NOTE, Path("/tmp/x.md"), "")
    assert "<b>alpha</b>: shipped the thing" in msg
    assert "Open</b>\n• <b>alpha</b>: deploy still pending" in msg


def test_telegram_message_over_limit_drops_whole_bullets_not_tags():
    numbers = dj.compute_numbers({"projects": {}, "total_ledger": dj.LedgerStats()},
                                 "America/Los_Angeles")
    long = "## Glance\n" + "".join(f"- **p{i}**: {'x' * 700}\n" for i in range(6)) \
        + "\n## Open\n" + "".join(f"- **o{i}**: {'y' * 700}\n" for i in range(6))
    msg = dj.build_telegram_message(date(2026, 9, 17), numbers, long, Path("/tmp/x.md"), "")
    assert len(msg) <= dj.TG_TEXT_LIMIT
    assert msg.count("<b>") == msg.count("</b>")
    assert "<b>p0</b>" in msg  # the day's headline survives; open threads go first


def test_load_projects_skips_private_topics_and_blocks_registry_readd(tmp_path):
    """A topic marked "private": true never reaches the digest — not via topics.json,
    and not via a registry alias that resolves to the same cwd."""
    import json
    home = Path.home()
    (tmp_path / "topics.json").write_text(json.dumps({
        "shop": {"cwd": str(home / "projects" / "shop"), "project": "Shop"},
        "psy": {"cwd": str(home / "projects" / "psy"), "project": "Psy", "private": True},
    }), encoding="utf-8")
    (tmp_path / "registry.json").write_text(json.dumps({
        "psy-alias": "projects/psy",
        "other": "projects/other",
    }), encoding="utf-8")
    got = dj.load_projects(tmp_path)
    assert "shop" in got
    assert "psy" not in got
    assert "psy-alias" not in got
    assert all(p.cwd != str(home / "projects" / "psy") for p in got.values())
    assert "other" in got


# ─────────────────────────────────────────────────────────────────────────────
# Operator notes (optional input): `<journal dir>/YYYY-MM-DD<suffix>.md`, read-only
# ─────────────────────────────────────────────────────────────────────────────

_TZ = "America/Los_Angeles"
_NOTES_DAY = date(2026, 9, 20)

# Out of order on purpose; hidden comments after the text, on their own line, and before a
# continuation; frontmatter on top.
_NOTES_FILE = """---
source: records
date: 2026-09-20
---
- 14:05 · telegram · call the plumber about the leak <!-- rec:0003 -->
- 09:30 · voice · idea: ship the notes block <!-- rec:0001 -->
<!-- rec:0002 -->
- 09:30 · web · second thought, same minute <!-- rec:0002 -->
  a continuation line of that thought
- 07:45 · web · AT&T <bill> is due **today**
"""

_NOTES_SORTED = [
    "- 07:45 · web · AT&T <bill> is due **today**",
    "- 09:30 · voice · idea: ship the notes block",
    "- 09:30 · web · second thought, same minute",
    "  a continuation line of that thought",
    "- 14:05 · telegram · call the plumber about the leak",
]


@pytest.fixture(autouse=True)
def _notes_env_is_clean(monkeypatch):
    monkeypatch.delenv("JOURNAL_NOTES_SUFFIX", raising=False)


def _numbers():
    return dj.compute_numbers({"projects": {}, "total_ledger": dj.LedgerStats()}, _TZ)


def _flat(notes):
    return [ln for n in notes for ln in n.lines]


def _write_notes(directory: Path, text: str = _NOTES_FILE, suffix: str = "-notes",
                 day: date = _NOTES_DAY) -> Path:
    path = directory / f"{day.isoformat()}{suffix}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _run_day(tmp_path, monkeypatch, argv=(), body="## Day at a glance\n- **alpha**: did things",
             day=_NOTES_DAY):
    """One full `_process_one_day` with the model stubbed. Returns (ok, prompts, pings)."""
    monkeypatch.setattr(dj, "gather_day", lambda d, tz, data_dir: _empty_day(d))
    prompts, pings = [], []

    def fake_model(prompt, model, timeout_s=0):
        prompts.append(prompt)
        return body
    monkeypatch.setattr(dj, "call_model", fake_model)

    def fake_notify(d, n, b, path, root, warning="", **extra):
        pings.append({"body": b, "warning": warning, **extra})
    monkeypatch.setattr(dj, "notify_telegram", fake_notify)
    args = dj.build_arg_parser().parse_args(["--notify-telegram", *argv])
    ok = dj._process_one_day(day, _TZ, tmp_path, tmp_path, tmp_path / "_index.md", args, "en")
    return ok, prompts, pings


def _stable(note_text: str) -> str:
    """The journal file with its one wall-clock field (generated_at) blanked."""
    import re
    return re.sub(r"^generated_at: .*$", "generated_at: X", note_text, flags=re.MULTILINE)


def test_parse_day_notes_strips_header_and_comments_and_sorts_by_time():
    notes = dj.parse_day_notes(_NOTES_FILE)
    assert _flat(notes) == _NOTES_SORTED  # verbatim words, time order, continuation kept with its note
    joined = "\n".join(_flat(notes))
    assert "source: records" not in joined and "<!--" not in joined and "rec:000" not in joined


def test_parse_day_notes_edge_cases():
    # CRLF, BOM, no frontmatter, an invalid time (keeps its predecessor's slot), blank lines.
    text = "\ufeff- 10:10 · web · b\r\n\r\n- 25:99 · web · not a time\r\n- 08:00 · web · a\r\n"
    assert _flat(dj.parse_day_notes(text)) == [
        "- 08:00 · web · a", "- 10:10 · web · b", "- 25:99 · web · not a time"]
    # a multi-line hidden comment disappears whole
    assert _flat(dj.parse_day_notes("- 09:00 · x · keep <!-- a\nb --> me")) == ["- 09:00 · x · keep me"]
    # a file that is only a header (or only comments) holds no notes
    assert dj.parse_day_notes("---\nsource: records\n---\n") == []
    assert dj.parse_day_notes("<!-- nothing -->\n\n") == []


def test_resolve_notes_suffix_flag_env_default(monkeypatch):
    assert dj.resolve_notes_suffix(None) == "-notes"
    monkeypatch.setenv("JOURNAL_NOTES_SUFFIX", "-mine")
    assert dj.resolve_notes_suffix(None) == "-mine"
    assert dj.resolve_notes_suffix("-flag") == "-flag"  # the flag beats the environment
    assert dj.resolve_notes_suffix("") == ""            # an explicit empty flag switches notes off


def test_read_day_notes_no_file_empty_file_and_unusable_suffixes(tmp_path):
    assert dj.read_day_notes(tmp_path, _NOTES_DAY, "-notes") == []          # no file
    _write_notes(tmp_path, "")
    assert dj.read_day_notes(tmp_path, _NOTES_DAY, "-notes") == []          # empty file
    _write_notes(tmp_path, "---\nsource: records\n---\n")
    assert dj.read_day_notes(tmp_path, _NOTES_DAY, "-notes") == []          # header only
    _write_notes(tmp_path)
    assert dj.read_day_notes(tmp_path, _NOTES_DAY, "") == []                # disabled
    assert dj.read_day_notes(tmp_path, _NOTES_DAY, "/../x") == []           # never leaves the dir
    assert dj.notes_file_path(tmp_path, _NOTES_DAY, "") is None
    # the journal's own output is never read back as notes (a suffix pointed at it)
    generated = tmp_path / "2026-09-21.md"
    generated.write_text("---\ngenerated: cardloop-daily-journal\n---\n- 09:00 · x · y\n", encoding="utf-8")
    assert dj.read_day_notes(tmp_path, date(2026, 9, 21), "") == []


def test_read_day_notes_uses_the_suffix_it_is_given(tmp_path):
    _write_notes(tmp_path, "- 09:00 · web · default file\n")
    _write_notes(tmp_path, "- 10:00 · web · custom file\n", suffix=".mine")
    assert _flat(dj.read_day_notes(tmp_path, _NOTES_DAY, "-notes")) == ["- 09:00 · web · default file"]
    assert _flat(dj.read_day_notes(tmp_path, _NOTES_DAY, ".mine")) == ["- 10:00 · web · custom file"]


def test_day_without_notes_is_byte_for_byte_the_old_output(tmp_path, monkeypatch):
    body = "## Day at a glance\n- **alpha**: did things\n\n## Open threads\n- **alpha**: deploy pending"
    ok, prompts, pings = _run_day(tmp_path, monkeypatch, body=body)
    assert ok is True
    written = (tmp_path / f"{_NOTES_DAY.isoformat()}.md").read_text(encoding="utf-8")
    numbers = _numbers()
    # exactly frontmatter + title + the model body — no section, no heading, no extra blank line
    assert _stable(written) == _stable(dj.compose_note(_NOTES_DAY, numbers, body))
    assert "\n\n# 2026-09-20 (Sunday)\n\n## Day at a glance\n" in written
    # the model got exactly the prompt it always got
    digest, _ = dj.render_digest(_empty_day(_NOTES_DAY), _TZ)
    assert prompts == [dj.NOTE_INSTRUCTIONS.format(lang="en") + digest]
    assert "OPERATOR NOTES" not in prompts[0] and "NOTES_HEADING" not in prompts[0]
    # the ping is called the old way, with no notes arguments
    assert pings == [{"body": body, "warning": ""}]
    # and the index line has no notes marker
    assert "notes" not in (tmp_path / "_index.md").read_text(encoding="utf-8")


def test_empty_notes_file_changes_nothing_at_all(tmp_path, monkeypatch):
    plain, empty = tmp_path / "plain", tmp_path / "empty"
    plain.mkdir()
    empty.mkdir()
    _write_notes(empty, "---\nsource: records\n---\n")
    _, prompts_a, pings_a = _run_day(plain, monkeypatch)
    _, prompts_b, pings_b = _run_day(empty, monkeypatch)
    name = f"{_NOTES_DAY.isoformat()}.md"
    assert _stable((plain / name).read_text(encoding="utf-8")) == _stable((empty / name).read_text(encoding="utf-8"))
    assert (plain / "_index.md").read_text(encoding="utf-8") == (empty / "_index.md").read_text(encoding="utf-8")
    assert prompts_a == prompts_b and pings_a == pings_b


def test_digest_and_message_are_unchanged_without_notes():
    g = _empty_day(_NOTES_DAY)
    assert dj.render_digest(g, _TZ)[0] == dj.render_digest(g, _TZ, [])[0] == dj.render_digest(g, _TZ, None)[0]
    plain = dj.build_telegram_message(_NOTES_DAY, _numbers(), _NOTE, Path("/tmp/x.md"), "")
    assert plain == dj.build_telegram_message(_NOTES_DAY, _numbers(), _NOTE, Path("/tmp/x.md"), "", "", [], "")
    assert "\U0001F4DD" not in plain
    assert dj.build_index_summary({}) == "no recorded activity"
    assert dj.build_index_summary({}, 0) == "no recorded activity"


def test_day_with_notes_puts_a_verbatim_section_above_the_generated_chronology(tmp_path, monkeypatch):
    _write_notes(tmp_path)
    body = "## Day at a glance\n- **alpha**: did things\n\n## Timeline\n| a | b |"
    ok, prompts, pings = _run_day(tmp_path, monkeypatch, body=body)
    assert ok is True
    written = (tmp_path / f"{_NOTES_DAY.isoformat()}.md").read_text(encoding="utf-8")
    section = "## Operator notes\n\n" + "\n".join(_NOTES_SORTED)
    assert section in written
    # title, then the notes, then the model's sections
    assert written.index("# 2026-09-20") < written.index("## Operator notes") \
        < written.index("## Day at a glance") < written.index("## Timeline")
    assert written.rstrip().endswith("| a | b |")  # the generated body is untouched and last
    assert "<!--" not in written and "rec:000" not in written
    assert "source: records" not in written  # the notes file's header is not carried over
    # the model sees them only as context, with the rules for the heading line
    assert "OPERATOR NOTES (4" in prompts[0] and "ship the notes block" in prompts[0]
    assert dj.NOTES_ADDENDUM.format(lang="en") in prompts[0]
    # the index line counts notes (the continuation line belongs to its note: 5 lines, 4 notes)
    assert "notes: 4" in (tmp_path / "_index.md").read_text(encoding="utf-8")
    # the ping carries the notes and the heading
    assert len(pings) == 1 and len(pings[0]["notes"]) == 4 and pings[0]["notes_heading"] == "Operator notes"


def test_model_localizes_the_heading_but_never_touches_the_notes(tmp_path, monkeypatch):
    _write_notes(tmp_path)
    body = "NOTES_HEADING: **Mes notes**\n\n## Day at a glance\n- fine"
    _, _, pings = _run_day(tmp_path, monkeypatch, body=body)
    written = (tmp_path / f"{_NOTES_DAY.isoformat()}.md").read_text(encoding="utf-8")
    assert "## Mes notes\n\n" + "\n".join(_NOTES_SORTED) in written
    assert "NOTES_HEADING" not in written and "Operator notes" not in written
    assert pings[0]["notes_heading"] == "Mes notes"
    assert "NOTES_HEADING" not in pings[0]["body"]
    # a model that ignores the instruction leaves the English default and an untouched body
    assert dj.split_notes_heading("## Day\n- x") == ("", "## Day\n- x")
    assert dj.split_notes_heading("NOTES_HEADING:\n\n## Day") == ("", "## Day")


def test_notes_survive_a_model_failure_and_no_model(tmp_path, monkeypatch):
    _write_notes(tmp_path)
    monkeypatch.setattr(dj, "gather_day", lambda d, tz, data_dir: _empty_day(d))

    def boom(*a, **k):
        raise RuntimeError("Not logged in")
    monkeypatch.setattr(dj, "call_model", boom)
    pings = []
    monkeypatch.setattr(dj, "notify_telegram", lambda d, n, b, p, r, warning="", **extra: pings.append(extra))
    args = dj.build_arg_parser().parse_args(["--notify-telegram"])
    assert dj._process_one_day(_NOTES_DAY, _TZ, tmp_path, tmp_path, tmp_path / "_index.md", args, "en") is False
    written = (tmp_path / f"{_NOTES_DAY.isoformat()}.md").read_text(encoding="utf-8")
    assert "## Operator notes\n\n" + "\n".join(_NOTES_SORTED) in written
    assert len(pings) == 1 and pings[0]["notes_heading"] == "Operator notes"

    args = dj.build_arg_parser().parse_args(["--no-model"])
    dj._process_one_day(_NOTES_DAY, _TZ, tmp_path, tmp_path, tmp_path / "_index.md", args, "en")
    assert "## Operator notes" in (tmp_path / f"{_NOTES_DAY.isoformat()}.md").read_text(encoding="utf-8")


def test_regenerating_a_day_picks_the_notes_up_again_and_is_idempotent(tmp_path, monkeypatch):
    name = f"{_NOTES_DAY.isoformat()}.md"
    _run_day(tmp_path, monkeypatch)                       # first run: no notes yet
    assert "Operator notes" not in (tmp_path / name).read_text(encoding="utf-8")
    _write_notes(tmp_path)                                # the notes arrive later
    _run_day(tmp_path, monkeypatch)                       # `--date` regeneration
    first = _stable((tmp_path / name).read_text(encoding="utf-8"))
    assert "## Operator notes" in first
    _run_day(tmp_path, monkeypatch)
    assert _stable((tmp_path / name).read_text(encoding="utf-8")) == first
    assert (tmp_path / "_index.md").read_text(encoding="utf-8").count("[[2026-09-20]]") == 1


def test_notes_suffix_flag_and_env_choose_the_file(tmp_path, monkeypatch):
    _write_notes(tmp_path, "- 09:00 · web · from the default file\n")
    _write_notes(tmp_path, "- 10:00 · web · from the flag file\n", suffix="-flag")
    _write_notes(tmp_path, "- 11:00 · web · from the env file\n", suffix="-env")
    name = tmp_path / f"{_NOTES_DAY.isoformat()}.md"

    _run_day(tmp_path, monkeypatch, argv=["--notes-suffix=-flag"])
    assert "from the flag file" in name.read_text(encoding="utf-8")
    assert "default file" not in name.read_text(encoding="utf-8")

    monkeypatch.setenv("JOURNAL_NOTES_SUFFIX", "-env")
    _run_day(tmp_path, monkeypatch)
    assert "from the env file" in name.read_text(encoding="utf-8")

    _run_day(tmp_path, monkeypatch, argv=["--notes-suffix="])   # switched off
    assert "Operator notes" not in name.read_text(encoding="utf-8")


def test_the_notes_file_is_never_modified(tmp_path, monkeypatch):
    path = _write_notes(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    _run_day(tmp_path, monkeypatch)
    _run_day(tmp_path, monkeypatch, argv=["--no-model"])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-09-20-notes.md", "2026-09-20.md", "_index.md"]  # no stray temp or copy files


def test_dry_run_prints_the_section_and_writes_nothing(tmp_path, monkeypatch, capsys):
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    path = _write_notes(vault)
    before = path.read_bytes()
    monkeypatch.setattr(dj, "gather_day", lambda d, tz, data_dir: _empty_day(d))
    rc = dj.main(["--date", "2026-09-20", "--dry-run", "--no-model", "--tz", _TZ,
                  "--data-dir", str(data), "--vault-dir", str(vault)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Operator notes" in out and "- 14:05 · telegram · call the plumber about the leak" in out
    assert "OPERATOR NOTES (4" in out  # the digest carries them to the model as context too
    assert sorted(p.name for p in vault.iterdir()) == ["2026-09-20-notes.md"]
    assert path.read_bytes() == before


def test_index_summary_counts_notes():
    pd = _project_day("alpha", turns=[dj.Turn(ts_ms=1, prompt="a")], commits=[{"hash": "h"}])
    assert dj.build_index_summary({"alpha": pd}, 3) == "busiest: alpha, 1 commit, notes: 3"
    assert dj.build_index_summary({"alpha": pd}, 0) == "busiest: alpha, 1 commit"
    assert dj.build_index_summary({}, 2) == "no recorded activity, notes: 2"


def test_digest_notes_are_bounded_for_the_model(monkeypatch):
    monkeypatch.setattr(dj, "NOTES_DIGEST_CHAR_BUDGET", 400)
    notes = dj.parse_day_notes("".join(f"- 10:{i:02d} · web · {'n' * 90}\n" for i in range(30)))
    digest, _ = dj.render_digest(_empty_day(_NOTES_DAY), _TZ, notes)
    assert "OPERATOR NOTES (30" in digest and "more notes omitted" in digest
    assert "10:00" in digest and "10:29" not in digest  # the earliest win


def test_telegram_notes_block_comes_first_and_is_escaped_not_bolded():
    notes = dj.parse_day_notes(_NOTES_FILE)
    msg = dj.build_telegram_message(_NOTES_DAY, _numbers(), _NOTE, Path("/tmp/x.md"), "", "",
                                    notes, "Mes notes")
    assert "\U0001F4DD <b>Mes notes</b>\n• 07:45 · web · AT&amp;T &lt;bill&gt; is due **today**\n" in msg
    # the continuation line is folded into its note; list markers and hidden ids are gone
    assert "• 09:30 · web · second thought, same minute a continuation line of that thought" in msg
    assert "rec:000" not in msg and "- 09:30" not in msg
    assert msg.index("Mes notes") < msg.index("shipped the thing") < msg.index("Open</b>")
    assert "+" not in msg.split("Mes notes", 1)[1].split("\n\n", 1)[0]  # nothing hidden here
    assert msg.count("<b>") == msg.count("</b>")


def test_telegram_notes_over_the_cap_say_how_many_more():
    notes = dj.parse_day_notes("".join(f"- 10:{i:02d} · web · note {i}\n" for i in range(14)))
    msg = dj.build_telegram_message(_NOTES_DAY, _numbers(), _NOTE, Path("/tmp/x.md"), "", "", notes)
    block = msg.split("\n\n")[1]
    assert block.startswith("\U0001F4DD <b>Operator notes</b>\n")
    assert block.count("•") == dj.TG_NOTES_MAX_ITEMS
    assert block.endswith("… +4 more")
    assert "note 9" in block and "note 10" not in block


def test_telegram_notes_respect_the_character_budget_and_the_message_limit():
    notes = dj.parse_day_notes("".join(f"- 10:{i:02d} · web · {'w' * 600}\n" for i in range(20)))
    rows, hidden = dj._tg_notes_rows(notes)
    assert all(len(r) <= dj.TG_NOTE_ITEM_CHARS for r in rows) and rows[0].endswith("…")
    assert sum(len(r) for r in rows) <= dj.TG_NOTES_CHAR_BUDGET and hidden == 20 - len(rows) > 0
    big = "## Glance\n" + "".join(f"- **p{i}**: {'x' * 700}\n" for i in range(6)) \
        + "\n## Open\n" + "".join(f"- **o{i}**: {'y' * 700}\n" for i in range(6))
    msg = dj.build_telegram_message(_NOTES_DAY, _numbers(), big, Path("/tmp/x.md"), "", "w" * 150, notes)
    assert len(msg) <= dj.TG_TEXT_LIMIT and msg.count("<b>") == msg.count("</b>")
    assert "Operator notes" in msg and "<b>p0</b>" in msg

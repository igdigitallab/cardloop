"""spec-052 Phases 5-6: card↔spec link round-trip + generated ## Tasks mirror
+ auto-close detection."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from board import _parse_tasks, _serialize_tasks, _done_archive_line
from spec_mirror import sync_spec_mirror, _MIRROR_BEGIN, _MIRROR_END, _stamp_status


# ─── board.py: spec= round-trip ──────────────────────────────────────────────

def test_card_spec_roundtrips_through_parse_serialize():
    md = ("# Tasks\n\n## Backlog\n"
          "- [ ] Linked card <!--ops:abc123 spec=052-->\n"
          "- [ ] Model+spec <!--ops:def456 model=haiku spec=049-->\n"
          "- [ ] Plain card <!--ops:no0spec-->\n\n## In Progress\n\n## Review\n\n## Failed\n")
    pre, cols = _parse_tasks(md)
    by_id = {c["id"]: c for c in cols["backlog"]}
    assert by_id["abc123"]["spec"] == "052"
    assert by_id["def456"]["spec"] == "049"
    assert by_id["def456"]["model"] == "haiku"
    assert "spec" not in by_id["no0spec"]
    out = _serialize_tasks(pre, cols, "proj")
    assert "spec=052" in out and "spec=049" in out and "model=haiku" in out


def test_invalid_spec_value_is_dropped():
    md = "## Backlog\n- [ ] Bad spec <!--ops:abc123 spec=BAD!!-->\n"
    _, cols = _parse_tasks(md)
    assert "spec" not in cols["backlog"][0]


# ─── spec_mirror: mirror generation ──────────────────────────────────────────

def _setup(tmp_path: Path, board_md: str, spec_body: str = "# spec-052 — Test\n\nIntro.\n"):
    (tmp_path / "TASKS.md").write_text(board_md, encoding="utf-8")
    specs = tmp_path / "docs" / "internal" / "specs"
    specs.mkdir(parents=True)
    (specs / "spec-052-test.md").write_text(spec_body, encoding="utf-8")
    return str(tmp_path)


def test_mirror_creates_tasks_section_with_status_glyphs(tmp_path):
    board = ("## Backlog\n- [ ] Backlog one <!--ops:aaa111 spec=052-->\n\n"
             "## In Progress\n- [~] Active <!--ops:bbb222 spec=052-->\n\n"
             "## Review\n- [?] Reviewing <!--ops:ccc333 spec=052-->\n\n## Failed\n")
    cwd = _setup(tmp_path, board)
    res = sync_spec_mirror(cwd, "052")
    assert res is not None
    assert res["total"] == 3 and res["done"] == 1  # review counts as ✓ (done glyph)
    assert res["all_done"] is False
    text = (tmp_path / "docs/internal/specs/spec-052-test.md").read_text()
    assert _MIRROR_BEGIN in text and _MIRROR_END in text
    assert "[○] [aaa111]" in text  # backlog
    assert "[◐] [bbb222]" in text  # in_progress
    assert "[✓] [ccc333]" in text  # review


def test_mirror_idempotent_on_second_run(tmp_path):
    board = "## Backlog\n- [ ] Only <!--ops:aaa111 spec=052-->\n"
    cwd = _setup(tmp_path, board)
    sync_spec_mirror(cwd, "052")
    first = (tmp_path / "docs/internal/specs/spec-052-test.md").read_text()
    sync_spec_mirror(cwd, "052")
    second = (tmp_path / "docs/internal/specs/spec-052-test.md").read_text()
    assert first == second  # no duplicate ## Tasks blocks


def test_mirror_auto_close_when_all_cards_done(tmp_path):
    # One linked card, currently in backlog.
    board = "## Backlog\n- [ ] The only card <!--ops:aaa111 spec=052-->\n"
    cwd = _setup(tmp_path, board)
    r1 = sync_spec_mirror(cwd, "052")
    assert r1["all_done"] is False and r1["newly_closed"] is False

    # Now the card is done: removed from the board, archived to DONE.md (id kept).
    (tmp_path / "TASKS.md").write_text("## Backlog\n\n## In Progress\n\n## Review\n", encoding="utf-8")
    (tmp_path / "DONE.md").write_text("- [x] The only card <!--ops:aaa111--> · 2026-06-24\n", encoding="utf-8")
    r2 = sync_spec_mirror(cwd, "052")
    assert r2["total"] == 1 and r2["done"] == 1
    assert r2["all_done"] is True
    assert r2["newly_closed"] is True  # transitioned to complete this run
    text = (tmp_path / "docs/internal/specs/spec-052-test.md").read_text()
    assert "✅ Complete" in text and "[✓] [aaa111]" in text

    # Running again: still complete, but not NEWLY closed.
    r3 = sync_spec_mirror(cwd, "052")
    assert r3["all_done"] is True and r3["newly_closed"] is False


def test_mirror_drops_unlinked_card(tmp_path):
    # First sync records aaa111. Then it's unlinked (no spec) and not in DONE → dropped.
    board = "## Backlog\n- [ ] Card <!--ops:aaa111 spec=052-->\n"
    cwd = _setup(tmp_path, board)
    sync_spec_mirror(cwd, "052")
    (tmp_path / "TASKS.md").write_text("## Backlog\n- [ ] Card <!--ops:aaa111-->\n", encoding="utf-8")
    res = sync_spec_mirror(cwd, "052")
    assert res["total"] == 0  # unlinked + not done → dropped from mirror
    text = (tmp_path / "docs/internal/specs/spec-052-test.md").read_text()
    assert "aaa111" not in text.split(_MIRROR_BEGIN)[1].split(_MIRROR_END)[0]


def test_mirror_missing_spec_file_returns_none(tmp_path):
    board = "## Backlog\n- [ ] Card <!--ops:aaa111 spec=999-->\n"
    cwd = _setup(tmp_path, board)
    assert sync_spec_mirror(cwd, "999") is None  # no spec-999-*.md


def test_done_archive_line_preserves_spec():
    line = _done_archive_line({"id": "aaa111", "text": "Done card", "spec": "052"}, "2026-06-24")
    assert "<!--ops:aaa111 spec=052-->" in line
    # No spec → bare marker
    assert "spec=" not in _done_archive_line({"id": "bbb222", "text": "Plain"})


# ─── spec-059 Move 5: status auto-stamp on close ─────────────────────────────

def test_stamp_status_overwrites_yaml_placeholder():
    text = "---\ncreated: 2026-01-01\nstatus: draft\nauthor: x\n---\n\n# Spec 100\n"
    new, changed = _stamp_status(text, "shipped (2026-06-26)")
    assert changed is True
    assert "status: shipped (2026-06-26)" in new
    assert "status: draft" not in new
    assert "created: 2026-01-01" in new and "author: x" in new  # siblings untouched


def test_stamp_status_leaves_curated_yaml():
    text = "---\nstatus: ABSORBED into spec-052\n---\n\n# Spec 49\n"
    new, changed = _stamp_status(text, "shipped (2026-06-26)")
    assert changed is False and new == text


def test_stamp_status_frontmatter_without_status_is_noop():
    text = "---\ncreated: 2026-01-01\n---\n\n# Spec 100\n"
    new, changed = _stamp_status(text, "shipped (2026-06-26)")
    assert changed is False and new == text


def test_stamp_status_overwrites_inline_bold_placeholder():
    text = "# Spec 100\n\n**Status:** draft\n\nBody.\n"
    new, changed = _stamp_status(text, "shipped (2026-06-26)")
    assert changed is True
    assert "**Status:** shipped (2026-06-26)" in new
    assert "Body." in new


def test_stamp_status_leaves_curated_inline_cyrillic():
    # multi-phase spec: a hand-curated status must never be clobbered
    text = "# Spec 60\n\n**Статус:** [x] Фаза A реализована (2026-06-25)\n"
    new, changed = _stamp_status(text, "shipped (2026-06-26)")
    assert changed is False and new == text


def test_stamp_status_noop_without_any_status_field():
    text = "# Spec 100\n\nJust prose, no status line.\n"
    new, changed = _stamp_status(text, "shipped (2026-06-26)")
    assert changed is False and new == text


def test_auto_close_stamps_draft_status(tmp_path):
    spec_body = "---\nstatus: draft\ncreated: 2026-01-01\n---\n\n# spec-052 — Test\n\nIntro.\n"
    board = "## Backlog\n- [ ] Only <!--ops:aaa111 spec=052-->\n"
    cwd = _setup(tmp_path, board, spec_body=spec_body)
    r1 = sync_spec_mirror(cwd, "052")
    assert r1["newly_closed"] is False and r1["status_stamped"] is False
    # card done → archived
    (tmp_path / "TASKS.md").write_text("## Backlog\n\n## In Progress\n\n## Review\n", encoding="utf-8")
    (tmp_path / "DONE.md").write_text("- [x] Only <!--ops:aaa111--> · 2026-06-24\n", encoding="utf-8")
    r2 = sync_spec_mirror(cwd, "052")
    assert r2["newly_closed"] is True and r2["status_stamped"] is True
    text = (tmp_path / "docs/internal/specs/spec-052-test.md").read_text()
    assert "status: shipped" in text and "status: draft" not in text
    assert "✅ Complete" in text  # block close still works alongside the stamp
    # idempotent: a later run is not newly-closed → no re-stamp
    r3 = sync_spec_mirror(cwd, "052")
    assert r3["newly_closed"] is False and r3["status_stamped"] is False


def test_auto_close_preserves_curated_status(tmp_path):
    # All linked cards done, but the operator's status is curated → keep it verbatim.
    spec_body = "# spec-052 — Test\n\n**Status:** [x] Phase A shipped\n\nIntro.\n"
    cwd = _setup(tmp_path, "## Backlog\n\n## In Progress\n\n## Review\n", spec_body=spec_body)
    (Path(cwd) / "DONE.md").write_text(
        _done_archive_line({"id": "aaa111", "text": "Only", "spec": "052"}, "2026-06-24"),
        encoding="utf-8",
    )
    res = sync_spec_mirror(cwd, "052")
    assert res["newly_closed"] is True and res["status_stamped"] is False
    text = (Path(cwd) / "docs/internal/specs/spec-052-test.md").read_text()
    assert "**Status:** [x] Phase A shipped" in text  # untouched


def test_mirror_detects_done_card_never_mirrored_while_open(tmp_path):
    """Regression: a card archived to DONE.md (with spec= preserved) before the
    spec was ever mirrored must still be detected as done — not lost. This is the
    real bug hit on the live b052cb card."""
    # Empty open board; the only card is already done in DONE.md, tagged spec=052.
    cwd = _setup(tmp_path, "## Backlog\n\n## In Progress\n\n## Review\n")
    (Path(cwd) / "DONE.md").write_text(
        _done_archive_line({"id": "zzz999", "text": "Shipped feature", "spec": "052"}, "2026-06-24"),
        encoding="utf-8",
    )
    res = sync_spec_mirror(cwd, "052")
    assert res is not None
    assert res["total"] == 1 and res["done"] == 1
    assert res["all_done"] is True and res["newly_closed"] is True
    text = (Path(cwd) / "docs/internal/specs/spec-052-test.md").read_text()
    assert "✅ Complete" in text and "[✓] [zzz999]" in text

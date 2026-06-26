"""Tests for spec_epics.py — the epic-lens aggregation (specs <-> board cards)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import board  # noqa: E402
import spec_epics  # noqa: E402


def _make_project(tmp_path, specs: dict, cols: dict, done_lines: list) -> str:
    """Build a throwaway project tree: docs/internal/specs/*.md + TASKS.md + DONE.md."""
    specs_dir = tmp_path / "docs" / "internal" / "specs"
    specs_dir.mkdir(parents=True)
    for fname, body in specs.items():
        (specs_dir / fname).write_text(body, encoding="utf-8")
    full = {k: [] for k, _, _ in board.BOARD_COLUMNS}
    full.update(cols)
    board._save_board(str(tmp_path), "proj", "", full)
    if done_lines:
        (tmp_path / "DONE.md").write_text("\n".join(done_lines) + "\n", encoding="utf-8")
    return str(tmp_path)


# ── _spec_id_from_name ────────────────────────────────────────────────────────
def test_spec_id_from_name():
    assert spec_epics._spec_id_from_name("spec-060-multi-provider.md") == "060"
    assert spec_epics._spec_id_from_name("spec-049-spec-board-linkage.md") == "049"
    assert spec_epics._spec_id_from_name("spec-005-c2-gate.md") == "005"
    assert spec_epics._spec_id_from_name("spec-060.md") == "060"


# ── _norm_spec_id ─────────────────────────────────────────────────────────────
def test_norm_spec_id():
    assert spec_epics._norm_spec_id("060") == "060"
    assert spec_epics._norm_spec_id("spec-060") == "060"
    assert spec_epics._norm_spec_id(None) == ""
    assert spec_epics._norm_spec_id("  049 ") == "049"


# ── _parse_spec_meta ──────────────────────────────────────────────────────────
def test_parse_meta_yaml_frontmatter(tmp_path):
    p = tmp_path / "spec-100-foo.md"
    p.write_text("---\nstatus: draft\ncreated: 2026-01-01\n---\n\n# Spec 100 — Foo\n")
    meta = spec_epics._parse_spec_meta(p)
    assert meta["status"] == "draft"
    assert meta["title"] == "Spec 100 — Foo"


def test_parse_meta_bold_status_en(tmp_path):
    p = tmp_path / "spec-101.md"
    p.write_text("# spec-101 — Bar\n\n**Status:** IMPLEMENTED 2026-06-24\n")
    meta = spec_epics._parse_spec_meta(p)
    assert meta["status"].startswith("IMPLEMENTED")
    assert meta["title"] == "spec-101 — Bar"


def test_parse_meta_bold_status_ru(tmp_path):
    p = tmp_path / "spec-102.md"
    p.write_text("# Spec-102: Ultra\n\n**Статус:** [x] Фаза A реализована\n")
    meta = spec_epics._parse_spec_meta(p)
    assert "Фаза A" in meta["status"]


def test_parse_meta_missing_status(tmp_path):
    p = tmp_path / "spec-103.md"
    p.write_text("# Spec 103\n\nsome body, no status line\n")
    meta = spec_epics._parse_spec_meta(p)
    assert meta["status"] == ""
    assert meta["title"] == "Spec 103"


# ── build_epic_specs ──────────────────────────────────────────────────────────
def test_build_epic_specs_links_and_progress(tmp_path):
    cwd = _make_project(
        tmp_path,
        specs={
            "spec-100-foo.md": "---\nstatus: draft\n---\n\n# Spec 100 — Foo\n",
            "spec-101-bar.md": "# Spec 101 — Bar\n\n**Status:** done\n",
        },
        cols={
            "backlog": [{"id": "aaaaaa", "text": "open A", "spec": "100"}],
            "review": [{"id": "bbbbbb", "text": "open B", "spec": "spec-100"}],  # stray prefix
        },
        done_lines=["- [x] done C <!--ops:cccccc spec=100-->"],
    )
    by_id = {s["spec_id"]: s for s in spec_epics.build_epic_specs(cwd)}
    assert set(by_id) == {"100", "101"}

    s100 = by_id["100"]
    assert s100["title"] == "Spec 100 — Foo"
    assert s100["status"] == "draft"
    assert s100["name"] == "spec-100-foo.md"
    assert {c["id"] for c in s100["cards"]["open"]} == {"aaaaaa", "bbbbbb"}  # both link (norm)
    assert {c["id"] for c in s100["cards"]["done"]} == {"cccccc"}
    assert s100["done_count"] == 1
    assert s100["total"] == 3
    assert abs(s100["progress"] - 1 / 3) < 1e-9
    cols_by_card = {c["id"]: c["column"] for c in s100["cards"]["open"]}
    assert cols_by_card["aaaaaa"] == board._COLUMN_LABEL["backlog"]
    assert cols_by_card["bbbbbb"] == board._COLUMN_LABEL["review"]

    s101 = by_id["101"]
    assert s101["total"] == 0
    assert s101["progress"] == 0.0
    assert s101["cards"]["open"] == [] and s101["cards"]["done"] == []


def test_build_epic_specs_sorted_newest_first(tmp_path):
    cwd = _make_project(
        tmp_path,
        specs={"spec-010-a.md": "# A\n", "spec-100-b.md": "# B\n", "spec-050-c.md": "# C\n"},
        cols={}, done_lines=[],
    )
    assert [s["spec_id"] for s in spec_epics.build_epic_specs(cwd)] == ["100", "050", "010"]


def test_build_epic_specs_no_specs_dir(tmp_path):
    assert spec_epics.build_epic_specs(str(tmp_path)) == []


# ── get_epic_spec_content ─────────────────────────────────────────────────────
def test_get_content_reads_file(tmp_path):
    cwd = _make_project(tmp_path, {"spec-100-foo.md": "# Spec 100\n\nbody here\n"}, {}, [])
    content = spec_epics.get_epic_spec_content(cwd, "spec-100-foo.md")
    assert content is not None and "body here" in content


def test_get_content_rejects_traversal(tmp_path):
    cwd = _make_project(tmp_path, {"spec-100-foo.md": "# x\n"}, {}, [])
    (tmp_path / "secret.md").write_text("TOPSECRET\n")
    assert spec_epics.get_epic_spec_content(cwd, "../../secret.md") is None
    assert spec_epics.get_epic_spec_content(cwd, "../secret.md") is None


def test_get_content_rejects_non_md(tmp_path):
    cwd = _make_project(tmp_path, {"spec-100-foo.md": "# x\n"}, {}, [])
    assert spec_epics.get_epic_spec_content(cwd, "spec-100-foo.txt") is None


def test_get_content_missing(tmp_path):
    cwd = _make_project(tmp_path, {"spec-100-foo.md": "# x\n"}, {}, [])
    assert spec_epics.get_epic_spec_content(cwd, "spec-999-nope.md") is None

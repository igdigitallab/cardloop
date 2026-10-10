"""Superseded / rejected memory articles stay on disk but are never recalled.

The memory source carries the highest rank weight in the search corpus, so a stale article
would outrank live code and fresh chat. `status: superseded` (with `superseded_by:`) or
`status: rejected` in the frontmatter is the soft alternative to deleting it.
"""
from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

import context_pack as CP
import memory_status as MS
import search as S

_SPEC = importlib.util.spec_from_file_location(
    "memory_lint", Path(__file__).resolve().parent.parent / "tools" / "memory-lint.py")
ml = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ml)  # type: ignore[union-attr]


ACTIVE = "---\nname: live\ndescription: x\n---\nThe zephyrine gateway retries three times.\n"
SUPERSEDED = ("---\nname: old\ndescription: x\nmetadata:\n  type: project\n  status: superseded\n"
              "  superseded_by: live\n---\nThe zephyrine gateway never retries.\n")
REJECTED = "---\nname: no\nstatus: \"Rejected\"\n---\nThe zephyrine gateway is a queue.\n"


# ─────────────────────────── helper ───────────────────────────

def test_frontmatter_fields_top_level_and_nested():
    assert MS.frontmatter_fields(SUPERSEDED) == {"status": "superseded", "superseded_by": "live"}
    assert MS.frontmatter_fields(REJECTED)["status"] == "rejected"  # quotes + case normalised
    assert MS.frontmatter_fields(ACTIVE) == {}


def test_status_outside_frontmatter_is_ignored():
    body = "---\nname: a\n---\nstatus: superseded\n"
    assert not MS.is_inactive(body)
    assert not MS.is_inactive("no frontmatter at all\nstatus: rejected\n")


def test_is_inactive():
    assert MS.is_inactive(SUPERSEDED) and MS.is_inactive(REJECTED)
    assert not MS.is_inactive(ACTIVE)
    assert not MS.is_inactive("---\nstatus: active\n---\n")


# ─────────────────────────── search index ───────────────────────────

@pytest.fixture()
def conn():
    c = S.get_db(":memory:")
    S.init_db(c)
    yield c
    c.close()


def _memory_hits(conn, q="zephyrine"):
    return [h for h in S.search(conn, q) if h["source"] == "memory"]


def test_inactive_articles_are_not_indexed(conn, tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "live.md").write_text(ACTIVE, encoding="utf-8")
    (mem / "old.md").write_text(SUPERSEDED, encoding="utf-8")
    (mem / "no.md").write_text(REJECTED, encoding="utf-8")
    stats = S.index_project_files(conn, "p", "P", [mem], source_kind="memory", index_code=False)
    assert stats["inactive"] == 2
    names = {h["ref"]["memory"] for h in _memory_hits(conn)}
    assert names == {"live.md"}


def test_marking_superseded_drops_rows_and_unmarking_restores_them(conn, tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    art = mem / "old.md"
    art.write_text(ACTIVE.replace("name: live", "name: old"), encoding="utf-8")
    S.index_project_files(conn, "p", "P", [mem], source_kind="memory", index_code=False)
    assert {h["ref"]["memory"] for h in _memory_hits(conn)} == {"old.md"}

    art.write_text(SUPERSEDED, encoding="utf-8")
    os.utime(art, (time.time() + 5, time.time() + 5))  # make the change visible to the mtime check
    S.index_project_files(conn, "p", "P", [mem], source_kind="memory", index_code=False)
    assert _memory_hits(conn) == []

    # unchanged inactive file is not re-read (file state was saved) and stays out
    stats = S.index_project_files(conn, "p", "P", [mem], source_kind="memory", index_code=False)
    assert stats["inactive"] == 0 and _memory_hits(conn) == []

    art.write_text(ACTIVE.replace("name: live", "name: old"), encoding="utf-8")
    os.utime(art, (time.time() + 10, time.time() + 10))
    S.index_project_files(conn, "p", "P", [mem], source_kind="memory", index_code=False)
    assert {h["ref"]["memory"] for h in _memory_hits(conn)} == {"old.md"}


def test_status_only_filters_the_memory_source(conn, tmp_path):
    """An ordinary project file that happens to carry `status: superseded` is still indexed."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "notes.md").write_text(SUPERSEDED, encoding="utf-8")
    S.index_project_files(conn, "p", "P", root)
    assert any(h["source"] == "file" for h in S.search(conn, "zephyrine"))


# ─────────────────────────── context pack ───────────────────────────

def test_context_pack_skips_inactive_articles(tmp_path):
    mem = tmp_path / ".claude-ops" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [Live](live.md)\n", encoding="utf-8")
    (mem / "live.md").write_text(ACTIVE, encoding="utf-8")
    (mem / "old.md").write_text(SUPERSEDED, encoding="utf-8")
    (mem / "no.md").write_text(REJECTED, encoding="utf-8")
    index_text, others = CP._read_memory(str(tmp_path))
    assert index_text.startswith("- [Live]")
    assert [o["name"] for o in others] == ["live.md"]


# ─────────────────────────── lint ───────────────────────────

def _lint(d):
    return ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)


def test_lint_flags_superseded_without_a_live_successor(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    (d / "MEMORY.md").write_text("- [Live](live.md)\n", encoding="utf-8")
    (d / "live.md").write_text(ACTIVE, encoding="utf-8")
    (d / "ok.md").write_text(SUPERSEDED, encoding="utf-8")                                  # → live
    (d / "nosucc.md").write_text("---\nstatus: superseded\n---\nx\n", encoding="utf-8")
    (d / "ghost.md").write_text("---\nstatus: superseded\nsuperseded_by: gone\n---\nx\n", encoding="utf-8")
    (d / "chain.md").write_text("---\nstatus: superseded\nsuperseded_by: [[ok]]\n---\nx\n", encoding="utf-8")
    (d / "no.md").write_text(REJECTED, encoding="utf-8")                                    # needs none

    r = _lint(d)
    flagged = {s["name"]: s["why"] for s in r["superseded_without_successor"]}
    assert set(flagged) == {"nosucc.md", "ghost.md", "chain.md"}
    assert flagged["nosucc.md"] == "no superseded_by"
    assert "does not exist" in flagged["ghost.md"]
    assert "itself superseded" in flagged["chain.md"]
    assert set(r["inactive"]) == {"ok.md", "nosucc.md", "ghost.md", "chain.md", "no.md"}
    rendered = ml._render(r)
    assert "nothing replaces it (3)" in rendered
    assert "Not recalled" in rendered

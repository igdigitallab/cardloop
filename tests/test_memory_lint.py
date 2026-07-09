"""Tests for tools/memory-lint.py — the memory-wiki lint operation (spec-078).

Covers: orphan detection, dead index links, oversized bodies, near-duplicate
titles, and that the pass never deletes anything (read-only).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "memory_lint", Path(__file__).resolve().parent.parent / "tools" / "memory-lint.py")
ml = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ml)  # type: ignore[union-attr]


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


def test_orphan_and_dead_link(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [Kept](kept.md) — a note\n- [Gone](ghost.md) — missing file\n")
    _write(d, "kept.md", "---\nname: kept\n---\nbody\n")
    _write(d, "orphan.md", "---\nname: orphan\n---\nnot in the index\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert "orphan.md" in r["orphans"]
    assert "kept.md" not in r["orphans"]
    assert "ghost.md" in r["dead_index_links"]
    # nothing deleted
    assert (d / "orphan.md").exists() and (d / "kept.md").exists()


def test_oversized(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [Big](big.md)\n")
    _write(d, "big.md", "x" * 7000)
    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert any(o["name"] == "big.md" for o in r["oversized"])


def test_near_duplicate_titles(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [a](a.md)\n- [b](b.md)\n")
    _write(d, "a.md", "---\nname: spec-041 oss campaign progress\n---\n")
    _write(d, "b.md", "---\nname: spec-041 progress campaign oss\n---\n")
    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.5)
    assert r["near_duplicates"], "expected the two spec-041 notes flagged as near-duplicates"


def test_stale_refs_uses_repo_index(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "live.py").write_text("x", encoding="utf-8")
    _write(d, "MEMORY.md", "- [m](m.md)\n")
    _write(d, "m.md", "references sub/live.py (exists) and old/dead.py (gone)\n")
    r = ml.lint(d, "MEMORY.md", repo=repo, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    flagged = {s["name"]: s["missing"] for s in r["stale_refs"]}
    assert "m.md" in flagged
    assert "old/dead.py" in flagged["m.md"]
    assert "sub/live.py" not in flagged["m.md"]

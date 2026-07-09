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


# ── index-entry length: the rot nothing could see ─────────────────────────────
#
# MEMORY.md is loaded VERBATIM into every bootstrap, so a 300-char "hook" is paid forever.
# The lint checked article bodies but never index lines — it could not catch the single most
# expensive kind of rot. The home wiki had 48 such entries (median 170 chars).


def test_oversized_index_entry_is_flagged(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    long_hook = "x" * 200
    _write(d, "MEMORY.md", f"- [Short](a.md) — fine\n- [Long](b.md) — {long_hook}\n")
    _write(d, "a.md", "---\nname: a\n---\nbody\n")
    _write(d, "b.md", "---\nname: b\n---\nbody\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6,
                max_entry_chars=150)

    names = [e["line"] for e in r["oversized_entries"]]
    assert names == [2], "only the long entry should be flagged"


def test_non_entry_lines_are_never_flagged(tmp_path):
    """Group headers and prose can be long — only `- ` entries are the per-bootstrap cost."""
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "# Index\n\n" + "## " + ("h" * 300) + "\n\n" + ("prose " * 60) + "\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert r["oversized_entries"] == []


# ── external links: resolve them, don't call them dead ────────────────────────


def test_external_link_that_resolves_is_not_dead(tmp_path):
    """An index may point outside the memory dir. Comparing by basename called every such
    link dead — a false-positive class that buried the real broken ones."""
    d = tmp_path / "memory"
    d.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "GUIDE.md").write_text("real", encoding="utf-8")
    _write(d, "MEMORY.md", "- [Guide](../docs/GUIDE.md) — lives in the repo\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert r["dead_index_links"] == []


def test_external_link_that_does_not_resolve_is_dead(tmp_path):
    """iggo-llc had three of these — one `../` short of real files."""
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [Guide](../../nope/GUIDE.md) — wrong depth\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert r["dead_index_links"] == ["../../nope/GUIDE.md"]


# ── the CLI's silent truncation ceiling ───────────────────────────────────────


def test_index_budget_reports_percent_of_cli_cap(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [A](a.md) — hook\n")

    b = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90,
                dup_threshold=0.6)["index_budget"]

    assert b["max_lines"] == ml.CLI_INDEX_MAX_LINES == 200
    assert b["max_bytes"] == ml.CLI_INDEX_MAX_BYTES == 25_000
    assert b["over_budget"] is False


def test_index_budget_warns_before_entries_vanish(tmp_path):
    """Past 200 lines / 25000 bytes the CLI drops entries from context with NO operator signal."""
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "\n".join(f"- [E{i}](e{i}.md) — hook" for i in range(170)))

    b = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90,
                dup_threshold=0.6)["index_budget"]

    assert b["over_budget"] is True
    assert b["pct_of_cap"] >= 80


# ── log.md: the method's second special file ──────────────────────────────────


def test_missing_log_is_reported(tmp_path):
    """Without a chronology neither agent nor operator can see how the wiki got here."""
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [A](a.md) — hook\n")
    _write(d, "a.md", "---\nname: a\n---\nbody\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert r["has_log"] is False


def test_present_log_is_reported(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [A](a.md) — hook\n")
    _write(d, "a.md", "---\nname: a\n---\nbody\n")
    _write(d, "log.md", "# Wiki log\n\n## [2026-07-09] init | bootstrapped\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert r["has_log"] is True


def test_log_is_not_an_article(tmp_path):
    """log.md is the chronology, never linked and never loaded — it must not become a phantom orphan."""
    d = tmp_path / "memory"
    d.mkdir()
    _write(d, "MEMORY.md", "- [A](a.md) — hook\n")
    _write(d, "a.md", "---\nname: a\n---\nbody\n")
    _write(d, "log.md", "# Wiki log\n\n## [2026-07-09] init | bootstrapped\n")

    r = ml.lint(d, "MEMORY.md", repo=None, max_bytes=6000, stale_days=90, dup_threshold=0.6)
    assert r["orphans"] == []
    assert r["total_files"] == 1

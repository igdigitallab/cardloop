"""
Tests for spec-090 — closing the three holes in an index that already worked:

1. MEMORY WAS HALF-INVISIBLE. Curated memory articles live in two places —
   `<cwd>/.claude-ops/memory/` (inside the project, so the file walk saw it) and
   `~/.claude/projects/<slug>/memory/` (native auto-memory, OUTSIDE the cwd, so nothing
   indexed it at all). They are now one dedicated `memory` source covering both roots,
   which also means the file walk must PRUNE them or the same article gets indexed twice
   under two sources whose sweeps then delete each other's rows.

2. NO MORPHOLOGY. FTS5's unicode61 tokenizer does not stem, so `переезда` and `переезд`
   were different terms. Query-side stemming widens each token to its stem-length prefix.
   The prefix is sliced out of the ORIGINAL token, never taken from the stem, because
   Snowball rewrites letters (ru ё→е) while FTS5 does not fold them.

3. NO RECALL TELEMETRY. `doc_hits` records what retrieval actually returned, per channel,
   so "was this article ever put in front of an agent" is answerable — and it survives a
   reindex, because measurement is not derived data.

Every fixture is a synthetic tmp_path tree; nothing here touches ~/.claude or data/.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import search as S


@pytest.fixture()
def conn():
    c = S.get_db(":memory:")
    S.init_db(c)
    yield c
    c.close()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """A project cwd with an in-repo memory dir, plus an out-of-tree native memory dir."""
    cwd = tmp_path / "proj"
    (cwd / ".claude-ops" / "memory").mkdir(parents=True)
    (cwd / ".claude-ops" / "memory" / "inrepo-note.md").write_text(
        "# In-repo\nThe portal reads the bot database read-only.\n", encoding="utf-8")
    (cwd / "README.md").write_text("# Readme\nordinary prose\n", encoding="utf-8")

    native = tmp_path / "sdk" / "slug" / "memory"
    native.mkdir(parents=True)
    (native / "native-note.md").write_text(
        "# Native\nCoolify pulls production images straight from GitHub.\n", encoding="utf-8")
    return cwd, native


def _index(conn, cwd: Path, native: Path) -> None:
    """Mirrors webapp._build_search_sources: one file source (memory pruned) + one
    memory source spanning both roots."""
    roots = [cwd / ".claude-ops" / "memory", native]
    S.index_project_files(conn, "p", "P", cwd, skip_roots=roots)
    S.index_project_files(conn, "p", "P", roots, source_kind="memory", index_code=False)


# ─────────────────────────── 1. the memory source ───────────────────────────

def test_native_memory_outside_cwd_is_indexed(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    hits = S.search(conn, "Coolify production images")
    assert hits, "native auto-memory outside the cwd must be searchable"
    assert hits[0]["source"] == "memory"
    assert hits[0]["ref"]["memory"] == "native-note.md"


def test_in_repo_memory_moves_to_the_memory_source(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT ref, source FROM docs WHERE ref LIKE '%note.md'")}
    assert rows == {"inrepo-note.md": "memory", "native-note.md": "memory"}


def test_memory_article_is_not_indexed_twice(conn, tmp_path):
    """Without skip_roots the article lands under both 'file' and 'memory', and the two
    sweeps then delete each other's rows on every subsequent scan."""
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    n = conn.execute(
        "SELECT COUNT(*) FROM docs WHERE ref = 'inrepo-note.md'").fetchone()[0]
    assert n == 1


def test_rescan_is_stable(conn, tmp_path):
    """Two sources, same project: the second scan must not sweep the first source's rows."""
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    before = conn.execute("SELECT source, COUNT(*) FROM docs GROUP BY source").fetchall()
    _index(conn, cwd, native)
    after = conn.execute("SELECT source, COUNT(*) FROM docs GROUP BY source").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_deleted_memory_article_is_swept(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    (native / "native-note.md").unlink()
    _index(conn, cwd, native)
    assert S.search(conn, "Coolify production images") == []


def test_ordinary_project_files_still_indexed_as_file(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    src = conn.execute("SELECT source FROM docs WHERE ref = 'README.md'").fetchone()[0]
    assert src == "file"


def test_memory_outranks_chat_for_the_same_words(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    S._insert_doc(conn, "p", "P", "chat", 9e9, "sess", "/chat.jsonl",
                  "Coolify production images, thinking out loud about it")
    conn.commit()
    hits = S.search(conn, "Coolify production images")
    assert hits[0]["source"] == "memory", "curated memory must outrank chat chatter"


def test_source_and_is_filters_reach_memory(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    for query in ("source:memory Coolify", "is:memory Coolify"):
        hits = S.search(conn, query)
        assert hits and all(h["source"] == "memory" for h in hits), query


# ─────────────────────────── 2. query-side morphology ───────────────────────────

@pytest.mark.parametrize("indexed, queried", [
    ("переезд", "переезда"),
    ("переезда", "переезд"),
    ("бэкапы", "бэкап"),
    ("эмбеддинги", "эмбеддингов"),
])
def test_russian_case_endings_match_each_other(conn, indexed, queried):
    pytest.importorskip("snowballstemmer")
    S._insert_doc(conn, "p", "P", "chat", 0, "s", "/c.jsonl", f"обсудили {indexed} проекта")
    conn.commit()
    assert S.search(conn, queried), f"{queried!r} must find {indexed!r}"


def test_stem_prefix_is_always_a_prefix_of_the_original_token():
    """Snowball rewrites letters (ru ё→е, en happy→happi). Using the stem verbatim would
    build a prefix that matches nothing, since FTS5's unicode61 does not fold ё."""
    pytest.importorskip("snowballstemmer")
    for token in ("ёлки", "счёта", "переезда", "happy", "deployed", "серии"):
        assert token.startswith(S._stem_prefix(token)), token


def test_yo_survives_stemming(conn):
    pytest.importorskip("snowballstemmer")
    S._insert_doc(conn, "p", "P", "chat", 0, "s", "/c.jsonl", "перевёл счёта вчера")
    conn.commit()
    assert S.search(conn, "счёта"), "ё must not be normalised out of the query prefix"


def test_short_tokens_are_left_alone():
    assert S._stem_prefix("лог") == "лог"
    assert S._stem_prefix("id") == "id"


def test_non_word_tokens_are_left_alone():
    for token in ("2026-09-02", "!!!", "123456"):
        assert S._stem_prefix(token) == token


def test_stemming_absent_degrades_to_the_old_behaviour(monkeypatch):
    """The dependency is optional: without it every term keeps its literal prefix."""
    monkeypatch.setattr(S, "_STEMMER_CACHE", {"russian": None, "english": None})
    assert S._stem_prefix("переезда") == "переезда"
    assert S._build_match_expr("переезда") == '"переезда"*'


def test_match_expression_stays_syntactically_safe(conn):
    """Stemming must not reopen the quoting hole — a query can never be invalid FTS5."""
    S._insert_doc(conn, "p", "P", "chat", 0, "s", "/c.jsonl", "plain body")
    conn.commit()
    for hostile in ('what\'s "this"', "NEAR(a b)", "a:b:c", 'un"balanced', "OR AND NOT"):
        S.search(conn, hostile)  # must not raise


# ─────────────────────────── 3. recall telemetry ───────────────────────────

def test_hits_are_recorded_only_when_a_channel_is_given(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    S.search(conn, "Coolify")
    assert conn.execute("SELECT COUNT(*) FROM doc_hits").fetchone()[0] == 0
    S.search(conn, "Coolify", channel="cli")
    assert conn.execute("SELECT COUNT(*) FROM doc_hits").fetchone()[0] >= 1


def test_hits_accumulate_per_channel(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    S.search(conn, "Coolify", channel="cli")
    S.search(conn, "Coolify", channel="cli")
    S.search(conn, "Coolify", channel="pack")
    rows = dict(conn.execute(
        "SELECT channel, hits FROM doc_hits WHERE ref = 'native-note.md'").fetchall())
    assert rows == {"cli": 2, "pack": 1}


def test_hit_stats_separates_returned_from_never_returned(conn, tmp_path):
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    S.search(conn, "Coolify", channel="pack")
    report = S.hit_stats(conn, source="memory")
    assert report["totals"]["indexed_docs"] == 2
    assert [r["ref"] for r in report["top"]] == ["native-note.md"]
    assert [r["ref"] for r in report["cold"]] == ["inrepo-note.md"]


def test_telemetry_survives_a_full_reindex(conn, tmp_path):
    """doc_hits is measurement, not derived data — a rebuild must not erase the history."""
    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    S.search(conn, "Coolify", channel="pack")
    S.reset_db(conn)
    assert conn.execute("SELECT SUM(hits) FROM doc_hits").fetchone()[0] == 1


def test_telemetry_failure_never_breaks_a_search(conn, tmp_path):
    """Telemetry runs inline on the request path, so ANY failure in it — a locked database
    or a malformed row — must be swallowed rather than surface as a failed search."""

    class LockedConn:
        def execute(self, *_a, **_kw):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            pass

    assert S.record_hits(LockedConn(), "cli",
                          [{"ref": "x", "project_id": "p", "source": "memory"}]) == 0
    # A row of the wrong shape is not a sqlite error, and must not escape either.
    assert S.record_hits(conn, "cli", [object()]) == 0

    cwd, native = _project(tmp_path)
    _index(conn, cwd, native)
    assert S.search(conn, "Coolify", channel="cli"), "the search itself still returns hits"

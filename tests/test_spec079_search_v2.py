"""
Tests for spec-079 — making search hits OPENABLE (phase C) and indexing project
FILES (phase A).

Phase C — the three backend pieces the deep-link rests on:
- search.py schema versioning: an index written by an older SCHEMA_VERSION is dropped
  AND its file_state with it (otherwise the rebuild resumes from stored byte offsets
  and the fresh table stays permanently empty).
- deep-link anchors surfaced by search(): chat → session_id + message uuid,
  board → card_id (indexed since spec-074 but never returned, so board hits could
  not be opened).
- webapp._window_history: the feed is capped at `limit` messages, so a hit deeper than
  that is unreachable unless the window is centred on the match.

Phase A — coverage and the traps that come with it:
- chunking replaces truncation, so a long body's tail stays findable;
- the file walker honours the file browser's OWN exclusion rules (secrets, node_modules)
  so the index can never surface something the cockpit refuses to show;
- deleted files are swept, and the sweep is scoped per project;
- doc_rows, the side index that keeps delete-by-path off a full FTS scan.

Every fixture is a synthetic tmp_path file; nothing here touches ~/.claude.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import search as S
import webapp as _webapp


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture
def conn(tmp_path):
    c = S.get_db(tmp_path / "search.db")
    S.init_db(c)
    yield c
    c.close()


# ═══════════════════════════ schema versioning ═══════════════════════════

class TestSchemaVersion:
    def test_init_db_stamps_current_version(self, conn):
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(row[0]) == S.SCHEMA_VERSION

    def test_init_db_is_idempotent(self, conn, tmp_path):
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "s1.jsonl", [
            {"type": "user", "sessionId": "s1", "uuid": "u1",
             "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": "hello indexing"}},
        ])
        S.index_transcripts(conn, "p", "P", sdk)
        before = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
        S.init_db(conn)  # same version → must NOT wipe
        assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == before

    def test_stale_version_drops_docs_and_file_state(self, tmp_path):
        """The migration must clear file_state too. Leaving it behind is the subtle
        failure: the indexer resumes from the stored offset, finds nothing new, and the
        rebuilt index stays empty forever."""
        db = tmp_path / "search.db"
        c = S.get_db(db)
        S.init_db(c)
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "s1.jsonl", [
            {"type": "user", "sessionId": "s1", "uuid": "u1",
             "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": "kanban discussion"}},
        ])
        S.index_transcripts(c, "p", "P", sdk)
        assert conn_count(c, "docs") == 1
        assert conn_count(c, "file_state") == 1

        # Simulate an index written by an older build.
        c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        c.commit()
        S.init_db(c)
        assert conn_count(c, "docs") == 0
        assert conn_count(c, "file_state") == 0

        # And the rebuild must actually repopulate (this is what a stale file_state broke).
        S.index_transcripts(c, "p", "P", sdk)
        assert conn_count(c, "docs") == 1
        c.close()

    def test_fresh_db_is_not_wiped_on_first_init(self, tmp_path):
        """A brand-new DB has no docs table and no version row — it must be created,
        not treated as a stale index."""
        c = S.get_db(tmp_path / "fresh.db")
        S.init_db(c)
        assert conn_count(c, "docs") == 0
        assert int(c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) \
            == S.SCHEMA_VERSION
        c.close()


def conn_count(c: sqlite3.Connection, table: str) -> int:
    return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ═══════════════════════════ deep-link anchors ═══════════════════════════

class TestDeepLinkAnchors:
    def test_chat_hit_carries_session_and_uuid(self, conn, tmp_path):
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "sess-abc.jsonl", [
            {"type": "user", "sessionId": "sess-abc", "uuid": "uuid-111",
             "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": "pricing for lawyers"}},
        ])
        S.index_transcripts(conn, "p", "P", sdk)
        hits = S.search(conn, "lawyers")
        assert len(hits) == 1
        assert hits[0]["ref"] == {"session_id": "sess-abc", "uuid": "uuid-111"}

    def test_assistant_hit_also_carries_uuid(self, conn, tmp_path):
        """Assistant uuids are stored even though the feed does not expose them yet —
        the frontend degrades to nearest-ts today and upgrades for free later."""
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "s2.jsonl", [
            {"type": "assistant", "sessionId": "s2", "uuid": "uuid-999",
             "timestamp": "2026-07-01T10:00:05.000Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "the invoice module"}]}},
        ])
        S.index_transcripts(conn, "p", "P", sdk)
        hits = S.search(conn, "invoice")
        assert hits[0]["ref"]["uuid"] == "uuid-999"

    def test_chat_hit_without_uuid_omits_the_key(self, conn, tmp_path):
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "s3.jsonl", [
            {"type": "user", "sessionId": "s3", "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": "orphan message"}},
        ])
        S.index_transcripts(conn, "p", "P", sdk)
        ref = S.search(conn, "orphan")[0]["ref"]
        assert ref == {"session_id": "s3"}
        assert "uuid" not in ref

    def test_board_hit_carries_card_id(self, conn, tmp_path):
        """Regression: card_id was indexed since spec-074 but dropped on the way out,
        so a board hit had nothing to open."""
        tasks = tmp_path / "TASKS.md"
        tasks.write_text("## Backlog\n- [ ] rewrite the billing exporter <!--ops:a1b2c3-->\n",
                         encoding="utf-8")
        S.index_board_file(conn, "p", "P", tasks)
        hits = S.search(conn, "billing")
        assert len(hits) == 1
        assert hits[0]["ref"] == {"card_id": "a1b2c3"}

    def test_timeline_hit_has_no_anchor(self, conn, tmp_path):
        tl = tmp_path / "tl.jsonl"
        _write_jsonl(tl, [{"kind": "text", "ts": 1751000000.0, "text": "deployed the proxy"}])
        S.index_timeline_file(conn, "p", "P", tl)
        assert S.search(conn, "proxy")[0]["ref"] == {}


# ═══════════════════════════ history windowing ═══════════════════════════

def _msgs(n: int) -> list:
    """n messages, uuid u0..u(n-1), ts 1000, 2000, … (epoch ms)."""
    return [{"role": "user", "text": f"m{i}", "tools": [],
             "uuid": f"u{i}", "ts": (i + 1) * 1000} for i in range(n)]


class TestWindowHistory:
    def test_no_anchor_returns_the_tail(self):
        out = _webapp._window_history(_msgs(50), 10)
        assert [m["text"] for m in out] == [f"m{i}" for i in range(40, 50)]

    def test_short_history_is_returned_whole(self):
        out = _webapp._window_history(_msgs(4), 10, around_uuid="u1")
        assert len(out) == 4

    def test_uuid_anchor_centres_the_window(self):
        out = _webapp._window_history(_msgs(100), 10, around_uuid="u50")
        texts = [m["text"] for m in out]
        assert len(out) == 10
        assert "m50" in texts
        # centred, not tail-pinned — this is the whole point
        assert "m99" not in texts

    def test_ts_anchor_picks_the_nearest_message(self):
        """An assistant hit has no uuid, so it anchors by timestamp — and the search
        index's second-resolution ts will rarely land exactly on a feed ms value."""
        out = _webapp._window_history(_msgs(100), 10, around_ts=50_400)
        assert "m49" in [m["text"] for m in out]

    def test_uuid_wins_over_ts(self):
        out = _webapp._window_history(_msgs(100), 10, around_uuid="u20", around_ts=90_000)
        texts = [m["text"] for m in out]
        assert "m20" in texts and "m89" not in texts

    def test_unknown_anchor_degrades_to_the_tail(self):
        out = _webapp._window_history(_msgs(50), 10, around_uuid="nope")
        assert [m["text"] for m in out] == [f"m{i}" for i in range(40, 50)]

    def test_anchor_near_the_start_still_returns_a_full_window(self):
        out = _webapp._window_history(_msgs(100), 10, around_uuid="u0")
        assert len(out) == 10
        assert out[0]["text"] == "m0"

    def test_anchor_near_the_end_still_returns_a_full_window(self):
        out = _webapp._window_history(_msgs(100), 10, around_uuid="u99")
        assert len(out) == 10
        assert out[-1]["text"] == "m99"

    def test_messages_without_ts_are_skipped_by_the_ts_anchor(self):
        msgs = _msgs(40)
        for m in msgs[:20]:
            m["ts"] = None
        out = _webapp._window_history(msgs, 6, around_ts=25_000)
        assert "m24" in [m["text"] for m in out]


# ═══════════════════════════ chunking (A2) ═══════════════════════════

class TestChunking:
    def test_short_body_is_one_chunk(self):
        assert list(S._chunks("hello world")) == [("hello world", 1)]

    def test_empty_body_yields_nothing(self):
        assert list(S._chunks("   \n  ")) == []

    def test_long_body_is_split_not_truncated(self, conn, tmp_path):
        """The regression this exists for: a long message used to be cut at BODY_CHAR_CAP,
        making its tail permanently unfindable."""
        tail = "zzunique_tail_marker"
        body = ("lorem ipsum dolor sit amet " * 400) + tail
        assert len(body) > S.BODY_CHAR_CAP
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "s.jsonl", [
            {"type": "user", "sessionId": "s", "uuid": "u",
             "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": body}},
        ])
        S.index_transcripts(conn, "p", "P", sdk)
        assert conn_count(conn, "docs") > 1                 # split, not one truncated row
        assert len(S.search(conn, "zzunique_tail_marker")) >= 1   # the tail is findable

    def test_chunks_report_increasing_start_lines(self):
        body = "\n".join(f"line {i} with some filler text to add width" for i in range(200))
        lines = [ln for _, ln in S._chunks(body)]
        assert len(lines) > 1
        assert lines == sorted(lines)
        assert lines[0] == 1


# ═══════════════════════════ project files (A3) ═══════════════════════════

_EXCLUDE = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist"}


def _is_secret(name: str) -> bool:
    return name.startswith(".env") and name != ".env.example"


def _mkproject(root: Path) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / ".claude-ops" / "memory").mkdir(parents=True, exist_ok=True)
    (root / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / "CLAUDE.md").write_text("# Project rules\nDeploy via Coolify.\n", encoding="utf-8")
    (root / "README.md").write_text("# Readme\nA billing exporter.\n", encoding="utf-8")
    (root / "app.py").write_text("def handler():\n    return 'zebrafish'\n", encoding="utf-8")
    (root / "docs" / "design.md").write_text("Пароли хранятся в сейфе.\n", encoding="utf-8")
    (root / ".claude-ops" / "memory" / "note.md").write_text(
        "Гвоздь программы: индексация.\n", encoding="utf-8")
    # .env.yml is the case that actually exercises the is_secret gate: its extension IS in
    # CODE_EXTS, so only the secret-name rule keeps it out. (A bare ".env" has no suffix at
    # all and is already dropped by the extension filter.)
    (root / ".env").write_text("SECRET_TOKEN=supersecret_zzz\n", encoding="utf-8")
    (root / ".env.yml").write_text("token: supersecret_yml\n", encoding="utf-8")
    (root / ".env.example").write_text("SECRET_TOKEN=placeholder_ok\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("var junk = 'zebrafish'\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")


class TestProjectFiles:
    def test_indexes_prose_and_code(self, conn, tmp_path):
        _mkproject(tmp_path)
        r = S.index_project_files(conn, "p", "P", tmp_path,
                                  exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert r["docs"] > 0
        paths = {h["ref"]["path"] for h in S.search(conn, "billing")}
        assert "README.md" in paths
        assert S.search(conn, "Coolify")[0]["ref"]["path"] == "CLAUDE.md"

    def test_indexes_russian_prose_and_memory_articles(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert S.search(conn, "сейфе")[0]["ref"]["path"] == "docs/design.md"
        # .claude-ops is a DOTTED dir and must survive the walk — it is curated prose
        assert S.search(conn, "индексация")[0]["ref"]["path"] == ".claude-ops/memory/note.md"

    def test_never_indexes_secrets(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert S.search(conn, "supersecret_zzz") == []
        # The one that would otherwise slip through on its .yml extension:
        assert S.search(conn, "supersecret_yml") == []

    def test_excluded_dirs_are_pruned(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        paths = {h["ref"]["path"] for h in S.search(conn, "zebrafish")}
        assert paths == {"app.py"}  # node_modules copy is not indexed

    def test_binary_and_unknown_extensions_are_skipped(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert not any(h["ref"]["path"].endswith(".png") for h in S.search(conn, "PNG"))

    def test_code_tier_is_skippable(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path, exclude_dirs=_EXCLUDE,
                              is_secret=_is_secret, index_code=False)
        assert S.search(conn, "zebrafish") == []
        assert S.search(conn, "Coolify")  # prose still indexed

    def test_tier_is_reported(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert S.search(conn, "Coolify")[0]["ref"]["tier"] == "doc"
        assert S.search(conn, "zebrafish")[0]["ref"]["tier"] == "code"

    def test_rescan_is_a_noop_when_nothing_changed(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        before = conn_count(conn, "docs")
        r = S.index_project_files(conn, "p", "P", tmp_path,
                                  exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert r["docs"] == 0
        assert conn_count(conn, "docs") == before

    def test_edited_file_is_reindexed_not_duplicated(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        (tmp_path / "README.md").write_text("# Readme\nNow about invoices.\n", encoding="utf-8")
        os.utime(tmp_path / "README.md", (2_000_000_000, 2_000_000_000))
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert S.search(conn, "billing") == []          # old content gone
        assert S.search(conn, "invoices")[0]["ref"]["path"] == "README.md"

    def test_deleted_file_is_swept(self, conn, tmp_path):
        """Without the sweep the index keeps answering with content that no longer exists."""
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert S.search(conn, "Coolify")
        (tmp_path / "CLAUDE.md").unlink()
        r = S.index_project_files(conn, "p", "P", tmp_path,
                                  exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert r["removed"] == 1
        assert S.search(conn, "Coolify") == []

    def test_sweep_is_scoped_to_the_project(self, conn, tmp_path):
        """A project whose root vanished must never wipe another project's file docs."""
        a, b = tmp_path / "a", tmp_path / "b"
        _mkproject(a)
        _mkproject(b)
        S.index_project_files(conn, "pa", "A", a, exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        S.index_project_files(conn, "pb", "B", b, exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        for f in a.rglob("*.md"):
            f.unlink()
        S.index_project_files(conn, "pa", "A", a, exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert {h["project_id"] for h in S.search(conn, "Coolify")} == {"pb"}

    def test_missing_root_is_a_noop(self, conn, tmp_path):
        r = S.index_project_files(conn, "p", "P", tmp_path / "nope",
                                  exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        assert r == {"files": 0, "docs": 0, "removed": 0, "code_skipped": False}

    def test_scan_all_accepts_file_sources(self, conn, tmp_path):
        _mkproject(tmp_path)
        stats = S.scan_all(conn, [], [], [], [{
            "project_id": "p", "project_name": "P", "root": tmp_path,
            "exclude_dirs": _EXCLUDE, "is_secret": _is_secret,
        }])
        assert stats["file_docs"] > 0
        assert S.search(conn, "Coolify")


class TestDocRowsIndex:
    def test_delete_by_path_clears_both_tables(self, conn, tmp_path):
        _mkproject(tmp_path)
        S.index_project_files(conn, "p", "P", tmp_path,
                              exclude_dirs=_EXCLUDE, is_secret=_is_secret)
        target = str(tmp_path / "CLAUDE.md")
        assert conn.execute("SELECT count(*) FROM doc_rows WHERE path=?", (target,)).fetchone()[0] > 0
        S._delete_docs_for_path(conn, target)
        assert conn.execute("SELECT count(*) FROM doc_rows WHERE path=?", (target,)).fetchone()[0] == 0
        assert S.search(conn, "Coolify") == []

    def test_side_index_tracks_every_source(self, conn, tmp_path):
        """doc_rows must cover chat/board/timeline too — they all delete by path."""
        sdk = tmp_path / "sdk"
        _write_jsonl(sdk / "s.jsonl", [
            {"type": "user", "sessionId": "s", "uuid": "u",
             "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"role": "user", "content": "anchovy"}},
        ])
        S.index_transcripts(conn, "p", "P", sdk)
        assert conn_count(conn, "doc_rows") == conn_count(conn, "docs")

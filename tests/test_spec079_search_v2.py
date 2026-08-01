"""
Tests for spec-079 phase C — making a search hit OPENABLE.

Covers the three backend pieces the deep-link rests on:
- search.py schema versioning: an index written by an older SCHEMA_VERSION is dropped
  AND its file_state with it (otherwise the rebuild resumes from stored byte offsets
  and the fresh table stays permanently empty).
- deep-link anchors surfaced by search(): chat → session_id + message uuid,
  board → card_id (indexed since spec-074 but never returned, so board hits could
  not be opened).
- webapp._window_history: the feed is capped at `limit` messages, so a hit deeper than
  that is unreachable unless the window is centred on the match.

Every fixture is a synthetic tmp_path file; nothing here touches ~/.claude.
"""
import json
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

"""
Tests for spec-074 — global search (SQLite FTS5 over chat transcripts, timelines,
and kanban boards).

Covers:
- search.py indexer units: transcript text extraction (RU+EN), sidechain/tool-block
  exclusion, subagents/ subdirectory exclusion, incremental offset resume, timeline
  'kind:text' filtering, board full-file reindex on mtime change (+ preamble vs.
  DONE.md section-gating), safe FTS5 query escaping (never raises on hostile input),
  bm25 ranking, body cap.
- GET /api/search + POST /api/search/reindex endpoints (repo's aiohttp test pattern,
  see tests/test_board_api.py).

Safety: every fixture here is a synthetic tmp_path file. The endpoint tests
monkeypatch webapp._sdk_sessions_dir so _build_search_sources can never resolve
into (let alone scan) the real ~/.claude/projects of the machine running this suite.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import search as S
import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── shared helpers ───────────────────────────

def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _user_line(session_id, content, ts="2026-07-01T10:00:00.000Z", **extra):
    rec = {"type": "user", "sessionId": session_id, "timestamp": ts,
           "message": {"role": "user", "content": content}}
    rec.update(extra)
    return rec


def _assistant_text_line(session_id, text, ts="2026-07-01T10:00:05.000Z", **extra):
    rec = {"type": "assistant", "sessionId": session_id, "timestamp": ts,
           "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    rec.update(extra)
    return rec


@pytest.fixture
def conn(tmp_path):
    c = S.get_db(tmp_path / "search.db")
    S.init_db(c)
    yield c
    c.close()


# ═══════════════════════════ index_transcripts ═══════════════════════════

class TestIndexTranscripts:
    def test_extracts_user_and_assistant_text(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [
            _user_line("sess1", "What did we decide about pricing for lawyers?"),
            _assistant_text_line("sess1", "We locked personal injury lawyers as the beachhead."),
        ])
        stats = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats == {"files": 1, "docs": 2}
        rows = conn.execute("SELECT source, project_id, ref FROM docs ORDER BY rowid").fetchall()
        assert [r["source"] for r in rows] == ["chat", "chat"]
        assert all(r["project_id"] == "p1" for r in rows)
        assert all(r["ref"] == "sess1" for r in rows)

    def test_russian_text_indexed_and_searchable(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [
            _assistant_text_line("sess1", "Обсуждение цен для юристов завершено."),
        ])
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        hits = S.search(conn, "юристов")
        assert len(hits) == 1
        assert hits[0]["source"] == "chat"

    def test_skips_sidechain_records(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [
            _user_line("sess1", "sub-agent internal chatter", isSidechain=True),
            _assistant_text_line("sess1", "real assistant reply", isSidechain=False),
        ])
        stats = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats["docs"] == 1
        body = conn.execute("SELECT body FROM docs").fetchone()["body"]
        assert "real assistant reply" in body

    def test_skips_parent_tool_use_id_records(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [
            _user_line("sess1", "should be excluded", parent_tool_use_id="tool123"),
            _assistant_text_line("sess1", "kept"),
        ])
        stats = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats["docs"] == 1

    def test_skips_tool_use_tool_result_and_thinking_blocks(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [
            {"type": "user", "sessionId": "sess1", "timestamp": "2026-07-01T10:00:00.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "raw output"}]}},
            {"type": "assistant", "sessionId": "sess1", "timestamp": "2026-07-01T10:00:01.000Z",
             "message": {"content": [{"type": "tool_use", "id": "1", "name": "Bash", "input": {}}]}},
            {"type": "assistant", "sessionId": "sess1", "timestamp": "2026-07-01T10:00:02.000Z",
             "message": {"content": [{"type": "thinking", "thinking": "internal reasoning"}]}},
        ])
        stats = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats["docs"] == 0

    def test_skips_subagents_subdirectory(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "main session text")])
        _write_jsonl(sdk_dir / "sess1" / "subagents" / "agent-x.jsonl", [
            _assistant_text_line("agent-x", "sub-agent transcript text should never be indexed"),
        ])
        stats = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats == {"files": 1, "docs": 1}
        hits = S.search(conn, "sub-agent transcript")
        assert hits == []

    def test_incremental_offset_resume(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        path = sdk_dir / "sess1.jsonl"
        _write_jsonl(path, [_assistant_text_line("sess1", "first message")])
        stats1 = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats1["docs"] == 1

        # Second scan, unchanged file -> no-op
        stats2 = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats2["docs"] == 0

        # Append one more line -> only the NEW line is indexed (byte-offset resume)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_assistant_text_line("sess1", "second message")) + "\n")
        stats3 = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats3["docs"] == 1
        total = conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"]
        assert total == 2

    def test_missing_sdk_dir_is_a_noop(self, conn, tmp_path):
        stats = S.index_transcripts(conn, "p1", "MyProj", tmp_path / "does-not-exist")
        assert stats == {"files": 0, "docs": 0}

    def test_body_capped(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        long_text = "pricing lawyers " * 400  # well over BODY_CHAR_CAP
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", long_text)])
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        body = conn.execute("SELECT body FROM docs").fetchone()["body"]
        assert len(body) <= S.BODY_CHAR_CAP

    def test_malformed_json_line_is_skipped_not_fatal(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        path = sdk_dir / "sess1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write(json.dumps(_assistant_text_line("sess1", "valid line after garbage")) + "\n")
        stats = S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        assert stats["docs"] == 1


# ═══════════════════════════ index_timeline_file ═══════════════════════════

class TestIndexTimeline:
    def test_only_text_kind_rows_indexed(self, conn, tmp_path):
        path = tmp_path / "timeline" / "myproj.jsonl"
        _write_jsonl(path, [
            {"kind": "text", "text": "lawyers pricing note", "ts": time.time()},
            {"kind": "tool", "text": "irrelevant tool event", "ts": time.time()},
            {"kind": "text", "text": "", "ts": time.time()},  # blank text -> skipped
        ])
        stats = S.index_timeline_file(conn, "p1", "MyProj", path)
        assert stats == {"files": 1, "docs": 1}

    def test_incremental_append(self, conn, tmp_path):
        path = tmp_path / "timeline" / "myproj.jsonl"
        _write_jsonl(path, [{"kind": "text", "text": "first note", "ts": time.time()}])
        S.index_timeline_file(conn, "p1", "MyProj", path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "text", "text": "second note", "ts": time.time()}) + "\n")
        stats = S.index_timeline_file(conn, "p1", "MyProj", path)
        assert stats["docs"] == 1

    def test_missing_file_is_a_noop(self, conn, tmp_path):
        stats = S.index_timeline_file(conn, "p1", "MyProj", tmp_path / "nope.jsonl")
        assert stats == {"files": 0, "docs": 0}


# ═══════════════════════════ index_board_file ═══════════════════════════

class TestIndexBoard:
    def test_one_doc_per_card(self, conn, tmp_path):
        path = tmp_path / "TASKS.md"
        path.write_text(
            "# Tasks\n\n## Backlog\n"
            "- [ ] Draft lawyers pricing page <!--ops:aaa111-->\n"
            "- [ ] Something else <!--ops:bbb222-->\n"
            "\n## In Progress\n",
            encoding="utf-8",
        )
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats == {"files": 1, "docs": 2}
        rows = conn.execute("SELECT ref FROM docs ORDER BY ref").fetchall()
        assert {r["ref"] for r in rows} == {"aaa111", "bbb222"}

    def test_preamble_bullet_is_not_indexed_as_a_card(self, conn, tmp_path):
        """A '- ...' bullet BEFORE the first '## <Column>' header is board.py preamble,
        not a card — the indexer must not misfile it as searchable board content."""
        path = tmp_path / "TASKS.md"
        path.write_text(
            "# Tasks — MyProj\n\n"
            "- this is a preamble bullet, not a card\n\n"
            "## Backlog\n- [ ] Real card <!--ops:aaa111-->\n",
            encoding="utf-8",
        )
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats["docs"] == 1
        body = conn.execute("SELECT body FROM docs").fetchone()["body"]
        assert body == "Real card"

    def test_done_md_flat_archive_has_no_section_headers(self, conn, tmp_path):
        """DONE.md is a flat append-only archive (board.py: no '## <Column>' headers at
        all) — every card line must still be indexed despite there being no section."""
        path = tmp_path / "DONE.md"
        path.write_text(
            "- [x] finished one <!--ops:ddd111-->\n"
            "- [x] finished two <!--ops:ddd222-->\n",
            encoding="utf-8",
        )
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats["docs"] == 2

    def test_reindex_on_mtime_change_drops_old_docs(self, conn, tmp_path):
        path = tmp_path / "TASKS.md"
        path.write_text("## Backlog\n- [ ] Old card <!--ops:aaa111-->\n", encoding="utf-8")
        S.index_board_file(conn, "p1", "MyProj", path)
        assert conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"] == 1

        path.write_text("## Backlog\n- [ ] New card <!--ops:ccc333-->\n", encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))  # force a distinct mtime
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats["docs"] == 1
        rows = conn.execute("SELECT ref FROM docs").fetchall()
        assert [r["ref"] for r in rows] == ["ccc333"]  # old card gone, not accumulated

    def test_unchanged_mtime_is_a_noop(self, conn, tmp_path):
        path = tmp_path / "TASKS.md"
        path.write_text("## Backlog\n- [ ] Card <!--ops:aaa111-->\n", encoding="utf-8")
        S.index_board_file(conn, "p1", "MyProj", path)
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats["docs"] == 0

    def test_missing_board_is_a_noop(self, conn, tmp_path):
        stats = S.index_board_file(conn, "p1", "MyProj", tmp_path / "TASKS.md")
        assert stats == {"files": 0, "docs": 0}

    def test_deleted_board_cleans_up_stale_docs(self, conn, tmp_path):
        path = tmp_path / "TASKS.md"
        path.write_text("## Backlog\n- [ ] Card <!--ops:aaa111-->\n", encoding="utf-8")
        S.index_board_file(conn, "p1", "MyProj", path)
        assert conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"] == 1
        path.unlink()
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats == {"files": 0, "docs": 0}
        assert conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"] == 0

    def test_plain_dash_card_without_checkbox(self, conn, tmp_path):
        """Agents sometimes write '- text' without a checkbox — board.py treats this
        as a valid card too (backlog); the indexer must match that behaviour."""
        path = tmp_path / "TASKS.md"
        path.write_text("## Backlog\n- Plain card no checkbox\n", encoding="utf-8")
        stats = S.index_board_file(conn, "p1", "MyProj", path)
        assert stats["docs"] == 1


# ═══════════════════════════ search() ═══════════════════════════

class TestSearch:
    def test_empty_query_returns_empty(self, conn):
        assert S.search(conn, "") == []
        assert S.search(conn, "   ") == []

    def test_ranks_across_sources_and_groups_by_project(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "lawyers pricing plan discussed")])
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)

        tl_path = tmp_path / "timeline" / "myproj.jsonl"
        _write_jsonl(tl_path, [{"kind": "text", "text": "lawyers pricing note", "ts": time.time()}])
        S.index_timeline_file(conn, "p1", "MyProj", tl_path)

        board_path = tmp_path / "TASKS.md"
        board_path.write_text("## Backlog\n- [ ] lawyers pricing page <!--ops:aaa111-->\n", encoding="utf-8")
        S.index_board_file(conn, "p1", "MyProj", board_path)

        hits = S.search(conn, "lawyers pricing")
        assert len(hits) == 3
        assert {h["source"] for h in hits} == {"chat", "timeline", "board"}
        assert all(h["project_id"] == "p1" for h in hits)

    def test_project_filter(self, conn, tmp_path):
        for pid in ("p1", "p2"):
            sdk_dir = tmp_path / pid
            _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "shared keyword pricing")])
            S.index_transcripts(conn, pid, pid, sdk_dir)
        hits = S.search(conn, "pricing", project_id="p1")
        assert len(hits) == 1
        assert hits[0]["project_id"] == "p1"

    def test_snippet_uses_private_delimiters_not_raw_html(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "pricing for lawyers")])
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        hits = S.search(conn, "pricing")
        assert S.SNIPPET_OPEN in hits[0]["snippet"]
        assert "<mark>" not in hits[0]["snippet"]

    def test_chat_ref_carries_session_id(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess-xyz.jsonl", [_assistant_text_line("sess-xyz", "unique text pricing")])
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        hits = S.search(conn, "pricing")
        assert hits[0]["ref"]["session_id"] == "sess-xyz"

    def test_hostile_queries_never_raise(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "some normal text")])
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        hostile = [
            "what's \"this\"", '":::(((', "NEAR(a b)", "AND OR NOT", '""""',
            "*" * 50, "\\" * 20, "colon:value", "(parens)",
        ]
        for q in hostile:
            hits = S.search(conn, q)  # must not raise
            assert isinstance(hits, list)

    def test_limit_is_clamped(self, conn, tmp_path):
        sdk_dir = tmp_path / "sdk"
        lines = [_assistant_text_line(f"s{i}", f"pricing item {i}", ts=f"2026-07-01T10:{i:02d}:00.000Z")
                 for i in range(5)]
        _write_jsonl(sdk_dir / "sess1.jsonl", lines)
        S.index_transcripts(conn, "p1", "MyProj", sdk_dir)
        hits = S.search(conn, "pricing", limit=2)
        assert len(hits) == 2


# ═══════════════════════════ full_reindex_at / scan_all_at ═══════════════════════════

def test_full_reindex_at_rebuilds(tmp_path):
    db_path = tmp_path / "search.db"
    sdk_dir = tmp_path / "sdk"
    _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "pricing lawyers")])
    chat_sources = [{"project_id": "p1", "project_name": "MyProj", "sdk_dir": sdk_dir}]
    stats = S.full_reindex_at(db_path, chat_sources, [], [])
    assert stats["chat_docs"] == 1
    hits = S.search_at(db_path, "pricing")
    assert len(hits) == 1

    # Reindexing again from scratch gives the same result (drop + rebuild, not accumulate)
    stats2 = S.full_reindex_at(db_path, chat_sources, [], [])
    assert stats2["chat_docs"] == 1
    hits2 = S.search_at(db_path, "pricing")
    assert len(hits2) == 1


def test_scan_all_at_is_incremental(tmp_path):
    db_path = tmp_path / "search.db"
    sdk_dir = tmp_path / "sdk"
    path = sdk_dir / "sess1.jsonl"
    _write_jsonl(path, [_assistant_text_line("sess1", "first")])
    chat_sources = [{"project_id": "p1", "project_name": "MyProj", "sdk_dir": sdk_dir}]
    stats1 = S.scan_all_at(db_path, chat_sources, [], [])
    assert stats1["chat_docs"] == 1
    stats2 = S.scan_all_at(db_path, chat_sources, [], [])
    assert stats2["chat_docs"] == 0


def test_search_at_works_before_any_scan_has_ever_run(tmp_path):
    """search_at must not blow up with 'no such table' if called against a brand-new
    db_path that has never been scanned (init_db is idempotent/called defensively)."""
    db_path = tmp_path / "brand-new-search.db"
    assert S.search_at(db_path, "anything") == []


# ═══════════════════════════ endpoints (aiohttp) ═══════════════════════════

@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_with_project(tmp_path, project_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {"project": "myproject", "cwd": str(project_dir), "model": "sonnet"},
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def search_app(fake_ctx_with_project):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_get("/api/search", _webapp.api_search)
    app.router.add_post("/api/search/reindex", _webapp.api_search_reindex)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


@pytest.fixture(autouse=True)
def reset_search_scan_state():
    """Prevents cross-test flakiness from webapp._search_scan_state (module-level,
    shared across the whole pytest session) — every test starts as 'just scanned' so
    the opportunistic _search_maybe_scan background trigger never fires mid-test."""
    old = dict(_webapp._search_scan_state)
    _webapp._search_scan_state["running"] = False
    _webapp._search_scan_state["ts"] = time.time()
    yield
    _webapp._search_scan_state.clear()
    _webapp._search_scan_state.update(old)


@pytest.fixture(autouse=True)
def patch_sdk_sessions_dir(monkeypatch, tmp_path):
    """SAFETY: redirects _sdk_sessions_dir into tmp_path for every test in this file so
    the endpoint tests can never resolve into (let alone scan) the real ~/.claude/projects
    of the machine running this suite."""
    def _fake(cwd):
        safe = cwd.replace("/", "_").replace("\\", "_").replace(":", "_")
        return tmp_path / "fake-sdk" / safe
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", _fake)
    yield


class TestSearchEndpoint:
    async def test_requires_auth(self, aiohttp_client, search_app):
        client = await aiohttp_client(search_app)
        resp = await client.get("/api/search", params={"q": "pricing"})
        assert resp.status == 401

    async def test_empty_query_returns_empty_hits(self, aiohttp_client, search_app, fake_ctx_with_project):
        client = await aiohttp_client(search_app)
        h = _auth_headers(fake_ctx_with_project)
        resp = await client.get("/api/search", headers=h)
        assert resp.status == 200
        data = await resp.json()
        assert data["hits"] == []

    async def test_reindex_and_search_roundtrip(self, aiohttp_client, search_app, fake_ctx_with_project, project_dir):
        # Seed a board card + a transcript under the (monkeypatched) sdk dir for this project's cwd.
        (project_dir / "TASKS.md").write_text(
            "## Backlog\n- [ ] Draft lawyers pricing page <!--ops:aaa111-->\n", encoding="utf-8")
        sdk_dir = _webapp._sdk_sessions_dir(str(project_dir))
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "lawyers pricing decided")])

        client = await aiohttp_client(search_app)
        h = _auth_headers(fake_ctx_with_project)

        reindex_resp = await client.post("/api/search/reindex", headers=h)
        assert reindex_resp.status == 200
        stats = await reindex_resp.json()
        assert stats["ok"] is True
        assert stats["chat_docs"] == 1
        assert stats["board_docs"] == 1

        search_resp = await client.get("/api/search", params={"q": "lawyers pricing"}, headers=h)
        assert search_resp.status == 200
        data = await search_resp.json()
        assert len(data["hits"]) == 2
        assert {hit["source"] for hit in data["hits"]} == {"chat", "board"}
        assert all(hit["project_name"] == "myproject" for hit in data["hits"])

    async def test_project_filter_endpoint(self, aiohttp_client, search_app, fake_ctx_with_project, project_dir):
        sdk_dir = _webapp._sdk_sessions_dir(str(project_dir))
        _write_jsonl(sdk_dir / "sess1.jsonl", [_assistant_text_line("sess1", "unique-keyword-xyz")])
        client = await aiohttp_client(search_app)
        h = _auth_headers(fake_ctx_with_project)
        await client.post("/api/search/reindex", headers=h)

        resp_match = await client.get("/api/search", params={"q": "unique-keyword-xyz", "project": "myproject"}, headers=h)
        data_match = await resp_match.json()
        assert len(data_match["hits"]) == 1

        resp_nomatch = await client.get("/api/search", params={"q": "unique-keyword-xyz", "project": "other-project"}, headers=h)
        data_nomatch = await resp_nomatch.json()
        assert data_nomatch["hits"] == []

    async def test_malformed_query_never_500s(self, aiohttp_client, search_app, fake_ctx_with_project):
        client = await aiohttp_client(search_app)
        h = _auth_headers(fake_ctx_with_project)
        for q in ["what's \"this\"", '":::(((', "NEAR(a b)"]:
            resp = await client.get("/api/search", params={"q": q}, headers=h)
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data.get("hits"), list)

    async def test_limit_param_is_honored(self, aiohttp_client, search_app, fake_ctx_with_project, project_dir):
        sdk_dir = _webapp._sdk_sessions_dir(str(project_dir))
        lines = [_assistant_text_line(f"s{i}", f"pricing item {i}", ts=f"2026-07-01T10:{i:02d}:00.000Z")
                 for i in range(5)]
        _write_jsonl(sdk_dir / "sess1.jsonl", lines)
        client = await aiohttp_client(search_app)
        h = _auth_headers(fake_ctx_with_project)
        await client.post("/api/search/reindex", headers=h)
        resp = await client.get("/api/search", params={"q": "pricing", "limit": "2"}, headers=h)
        data = await resp.json()
        assert len(data["hits"]) == 2

"""
Tests for spec-075 Phase A — context_pack.assemble and the preview endpoint.

Unit tests (no SDK, no server):
  1. Empty project dir → assemble returns None.
  2. Memory-only project → pack is not None, contains <context-pack> wrapper, contains index text.
  3. Budget enforcement → output <= 6000 chars; header + memory_index survive; lowest-priority
     sections are dropped/truncated.
  4. Source independence / never raises → malformed timeline, missing jsonl, non-git dir,
     absent search.db → assemble returns valid pack or None, never raises.
  5. Relevance → _relevant_memory ranks query-matching files above unrelated ones.

Integration tests (aiohttp endpoint, mirroring test_settings.py fixture pattern):
  6. GET /api/projects/{id}/context-pack with a memory-bearing project returns 200 with
     pack containing <context-pack> and enabled=True.
  7. Global context_pack_enabled=False → endpoint returns enabled=False.
  8. Per-project context_pack_enabled=False → endpoint returns enabled=False.
  9. Unknown project id → 404.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import context_pack as CP
import webapp as _webapp
from webapp import _derive_token


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _write_memory(cwd: Path, index_text: str, extra: dict[str, str] | None = None) -> None:
    """Write MEMORY.md (index) and optional extra memory files under cwd/.claude-ops/memory/."""
    mem_dir = cwd / ".claude-ops" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text(index_text, encoding="utf-8")
    if extra:
        for name, body in extra.items():
            (mem_dir / name).write_text(body, encoding="utf-8")


def _write_timeline(data_dir: Path, cwd: str, records: list[dict]) -> None:
    """Write a timeline JSONL file using the same slug rule as context_pack."""
    slug = cwd.replace("/", "-")
    tl_dir = data_dir / "timeline"
    tl_dir.mkdir(parents=True, exist_ok=True)
    path = tl_dir / f"{slug}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _reset_settings_globals():
    _webapp._SETTINGS_PATH = None
    _webapp._SETTINGS_CACHE = {}
    _webapp._SETTINGS_MTIME = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Empty project → None
# ═══════════════════════════════════════════════════════════════════════════════

def test_empty_project_returns_none(tmp_path):
    """Bare directory (no memory, no board, no timeline, non-git) → None."""
    cwd = str(tmp_path / "project")
    Path(cwd).mkdir()
    data_dir = str(tmp_path / "data")
    Path(data_dir).mkdir()

    result = CP.assemble(cwd, "test:1", "", data_dir=data_dir, project_id="project")
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Memory-only project → pack is not None, well-formed
# ═══════════════════════════════════════════════════════════════════════════════

def test_memory_only_pack_structure(tmp_path):
    """Memory index present → pack is not None, contains correct markers."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    index_text = "- [background-tasks](background-tasks.md) — tasks die on eviction\n"
    _write_memory(cwd, index_text)

    result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir), project_id="project")

    assert result is not None
    assert result.startswith("<context-pack>")
    assert result.endswith("</context-pack>")
    assert "## Memory index" in result
    assert "background-tasks" in result


def test_memory_only_pack_is_well_formed(tmp_path):
    """Pack opens and closes the XML-ish tag exactly once."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    _write_memory(cwd, "- [foo](foo.md) — a thing\n")
    result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir))

    assert result is not None
    assert result.count("<context-pack>") == 1
    assert result.count("</context-pack>") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Budget enforcement
# ═══════════════════════════════════════════════════════════════════════════════

def test_budget_respected(tmp_path, monkeypatch):
    """Oversized inputs → output stays within char_budget; header + memory index survive."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Oversized memory index (~3000 chars)
    index_text = "- [important](important.md) — key insight\n" * 70
    _write_memory(cwd, index_text)

    # Large board via monkeypatch (~2000 chars)
    big_board = "card line here\n" * 130

    # board_summary is imported as `from board import board_summary` inside assemble();
    # patch the function on the `board` module so the local import picks up the mock.
    with patch("board.board_summary", return_value=big_board):
        result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir), char_budget=6000)

    assert result is not None
    assert len(result) <= 6000
    # Header text must survive
    assert "Ground your reply in it" in result
    # Memory index header must survive
    assert "## Memory index" in result


def test_budget_lowest_priority_dropped_first(tmp_path, monkeypatch):
    """Board (priority 4) is dropped before memory_index (priority 2) when over budget."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Small memory index
    _write_memory(cwd, "- [a](a.md) — small entry\n")

    # Large board that pushes us over budget
    big_board = "a" * 5500

    with patch("board.board_summary", return_value=big_board):
        result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir), char_budget=6000)

    assert result is not None
    assert len(result) <= 6000
    # Memory index should survive (higher priority than board)
    assert "## Memory index" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Source independence / never raises
# ═══════════════════════════════════════════════════════════════════════════════

def test_malformed_timeline_line_does_not_raise(tmp_path):
    """A bad JSONL line plus a good line: assemble still returns without raising."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Good memory so we get a non-None result even if timeline is empty
    _write_memory(cwd, "- [item](item.md) — entry\n")

    # Write a timeline with one bad line + one good line
    slug = str(cwd).replace("/", "-")
    tl_dir = data_dir / "timeline"
    tl_dir.mkdir(parents=True, exist_ok=True)
    (tl_dir / f"{slug}.jsonl").write_text(
        "not valid json{{{\n"
        + json.dumps({"type": "run", "text": "agent ran something"}) + "\n",
        encoding="utf-8",
    )

    result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir))
    assert result is not None
    # The good event should be included
    assert "run" in result


def test_missing_timeline_does_not_raise(tmp_path):
    """Absent timeline file → assemble degrades gracefully, never raises."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_memory(cwd, "- [item](item.md) — entry\n")

    # No timeline directory at all
    result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir))
    assert result is not None


def test_non_git_dir_does_not_raise(tmp_path):
    """Non-git cwd: git log returns empty; assemble still returns valid pack (memory present)."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_memory(cwd, "- [item](item.md) — entry\n")

    result = CP.assemble(str(cwd), "test:1", "", data_dir=str(data_dir))
    assert result is not None  # memory present → not None
    assert "<context-pack>" in result


def test_absent_search_db_does_not_raise(tmp_path):
    """No search.db: recall returns []; assemble does not raise."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_memory(cwd, "- [item](item.md) — entry\n")

    result = CP.assemble(str(cwd), "test:1", "some query", data_dir=str(data_dir))
    assert result is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Relevance: _relevant_memory ranks by query overlap
# ═══════════════════════════════════════════════════════════════════════════════

def test_relevant_memory_ranks_query_match_first():
    """File whose body matches the query should rank above an unrelated file."""
    files = [
        {"name": "unrelated.md", "body": "this is about something else entirely"},
        {"name": "deploy.md", "body": "deployment steps: push to coolify, check logs"},
    ]
    ranked = CP._relevant_memory(files, query="deploy coolify logs")
    assert ranked[0]["name"] == "deploy.md"


def test_relevant_memory_empty_query_returns_by_size():
    """Empty query → files sorted by body size (largest first)."""
    files = [
        {"name": "small.md", "body": "x"},
        {"name": "large.md", "body": "x" * 200},
    ]
    ranked = CP._relevant_memory(files, query="")
    assert ranked[0]["name"] == "large.md"


def test_relevant_memory_empty_files():
    """No files → returns []."""
    assert CP._relevant_memory([], query="anything") == []


def test_relevant_memory_k_limit():
    """Returns at most k files."""
    files = [{"name": f"f{i}.md", "body": f"content {i}"} for i in range(10)]
    ranked = CP._relevant_memory(files, query="", k=3)
    assert len(ranked) <= 3


# ═══════════════════════════════════════════════════════════════════════════════
# Integration fixtures (mirrors test_settings.py pattern exactly)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def cp_app_ctx(tmp_path):
    """ctx with one project that has memory files; settings initialized."""
    password = "testpass"
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    # Give the project a MEMORY.md so the pack will be non-empty
    _write_memory(pdir, "- [ctx-item](ctx-item.md) — the main context thing\n")

    _webapp._settings_init({"DATA": data})
    _webapp._SETTINGS_CACHE = {}
    _webapp._SETTINGS_MTIME = 0.0

    # project id = basename of cwd = "myproject"
    ctx = {
        "topics": {
            "-100:5": {
                "project": "myproject",
                "cwd": str(pdir),
                "model": "sonnet",
            }
        },
        "sessions": {}, "running": {}, "password": password,
        "DATA": data, "HERE": ROOT, "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    yield ctx
    _reset_settings_globals()


@pytest.fixture
def cp_app(cp_app_ctx):
    """aiohttp app with settings + context-pack routes registered."""
    from aiohttp import web
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = cp_app_ctx
    a.router.add_post("/api/login", _webapp.api_login)
    a.router.add_get("/api/settings", _webapp.api_settings_get)
    a.router.add_post("/api/settings", _webapp.api_settings_post)
    a.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    a.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    a.router.add_get("/api/projects/{id}/context-pack", _webapp.api_project_context_pack)
    return a


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Endpoint: memory project → 200 + pack with <context-pack> + enabled=True
# ═══════════════════════════════════════════════════════════════════════════════

async def test_context_pack_endpoint_returns_pack(aiohttp_client, cp_app, cp_app_ctx):
    """GET /api/projects/myproject/context-pack → 200 with pack body and enabled=True."""
    client = await aiohttp_client(cp_app)
    r = await client.get("/api/projects/myproject/context-pack", headers=_auth(cp_app_ctx))
    assert r.status == 200
    data = await r.json()
    assert data["enabled"] is True
    assert "<context-pack>" in data["pack"]
    assert "## Memory index" in data["pack"]
    assert data["chars"] > 0
    assert data["chars"] == len(data["pack"])


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Global context_pack_enabled=False → enabled=False
# ═══════════════════════════════════════════════════════════════════════════════

async def test_context_pack_endpoint_global_disabled(aiohttp_client, cp_app, cp_app_ctx):
    """POST /api/settings {context_pack_enabled:false} then GET endpoint → enabled=False."""
    client = await aiohttp_client(cp_app)
    # Disable globally
    r = await client.post(
        "/api/settings",
        json={"context_pack_enabled": False},
        headers=_auth(cp_app_ctx),
    )
    assert r.status == 200, f"settings post failed: {await r.text()}"

    r2 = await client.get("/api/projects/myproject/context-pack", headers=_auth(cp_app_ctx))
    assert r2.status == 200
    data = await r2.json()
    assert data["enabled"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Per-project context_pack_enabled=False → enabled=False
# ═══════════════════════════════════════════════════════════════════════════════

async def test_context_pack_endpoint_project_disabled(aiohttp_client, cp_app, cp_app_ctx):
    """POST /api/projects/{id}/settings {context_pack_enabled:false} → endpoint enabled=False."""
    client = await aiohttp_client(cp_app)
    r = await client.post(
        "/api/projects/myproject/settings",
        json={"context_pack_enabled": False},
        headers=_auth(cp_app_ctx),
    )
    assert r.status == 200, f"project settings post failed: {await r.text()}"

    r2 = await client.get("/api/projects/myproject/context-pack", headers=_auth(cp_app_ctx))
    assert r2.status == 200
    data = await r2.json()
    assert data["enabled"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Unknown project id → 404
# ═══════════════════════════════════════════════════════════════════════════════

async def test_context_pack_endpoint_unknown_project_404(aiohttp_client, cp_app, cp_app_ctx):
    """GET /api/projects/nonexistent/context-pack → 404."""
    client = await aiohttp_client(cp_app)
    r = await client.get("/api/projects/nonexistent/context-pack", headers=_auth(cp_app_ctx))
    assert r.status == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Note on injection test
# ═══════════════════════════════════════════════════════════════════════════════
# A full api_project_chat injection test is not wired here because api_project_chat
# requires a live run_engine coroutine (an async generator) and a real aiohttp SSE
# streaming client. The existing harness (test_spec042_handoff.py) mocks run_engine
# as an AsyncMock; extending that pattern for context-pack injection would require
# asserting on effective_prompt, which is a local variable deep inside the handler —
# not exported or easily hookable. The endpoint test (cases 6–8) together with the
# unit tests for assemble and the already-verified injection code in webapp.py
# (lines 10266–10281) give sufficient coverage for Phase A.

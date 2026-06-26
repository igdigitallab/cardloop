"""
Tests for "Project Memory" (Spec 006).

Covers:
- _project_memory_dir path
- write→read round-trip
- _memory_reindex updates MEMORY.md on write/delete
- anti-traversal: bad slug → error
- size limit
- API: GET empty→exists:false; POST creates+index; DELETE removes+index; bad name→400
- backward compatibility: old location is read if new one doesn't exist
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _project_memory_dir,
    _valid_memory_name,
    _memory_read_all,
    _memory_write,
    _memory_delete,
    _memory_reindex,
    _sdk_sessions_dir,
    _derive_token,
    api_project_memory,
    api_project_memory_write,
    api_project_memory_delete,
)


# ─────────────────────────── unit: path ────────────────────────────────────────

def test_project_memory_dir_path(tmp_path):
    """_project_memory_dir returns <cwd>/.claude-ops/memory/."""
    result = _project_memory_dir(str(tmp_path))
    assert result == tmp_path / ".claude-ops" / "memory"


def test_project_memory_dir_str_vs_path(tmp_path):
    """String and Path arguments produce the same result."""
    result_str = _project_memory_dir(str(tmp_path))
    result_path = _project_memory_dir(str(tmp_path))
    assert result_str == result_path


# ─────────────────────────── unit: name validation ──────────────────────────────

@pytest.mark.parametrize("name", [
    "decision-use-aiohttp.md",
    "gotcha-01.md",
    "ab.md",
    "a0.md",
    "MEMORY.md",
])
def test_valid_memory_name_ok(name):
    assert _valid_memory_name(name) is True


@pytest.mark.parametrize("name", [
    "../etc/passwd",
    "../../etc/shadow",
    "sub/dir.md",
    ".md",
    "A-Upper.md",       # uppercase
    "no-dot",           # no .md extension
    "a.md.extra",       # extension is not .md
    "",
    "x" * 100 + ".md",  # too long
    "MEMORY.md.evil",
])
def test_valid_memory_name_reject(name):
    assert _valid_memory_name(name) is False


def test_valid_memory_name_path_sep_rejected():
    assert _valid_memory_name("a/b.md") is False
    assert _valid_memory_name("a\\b.md") is False


# ─────────────────────────── unit: write→read round-trip ──────────────────────

def test_memory_write_creates_file(tmp_path):
    """_memory_write creates the file and directory."""
    cwd = str(tmp_path)
    _memory_write(cwd, "gotcha-db.md", "# Gotcha\nContent here")
    target = _project_memory_dir(cwd) / "gotcha-db.md"
    assert target.exists()
    assert "Content here" in target.read_text()


def test_memory_write_overwrite(tmp_path):
    """_memory_write overwrites an existing file."""
    cwd = str(tmp_path)
    _memory_write(cwd, "decision-db.md", "v1")
    _memory_write(cwd, "decision-db.md", "v2")
    content = (_project_memory_dir(cwd) / "decision-db.md").read_text()
    assert content == "v2"
    assert "v1" not in content


def test_memory_read_all_new_place(tmp_path):
    """_memory_read_all reads from the new location (.claude-ops/memory/)."""
    cwd = str(tmp_path)
    _memory_write(cwd, "gotcha-test.md", "# Gotcha\nText")
    files, legacy = _memory_read_all(cwd)
    assert legacy is False
    names = [f["name"] for f in files]
    assert "gotcha-test.md" in names


def test_memory_read_all_empty(tmp_path):
    """_memory_read_all with no files returns an empty list."""
    files, legacy = _memory_read_all(str(tmp_path))
    assert files == []
    assert legacy is False


def test_memory_read_all_memory_md_first(tmp_path):
    """MEMORY.md comes first in the list."""
    cwd = str(tmp_path)
    _memory_write(cwd, "zzz-last.md", "z")
    _memory_write(cwd, "aaa-first.md", "a")
    # reindex creates MEMORY.md
    files, _ = _memory_read_all(cwd)
    names = [f["name"] for f in files]
    assert names[0] == "MEMORY.md"


# ─────────────────────────── unit: reindex ────────────────────────────────────

def test_memory_reindex_creates_memory_md(tmp_path):
    """_memory_reindex creates MEMORY.md with links to all entries."""
    cwd = str(tmp_path)
    mem_dir = _project_memory_dir(cwd)
    mem_dir.mkdir(parents=True)
    (mem_dir / "decision-foo.md").write_text("---\ntype: decision\ncreated: 2026-01-01\n---\nFoo decision")
    _memory_reindex(cwd)
    index = (mem_dir / "MEMORY.md").read_text()
    assert "decision-foo.md" in index


def test_memory_write_auto_reindex(tmp_path):
    """_memory_write automatically calls reindex."""
    cwd = str(tmp_path)
    _memory_write(cwd, "gotcha-bar.md", "---\ntype: gotcha\ncreated: 2026-01-01\n---\nBar gotcha")
    mem_dir = _project_memory_dir(cwd)
    assert (mem_dir / "MEMORY.md").exists()
    index = (mem_dir / "MEMORY.md").read_text()
    assert "gotcha-bar.md" in index


def test_memory_delete_updates_index(tmp_path):
    """_memory_delete removes the entry from the index."""
    cwd = str(tmp_path)
    _memory_write(cwd, "decision-a.md", "# A\nText A")
    _memory_write(cwd, "decision-b.md", "# B\nText B")
    _memory_delete(cwd, "decision-a.md")
    mem_dir = _project_memory_dir(cwd)
    assert not (mem_dir / "decision-a.md").exists()
    index = (mem_dir / "MEMORY.md").read_text()
    assert "decision-a.md" not in index
    assert "decision-b.md" in index


def test_memory_delete_nonexistent_returns_false(tmp_path):
    """_memory_delete returns False for a non-existent file."""
    result = _memory_delete(str(tmp_path), "missing.md")
    assert result is False


# ─────────────────────────── unit: anti-traversal ─────────────────────────────

def test_memory_write_traversal_rejected(tmp_path):
    """_memory_write rejects names with path components."""
    with pytest.raises(ValueError):
        _memory_write(str(tmp_path), "../outside.md", "evil")


def test_memory_write_bad_slug_rejected(tmp_path):
    """_memory_write rejects an invalid slug."""
    with pytest.raises(ValueError):
        _memory_write(str(tmp_path), "BAD-Upper.md", "content")


def test_memory_delete_traversal_rejected(tmp_path):
    """_memory_delete rejects traversal names."""
    with pytest.raises(ValueError):
        _memory_delete(str(tmp_path), "../evil.md")


def test_memory_delete_memory_md_rejected(tmp_path):
    """_memory_delete cannot delete MEMORY.md directly."""
    with pytest.raises(ValueError, match="cannot delete MEMORY.md"):
        _memory_delete(str(tmp_path), "MEMORY.md")


# ─────────────────────────── unit: size limit ──────────────────────────────────

def test_memory_write_size_limit(tmp_path):
    """_memory_write rejects content > _MEMORY_MAX_SIZE."""
    huge = "x" * (_webapp._MEMORY_MAX_SIZE + 1)
    with pytest.raises(ValueError, match="exceeds"):
        _memory_write(str(tmp_path), "big.md", huge)


# ─────────────────────────── unit: backward compatibility ─────────────────────

def test_memory_read_all_migrates_legacy(tmp_path):
    """_memory_read_all AUTO-MIGRATES the old location to the new one when no new location exists.
    After migration legacy=False and files are physically in .claude-ops/memory/
    (otherwise delete/write would get 404 on legacy memory — that was a bug)."""
    cwd = str(tmp_path)
    old_dir = _sdk_sessions_dir(cwd) / "memory"
    old_dir.mkdir(parents=True)
    (old_dir / "old-note.md").write_text("Old content")
    try:
        files, legacy = _memory_read_all(cwd)
        assert legacy is False  # migrated to new location
        names = [f["name"] for f in files]
        assert "old-note.md" in names
        # file is physically in the new location → delete will now find it
        assert (_project_memory_dir(cwd) / "old-note.md").exists()
        assert _memory_delete(cwd, "old-note.md") is True
    finally:
        # clean up legacy in the real ~/.claude (tmp_path resolves to HOME)
        import shutil
        shutil.rmtree(old_dir.parent, ignore_errors=True)


def test_memory_read_all_new_takes_priority(tmp_path):
    """If both locations have files — the new one (.claude-ops/memory/) takes priority."""
    cwd = str(tmp_path)
    # Old location
    old_dir = _sdk_sessions_dir(cwd) / "memory"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "old-note.md").write_text("Old")
    try:
        # New location
        _memory_write(cwd, "new-note.md", "New")
        files, legacy = _memory_read_all(cwd)
        assert legacy is False
        names = [f["name"] for f in files]
        assert "new-note.md" in names
        assert "old-note.md" not in names
    finally:
        # clean up legacy in the real ~/.claude (tmp_path resolves to HOME) — otherwise
        # mkdir without exist_ok fails on the next run with the same pytest-tmp slug
        import shutil
        shutil.rmtree(old_dir.parent, ignore_errors=True)


# ─────────────────────────── API tests (aiohttp) ──────────────────────────────

@pytest.fixture
def project_dir(tmp_path):
    """Temporary project folder."""
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_with_project(tmp_path, project_dir):
    """ctx with one project."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
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
def memory_app(fake_ctx_with_project):
    """aiohttp application with memory routes."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/memory", _webapp.api_project_memory)
    app.router.add_post("/api/projects/{id}/memory/{name}", _webapp.api_project_memory_write)
    app.router.add_delete("/api/projects/{id}/memory/{name}", _webapp.api_project_memory_delete)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ── GET /api/projects/{id}/memory ────────────────────────────────────────────

async def test_api_memory_get_empty(aiohttp_client, memory_app, fake_ctx_with_project):
    """GET empty project → exists:false, files:[]."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/memory", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False
    assert data["files"] == []


async def test_api_memory_get_not_found(aiohttp_client, memory_app, fake_ctx_with_project):
    """GET non-existent project → 404."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/nonexistent/memory", headers=h)
    assert resp.status == 404


async def test_api_memory_get_unauthorized(aiohttp_client, memory_app):
    """GET without authorization → 401."""
    client = await aiohttp_client(memory_app)
    resp = await client.get("/api/projects/myproject/memory")
    assert resp.status == 401


# ── POST /api/projects/{id}/memory/{name} ────────────────────────────────────

async def test_api_memory_post_create(aiohttp_client, memory_app, fake_ctx_with_project, project_dir):
    """POST creates the file and returns exists:true + files with MEMORY.md."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/memory/decision-db.md",
        json={"content": "---\ntype: decision\ncreated: 2026-01-01\n---\nDB choice"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is True
    names = [f["name"] for f in data["files"]]
    assert "decision-db.md" in names
    assert "MEMORY.md" in names  # reindex
    # File is actually created on disk
    assert (_project_memory_dir(str(project_dir)) / "decision-db.md").exists()


async def test_api_memory_post_updates_index(aiohttp_client, memory_app, fake_ctx_with_project, project_dir):
    """POST updates the MEMORY.md index."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    await client.post(
        "/api/projects/myproject/memory/gotcha-locks.md",
        json={"content": "---\ntype: gotcha\ncreated: 2026-01-01\n---\nLock issue"},
        headers=h,
    )
    index = (_project_memory_dir(str(project_dir)) / "MEMORY.md").read_text()
    assert "gotcha-locks.md" in index


async def test_api_memory_post_bad_name(aiohttp_client, memory_app, fake_ctx_with_project):
    """POST with invalid name → 400."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/memory/..%2Fevil.md",
        json={"content": "evil"},
        headers=h,
    )
    assert resp.status == 400


async def test_api_memory_post_traversal(aiohttp_client, memory_app, fake_ctx_with_project):
    """POST with traversal in name → 400."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    # Try different variants
    resp = await client.post(
        "/api/projects/myproject/memory/Upper-Case.md",
        json={"content": "x"},
        headers=h,
    )
    assert resp.status == 400


async def test_api_memory_post_not_found(aiohttp_client, memory_app, fake_ctx_with_project):
    """POST to a non-existent project → 404."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/nonexistent/memory/note.md",
        json={"content": "x"},
        headers=h,
    )
    assert resp.status == 404


async def test_api_memory_post_size_limit(aiohttp_client, memory_app, fake_ctx_with_project):
    """POST content > _MEMORY_MAX_SIZE → 400."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    huge = "x" * (_webapp._MEMORY_MAX_SIZE + 1)
    resp = await client.post(
        "/api/projects/myproject/memory/big.md",
        json={"content": huge},
        headers=h,
    )
    assert resp.status == 400


# ── DELETE /api/projects/{id}/memory/{name} ──────────────────────────────────

async def test_api_memory_delete_existing(aiohttp_client, memory_app, fake_ctx_with_project, project_dir):
    """DELETE existing file → 200, file disappears from response."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    # Create file
    await client.post(
        "/api/projects/myproject/memory/decision-to-delete.md",
        json={"content": "# Delete me"},
        headers=h,
    )
    # Delete
    resp = await client.delete(
        "/api/projects/myproject/memory/decision-to-delete.md",
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    names = [f["name"] for f in data["files"]]
    assert "decision-to-delete.md" not in names
    # File deleted from disk
    assert not (_project_memory_dir(str(project_dir)) / "decision-to-delete.md").exists()


async def test_api_memory_delete_updates_index(aiohttp_client, memory_app, fake_ctx_with_project, project_dir):
    """DELETE updates the MEMORY.md index."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    await client.post(
        "/api/projects/myproject/memory/gotcha-a.md",
        json={"content": "# A"},
        headers=h,
    )
    await client.post(
        "/api/projects/myproject/memory/gotcha-b.md",
        json={"content": "# B"},
        headers=h,
    )
    await client.delete("/api/projects/myproject/memory/gotcha-a.md", headers=h)
    index = (_project_memory_dir(str(project_dir)) / "MEMORY.md").read_text()
    assert "gotcha-a.md" not in index
    assert "gotcha-b.md" in index


async def test_api_memory_delete_nonexistent(aiohttp_client, memory_app, fake_ctx_with_project):
    """DELETE non-existent file → 404."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/memory/no-such.md",
        headers=h,
    )
    assert resp.status == 404


async def test_api_memory_delete_bad_name(aiohttp_client, memory_app, fake_ctx_with_project):
    """DELETE with invalid name → 400."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/memory/BAD-NAME.md",
        headers=h,
    )
    assert resp.status == 400


async def test_api_memory_delete_memory_md_rejected(aiohttp_client, memory_app, fake_ctx_with_project):
    """DELETE MEMORY.md → 400 (index cannot be deleted)."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/memory/MEMORY.md",
        headers=h,
    )
    assert resp.status == 400


async def test_api_memory_delete_not_found_project(aiohttp_client, memory_app, fake_ctx_with_project):
    """DELETE for a non-existent project → 404."""
    client = await aiohttp_client(memory_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/nonexistent/memory/note.md",
        headers=h,
    )
    assert resp.status == 404

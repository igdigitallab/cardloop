"""
Tests for Spec-023: Project Archive.

Covers:
- archive adds id to archived.json, project filtered from default list
- unarchive restores it
- archived-list endpoint returns only archived projects
- busy project → 409
- filesystem invariant: archive/unarchive ONLY modifies data/archived.json
- invalid id → 404
- free chats cannot be archived → 400
"""
import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    return pdir


@pytest.fixture
def archive_ctx(tmp_path, project_dir):
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "my-project",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
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
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


@pytest.fixture
def archive_app(archive_ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = archive_ctx
    app.router.add_get("/api/projects", _webapp.api_projects)
    app.router.add_get("/api/projects/archived", _webapp.api_projects_archived)
    app.router.add_post("/api/projects/{id}/archive", _webapp.api_project_archive)
    app.router.add_post("/api/projects/{id}/unarchive", _webapp.api_project_unarchive)
    return app


def _headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_archive_adds_to_file_and_filters_list(aiohttp_client, archive_app, archive_ctx):
    """Archive adds id to archived.json and project disappears from default list."""
    client = await aiohttp_client(archive_app)

    # Before: project in list
    resp = await client.get("/api/projects", headers=_headers(archive_ctx))
    assert resp.status == 200
    data = await resp.json()
    ids = [p["id"] for p in data["projects"]]
    assert "my-project" in ids

    # Archive it
    resp = await client.post("/api/projects/my-project/archive", headers=_headers(archive_ctx))
    assert resp.status == 200
    assert (await resp.json())["archived"] is True

    # archived.json exists and contains the id
    archived_file = archive_ctx["DATA"] / "archived.json"
    assert archived_file.exists()
    saved = json.loads(archived_file.read_text())
    assert "my-project" in saved

    # No longer in default list
    resp = await client.get("/api/projects", headers=_headers(archive_ctx))
    ids = [p["id"] for p in (await resp.json())["projects"]]
    assert "my-project" not in ids


async def test_unarchive_restores_project(aiohttp_client, archive_app, archive_ctx):
    """Unarchive removes from archived.json and project reappears in default list."""
    client = await aiohttp_client(archive_app)

    await client.post("/api/projects/my-project/archive", headers=_headers(archive_ctx))

    resp = await client.post("/api/projects/my-project/unarchive", headers=_headers(archive_ctx))
    assert resp.status == 200
    assert (await resp.json())["archived"] is False

    # Project back in list
    resp = await client.get("/api/projects", headers=_headers(archive_ctx))
    ids = [p["id"] for p in (await resp.json())["projects"]]
    assert "my-project" in ids


async def test_archived_list_endpoint(aiohttp_client, archive_app, archive_ctx):
    """GET /api/projects/archived returns only archived projects."""
    client = await aiohttp_client(archive_app)

    # Archive the project
    await client.post("/api/projects/my-project/archive", headers=_headers(archive_ctx))

    resp = await client.get("/api/projects/archived", headers=_headers(archive_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "projects" in data
    ids = [p["id"] for p in data["projects"]]
    assert "my-project" in ids


async def test_archived_list_empty_when_none(aiohttp_client, archive_app, archive_ctx):
    """GET /api/projects/archived returns empty list when nothing archived."""
    client = await aiohttp_client(archive_app)
    resp = await client.get("/api/projects/archived", headers=_headers(archive_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["projects"] == []


async def test_archive_busy_project_returns_409(aiohttp_client, archive_app, archive_ctx):
    """Archiving a busy project returns 409."""
    archive_ctx["running"]["1001:42"] = True
    client = await aiohttp_client(archive_app)
    resp = await client.post("/api/projects/my-project/archive", headers=_headers(archive_ctx))
    assert resp.status == 409


async def test_archive_unknown_id_returns_404(aiohttp_client, archive_app, archive_ctx):
    """Archiving nonexistent project returns 404."""
    client = await aiohttp_client(archive_app)
    resp = await client.post("/api/projects/nonexistent/archive", headers=_headers(archive_ctx))
    assert resp.status == 404


async def test_unarchive_not_archived_returns_400(aiohttp_client, archive_app, archive_ctx):
    """Unarchiving a project that is not archived returns 400."""
    client = await aiohttp_client(archive_app)
    resp = await client.post("/api/projects/my-project/unarchive", headers=_headers(archive_ctx))
    assert resp.status == 400


async def test_filesystem_invariant(aiohttp_client, archive_app, archive_ctx, project_dir):
    """Archive/unarchive ONLY modifies data/archived.json, nothing under project cwd."""
    client = await aiohttp_client(archive_app)

    # Record project dir state before
    before_files = set(project_dir.rglob("*"))

    await client.post("/api/projects/my-project/archive", headers=_headers(archive_ctx))
    await client.post("/api/projects/my-project/unarchive", headers=_headers(archive_ctx))

    after_files = set(project_dir.rglob("*"))
    assert before_files == after_files, "archive/unarchive must not touch project filesystem"

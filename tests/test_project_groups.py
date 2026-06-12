"""
Tests for Spec-024: Project Groups.

Covers:
- assign project to group (auto-creates if new)
- null assignment → ungrouped
- GET /api/project-groups returns groups+assignments
- GET /api/projects includes group field
- delete group → members ungrouped (POST /api/project-groups with new list)
- reorder persists
- filesystem invariant: group ops ONLY modify data/project_groups.json
- invalid/empty label → 400
- unknown project id → 404
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
def groups_ctx(tmp_path, project_dir):
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
def groups_app(groups_ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = groups_ctx
    app.router.add_get("/api/projects", _webapp.api_projects)
    app.router.add_get("/api/project-groups", _webapp.api_project_groups_get)
    app.router.add_post("/api/project-groups", _webapp.api_project_groups_manage)
    app.router.add_post("/api/projects/{id}/group", _webapp.api_project_group_set)
    return app


def _headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_assign_project_to_group_auto_creates(aiohttp_client, groups_app, groups_ctx):
    """Assigning project to a new group auto-creates that group."""
    client = await aiohttp_client(groups_app)

    resp = await client.post(
        "/api/projects/my-project/group",
        json={"group": "Frontend"},
        headers=_headers(groups_ctx),
    )
    assert resp.status == 200

    # Group should now exist
    resp = await client.get("/api/project-groups", headers=_headers(groups_ctx))
    data = await resp.json()
    assert "Frontend" in data["groups"]
    assert data["assignments"].get("my-project") == "Frontend"


async def test_null_assignment_removes_group(aiohttp_client, groups_app, groups_ctx):
    """Setting group to null removes the project from any group."""
    client = await aiohttp_client(groups_app)

    await client.post("/api/projects/my-project/group", json={"group": "Frontend"}, headers=_headers(groups_ctx))
    resp = await client.post("/api/projects/my-project/group", json={"group": None}, headers=_headers(groups_ctx))
    assert resp.status == 200

    resp = await client.get("/api/project-groups", headers=_headers(groups_ctx))
    data = await resp.json()
    assert "my-project" not in data["assignments"]


async def test_get_project_groups_returns_groups_and_assignments(aiohttp_client, groups_app, groups_ctx):
    """GET /api/project-groups returns the full groups+assignments structure."""
    client = await aiohttp_client(groups_app)

    await client.post("/api/projects/my-project/group", json={"group": "Backend"}, headers=_headers(groups_ctx))

    resp = await client.get("/api/project-groups", headers=_headers(groups_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "groups" in data
    assert "assignments" in data
    assert "Backend" in data["groups"]


async def test_projects_includes_group_field(aiohttp_client, groups_app, groups_ctx):
    """GET /api/projects includes group field for each project."""
    client = await aiohttp_client(groups_app)

    await client.post("/api/projects/my-project/group", json={"group": "MyGroup"}, headers=_headers(groups_ctx))

    resp = await client.get("/api/projects", headers=_headers(groups_ctx))
    assert resp.status == 200
    projects = (await resp.json())["projects"]
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("group") == "MyGroup"


async def test_delete_group_ungroupes_members(aiohttp_client, groups_app, groups_ctx):
    """Deleting a group (omitting it from POST /api/project-groups) ungroups its members."""
    client = await aiohttp_client(groups_app)

    await client.post("/api/projects/my-project/group", json={"group": "ToDelete"}, headers=_headers(groups_ctx))

    # Delete group by posting a list without it
    resp = await client.post("/api/project-groups", json={"groups": []}, headers=_headers(groups_ctx))
    assert resp.status == 200

    # Project should now have no group
    resp = await client.get("/api/projects", headers=_headers(groups_ctx))
    projects = (await resp.json())["projects"]
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("group") is None


async def test_reorder_groups_persists(aiohttp_client, groups_app, groups_ctx):
    """POST /api/project-groups with reordered list persists new order."""
    client = await aiohttp_client(groups_app)

    await client.post("/api/project-groups", json={"groups": ["A", "B", "C"]}, headers=_headers(groups_ctx))

    resp = await client.post("/api/project-groups", json={"groups": ["C", "A", "B"]}, headers=_headers(groups_ctx))
    assert resp.status == 200

    resp = await client.get("/api/project-groups", headers=_headers(groups_ctx))
    data = await resp.json()
    assert data["groups"] == ["C", "A", "B"]


async def test_empty_group_label_returns_400(aiohttp_client, groups_app, groups_ctx):
    """Empty string group label returns 400."""
    client = await aiohttp_client(groups_app)
    resp = await client.post(
        "/api/projects/my-project/group",
        json={"group": ""},
        headers=_headers(groups_ctx),
    )
    assert resp.status == 400


async def test_unknown_project_returns_404(aiohttp_client, groups_app, groups_ctx):
    """Setting group on nonexistent project returns 404."""
    client = await aiohttp_client(groups_app)
    resp = await client.post(
        "/api/projects/nonexistent/group",
        json={"group": "Foo"},
        headers=_headers(groups_ctx),
    )
    assert resp.status == 404


async def test_filesystem_invariant(aiohttp_client, groups_app, groups_ctx, project_dir):
    """Group ops ONLY modify data/project_groups.json, nothing under project cwd."""
    client = await aiohttp_client(groups_app)

    before_files = set(project_dir.rglob("*"))

    await client.post("/api/projects/my-project/group", json={"group": "TestGroup"}, headers=_headers(groups_ctx))
    await client.post("/api/projects/my-project/group", json={"group": None}, headers=_headers(groups_ctx))

    after_files = set(project_dir.rglob("*"))
    assert before_files == after_files, "group ops must not touch project filesystem"

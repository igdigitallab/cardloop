"""
Tests for the api_project_label endpoint (POST /api/projects/{id}/label).

Verifies:
- happy path: display name updated in topics, id and cwd unchanged, 200 body {ok, id, name}
- unicode / emoji / space names accepted
- empty name → 400
- name longer than 80 chars → 400
- unknown id → 404
- two topics entries sharing the same cwd both get the new name, neither gets a changed cwd
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    return pdir


@pytest.fixture
def label_ctx(tmp_path, project_dir):
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
def label_app(label_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = label_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_post("/api/projects/{id}/label", _webapp.api_project_label)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── tests ───────────────────────────────


async def test_label_happy_path(aiohttp_client, label_app, label_ctx, project_dir):
    """Valid name → 200, project field updated, id and cwd unchanged."""
    client = await aiohttp_client(label_app)

    resp = await client.post(
        "/api/projects/my-project/label",
        json={"name": "My Shiny Project"},
        headers=_auth_headers(label_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert data.get("id") == "my-project"
    assert data.get("name") == "My Shiny Project"

    # topics entry updated
    topic = label_ctx["topics"]["1001:42"]
    assert topic["project"] == "My Shiny Project"
    # cwd must not change
    assert topic["cwd"] == str(project_dir)
    # folder must not be moved
    assert project_dir.exists()


async def test_label_unicode_emoji_spaces(aiohttp_client, label_app, label_ctx):
    """Unicode, emoji, and spaces in the name are accepted."""
    client = await aiohttp_client(label_app)

    resp = await client.post(
        "/api/projects/my-project/label",
        json={"name": "Мой лендинг 🎯"},
        headers=_auth_headers(label_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("name") == "Мой лендинг 🎯"
    assert label_ctx["topics"]["1001:42"]["project"] == "Мой лендинг 🎯"


async def test_label_empty_name_returns_400(aiohttp_client, label_app, label_ctx):
    """Empty name → 400."""
    client = await aiohttp_client(label_app)

    for bad in ["", "   "]:
        resp = await client.post(
            "/api/projects/my-project/label",
            json={"name": bad},
            headers=_auth_headers(label_ctx),
        )
        assert resp.status == 400, f"Expected 400 for name={bad!r}, got {resp.status}"
        data = await resp.json()
        assert "name" in data.get("error", "").lower()


async def test_label_too_long_returns_400(aiohttp_client, label_app, label_ctx):
    """Name longer than 80 characters → 400."""
    client = await aiohttp_client(label_app)

    long_name = "x" * 81
    resp = await client.post(
        "/api/projects/my-project/label",
        json={"name": long_name},
        headers=_auth_headers(label_ctx),
    )
    assert resp.status == 400
    data = await resp.json()
    assert "long" in data.get("error", "").lower() or "80" in data.get("error", "")


async def test_label_exactly_80_chars_accepted(aiohttp_client, label_app, label_ctx):
    """Name of exactly 80 characters is accepted."""
    client = await aiohttp_client(label_app)

    name_80 = "a" * 80
    resp = await client.post(
        "/api/projects/my-project/label",
        json={"name": name_80},
        headers=_auth_headers(label_ctx),
    )
    assert resp.status == 200
    assert label_ctx["topics"]["1001:42"]["project"] == name_80


async def test_label_unknown_id_returns_404(aiohttp_client, label_app, label_ctx):
    """Non-existent project id → 404."""
    client = await aiohttp_client(label_app)

    resp = await client.post(
        "/api/projects/ghost-project/label",
        json={"name": "Whatever"},
        headers=_auth_headers(label_ctx),
    )
    assert resp.status == 404
    data = await resp.json()
    assert "not found" in data.get("error", "").lower()


async def test_label_two_topics_same_cwd(aiohttp_client, label_ctx, project_dir):
    """When two topics entries share the same cwd, both get the new name; neither cwd changes."""
    from aiohttp import web

    # Add a second topic entry pointing at the same cwd (different thread key)
    label_ctx["topics"]["2002:99"] = {
        "project": "my-project",
        "cwd": str(project_dir),
        "model": "haiku",
    }

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = label_ctx
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_post("/api/projects/{id}/label", _webapp.api_project_label)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/projects/my-project/label",
            json={"name": "Shared Label"},
            headers=_auth_headers(label_ctx),
        )
        assert resp.status == 200

    # Both entries updated
    for key in ("1001:42", "2002:99"):
        t = label_ctx["topics"][key]
        assert t["project"] == "Shared Label", f"topic {key} project not updated"
        assert t["cwd"] == str(project_dir), f"topic {key} cwd must not change"


async def test_label_id_unchanged_in_response(aiohttp_client, label_app, label_ctx):
    """Response id must equal the original project id (basename of cwd), never the new name."""
    client = await aiohttp_client(label_app)

    resp = await client.post(
        "/api/projects/my-project/label",
        json={"name": "Completely Different Display"},
        headers=_auth_headers(label_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    # id stays the folder basename, not the new display label
    assert data["id"] == "my-project"
    assert data["name"] == "Completely Different Display"

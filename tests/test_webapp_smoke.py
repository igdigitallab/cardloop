"""
Minimal smoke tests for web routes via aiohttp.test_utils.

Tests only public/anonymous endpoints and the auth mechanism.
SDK and PTB are not initialised — only the aiohttp application with a fake ctx.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


# ─── Build the aiohttp application from webapp ────────────────────────────────
# webapp.start() registers routes and returns the app.
# To avoid starting a real server — use aiohttp.test_utils.TestClient.

@pytest.fixture
def fake_ctx_for_app(tmp_path):
    """ctx sufficient to create the aiohttp application."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {},
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
    # Pre-compute auth token (mirrors what start() does)
    ctx["_auth_token"] = _webapp._derive_token(password)
    return ctx


@pytest.fixture
def web_app(fake_ctx_for_app):
    """Creates an aiohttp.web.Application with routes without starting a server."""
    import webapp
    from aiohttp import web

    app = web.Application(middlewares=[webapp.auth_middleware])
    app["ctx"] = fake_ctx_for_app

    # Register a minimal set of routes manually
    # (instead of calling webapp.start() which requires an event loop and static setup)
    app.router.add_get("/api/health", webapp.api_health)
    app.router.add_post("/api/login", webapp.api_login)
    app.router.add_get("/api/projects", webapp.api_projects)
    app.router.add_get("/api/me", webapp.api_me)

    return app


# ─── tests ───────────────────────────────────────────────────────────────────

async def test_health_no_auth(aiohttp_client, web_app):
    """GET /api/health without a cookie → 200 (no auth required)."""
    client = await aiohttp_client(web_app)
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True


async def test_projects_without_cookie_returns_401(aiohttp_client, web_app):
    """GET /api/projects without a cookie → 401 Unauthorized."""
    client = await aiohttp_client(web_app)
    resp = await client.get("/api/projects")
    assert resp.status == 401


async def test_login_correct_password(aiohttp_client, web_app):
    """POST /api/login with the correct password → 200 + cookie cops_auth."""
    client = await aiohttp_client(web_app)
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True

    # Verify that the cookie is set
    cookies = resp.cookies
    assert "cops_auth" in cookies, f"Cookie cops_auth must be in the response, cookies={dict(cookies)}"
    expected_token = _webapp._derive_token("testpass")
    assert cookies["cops_auth"].value == expected_token


async def test_login_wrong_password(aiohttp_client, web_app):
    """POST /api/login with the wrong password → 401."""
    client = await aiohttp_client(web_app)
    resp = await client.post("/api/login", json={"password": "wrongpass"})
    assert resp.status == 401


async def test_projects_with_valid_cookie(aiohttp_client, web_app, fake_ctx_for_app):
    """GET /api/projects with a valid cookie → 200 + list of projects.
    In tests (HTTP, not HTTPS) the Secure cookie is not sent automatically by the browser,
    so the token is passed directly in the Cookie header."""
    client = await aiohttp_client(web_app)
    token = fake_ctx_for_app["_auth_token"]
    resp = await client.get("/api/projects", headers={"Cookie": f"cops_auth={token}"})
    assert resp.status == 200
    data = await resp.json()
    assert "projects" in data
    # ctx["topics"] is empty → projects is an empty list
    assert data["projects"] == []


async def test_me_with_valid_cookie(aiohttp_client, web_app, fake_ctx_for_app):
    """GET /api/me with a valid cookie → 200 {authed: true}."""
    client = await aiohttp_client(web_app)
    token = fake_ctx_for_app["_auth_token"]
    resp = await client.get("/api/me", headers={"Cookie": f"cops_auth={token}"})
    assert resp.status == 200
    data = await resp.json()
    assert data.get("authed") is True

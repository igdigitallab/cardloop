"""Endpoint tests for /api/modules — spec-065 Phase A.

Covers:
- GET /api/modules → 200 {"modules": [...]}
- POST /api/modules/{id} → 200 {"ok": true, "module": {...}}
- POST /api/modules/{unknown} → 404 {"error": "unknown module"}
- POST /api/modules/{id} without enabled → 400
- POST /api/modules/{id} with non-bool enabled → 400
- Auth middleware: 401 without cookie
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def modules_tmp(tmp_path):
    """Redirect module storage to a temp dir for test isolation."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    os.environ["_CARDLOOP_DATA_DIR"] = str(data_dir)
    yield data_dir
    os.environ.pop("_CARDLOOP_DATA_DIR", None)


@pytest.fixture
def app_ctx(tmp_path, modules_tmp):
    password = "testpass"
    data = tmp_path / "app_data"
    data.mkdir()
    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data,
        "HERE": ROOT,
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
def app(app_ctx):
    from aiohttp import web
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = app_ctx
    a.router.add_post("/api/login", _webapp.api_login)
    a.router.add_get("/api/modules", _webapp.api_modules_list)
    a.router.add_post("/api/modules/{id}", _webapp.api_modules_set)
    return a


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ---------------------------------------------------------------------------
# GET /api/modules
# ---------------------------------------------------------------------------

async def test_get_modules_200(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.get("/api/modules", headers=_auth(app_ctx))
    assert r.status == 200
    body = await r.json()
    assert "modules" in body
    modules = body["modules"]
    assert isinstance(modules, list)
    assert len(modules) == 2


async def test_get_modules_shape(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.get("/api/modules", headers=_auth(app_ctx))
    body = await r.json()
    for m in body["modules"]:
        for field in ("id", "name", "description", "version", "provides", "enabled"):
            assert field in m, f"Missing {field!r} in module {m.get('id')!r}"
        assert "default_enabled" not in m
        assert isinstance(m["provides"], list)
        assert isinstance(m["enabled"], bool)


async def test_get_modules_defaults(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.get("/api/modules", headers=_auth(app_ctx))
    body = await r.json()
    by_id = {m["id"]: m for m in body["modules"]}
    assert by_id["github"]["enabled"] is True
    assert by_id["browser"]["enabled"] is False


async def test_get_modules_401_without_auth(aiohttp_client, app):
    client = await aiohttp_client(app)
    r = await client.get("/api/modules")
    assert r.status == 401


# ---------------------------------------------------------------------------
# POST /api/modules/{id}
# ---------------------------------------------------------------------------

async def test_post_module_enable(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/browser",
        json={"enabled": True},
        headers=_auth(app_ctx),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["module"]["id"] == "browser"
    assert body["module"]["enabled"] is True


async def test_post_module_disable(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/github",
        json={"enabled": False},
        headers=_auth(app_ctx),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["module"]["enabled"] is False


async def test_post_module_persists(aiohttp_client, app, app_ctx):
    """Enable browser, then GET /api/modules — should reflect the change."""
    client = await aiohttp_client(app)
    await client.post(
        "/api/modules/browser",
        json={"enabled": True},
        headers=_auth(app_ctx),
    )
    r = await client.get("/api/modules", headers=_auth(app_ctx))
    body = await r.json()
    by_id = {m["id"]: m for m in body["modules"]}
    assert by_id["browser"]["enabled"] is True


async def test_post_module_shape(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/github",
        json={"enabled": True},
        headers=_auth(app_ctx),
    )
    body = await r.json()
    m = body["module"]
    for field in ("id", "name", "description", "version", "provides", "enabled"):
        assert field in m
    assert "default_enabled" not in m


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

async def test_post_module_unknown_id_404(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/nonexistent",
        json={"enabled": True},
        headers=_auth(app_ctx),
    )
    assert r.status == 404
    body = await r.json()
    assert body.get("error") == "unknown module"


async def test_post_module_missing_enabled_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/github",
        json={"something": "else"},
        headers=_auth(app_ctx),
    )
    assert r.status == 400


async def test_post_module_non_bool_enabled_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/github",
        json={"enabled": "yes"},
        headers=_auth(app_ctx),
    )
    assert r.status == 400


async def test_post_module_empty_body_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/github",
        data=b"not json",
        headers={**_auth(app_ctx), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_post_module_401_without_auth(aiohttp_client, app):
    client = await aiohttp_client(app)
    r = await client.post("/api/modules/github", json={"enabled": True})
    assert r.status == 401


# ---------------------------------------------------------------------------
# spec-066: POST /api/modules/{id} with config + GET reflects it
# ---------------------------------------------------------------------------

async def test_get_modules_includes_config(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.get("/api/modules", headers=_auth(app_ctx))
    body = await r.json()
    browser = next(m for m in body["modules"] if m["id"] == "browser")
    assert browser["config"]["backend"] == "builtin"


async def test_post_module_config_persists(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/browser",
        json={"config": {"backend": "cloakbrowser", "agent_actions": "full"}},
        headers=_auth(app_ctx),
    )
    assert r.status == 200
    body = await r.json()
    assert body["module"]["config"]["backend"] == "cloakbrowser"
    assert body["module"]["config"]["agent_actions"] == "full"
    # GET reflects the persisted config.
    r2 = await client.get("/api/modules", headers=_auth(app_ctx))
    browser = next(m for m in (await r2.json())["modules"] if m["id"] == "browser")
    assert browser["config"]["backend"] == "cloakbrowser"


async def test_post_module_config_drops_unknown_keys(aiohttp_client, app, app_ctx):
    """A secret smuggled in config must never be persisted (it belongs in the safe)."""
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/browser",
        json={"config": {"backend": "external-cdp", "manager_token": "SECRET"}},
        headers=_auth(app_ctx),
    )
    assert r.status == 200
    body = await r.json()
    assert "manager_token" not in body["module"]["config"]


async def test_post_module_config_non_object_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/browser",
        json={"config": "not-an-object"},
        headers=_auth(app_ctx),
    )
    assert r.status == 400


async def test_post_module_neither_enabled_nor_config_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/modules/browser",
        json={"foo": "bar"},
        headers=_auth(app_ctx),
    )
    assert r.status == 400

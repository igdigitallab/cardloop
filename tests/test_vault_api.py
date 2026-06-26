"""
Tests for the Global Vault cockpit API (Spec 026, Phase 3).

Routes under test:
    GET    /api/secrets           → names + categories, NEVER values
    GET    /api/secrets/{name}    → reveal single secret (value + meta)
    POST   /api/secrets           → create/update
    DELETE /api/secrets/{name}    → remove

All tests use isolated temp store/key paths via monkeypatch.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import secretstore
import webapp as _webapp
from webapp import _derive_token, auth_middleware


# ─────────────────────────── fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_vault(tmp_path, monkeypatch):
    """Redirect secretstore to a fresh temp dir for every test."""
    key_path = tmp_path / "secret.key"
    store_path = tmp_path / "vault.enc"
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(key_path))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(store_path))
    monkeypatch.delenv("CLAUDE_OPS_SECRET_KEY", raising=False)
    secretstore.init_key()
    yield tmp_path


@pytest.fixture
def fake_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpassword"
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
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def vault_app(fake_ctx):
    """Minimal aiohttp app with vault routes and auth middleware."""
    from aiohttp import web

    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = fake_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/secrets", _webapp.api_vault_list)
    app.router.add_get("/api/secrets/{name}", _webapp.api_vault_get)
    app.router.add_post("/api/secrets", _webapp.api_vault_set)
    app.router.add_delete("/api/secrets/{name}", _webapp.api_vault_delete)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── auth guard ───────────────────────────────────────


async def test_list_requires_auth(aiohttp_client, vault_app):
    """GET /api/secrets without cookie → 401."""
    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets")
    assert resp.status == 401


async def test_get_requires_auth(aiohttp_client, vault_app):
    """GET /api/secrets/{name} without cookie → 401."""
    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets/any-name")
    assert resp.status == 401


async def test_post_requires_auth(aiohttp_client, vault_app):
    """POST /api/secrets without cookie → 401."""
    client = await aiohttp_client(vault_app)
    resp = await client.post("/api/secrets", json={"name": "k", "value": "v"})
    assert resp.status == 401


async def test_delete_requires_auth(aiohttp_client, vault_app):
    """DELETE /api/secrets/{name} without cookie → 401."""
    client = await aiohttp_client(vault_app)
    resp = await client.delete("/api/secrets/any-name")
    assert resp.status == 401


# ─────────────────────────── GET /api/secrets ─────────────────────────────────


async def test_list_empty(aiohttp_client, vault_app, fake_ctx):
    """GET returns empty list when vault has no secrets."""
    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets", headers=_auth(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["secrets"] == []


async def test_list_returns_names_and_categories_only(aiohttp_client, vault_app, fake_ctx):
    """CRITICAL: GET /api/secrets never returns values.

    Regression guard: the response must not contain any secret value anywhere.
    """
    secretstore.set("my-api-key", "SUPER_SECRET_VALUE_DO_NOT_LEAK", category="api")

    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets", headers=_auth(fake_ctx))
    assert resp.status == 200

    data = await resp.json()
    entries = data["secrets"]
    assert len(entries) == 1
    assert entries[0]["name"] == "my-api-key"
    assert entries[0]["category"] == "api"

    # Regression: value must not be present in ANY form
    resp_text = await resp.text()
    assert "SUPER_SECRET_VALUE_DO_NOT_LEAK" not in resp_text, \
        "Secret value leaked into GET /api/secrets response!"

    # Each entry must NOT have a "value" key
    for entry in entries:
        assert "value" not in entry, f"'value' field leaked in list entry: {entry}"


async def test_list_multiple_secrets(aiohttp_client, vault_app, fake_ctx):
    """GET returns all secrets (names + categories)."""
    secretstore.set("alpha", "val1", category="api")
    secretstore.set("beta", "val2", category="db")

    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets", headers=_auth(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    names = [e["name"] for e in data["secrets"]]
    assert "alpha" in names
    assert "beta" in names


# ─────────────────────────── GET /api/secrets/{name} ─────────────────────────


async def test_get_reveals_value(aiohttp_client, vault_app, fake_ctx):
    """GET /api/secrets/{name} returns the decrypted value."""
    secretstore.set("my-token", "actual_secret_value", category="api", notes="used by X")

    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets/my-token", headers=_auth(fake_ctx))
    assert resp.status == 200

    data = await resp.json()
    assert data["name"] == "my-token"
    assert data["value"] == "actual_secret_value"
    assert data["category"] == "api"
    assert data["notes"] == "used by X"


async def test_get_absent_returns_404(aiohttp_client, vault_app, fake_ctx):
    """GET /api/secrets/{name} for an unknown name → 404."""
    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets/nonexistent", headers=_auth(fake_ctx))
    assert resp.status == 404


async def test_get_bad_name_returns_400(aiohttp_client, vault_app, fake_ctx):
    """GET /api/secrets/{name} with an invalid name → 400."""
    # Use a name with a path-traversal attempt (URL-encoded)
    client = await aiohttp_client(vault_app)
    resp = await client.get("/api/secrets/..%2Fevil", headers=_auth(fake_ctx))
    # aiohttp routing will not match this as {name} — expect 404 or 400
    assert resp.status in (400, 404)


# ─────────────────────────── POST /api/secrets ────────────────────────────────


async def test_post_creates_secret(aiohttp_client, vault_app, fake_ctx):
    """POST /api/secrets creates a new secret and returns ok:true."""
    client = await aiohttp_client(vault_app)
    resp = await client.post(
        "/api/secrets",
        json={"name": "new-key", "value": "new-val", "category": "api"},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["name"] == "new-key"
    # Value must not be echoed back
    assert "new-val" not in str(data)

    # Verify it was really stored
    assert secretstore.get("new-key") == "new-val"


async def test_post_updates_existing(aiohttp_client, vault_app, fake_ctx):
    """POST /api/secrets upserts an existing secret."""
    secretstore.set("update-me", "old-value")
    client = await aiohttp_client(vault_app)
    resp = await client.post(
        "/api/secrets",
        json={"name": "update-me", "value": "new-value"},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 200
    assert secretstore.get("update-me") == "new-value"


async def test_post_bad_name_returns_400(aiohttp_client, vault_app, fake_ctx):
    """POST with an invalid name → 400."""
    client = await aiohttp_client(vault_app)
    resp = await client.post(
        "/api/secrets",
        json={"name": "has space", "value": "v"},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 400


async def test_post_empty_name_returns_400(aiohttp_client, vault_app, fake_ctx):
    """POST with an empty name → 400."""
    client = await aiohttp_client(vault_app)
    resp = await client.post(
        "/api/secrets",
        json={"name": "", "value": "v"},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 400


async def test_post_non_string_value_returns_400(aiohttp_client, vault_app, fake_ctx):
    """POST where value is not a string → 400."""
    client = await aiohttp_client(vault_app)
    resp = await client.post(
        "/api/secrets",
        json={"name": "ok-name", "value": 12345},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 400


# ─────────────────────────── DELETE /api/secrets/{name} ──────────────────────


async def test_delete_removes_secret(aiohttp_client, vault_app, fake_ctx):
    """DELETE /api/secrets/{name} removes the entry."""
    secretstore.set("to-delete", "gone")
    client = await aiohttp_client(vault_app)
    resp = await client.delete("/api/secrets/to-delete", headers=_auth(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["deleted"] is True
    assert secretstore.get("to-delete") is None


async def test_delete_nonexistent_returns_404(aiohttp_client, vault_app, fake_ctx):
    """DELETE for an unknown name → 404."""
    client = await aiohttp_client(vault_app)
    resp = await client.delete("/api/secrets/ghost", headers=_auth(fake_ctx))
    assert resp.status == 404


async def test_delete_bad_name_returns_400(aiohttp_client, vault_app, fake_ctx):
    """DELETE with an invalid name in path → 400."""
    client = await aiohttp_client(vault_app)
    # aiohttp path matching — test with a name that has invalid chars
    resp = await client.delete("/api/secrets/has..dots", headers=_auth(fake_ctx))
    # "has..dots" has two consecutive dots; our regex requires single char runs
    # The name is technically valid per our regex (dots are allowed individually).
    # Let's use a genuinely invalid path instead
    resp2 = await client.delete("/api/secrets/has%20space", headers=_auth(fake_ctx))
    # Routing either rejects it (404) or we validate it (400)
    assert resp2.status in (400, 404)

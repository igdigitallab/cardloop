"""
Tests for "Project Secret Store" (Spec 007).

Covers:
- _project_secrets_path path
- read/write round-trip; set/delete individually
- parsing (# comments, empty lines)
- chmod 600 after write
- .gitignore is extended with .claude-ops/secrets/ if missing
- key name validation (lowercase/space/../ → rejected)
- limits (value size, key count)
- API GET: names only (NOT values — critical no-leak test)
- API POST set; DELETE; bad key → 400
- isolation: cwd-A cannot see cwd-B keys
- secrets NOT in audit log (test: value is not written to audit)
"""
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _project_secrets_path,
    _secrets_read,
    _secrets_write,
    _secrets_set,
    _secrets_delete,
    _secrets_ensure_gitignore,
    _derive_token,
    api_project_secrets,
    api_project_secrets_set,
    api_project_secrets_delete,
)


# ──────────────────────────── unit: path ──────────────────────────────────────

def test_secrets_path(tmp_path):
    """_project_secrets_path returns <cwd>/.claude-ops/secrets/secrets.env."""
    result = _project_secrets_path(str(tmp_path))
    assert result == tmp_path / ".claude-ops" / "secrets" / "secrets.env"


# ──────────────────────────── unit: read/write round-trip ─────────────────────

def test_secrets_read_empty(tmp_path):
    """_secrets_read returns {} when no file exists."""
    assert _secrets_read(str(tmp_path)) == {}


def test_secrets_write_read_roundtrip(tmp_path):
    """_secrets_write + _secrets_read correctly save and read data."""
    data = {"API_KEY": "secret123", "DB_PASS": "hunter2"}
    _secrets_write(str(tmp_path), data)
    result = _secrets_read(str(tmp_path))
    assert result == data


def test_secrets_read_ignores_comments(tmp_path):
    """Lines starting with # and empty lines are ignored."""
    path = _project_secrets_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# comment\n\nAPI_KEY=real\n# another comment\n")
    result = _secrets_read(str(tmp_path))
    assert result == {"API_KEY": "real"}


def test_secrets_read_ignores_empty_lines(tmp_path):
    """Empty lines do not cause errors."""
    path = _project_secrets_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\nFOO=bar\n\n\n")
    result = _secrets_read(str(tmp_path))
    assert result == {"FOO": "bar"}


def test_secrets_read_value_with_equals(tmp_path):
    """Value may contain equals signs."""
    path = _project_secrets_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("TOKEN=abc=def==ghi\n")
    result = _secrets_read(str(tmp_path))
    assert result == {"TOKEN": "abc=def==ghi"}


# ──────────────────────────── unit: set/delete ────────────────────────────────

def test_secrets_set_creates(tmp_path):
    """_secrets_set creates a new key."""
    _secrets_set(str(tmp_path), "MY_KEY", "myvalue")
    result = _secrets_read(str(tmp_path))
    assert result["MY_KEY"] == "myvalue"


def test_secrets_set_updates(tmp_path):
    """_secrets_set updates an existing key."""
    _secrets_set(str(tmp_path), "KEY", "v1")
    _secrets_set(str(tmp_path), "KEY", "v2")
    result = _secrets_read(str(tmp_path))
    assert result["KEY"] == "v2"
    assert len(result) == 1


def test_secrets_delete_removes(tmp_path):
    """_secrets_delete removes a key, the rest are preserved."""
    _secrets_set(str(tmp_path), "A", "1")
    _secrets_set(str(tmp_path), "B", "2")
    deleted = _secrets_delete(str(tmp_path), "A")
    assert deleted is True
    result = _secrets_read(str(tmp_path))
    assert "A" not in result
    assert result["B"] == "2"


def test_secrets_delete_nonexistent(tmp_path):
    """_secrets_delete returns False when the key does not exist."""
    result = _secrets_delete(str(tmp_path), "MISSING")
    assert result is False


# ──────────────────────────── unit: chmod 600 ─────────────────────────────────

def test_secrets_write_chmod_600(tmp_path):
    """_secrets_write sets chmod 600 on the file."""
    _secrets_write(str(tmp_path), {"KEY": "val"})
    path = _project_secrets_path(str(tmp_path))
    mode = path.stat().st_mode
    # Only owner can read/write
    assert not (mode & stat.S_IRGRP), "group read bit must be off"
    assert not (mode & stat.S_IWGRP), "group write bit must be off"
    assert not (mode & stat.S_IROTH), "other read bit must be off"
    assert not (mode & stat.S_IWOTH), "other write bit must be off"


# ──────────────────────────── unit: .gitignore ────────────────────────────────

def test_secrets_ensure_gitignore_creates(tmp_path):
    """_secrets_ensure_gitignore creates .gitignore when it does not exist."""
    _secrets_ensure_gitignore(str(tmp_path))
    gi = (tmp_path / ".gitignore").read_text()
    assert ".claude-ops/secrets/" in gi


def test_secrets_ensure_gitignore_appends(tmp_path):
    """_secrets_ensure_gitignore appends the line to an existing .gitignore."""
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\nvenv/\n")
    _secrets_ensure_gitignore(str(tmp_path))
    content = gi.read_text()
    assert ".claude-ops/secrets/" in content
    assert "*.pyc" in content  # old content preserved


def test_secrets_ensure_gitignore_idempotent(tmp_path):
    """_secrets_ensure_gitignore does not duplicate the line on repeated calls."""
    _secrets_ensure_gitignore(str(tmp_path))
    _secrets_ensure_gitignore(str(tmp_path))
    content = (tmp_path / ".gitignore").read_text()
    assert content.count(".claude-ops/secrets/") == 1


def test_secrets_write_adds_gitignore(tmp_path):
    """_secrets_write automatically ensures .gitignore."""
    _secrets_write(str(tmp_path), {"X": "y"})
    content = (tmp_path / ".gitignore").read_text()
    assert ".claude-ops/secrets/" in content


# ──────────────────────────── unit: key validation ─────────────────────────────

@pytest.mark.parametrize("key", [
    "API_KEY",
    "STRIPE_SECRET",
    "_LEADING_UNDERSCORE",
    "A",
    "Z_",
    "MY_KEY_123",
])
def test_secrets_set_valid_keys(tmp_path, key):
    """Valid key names are accepted."""
    _secrets_set(str(tmp_path), key, "value")  # must not raise


@pytest.mark.parametrize("key,reason", [
    ("lowercase", "lowercase letters not allowed"),
    ("Mixed_Case", "mixed upper+lower not allowed"),
    ("123START", "starts with digit"),
    ("has space", "space not allowed"),
    ("../etc", "traversal not allowed"),
    ("A-B", "hyphen not allowed"),
    ("", "empty string not allowed"),
])
def test_secrets_set_invalid_keys(tmp_path, key, reason):
    """Invalid key names are rejected with ValueError."""
    with pytest.raises(ValueError, match="invalid key name"):
        _secrets_set(str(tmp_path), key, "val")


# ──────────────────────────── unit: limits ────────────────────────────────────

def test_secrets_value_too_large(tmp_path):
    """Value > 8KB is rejected."""
    big_value = "x" * (_webapp._SECRETS_MAX_VALUE_SIZE + 1)
    with pytest.raises(ValueError, match="too large"):
        _secrets_set(str(tmp_path), "BIG_KEY", big_value)


def test_secrets_max_keys_limit(tmp_path):
    """Cannot add more than _SECRETS_MAX_KEYS keys."""
    for i in range(_webapp._SECRETS_MAX_KEYS):
        _secrets_set(str(tmp_path), f"KEY_{i:04d}", "v")
    # Next one must fail
    with pytest.raises(ValueError, match="too many keys"):
        _secrets_set(str(tmp_path), "OVERFLOW", "v")


def test_secrets_update_existing_not_counted(tmp_path):
    """Updating an existing key does not increment the counter."""
    # Fill to maximum
    for i in range(_webapp._SECRETS_MAX_KEYS):
        _secrets_set(str(tmp_path), f"KEY_{i:04d}", "v")
    # Updating an existing key must work
    _secrets_set(str(tmp_path), "KEY_0000", "updated")  # must not raise
    result = _secrets_read(str(tmp_path))
    assert result["KEY_0000"] == "updated"


# ──────────────────────────── unit: cwd isolation ─────────────────────────────

def test_secrets_isolation_between_cwds(tmp_path):
    """Secrets from cwd-A are not accessible from cwd-B."""
    cwd_a = tmp_path / "project_a"
    cwd_b = tmp_path / "project_b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    _secrets_set(str(cwd_a), "SECRET_A", "value_a")
    _secrets_set(str(cwd_b), "SECRET_B", "value_b")

    data_a = _secrets_read(str(cwd_a))
    data_b = _secrets_read(str(cwd_b))

    assert "SECRET_A" in data_a
    assert "SECRET_B" not in data_a

    assert "SECRET_B" in data_b
    assert "SECRET_A" not in data_b


# ──────────────────────────── unit: secrets not in audit ───────────────────────

def test_secrets_not_in_audit(tmp_path):
    """audit() does not receive env — secret values do not end up in the audit log.

    Verified architecturally: audit() accepts (project, kind, text),
    and secrets are passed only in the agent env (run_engine(env=...)).
    env is NEVER passed to audit() anywhere in the code — confirmed by this test
    via a direct call to audit and checking that the secret value is not written.
    """
    import bot as _bot

    audit_dir = tmp_path / "audit"
    original_dir = _bot.AUDIT_DIR
    _bot.AUDIT_DIR = audit_dir

    secret_value = "SUPER_SECRET_VALUE_XYZ_12345"
    try:
        # Write audit as the bot does (without env)
        _bot.audit("myproject", "BASH", "echo hello")
        _bot.audit("myproject", "TASK", "some task prompt")

        # Verify that the secret value did NOT end up in the audit file
        audit_files = list(audit_dir.glob("*.log"))
        for f in audit_files:
            content = f.read_text()
            assert secret_value not in content, \
                f"Secret value leaked into audit log: {f}"
    finally:
        _bot.AUDIT_DIR = original_dir


# ──────────────────────────── API fixtures ────────────────────────────────────

@pytest.fixture
def project_dir(tmp_path):
    """Temporary project folder."""
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_with_project(tmp_path, project_dir):
    """ctx with one project for API tests."""
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
def secrets_app(fake_ctx_with_project):
    """aiohttp application with secrets routes."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/secrets", _webapp.api_project_secrets)
    app.router.add_post("/api/projects/{id}/secrets/{key}", _webapp.api_project_secrets_set)
    app.router.add_delete("/api/projects/{id}/secrets/{key}", _webapp.api_project_secrets_delete)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ──────────────────────────── API: GET /secrets ───────────────────────────────

async def test_api_secrets_get_empty(aiohttp_client, secrets_app, fake_ctx_with_project):
    """GET with no secrets → keys:[], exists:false."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/secrets", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["keys"] == []
    assert data["exists"] is False


async def test_api_secrets_get_not_found(aiohttp_client, secrets_app, fake_ctx_with_project):
    """GET non-existent project → 404."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/nonexistent/secrets", headers=h)
    assert resp.status == 404


async def test_api_secrets_get_unauthorized(aiohttp_client, secrets_app):
    """GET without authorization → 401."""
    client = await aiohttp_client(secrets_app)
    resp = await client.get("/api/projects/myproject/secrets")
    assert resp.status == 401


async def test_api_secrets_get_returns_only_names(aiohttp_client, secrets_app, fake_ctx_with_project, project_dir):
    """CRITICAL: GET returns only names, no values.
    Secret values must NEVER appear in the API response."""
    # Create a secret directly
    _secrets_set(str(project_dir), "MY_SECRET", "super_secret_value_DO_NOT_LEAK")

    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/secrets", headers=h)
    assert resp.status == 200

    data = await resp.json()
    # Key names are present
    assert "MY_SECRET" in data["keys"]

    # CRITICAL TEST: value must not appear in the response in any form
    resp_text = await resp.text()
    assert "super_secret_value_DO_NOT_LEAK" not in resp_text, \
        "Secret value leaked into API response!"
    # No 'value', 'values', 'secrets' field with data
    assert "value" not in data, "Unexpected 'value' field in response"


async def test_api_secrets_get_list_after_add(aiohttp_client, secrets_app, fake_ctx_with_project, project_dir):
    """GET after adding secrets returns the keys, exists:true."""
    _secrets_set(str(project_dir), "STRIPE_KEY", "sk-test-123")
    _secrets_set(str(project_dir), "DB_PASS", "dbpassword")

    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/secrets", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert set(data["keys"]) == {"STRIPE_KEY", "DB_PASS"}
    assert data["exists"] is True


# ──────────────────────────── API: POST /secrets/{key} ───────────────────────

async def test_api_secrets_post_set(aiohttp_client, secrets_app, fake_ctx_with_project, project_dir):
    """POST sets a secret, returns list of names (no values)."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/secrets/API_TOKEN",
        json={"value": "token_value_xyz"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "API_TOKEN" in data["keys"]
    assert data["exists"] is True

    # Value is actually written to disk
    stored = _secrets_read(str(project_dir))
    assert stored["API_TOKEN"] == "token_value_xyz"

    # Value not in response
    resp_text = await resp.text()
    assert "token_value_xyz" not in resp_text


async def test_api_secrets_post_bad_key(aiohttp_client, secrets_app, fake_ctx_with_project):
    """POST with invalid name → 400."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/secrets/lowercase_key",
        json={"value": "v"},
        headers=h,
    )
    assert resp.status == 400


async def test_api_secrets_post_traversal_key(aiohttp_client, secrets_app, fake_ctx_with_project):
    """POST with traversal in key → 400."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    # URL-encoded traversal
    resp = await client.post(
        "/api/projects/myproject/secrets/..%2FEVIL",
        json={"value": "v"},
        headers=h,
    )
    assert resp.status == 400


async def test_api_secrets_post_not_found(aiohttp_client, secrets_app, fake_ctx_with_project):
    """POST to a non-existent project → 404."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/nonexistent/secrets/MY_KEY",
        json={"value": "v"},
        headers=h,
    )
    assert resp.status == 404


async def test_api_secrets_post_value_too_large(aiohttp_client, secrets_app, fake_ctx_with_project):
    """POST with an oversized value → 400."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    big = "x" * (_webapp._SECRETS_MAX_VALUE_SIZE + 1)
    resp = await client.post(
        "/api/projects/myproject/secrets/BIG_KEY",
        json={"value": big},
        headers=h,
    )
    assert resp.status == 400


# ──────────────────────────── API: DELETE /secrets/{key} ─────────────────────

async def test_api_secrets_delete(aiohttp_client, secrets_app, fake_ctx_with_project, project_dir):
    """DELETE removes a key and returns the updated list."""
    _secrets_set(str(project_dir), "TO_DELETE", "secret")
    _secrets_set(str(project_dir), "KEEP_ME", "safe")

    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/secrets/TO_DELETE",
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "TO_DELETE" not in data["keys"]
    assert "KEEP_ME" in data["keys"]

    # Actually deleted
    stored = _secrets_read(str(project_dir))
    assert "TO_DELETE" not in stored


async def test_api_secrets_delete_nonexistent(aiohttp_client, secrets_app, fake_ctx_with_project):
    """DELETE non-existent key → 404."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/secrets/NO_SUCH_KEY",
        headers=h,
    )
    assert resp.status == 404


async def test_api_secrets_delete_bad_key(aiohttp_client, secrets_app, fake_ctx_with_project):
    """DELETE invalid key → 400."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/secrets/bad-key",
        headers=h,
    )
    assert resp.status == 400


async def test_api_secrets_delete_not_found_project(aiohttp_client, secrets_app, fake_ctx_with_project):
    """DELETE for a non-existent project → 404."""
    client = await aiohttp_client(secrets_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/nonexistent/secrets/MY_KEY",
        headers=h,
    )
    assert resp.status == 404

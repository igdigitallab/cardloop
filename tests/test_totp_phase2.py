"""
Tests for Spec 026, Phase 2 — TOTP second factor.

Coverage:
1. RFC 6238 known test vector (totp_now / verify).
2. Login unchanged when no active secret.
3. After activate: login without totp → 401 totp_required.
4. After activate: login with valid code → 200.
5. After activate: login with wrong code → 401 totp_invalid.
6. Recovery code works once, then is consumed (second use → 401 totp_invalid).
7. Disable → login back to password-only.
8. Enroll → activate happy path.
9. Activate with wrong code → 400.
10. Reserved names hidden from list_meta() and GET /api/secrets.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import secretstore
import totp as _totp
import webapp as _webapp
from webapp import _derive_token, auth_middleware, _login_attempts


# ─────────────────────────── RFC 6238 test vector ───────────────────────────


def test_rfc6238_vector_sha1():
    """Verify totp_now against RFC 6238 Appendix B SHA-1 test vectors.

    Secret: ASCII b"12345678901234567890" → base32 GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
    All expected codes are 8 digits (the RFC tests 8-digit mode).
    """
    SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    vectors = [
        (59,          "94287082"),
        (1111111109,  "07081804"),
        (1111111111,  "14050471"),
        (1234567890,  "89005924"),
        (2000000000,  "69279037"),
        (20000000000, "65353130"),
    ]
    for ts, expected in vectors:
        got = _totp.totp_now(SECRET_B32, t=ts, digits=8)
        assert got == expected, f"t={ts}: expected {expected!r}, got {got!r}"


def test_verify_accepts_current_code():
    """verify() returns True for the current code."""
    secret = _totp.random_base32()
    code = _totp.totp_now(secret)
    assert _totp.verify(secret, code) is True


def test_verify_rejects_wrong_code():
    """verify() returns False for a wrong code."""
    secret = _totp.random_base32()
    assert _totp.verify(secret, "000000") is False


def test_verify_accepts_within_window():
    """verify() accepts a code from one step back (clock skew)."""
    secret = _totp.random_base32()
    t = time.time()
    code_prev = _totp.totp_now(secret, t=t - 30)
    assert _totp.verify(secret, code_prev, window=1, t=t) is True


def test_verify_no_replay_rejects_reuse():
    """verify_no_replay() accepts a code once, then rejects the same code (replay)."""
    secret = _totp.random_base32()
    _totp._last_accepted_counter.pop(secret, None)  # isolate from other tests
    t = time.time()
    code = _totp.totp_now(secret, t=t)
    assert _totp.verify_no_replay(secret, code, t=t) is True   # first use OK
    assert _totp.verify_no_replay(secret, code, t=t) is False  # replay rejected


def test_recovery_codes_are_64_bit():
    """gen_recovery_codes → 16 hex chars in four groups (64 bits)."""
    codes = _totp.gen_recovery_codes(3)
    for c in codes:
        groups = c.split("-")
        assert len(groups) == 4 and all(len(g) == 4 for g in groups), c
        assert len(c.replace("-", "")) == 16  # 16 hex = 64 bits


def test_recovery_codes_hash_and_verify():
    """gen_recovery_codes → hash_code → verify_and_consume round-trip."""
    codes = _totp.gen_recovery_codes(5)
    assert len(codes) == 5
    hashes = [_totp.hash_code(c) for c in codes]

    ok, remaining = _totp.verify_and_consume(codes[2], hashes)
    assert ok is True
    assert len(remaining) == 4
    # The consumed code no longer works
    ok2, _ = _totp.verify_and_consume(codes[2], remaining)
    assert ok2 is False


# ─────────────────────────── fixtures ───────────────────────────────────────


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


@pytest.fixture(autouse=True)
def clean_rate_limit():
    """Clear global login attempt dict before/after each test."""
    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def fake_ctx(tmp_path):
    password = "hunter2"
    ctx = {
        "topics": {},
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
def totp_app(fake_ctx):
    """Minimal aiohttp app with login + TOTP enrollment routes."""
    from aiohttp import web

    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/auth/totp/status", _webapp.api_totp_status)
    app.router.add_post("/api/auth/totp/enroll", _webapp.api_totp_enroll)
    app.router.add_post("/api/auth/totp/activate", _webapp.api_totp_activate)
    app.router.add_delete("/api/auth/totp", _webapp.api_totp_disable)
    app.router.add_get("/api/secrets", _webapp.api_vault_list)
    return app


def _auth_cookie(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def _login(client, password, *, totp_code=None, ip="1.2.3.4"):
    body = {"password": password}
    if totp_code is not None:
        body["totp"] = totp_code
    return await client.post(
        "/api/login", json=body, headers={"CF-Connecting-IP": ip}
    )


# ─────────────────────────── login when no 2FA active ───────────────────────


async def test_login_no_totp_active_unchanged(aiohttp_client, totp_app, fake_ctx):
    """Login with correct password succeeds when no TOTP secret is enrolled."""
    client = await aiohttp_client(totp_app)
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True


async def test_login_wrong_password_no_totp(aiohttp_client, totp_app, fake_ctx):
    """Wrong password is still rejected when no TOTP is active."""
    client = await aiohttp_client(totp_app)
    resp = await _login(client, "wrong-password")
    assert resp.status == 401


# ─────────────────────────── enroll → activate happy path ───────────────────


async def test_enroll_activate_happy_path(aiohttp_client, totp_app, fake_ctx):
    """Enroll + activate flow produces an active TOTP and returns recovery codes."""
    client = await aiohttp_client(totp_app)

    # 1. Status: disabled initially
    resp = await client.get("/api/auth/totp/status", headers=_auth_cookie(fake_ctx))
    assert resp.status == 200
    assert (await resp.json())["enabled"] is False

    # 2. Enroll
    resp = await client.post("/api/auth/totp/enroll", headers=_auth_cookie(fake_ctx))
    assert resp.status == 200
    enroll_data = await resp.json()
    assert "secret" in enroll_data
    assert "otpauth_uri" in enroll_data
    assert len(enroll_data["recovery_codes"]) == 10

    secret = enroll_data["secret"]
    assert enroll_data["otpauth_uri"].startswith("otpauth://totp/")

    # TOTP still not active — login without code still works
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 200, "login must still work before activation"

    # 3. Activate with a valid code
    code = _totp.totp_now(secret)
    resp = await client.post(
        "/api/auth/totp/activate",
        json={"code": code},
        headers=_auth_cookie(fake_ctx),
    )
    assert resp.status == 200
    activate_data = await resp.json()
    assert activate_data["enabled"] is True
    # Fresh recovery codes returned at activation
    assert len(activate_data["recovery_codes"]) == 10

    # 4. Status: now enabled
    resp = await client.get("/api/auth/totp/status", headers=_auth_cookie(fake_ctx))
    assert (await resp.json())["enabled"] is True

    # 5. Login without TOTP code → 401 totp_required
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 401
    assert (await resp.json())["error"] == "totp_required"

    # 6. Login with valid TOTP code → 200
    code2 = _totp.totp_now(secret)
    resp = await _login(client, fake_ctx["password"], totp_code=code2)
    assert resp.status == 200

    # 7. Login with wrong TOTP code → 401 totp_invalid
    resp = await _login(client, fake_ctx["password"], totp_code="000000")
    assert resp.status == 401
    assert (await resp.json())["error"] == "totp_invalid"


# ─────────────────────────── activate with wrong code ───────────────────────


async def test_activate_wrong_code_returns_400(aiohttp_client, totp_app, fake_ctx):
    """Activate with a wrong TOTP code → 400 totp_invalid (pending not promoted)."""
    client = await aiohttp_client(totp_app)

    # Enroll first
    resp = await client.post("/api/auth/totp/enroll", headers=_auth_cookie(fake_ctx))
    assert resp.status == 200

    # Attempt activate with bad code
    resp = await client.post(
        "/api/auth/totp/activate",
        json={"code": "000000"},
        headers=_auth_cookie(fake_ctx),
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "totp_invalid"

    # Secret must NOT be active
    assert secretstore.get("__totp_secret__") is None

    # Login still works without TOTP (pending ≠ active)
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 200


# ─────────────────────────── recovery code flow ─────────────────────────────


async def _activate_totp(client, fake_ctx):
    """Helper: enroll + activate TOTP and return (secret, recovery_codes)."""
    resp = await client.post("/api/auth/totp/enroll", headers=_auth_cookie(fake_ctx))
    assert resp.status == 200
    secret = (await resp.json())["secret"]

    code = _totp.totp_now(secret)
    resp = await client.post(
        "/api/auth/totp/activate",
        json={"code": code},
        headers=_auth_cookie(fake_ctx),
    )
    assert resp.status == 200
    recovery = (await resp.json())["recovery_codes"]
    return secret, recovery


async def test_recovery_code_works_once_then_consumed(
    aiohttp_client, totp_app, fake_ctx
):
    """A recovery code grants login exactly once; reuse is rejected."""
    client = await aiohttp_client(totp_app)
    _, recovery = await _activate_totp(client, fake_ctx)

    recovery_code = recovery[0]

    # First use of recovery code → 200
    resp = await _login(client, fake_ctx["password"], totp_code=recovery_code)
    assert resp.status == 200

    # Second use of the SAME code → 401 totp_invalid (consumed)
    resp = await _login(client, fake_ctx["password"], totp_code=recovery_code)
    assert resp.status == 401
    assert (await resp.json())["error"] == "totp_invalid"


# ─────────────────────────── disable 2FA ────────────────────────────────────


async def test_disable_reverts_to_password_only(aiohttp_client, totp_app, fake_ctx):
    """After disabling TOTP, login reverts to password-only."""
    client = await aiohttp_client(totp_app)
    secret, _ = await _activate_totp(client, fake_ctx)

    # Verify 2FA is active
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 401
    assert (await resp.json())["error"] == "totp_required"

    # Disable
    resp = await client.delete("/api/auth/totp", headers=_auth_cookie(fake_ctx))
    assert resp.status == 200
    assert (await resp.json())["enabled"] is False

    # Status: disabled
    resp = await client.get("/api/auth/totp/status", headers=_auth_cookie(fake_ctx))
    assert (await resp.json())["enabled"] is False

    # Login now works with password only (no TOTP needed)
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 200

    # Reserved keys were removed from vault
    assert secretstore.get("__totp_secret__") is None
    assert secretstore.get("__totp_recovery__") is None


# ─────────────────────────── reserved names hidden from list ────────────────


async def test_reserved_names_hidden_from_vault_list_api(
    aiohttp_client, totp_app, fake_ctx
):
    """GET /api/secrets must NOT expose __totp_secret__, __totp_pending__, __totp_recovery__."""
    # Directly inject reserved keys into the store
    secretstore.set("__totp_secret__", "fakesecret", category="totp")
    secretstore.set("__totp_pending__", "fakepending", category="totp")
    secretstore.set("__totp_recovery__", json.dumps([]), category="totp")
    # Also a normal user secret
    secretstore.set("my-api-key", "abc123", category="api")

    client = await aiohttp_client(totp_app)
    resp = await client.get("/api/secrets", headers=_auth_cookie(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    names = [e["name"] for e in data["secrets"]]

    # Reserved names must be absent
    assert "__totp_secret__" not in names
    assert "__totp_pending__" not in names
    assert "__totp_recovery__" not in names
    # Normal user secret is present
    assert "my-api-key" in names


def test_reserved_names_hidden_from_list_meta():
    """secretstore.list_meta() hides __.*__ names by default."""
    secretstore.set("__totp_secret__", "s", category="totp")
    secretstore.set("__totp_recovery__", "[]", category="totp")
    secretstore.set("visible-key", "v", category="api")

    metas = secretstore.list_meta()
    names = [m["name"] for m in metas]
    assert "__totp_secret__" not in names
    assert "__totp_recovery__" not in names
    assert "visible-key" in names


def test_reserved_names_visible_with_include_reserved():
    """secretstore.list_meta(include_reserved=True) shows all names."""
    secretstore.set("__totp_secret__", "s", category="totp")
    secretstore.set("visible-key", "v", category="api")

    metas = secretstore.list_meta(include_reserved=True)
    names = [m["name"] for m in metas]
    assert "__totp_secret__" in names
    assert "visible-key" in names


# ─────────────────────────── auth required on TOTP endpoints ────────────────


async def test_totp_status_requires_auth(aiohttp_client, totp_app):
    """GET /api/auth/totp/status without cookie → 401."""
    client = await aiohttp_client(totp_app)
    resp = await client.get("/api/auth/totp/status")
    assert resp.status == 401


async def test_totp_enroll_requires_auth(aiohttp_client, totp_app):
    """POST /api/auth/totp/enroll without cookie → 401."""
    client = await aiohttp_client(totp_app)
    resp = await client.post("/api/auth/totp/enroll")
    assert resp.status == 401


async def test_totp_activate_requires_auth(aiohttp_client, totp_app):
    """POST /api/auth/totp/activate without cookie → 401."""
    client = await aiohttp_client(totp_app)
    resp = await client.post("/api/auth/totp/activate", json={"code": "123456"})
    assert resp.status == 401


async def test_totp_disable_requires_auth(aiohttp_client, totp_app):
    """DELETE /api/auth/totp without cookie → 401."""
    client = await aiohttp_client(totp_app)
    resp = await client.delete("/api/auth/totp")
    assert resp.status == 401


# ─────────────────────────── activate with no pending ───────────────────────


async def test_activate_without_enroll_returns_400(aiohttp_client, totp_app, fake_ctx):
    """Calling activate before enroll → 400 no_pending_enrollment."""
    client = await aiohttp_client(totp_app)
    resp = await client.post(
        "/api/auth/totp/activate",
        json={"code": "123456"},
        headers=_auth_cookie(fake_ctx),
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "no_pending_enrollment"


# ─────────────────────────── rate-limit still wraps TOTP ────────────────────


async def test_wrong_totp_counts_as_failed_attempt(aiohttp_client, totp_app, fake_ctx):
    """Wrong TOTP code counts as a failed login attempt toward the rate limit."""
    client = await aiohttp_client(totp_app)
    secret, _ = await _activate_totp(client, fake_ctx)

    ip = "10.9.8.7"
    # 5 bad TOTP attempts
    for _ in range(5):
        resp = await _login(client, fake_ctx["password"], totp_code="000000", ip=ip)
        assert resp.status == 401

    # 6th attempt is rate-limited
    resp = await _login(client, fake_ctx["password"], totp_code="000000", ip=ip)
    assert resp.status == 429

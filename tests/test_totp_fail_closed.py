"""spec-096 P3.2 — TOTP must fail CLOSED.

`get("__totp_secret__")` returning None means "2FA not enrolled" (password alone logs in, as
before). An exception means "cannot tell": the old `except Exception: active_secret = None` read
that as "not enrolled" and let the password alone in whenever the vault key or store was broken.
Same for the recovery-code read and for persisting a consumed recovery code.
"""
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import secretstore  # noqa: E402
import totp as _totp  # noqa: E402
import webapp as _webapp  # noqa: E402
from webapp import _derive_token, auth_middleware, _login_attempts  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(tmp_path / "secret.key"))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(tmp_path / "vault.enc"))
    monkeypatch.delenv("CLAUDE_OPS_SECRET_KEY", raising=False)
    secretstore.init_key()
    yield tmp_path


@pytest.fixture(autouse=True)
def clean_rate_limit():
    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def fake_ctx(tmp_path):
    ctx = {
        "topics": {}, "sessions": {}, "running": {}, "password": "hunter2",
        "DATA": tmp_path / "data", "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects", "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("hunter2")
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web
    a = web.Application(middlewares=[auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_post("/api/login", _webapp.api_login)
    a.router.add_post("/api/auth/totp/enroll", _webapp.api_totp_enroll)
    a.router.add_post("/api/auth/totp/activate", _webapp.api_totp_activate)
    return a


def _cookie(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def _login(client, password, *, totp_code=None, ip="1.2.3.4"):
    body = {"password": password}
    if totp_code is not None:
        body["totp"] = totp_code
    return await client.post("/api/login", json=body, headers={"CF-Connecting-IP": ip})


async def _enrol(client, ctx):
    resp = await client.post("/api/auth/totp/enroll", headers=_cookie(ctx))
    secret = (await resp.json())["secret"]
    resp = await client.post("/api/auth/totp/activate", json={"code": _totp.totp_now(secret)},
                             headers=_cookie(ctx))
    assert resp.status == 200
    return secret, (await resp.json())["recovery_codes"]


def _break_key(tmp_path, monkeypatch):
    """The store file stays, the key is gone: every vault read now raises."""
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(tmp_path / "no-such.key"))


# ── the secret read ──────────────────────────────────────────────────────────

async def test_not_enrolled_still_logs_in_with_the_password_alone(aiohttp_client, app, fake_ctx):
    """get() -> None is the legitimate 'no 2FA' state and must keep working."""
    resp = await _login(await aiohttp_client(app), fake_ctx["password"])
    assert resp.status == 200
    assert "cops_auth" in resp.cookies


async def test_lost_vault_key_refuses_login_with_503(aiohttp_client, app, fake_ctx, tmp_path, monkeypatch):
    client = await aiohttp_client(app)
    await _enrol(client, fake_ctx)
    _break_key(tmp_path, monkeypatch)
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 503
    assert (await resp.json()) == {"error": "2fa_state_unreadable"}
    assert "cops_auth" not in resp.cookies


async def test_corrupt_store_refuses_login_with_503(aiohttp_client, app, fake_ctx, tmp_path):
    client = await aiohttp_client(app)
    await _enrol(client, fake_ctx)
    (tmp_path / "vault.enc").write_bytes(b"not a fernet token")
    resp = await _login(client, fake_ctx["password"])
    assert resp.status == 503
    assert (await resp.json())["error"] == "2fa_state_unreadable"
    assert "cops_auth" not in resp.cookies


async def test_unreadable_store_still_rejects_a_wrong_password_with_401(
        aiohttp_client, app, fake_ctx, tmp_path, monkeypatch):
    """The vault is only consulted after the password: a stranger learns nothing from the 503."""
    client = await aiohttp_client(app)
    await _enrol(client, fake_ctx)
    _break_key(tmp_path, monkeypatch)
    resp = await _login(client, "wrong-password")
    assert resp.status == 401


async def test_unreadable_state_counts_against_the_rate_limiter(
        aiohttp_client, app, fake_ctx, tmp_path, monkeypatch):
    client = await aiohttp_client(app)
    await _enrol(client, fake_ctx)
    _break_key(tmp_path, monkeypatch)
    for _ in range(5):
        assert (await _login(client, fake_ctx["password"], ip="9.9.9.9")).status == 503
    assert (await _login(client, fake_ctx["password"], ip="9.9.9.9")).status == 429


async def test_unreadable_state_writes_a_journal_line_without_exception_text(
        aiohttp_client, app, fake_ctx, monkeypatch, caplog):
    def boom(name):
        raise RuntimeError("vault-detail-that-must-not-reach-the-journal")
    monkeypatch.setattr(_webapp._secretstore, "get", boom)
    with caplog.at_level(logging.ERROR):
        resp = await _login(await aiohttp_client(app), fake_ctx["password"])
    assert resp.status == 503
    assert "2FA state unreadable" in caplog.text
    assert "secret rm __totp_secret__" in caplog.text            # the break-glass is in the line
    assert "vault-detail-that-must-not-reach-the-journal" not in caplog.text
    assert "hunter2" not in caplog.text


# ── the recovery-code read and write ─────────────────────────────────────────

def _fail_on(monkeypatch, fn_name, secret_name):
    real = getattr(_webapp._secretstore, fn_name)

    def wrapper(name, *a, **kw):
        if name == secret_name:
            raise RuntimeError("simulated vault failure")
        return real(name, *a, **kw)
    monkeypatch.setattr(_webapp._secretstore, fn_name, wrapper)


async def test_unreadable_recovery_hashes_refuse_with_503(aiohttp_client, app, fake_ctx, monkeypatch):
    client = await aiohttp_client(app)
    await _enrol(client, fake_ctx)
    _fail_on(monkeypatch, "get", "__totp_recovery__")
    resp = await _login(client, fake_ctx["password"], totp_code="000000")
    assert resp.status == 503
    assert (await resp.json())["error"] == "2fa_state_unreadable"
    assert "cops_auth" not in resp.cookies


async def test_corrupt_recovery_json_refuses_with_503(aiohttp_client, app, fake_ctx):
    client = await aiohttp_client(app)
    await _enrol(client, fake_ctx)
    secretstore.set("__totp_recovery__", "{not json", category="totp")
    resp = await _login(client, fake_ctx["password"], totp_code="000000")
    assert resp.status == 503


async def test_recovery_code_that_cannot_be_marked_used_does_not_log_in(
        aiohttp_client, app, fake_ctx, monkeypatch):
    """If the consumed hash is not persisted the same code would work again -> refuse."""
    client = await aiohttp_client(app)
    _, recovery = await _enrol(client, fake_ctx)
    _fail_on(monkeypatch, "set", "__totp_recovery__")
    resp = await _login(client, fake_ctx["password"], totp_code=recovery[0])
    assert resp.status == 503
    assert "cops_auth" not in resp.cookies


async def test_healthy_paths_are_unchanged(aiohttp_client, app, fake_ctx):
    client = await aiohttp_client(app)
    secret, recovery = await _enrol(client, fake_ctx)
    assert (await _login(client, fake_ctx["password"])).status == 401                  # totp_required
    assert (await _login(client, fake_ctx["password"], totp_code="000000")).status == 401
    assert (await _login(client, fake_ctx["password"], totp_code=recovery[0])).status == 200
    assert (await _login(client, fake_ctx["password"], totp_code=recovery[0])).status == 401   # consumed

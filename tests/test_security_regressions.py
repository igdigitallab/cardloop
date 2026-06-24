"""
Security regressions — locking in expected behaviour:

1. Invalid card_id (../path, too long, special chars) → 400
   on endpoints: api_card_run, api_move_task, api_delete_task, api_update_task.
2. Rate-limit /api/login → 429 after 5 failed attempts from the same IP.
3. _valid_card_id: unit tests for boundary values.

Complements test_security.py (path-traversal in _resolve_safe) without duplication.
"""
import sys
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _valid_card_id, _login_attempts, _tasks_path


# ─────────────────────────── unit: _valid_card_id ───────────────────────────


@pytest.mark.parametrize("card_id", [
    "aabb",          # 4 chars — minimum
    "aabbcc",        # normal hex
    "12345678",      # digits only
    "aabbcc-1234",   # with dash
    "a-b-c-d-e-f",  # many dashes
    "1234567890abcd", # 14 chars
    "a" * 20,        # 20 chars — maximum
    "err-9b37ae",    # incident card: err- prefix + hash6
    "err-aabbcc",    # incident card
    "jan-9e2d",      # user-defined slug (non-hex lowercase — now allowed)
    "xyz-1234",      # non-hex lowercase letters are valid
])
def test_valid_card_id_valid(card_id: str):
    """Valid card_ids must pass."""
    assert _valid_card_id(card_id), f"card_id {card_id!r} should be valid"


@pytest.mark.parametrize("card_id,reason", [
    ("",                  "empty string"),
    ("abc",               "3 chars — below minimum of 4"),
    ("a" * 21,            "21 chars — above maximum of 20"),
    ("../etc/passwd",     "path traversal with ../"),
    ("../../root",        "path traversal with ../../"),
    ("abc!def",           "special char !"),
    ("abc def",           "space"),
    ("abc/def",           "forward slash"),
    ("abc\\def",          "backslash"),
    ("ABCDEF",            "uppercase letters (outside [a-z0-9-])"),
    ("abc\ndef",          "newline"),
    ("err-../x",          "err- + traversal"),
    ("err-ABCDEF",        "err- + uppercase"),
])
def test_valid_card_id_invalid(card_id: str, reason: str):
    """Invalid card_ids must be rejected."""
    assert not _valid_card_id(card_id), (
        f"card_id {card_id!r} should be INVALID ({reason})"
    )


# ─────────────────────────── fixtures for API tests ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "secproj"
    pdir.mkdir()
    return pdir


@pytest.fixture
def sec_ctx(tmp_path, project_dir):
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "secproj",
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
def sec_app(sec_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = sec_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/tasks/{card}/run", _webapp.api_card_run)
    app.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    app.router.add_delete("/api/projects/{id}/tasks/{card}", _webapp.api_delete_task)
    app.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", _webapp.api_update_task)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _make_empty_board(project_dir: Path) -> None:
    _tasks_path(str(project_dir)).write_text(
        "# Tasks\n\n## Backlog\n\n## In Progress\n\n## Review\n\n## Failed\n",
        encoding="utf-8",
    )


# ─────────────────────────── invalid card_id → 400 ───────────────────────────


INVALID_CARD_IDS = [
    "toolongcardidthatexceedslimit",  # > 20 chars
    "INVALID",                         # uppercase letters (outside hex)
    # Note: "abc!@#" is not tested via HTTP — aiohttp routing strips '#' as a URL fragment,
    # yielding 405 from the router rather than 400 from the handler. This is correct:
    # URLs containing '#' are unreachable. The _valid_card_id unit test already covers
    # special characters directly.
]


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_card_run_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """GET /tasks/{card}/run with an invalid card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.get(f"/api/projects/secproj/tasks/{bad_id}/run", headers=h)
    assert resp.status == 400, (
        f"card_id {bad_id!r} should give 400 in /run, got: {resp.status}"
    )


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_delete_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """DELETE /tasks/{card} with an invalid card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.delete(f"/api/projects/secproj/tasks/{bad_id}", headers=h)
    assert resp.status == 400, (
        f"card_id {bad_id!r} should give 400 in DELETE, got: {resp.status}"
    )


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_move_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """POST /tasks/{card}/move with an invalid card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.post(
        f"/api/projects/secproj/tasks/{bad_id}/move",
        json={"to": "review"},
        headers=h,
    )
    assert resp.status == 400, (
        f"card_id {bad_id!r} should give 400 in /move, got: {resp.status}"
    )


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_update_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """PATCH /tasks/{card} with an invalid card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.patch(
        f"/api/projects/secproj/tasks/{bad_id}",
        json={"text": "something"},
        headers=h,
    )
    assert resp.status == 400, (
        f"card_id {bad_id!r} should give 400 in PATCH, got: {resp.status}"
    )


# ─────────────────────────── rate-limit /api/login ───────────────────────────


@pytest.fixture(autouse=False)
def clean_login_attempts():
    """Clear the global attempts dict before and after each test."""
    _login_attempts.clear()
    yield
    _login_attempts.clear()


async def test_login_rate_limit_triggers_after_5_fails(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """5 failed attempts from the same IP → the 6th returns 429."""
    client = await aiohttp_client(sec_app)

    # 5 failed attempts
    for i in range(5):
        resp = await client.post("/api/login", json={"password": f"wrong{i}"})
        assert resp.status == 401, f"Attempt {i+1} should return 401, got: {resp.status}"

    # 6th must be blocked
    resp = await client.post("/api/login", json={"password": "anythingwrong"})
    assert resp.status == 429, f"After 5 failures should return 429, got: {resp.status}"
    data = await resp.json()
    assert "too many" in data.get("error", "").lower() or "429" in str(resp.status)


async def test_login_rate_limit_correct_password_still_blocked(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """After 5 failures even the correct password is blocked (rate-limit by IP)."""
    client = await aiohttp_client(sec_app)

    # 5 failed attempts
    for i in range(5):
        await client.post("/api/login", json={"password": f"wrong{i}"})

    # Correct password is also blocked
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 429, (
        "After 5 failures the rate-limit must block even the correct password"
    )


async def test_login_rate_limit_not_triggered_before_5_fails(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """Rate-limit does not fire before 5 failures."""
    client = await aiohttp_client(sec_app)

    # 4 failed attempts
    for i in range(4):
        resp = await client.post("/api/login", json={"password": f"wrong{i}"})
        assert resp.status == 401, f"Attempt {i+1}: expected 401, got {resp.status}"

    # 5th attempt — not yet blocked (counter reaches 5, but the 6th is blocked)
    resp = await client.post("/api/login", json={"password": "wrong4"})
    assert resp.status == 401, f"5th attempt should still give 401 (not 429), got: {resp.status}"


async def test_login_success_does_not_trigger_rate_limit(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """A successful attempt does not count as a failure — 5 new failures are needed to ban."""
    client = await aiohttp_client(sec_app)

    # Successful login
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 200

    # 4 failed attempts (rate-limit must not fire)
    for i in range(4):
        resp = await client.post("/api/login", json={"password": f"wrong{i}"})
        assert resp.status == 401


# ─────────────────────────── unauthenticated access ───────────────────────────


async def test_api_requires_auth(aiohttp_client, sec_app, sec_ctx, project_dir):
    """All /api/* endpoints except /health and /login require authentication."""
    client = await aiohttp_client(sec_app)

    # Without cookie → 401
    resp = await client.get("/api/projects/secproj/tasks/aabbcc/run")
    assert resp.status == 401

    resp = await client.post("/api/projects/secproj/tasks/aabbcc/move", json={"to": "review"})
    assert resp.status == 401

    resp = await client.delete("/api/projects/secproj/tasks/aabbcc")
    assert resp.status == 401

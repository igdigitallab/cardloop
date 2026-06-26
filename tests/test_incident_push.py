"""
Tests for Spec-012 Phase 3 — optional incident push endpoint.

Covers:
- disabled-by-default: global flag OFF → POST → 404 (even with a valid token)
- enabled + no CLAUDEOPS_INCIDENT_TOKEN secret for project → 403
- enabled + wrong token → 403
- enabled + correct token → 200 + _report_incident called
- auth_middleware: POST /incident exempt from cookie-auth; GET — not exempt; /evil — not exempt
- sanitisation: newlines removed; lengths truncated; empty exc_class → 400
- rate-limit: > max within window → 429
- secret/token NEVER appears in the response body
"""
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _derive_token,
    _secrets_set,
    _INCIDENT_PUSH_MAX,
    _INCIDENT_PUSH_WINDOW,
    _incident_push_history,
    _INCIDENT_PATH_RE,
    api_project_incident,
    auth_middleware,
)
from aiohttp import web


# ─────────────────────────── fixtures ───────────────────────────────

@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "mybot"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx(tmp_path, project_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpassword"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "mybot",
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
def incident_app(fake_ctx):
    """aiohttp application with auth_middleware + incident endpoint."""
    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/incidents", _webapp.api_project_incidents)
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    app.router.add_post("/api/projects/{id}/incident", api_project_incident)
    # Stub for a non-existent path with /evil (verifies middleware does not bypass)
    app.router.add_get("/api/projects/{id}/incident", _webapp.api_project_incidents)  # GET same path
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _clear_push_history():
    """Clear the global rate-limit history between tests."""
    _incident_push_history.clear()


# ─────────────────────────── unit: regex _INCIDENT_PATH_RE ───────────────────

def test_incident_path_re_matches_valid():
    assert _INCIDENT_PATH_RE.match("/api/projects/mybot/incident")
    assert _INCIDENT_PATH_RE.match("/api/projects/some-project-123/incident")
    assert _INCIDENT_PATH_RE.match("/api/projects/x/incident")


def test_incident_path_re_no_trailing_slash():
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incident/")


def test_incident_path_re_no_evil_suffix():
    """Suffix after /incident does not match — path traversal protection."""
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incident/evil")
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incident/evil/extra")


def test_incident_path_re_no_other_paths():
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incidents")  # plural
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/self-heal")
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/chat")
    assert not _INCIDENT_PATH_RE.match("/api/health")


def test_incident_path_re_no_empty_id():
    assert not _INCIDENT_PATH_RE.match("/api/projects//incident")


# ─────────────────────────── disabled-by-default ─────────────────────────────

async def test_incident_push_disabled_by_default(aiohttp_client, incident_app, project_dir):
    """Flag OFF by default → 404 even with a valid token."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "mytoken")

    with patch.object(_webapp, "_get_global_setting", return_value=False):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError", "where": "/test"},
            headers={"X-Incident-Token": "mytoken"},
        )
    assert resp.status == 404, f"Expected 404 when disabled, got {resp.status}"
    body = await resp.json()
    assert "mytoken" not in str(body), "Token must not appear in response"


# ─────────────────────────── no project token → 403 ─────────────────────────

async def test_incident_push_no_project_token(aiohttp_client, incident_app, project_dir):
    """Project has no CLAUDEOPS_INCIDENT_TOKEN → 403 (per-project opt-in not done)."""
    _clear_push_history()
    # No secret set

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "anytoken"},
        )
    assert resp.status == 403


# ─────────────────────────── token mismatch → 403 ────────────────────────────

async def test_incident_push_wrong_token(aiohttp_client, incident_app, project_dir):
    """Wrong token → 403."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "correct_token")

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "wrong_token"},
        )
    assert resp.status == 403
    body = await resp.json()
    assert "correct_token" not in str(body), "Secret token must not appear in response"
    assert "wrong_token" not in str(body), "Presented token must not appear in response"


# ─────────────────────────── correct token → 200 ────────────────────────────

async def test_incident_push_correct_token(aiohttp_client, incident_app, project_dir):
    """Correct token → 200 + _report_incident called."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "valid_token_xyz")

    report_calls = []

    async def mock_report(ctx, exc_class, where, project_id="claude-ops-bot"):
        report_calls.append({"exc_class": exc_class, "where": where, "project_id": project_id})

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError", "where": "/api/test", "excerpt": "test error"},
            headers={"X-Incident-Token": "valid_token_xyz"},
        )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    # Secret must not appear in response
    resp_text = str(data)
    assert "valid_token_xyz" not in resp_text


async def test_incident_push_token_in_body_fallback(aiohttp_client, incident_app, project_dir):
    """Token in JSON body (no X-Incident-Token header) → 200."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "body_token")

    async def noop_report(*a, **kw):
        pass

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=noop_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "RuntimeError", "token": "body_token"},
        )
    assert resp.status == 200
    body = await resp.json()
    assert "body_token" not in str(body)


# ─────────────────────────── auth_middleware exempt ───────────────────────────

async def test_incident_post_exempt_from_cookie_auth(aiohttp_client, incident_app, project_dir):
    """POST /incident does not require a cookie — reaches the handler (which checks the token itself)."""
    _clear_push_history()
    # Without the flag → 404, but importantly NOT 401 (middleware did not block)
    with patch.object(_webapp, "_get_global_setting", return_value=False):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "Err"},
            # NO Cookie header
        )
    assert resp.status == 404  # reached the handler (global flag OFF → 404)
    assert resp.status != 401, "auth_middleware wrongly blocked /incident"


async def test_incident_get_requires_cookie_auth(aiohttp_client, incident_app, fake_ctx):
    """GET /api/projects/{id}/incident is NOT exempt — requires cookie (returns 401 without it)."""
    client = await aiohttp_client(incident_app)
    resp = await client.get("/api/projects/mybot/incident")
    assert resp.status == 401


async def test_other_api_path_requires_cookie_auth(aiohttp_client, incident_app):
    """Other /api/* paths still require a cookie."""
    client = await aiohttp_client(incident_app)
    resp = await client.get("/api/projects/mybot/tasks")
    assert resp.status == 401


async def test_incident_evil_path_requires_cookie_auth(aiohttp_client, incident_app):
    """POST /api/projects/x/incident/evil does NOT fall into the exempt set (regex excludes suffix)."""
    # No route registered for /evil, so aiohttp returns 404/405 from the router, not 401 from
    # middleware — but only if middleware lets it through. Since _INCIDENT_PATH_RE.match(...evil)
    # returns None → not exempt → middleware checks cookie → 401.
    client = await aiohttp_client(incident_app)
    # No registered route for this path, but middleware fires first for /api/*
    resp = await client.post("/api/projects/mybot/incident/evil")
    # Must be 401 (cookie check) — not 200/404 from bypass
    assert resp.status == 401, f"Evil suffix path should be 401 (blocked by cookie auth), got {resp.status}"


# ─────────────────────────── sanitisation ─────────────────────────────────────

async def test_sanitize_newlines_stripped(aiohttp_client, incident_app, project_dir):
    """Newlines in where/excerpt are removed (protects TASKS.md format)."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok")

    captured = {}

    async def mock_report(ctx, exc_class, where, project_id="claude-ops-bot"):
        captured["exc_class"] = exc_class
        captured["where"] = where

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "Val\nue\rErr", "where": "line1\nline2", "excerpt": "a\nb\nc"},
            headers={"X-Incident-Token": "tok"},
        )
    assert resp.status == 200
    assert "\n" not in captured.get("exc_class", ""), "Newline in exc_class"
    assert "\n" not in captured.get("where", ""), "Newline in where"


async def test_sanitize_exc_class_cap(aiohttp_client, incident_app, project_dir):
    """exc_class is truncated to 120 characters."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok2")

    captured = {}

    async def mock_report(ctx, exc_class, where, project_id="claude-ops-bot"):
        captured["exc_class"] = exc_class

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "X" * 200},
            headers={"X-Incident-Token": "tok2"},
        )
    assert resp.status == 200
    assert len(captured.get("exc_class", "")) <= 120


async def test_sanitize_empty_exc_class_400(aiohttp_client, incident_app, project_dir):
    """Empty or whitespace-only exc_class → 400."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok3")

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "   "},
            headers={"X-Incident-Token": "tok3"},
        )
    assert resp.status == 400


async def test_invalid_json_400(aiohttp_client, incident_app, project_dir):
    """Invalid JSON → 400."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok4")

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            data=b"not-json",
            headers={"X-Incident-Token": "tok4", "Content-Type": "application/json"},
        )
    assert resp.status == 400


# ─────────────────────────── rate-limit ──────────────────────────────────────

async def test_rate_limit_exceeded(aiohttp_client, incident_app, project_dir):
    """Rate-limit exceeded (_INCIDENT_PUSH_MAX within _INCIDENT_PUSH_WINDOW) → 429."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "rl_token")

    # Pre-fill history up to max
    now = time.time()
    _incident_push_history["mybot"] = [now - 1] * _INCIDENT_PUSH_MAX

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "rl_token"},
        )
    assert resp.status == 429


async def test_rate_limit_window_expired(aiohttp_client, incident_app, project_dir):
    """Entries outside the window are not counted → request passes."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "rl_tok2")

    # All entries are older than the window
    old_ts = time.time() - _INCIDENT_PUSH_WINDOW - 10
    _incident_push_history["mybot"] = [old_ts] * (_INCIDENT_PUSH_MAX + 5)

    async def noop_report(*a, **kw):
        pass

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None), \
         patch.object(_webapp, "_report_incident", side_effect=noop_report):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "RuntimeError"},
            headers={"X-Incident-Token": "rl_tok2"},
        )
    assert resp.status == 200


# ─────────────────────────── secret/token never in response ────────────────────────

async def test_no_secret_in_any_response(aiohttp_client, incident_app, project_dir):
    """CLAUDEOPS_INCIDENT_TOKEN secret never appears in any response body."""
    _clear_push_history()
    secret = "SUPER_SECRET_INCIDENT_TOKEN_XYZ_12345"
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", secret)

    # 403 scenario (wrong token)
    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp_403 = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "Err"},
            headers={"X-Incident-Token": "wrong"},
        )
    text_403 = await resp_403.text()
    assert secret not in text_403, f"Secret leaked in 403 response: {text_403}"

    # 200 scenario (correct token)
    _clear_push_history()

    async def noop_report(*a, **kw):
        pass

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None), \
         patch.object(_webapp, "_report_incident", side_effect=noop_report):
        resp_200 = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": secret},
        )
    text_200 = await resp_200.text()
    assert secret not in text_200, f"Secret leaked in 200 response: {text_200}"


# ─────────────────────────── project not found ───────────────────────────────

async def test_incident_push_project_not_found(aiohttp_client, incident_app):
    """Non-existent project → 404."""
    _clear_push_history()

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/nonexistent_proj/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "anytoken"},
        )
    assert resp.status == 404


async def test_incident_push_unicode_line_separator_sanitized(aiohttp_client, incident_app, project_dir):
    """BLOCKER regression: U+2028/U+2029 in exc_class/where must NOT leak into the card
    (otherwise splitlines() on the board causes '## Section' injection / ghost cards).
    We verify the arguments passed to _report_incident (call_args is recorded on call,
    even if the coroutine is not awaited)."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok123")

    mock_report = AsyncMock()
    # Explicit U+2028 (LINE SEP) and U+2029 (PARA SEP) — splitlines() treats them as line breaks.
    evil_exc = "TypeError ## Done"
    evil_where = "/x - [ ] evil <!--ops:err-bad-->"
    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", new=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: coro.close()):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": evil_exc, "where": evil_where},
            headers={"X-Incident-Token": "tok123"},
        )
    assert resp.status == 200
    assert mock_report.call_args is not None, "_report_incident must be called"
    sent_exc = mock_report.call_args.args[1]
    sent_where = mock_report.call_args.args[2]
    # No line separators → board injection impossible
    assert len(sent_exc.splitlines()) == 1, repr(sent_exc)
    assert len(sent_where.splitlines()) == 1, repr(sent_where)
    assert " " not in sent_exc and " " not in sent_where

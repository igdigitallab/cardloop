"""
Spec-011 Phase 3 — TEST-COVERAGE: new tests for previously uncovered endpoints.

Covers:
1. api_new_project  (POST /api/projects/new) — scaffolding + guard + run_engine=None
2. Sessions API     (GET sessions / POST session new+resume / GET session-history / GET session-context)
3. Free chats       (POST /api/free / DELETE /api/free/{id}) — create/list/delete
4. api_project_health ROUTE (GET /api/projects/{id}/health) — Phase 1/2 contract
5. api_project_audit + api_project_upgrade — create card + 404/409 + run_engine=None
6. _run_log_cmd timeout — unit test, does not hang
7. api_global_file_write  — path-traversal / .env block / legit write

Style: aiohttp_client + Cookie auth — like test_board_api.py / test_webapp_smoke.py.
run_engine is always None (degraded mode) — no real SDK.
"""

import sys
import json
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _tasks_path


# ──────────────────────────── shared helpers / fixtures ──────────────────────


def _auth(ctx):
    """Cookie header from a pre-computed auth token."""
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


@pytest.fixture
def base_ctx(tmp_path):
    """Minimal ctx for most route tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "secr3t"
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
        "GROUP_CHAT_ID": 0,
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def project_ctx(tmp_path):
    """ctx with one project 'myproj' in topics."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pdir = tmp_path / "myproj"
    pdir.mkdir()
    password = "secr3t"
    ctx = {
        "topics": {
            "0:1": {
                "project": "myproj",
                "cwd": str(pdir),
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
        "GROUP_CHAT_ID": 0,
    }
    ctx["_auth_token"] = _derive_token(password)
    ctx["_pdir"] = pdir
    return ctx


def _make_app(ctx, routes: list[tuple]):
    """Creates an aiohttp.web.Application with auth-middleware and the given routes."""
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    return app


# ══════════════════════════════════════════════════════════════════════════════
# 1. api_new_project  ─  POST /api/projects/new
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def new_project_app(base_ctx):
    return _make_app(base_ctx, [
        ("POST", "/api/projects/new", _webapp.api_new_project),
        ("GET",  "/api/health",       _webapp.api_health),
    ])


async def test_new_project_creates_scaffolding_files(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """POST /api/projects/new (run_engine=None) → folder created with CLAUDE.md / README.md / TASKS.md / .gitignore."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={"name": "test-proj"}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()

    # Basic response fields
    assert "id" in data
    assert "cwd" in data
    assert data.get("started") is False  # run_engine=None → no agent

    cwd = Path(data["cwd"])
    assert cwd.is_dir(), "project folder must exist"
    assert (cwd / "CLAUDE.md").is_file(), "CLAUDE.md must be created"
    assert (cwd / "README.md").is_file(), "README.md must be created"
    assert (cwd / "TASKS.md").is_file(), "TASKS.md must be created"
    assert (cwd / ".gitignore").is_file(), ".gitignore must be created"


async def test_new_project_tasks_has_init_card(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """TASKS.md of a new project contains a starter card in In Progress."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={}, headers=_auth(base_ctx))
    assert resp.status == 200
    cwd = Path((await resp.json())["cwd"])
    tasks_text = (cwd / "TASKS.md").read_text(encoding="utf-8")
    assert "<!--ops:" in tasks_text, "TASKS.md must contain an ops-marker for the starter card"


async def test_new_project_registered_in_topics(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """After creation the project is registered in ctx['topics'] with the correct project and cwd."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={"name": "my-new"}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()
    cwd = data["cwd"]

    # Exactly one entry appeared in topics (base_ctx started with empty topics)
    assert len(base_ctx["topics"]) >= 1
    # Find entry by cwd
    entry = next((v for v in base_ctx["topics"].values() if v.get("cwd") == cwd), None)
    assert entry is not None, f"topics does not contain entry with cwd={cwd!r}: {base_ctx['topics']!r}"
    assert entry["project"] == "my-new", f"project should be 'my-new', got {entry['project']!r}"
    assert entry["cwd"] == cwd, f"cwd should be {cwd!r}, got {entry['cwd']!r}"


async def test_new_project_no_auth_returns_401(aiohttp_client, new_project_app):
    """POST /api/projects/new without cookie → 401."""
    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={"name": "x"})
    assert resp.status == 401


async def test_new_project_409_on_existing_dir(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """spec-046: duplicate slug is disambiguated with -<ts> suffix, not rejected with 409.

    Old behaviour: same timestamp → 409.
    New behaviour: slug already exists → append -<ts> → new unique folder → 200.
    """
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Patch time.time so both calls get the same base slug
    import webapp

    _fixed_ts = 9999999999

    def fixed_time():
        return _fixed_ts

    monkeypatch.setattr(webapp.time, "time", fixed_time)

    # First call — creates untitled-9999999999
    client = await aiohttp_client(new_project_app)
    resp1 = await client.post("/api/projects/new", json={}, headers=_auth(base_ctx))
    assert resp1.status == 200
    data1 = await resp1.json()

    # Second call with same timestamp — slug collides → disambiguated with -<ts> suffix → also 200
    resp2 = await client.post("/api/projects/new", json={}, headers=_auth(base_ctx))
    assert resp2.status == 200
    data2 = await resp2.json()

    # The two projects must have different IDs and different directories
    assert data1["id"] != data2["id"]
    assert data1["cwd"] != data2["cwd"]


# ══════════════════════════════════════════════════════════════════════════════
# 2. Sessions API
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sessions_app(project_ctx):
    return _make_app(project_ctx, [
        ("GET",  "/api/projects/{id}/sessions",        _webapp.api_project_sessions),
        ("POST", "/api/projects/{id}/session",          _webapp.api_project_set_session),
        ("GET",  "/api/projects/{id}/session-history",  _webapp.api_project_session_history),
        ("GET",  "/api/projects/{id}/session-context",  _webapp.api_project_session_context),
    ])


async def test_sessions_list_empty(aiohttp_client, sessions_app, project_ctx):
    """GET /api/projects/{id}/sessions with no files → {"sessions": []}."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/sessions", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data == {"sessions": []}, f"Expected {{\"sessions\": []}}, got {data!r}"


async def test_sessions_list_unknown_project_404(aiohttp_client, sessions_app, project_ctx):
    """GET /sessions for an unknown project → 404."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/ghost/sessions", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_session_new_clears_active(aiohttp_client, sessions_app, project_ctx):
    """POST /session {action:new} → active=None (old session cleared)."""
    # Simulate an existing active session
    project_ctx["sessions"]["0:1"] = "old-session-id"

    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "new"}, headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("active") is None
    assert "0:1" not in project_ctx["sessions"]


async def test_session_resume_missing_file_returns_400(aiohttp_client, sessions_app, project_ctx):
    """POST /session {action:resume, session_id:nonexistent} → 400 (file not found)."""
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "resume", "session_id": "nosuchsession"},
                             headers=_auth(project_ctx))
    assert resp.status == 400


async def test_session_resume_traversal_rejected(aiohttp_client, sessions_app, project_ctx):
    """POST /session {action:resume, session_id:'../evil'} → 400 (traversal sanitization)."""
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "resume", "session_id": "../evil"},
                             headers=_auth(project_ctx))
    assert resp.status == 400


async def test_session_resume_valid(aiohttp_client, sessions_app, project_ctx, tmp_path, monkeypatch):
    """POST /session {action:resume, session_id:valid} → active=session_id."""
    pdir = project_ctx["_pdir"]

    # Create a fake .jsonl at the path _sdk_sessions_dir would return
    fake_sdk_dir = tmp_path / "sdk-dir"
    fake_sdk_dir.mkdir()
    (fake_sdk_dir / "abcdef123456.jsonl").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: fake_sdk_dir)

    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "resume", "session_id": "abcdef123456"},
                             headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("active") == "abcdef123456"


async def test_session_bad_action_400(aiohttp_client, sessions_app, project_ctx):
    """POST /session with unknown action → 400."""
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "teleport"},
                             headers=_auth(project_ctx))
    assert resp.status == 400


async def test_session_set_while_busy_409(aiohttp_client, sessions_app, project_ctx):
    """POST /session while project is busy (running) → 409."""
    project_ctx["running"]["0:1"] = True
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "new"}, headers=_auth(project_ctx))
    assert resp.status == 409


async def test_session_history_no_session(aiohttp_client, sessions_app, project_ctx):
    """GET /session-history with no active session → messages=[], session_id=None."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/session-history", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("messages") == []
    assert data.get("session_id") is None


async def test_session_history_with_jsonl(aiohttp_client, sessions_app, project_ctx, tmp_path, monkeypatch):
    """GET /session-history?session_id=... with a real .jsonl → messages is non-empty."""
    fake_sdk_dir = tmp_path / "sdk-hist"
    fake_sdk_dir.mkdir()
    jsonl_path = fake_sdk_dir / "sess001.jsonl"
    # Minimal SDK transcript: one user message
    entry = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "Hello Claude"},
    })
    jsonl_path.write_text(entry + "\n", encoding="utf-8")

    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: fake_sdk_dir)

    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/session-history?session_id=sess001",
                            headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("session_id") == "sess001"
    assert isinstance(data.get("messages"), list)
    assert len(data["messages"]) >= 1
    assert data["messages"][0]["role"] == "user"
    assert "Hello Claude" in data["messages"][0]["text"]


async def test_session_context_no_session(aiohttp_client, sessions_app, project_ctx):
    """GET /session-context with no active session → all fields empty, session_id=None."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/session-context", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data == {"read": [], "edited": [], "commands": [], "session_id": None}, (
        f"Expected empty context, got {data!r}"
    )


async def test_sessions_require_auth(aiohttp_client, sessions_app):
    """GET /sessions without cookie → 401."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/sessions")
    assert resp.status == 401


# ══════════════════════════════════════════════════════════════════════════════
# 2b. _session_context_tokens unit tests
# ══════════════════════════════════════════════════════════════════════════════


def _make_jsonl_with_usage(tmp_path, usages: list[dict]) -> "Path":
    """Write a .jsonl file with assistant messages carrying the given usage dicts."""
    lines = []
    for u in usages:
        msg = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "usage": u,
            },
        }
        lines.append(json.dumps(msg))
    p = tmp_path / "test_session.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_session_context_tokens_returns_last_usage(tmp_path):
    """_session_context_tokens sums tokens from the LAST assistant usage block."""
    p = _make_jsonl_with_usage(tmp_path, [
        # First turn — should be ignored
        {"input_tokens": 100, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50},
        # Last turn — should be used
        {"input_tokens": 10, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 300},
    ])
    result = _webapp._session_context_tokens(p)
    assert result == 10 + 500 + 300, f"Expected 810 tokens, got {result}"


def test_session_context_tokens_no_usage_returns_zero(tmp_path):
    """_session_context_tokens returns 0 when no usage blocks are present."""
    p = tmp_path / "empty.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    result = _webapp._session_context_tokens(p)
    assert result == 0, f"Expected 0 tokens, got {result}"


def test_session_context_tokens_missing_file_returns_zero(tmp_path):
    """_session_context_tokens returns 0 for a non-existent file (never raises)."""
    result = _webapp._session_context_tokens(tmp_path / "nonexistent.jsonl")
    assert result == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2c. /live pending_handoff field
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def live_app(project_ctx):
    return _make_app(project_ctx, [
        ("GET", "/api/projects/{id}/live", _webapp.api_project_live),
    ])


async def test_live_includes_pending_handoff_null(aiohttp_client, live_app, project_ctx):
    """GET /live includes pending_handoff key (null when no handoff is set)."""
    _webapp._live_turns.clear()
    client = await aiohttp_client(live_app)
    resp = await client.get("/api/projects/myproj/live", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "pending_handoff" in data, "Response must include pending_handoff key"
    assert data["pending_handoff"] is None


async def test_live_includes_pending_handoff_value(aiohttp_client, live_app, project_ctx):
    """GET /live returns pending_handoff string when one is stored in ctx."""
    _webapp._live_turns.clear()
    project_ctx["pending_handoff"] = {"0:1": "Session summary: worked on feature X"}
    client = await aiohttp_client(live_app)
    resp = await client.get("/api/projects/myproj/live", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("pending_handoff") == "Session summary: worked on feature X"
    # Clean up
    del project_ctx["pending_handoff"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. Free chats  —  POST /api/free / DELETE /api/free/{id}
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def free_app(base_ctx):
    return _make_app(base_ctx, [
        ("POST",   "/api/free",          _webapp.api_free_create),
        ("POST",   "/api/free/{id}/rename", _webapp.api_free_rename),
        ("DELETE", "/api/free/{id}",     _webapp.api_free_delete),
        ("GET",    "/api/projects",      _webapp.api_projects),
    ])


async def test_free_create_returns_free_id(aiohttp_client, free_app, base_ctx):
    """POST /api/free → id starts with 'free-'."""
    client = await aiohttp_client(free_app)
    resp = await client.post("/api/free", json={}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["id"].startswith("free-"), f"id should start with 'free-', got {data['id']!r}"


async def test_free_create_persists_in_projects_list(aiohttp_client, free_app, base_ctx):
    """After creating a free chat GET /api/projects includes it."""
    client = await aiohttp_client(free_app)
    cr = await client.post("/api/free", json={"label": "My Free Chat"}, headers=_auth(base_ctx))
    assert cr.status == 200
    fid = (await cr.json())["id"]

    resp = await client.get("/api/projects", headers=_auth(base_ctx))
    assert resp.status == 200
    projects = (await resp.json())["projects"]
    free_ids = [p["id"] for p in projects if p.get("is_free")]
    assert fid in free_ids, f"free chat {fid} should be in the projects list"


async def test_free_create_with_label(aiohttp_client, free_app, base_ctx):
    """POST /api/free with label → label is stored."""
    client = await aiohttp_client(free_app)
    resp = await client.post("/api/free", json={"label": "Research session"}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("label") == "Research session"


async def test_free_delete_removes_from_list(aiohttp_client, free_app, base_ctx):
    """DELETE /api/free/{id} → ok=True, removed from list."""
    client = await aiohttp_client(free_app)
    cr = await client.post("/api/free", json={}, headers=_auth(base_ctx))
    fid = (await cr.json())["id"]

    del_resp = await client.delete(f"/api/free/{fid}", headers=_auth(base_ctx))
    assert del_resp.status == 200
    assert (await del_resp.json()).get("ok") is True

    list_resp = await client.get("/api/projects", headers=_auth(base_ctx))
    free_ids = [p["id"] for p in (await list_resp.json())["projects"] if p.get("is_free")]
    assert fid not in free_ids, "deleted free chat must not be in the list"


async def test_free_delete_nonexistent_404(aiohttp_client, free_app, base_ctx):
    """DELETE non-existent free chat → 404."""
    client = await aiohttp_client(free_app)
    resp = await client.delete("/api/free/free-000000ff", headers=_auth(base_ctx))
    assert resp.status == 404


async def test_free_delete_busy_409(aiohttp_client, free_app, base_ctx):
    """DELETE /api/free/{id} while chat is busy → 409."""
    client = await aiohttp_client(free_app)
    cr = await client.post("/api/free", json={}, headers=_auth(base_ctx))
    fid = (await cr.json())["id"]

    # Simulate busy state
    base_ctx["running"][fid] = True

    resp = await client.delete(f"/api/free/{fid}", headers=_auth(base_ctx))
    assert resp.status == 409


async def test_free_require_auth(aiohttp_client, free_app):
    """POST /api/free without cookie → 401."""
    client = await aiohttp_client(free_app)
    resp = await client.post("/api/free", json={})
    assert resp.status == 401


# ══════════════════════════════════════════════════════════════════════════════
# 4. api_project_health ROUTE  —  GET /api/projects/{id}/health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def health_app(project_ctx):
    return _make_app(project_ctx, [
        ("GET", "/api/projects/{id}/health", _webapp.api_project_health),
    ])


async def test_health_route_returns_expected_shape(aiohttp_client, health_app, project_ctx):
    """GET /api/projects/{id}/health → {archetype, capabilities, security_warn, security_hint}."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "archetype" in data, "response must contain 'archetype'"
    assert isinstance(data["capabilities"], list), "capabilities must be a list"
    assert isinstance(data["security_warn"], bool), "security_warn must be a bool"


async def test_health_route_404_on_unknown_project(aiohttp_client, health_app, project_ctx):
    """GET /health for a non-existent project → 404."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/ghost/health", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_health_route_capability_items_present(aiohttp_client, health_app, project_ctx):
    """Capability items logs, tests, secrets are present for default (software) archetype."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    keys = {item["key"] for item in data["capabilities"]}
    assert "logs" in keys, "logs capability must be present"
    assert "tests" in keys, "tests capability must be present for software archetype"
    assert "secrets" in keys, "secrets capability must be present for software archetype"


async def test_health_route_logs_capability_has_on_field(aiohttp_client, health_app, project_ctx):
    """logs capability must have an 'on' boolean field."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    cap_logs = next((i for i in data["capabilities"] if i["key"] == "logs"), None)
    assert cap_logs is not None, "logs capability not found"
    assert isinstance(cap_logs.get("on"), bool), "logs.on must be a bool"


async def test_health_route_logs_off_when_no_log_cmd(aiohttp_client, health_app, project_ctx):
    """logs capability is off when log_cmd is not set (default project_ctx)."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    cap_logs = next((i for i in data["capabilities"] if i["key"] == "logs"), None)
    assert cap_logs is not None
    assert cap_logs["on"] is False, "logs must be off when log_cmd is unset"


async def test_health_route_security_warn_is_bool(aiohttp_client, health_app, project_ctx):
    """security_warn must always be a bool."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    assert isinstance(data["security_warn"], bool)


async def test_health_route_requires_auth(aiohttp_client, health_app):
    """GET /health without cookie → 401."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health")
    assert resp.status == 401


async def test_health_all_capabilities_on(aiohttp_client, health_app, project_ctx):
    """Project with log_cmd + test_cmd set and .git/.gitignore(with .env) → all capabilities on,
    security_warn=False."""
    pdir = project_ctx["_pdir"]

    (pdir / ".gitignore").write_text(".env\n", encoding="utf-8")
    (pdir / ".git").mkdir()

    project_ctx["topics"]["0:1"]["log_cmd"] = "echo hello"
    project_ctx["topics"]["0:1"]["test_cmd"] = "pytest"

    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    assert data["security_warn"] is False, "security_warn must be False when .env is gitignored"
    all_on = all(cap["on"] for cap in data["capabilities"])
    assert all_on, (
        f"all capabilities must be on, got: {[(c['key'], c['on']) for c in data['capabilities']]}"
    )


async def test_health_content_archetype_only_logs(aiohttp_client, health_app, project_ctx):
    """Content archetype: only 'logs' capability present (no tests/secrets)."""
    project_ctx["topics"]["0:1"]["type"] = "content"
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["archetype"] == "content"
    keys = {cap["key"] for cap in data["capabilities"]}
    assert "logs" in keys, "logs must be present for content archetype"
    assert "tests" not in keys, "tests must NOT be present for content archetype"
    assert "secrets" not in keys, "secrets must NOT be present for content archetype"


async def test_health_software_archetype_has_all_capabilities(aiohttp_client, health_app, project_ctx):
    """Software archetype (default): logs + tests + secrets all present."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    # default project has no 'type' set → falls back to 'software'
    assert data["archetype"] == "software"
    keys = {cap["key"] for cap in data["capabilities"]}
    assert "logs" in keys
    assert "tests" in keys
    assert "secrets" in keys


# ══════════════════════════════════════════════════════════════════════════════
# 5. api_project_audit + api_project_upgrade
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def audit_upgrade_app(project_ctx):
    return _make_app(project_ctx, [
        ("POST", "/api/projects/{id}/audit",   _webapp.api_project_audit),
        ("POST", "/api/projects/{id}/upgrade", _webapp.api_project_upgrade),
        ("GET",  "/api/projects/{id}/tasks",   _webapp.api_project_tasks),
    ])


async def test_audit_creates_card_run_engine_none(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /audit (run_engine=None) → ok=True, card_id present, started=False."""
    pdir = project_ctx["_pdir"]
    # Empty board
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/audit", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert "card_id" in data
    assert data.get("started") is False


async def test_audit_card_appears_in_in_progress(aiohttp_client, audit_upgrade_app, project_ctx):
    """After /audit a card with emoji '🩺' is in In Progress on the board."""
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    await client.post("/api/projects/myproj/audit", headers=_auth(project_ctx))

    tasks_resp = await client.get("/api/projects/myproj/tasks", headers=_auth(project_ctx))
    data = await tasks_resp.json()
    ip_col = next(c for c in data["columns"] if c["key"] == "in_progress")
    assert len(ip_col["cards"]) >= 1
    card_texts = [c.get("text", "") for c in ip_col["cards"]]
    assert any("🩺" in t for t in card_texts), (
        f"Audit card must contain '🩺', card texts: {card_texts!r}"
    )


async def test_audit_404_unknown_project(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /audit for an unknown project → 404."""
    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/ghost/audit", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_audit_409_when_busy(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /audit while project is busy → 409."""
    project_ctx["running"]["0:1"] = True
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/audit", headers=_auth(project_ctx))
    assert resp.status == 409


async def test_upgrade_creates_card_run_engine_none(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /upgrade (run_engine=None) → ok=True, card_id present, started=False."""
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/upgrade", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert "card_id" in data
    assert data.get("started") is False


async def test_upgrade_card_appears_in_in_progress(aiohttp_client, audit_upgrade_app, project_ctx):
    """After /upgrade a card '🔧' is in In Progress."""
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    await client.post("/api/projects/myproj/upgrade", headers=_auth(project_ctx))

    tasks_resp = await client.get("/api/projects/myproj/tasks", headers=_auth(project_ctx))
    ip_col = next(c for c in (await tasks_resp.json())["columns"] if c["key"] == "in_progress")
    assert len(ip_col["cards"]) >= 1
    card_texts = [c.get("text", "") for c in ip_col["cards"]]
    assert any("🔧" in t for t in card_texts), (
        f"Upgrade card must contain '🔧', card texts: {card_texts!r}"
    )


async def test_upgrade_404_unknown_project(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /upgrade for an unknown project → 404."""
    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/ghost/upgrade", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_upgrade_409_when_busy(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /upgrade while project is busy → 409."""
    project_ctx["running"]["0:1"] = True
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/upgrade", headers=_auth(project_ctx))
    assert resp.status == 409


# ══════════════════════════════════════════════════════════════════════════════
# 6. _run_log_cmd — timeout unit test
# ══════════════════════════════════════════════════════════════════════════════


async def test_run_log_cmd_timeout_returns_empty_string():
    """_run_log_cmd with a command longer than timeout → returns '' (does not hang)."""
    from webapp import _run_log_cmd

    result = await _run_log_cmd("sleep 5", timeout=0.3)
    assert result == "", f"On timeout an empty string must be returned, got {result!r}"


async def test_run_log_cmd_fast_echo_returns_output(tmp_path):
    """_run_log_cmd of an allowlisted command → stdout is returned."""
    from webapp import _run_log_cmd

    f = tmp_path / "log.txt"
    f.write_text("hello_log\n")
    result = await _run_log_cmd(f"cat {f}")  # 'cat' is on the diag allowlist
    assert "hello_log" in result, f"Expected 'hello_log' in output, got {result!r}"


async def test_run_log_cmd_rejects_non_allowlisted():
    """_run_log_cmd of a non-allowlisted / unsafe command → '' (not executed)."""
    from webapp import _run_log_cmd

    assert await _run_log_cmd("echo hi") == ""  # echo not on allowlist
    assert await _run_log_cmd("cat /etc/passwd; rm -rf /tmp/x") == ""  # chaining blocked


async def test_run_log_cmd_nonexistent_command_returns_empty():
    """_run_log_cmd for a non-existent command → '' (no exception raised)."""
    from webapp import _run_log_cmd

    result = await _run_log_cmd("__no_such_cmd_xyz123__")
    assert result == "", f"Non-existent command must return '', got {result!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. api_global_file_write  (POST /api/global/file)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def global_file_app(base_ctx):
    return _make_app(base_ctx, [
        ("POST", "/api/global/file", _webapp.api_global_file_write),
        ("GET",  "/api/global/file", _webapp.api_global_file),
    ])


async def test_global_file_write_legit(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Writing an allowed file inside home-dir → ok=True, file updated."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Create file inside home
    target = tmp_path / "notes.txt"
    target.write_text("old content", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=notes.txt",
        json={"content": "new content"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert target.read_text(encoding="utf-8") == "new content"


async def test_global_file_write_traversal_rejected(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Path traversal '../etc/passwd' in POST /api/global/file → 400."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=../etc/passwd",
        json={"content": "evil"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 400, f"Traversal must return 400, got {resp.status}"


async def test_global_file_write_env_blocked(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Writing .env → 403 (secrets protection)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Create file so we don't get 404 before the name check
    (tmp_path / ".env").write_text("SECRET=old", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=.env",
        json={"content": "SECRET=evil"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 403, f"Writing .env must return 403, got {resp.status}"


async def test_global_file_write_env_production_blocked(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Writing .env.production → 403 (any .env* except .env.example)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    (tmp_path / ".env.production").write_text("KEY=old", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=.env.production",
        json={"content": "KEY=evil"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 403


async def test_global_file_write_env_example_allowed(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """.env.example is NOT blocked (_is_secret_name returns False for it)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    target = tmp_path / ".env.example"
    target.write_text("KEY=placeholder", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=.env.example",
        json={"content": "KEY=newplaceholder"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert target.read_text(encoding="utf-8") == "KEY=newplaceholder", (
        f"File should contain new content, got {target.read_text(encoding='utf-8')!r}"
    )


async def test_global_file_write_requires_auth(aiohttp_client, global_file_app, tmp_path, monkeypatch):
    """POST /api/global/file without cookie → 401."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))
    client = await aiohttp_client(global_file_app)
    resp = await client.post("/api/global/file?path=notes.txt", json={"content": "x"})
    assert resp.status == 401


async def test_global_file_write_no_path_param_400(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """POST /api/global/file without ?path= → 400."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))
    client = await aiohttp_client(global_file_app)
    resp = await client.post("/api/global/file", json={"content": "x"}, headers=_auth(base_ctx))
    assert resp.status == 400

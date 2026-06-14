"""
Tests for ops:b2a081 — tab activity state (running / awaiting / seen).

Covers:
- _awaiting dict is populated on run_end bus event
- _awaiting entry is cleared on run_start bus event
- api_project_seen clears the awaiting marker and updates _seen
- api_projects response includes running and awaiting fields
- awaiting is False when last_seen >= last_finished (already seen)
- awaiting is True when last_finished > last_seen
- 404 on unknown project for /seen endpoint
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _awaiting,
    _seen,
    _bus_publish,
    _derive_token,
    api_project_seen,
    api_projects,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_project(cwd: str, session_key: str = "chat:1001") -> dict:
    return {
        "id": "test-proj",
        "name": "Test Project",
        "cwd": cwd,
        "model": "sonnet",
        "tg_thread": session_key,
        "is_free": False,
    }


def _make_ctx(tmp_path: Path, running: dict | None = None) -> dict:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return {
        "topics": {},
        "sessions": {},
        "running": running if running is not None else {},
        "password": "testpass",
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


# ─── unit: _bus_publish hooks ────────────────────────────────────────────────

def test_run_end_sets_awaiting(tmp_path):
    """_bus_publish with run_end event sets _awaiting[session_key]."""
    sk = "chat:bus-test-end"
    _awaiting.pop(sk, None)
    _seen.pop(sk, None)

    _bus_publish(sk, {"kind": "run_end", "outcome": "ok", "run_id": "r1"}, persist=False)

    assert sk in _awaiting, "_awaiting should be set after run_end"
    assert isinstance(_awaiting[sk], float)
    assert _awaiting[sk] > 0


def test_run_start_clears_awaiting(tmp_path):
    """_bus_publish with run_start clears any existing _awaiting entry."""
    sk = "chat:bus-test-start"
    import time
    _awaiting[sk] = time.time()  # pre-set as if a previous run finished

    _bus_publish(sk, {"kind": "run_start", "source": "card", "run_id": "r2"}, persist=False)

    assert sk not in _awaiting, "_awaiting should be cleared after run_start"


def test_unrelated_event_does_not_touch_awaiting():
    """Non run_end/run_start events leave _awaiting unchanged."""
    sk = "chat:bus-test-text"
    _awaiting.pop(sk, None)

    _bus_publish(sk, {"kind": "text", "text": "hello"}, persist=False)

    assert sk not in _awaiting, "_awaiting should not be set for text events"


# ─── unit: awaiting computation ──────────────────────────────────────────────

def test_awaiting_true_when_finished_after_seen():
    """awaiting = True when last_finished > last_seen."""
    import time
    sk = "chat:await-true"
    _seen[sk] = time.time() - 10  # seen 10 seconds ago
    _awaiting[sk] = time.time()   # finished just now

    finished_ts = _awaiting[sk]
    seen_ts = _seen[sk]
    awaiting = finished_ts > 0 and finished_ts > seen_ts
    assert awaiting is True


def test_awaiting_false_when_seen_after_finished():
    """awaiting = False when last_seen >= last_finished."""
    import time
    sk = "chat:await-false"
    _awaiting[sk] = time.time() - 10  # finished 10 seconds ago
    _seen[sk] = time.time()            # seen just now

    finished_ts = _awaiting[sk]
    seen_ts = _seen[sk]
    awaiting = finished_ts > 0 and finished_ts > seen_ts
    assert awaiting is False


def test_awaiting_false_when_never_finished():
    """awaiting = False when there is no finished_ts (project never ran)."""
    sk = "chat:await-never"
    _awaiting.pop(sk, None)
    _seen.pop(sk, None)

    finished_ts = _awaiting.get(sk, 0.0)
    seen_ts = _seen.get(sk, 0.0)
    awaiting = finished_ts > 0 and finished_ts > seen_ts
    assert awaiting is False


# ─── HTTP endpoint tests ──────────────────────────────────────────────────────

@pytest.fixture
def fake_ctx_for_app(tmp_path):
    """ctx sufficient for creating an aiohttp app."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = _make_ctx(tmp_path)
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def web_app(fake_ctx_for_app, tmp_path):
    """aiohttp app with auth + seen + projects routes registered."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_for_app

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects", _webapp.api_projects)
    app.router.add_post("/api/projects/{id}/seen", _webapp.api_project_seen)

    # Register a test project in topics
    proj_cwd = str(tmp_path / "my-project")
    Path(proj_cwd).mkdir(exist_ok=True)
    fake_ctx_for_app["topics"]["chat:9999"] = {
        "project": "my-project",
        "cwd": proj_cwd,
        "model": "sonnet",
        "tg_thread": "chat:9999",
    }

    return app


@pytest.fixture
async def auth_client(aiohttp_client, web_app):
    """Authenticated aiohttp client."""
    client = await aiohttp_client(web_app)
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 200
    return client


async def test_seen_clears_awaiting_flag(auth_client, fake_ctx_for_app, tmp_path):
    """POST /api/projects/{id}/seen clears _awaiting for that project."""
    import time
    sk = "chat:9999"
    # Simulate: agent just finished a turn
    _awaiting[sk] = time.time() - 1  # finished 1s ago
    _seen.pop(sk, None)

    resp = await auth_client.post("/api/projects/my-project/seen")
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert data.get("awaiting") is False
    # _awaiting should be cleared
    assert sk not in _awaiting


async def test_seen_404_unknown_project(auth_client):
    """POST /api/projects/{id}/seen → 404 for nonexistent project."""
    resp = await auth_client.post("/api/projects/no-such-project/seen")
    assert resp.status == 404


async def test_projects_includes_running_field(auth_client, fake_ctx_for_app, tmp_path):
    """GET /api/projects includes 'running' field per project."""
    resp = await auth_client.get("/api/projects")
    assert resp.status == 200
    data = await resp.json()
    projects = data["projects"]
    assert len(projects) > 0
    for p in projects:
        assert "running" in p, f"Project {p.get('id')} missing 'running' field"
        assert isinstance(p["running"], bool)


async def test_projects_includes_awaiting_field(auth_client, fake_ctx_for_app, tmp_path):
    """GET /api/projects includes 'awaiting' field per project."""
    resp = await auth_client.get("/api/projects")
    assert resp.status == 200
    data = await resp.json()
    projects = data["projects"]
    assert len(projects) > 0
    for p in projects:
        assert "awaiting" in p, f"Project {p.get('id')} missing 'awaiting' field"
        assert isinstance(p["awaiting"], bool)


async def test_projects_running_true_when_active(auth_client, fake_ctx_for_app):
    """GET /api/projects shows running=True when ctx['running'] has the session key."""
    sk = "chat:9999"
    fake_ctx_for_app["running"][sk] = True  # simulate active run
    try:
        resp = await auth_client.get("/api/projects")
        assert resp.status == 200
        data = await resp.json()
        projects = data["projects"]
        proj = next((p for p in projects if p["id"] == "my-project"), None)
        assert proj is not None
        assert proj["running"] is True
    finally:
        fake_ctx_for_app["running"].pop(sk, None)


async def test_projects_awaiting_true_when_finished_not_seen(auth_client, fake_ctx_for_app):
    """GET /api/projects shows awaiting=True when run_end fired but /seen not called."""
    import time
    sk = "chat:9999"
    _awaiting[sk] = time.time()  # run just finished
    _seen.pop(sk, None)           # never seen
    try:
        resp = await auth_client.get("/api/projects")
        assert resp.status == 200
        data = await resp.json()
        proj = next((p for p in data["projects"] if p["id"] == "my-project"), None)
        assert proj is not None
        assert proj["awaiting"] is True
    finally:
        _awaiting.pop(sk, None)


async def test_projects_awaiting_false_after_seen(auth_client, fake_ctx_for_app):
    """GET /api/projects shows awaiting=False after POST /seen clears the marker."""
    import time
    sk = "chat:9999"
    _awaiting[sk] = time.time() - 5  # finished 5 seconds ago
    _seen.pop(sk, None)

    # Mark as seen
    await auth_client.post("/api/projects/my-project/seen")

    resp = await auth_client.get("/api/projects")
    assert resp.status == 200
    data = await resp.json()
    proj = next((p for p in data["projects"] if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj["awaiting"] is False

"""
Tests for spec-046: Generative Scaffolding — project creation automation.

Covers:
- _infer_archetype: keyword-based archetype detection
- _intent_to_slug: kebab-case slug derivation
- _intent_to_display_name: display name derivation
- api_new_project endpoint: type stored, .gitignore excluded for content, no TG topic
- _render_template_archetype: conditional sections per archetype
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _infer_archetype,
    _intent_to_slug,
    _intent_to_display_name,
    _render_template_archetype,
    _derive_token,
)
import webapp as _webapp


# ─────────────────────────── unit: _infer_archetype ────────────────────────────

def test_infer_archetype_software():
    assert _infer_archetype("build a next.js app") == "software"


def test_infer_archetype_content():
    assert _infer_archetype("write a blog post about AI") == "content"


def test_infer_archetype_ops():
    assert _infer_archetype("automate my backup pipeline") == "ops"


def test_infer_archetype_default():
    """No strong signal → default to software."""
    assert _infer_archetype("something unclear xyz") == "software"


def test_infer_archetype_empty():
    assert _infer_archetype("") == "software"


# ─────────────────────────── unit: _intent_to_slug ─────────────────────────────

def test_intent_to_slug_basic():
    assert _intent_to_slug("write a blog post") == "write-a-blog-post"


def test_intent_to_slug_truncate():
    long = "a" * 100
    result = _intent_to_slug(long)
    assert len(result) <= 40


def test_intent_to_slug_empty():
    assert _intent_to_slug("") == ""


def test_intent_to_slug_special_chars():
    assert _intent_to_slug("Build a React app! (2024)") == "build-a-react-app-2024"


def test_intent_to_slug_unicode():
    # Unicode gets stripped to ASCII approximation
    result = _intent_to_slug("Créer un projet")
    assert "-" in result or result.isalnum()
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in result)


def test_intent_to_slug_short_result_returns_empty():
    # Input that produces < 2 chars slug → empty string
    result = _intent_to_slug("!@#$%^")
    assert result == ""


# ─────────────────────────── unit: _intent_to_display_name ─────────────────────

def test_intent_to_display_name_basic():
    result = _intent_to_display_name("write a blog post about AI")
    assert result == "Write A Blog Post About"


def test_intent_to_display_name_short():
    result = _intent_to_display_name("build api")
    assert result == "Build Api"


def test_intent_to_display_name_empty():
    assert _intent_to_display_name("") == ""


# ─────────────────────────── unit: _render_template_archetype ──────────────────

def test_template_archetype_software_has_error_handler():
    """Rendered CLAUDE.md for software type contains error handler section."""
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "software"}
    result = _render_template_archetype("CLAUDE.md.tpl", vars_, here, "software")
    assert "Error Handler" in result


def test_template_archetype_content_no_error_handler():
    """Rendered CLAUDE.md for content type does NOT contain error handler section."""
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "content"}
    result = _render_template_archetype("CLAUDE.md.tpl", vars_, here, "content")
    assert "Error Handler" not in result


def test_template_archetype_software_has_stack():
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "software"}
    result = _render_template_archetype("CLAUDE.md.tpl", vars_, here, "software")
    assert "## Stack" in result


def test_template_archetype_content_no_stack():
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "content"}
    result = _render_template_archetype("CLAUDE.md.tpl", vars_, here, "content")
    assert "## Stack" not in result


def test_template_archetype_tasks_software_has_log_cmd():
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "software"}
    result = _render_template_archetype("TASKS.md.tpl", vars_, here, "software")
    assert "log_cmd" in result


def test_template_archetype_tasks_content_no_log_cmd():
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "content"}
    result = _render_template_archetype("TASKS.md.tpl", vars_, here, "content")
    assert "log_cmd" not in result


# ─────────────────────────── API endpoint fixtures ─────────────────────────────

@pytest.fixture
def new_project_ctx(tmp_path):
    """Minimal ctx for api_new_project tests."""
    password = "testpass"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
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
        "run_engine": None,  # degraded mode — no actual agent launch
        "ptb_app": None,
        "GROUP_CHAT_ID": 0,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx, tmp_path


@pytest.fixture
def new_project_app(new_project_ctx):
    from aiohttp import web

    ctx, tmp_path = new_project_ctx

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_post("/api/projects/new", _webapp.api_new_project)

    return app, ctx, tmp_path


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── API endpoint tests ─────────────────────────────────

async def test_new_project_type_stored(aiohttp_client, new_project_app, monkeypatch):
    """POST /api/projects/new with intent → topic_entry has 'type' field set."""
    app, ctx, tmp_path = new_project_app
    # Patch Path.home() to use tmp_path so we don't write to real ~/projects
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "build a python api service"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("id") is not None

    # Check that topic_entry has 'type' stored
    pid = data["id"]
    assert pid in ctx["topics"]
    entry = ctx["topics"][pid]
    assert "type" in entry
    assert entry["type"] == "software"


async def test_new_project_software_gitignore_excluded_for_content(aiohttp_client, new_project_app, monkeypatch):
    """Content type does NOT get .gitignore written."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "write a blog post about machine learning", "type": "content"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    cwd = Path(data["cwd"])

    # .gitignore should NOT exist for content type
    assert not (cwd / ".gitignore").exists()
    # CLAUDE.md should exist
    assert (cwd / "CLAUDE.md").exists()
    # CLAUDE.md should NOT contain error handler section
    claude_md = (cwd / "CLAUDE.md").read_text()
    assert "Error Handler" not in claude_md


async def test_new_project_software_has_gitignore(aiohttp_client, new_project_app, monkeypatch):
    """Software type DOES get .gitignore written."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "build a react website"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    cwd = Path(data["cwd"])
    assert (cwd / ".gitignore").exists()


# ─────────────── engineering-skills wiring (mattpocock/skills → board) ───────────

def test_template_archetype_software_has_agent_skills():
    """Rendered CLAUDE.md for software type carries the ## Agent skills block."""
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "software"}
    result = _render_template_archetype("CLAUDE.md.tpl", vars_, here, "software")
    assert "## Agent skills" in result
    assert "docs/agents/issue-tracker.md" in result


def test_template_archetype_content_no_agent_skills():
    """Content type does NOT get the ## Agent skills block (software/ops only)."""
    here = ROOT
    vars_ = {"name": "Test", "date": "2026-01-01", "slug": "test", "type": "content"}
    result = _render_template_archetype("CLAUDE.md.tpl", vars_, here, "content")
    assert "## Agent skills" not in result


async def test_new_project_software_writes_agents_docs(aiohttp_client, new_project_app, monkeypatch):
    """Software type ships board-mapped docs/agents/*.md so mattpocock/skills target the board."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "build a python api service"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    cwd = Path((await resp.json())["cwd"])
    agents = cwd / "docs" / "agents"
    for fn in ("issue-tracker.md", "domain.md", "triage-labels.md"):
        assert (agents / fn).exists(), f"missing docs/agents/{fn}"
    # Board-mapped, not GitHub Issues.
    assert "Cardloop board" in (agents / "issue-tracker.md").read_text()


async def test_new_project_content_no_agents_docs(aiohttp_client, new_project_app, monkeypatch):
    """Content/personal projects don't get the engineering-skills wiring."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "write a blog post about machine learning", "type": "content"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    cwd = Path((await resp.json())["cwd"])
    assert not (cwd / "docs" / "agents").exists()


async def test_new_project_no_tg_topic_created(aiohttp_client, new_project_app, monkeypatch):
    """Even if ptb_app mock exists, TG forum topic is NOT created (Phase D)."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Install a mock ptb_app that would error if create_forum_topic is called
    class MockBot:
        async def create_forum_topic(self, **kwargs):
            raise AssertionError("create_forum_topic should not be called (Phase D)")

    class MockPtbApp:
        bot = MockBot()

    ctx["ptb_app"] = MockPtbApp()
    ctx["GROUP_CHAT_ID"] = -100123456789

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "automate my server backup"},
        headers=_auth_headers(ctx),
    )
    # Should succeed (200) without calling create_forum_topic
    assert resp.status == 200
    data = await resp.json()
    pid = data["id"]
    # No tg_key in topic entry
    assert "tg_key" not in ctx["topics"].get(pid, {})


async def test_new_project_has_uuid_id_field(aiohttp_client, new_project_app, monkeypatch):
    """New topic entries include a stable UUID 'id' field."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={"intent": "build a cli tool"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    pid = data["id"]
    entry = ctx["topics"][pid]
    assert "id" in entry
    # Looks like a UUID
    import uuid
    uuid.UUID(entry["id"])  # raises if not valid UUID


async def test_new_project_empty_intent_creates_untitled(aiohttp_client, new_project_app, monkeypatch):
    """Empty intent → untitled-<ts> slug, default software type."""
    app, ctx, tmp_path = new_project_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/new",
        json={},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["id"].startswith("untitled-")
    pid = data["id"]
    assert ctx["topics"][pid]["type"] == "software"

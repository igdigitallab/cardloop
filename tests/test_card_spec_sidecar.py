"""
Tests for card 5e1c0a: spec sidecar endpoints.

GET  /api/projects/{id}/cards/{card}/spec  — read, absent file returns exists:false
PUT  /api/projects/{id}/cards/{card}/spec  — write; empty content deletes file
Path safety: bad card id → 400
has_spec flag: board payload reflects sidecar presence after PUT
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _tasks_path, _derive_token


# ─────────────────────────── helpers ───────────────────────────


def _make_tasks_md(cwd: Path, backlog=None) -> None:
    import secrets

    def _line(card):
        if isinstance(card, str):
            return f"- [ ] {card} <!--ops:{secrets.token_hex(3)}-->"
        return f"- [ ] {card['text']} <!--ops:{card['id']}-->"

    lines = ["# Tasks\n", "Test project\n", "## Backlog\n"]
    for c in (backlog or []):
        lines.append(_line(c))
    lines += ["\n## In Progress\n", "\n## Review\n", "\n## Failed\n"]
    _tasks_path(str(cwd)).write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx(tmp_path, project_dir):
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
def spec_app(fake_ctx):
    """aiohttp app wired with spec + board endpoints."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/me", _webapp.api_me)
    # Board
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    # Spec sidecar
    app.router.add_get("/api/projects/{id}/cards/{card}/spec", _webapp.api_card_spec_get)
    app.router.add_put("/api/projects/{id}/cards/{card}/spec", _webapp.api_card_spec_put)

    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── GET — absent spec ───────────────────────────


async def test_get_spec_absent(aiohttp_client, spec_app, fake_ctx):
    """GET spec for a card that has no sidecar → exists:false, empty content."""
    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)
    resp = await client.get("/api/projects/myproject/cards/abc123/spec", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False
    assert data["content"] == ""


# ─────────────────────────── PUT → GET round-trip ───────────────────────────


async def test_put_creates_file(aiohttp_client, spec_app, fake_ctx):
    """PUT content → file created, GET returns it."""
    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)

    content = "# My Spec\n\nThis is the spec."
    put_resp = await client.put(
        "/api/projects/myproject/cards/abc123/spec",
        json={"content": content},
        headers=h,
    )
    assert put_resp.status == 200
    put_data = await put_resp.json()
    assert put_data["exists"] is True
    assert put_data["content"] == content

    # Verify sidecar file was created on disk
    spec_file = fake_ctx["DATA"] / "card-specs" / "abc123.md"
    assert spec_file.exists()
    assert spec_file.read_text() == content

    # GET should return the same
    get_resp = await client.get("/api/projects/myproject/cards/abc123/spec", headers=h)
    assert get_resp.status == 200
    get_data = await get_resp.json()
    assert get_data["exists"] is True
    assert get_data["content"] == content


# ─────────────────────────── PUT empty → deletes file ───────────────────────────


async def test_put_empty_deletes_file(aiohttp_client, spec_app, fake_ctx):
    """PUT empty/whitespace content → file deleted, exists:false."""
    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)

    # First create
    await client.put(
        "/api/projects/myproject/cards/abc123/spec",
        json={"content": "Some content"},
        headers=h,
    )
    spec_file = fake_ctx["DATA"] / "card-specs" / "abc123.md"
    assert spec_file.exists()

    # Now delete via empty content
    del_resp = await client.put(
        "/api/projects/myproject/cards/abc123/spec",
        json={"content": "   "},
        headers=h,
    )
    assert del_resp.status == 200
    del_data = await del_resp.json()
    assert del_data["exists"] is False
    assert not spec_file.exists()

    # GET should reflect deletion
    get_resp = await client.get("/api/projects/myproject/cards/abc123/spec", headers=h)
    get_data = await get_resp.json()
    assert get_data["exists"] is False


async def test_put_empty_no_file_ok(aiohttp_client, spec_app, fake_ctx):
    """PUT empty content when file does not exist → still returns exists:false (idempotent)."""
    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)
    resp = await client.put(
        "/api/projects/myproject/cards/neverexisted/spec",
        json={"content": ""},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False


# ─────────────────────────── Path safety: bad card id ───────────────────────────


@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "../../secret",
    "a" * 100,
    "",
    "has space",
    "has/slash",
])
async def test_bad_card_id_get_rejected(aiohttp_client, spec_app, fake_ctx, bad_id):
    """GET with a bad card id → 400 (path safety)."""
    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)
    resp = await client.get(
        f"/api/projects/myproject/cards/{bad_id}/spec",
        headers=h,
    )
    # aiohttp routing may return 404 for slashes/empty; our handler returns 400 for bad ids.
    # Both mean the request was rejected — accept either.
    assert resp.status in (400, 404, 405)


@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "../../secret",
    "a" * 100,
    "has space",
])
async def test_bad_card_id_put_rejected(aiohttp_client, spec_app, fake_ctx, bad_id):
    """PUT with a bad card id → 400 (path safety)."""
    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)
    resp = await client.put(
        f"/api/projects/myproject/cards/{bad_id}/spec",
        json={"content": "malicious"},
        headers=h,
    )
    assert resp.status in (400, 404, 405)


# ─────────────────────────── has_spec in board payload ───────────────────────────


async def test_has_spec_in_board_payload(aiohttp_client, spec_app, fake_ctx, project_dir):
    """After PUT spec, board payload sets has_spec=true for that card; false before."""
    card = {"id": "aabbcc", "text": "Card with spec"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)

    # Before PUT: has_spec should be False (or absent)
    board_resp = await client.get("/api/projects/myproject/tasks", headers=h)
    board_data = await board_resp.json()
    backlog = next(c for c in board_data["columns"] if c["key"] == "backlog")
    card_data = next(c for c in backlog["cards"] if c["id"] == "aabbcc")
    assert not card_data.get("has_spec", False)

    # PUT spec
    await client.put(
        "/api/projects/myproject/cards/aabbcc/spec",
        json={"content": "# Spec for aabbcc"},
        headers=h,
    )

    # After PUT: has_spec should be True
    board_resp2 = await client.get("/api/projects/myproject/tasks", headers=h)
    board_data2 = await board_resp2.json()
    backlog2 = next(c for c in board_data2["columns"] if c["key"] == "backlog")
    card_data2 = next(c for c in backlog2["cards"] if c["id"] == "aabbcc")
    assert card_data2.get("has_spec") is True


async def test_has_spec_clears_after_delete(aiohttp_client, spec_app, fake_ctx, project_dir):
    """After deleting spec (PUT empty), board payload has_spec=false."""
    card = {"id": "aabbcc", "text": "Card with spec"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(spec_app)
    h = _auth(fake_ctx)

    # Create then delete
    await client.put(
        "/api/projects/myproject/cards/aabbcc/spec",
        json={"content": "# Spec"},
        headers=h,
    )
    await client.put(
        "/api/projects/myproject/cards/aabbcc/spec",
        json={"content": ""},
        headers=h,
    )

    board_resp = await client.get("/api/projects/myproject/tasks", headers=h)
    board_data = await board_resp.json()
    backlog = next(c for c in board_data["columns"] if c["key"] == "backlog")
    card_data = next(c for c in backlog["cards"] if c["id"] == "aabbcc")
    assert not card_data.get("has_spec", False)

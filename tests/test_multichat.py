"""
Tests for spec-037: multi-chat per project.

Covers:
- chats.json CRUD + lock round-trip
- refuse delete last chat (400)
- active fallback when deleting the active chat
- migration seeds "Main" from ctx["sessions"][session_key] (no context loss)
- api_project_chat resolves AND writes back active chat's session_id (not the flat map)
- ctx["sessions"] mirrors the active chat so TG/card read-through still works
"""
import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _load_chats,
    _save_chats,
    _ensure_chat_entry,
    _mirror_active_chat_to_sessions,
    _new_chat_id,
    _valid_chat_id,
    _derive_token,
)


# ─────────────────────────── fixtures ────────────────────────────────────────


@pytest.fixture
def fake_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(tmp_path / "myproject"),
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
    (tmp_path / "myproject").mkdir()
    return ctx


@pytest.fixture
def chats_app(fake_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/me", _webapp.api_me)
    app.router.add_get("/api/projects/{id}/chats", _webapp.api_project_chats_list)
    app.router.add_post("/api/projects/{id}/chats", _webapp.api_project_chats_create)
    app.router.add_route("PATCH", "/api/projects/{id}/chats/{chat_id}", _webapp.api_project_chats_patch)
    app.router.add_delete("/api/projects/{id}/chats/{chat_id}", _webapp.api_project_chats_delete)

    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── unit: helpers ────────────────────────────────────


def test_new_chat_id_format():
    """Generated chat ids are 6-char lowercase hex."""
    for _ in range(20):
        cid = _new_chat_id()
        assert _valid_chat_id(cid), f"invalid id: {cid!r}"


def test_valid_chat_id_rejects_bad():
    assert not _valid_chat_id("../etc")
    assert not _valid_chat_id("")
    assert not _valid_chat_id("toolong" * 10)
    assert not _valid_chat_id("ABCDEF")   # uppercase not allowed


def test_valid_chat_id_accepts_good():
    assert _valid_chat_id("abc123")
    assert _valid_chat_id("000000")
    assert _valid_chat_id("ffffff")


# ─────────────────────────── unit: _ensure_chat_entry migration ───────────────


def test_ensure_chat_entry_seeds_main_from_existing_session(fake_ctx):
    """Migration: first access seeds 'Main' chat with the existing session_id (no context loss)."""
    fake_ctx["sessions"]["1001:42"] = "existing-session-id-abc"
    chats_data = _ensure_chat_entry(fake_ctx, "myproject", "1001:42")
    entry = chats_data["myproject"]
    assert len(entry["chats"]) == 1
    main = entry["chats"][0]
    assert main["name"] == "Main"
    # The existing session_id must be preserved in the Main chat
    assert main["session_id"] == "existing-session-id-abc"
    assert entry["active"] == main["id"]


def test_ensure_chat_entry_seeds_main_no_session(fake_ctx):
    """Migration: first access with no existing session seeds Main with session_id=None."""
    chats_data = _ensure_chat_entry(fake_ctx, "myproject", "1001:42")
    entry = chats_data["myproject"]
    main = entry["chats"][0]
    assert main["name"] == "Main"
    assert main["session_id"] is None


def test_ensure_chat_entry_idempotent(fake_ctx):
    """Calling _ensure_chat_entry twice does not duplicate the Main entry."""
    _ensure_chat_entry(fake_ctx, "myproject", "1001:42")
    chats_data2 = _ensure_chat_entry(fake_ctx, "myproject", "1001:42")
    assert len(chats_data2["myproject"]["chats"]) == 1


# ─────────────────────────── unit: _mirror_active_chat_to_sessions ────────────


def test_mirror_sets_sessions(fake_ctx):
    """_mirror_active_chat_to_sessions writes session_id into ctx['sessions']."""
    chat_id = _new_chat_id()
    chats_data = {
        "myproject": {
            "active": chat_id,
            "chats": [{"id": chat_id, "name": "Main", "session_id": "sid-xyz", "created_at": 0}],
        }
    }
    _mirror_active_chat_to_sessions(fake_ctx, "myproject", "1001:42", chats_data)
    assert fake_ctx["sessions"]["1001:42"] == "sid-xyz"


def test_mirror_clears_sessions_when_null(fake_ctx):
    """_mirror_active_chat_to_sessions removes the key when session_id is None."""
    fake_ctx["sessions"]["1001:42"] = "old-sid"
    chat_id = _new_chat_id()
    chats_data = {
        "myproject": {
            "active": chat_id,
            "chats": [{"id": chat_id, "name": "Main", "session_id": None, "created_at": 0}],
        }
    }
    _mirror_active_chat_to_sessions(fake_ctx, "myproject", "1001:42", chats_data)
    assert "1001:42" not in fake_ctx["sessions"]


# ─────────────────────────── unit: load/save round-trip ──────────────────────


def test_save_load_roundtrip(fake_ctx):
    """Saving and loading chats.json returns the same data."""
    data = {
        "myproject": {
            "active": "aabbcc",
            "chats": [{"id": "aabbcc", "name": "Main", "session_id": None, "created_at": 1000.0}],
        }
    }
    _save_chats(fake_ctx, data)
    loaded = _load_chats(fake_ctx)
    assert loaded == data


def test_load_missing_returns_empty(fake_ctx):
    """Loading when chats.json does not exist returns {}."""
    result = _load_chats(fake_ctx)
    assert result == {}


# ─────────────────────────── API: GET /chats ─────────────────────────────────


async def test_get_chats_creates_main_on_first_access(aiohttp_client, chats_app, fake_ctx):
    """GET /chats on a project with no chats.json → seeds Main, returns it."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    resp = await client.get("/api/projects/myproject/chats", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert len(data["chats"]) == 1
    assert data["chats"][0]["name"] == "Main"
    assert data["active"] == data["chats"][0]["id"]


async def test_get_chats_preserves_existing_session(aiohttp_client, chats_app, fake_ctx):
    """GET /chats seeds Main with the pre-existing session_id (migration = no context loss)."""
    fake_ctx["sessions"]["1001:42"] = "my-live-session"
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    resp = await client.get("/api/projects/myproject/chats", headers=h)
    assert resp.status == 200
    data = await resp.json()
    main = data["chats"][0]
    assert main["session_id"] == "my-live-session"


# ─────────────────────────── API: POST /chats ─────────────────────────────────


async def test_create_chat(aiohttp_client, chats_app, fake_ctx):
    """POST /chats → creates a new chat with session_id=null."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    resp = await client.post("/api/projects/myproject/chats", json={"name": "Sprint 2"}, headers=h)
    assert resp.status == 201
    data = await resp.json()
    assert data["name"] == "Sprint 2"
    assert data["session_id"] is None
    assert _valid_chat_id(data["id"])


async def test_create_chat_default_name(aiohttp_client, chats_app, fake_ctx):
    """POST /chats with no name → defaults to 'Chat'."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    resp = await client.post("/api/projects/myproject/chats", json={}, headers=h)
    assert resp.status == 201
    data = await resp.json()
    assert data["name"] == "Chat"


# ─────────────────────────── API: PATCH /chats/{id} ──────────────────────────


async def test_patch_rename(aiohttp_client, chats_app, fake_ctx):
    """PATCH chat → rename updates the name."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    # First: create a chat
    cr = await client.post("/api/projects/myproject/chats", json={"name": "Old"}, headers=h)
    cid = (await cr.json())["id"]

    resp = await client.patch(f"/api/projects/myproject/chats/{cid}", json={"name": "New"}, headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["chat"]["name"] == "New"


async def test_patch_set_active_mirrors_sessions(aiohttp_client, chats_app, fake_ctx):
    """PATCH active=true → ctx['sessions'] mirrors that chat's session_id (TG read-through)."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)

    # Seed first chat (Main) with a session_id
    fake_ctx["sessions"]["1001:42"] = "main-sid"
    await client.get("/api/projects/myproject/chats", headers=h)  # ensure Main seeded

    # Create second chat
    cr = await client.post("/api/projects/myproject/chats", json={"name": "Alt"}, headers=h)
    alt_id = (await cr.json())["id"]

    # Manually set its session_id in chats.json
    chats_data = _load_chats(fake_ctx)
    alt_chat = next(c for c in chats_data["myproject"]["chats"] if c["id"] == alt_id)
    alt_chat["session_id"] = "alt-sid"
    _save_chats(fake_ctx, chats_data)

    # PATCH to set alt as active
    resp = await client.patch(f"/api/projects/myproject/chats/{alt_id}", json={"active": True}, headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["active"] == alt_id

    # ctx["sessions"] must now hold the alt chat's session_id (TG/card read-through)
    assert fake_ctx["sessions"].get("1001:42") == "alt-sid"


async def test_patch_invalid_chat_id(aiohttp_client, chats_app, fake_ctx):
    """PATCH with bad chat_id → 400."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    resp = await client.patch("/api/projects/myproject/chats/../etc", json={"name": "x"}, headers=h)
    assert resp.status in (400, 404)


# ─────────────────────────── API: DELETE /chats/{id} ─────────────────────────


async def test_delete_non_active_chat(aiohttp_client, chats_app, fake_ctx):
    """DELETE a non-active chat → ok, active chat unchanged."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    # Ensure Main seeded
    gr = await client.get("/api/projects/myproject/chats", headers=h)
    main_id = (await gr.json())["active"]

    # Create second chat
    cr = await client.post("/api/projects/myproject/chats", json={"name": "Alt"}, headers=h)
    alt_id = (await cr.json())["id"]

    # Delete second chat
    resp = await client.delete(f"/api/projects/myproject/chats/{alt_id}", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["active"] == main_id


async def test_delete_active_chat_falls_back(aiohttp_client, chats_app, fake_ctx):
    """DELETE the active chat → another chat becomes active; ctx['sessions'] mirrors it."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)

    # Ensure Main seeded
    gr = await client.get("/api/projects/myproject/chats", headers=h)
    main_id = (await gr.json())["active"]

    # Create second chat and make it active
    cr = await client.post("/api/projects/myproject/chats", json={"name": "Alt"}, headers=h)
    alt_id = (await cr.json())["id"]
    await client.patch(f"/api/projects/myproject/chats/{alt_id}", json={"active": True}, headers=h)

    # Delete alt (active)
    resp = await client.delete(f"/api/projects/myproject/chats/{alt_id}", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["active"] == main_id


async def test_delete_last_chat_rejected(aiohttp_client, chats_app, fake_ctx):
    """DELETE the last remaining chat → 400."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)

    # Seed Main (only chat)
    gr = await client.get("/api/projects/myproject/chats", headers=h)
    main_id = (await gr.json())["active"]

    resp = await client.delete(f"/api/projects/myproject/chats/{main_id}", headers=h)
    assert resp.status == 400
    data = await resp.json()
    assert "last" in data["error"].lower()


async def test_delete_invalid_chat_id(aiohttp_client, chats_app, fake_ctx):
    """DELETE with a bad chat_id → 400."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)
    resp = await client.delete("/api/projects/myproject/chats/../../etc", headers=h)
    assert resp.status in (400, 404)


# ─────────────────────────── ctx["sessions"] as derived cache ─────────────────


async def test_sessions_derived_cache_tg_readthrough(aiohttp_client, chats_app, fake_ctx):
    """After switching active chat, ctx['sessions'][session_key] always holds the active chat's
    session_id — TG run_agent and _run_card read-through still see correct value."""
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)

    # Seed Main with a known session_id via migration
    fake_ctx["sessions"]["1001:42"] = "tg-session-abc"
    gr = await client.get("/api/projects/myproject/chats", headers=h)
    main_id = (await gr.json())["active"]

    # Create chat B with a different session_id
    cr = await client.post("/api/projects/myproject/chats", json={"name": "Chat B"}, headers=h)
    b_id = (await cr.json())["id"]
    chats_data = _load_chats(fake_ctx)
    b_chat = next(c for c in chats_data["myproject"]["chats"] if c["id"] == b_id)
    b_chat["session_id"] = "chatb-session-xyz"
    _save_chats(fake_ctx, chats_data)

    # Switch to chat B → ctx["sessions"] must reflect chatb-session-xyz
    await client.patch(f"/api/projects/myproject/chats/{b_id}", json={"active": True}, headers=h)
    assert fake_ctx["sessions"]["1001:42"] == "chatb-session-xyz"

    # Switch back to Main → ctx["sessions"] must reflect tg-session-abc
    await client.patch(f"/api/projects/myproject/chats/{main_id}", json={"active": True}, headers=h)
    assert fake_ctx["sessions"]["1001:42"] == "tg-session-abc"


# ─────────────────────────── file-lock atomicity ──────────────────────────────


def test_save_chats_atomic(fake_ctx):
    """_save_chats uses tmp+replace (no partial file visible to readers)."""
    data = {"myproject": {"active": "aabbcc", "chats": []}}
    _save_chats(fake_ctx, data)
    p = fake_ctx["DATA"] / "chats.json"
    tmp = fake_ctx["DATA"] / "chats.json.tmp"
    assert p.exists()
    assert not tmp.exists()  # tmp should be gone after atomic replace


# ─────────────────────────── startup desync repair ────────────────────────────


async def test_get_chats_repairs_sessions_desync(aiohttp_client, chats_app, fake_ctx):
    """GET /chats syncs ctx["sessions"] from the active chat's session_id.

    Scenario: sessions.json was NOT updated after a run (save_sessions failed),
    so ctx["sessions"] holds a stale value while chats.json has the correct
    session_id. The first GET /chats after a service restart must repair the cache.
    """
    client = await aiohttp_client(chats_app)
    h = _auth(fake_ctx)

    # Seed chats.json manually: Main has session_id "real-session-abc"
    chat_id = "aa1122"
    chats_data = {
        "myproject": {
            "active": chat_id,
            "chats": [{"id": chat_id, "name": "Main", "session_id": "real-session-abc", "created_at": 0}],
        }
    }
    _save_chats(fake_ctx, chats_data)

    # Simulate stale ctx["sessions"] (e.g. sessions.json had old value from before the run)
    fake_ctx["sessions"]["1001:42"] = "stale-session-xyz"

    # GET /chats should repair ctx["sessions"] to match chats.json's active chat
    resp = await client.get("/api/projects/myproject/chats", headers=h)
    assert resp.status == 200
    assert fake_ctx["sessions"]["1001:42"] == "real-session-abc", (
        "GET /chats must sync ctx['sessions'] from the active chat's session_id "
        f"to repair startup desync, got: {fake_ctx['sessions'].get('1001:42')!r}"
    )

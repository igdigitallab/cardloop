"""
spec-047 workstream A — version & self-update endpoints.

Covers:
- _version_info() logic: clean/behind/dirty/no-origin/detached/no-systemd
- GET /api/version: auth + JSON shape
- POST /api/update: 409 (can't self-update), 200 (up to date), 202 (spawns updater)

git and subprocess are monkeypatched — no real git/network/process is touched.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from aiohttp import web


def _fake_git(mapping, *, default=(1, "")):
    """Build a fake _git(here, *args) that looks up by the args tuple's key verb."""
    def _g(here, *args, timeout=5.0):
        # Match on a distinctive subcommand signature.
        key = args[0]
        if key == "rev-parse" and "--git-dir" in args:
            return mapping.get("is_git", (0, ".git"))
        if key == "rev-parse" and "--abbrev-ref" in args:
            return mapping.get("branch", (0, "master"))
        if key == "describe":
            return mapping.get("describe", (0, "v1.2.3"))
        if key == "remote":
            return mapping.get("origin", (0, "git@example.com:x/y.git"))
        if key == "status":
            return mapping.get("status", (0, ""))
        if key == "rev-list":
            return mapping.get("behind", (0, "0"))
        if key == "tag":
            return mapping.get("tags", (0, "v1.2.3"))
        return default
    return _g


def _ctx(tmp_path):
    data = tmp_path / "data"; data.mkdir(exist_ok=True)
    password = "secr3t"
    ctx = {
        "password": password, "DATA": data, "HERE": ROOT,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _webapp._derive_token(password)
    return ctx


def _app(ctx):
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/version", _webapp.api_version)
    app.router.add_post("/api/update", _webapp.api_update)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── _version_info logic ───────────────────────────

def test_version_clean_up_to_date(monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "0")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    info = _webapp._version_info(ROOT)
    assert info["current"] == "v1.2.3"
    assert info["behind"] == 0
    assert info["update_available"] is False
    assert info["can_self_update"] is True
    assert info["reason"] is None


def test_version_behind_update_available(monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "3"), "tags": (0, "v1.3.0\nv1.2.3")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    info = _webapp._version_info(ROOT)
    assert info["behind"] == 3
    assert info["latest"] == "v1.3.0"
    assert info["update_available"] is True
    assert info["can_self_update"] is True


def test_version_dirty_blocks_self_update(monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "2"), "status": (0, " M webapp.py")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    info = _webapp._version_info(ROOT)
    assert info["can_self_update"] is False
    assert info["update_available"] is False
    assert "local changes" in info["reason"]


def test_version_no_origin(monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"origin": (1, "")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    info = _webapp._version_info(ROOT)
    assert info["can_self_update"] is False
    assert "origin" in info["reason"]


def test_version_not_git(monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"is_git": (1, "")}))
    info = _webapp._version_info(ROOT)
    assert info["can_self_update"] is False
    assert info["current"] == "unknown"


def test_version_no_systemd_docker(monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "1")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: None)
    info = _webapp._version_info(ROOT)
    assert info["can_self_update"] is False
    assert "docker" in info["reason"].lower()


# ─────────────────────────── endpoints ───────────────────────────

async def test_api_version_requires_auth(aiohttp_client, tmp_path):
    ctx = _ctx(tmp_path)
    client = await aiohttp_client(_app(ctx))
    resp = await client.get("/api/version")
    assert resp.status == 401


async def test_api_version_shape(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "0")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    # avoid background fetch in the test
    monkeypatch.setattr(_webapp, "_version_fetch_at", _webapp.time.time())
    ctx = _ctx(tmp_path)
    client = await aiohttp_client(_app(ctx))
    resp = await client.get("/api/version", headers=_auth(ctx))
    assert resp.status == 200
    data = await resp.json()
    assert set(["current", "latest", "behind", "update_available",
                "channel", "can_self_update", "reason", "update_status"]).issubset(data)


async def test_api_update_conflict_when_dirty(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "2"), "status": (0, " M x")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    ctx = _ctx(tmp_path)
    client = await aiohttp_client(_app(ctx))
    resp = await client.post("/api/update", headers=_auth(ctx))
    assert resp.status == 409


async def test_api_update_up_to_date(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "0")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    ctx = _ctx(tmp_path)
    client = await aiohttp_client(_app(ctx))
    resp = await client.post("/api/update", headers=_auth(ctx))
    assert resp.status == 200
    assert (await resp.json())["status"] == "up_to_date"


async def test_api_update_spawns_detached(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_git", _fake_git({"behind": (0, "2"), "tags": (0, "v9.9.9")}))
    monkeypatch.setattr(_webapp.shutil, "which", lambda _: "/usr/bin/systemctl")
    # updater script must exist (it does in the repo); intercept Popen so nothing runs
    spawned = {}
    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        spawned["new_session"] = kw.get("start_new_session")
        class _P: pass
        return _P()
    monkeypatch.setattr(_webapp.subprocess, "Popen", fake_popen)
    ctx = _ctx(tmp_path)
    client = await aiohttp_client(_app(ctx))
    resp = await client.post("/api/update", headers=_auth(ctx))
    assert resp.status == 202
    assert (await resp.json())["status"] == "updating"
    assert spawned["new_session"] is True
    assert "self-update.sh" in " ".join(spawned["cmd"])

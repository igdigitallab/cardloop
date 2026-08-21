"""HTTP surface of the multi-subscription switch.

The endpoint-level invariants:
- /api/usage keeps its exact old shape on a single-account install (no `accounts` block).
- Switching invalidates the usage cache, so the badge can never show the previous
  subscription's percentages after a move.
- An unusable account is refused with a reason instead of being silently activated.
- A non-active account's limits are read from ITS credentials file, never the main one's.
"""
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
import accounts as _accounts
from webapp import _derive_token


@pytest.fixture
def acct_env(tmp_path, monkeypatch):
    """Isolated DATA + accounts root + a fake ~/.claude holding the main credentials."""
    data = tmp_path / "data"
    data.mkdir()
    root = tmp_path / "accts"
    root.mkdir()
    home = tmp_path / "home"
    main_cfg = home / ".claude"
    (main_cfg / "projects").mkdir(parents=True)
    (main_cfg / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-main", "subscriptionType": "max"}}))

    monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_ACCOUNTS_DIR", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(main_cfg))
    monkeypatch.delenv("CLAUDE_CREDENTIALS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Never let a test touch the real usage endpoint or leak cache between tests.
    _webapp._usage_cache.update({"data": None, "ts": 0.0, "account": _accounts.MAIN_ID})
    _webapp._usage_cache_others.clear()
    yield tmp_path
    _webapp._usage_cache.update({"data": None, "ts": 0.0, "account": _accounts.MAIN_ID})
    _webapp._usage_cache_others.clear()


@pytest.fixture
def app(tmp_path, acct_env):
    ctx = {
        "topics": {}, "sessions": {}, "running": {}, "password": "testpass",
        "DATA": tmp_path / "data", "HERE": ROOT, "rate_limits": {},
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None,
    }
    ctx["_auth_token"] = _derive_token("testpass")
    application = web.Application(middlewares=[_webapp.auth_middleware])
    application["ctx"] = ctx
    application.router.add_get("/api/usage", _webapp.api_usage)
    application.router.add_get("/api/accounts", _webapp.api_accounts)
    application.router.add_post("/api/accounts", _webapp.api_accounts_create)
    application.router.add_post("/api/accounts/active", _webapp.api_accounts_activate)
    application.router.add_post("/api/accounts/remove", _webapp.api_accounts_remove)
    return application


def _hdr(app):
    return {"Cookie": f"cops_auth={app['ctx']['_auth_token']}"}


def _login(aid, token="tok-second"):
    cdir = _accounts.accounts_root() / aid
    (cdir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, "subscriptionType": "max"}}))


async def test_single_account_usage_payload_is_unchanged(aiohttp_client, app, monkeypatch):
    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", lambda *a, **k: _noop())
    client = await aiohttp_client(app)
    body = await (await client.get("/api/usage", headers=_hdr(app))).json()
    assert body["accounts"] is None          # nothing extra registered
    assert body["account"] == "main"
    assert "limits" in body and "codex" in body


async def _noop():
    return None


async def test_create_then_activate_flow(aiohttp_client, app, monkeypatch):
    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", lambda *a, **k: _noop())
    client = await aiohttp_client(app)

    r = await client.post("/api/accounts", json={"id": "work", "label": "Work"}, headers=_hdr(app))
    created = await r.json()
    assert r.status == 200 and created["ok"]
    assert "projects" in created["linked"]          # history stays shared
    assert created["account"]["ok"] is False        # not logged in yet

    # Activating before login must be refused, with a reason the UI can show.
    r = await client.post("/api/accounts/active", json={"id": "work"}, headers=_hdr(app))
    assert r.status == 400
    assert "not logged in" in (await r.json())["error"]
    assert _accounts.active_id() == "main"

    _login("work")
    r = await client.post("/api/accounts/active", json={"id": "work"}, headers=_hdr(app))
    assert r.status == 200 and (await r.json())["active"] == "work"
    assert _accounts.active_id() == "work"
    assert _accounts.env_overrides()["CLAUDE_CONFIG_DIR"].endswith("/work")


async def test_switch_invalidates_the_usage_cache(aiohttp_client, app, monkeypatch):
    """After a switch the badge must not keep showing the old subscription's numbers."""
    seen: list = []

    async def fake_fetch(creds_path=None):
        seen.append(creds_path)
        pct = 90 if creds_path and creds_path.endswith("home/.claude/.credentials.json") else 10
        return {"five_hour": {"utilization": pct, "resets_at": None}}

    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", fake_fetch)
    client = await aiohttp_client(app)

    body = await (await client.get("/api/usage", headers=_hdr(app))).json()
    assert body["limits"]["five_hour"]["utilization"] == pytest.approx(0.9)

    await client.post("/api/accounts", json={"id": "work"}, headers=_hdr(app))
    _login("work")
    await client.post("/api/accounts/active", json={"id": "work"}, headers=_hdr(app))

    body = await (await client.get("/api/usage", headers=_hdr(app))).json()
    assert body["account"] == "work"
    assert body["limits"]["five_hour"]["utilization"] == pytest.approx(0.1)   # the OTHER account
    # The active account's own credentials were read (main is also polled, for its row).
    assert any(p.endswith("/work/.credentials.json") for p in seen)


async def test_accounts_block_reports_both_subscriptions(aiohttp_client, app, monkeypatch):
    async def fake_fetch(creds_path=None):
        pct = 12 if creds_path and creds_path.endswith("/work/.credentials.json") else 77
        return {"five_hour": {"utilization": pct, "resets_at": None}}

    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", fake_fetch)
    client = await aiohttp_client(app)
    await client.post("/api/accounts", json={"id": "work", "label": "Work"}, headers=_hdr(app))
    _login("work")

    body = await (await client.get("/api/usage", headers=_hdr(app))).json()
    rows = {a["id"]: a for a in body["accounts"]}
    assert rows["main"]["active"] is True
    assert rows["main"]["limits"]["five_hour"]["utilization"] == pytest.approx(0.77)
    # The inactive account is polled with its OWN credentials.
    assert rows["work"]["limits"]["five_hour"]["utilization"] == pytest.approx(0.12)


async def test_inactive_account_with_stale_token_shows_no_numbers(aiohttp_client, app, monkeypatch):
    """A sleeping account's token expires — showing nothing beats inventing a 0%."""
    async def fake_fetch(creds_path=None):
        if creds_path and creds_path.endswith("/work/.credentials.json"):
            return None                       # 401 from the usage endpoint
        return {"five_hour": {"utilization": 50, "resets_at": None}}

    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", fake_fetch)
    client = await aiohttp_client(app)
    await client.post("/api/accounts", json={"id": "work"}, headers=_hdr(app))
    _login("work")

    body = await (await client.get("/api/usage", headers=_hdr(app))).json()
    rows = {a["id"]: a for a in body["accounts"]}
    assert rows["work"]["ok"] is True          # usable for runs…
    assert rows["work"]["limits"] is None      # …but its quota is simply unknown
    assert rows["work"]["limits_ts"] is None


async def test_remove_account_returns_to_main(aiohttp_client, app, monkeypatch):
    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", lambda *a, **k: _noop())
    client = await aiohttp_client(app)
    await client.post("/api/accounts", json={"id": "work"}, headers=_hdr(app))
    _login("work")
    await client.post("/api/accounts/active", json={"id": "work"}, headers=_hdr(app))

    r = await client.post("/api/accounts/remove", json={"id": "work"}, headers=_hdr(app))
    assert r.status == 200 and (await r.json())["active"] == "main"
    assert _accounts.env_overrides() == {}


async def test_accounts_requires_auth(aiohttp_client, app):
    client = await aiohttp_client(app)
    assert (await client.get("/api/accounts")).status in (401, 403)
    assert (await client.post("/api/accounts/active", json={"id": "work"})).status in (401, 403)


# ── per-project pinning over HTTP ───────────────────────────────────────────────────────────

async def test_project_settings_reject_unknown_account(aiohttp_client, tmp_path, acct_env, monkeypatch):
    """Pinning a project to an account that cannot run must fail loudly, not silently."""
    ctx = {
        "topics": {"1:1": {"project": "proj", "cwd": str(tmp_path / "proj"), "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": "testpass",
        "DATA": tmp_path / "data", "HERE": ROOT, "rate_limits": {},
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None,
    }
    (tmp_path / "proj").mkdir()
    ctx["_auth_token"] = _derive_token("testpass")
    application = web.Application(middlewares=[_webapp.auth_middleware])
    application["ctx"] = ctx
    application.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    application.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    client = await aiohttp_client(application)
    hdr = {"Cookie": f"cops_auth={ctx['_auth_token']}"}

    r = await client.post("/api/projects/proj/settings", json={"account": "ghost"}, headers=hdr)
    assert r.status == 400 and "not registered" in (await r.json())["error"]

    # main is always valid; storing it pins the project even if the global switch moves.
    r = await client.post("/api/projects/proj/settings", json={"account": "main"}, headers=hdr)
    assert r.status == 200 and (await r.json())["settings"]["account"] == "main"
    assert ctx["topics"]["1:1"]["account"] == "main"

    # Empty string clears the pin back to "inherit global".
    r = await client.post("/api/projects/proj/settings", json={"account": ""}, headers=hdr)
    assert r.status == 200 and (await r.json())["settings"]["account"] is None
    assert "account" not in ctx["topics"]["1:1"]

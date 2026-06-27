"""
Tests for Web Push API endpoints — spec-053 Phase B.

Covers:
- GET /api/push/vapid-public: returns a non-empty base64url key.
- POST /api/push/subscribe: stores a subscription, deduplicates by endpoint.
- POST /api/push/unsubscribe: removes the subscription.
- Storage helpers (_load_push_subs / _save_push_subs).
- VAPID key generation and persistence (_push_ensure_vapid_keys).

Network send (pywebpush → real push gateway) is NOT tested here —
it requires a real browser subscription and a live push endpoint.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── storage helpers ────────────────────────────────


def test_push_ensure_vapid_keys_generates_and_persists(tmp_path):
    """_push_ensure_vapid_keys writes a keypair to disk and populates globals."""
    _webapp._PUSH_VAPID_FILE = tmp_path / "push-vapid.json"
    _webapp._PUSH_PRIV_KEY = None
    _webapp._PUSH_PUB_KEY = None

    _webapp._push_ensure_vapid_keys()

    assert _webapp._PUSH_PRIV_KEY, "Private key global should be set"
    assert _webapp._PUSH_PUB_KEY, "Public key global should be set"
    # P-256 uncompressed public key = 65 raw bytes → 87 base64url chars (no padding).
    assert len(_webapp._PUSH_PUB_KEY) == 87, (
        f"Expected 87-char base64url public key, got {len(_webapp._PUSH_PUB_KEY)}"
    )
    # Private key = 32 raw bytes → 43 base64url chars (no padding).
    assert len(_webapp._PUSH_PRIV_KEY) == 43, (
        f"Expected 43-char base64url private key, got {len(_webapp._PUSH_PRIV_KEY)}"
    )
    # File must exist and contain valid JSON.
    data = json.loads((tmp_path / "push-vapid.json").read_text())
    assert data["public_key"] == _webapp._PUSH_PUB_KEY
    assert data["private_key"] == _webapp._PUSH_PRIV_KEY


def test_push_ensure_vapid_keys_reloads_from_disk(tmp_path):
    """Second call to _push_ensure_vapid_keys re-loads existing keys, does not regenerate."""
    _webapp._PUSH_VAPID_FILE = tmp_path / "push-vapid.json"
    _webapp._PUSH_PRIV_KEY = None
    _webapp._PUSH_PUB_KEY = None

    _webapp._push_ensure_vapid_keys()
    first_pub = _webapp._PUSH_PUB_KEY

    # Reset globals but keep the file.
    _webapp._PUSH_PRIV_KEY = None
    _webapp._PUSH_PUB_KEY = None
    _webapp._push_ensure_vapid_keys()

    assert _webapp._PUSH_PUB_KEY == first_pub, "Second load should return the same public key"


def test_load_save_push_subs_roundtrip(tmp_path):
    """_save_push_subs persists, _load_push_subs restores."""
    _webapp._PUSH_SUBS_FILE = tmp_path / "push-subscriptions.json"
    subs = [{"endpoint": "https://fcm.example.com/1", "keys": {"auth": "a", "p256dh": "b"}}]
    _webapp._save_push_subs(subs)
    loaded = _webapp._load_push_subs()
    assert loaded == subs


def test_load_push_subs_empty_when_missing(tmp_path):
    """_load_push_subs returns [] when no file exists."""
    _webapp._PUSH_SUBS_FILE = tmp_path / "nonexistent.json"
    assert _webapp._load_push_subs() == []


# ─────────────────────────── HTTP endpoints ─────────────────────────────────


@pytest.fixture
def push_ctx(tmp_path):
    """Minimal ctx + push globals wired to tmp_path."""
    password = "testpass"
    data = tmp_path / "data"
    data.mkdir()
    ctx = {
        "topics": {},
        "sessions": {}, "running": {}, "password": password,
        "DATA": data, "HERE": ROOT, "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    # Wire push module globals to this tmp dir.
    _webapp._PUSH_VAPID_FILE = data / "push-vapid.json"
    _webapp._PUSH_SUBS_FILE = data / "push-subscriptions.json"
    _webapp._PUSH_PRIV_KEY = None
    _webapp._PUSH_PUB_KEY = None
    _webapp._PUSH_LOCK = asyncio.Lock()
    yield ctx
    # Reset globals so other tests are not affected.
    _webapp._PUSH_VAPID_FILE = None
    _webapp._PUSH_SUBS_FILE = None
    _webapp._PUSH_PRIV_KEY = None
    _webapp._PUSH_PUB_KEY = None
    _webapp._PUSH_LOCK = None


@pytest.fixture
def push_app(push_ctx):
    from aiohttp import web

    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = push_ctx
    a.router.add_post("/api/login", _webapp.api_login)
    a.router.add_get("/api/push/vapid-public", _webapp.api_push_vapid_public)
    a.router.add_post("/api/push/subscribe", _webapp.api_push_subscribe)
    a.router.add_post("/api/push/unsubscribe", _webapp.api_push_unsubscribe)
    return a


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_vapid_public_returns_key(aiohttp_client, push_app, push_ctx):
    """GET /api/push/vapid-public returns a non-empty base64url key."""
    client = await aiohttp_client(push_app)
    r = await client.get("/api/push/vapid-public", headers=_auth(push_ctx))
    assert r.status == 200
    data = await r.json()
    assert "key" in data
    assert len(data["key"]) == 87, f"Expected 87-char key, got {len(data['key'])}"


async def test_subscribe_stores_subscription(aiohttp_client, push_app, push_ctx):
    """POST /api/push/subscribe stores the subscription and returns ok."""
    client = await aiohttp_client(push_app)
    sub = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/test-token",
        "keys": {"auth": "authkey", "p256dh": "p256key"},
    }
    r = await client.post("/api/push/subscribe", json=sub, headers=_auth(push_ctx))
    assert r.status == 200
    body = await r.json()
    assert body.get("ok") is True

    stored = _webapp._load_push_subs()
    assert len(stored) == 1
    assert stored[0]["endpoint"] == sub["endpoint"]


async def test_subscribe_deduplicates_by_endpoint(aiohttp_client, push_app, push_ctx):
    """Posting the same endpoint twice results in only one stored entry (dedupe)."""
    client = await aiohttp_client(push_app)
    sub1 = {"endpoint": "https://fcm.example.com/same", "keys": {"auth": "a1", "p256dh": "b1"}}
    sub2 = {"endpoint": "https://fcm.example.com/same", "keys": {"auth": "a2", "p256dh": "b2"}}

    await client.post("/api/push/subscribe", json=sub1, headers=_auth(push_ctx))
    await client.post("/api/push/subscribe", json=sub2, headers=_auth(push_ctx))

    stored = _webapp._load_push_subs()
    assert len(stored) == 1, f"Expected 1 entry after dedupe, got {len(stored)}"
    # Latest version should win.
    assert stored[0]["keys"]["auth"] == "a2"


async def test_subscribe_missing_endpoint_400(aiohttp_client, push_app, push_ctx):
    """POST /api/push/subscribe without endpoint returns 400."""
    client = await aiohttp_client(push_app)
    r = await client.post("/api/push/subscribe", json={"keys": {}}, headers=_auth(push_ctx))
    assert r.status == 400


async def test_unsubscribe_removes_entry(aiohttp_client, push_app, push_ctx):
    """POST /api/push/unsubscribe removes the matching endpoint."""
    client = await aiohttp_client(push_app)
    endpoint = "https://fcm.example.com/remove-me"
    sub = {"endpoint": endpoint, "keys": {"auth": "a", "p256dh": "b"}}

    await client.post("/api/push/subscribe", json=sub, headers=_auth(push_ctx))
    assert len(_webapp._load_push_subs()) == 1

    r = await client.post("/api/push/unsubscribe", json={"endpoint": endpoint}, headers=_auth(push_ctx))
    assert r.status == 200
    assert (await r.json()).get("ok") is True
    assert _webapp._load_push_subs() == []


async def test_unsubscribe_noop_when_not_present(aiohttp_client, push_app, push_ctx):
    """POST /api/push/unsubscribe with unknown endpoint returns ok (idempotent)."""
    client = await aiohttp_client(push_app)
    r = await client.post(
        "/api/push/unsubscribe",
        json={"endpoint": "https://fcm.example.com/unknown"},
        headers=_auth(push_ctx),
    )
    assert r.status == 200
    assert (await r.json()).get("ok") is True

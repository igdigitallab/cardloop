"""
spec-093: "Follow default" — the one action that hands a pinned chat back to the
project/global default.

Before it, every picker row sent an explicit account id (a pick is a pin), so a chat picked
once could never be un-picked from the UI: a chat pinned to Main kept spending Main after the
operator switched the default to work. The frontend's Follow-default action sends
{account: null, backend: ""} — and `provider` (+ a model) ONLY when it actually changes, i.e.
when leaving Codex. These tests pin the server half of that contract end to end, through the
real PATCH handler, on the chat shape the frontend meets most: one with NO model pin (every
project's seeded "Main" chat is created with model=None).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import webapp as _webapp

from test_spec092_runtime_wiring import (  # noqa: F401 — fixtures are used by name
    _auth, _enable_codex, chats_app, fake_ctx, reset_chat_queue, reset_monitors_and_bg,
)


def _two_accounts(monkeypatch):
    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [
        {"id": "main", "label": "Main", "ok": True, "active": True},
        {"id": "work", "label": "work", "ok": True, "active": False},
    ])
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))


async def _patch(client, fake_ctx, body):
    return await client.patch("/api/projects/myproject/chats/aaaaaa", json=body,
                              headers=_auth(fake_ctx))


@pytest.mark.asyncio
async def test_follow_default_drops_the_account_pin_and_the_turn_inherits(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    _two_accounts(monkeypatch)
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "s1"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet", "account": "main", "runtime_revision": 3}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await _patch(client, fake_ctx, {"account": None, "backend": "", "expected_revision": 3})
    assert resp.status == 200, await resp.text()
    chat = (await resp.json())["chat"]
    assert not chat.get("account"), "the pin must be gone, not rewritten to the default's id"
    assert chat["runtime_revision"] == 4

    stored = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert not stored.get("account")

    # The next turn carries no chat-level account: the engine gets the project's (none here),
    # which accounts.resolve() turns into the globally active one at run time.
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hi", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and not calls[0]["project_account"]


@pytest.mark.asyncio
async def test_follow_default_from_codex_crosses_back_to_claude(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    _two_accounts(monkeypatch)
    fake_ctx["codex_provider_info"] = _enable_codex(monkeypatch)
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "codex",
                                 "model": "gpt-5.6-sol", "runtime_revision": 1}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await _patch(client, fake_ctx, {"provider": "claude", "account": None, "backend": "",
                                           "model": "sonnet", "expected_revision": 1})
    assert resp.status == 200, await resp.text()
    chat = (await resp.json())["chat"]
    assert chat["provider"] == "claude"
    assert not chat.get("account")
    assert chat["model"] == "sonnet"


@pytest.mark.asyncio
async def test_follow_default_on_a_chat_with_no_model_pin(aiohttp_client, fake_ctx, chats_app, monkeypatch):
    """The review blocker: re-sending the unchanged provider on a model-less chat is refused
    ("leaves no model"). The frontend now omits it; this is the shape it sends."""
    _two_accounts(monkeypatch)
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": None, "account": "work", "runtime_revision": 2}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await _patch(client, fake_ctx, {"account": None, "backend": "", "expected_revision": 2})
    assert resp.status == 200, await resp.text()
    chat = (await resp.json())["chat"]
    assert not chat.get("account")
    assert chat.get("model") is None, "unpinning the account must not freeze a model onto the chat"


@pytest.mark.asyncio
async def test_account_pick_on_a_chat_with_no_model_pin(aiohttp_client, fake_ctx, chats_app, monkeypatch):
    """Same root for a plain row pick (Main -> work) — it was broken since spec-092."""
    _two_accounts(monkeypatch)
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": None, "runtime_revision": 0}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await _patch(client, fake_ctx, {"account": "work", "backend": "", "expected_revision": 0})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["chat"]["account"] == "work"


@pytest.mark.asyncio
async def test_resending_an_unchanged_provider_on_a_model_less_chat_is_refused(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    """Why the frontend must NOT send `provider` unless it changes. If the server ever relaxes
    this, the frontend guard becomes redundant — not wrong — and this test says so."""
    _two_accounts(monkeypatch)
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": None, "account": "work", "runtime_revision": 0}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await _patch(client, fake_ctx, {"provider": "claude", "account": None, "backend": "",
                                           "expected_revision": 0})
    assert resp.status == 400
    assert "no model" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_follow_default_on_an_unpinned_chat_is_a_no_op(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    """Nothing to drop: the revision must not move, or a second tab's pending pick would be
    refused as stale for a change that changed nothing."""
    _two_accounts(monkeypatch)
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet", "backend": "", "runtime_revision": 5}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await _patch(client, fake_ctx, {"account": None, "backend": "", "expected_revision": 5})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["chat"]["runtime_revision"] == 5

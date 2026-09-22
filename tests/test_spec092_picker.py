"""
spec-092 P1 (frontend-facing half): the account dimension, the runtime picker's server
contract, and the second ask-mode door on the queue.

Covers:
  1. GET /api/agent-providers advertises the ACCOUNT dimension (the picker's third axis).
  2. `account` is a per-CHAT override on the run path, not only a project setting — and an
     explicit chat pin that cannot run errors instead of quietly spending `main`.
  3. A queued message pins the account it was accepted against (key presence = pinned).
  4. The drain checks the per-turn flags against the runtime that will ACTUALLY answer
     (spec-092 P1c: _last_turn_options is session-wide, so a Claude turn's ask_mode could
     ride into a Codex drain where run_codex_engine swallows it in **_ignored).
  5. runtime.validate_runtime_change treats a null account as INHERIT, not as an unknown id.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import runtime as rt
import webapp as _webapp

from test_spec092_runtime_wiring import (  # noqa: F401 — fixtures are used by name
    _auth, _enable_codex, chats_app, fake_ctx, reset_chat_queue, reset_monitors_and_bg,
)


# ───────────────────────── 5: null account means inherit ──────────────────────


def test_null_account_is_inherit_not_an_unknown_id():
    """The picker's "Claude · Main (default)" row hands a chat BACK to the project/global
    choice. Before this, `str(None)` was compared against the account id set and every
    inherit pick was rejected as `unknown account: None`."""
    providers = {"claude": rt.ProviderInfo(provider="claude", available=True, models=("sonnet",))}
    accounts = [{"id": "main"}, {"id": "work"}]
    ok, reason = rt.validate_runtime_change(
        {"account": None}, providers=providers, accounts_list=accounts,
        current={"provider": "claude", "model": "sonnet"},
    )
    assert ok, reason
    ok, reason = rt.validate_runtime_change(
        {"account": ""}, providers=providers, accounts_list=accounts,
        current={"provider": "claude", "model": "sonnet"},
    )
    assert ok, reason
    # A genuinely unknown id is still rejected — the inherit branch must not swallow typos.
    ok, reason = rt.validate_runtime_change(
        {"account": "nope"}, providers=providers, accounts_list=accounts,
        current={"provider": "claude", "model": "sonnet"},
    )
    assert not ok and "unknown account" in reason


# ───────────────────────── 1: accounts in the registry ────────────────────────


@pytest.mark.asyncio
async def test_agent_providers_advertises_the_account_dimension(aiohttp_client, fake_ctx, monkeypatch):
    """The picker is provider x account x model in ONE menu, so the payload that feeds it
    must carry all three. An account whose credentials are gone is listed with
    available=False — hidden would be indistinguishable from never-configured."""
    from aiohttp import web

    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [
        {"id": "main", "label": "Main", "ok": True, "active": True, "email": "a@b.c", "plan": "max"},
        {"id": "work", "label": "Work", "ok": False, "active": False, "reason": "needs login"},
    ])
    fake_ctx["codex_provider_info"] = _enable_codex(monkeypatch)
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/agent-providers", _webapp.api_agent_providers)
    client = await aiohttp_client(app)
    resp = await client.get("/api/agent-providers", headers=_auth(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    claude = next(p for p in body["providers"] if p["provider"] == "claude")
    ids = [a["id"] for a in claude["accounts"]]
    assert ids == ["main", "work"]
    assert claude["accounts"][0]["available"] is True
    assert claude["accounts"][1]["available"] is False
    assert claude["accounts"][1]["reason"] == "needs login"
    codex = next(p for p in body["providers"] if p["provider"] == "codex")
    assert codex["accounts"] == [], "a provider with no account dimension reports [], not a missing key"


# ───────────────────────── 2: chat-level account on the run path ──────────────


def test_chat_account_overrides_the_project_account(monkeypatch):
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))
    acct, err = _webapp._resolve_run_account({"account": "work"}, {"account": "main"})
    assert (acct, err) == ("work", "")


def test_chat_without_an_account_inherits_the_project(monkeypatch):
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))
    assert _webapp._resolve_run_account({"id": "c1"}, {"account": "work"}) == ("work", "")
    assert _webapp._resolve_run_account(None, {}) == (None, "")


def test_unusable_chat_account_is_an_error_not_a_silent_fallback(monkeypatch):
    """accounts.resolve() degrades to `main` — right for an inherited default, wrong as the
    answer to "run THIS chat on work": it spends a different subscription than the picker
    shows, invisibly. An explicit chat pin must report instead."""
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (False, "credentials expired"))
    acct, err = _webapp._resolve_run_account({"account": "work"}, {"account": "main"})
    assert acct is None
    assert "work" in err and "credentials expired" in err


def test_unusable_project_account_keeps_the_legacy_silent_fallback(monkeypatch):
    """Deliberate asymmetry: the project default is inherited, not chosen per turn, so the
    pre-spec-092 degrade stays. Turning it into a hard error would brick every chat of a
    project the moment a subscription's token expires."""
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (False, "credentials expired"))
    assert _webapp._resolve_run_account({"id": "c1"}, {"account": "work"}) == ("work", "")


@pytest.mark.asyncio
async def test_chat_account_reaches_the_engine(aiohttp_client, fake_ctx, chats_app, monkeypatch):
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "hi"}
        yield {"type": "result", "session_id": "s1"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "account": "work"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hi", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and calls[0]["project_account"] == "work"


@pytest.mark.asyncio
async def test_chat_pinned_to_a_dead_account_refuses_the_turn(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (False, "needs login"))

    async def never(**kwargs):
        raise AssertionError("the engine must not run for an unusable explicit account")
        yield  # pragma: no cover

    fake_ctx["run_engine"] = never
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "account": "gone"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hi", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 409
    assert "needs login" in (await resp.json())["error"]


# ───────────────────────── 3: the account rides on the queue ──────────────────


@pytest.mark.asyncio
async def test_queue_add_pins_the_account_it_was_accepted_against(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet", "account": "work"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat/queue",
                             json={"text": "later", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 201
    item = (await resp.json())["item"]
    assert item["runtime"]["account"] == "work"


@pytest.mark.asyncio
async def test_queue_add_on_an_inherited_account_stores_no_account_key(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    """Key PRESENCE is the pin. An item accepted on the project default must keep inheriting
    so that changing the project default still reaches it; freezing today's value would make
    a settings change silently skip everything already queued."""
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat/queue",
                             json={"text": "later", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    item = (await resp.json())["item"]
    assert "account" not in item["runtime"]


@pytest.mark.asyncio
async def test_queue_add_carries_its_own_ask_mode(aiohttp_client, fake_ctx, chats_app):
    """spec-092 P1c, first door: without this the item had no ask_mode of its own and the
    drain fell back to session-wide _last_turn_options — which another chat's turn writes."""
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat/queue",
                             json={"text": "later", "chat_id": "aaaaaa", "ask_mode": True},
                             headers=_auth(fake_ctx))
    assert (await resp.json())["item"]["ask_mode"] is True


# ───────────────────────── 4: the drain's capability gate ─────────────────────


@pytest.mark.asyncio
async def test_drain_clears_an_ask_mode_the_pinned_runtime_cannot_honour(
    fake_ctx, monkeypatch, capsys
):
    """spec-092 P1c, second door: a Codex-pinned item must never reach run_codex_engine with
    ask_mode set. It has no ask_mode parameter at all (**_ignored), so the approval gate the
    operator switched on would simply not exist while the UI claims it does.

    Cleared, not refused: the message was already accepted, and dropping it to close a gap
    that clearing already closes would lose work."""
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)
    monkeypatch.setattr(_webapp._accounts, "resolve", lambda a=None: "main")
    codex_calls: list = []

    async def fake_codex_engine(**kwargs):
        codex_calls.append(kwargs)
        yield {"type": "text", "text": "ran"}

    fake_ctx["run_codex_engine"] = fake_codex_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "codex",
                                 "model": "gpt-5.6-sol"}]},
    })
    # The poisoned inheritance: an unrelated Claude turn on the SAME session_key left
    # ask_mode ON in the session-wide options dict.
    _webapp._last_turn_options["1001:42"] = {"ask_mode": True, "effort": None, "ultracode": False}
    try:
        item = _webapp._chat_queue_enqueue(
            "1001:42", "queued while busy", "aaaaaa", "myproject",
            pinned_runtime={"provider": "codex", "model": "gpt-5.6-sol"},
        )
        assert item is not None
        await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)
    finally:
        _webapp._last_turn_options.pop("1001:42", None)
    assert codex_calls, "the message must still run — clearing the flag, not dropping it"
    assert not codex_calls[0].get("ask_mode")
    assert "cannot honour ask_mode" in capsys.readouterr().out


# ───────────── review findings: the drain must never substitute in silence ────


@pytest.mark.asyncio
async def test_drain_traces_a_pinned_account_that_went_bad(fake_ctx, monkeypatch, capsys):
    """Review finding 2: the account was validated at ACCEPT time, but by drain time the
    operator can have logged it out. engine.py then calls _accounts.resolve(), whose
    documented behaviour is a SILENT fallback to the global account — so a message pinned to
    `work` quietly spends something else with nothing to diagnose it.

    The message must still run (it was already accepted), but the substitution must be
    logged AND traced, like the capability downgrade it sits next to."""
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (False, "needs login"))
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "ran"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet"}]},
    })
    item = _webapp._chat_queue_enqueue(
        "1001:42", "queued", "aaaaaa", "myproject",
        pinned_runtime={"provider": "claude", "model": "sonnet", "account": "work"},
    )
    assert item is not None
    await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)
    assert calls, "an accepted message must still run"
    assert calls[0]["project_account"] is None, (
        "a dead pin must fall back explicitly, not be handed to the engine as if it were live"
    )
    out = capsys.readouterr().out
    assert "cannot run" in out and "work" in out


@pytest.mark.asyncio
async def test_drain_keeps_a_live_pinned_account(fake_ctx, monkeypatch):
    """Regression guard for the check above: a VALID pin must reach the engine untouched."""
    monkeypatch.setattr(_webapp._accounts, "validate", lambda aid: (True, ""))
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "ran"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet"}]},
    })
    item = _webapp._chat_queue_enqueue(
        "1001:42", "queued", "aaaaaa", "myproject",
        pinned_runtime={"provider": "claude", "model": "sonnet", "account": "work"},
    )
    await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)
    assert calls and calls[0]["project_account"] == "work"

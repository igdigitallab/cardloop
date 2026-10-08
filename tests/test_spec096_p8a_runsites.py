"""
spec-096 P8a (review-spec095-seam / -readers): run-site behaviour of the provider seam.

  item 11 - the Grok send ledger is blocking file IO and must not run on the event loop;
  item 7  - the queue drain fails closed on a pin / provider it cannot honour;
  item 12 - a provider gate is enforced at all three run sites (card, queue drain, direct POST);
  item 13 - handoff staleness after a pinned old-provider drain, the disabled-provider active chat,
            and the Settings board-model rows.
"""
import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import grok_sends
import providers
import webapp as _webapp

from test_grok_wiring import (  # noqa: F401 - fixtures used by name
    CHAT_ID, PROJECT_ID, SESSION_KEY, _auth, _chat_record, _drain, _every_grok_session_exists,
    _run_card_with, _seed_chat, _sse_events, codex_on, engines, fake_ctx, grok_on, isolate,
)


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    ap = web.Application(middlewares=[_webapp.auth_middleware])
    ap["ctx"] = fake_ctx
    ap.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    return ap


async def _post_chat(client, ctx, prompt="hello"):
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": prompt, "chat_id": CHAT_ID}, headers=_auth(ctx))
        events = await _sse_events(resp)
    return resp, events


# ═════════════════════════ item 11: the ledger is off the event loop ══════════


@pytest.fixture
def ledger_calls(monkeypatch):
    """Every `grok_sends.record` call as (session id, thread id, engines already started)."""
    calls = []

    def record(data_dir, session_id, prompt):
        calls.append({"sid": session_id, "thread": threading.get_ident(), "prompt": prompt})
        return True

    monkeypatch.setattr(grok_sends, "record", record)
    return calls


def _off_loop(calls):
    return [c["thread"] != threading.get_ident() for c in calls]


@pytest.mark.asyncio
async def test_the_queue_drain_records_the_ledger_off_the_event_loop_and_before_the_run(
    fake_ctx, engines, grok_on, ledger_calls
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID")
    seen_at_start = []
    inner = fake_ctx["run_grok_engine"]

    async def engine(**kw):
        seen_at_start.append([c["sid"] for c in ledger_calls])
        async for ev in inner(**kw):
            yield ev

    fake_ctx["run_grok_engine"] = engine
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                pinned_runtime={"provider": "grok", "model": "grok-4.7"}))
    assert [c["sid"] for c in ledger_calls] == ["OLD-ID", "NEW-ID"]
    assert _off_loop(ledger_calls) == [True, True], "blocking file IO ran on the event-loop thread"
    assert seen_at_start == [["OLD-ID"]], "the pre-run record must exist before the engine starts"


@pytest.mark.asyncio
async def test_the_direct_post_records_the_ledger_off_the_event_loop_and_before_the_run(
    aiohttp_client, fake_ctx, app, engines, grok_on, ledger_calls
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID")
    seen_at_start = []
    inner = fake_ctx["run_grok_engine"]

    async def engine(**kw):
        seen_at_start.append([c["sid"] for c in ledger_calls])
        async for ev in inner(**kw):
            yield ev

    fake_ctx["run_grok_engine"] = engine
    client = await aiohttp_client(app)
    resp, _ = await _post_chat(client, fake_ctx)
    assert resp.status == 200
    assert [c["sid"] for c in ledger_calls] == ["OLD-ID", "NEW-ID"]
    assert _off_loop(ledger_calls) == [True, True], "blocking file IO ran on the event-loop thread"
    assert seen_at_start == [["OLD-ID"]]


@pytest.mark.asyncio
async def test_a_board_card_records_the_ledger_off_the_event_loop(
    fake_ctx, tmp_path, engines, grok_on, ledger_calls
):
    await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "grok"})
    assert [c["sid"] for c in ledger_calls] == ["NEW-ID"]
    assert _off_loop(ledger_calls) == [True]


# ═════════════════════════ item 7: the queue drain fails closed ═══════════════


def _live_events():
    return list((_webapp._live_turns.get(SESSION_KEY) or {}).get("events") or [])


def _error_texts():
    return [str(e.get("error") or "") for e in _live_events() if e.get("type") == "error"]


def _nothing_ran(engines):
    return {k: len(v) for k, v in engines.items()} == {"claude": 0, "codex": 0, "grok": 0}


async def _drain_item(fake_ctx, **item_kwargs):
    """Drain one item and report whether the drain dispatched it (it always does: the refusal is
    the TURN's own failure, not a silent skip)."""
    item = _webapp._chat_queue_enqueue(SESSION_KEY, "queued text", **item_kwargs)
    assert item is not None
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is True
        await asyncio.sleep(0.05)


def _replace_chats_with_another_claude_chat(ctx):
    _webapp._save_chats(ctx, {PROJECT_ID: {"active": "bbbbbb", "chats": [
        {"id": "bbbbbb", "name": "Other", "provider": "claude", "model": "opus",
         "session_id": "CLAUDE-OWN"}]}})


@pytest.mark.asyncio
async def test_a_pinned_grok_item_whose_chat_was_deleted_runs_nowhere_and_says_so(
    fake_ctx, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-OLD")
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert pinned["provider"] == "grok"
    _replace_chats_with_another_claude_chat(fake_ctx)     # the chat is deleted before the drain
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned)

    assert _nothing_ran(engines), "the Grok-pinned message ran on another engine"
    assert fake_ctx["sessions"][SESSION_KEY] == "CLAUDE-FLAT-SESSION"
    errors = _error_texts()
    assert errors and "grok" in errors[0].lower(), errors
    assert any(e.get("kind") == "run_end" and e.get("outcome") == "fail" for e in _live_events())
    assert not fake_ctx["running"].get(SESSION_KEY), "the slot is released"


@pytest.mark.asyncio
async def test_a_pinned_grok_item_when_the_chats_file_cannot_be_read_runs_nowhere(
    fake_ctx, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-OLD")
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)

    def boom(*_a, **_k):
        raise OSError("chats.json unreadable")

    with patch.object(_webapp, "_ensure_chat_entry", side_effect=boom):
        await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned)
    assert _nothing_ran(engines)
    assert fake_ctx["sessions"][SESSION_KEY] == "CLAUDE-FLAT-SESSION"
    assert any("grok" in t.lower() for t in _error_texts()), _error_texts()


@pytest.mark.asyncio
async def test_a_pinned_claude_item_with_an_unresolved_chat_keeps_its_legacy_flat_map_run(
    fake_ctx, engines
):
    """Control: only a non-Claude pin is refused when the chat does not resolve; Claude's own
    flat-map fallback (what the legacy queue always did) still runs."""
    _seed_chat(fake_ctx, provider="claude", session_id="CLAUDE-OWN")
    _replace_chats_with_another_claude_chat(fake_ctx)
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID,
                      pinned_runtime={"provider": "claude", "model": "opus"})
    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [1, 0, 0]
    assert engines["claude"][0]["resume_session_id"] == "CLAUDE-FLAT-SESSION"
    assert not _error_texts()


@pytest.mark.asyncio
async def test_a_legacy_item_on_a_chat_naming_an_unregistered_provider_is_an_error_not_claude(
    fake_ctx, engines
):
    _seed_chat(fake_ctx, provider="vertex", model="m", session_id="CLAUDE-OWN")
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID)    # unpinned, as wakes are
    assert _nothing_ran(engines), "an unknown provider name silently became Claude"
    errors = _error_texts()
    assert errors and "vertex" in errors[0] and "not a registered provider" in errors[0], errors


@pytest.mark.asyncio
async def test_a_legacy_item_on_a_chat_whose_provider_is_switched_off_is_an_error_not_claude(
    fake_ctx, engines
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-OLD")      # grok_on NOT requested
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID)
    assert _nothing_ran(engines)
    errors = _error_texts()
    assert errors and "grok" in errors[0] and "unavailable" in errors[0], errors


@pytest.mark.asyncio
async def test_a_pin_to_an_unregistered_provider_is_a_visible_error_too(fake_ctx, engines):
    _seed_chat(fake_ctx, provider="claude", session_id="CLAUDE-OWN")
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID,
                      pinned_runtime={"provider": "vertex", "model": "m"})
    assert _nothing_ran(engines)
    errors = _error_texts()
    assert errors and "vertex" in errors[0], errors

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

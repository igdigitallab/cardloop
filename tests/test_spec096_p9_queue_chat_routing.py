"""spec-096 P9 item F: a queued item with no chat id never runs against a chat of another provider.

An item enqueued without a chat id is routed to the visible (active) chat. If it is pinned to a
provider and that chat runs another one, the drain used to run the pinned engine anyway and
write its continuity id (a Grok session id) onto a chat of the wrong provider - and, for a Claude
chat, run Grok code over a conversation the operator was looking at. It is refused, visibly, now.
An item WITH a chat id keeps its designed behaviour: it runs on the provider it was accepted
against even if that chat has been switched since.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from test_grok_wiring import (  # noqa: F401 - fixtures used by name
    CHAT_ID, PROJECT_ID, _chat_record, _drain, _every_grok_session_exists, _live_events, _no_engine_calls,
    _seed_chat,
    engines, fake_ctx, grok_on, isolate,
)

GROK_PIN = {"provider": "grok", "model": "grok-4.7-build-fast"}


@pytest.mark.asyncio
async def test_a_grok_item_without_a_chat_id_is_refused_when_the_visible_chat_is_claude(
    fake_ctx, engines, grok_on
):
    _seed_chat(fake_ctx, provider="claude", model="opus", session_id="CLAUDE-OWN")
    await _drain(fake_ctx, dict(chat_id=None, project_id=PROJECT_ID, pinned_runtime=GROK_PIN))
    assert _no_engine_calls(engines), "no engine may run a Grok item against a Claude chat"
    errors = [e for e in _live_events() if e.get("type") == "error"]
    assert errors, "the refusal must be visible on the turn"
    assert "no chat of its own" in errors[0]["error"] and "grok" in errors[0]["error"]
    rec = _chat_record(fake_ctx)
    assert not rec.get("grok_session_id"), "a Grok session id was written onto a Claude chat"
    assert rec["session_id"] == "CLAUDE-OWN"


@pytest.mark.asyncio
async def test_a_claude_item_without_a_chat_id_is_refused_when_the_visible_chat_is_grok(
    fake_ctx, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="G-OWN")
    await _drain(fake_ctx, dict(chat_id=None, project_id=PROJECT_ID,
                                pinned_runtime={"provider": "claude", "model": "sonnet"}))
    assert _no_engine_calls(engines)
    assert [e for e in _live_events() if e.get("type") == "error"]
    assert _chat_record(fake_ctx)["grok_session_id"] == "G-OWN"


@pytest.mark.asyncio
async def test_a_pinned_item_without_a_chat_id_runs_when_the_visible_chat_has_that_provider(
    fake_ctx, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="G-OWN")
    await _drain(fake_ctx, dict(chat_id=None, project_id=PROJECT_ID, pinned_runtime=GROK_PIN))
    assert len(engines["grok"]) == 1 and not engines["claude"]
    assert _chat_record(fake_ctx)["grok_session_id"] == "NEW-ID"


@pytest.mark.asyncio
async def test_an_unpinned_item_without_a_chat_id_still_follows_the_visible_chat(
    fake_ctx, engines, grok_on
):
    """Completion wakes, the Stop-agents turn and plan follow-ups are enqueued unpinned on purpose."""
    _seed_chat(fake_ctx, provider="claude", model="opus", session_id="CLAUDE-OWN")
    await _drain(fake_ctx, dict(chat_id=None, project_id=PROJECT_ID))
    assert len(engines["claude"]) == 1 and not engines["grok"]
    assert [e for e in _live_events() if e.get("type") == "error"] == []


@pytest.mark.asyncio
async def test_an_item_with_a_chat_id_keeps_running_on_the_provider_it_was_accepted_on(
    fake_ctx, engines, grok_on
):
    """The designed case: the chat was switched while the message waited - the pin wins."""
    _seed_chat(fake_ctx, provider="claude", model="opus", session_id="CLAUDE-OWN",
               grok_session_id="GROK-THREAD")
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=GROK_PIN))
    assert len(engines["grok"]) == 1 and not engines["claude"]
    assert engines["grok"][0]["resume_session_id"] == "GROK-THREAD"

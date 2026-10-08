"""spec-096 P9 item E: a turn clears only the handoff block IT delivered.

At the end of a turn the code deleted whatever `runtime_handoff` the chat held at that moment.
The operator can switch runtime while a turn runs (arming a block for the OTHER engine); that
block was never delivered, yet the turn's write-back popped it. Now the stored block must be the
very one the turn put in front of its prompt (same `created_at` and text) to be cleared.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import webapp as _webapp

from test_spec092_handoff import _app  # noqa: F401
from test_spec092_runtime_wiring import (  # noqa: F401 - fixtures used by name
    _auth, chats_app, fake_ctx, reset_chat_queue, reset_monitors_and_bg,
)

BLOCK_A = {"text": "# Handoff: X → Claude\n\nblock A", "created_at": 1000.0, "for_provider": "claude",
           "for_backend": "", "from_label": "X", "to_label": "Claude"}
BLOCK_B = {"text": "# Handoff: Claude → Codex\n\nblock B", "created_at": 2000.0, "for_provider": "codex",
           "for_backend": "", "from_label": "Claude", "to_label": "Codex"}


def _seed(fake_ctx, block):
    chat = {"id": "aaaaaa", "name": "Main", "provider": "claude"}
    if block is not None:
        chat["runtime_handoff"] = dict(block)
    _webapp._save_chats(fake_ctx, {"myproject": {"active": "aaaaaa", "chats": [chat]}})


def _stored(fake_ctx):
    return _webapp._load_chats(fake_ctx)["myproject"]["chats"][0].get("runtime_handoff")


def _engine_that_arms(fake_ctx, calls, block):
    """An engine during whose turn the operator arms `block` on the chat."""
    async def engine(**kwargs):
        calls.append(kwargs)
        data = _webapp._load_chats(fake_ctx)
        data["myproject"]["chats"][0]["runtime_handoff"] = dict(block)
        _webapp._save_chats(fake_ctx, data)
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "s-new"}
    return engine


async def _direct(aiohttp_client, fake_ctx):
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "continue", "chat_id": "aaaaaa"}, headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()


async def _queued(fake_ctx):
    item = _webapp._chat_queue_enqueue("1001:42", "continue", "aaaaaa", "myproject",
                                       pinned_runtime={"provider": "claude", "model": "sonnet"})
    _webapp._chat_queue_pop_ready("1001:42")
    await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)


# ─────────────────────────── direct path ───────────────────────────


@pytest.mark.asyncio
async def test_direct_turn_does_not_clear_a_block_armed_for_another_engine_during_it(aiohttp_client, fake_ctx):
    calls: list = []
    _seed(fake_ctx, BLOCK_A)
    fake_ctx["run_engine"] = _engine_that_arms(fake_ctx, calls, BLOCK_B)
    await _direct(aiohttp_client, fake_ctx)
    assert calls[0]["prompt"].startswith("# Handoff: X"), "block A rode this turn"
    assert _stored(fake_ctx) == BLOCK_B, "block B was never delivered and must survive"


@pytest.mark.asyncio
async def test_direct_turn_that_carried_no_block_clears_nothing(aiohttp_client, fake_ctx):
    calls: list = []
    _seed(fake_ctx, None)
    fake_ctx["run_engine"] = _engine_that_arms(fake_ctx, calls, BLOCK_B)
    await _direct(aiohttp_client, fake_ctx)
    assert "block B" not in calls[0]["prompt"]
    assert _stored(fake_ctx) == BLOCK_B


@pytest.mark.asyncio
async def test_direct_turn_still_clears_the_block_it_delivered(aiohttp_client, fake_ctx):
    calls: list = []

    async def engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s-new"}

    _seed(fake_ctx, BLOCK_A)
    fake_ctx["run_engine"] = engine
    await _direct(aiohttp_client, fake_ctx)
    assert calls[0]["prompt"].startswith("# Handoff: X")
    assert _stored(fake_ctx) is None


# ─────────────────────────── queued path ───────────────────────────


@pytest.mark.asyncio
async def test_queued_turn_does_not_clear_a_block_armed_for_another_engine_during_it(fake_ctx):
    calls: list = []
    _seed(fake_ctx, BLOCK_A)
    fake_ctx["run_engine"] = _engine_that_arms(fake_ctx, calls, BLOCK_B)
    await _queued(fake_ctx)
    assert calls[0]["prompt"].startswith("# Handoff: X")
    assert _stored(fake_ctx) == BLOCK_B


@pytest.mark.asyncio
async def test_queued_turn_still_clears_the_block_it_delivered(fake_ctx):
    calls: list = []

    async def engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s-q"}

    _seed(fake_ctx, BLOCK_A)
    fake_ctx["run_engine"] = engine
    await _queued(fake_ctx)
    assert calls[0]["prompt"].startswith("# Handoff: X")
    assert _stored(fake_ctx) is None


# ─────────────────────────── the identity rule itself ───────────────────────────


def test_a_block_is_the_delivered_one_only_when_stamp_and_text_match():
    pop = _webapp._pop_delivered_handoff
    chat = {"runtime_handoff": dict(BLOCK_A)}
    assert pop(chat, dict(BLOCK_B)) is False and chat["runtime_handoff"] == BLOCK_A
    assert pop(chat, {**BLOCK_A, "created_at": 1.0}) is False and "runtime_handoff" in chat   # re-armed, same text
    assert pop(chat, {**BLOCK_A, "text": "edited"}) is False and "runtime_handoff" in chat
    assert pop(chat, None) is False and "runtime_handoff" in chat
    assert pop(chat, dict(BLOCK_A)) is True and "runtime_handoff" not in chat
    assert pop(chat, dict(BLOCK_A)) is False                                                    # nothing left
    legacy = {"runtime_handoff": {"text": "# Handoff\nold"}}                                   # no created_at at all
    assert pop(legacy, {"text": "# Handoff\nold"}) is True

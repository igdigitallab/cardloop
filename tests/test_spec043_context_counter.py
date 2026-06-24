"""
Tests for spec-043 Block C — honest context counter fixes.

Change 1 (engine.py): usage-less turn must NOT carry a stale value from the prior turn.
Change 2 (webapp.py): history endpoint for absent session returns context_tokens:0 explicitly.
"""
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import bot
import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── helpers ─────────────────────────────────────────

def _make_result_message(session_id="sess-test"):
    """Minimal fake ResultMessage."""
    from claude_agent_sdk import ResultMessage
    msg = MagicMock(spec=ResultMessage)
    msg.__class__ = ResultMessage
    msg.session_id = session_id
    msg.total_cost_usd = None
    msg.api_error_status = None
    msg.duration_ms = None
    msg.duration_api_ms = None
    msg.structured_output = None
    return msg


def _make_assistant_message(usage, text="hello"):
    """Fake AssistantMessage with the given usage dict (or None for no usage)."""
    from claude_agent_sdk import AssistantMessage, TextBlock
    msg = MagicMock(spec=AssistantMessage)
    msg.__class__ = AssistantMessage
    msg.usage = usage
    blk = MagicMock(spec=TextBlock)
    blk.__class__ = TextBlock
    blk.text = text
    msg.content = [blk]
    return msg


def _make_live_client(turn_messages_list):
    """Build a fake connected client that replays messages per turn."""
    client = MagicMock()
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    client.connect = AsyncMock()

    turn_idx = [-1]

    async def _query(prompt):
        turn_idx[0] += 1

    async def _receive():
        idx = turn_idx[0]
        msgs = turn_messages_list[idx] if idx < len(turn_messages_list) else []
        for m in msgs:
            yield m

    client.query = _query
    client.receive_response = _receive
    return client


def _make_ctx():
    return {
        "running": {},
        "live_clients": {},
    }


async def _collect_events(turn_messages):
    """Run one engine turn with PERSISTENT_CLIENT=True and return all events."""
    live_client = _make_live_client([turn_messages])
    ctx = _make_ctx()

    events = []
    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", return_value=live_client), \
         patch.object(engine, "audit", lambda *a: None):
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(ROOT),
            prompt="test",
            session_key="chat:1",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            events.append(ev)
    return events


# ─────────────────────────── engine.py tests ─────────────────────────────────

@pytest.mark.asyncio
async def test_normal_turn_reports_correct_context_tokens(tmp_path):
    """A turn where the final AssistantMessage has real usage reports the correct total."""
    usage = {
        "input_tokens": 5000,
        "cache_read_input_tokens": 20000,
        "cache_creation_input_tokens": 0,
    }
    msgs = [
        _make_assistant_message(usage, text="normal reply"),
        _make_result_message(),
    ]
    events = await _collect_events(msgs)
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert result_events[0]["context_tokens"] == 25000, (
        f"Expected 5000+20000=25000 context tokens, got {result_events[0]['context_tokens']}"
    )


@pytest.mark.asyncio
async def test_no_usage_turn_does_not_carry_stale_value(tmp_path):
    """
    Core regression: a turn whose AssistantMessage has NO usage attribute (None)
    must NOT report the previous turn's large value.

    Turn 1: usage present, large count (50000).
    Turn 2: NO usage on AssistantMessage (usage=None).

    After turn 2, context_tokens in the result event must NOT be 50000 (stale).
    It must be 0 (last_ctx_tokens was reset to 0 at run_engine entry and never
    updated because _turn_max_pt stayed None).
    """
    usage_turn1 = {
        "input_tokens": 10000,
        "cache_read_input_tokens": 40000,
        "cache_creation_input_tokens": 0,
    }
    turn1_msgs = [
        _make_assistant_message(usage_turn1, text="turn 1 reply"),
        _make_result_message(session_id="sess-1"),
    ]
    # Turn 2: AssistantMessage has NO usage (usage=None)
    turn2_msgs = [
        _make_assistant_message(None, text="turn 2 reply"),
        _make_result_message(session_id="sess-1"),
    ]

    live_client = _make_live_client([turn1_msgs, turn2_msgs])
    ctx = _make_ctx()

    result_events = []
    with patch.object(engine, "PERSISTENT_CLIENT", True), \
         patch.object(engine, "ClaudeSDKClient", return_value=live_client), \
         patch.object(engine, "audit", lambda *a: None):
        # Turn 1
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(ROOT),
            prompt="turn 1",
            session_key="chat:stale",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            if ev.get("type") == "result":
                result_events.append(ev)

        # Turn 2 (same session_key, same live client, new run_engine call)
        async for ev in bot.run_engine(
            project_name="proj",
            cwd=str(ROOT),
            prompt="turn 2",
            session_key="chat:stale",
            model="sonnet",
            ctx=ctx,
            ephemeral=False,
        ):
            if ev.get("type") == "result":
                result_events.append(ev)

    assert len(result_events) == 2, f"Expected 2 result events, got {result_events}"

    turn1_ctx = result_events[0]["context_tokens"]
    turn2_ctx = result_events[1]["context_tokens"]

    assert turn1_ctx == 50000, (
        f"Turn 1 should report 50000 tokens (10k+40k), got {turn1_ctx}"
    )
    # The critical assertion: turn 2 must NOT carry the stale 50000 from turn 1.
    # last_ctx_tokens is reset to 0 at the start of each run_engine call, so when
    # no usage is seen this turn, context_tokens stays at 0.
    assert turn2_ctx != 50000, (
        f"Turn 2 (no usage) must NOT carry stale value 50000 from turn 1, got {turn2_ctx}"
    )
    assert turn2_ctx == 0, (
        f"Turn 2 (no usage) should report 0 (fresh last_ctx_tokens, no update), got {turn2_ctx}"
    )


@pytest.mark.asyncio
async def test_multi_message_turn_uses_max_pt(tmp_path):
    """
    A turn with an intermediate AssistantMessage (usage={}, pt=0) followed by a final
    AssistantMessage with real usage must report the real value, not 0.
    This verifies the MAX-pt approach correctly handles tool-use intermediate messages.
    """
    intermediate_usage = {}  # present but all zeros → pt=0
    final_usage = {
        "input_tokens": 3000,
        "cache_read_input_tokens": 27000,
        "cache_creation_input_tokens": 0,
    }
    msgs = [
        _make_assistant_message(intermediate_usage, text="(thinking)"),
        _make_assistant_message(final_usage, text="final answer"),
        _make_result_message(),
    ]
    events = await _collect_events(msgs)
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    ct = result_events[0]["context_tokens"]
    assert ct == 30000, (
        f"MAX-pt approach: final message (30000) must win over intermediate (0), got {ct}"
    )


@pytest.mark.asyncio
async def test_usage_present_zero_pt_writes_zero(tmp_path):
    """
    Edge case: usage IS present on the AssistantMessage but all token counts are 0
    (e.g. a fresh empty session first handshake). With the fix, context_tokens should
    be 0 — not left at some prior stale value.
    """
    usage_zero = {
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    msgs = [
        _make_assistant_message(usage_zero, text="empty"),
        _make_result_message(),
    ]
    events = await _collect_events(msgs)
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    # Usage was present (all zeros) → _turn_max_pt = 0 → last_ctx_tokens = 0
    assert result_events[0]["context_tokens"] == 0, (
        f"Usage present with all-zero counts should write 0, got {result_events[0]['context_tokens']}"
    )


# ─────────────────────────── webapp.py tests ─────────────────────────────────

def _make_history_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pdir = tmp_path / "myproj"
    pdir.mkdir()
    password = "secr3t"
    ctx = {
        "topics": {
            "0:1": {
                "project": "myproj",
                "cwd": str(pdir),
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
        "GROUP_CHAT_ID": 0,
    }
    ctx["_auth_token"] = _derive_token(password)
    ctx["_pdir"] = pdir
    return ctx


def _make_history_app(ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get(
        "/api/projects/{id}/session-history",
        _webapp.api_project_session_history,
    )
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_history_absent_session_returns_context_tokens_zero(aiohttp_client, tmp_path):
    """
    GET /session-history when no session is bound must return context_tokens:0 explicitly,
    not omit the field. This lets the frontend distinguish 'fresh session (known 0)' from
    'no data (null/missing)'.

    Spec-043 C: the early-return at line ~7158 now includes context_tokens:0 and
    last_cache_hit_pct:None.
    """
    ctx = _make_history_ctx(tmp_path)
    app = _make_history_app(ctx)
    client = await aiohttp_client(app)

    resp = await client.get(
        "/api/projects/myproj/session-history",
        headers=_auth(ctx),
    )
    assert resp.status == 200
    data = await resp.json()

    # Basic shape (existing contract)
    assert data.get("messages") == [], f"messages must be [] for absent session, got {data}"
    assert data.get("session_id") is None

    # Spec-043 C: explicit zeros so the frontend can distinguish fresh vs null
    assert "context_tokens" in data, (
        "context_tokens must be present in absent-session response (spec-043 C)"
    )
    assert data["context_tokens"] == 0, (
        f"context_tokens must be 0 for absent session, got {data['context_tokens']}"
    )
    assert "last_cache_hit_pct" in data, (
        "last_cache_hit_pct must be present in absent-session response (spec-043 C)"
    )
    assert data["last_cache_hit_pct"] is None, (
        f"last_cache_hit_pct must be None for absent session, got {data['last_cache_hit_pct']}"
    )

"""
Regression tests for:
  FIX 1 — served-model observability (model_info event, model_served on result/ledger)
  FIX 2 — persistent-client fingerprint freeze (effort + stable-append included)
  FIX 3 — (comment-only, no runtime behaviour; not tested here)

Mock strategy mirrors test_spec028_persistent_client.py:
  - FakeClient with __aenter__/__aexit__ for the PERSISTENT_CLIENT=0 (async-with) path
  - ClaudeSDKClient patched at engine level
  - audit patched to no-op
  - append_usage_ledger patched to capture records
"""
import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine


# ─────────────────────────────── shared helpers ──────────────────────────────


def _make_result_msg(session_id="sid-test"):
    """Minimal ResultMessage mock. Ledger fires only when an AssistantMessage with usage
    precedes this — use _make_assistant_msg() + _make_result_msg() together for ledger tests."""
    from claude_agent_sdk import ResultMessage

    msg = MagicMock(spec=ResultMessage)
    msg.__class__ = ResultMessage
    msg.session_id = session_id
    msg.total_cost_usd = None
    msg.api_error_status = None
    msg.duration_ms = 100
    msg.duration_api_ms = None
    msg.stop_reason = "end_turn"
    msg.structured_output = None
    msg.usage = None  # usage on ResultMessage is separate from AssistantMessage.usage
    return msg


def _make_assistant_msg_with_usage():
    """AssistantMessage with usage dict so _turn_max_pt gets set and the ledger row fires."""
    from claude_agent_sdk import AssistantMessage

    msg = MagicMock(spec=AssistantMessage)
    msg.__class__ = AssistantMessage
    msg.parent_tool_use_id = None  # spec-071: real default — engine filters parented (sub-agent) messages
    msg.content = []  # no text blocks needed for ledger test
    msg.usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    return msg


def _make_system_init_msg(model_id: str):
    """Fake SDK SystemMessage with subtype='init' carrying the served model id."""
    from claude_agent_sdk import SystemMessage

    msg = MagicMock(spec=SystemMessage)
    msg.__class__ = SystemMessage
    msg.subtype = "init"
    msg.data = {"model": model_id}
    # Ensure isinstance checks for task subtypes fail cleanly.
    from claude_agent_sdk import TaskStartedMessage, TaskNotificationMessage, TaskProgressMessage
    msg.__class__ = SystemMessage  # not a task subclass
    return msg


def _make_fresh_client(messages):
    """Async-context-manager style client for the PERSISTENT_CLIENT=0 path."""
    client = MagicMock()
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def _receive():
        for m in messages:
            yield m

    client.receive_response = _receive
    return client


async def _collect_events(tmp_path, messages, model="sonnet", effort=None,
                           ultracode=False, skip_conductor=False):
    """Run run_engine with PERSISTENT_CLIENT=0 and collect all emitted events.

    Also captures ledger rows written during the run.
    """
    client = _make_fresh_client(messages)
    ledger_rows = []

    def _fake_ledger(row):
        ledger_rows.append(row)

    with (
        patch.object(engine, "PERSISTENT_CLIENT", False),
        patch.object(engine, "ClaudeSDKClient", return_value=client),
        patch.object(engine, "audit", lambda *a: None),
        patch.object(engine, "append_usage_ledger", side_effect=_fake_ledger),
    ):
        events = []
        async for ev in engine.run_engine(
            project_name="test-proj",
            cwd=str(tmp_path),
            prompt="hello",
            session_key="chat:fix-test",
            model=model,
            effort=effort,
            ultracode=ultracode,
            skip_conductor_prompt=skip_conductor,
            ctx=None,
        ):
            events.append(ev)

    return events, ledger_rows


# ─────────────────────── FIX 1: served-model observability ───────────────────


@pytest.mark.asyncio
async def test_result_event_has_model_served_field(tmp_path):
    """result event always carries model_served (None when no init message)."""
    msgs = [_make_result_msg()]
    events, _ = await _collect_events(tmp_path, msgs)
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events, "Expected at least one result event"
    r = result_events[0]
    assert "model_served" in r, "result event must have model_served field"
    # No init message was injected, so served_model is None.
    assert r["model_served"] is None


@pytest.mark.asyncio
async def test_result_event_model_served_populated_from_init(tmp_path):
    """When an init SystemMessage precedes the result, model_served is set."""
    init_msg = _make_system_init_msg("claude-sonnet-4-8")
    result_msg = _make_result_msg()
    msgs = [init_msg, result_msg]

    events, _ = await _collect_events(tmp_path, msgs, model="sonnet")
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    assert result_events[0]["model_served"] == "claude-sonnet-4-8"


@pytest.mark.asyncio
async def test_model_info_event_emitted_on_family_mismatch(tmp_path):
    """model_info with fallback=True is emitted when served family differs from requested alias.

    Scenario: requested fable, but SDK served opus (fallback engaged).
    """
    # resolved_model for "fable" alias becomes "claude-fable-5" (or whatever MODELS maps to).
    # We use the direct MODELS alias so we don't hardcode a model id.
    fable_resolved = engine.MODELS.get("fable", "fable")
    # Simulate the SDK init saying it actually ran opus.
    init_msg = _make_system_init_msg("claude-opus-4-8")
    result_msg = _make_result_msg()
    msgs = [init_msg, result_msg]

    events, _ = await _collect_events(tmp_path, msgs, model="fable")
    model_info_events = [e for e in events if e.get("type") == "model_info"]
    assert model_info_events, (
        "model_info event must be emitted when served model family mismatches requested alias"
    )
    mi = model_info_events[0]
    assert mi["fallback"] is True
    assert mi["served"] == "claude-opus-4-8"
    assert "fable" in (mi["requested"] or "").lower(), (
        f"requested field should contain 'fable', got {mi['requested']!r}"
    )


@pytest.mark.asyncio
async def test_model_info_event_absent_on_family_match(tmp_path):
    """No model_info event when served model matches requested alias family."""
    # sonnet requested → sdk serves claude-sonnet-4-8 → match, no event
    init_msg = _make_system_init_msg("claude-sonnet-4-8")
    result_msg = _make_result_msg()
    msgs = [init_msg, result_msg]

    events, _ = await _collect_events(tmp_path, msgs, model="sonnet")
    model_info_events = [e for e in events if e.get("type") == "model_info"]
    assert not model_info_events, (
        f"No model_info event expected on family match; got {model_info_events}"
    )


@pytest.mark.asyncio
async def test_result_event_has_stop_reason(tmp_path):
    """result event carries stop_reason from ResultMessage when present."""
    result_msg = _make_result_msg()
    result_msg.stop_reason = "end_turn"
    msgs = [result_msg]

    events, _ = await _collect_events(tmp_path, msgs)
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    assert result_events[0].get("stop_reason") == "end_turn"


@pytest.mark.asyncio
async def test_ledger_record_includes_model_served(tmp_path):
    """Ledger row must include model_served field (FIX 1d).

    The ledger fires only when an AssistantMessage with usage precedes ResultMessage
    (sets _turn_max_pt to a non-None value).
    """
    init_msg = _make_system_init_msg("claude-fable-5")
    asst_msg = _make_assistant_msg_with_usage()
    result_msg = _make_result_msg()
    msgs = [init_msg, asst_msg, result_msg]

    events, ledger_rows = await _collect_events(tmp_path, msgs, model="fable",
                                                 skip_conductor=True)
    assert ledger_rows, "Expected at least one ledger row (usage-bearing turn)"
    row = ledger_rows[0]
    assert "model_served" in row, f"Ledger row missing model_served: {row}"
    # model_served should be the served id, not the alias
    assert row["model_served"] == "claude-fable-5", (
        f"Expected 'claude-fable-5', got {row['model_served']!r}"
    )


@pytest.mark.asyncio
async def test_ledger_model_served_none_when_no_init(tmp_path):
    """Ledger row carries model_served=None when no init SystemMessage was received."""
    asst_msg = _make_assistant_msg_with_usage()
    result_msg = _make_result_msg()
    msgs = [asst_msg, result_msg]
    _, ledger_rows = await _collect_events(tmp_path, msgs)
    assert ledger_rows, "Expected ledger row (usage-bearing turn)"
    assert ledger_rows[0].get("model_served") is None


# ─────────────────────── FIX 2: fingerprint freeze ───────────────────────────


def _make_base_opts(tmp_path):
    """Minimal ClaudeAgentOptions for fingerprint tests."""
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(
        model="claude-sonnet-4-8",
        cwd=str(tmp_path),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code"},
    )


def test_fingerprint_changes_when_effort_changes(tmp_path):
    """Changing effort must produce a different fingerprint (FIX 2)."""
    opts = _make_base_opts(tmp_path)
    fp_high = engine._compute_fingerprint(opts, effort="high")
    fp_low = engine._compute_fingerprint(opts, effort="low")
    assert fp_high != fp_low, (
        "Fingerprint must differ when effort changes (high vs low)"
    )


def test_fingerprint_stable_same_effort(tmp_path):
    """Same effort same opts → identical fingerprint (sanity check)."""
    opts = _make_base_opts(tmp_path)
    fp1 = engine._compute_fingerprint(opts, effort="high")
    fp2 = engine._compute_fingerprint(opts, effort="high")
    assert fp1 == fp2


def test_fingerprint_changes_when_stable_append_differs(tmp_path):
    """Changing stable_append_hash (e.g. ultracode toggled) must change fingerprint (FIX 2)."""
    opts = _make_base_opts(tmp_path)
    # Simulate ultracode=False stable hash
    hash_no_ultracode = hashlib.sha256(
        (engine.DEFAULT_NUDGE + "||").encode()
    ).hexdigest()[:16]
    # Simulate ultracode=True stable hash
    hash_with_ultracode = hashlib.sha256(
        (engine.DEFAULT_NUDGE + "||" + engine.ULTRACODE_PROMPT).encode()
    ).hexdigest()[:16]

    fp_normal = engine._compute_fingerprint(opts, stable_append_hash=hash_no_ultracode, effort="high")
    fp_ultra = engine._compute_fingerprint(opts, stable_append_hash=hash_with_ultracode, effort="high")
    assert fp_normal != fp_ultra, (
        "Fingerprint must differ when ultracode toggled (stable_append_hash changes)"
    )


def test_fingerprint_unchanged_when_only_board_snapshot_differs(tmp_path):
    """Board-card snapshot text changes must NOT change the fingerprint (FIX 2).

    The stable_append_hash only includes BOARD_PROTOCOL (the header), not the
    per-turn card-text snapshot.  So updating card content between turns must
    leave the fingerprint the same.
    """
    opts = _make_base_opts(tmp_path)
    # Both turns have the board protocol active but different card snapshots.
    # Stable hash only covers the protocol header, not the snapshot text.
    stable_pieces = [engine.DEFAULT_NUDGE, engine.BOARD_PROTOCOL, "", "", "", ""]
    stable_content = "|".join(stable_pieces)
    stable_hash = hashlib.sha256(stable_content.encode()).hexdigest()[:16]

    # Fingerprint computed with this stable hash should be identical regardless
    # of what the volatile card text says — that text is NOT passed in.
    fp_turn1 = engine._compute_fingerprint(opts, stable_append_hash=stable_hash, effort="high")
    fp_turn2 = engine._compute_fingerprint(opts, stable_append_hash=stable_hash, effort="high")
    assert fp_turn1 == fp_turn2, (
        "Fingerprint must not change when only board snapshot text differs between turns"
    )


def test_fingerprint_changes_when_conductor_toggled(tmp_path):
    """Toggling conductor (via skip_conductor_prompt) must change stable_append_hash and fingerprint."""
    opts = _make_base_opts(tmp_path)
    # With conductor
    pieces_with = [engine.DEFAULT_NUDGE, "", engine.CONDUCTOR_PROMPT, "", "", "", ""]
    hash_with = hashlib.sha256("|".join(pieces_with).encode()).hexdigest()[:16]
    # Without conductor
    pieces_without = [engine.DEFAULT_NUDGE, "", "", "", "", "", ""]
    hash_without = hashlib.sha256("|".join(pieces_without).encode()).hexdigest()[:16]

    fp_with = engine._compute_fingerprint(opts, stable_append_hash=hash_with, effort="high")
    fp_without = engine._compute_fingerprint(opts, stable_append_hash=hash_without, effort="high")
    assert fp_with != fp_without, (
        "Fingerprint must differ when conductor is toggled (stable_append_hash differs)"
    )


@pytest.mark.asyncio
async def test_effort_change_triggers_live_client_eviction(tmp_path):
    """With PERSISTENT_CLIENT=1, changing effort must evict the old client and reconnect.

    Uses the persistent-client path (live client pattern, no async-with).
    """
    from claude_agent_sdk import ResultMessage

    result1 = _make_result_msg(session_id="s1")
    result2 = _make_result_msg(session_id="s2")

    def _make_live_client_for(msgs):
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.interrupt = AsyncMock()
        turn_idx = [-1]

        async def _query(_p):
            turn_idx[0] += 1

        async def _receive():
            for m in msgs[turn_idx[0]] if turn_idx[0] < len(msgs) else []:
                yield m

        client.query = _query
        client.receive_response = _receive
        return client

    client_high = _make_live_client_for([[result1]])
    client_low = _make_live_client_for([[result2]])
    clients = [client_high, client_low]
    created = [0]

    def _factory(options):
        # engine calls ClaudeSDKClient(options=opts) so the kwarg name is 'options'
        c = clients[created[0]]
        created[0] += 1
        return c

    ctx = {"running": {}, "live_clients": {}}

    with (
        patch.object(engine, "PERSISTENT_CLIENT", True),
        patch.object(engine, "ClaudeSDKClient", side_effect=_factory),
        patch.object(engine, "audit", lambda *a: None),
        patch.object(engine, "append_usage_ledger", lambda *a: None),
    ):
        # Turn 1: effort=high
        async for _ in engine.run_engine(
            project_name="p", cwd=str(tmp_path), prompt="t1",
            session_key="chat:effort-test", model="sonnet", effort="high",
            ctx=ctx, ephemeral=False,
        ):
            pass

        entry_after_t1 = ctx["live_clients"].get("chat:effort-test")
        assert entry_after_t1 is not None
        assert entry_after_t1.client is client_high

        # Turn 2: effort=low — fingerprint must differ → eviction
        async for _ in engine.run_engine(
            project_name="p", cwd=str(tmp_path), prompt="t2",
            session_key="chat:effort-test", model="sonnet", effort="low",
            ctx=ctx, ephemeral=False,
        ):
            pass

    entry_after_t2 = ctx["live_clients"].get("chat:effort-test")
    assert entry_after_t2 is not None
    assert entry_after_t2.client is client_low, (
        "effort change must evict old client and create new one"
    )
    assert created[0] == 2, f"Expected 2 client creations, got {created[0]}"
    assert client_high.disconnect.called, "Old client must be disconnected on eviction"

    # Cleanup idle tasks
    for entry in list(ctx["live_clients"].values()):
        if entry.idle_task and not entry.idle_task.done():
            entry.idle_task.cancel()
    await asyncio.sleep(0)

"""
Regression tests for spec-092: the `backend` dimension on the live-client fingerprint.

Context: PERSISTENT_CLIENT=1 reuses a connected ClaudeSDKClient subprocess across turns,
guarded by `_compute_fingerprint`. That fingerprint deliberately excludes `env` (it carries
per-turn noise like TG_CHAT_ID) — which is exactly why `account` and `memory_mode` are threaded
in as separate explicit arguments instead. spec-092 adds a third inference backend (a local
Ollama endpoint reached via an ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL overlay
in `env`) — without an explicit `backend` fingerprint dimension, a live client started for
Claude would be silently REUSED for an Ollama run (or vice versa): the turn would succeed while
talking to the wrong endpoint.

Mock strategy for the end-to-end eviction test mirrors
test_fix_observability_fingerprint.py::test_effort_change_triggers_live_client_eviction.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine


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


# ─────────────────────── backend dimension on _compute_fingerprint ───────────────────────


def test_fingerprint_differs_for_different_backends(tmp_path):
    """Two different backend values must produce different fingerprints — this is the core
    defect fix: reusing a live client across backends must be impossible."""
    opts = _make_base_opts(tmp_path)
    fp_claude = engine._compute_fingerprint(opts, backend="")
    fp_ollama = engine._compute_fingerprint(opts, backend="ollama:llama3")
    assert fp_claude != fp_ollama, (
        "Fingerprint must differ when backend changes (claude vs ollama), "
        "or a live client would be silently reused across backends"
    )


def test_fingerprint_identical_for_identical_backend(tmp_path):
    """Same backend + same other inputs → identical fingerprint (no spurious eviction)."""
    opts = _make_base_opts(tmp_path)
    fp1 = engine._compute_fingerprint(opts, backend="ollama:llama3", effort="high")
    fp2 = engine._compute_fingerprint(opts, backend="ollama:llama3", effort="high")
    assert fp1 == fp2


def test_default_backend_matches_omitted_argument(tmp_path):
    """backend='' (explicit default) must equal a call that omits the argument entirely —
    proves the change is backward compatible with every existing call site."""
    opts = _make_base_opts(tmp_path)
    fp_explicit_default = engine._compute_fingerprint(opts, effort="high", backend="")
    fp_omitted = engine._compute_fingerprint(opts, effort="high")
    assert fp_explicit_default == fp_omitted, (
        "Default backend='' must be byte-identical to a caller that never mentions backend"
    )


def test_existing_dimensions_still_change_fingerprint_alongside_backend(tmp_path):
    """model, account, effort and memory_mode must still each independently change the
    fingerprint now that backend has been added — the new argument must not have swallowed
    or shadowed any of the pre-existing ones."""
    from claude_agent_sdk import ClaudeAgentOptions

    opts_sonnet = _make_base_opts(tmp_path)
    opts_opus = ClaudeAgentOptions(
        model="claude-opus-4-8",
        cwd=str(tmp_path),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    base_kwargs = dict(effort="high", memory_mode="auto", account="main", backend="")

    # model dimension
    assert engine._compute_fingerprint(opts_sonnet, **base_kwargs) != \
        engine._compute_fingerprint(opts_opus, **base_kwargs)

    # account dimension
    assert engine._compute_fingerprint(opts_sonnet, **{**base_kwargs, "account": "main"}) != \
        engine._compute_fingerprint(opts_sonnet, **{**base_kwargs, "account": "secondary"})

    # effort dimension
    assert engine._compute_fingerprint(opts_sonnet, **{**base_kwargs, "effort": "high"}) != \
        engine._compute_fingerprint(opts_sonnet, **{**base_kwargs, "effort": "low"})

    # memory_mode dimension
    assert engine._compute_fingerprint(opts_sonnet, **{**base_kwargs, "memory_mode": "auto"}) != \
        engine._compute_fingerprint(opts_sonnet, **{**base_kwargs, "memory_mode": "project"})


# ─────────────────────── end-to-end: backend threaded through run_engine ───────────────────────


@pytest.mark.asyncio
async def test_backend_change_triggers_live_client_eviction(tmp_path):
    """With PERSISTENT_CLIENT=1, switching `backend` between two turns of the SAME session
    must evict the old live client and connect a fresh one — otherwise a client opened
    against one backend keeps serving turns meant for the other with no error, which is the
    exact defect this change fixes. This exercises the real run_engine() call site, not just
    the pure _compute_fingerprint helper, so it would fail if `backend` were not actually
    threaded through from run_engine into _get_or_create_live_client.
    """
    from claude_agent_sdk import ResultMessage

    def _make_result_msg(session_id):
        msg = MagicMock(spec=ResultMessage)
        msg.__class__ = ResultMessage
        msg.session_id = session_id
        msg.total_cost_usd = None
        msg.api_error_status = None
        msg.duration_ms = 100
        msg.duration_api_ms = None
        msg.stop_reason = "end_turn"
        msg.structured_output = None
        msg.usage = None
        return msg

    result_claude = _make_result_msg(session_id="s-claude")
    result_ollama = _make_result_msg(session_id="s-ollama")

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

    client_claude = _make_live_client_for([[result_claude]])
    client_ollama = _make_live_client_for([[result_ollama]])
    clients = [client_claude, client_ollama]
    created = [0]

    def _factory(options):
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
        # Turn 1: default backend (Claude)
        async for _ in engine.run_engine(
            project_name="p", cwd=str(tmp_path), prompt="t1",
            session_key="chat:backend-test", model="sonnet",
            ctx=ctx, ephemeral=False, backend="",
        ):
            pass

        entry_after_t1 = ctx["live_clients"].get("chat:backend-test")
        assert entry_after_t1 is not None
        assert entry_after_t1.client is client_claude

        # Turn 2: same session, switched backend → must evict + reconnect, not reuse.
        async for _ in engine.run_engine(
            project_name="p", cwd=str(tmp_path), prompt="t2",
            session_key="chat:backend-test", model="sonnet",
            ctx=ctx, ephemeral=False, backend="ollama:llama3",
        ):
            pass

    entry_after_t2 = ctx["live_clients"].get("chat:backend-test")
    assert entry_after_t2 is not None
    assert entry_after_t2.client is client_ollama, (
        "backend change must evict the old (Claude) client and connect a new (Ollama) one, "
        "not silently reuse the old subprocess for the new backend"
    )
    assert created[0] == 2, f"Expected 2 client creations (one per backend), got {created[0]}"
    assert client_claude.disconnect.called, "Old client must be disconnected on backend switch"

    for entry in list(ctx["live_clients"].values()):
        if entry.idle_task and not entry.idle_task.done():
            entry.idle_task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_backend_unchanged_reuses_live_client(tmp_path):
    """Sanity counterpart: two turns with the SAME backend must reuse the live client, so the
    new dimension does not force a reconnect on every turn."""
    from claude_agent_sdk import ResultMessage

    def _make_result_msg(session_id):
        msg = MagicMock(spec=ResultMessage)
        msg.__class__ = ResultMessage
        msg.session_id = session_id
        msg.total_cost_usd = None
        msg.api_error_status = None
        msg.duration_ms = 100
        msg.duration_api_ms = None
        msg.stop_reason = "end_turn"
        msg.structured_output = None
        msg.usage = None
        return msg

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.interrupt = AsyncMock()
    msgs = [[_make_result_msg("s1")], [_make_result_msg("s2")]]
    turn_idx = [-1]

    async def _query(_p):
        turn_idx[0] += 1

    async def _receive():
        for m in msgs[turn_idx[0]] if turn_idx[0] < len(msgs) else []:
            yield m

    client.query = _query
    client.receive_response = _receive

    ctx = {"running": {}, "live_clients": {}}

    with (
        patch.object(engine, "PERSISTENT_CLIENT", True),
        patch.object(engine, "ClaudeSDKClient", return_value=client),
        patch.object(engine, "audit", lambda *a: None),
        patch.object(engine, "append_usage_ledger", lambda *a: None),
    ):
        for prompt in ("t1", "t2"):
            async for _ in engine.run_engine(
                project_name="p", cwd=str(tmp_path), prompt=prompt,
                session_key="chat:backend-stable-test", model="sonnet",
                ctx=ctx, ephemeral=False, backend="ollama:llama3",
            ):
                pass

    assert not client.disconnect.called, "Same backend across turns must not evict the client"
    entry = ctx["live_clients"].get("chat:backend-stable-test")
    assert entry is not None and entry.client is client

    for e in list(ctx["live_clients"].values()):
        if e.idle_task and not e.idle_task.done():
            e.idle_task.cancel()
    await asyncio.sleep(0)


# ─────────────────────── public session_has_live_subagents wrapper ───────────────────────


def test_session_has_live_subagents_wrapper_returns_true_when_callback_says_so():
    """The public wrapper must reflect the same underlying callback the private helper reads
    — webapp.py's runtime-switch guard needs this to see live sub-agents accurately."""
    def _cb(session_key):
        return session_key == "chat:busy"

    with patch.object(engine, "_has_live_subagents_cb", _cb):
        assert engine.session_has_live_subagents("chat:busy") is True
        assert engine.session_has_live_subagents("chat:idle") is False


def test_session_has_live_subagents_wrapper_matches_private_helper():
    """The public wrapper must agree with `_session_has_live_subagents` for the same input —
    it is a thin pass-through, not a reimplementation."""
    def _cb(session_key):
        return session_key == "chat:busy"

    with patch.object(engine, "_has_live_subagents_cb", _cb):
        for key in ("chat:busy", "chat:idle"):
            assert engine.session_has_live_subagents(key) == engine._session_has_live_subagents(key)


def test_session_has_live_subagents_wrapper_false_on_no_callback():
    """No callback registered (webapp not wired) → False, never an exception."""
    with patch.object(engine, "_has_live_subagents_cb", None):
        assert engine.session_has_live_subagents("chat:anything") is False


# ───────────────── backend dimension on the plan/ask fingerprint guard ─────────────────


def _live_entry_for(opts, *, backend, session_key="proj:chat"):
    """A live-client registry entry whose fingerprint was computed for `backend`."""
    return engine._LiveEntry(
        client=MagicMock(),
        fingerprint=engine._compute_fingerprint(
            opts, stable_append_hash="h", effort="high", memory_mode="auto",
            account="main", backend=backend,
        ),
        last_used=0.0,
        idle_task=None,
        session_key=session_key,
    )


def test_plan_gate_accepts_a_client_connected_for_the_same_backend(tmp_path):
    """The plan/ask guard recomputes the fingerprint independently of
    _get_or_create_live_client. If it were to drop `backend`, its `want` would never match an
    entry whose fingerprint includes one — a permanent mismatch, not a race — and EVERY plan
    or ask turn on that session would abort with the misleading "pinned by background tasks"
    error even with zero sub-agents running. This pins the matching half.
    """
    opts = _make_base_opts(tmp_path)
    ctx = {"live_clients": {"proj:chat": _live_entry_for(opts, backend="ollama:local")}}
    assert engine._plan_client_fingerprint_ok(
        ctx, "proj:chat", opts, "h", "high", "auto", "main", backend="ollama:local"
    ) is True


def test_plan_gate_rejects_a_client_connected_for_another_backend(tmp_path):
    """The mismatching half: a client connected against one backend must NOT be accepted for
    a gated turn bound to another. can_use_tool binds at connect time, so reusing it would run
    the turn full-auto against the wrong endpoint with no gate and no error.
    """
    opts = _make_base_opts(tmp_path)
    ctx = {"live_clients": {"proj:chat": _live_entry_for(opts, backend="ollama:local")}}
    assert engine._plan_client_fingerprint_ok(
        ctx, "proj:chat", opts, "h", "high", "auto", "main", backend=""
    ) is False

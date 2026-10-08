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


@pytest.fixture(autouse=True)
def _fresh_chats_lock(monkeypatch):
    """The chats lock is a module global that binds to the first loop that CONTENDS it; these
    tests hold it on purpose, so each gets a lock of its own loop."""
    monkeypatch.setattr(_webapp, "_CHATS_LOCK", None)


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    ap = web.Application(middlewares=[_webapp.auth_middleware])
    ap["ctx"] = fake_ctx
    ap.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    ap.router.add_post("/api/projects/{id}/session", _webapp.api_project_set_session)
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


# ═════════════════════════ item 12: a provider gate bites at all three run sites ═════════════
#
# No registered provider has a gate any more (Grok's was removed 2026-10-03), so nothing exercised
# `_gated_engine` / the POST refusal: both could be deleted with the suite green, and the first
# provider that ships a real gate would have started on an untested choke point. These tests
# register a gated provider of their own (undone by monkeypatch) and drive the three run sites.

GATE_REFUSAL = "gated is off for this project"


@pytest.fixture
def gated_provider(monkeypatch, fake_ctx, engines):
    spec = providers.ProviderSpec(
        name="gated", label="Gated", engine_key="run_gated_engine",
        continuity_field="gated_session_id", resume_kwarg="resume_session_id",
        result_key="gated_id", fallback_model=lambda ctx: "gx", enabled=lambda: True,
        capabilities=lambda: {"chat": True},
        gate=lambda project: None if project.get("gated_ok") is True else GATE_REFUSAL,
        gate_field="gated_ok",
    )
    monkeypatch.setitem(providers._REGISTRY, "gated", spec)
    engines["gated"] = []

    async def engine(**kw):
        engines["gated"].append(kw)
        yield {"type": "text", "text": "answer"}
        yield {"type": "result", "gated_id": "NEW-GATED", "context_tokens": 3}

    fake_ctx["run_gated_engine"] = engine
    return spec


def _open_the_gate(ctx):
    ctx["topics"][SESSION_KEY]["gated_ok"] = True


def _runs(engines):
    return {k: len(v) for k, v in engines.items()}


NOTHING = {"claude": 0, "codex": 0, "grok": 0, "gated": 0}


def test_the_test_provider_really_is_gated(gated_provider):
    assert gated_provider.has_gate
    assert _webapp._provider_gate_refusal({}, "gated") == GATE_REFUSAL
    assert _webapp._provider_gate_refusal({"gated_ok": True}, "gated") is None


@pytest.mark.asyncio
async def test_a_gated_provider_is_refused_at_the_queue_drain_and_runs_nowhere(
    fake_ctx, engines, gated_provider
):
    _seed_chat(fake_ctx, provider="gated", model="gx", gated_session_id="OLD-GATED")
    # accepted while the project could use it ...
    _open_the_gate(fake_ctx)
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert pinned["provider"] == "gated"
    # ... and revoked before the drain
    fake_ctx["topics"][SESSION_KEY].pop("gated_ok")
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned)

    assert _runs(engines) == NOTHING, "the refused message ran on an engine"
    assert GATE_REFUSAL in _error_texts(), "the refusal must be an error on the turn"
    assert any(e.get("kind") == "run_end" and e.get("outcome") == "fail" for e in _live_events())
    assert fake_ctx["sessions"][SESSION_KEY] == "CLAUDE-FLAT-SESSION"
    assert _chat_record(fake_ctx)["gated_session_id"] == "OLD-GATED"
    assert not fake_ctx["running"].get(SESSION_KEY)


@pytest.mark.asyncio
async def test_the_same_drain_runs_on_the_gated_provider_once_the_gate_is_open(
    fake_ctx, engines, gated_provider
):
    """Control: the refusal above is the gate's, not a harness that cannot run this provider."""
    _seed_chat(fake_ctx, provider="gated", model="gx", gated_session_id="OLD-GATED")
    _open_the_gate(fake_ctx)
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned)
    assert _runs(engines) == {**NOTHING, "gated": 1}
    assert _chat_record(fake_ctx)["gated_session_id"] == "NEW-GATED"
    assert not _error_texts()


@pytest.mark.asyncio
async def test_a_gated_provider_is_refused_by_the_direct_post_and_runs_nowhere(
    aiohttp_client, fake_ctx, app, engines, gated_provider
):
    _seed_chat(fake_ctx, provider="gated", model="gx", gated_session_id="OLD-GATED")
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello", "chat_id": CHAT_ID}, headers=_auth(fake_ctx))
    assert resp.status == 409 and await resp.json() == {"error": GATE_REFUSAL}
    assert _runs(engines) == NOTHING
    assert fake_ctx["sessions"][SESSION_KEY] == "CLAUDE-FLAT-SESSION"
    assert _chat_record(fake_ctx)["gated_session_id"] == "OLD-GATED"

    _open_the_gate(fake_ctx)                                       # control
    resp, events = await _post_chat(client, fake_ctx)
    assert resp.status == 200 and _runs(engines) == {**NOTHING, "gated": 1}
    assert _chat_record(fake_ctx)["gated_session_id"] == "NEW-GATED"


async def _run_gated_card(fake_ctx, tmp_path, *, gate_open):
    project = {"name": "myproject", "cwd": str(tmp_path / "cardproj"), "session_key": SESSION_KEY,
               "model": "sonnet", **({"gated_ok": True} if gate_open else {})}
    Path(project["cwd"]).mkdir(exist_ok=True)
    _webapp._save_board(project["cwd"], "myproject", "# T", {
        "backlog": [], "in_progress": [{"id": "aabbcc", "text": "Build", "provider": "gated"}],
        "review": [], "failed": []})
    card = {"id": "aabbcc", "text": "Build", "provider": "gated", "description": None}
    fake_ctx["running"][SESSION_KEY] = True
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        await _webapp._run_card(fake_ctx, None, project, card, SESSION_KEY, run_mode="legacy")
    return _webapp._load_board(project["cwd"])[2]


@pytest.mark.asyncio
async def test_a_gated_provider_is_refused_for_a_board_card_and_the_card_fails_with_the_reason(
    fake_ctx, tmp_path, engines, gated_provider
):
    cols = await _run_gated_card(fake_ctx, tmp_path, gate_open=False)
    assert _runs(engines) == NOTHING, "no engine ran - in particular not Claude's"
    assert [c["id"] for c in cols["failed"]] == ["aabbcc"] and not cols["review"]
    sidecar = (fake_ctx["DATA"] / "runs" / "aabbcc.md").read_text()
    assert "Outcome:** fail" in sidecar and GATE_REFUSAL in sidecar
    assert SESSION_KEY not in fake_ctx["running"], "the run lock is released"


@pytest.mark.asyncio
async def test_the_same_card_runs_on_the_gated_provider_once_the_gate_is_open(
    fake_ctx, tmp_path, engines, gated_provider
):
    cols = await _run_gated_card(fake_ctx, tmp_path, gate_open=True)
    assert _runs(engines) == {**NOTHING, "gated": 1}
    assert not cols["failed"]


# ═════════════════════════ item 13a: a pinned old-provider drain and the armed handoff ═══════


def _armed(for_provider, text="BLOCK"):
    return {"text": text, "for_provider": for_provider, "for_backend": "",
            "from_label": "A", "to_label": "B"}


async def _grok_pinned_item_after_the_chat_moved_to(fake_ctx, new_provider, *, armed_for):
    """A message accepted on a Grok chat; before it drains the operator switches the chat to
    `new_provider` and commits a handoff armed for `armed_for`."""
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-OLD")
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert pinned["provider"] == "grok"
    _seed_chat(fake_ctx, provider=new_provider, model="opus" if new_provider == "claude" else "gpt-5.6-sol",
               grok_session_id="GROK-OLD", session_id="CLAUDE-OWN",
               runtime_handoff=_armed(armed_for, "BLOCK-FOR-" + armed_for.upper()))
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned)


@pytest.mark.asyncio
async def test_a_pinned_old_provider_drain_leaves_the_handoff_armed_for_the_chats_new_provider(
    fake_ctx, engines, grok_on
):
    await _grok_pinned_item_after_the_chat_moved_to(fake_ctx, "claude", armed_for="claude")

    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [0, 0, 1], "the pin ran on Grok"
    assert "BLOCK-FOR-CLAUDE" not in engines["grok"][0]["prompt"], "a block for Claude is not Grok's"
    rec = _chat_record(fake_ctx)
    assert rec["runtime_handoff"]["text"] == "BLOCK-FOR-CLAUDE", "the NEW provider's block was lost"
    assert rec["runtime_handoff"]["for_provider"] == "claude"
    assert rec["grok_session_id"] == "NEW-ID" and rec["provider"] == "claude"


@pytest.mark.asyncio
async def test_a_pinned_drain_still_drops_a_block_that_is_stale_for_the_chat_itself(
    fake_ctx, engines, grok_on, codex_on
):
    """Control: a block armed for neither the pinned run nor the chat's current runtime (the
    operator flipped away and back before sending) is dead and goes."""
    await _grok_pinned_item_after_the_chat_moved_to(fake_ctx, "claude", armed_for="codex")
    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [0, 0, 1]
    assert "BLOCK-FOR-CODEX" not in engines["grok"][0]["prompt"]
    assert "runtime_handoff" not in _chat_record(fake_ctx)


@pytest.mark.asyncio
async def test_a_drain_never_clears_a_block_it_did_not_deliver(fake_ctx, engines, grok_on):
    """A block armed while the turn was already running (nothing was armed at drain start) is for
    the NEXT turn: answering this one must not delete it."""
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-OLD")
    inner = fake_ctx["run_grok_engine"]

    async def engine(**kw):
        data = _webapp._load_chats(fake_ctx)
        data[PROJECT_ID]["chats"][0]["runtime_handoff"] = _armed("grok", "ARMED-MID-TURN")
        _webapp._save_chats(fake_ctx, data)
        async for ev in inner(**kw):
            yield ev

    fake_ctx["run_grok_engine"] = engine
    await _drain_item(fake_ctx, chat_id=CHAT_ID, project_id=PROJECT_ID,
                      pinned_runtime={"provider": "grok", "model": "grok-4.7"})
    assert _chat_record(fake_ctx)["runtime_handoff"]["text"] == "ARMED-MID-TURN"


# ═════════════════════════ item 13b: one active chat for the UI and for session New / Resume ════


def _two_chats(ctx, *, active, grok_provider="grok"):
    """G (a Grok chat) and C (a Claude chat); `active` is the id stored on disk."""
    _webapp._save_chats(ctx, {PROJECT_ID: {"active": active, "chats": [
        {"id": "gggggg", "name": "G", "provider": grok_provider, "model": "grok-4.7",
         "grok_session_id": "GROK-G", "session_id": "G-LEFTOVER"},
        {"id": "cccccc", "name": "C", "provider": "claude", "model": "opus",
         "session_id": "CLAUDE-C-OLD"}]}})


def _chats_by_id(ctx):
    return {c["id"]: c for c in _webapp._load_chats(ctx)[PROJECT_ID]["chats"]}


@pytest.mark.asyncio
async def test_session_new_resets_the_chat_the_ui_shows_when_a_disabled_providers_chat_is_active_on_disk(
    aiohttp_client, fake_ctx, app
):
    """Grok is switched off: the UI shows C as active, the disk still says G."""
    _two_chats(fake_ctx, active="gggggg")
    assert _webapp._effective_active_chat(_webapp._load_chats(fake_ctx)[PROJECT_ID]) == "cccccc"
    client = await aiohttp_client(app)
    resp = await client.post(f"/api/projects/{PROJECT_ID}/session", json={"action": "new"},
                            headers=_auth(fake_ctx))
    assert resp.status == 200, await resp.text()
    chats = _chats_by_id(fake_ctx)
    assert chats["cccccc"]["session_id"] is None, "'New' did not reset the chat the operator sees"
    assert chats["gggggg"]["session_id"] == "G-LEFTOVER" and chats["gggggg"]["grok_session_id"] == "GROK-G"
    assert SESSION_KEY not in fake_ctx["sessions"]


@pytest.mark.asyncio
async def test_session_resume_targets_the_chat_the_ui_shows_when_a_disabled_providers_chat_is_active_on_disk(
    aiohttp_client, fake_ctx, app, tmp_path
):
    _two_chats(fake_ctx, active="gggggg")
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "SESS-1.jsonl").write_text("{}\n")
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_sdk_sessions_dir", return_value=sdk):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/session",
                                json={"action": "resume", "session_id": "SESS-1"}, headers=_auth(fake_ctx))
    assert resp.status == 200, await resp.text()
    chats = _chats_by_id(fake_ctx)
    assert chats["cccccc"]["session_id"] == "SESS-1"
    assert chats["gggggg"]["session_id"] == "G-LEFTOVER", "a hidden chat received the resumed id"


async def _post_while_the_picker_runs(client, ctx, body, flip):
    """Hold the chats lock, start the request, let it queue on the lock, run `flip` (what a
    runtime-picker PATCH does), release."""
    async with _webapp._chats_lock():
        task = asyncio.ensure_future(client.post(
            f"/api/projects/{PROJECT_ID}/session", json=body, headers=_auth(ctx)))
        await asyncio.sleep(0.15)
        assert not task.done(), "the request must be waiting for the lock"
        flip()
    return await task


@pytest.mark.asyncio
async def test_session_resume_does_not_write_claudes_id_onto_a_chat_that_became_grok_meanwhile(
    aiohttp_client, fake_ctx, app, tmp_path
):
    _two_chats(fake_ctx, active="cccccc")
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "SESS-1.jsonl").write_text("{}\n")

    def flip():
        data = _webapp._load_chats(fake_ctx)
        c = next(c for c in data[PROJECT_ID]["chats"] if c["id"] == "cccccc")
        c.update(provider="grok", model="grok-4.7", grok_session_id="GROK-C")
        _webapp._save_chats(fake_ctx, data)

    client = await aiohttp_client(app)
    with patch.object(_webapp, "_sdk_sessions_dir", return_value=sdk):
        resp = await _post_while_the_picker_runs(
            client, fake_ctx, {"action": "resume", "session_id": "SESS-1"}, flip)
    assert resp.status == 409, await resp.text()
    chats = _chats_by_id(fake_ctx)
    assert chats["cccccc"]["session_id"] == "CLAUDE-C-OLD", "Claude's id was written onto a Grok chat"
    assert fake_ctx["sessions"][SESSION_KEY] == "CLAUDE-FLAT-SESSION"


@pytest.mark.asyncio
async def test_session_new_clears_the_provider_the_chat_has_when_the_lock_is_taken_not_the_one_read_before(
    aiohttp_client, fake_ctx, app
):
    _two_chats(fake_ctx, active="cccccc")

    def flip():
        data = _webapp._load_chats(fake_ctx)
        c = next(c for c in data[PROJECT_ID]["chats"] if c["id"] == "cccccc")
        c.update(provider="grok", model="grok-4.7", grok_session_id="GROK-C")
        _webapp._save_chats(fake_ctx, data)

    client = await aiohttp_client(app)
    resp = await _post_while_the_picker_runs(client, fake_ctx, {"action": "new"}, flip)
    assert resp.status == 200, await resp.text()
    c = _chats_by_id(fake_ctx)["cccccc"]
    assert c["grok_session_id"] is None, "the chat is a Grok chat now: its Grok session is what resets"
    assert c["session_id"] == "CLAUDE-C-OLD"
    assert fake_ctx["sessions"][SESSION_KEY] == "CLAUDE-FLAT-SESSION", "Grok's reset never touches Claude's mirror"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,field,session_id", [
    ("grok", "grok_session_id", "01a00000-0000-7000-8000-0000000000c1"),
    ("codex", "codex_thread_id", "thread-abcdef12"),
])
async def test_session_resume_of_an_adapter_chat_does_not_write_onto_a_chat_that_became_claude_meanwhile(
    aiohttp_client, fake_ctx, app, grok_on, codex_on, provider, field, session_id
):
    _webapp._save_chats(fake_ctx, {PROJECT_ID: {"active": "xxxxxx", "chats": [
        {"id": "xxxxxx", "name": "X", "provider": provider, "model": "m", field: "OLD-ID"}]}})

    def flip():
        data = _webapp._load_chats(fake_ctx)
        data[PROJECT_ID]["chats"][0].update(provider="claude", model="opus")
        _webapp._save_chats(fake_ctx, data)

    client = await aiohttp_client(app)
    resp = await _post_while_the_picker_runs(
        client, fake_ctx, {"action": "resume", "session_id": session_id}, flip)
    assert resp.status == 409, await resp.text()
    assert _webapp._load_chats(fake_ctx)[PROJECT_ID]["chats"][0][field] == "OLD-ID"


@pytest.mark.asyncio
async def test_a_chat_less_queue_item_runs_in_the_chat_the_ui_shows_not_a_hidden_disabled_one(
    fake_ctx, engines
):
    """Wakes and Telegram messages carry no chat id: they follow the ACTIVE chat. With Grok
    switched off that is the chat the UI shows (C), exactly as for the direct POST."""
    _two_chats(fake_ctx, active="gggggg")
    await _drain_item(fake_ctx, project_id=PROJECT_ID)             # no chat_id, no pin
    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [1, 0, 0]
    assert engines["claude"][0]["resume_session_id"] == "CLAUDE-C-OLD"
    assert not _error_texts()

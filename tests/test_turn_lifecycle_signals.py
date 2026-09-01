"""The CLI's own account of how a turn ended, and the peer message that used to vanish.

Three behaviours are locked here:

1. Finding F3 — a peer/channel message that arrives BETWEEN turns. `_process_messages` handles
   the mid-turn case; the between-turns drain had no `UserMessage` branch at all, so such a
   message hit no if/elif and was dropped silently. Between turns is the likelier arrival
   window, since a peer message is injected by another session rather than triggered by the
   operator sending anything.
2. `_abort_reason_for` — the CLI's `terminal_reason` wins only in the catch-all bucket. A
   buffer overflow is our own reader giving up, so no CLI verdict can exist for it.
3. A clean `client.interrupt()` never raises, so it left zero `turn_aborted` evidence; the
   result frame now records it.
"""
import asyncio

import pytest

import engine
from claude_agent_sdk import (
    AssistantMessage,
    ProcessError,
    ResultError,
    ResultMessage,
    TextBlock,
    UserMessage,
)


class _FakeDrainClient:
    """Yields the scripted messages, then blocks forever (like a real idle stream)."""

    def __init__(self, messages):
        self._messages = messages
        self._blocker = asyncio.Event()

    async def receive_messages(self):
        for m in self._messages:
            yield m
        await self._blocker.wait()


def _peer_user_msg(text="ping from a peer", kind="peer-message", name="cardloop-a2"):
    return UserMessage(content=text, origin={"kind": kind, "name": name, "body": text})


async def _drain_once(msgs, monkeypatch):
    """Run the between-turns drain over `msgs`, return the published bus events."""
    published = []
    monkeypatch.setattr(engine, "_bus_publish_cb", lambda sk, ev: published.append((sk, ev)))
    monkeypatch.setattr(engine, "_bg_run_cb", lambda sk, phase, text=None: None)
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    return published


# ───────────────────────────── F3: peer messages between turns ──────────────────────────────────

@pytest.mark.asyncio
async def test_drain_surfaces_a_peer_message_that_arrives_between_turns(monkeypatch):
    published = await _drain_once([_peer_user_msg()], monkeypatch)
    assert published, "a peer message arriving between turns must reach the bus, not vanish"
    sk, ev = published[0]
    assert sk == "s"
    assert ev["kind"] == "peer_message", "the bus routing key must not be clobbered by origin.kind"
    assert ev["peer_kind"] == "peer-message"
    assert ev["sender"] == "cardloop-a2"
    assert ev["text"] == "ping from a peer"


@pytest.mark.asyncio
async def test_drain_ignores_a_plain_user_message(monkeypatch):
    """No origin = the operator's own prompt echoed back; nothing to surface."""
    published = await _drain_once([UserMessage(content="hello")], monkeypatch)
    assert published == []


@pytest.mark.asyncio
async def test_drain_ignores_sub_agent_user_traffic(monkeypatch):
    """parent_tool_use_id is sub-agent traffic and must stay out of the chat lane."""
    msg = UserMessage(content="sub noise", parent_tool_use_id="tu1",
                      origin={"kind": "peer-message", "name": "x", "body": "sub noise"})
    published = await _drain_once([msg], monkeypatch)
    assert published == []


@pytest.mark.asyncio
async def test_drain_keeps_working_after_a_peer_message(monkeypatch):
    """The new branch must not swallow the autonomous-turn path that follows it."""
    bg = []
    published = []
    monkeypatch.setattr(engine, "_bus_publish_cb", lambda sk, ev: published.append(ev))
    monkeypatch.setattr(engine, "_bg_run_cb",
                        lambda sk, phase, text=None: bg.append((phase, text)))
    msgs = [
        _peer_user_msg(),
        AssistantMessage(content=[TextBlock(text="background reply")], model="m"),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                      is_error=False, num_turns=1, session_id="sid"),
    ]
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    assert bg == [("start", None), ("text", "background reply"), ("end", None)]
    assert any(ev["kind"] == "peer_message" for ev in published)


@pytest.mark.asyncio
async def test_a_publish_error_never_kills_the_drain(monkeypatch):
    def _boom(sk, ev):
        raise RuntimeError("bus down")

    bg = []
    monkeypatch.setattr(engine, "_bus_publish_cb", _boom)
    monkeypatch.setattr(engine, "_bg_run_cb",
                        lambda sk, phase, text=None: bg.append(phase))
    msgs = [_peer_user_msg(),
            AssistantMessage(content=[TextBlock(text="still alive")], model="m")]
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    # "end" comes from the cancel path closing the half-open run — the point is that
    # "text" arrived at all, i.e. the failing publish did not abort the reader.
    assert bg[:2] == ["start", "text"], "the drain must survive a failing bus publish"


# ─────────────────────────────── abort reason: ours vs the CLI's ────────────────────────────────

def test_buffer_overflow_stays_ours_even_for_a_result_error():
    """Our reader's cap is a fact the CLI cannot have reported — the hint wins."""
    exc = ResultError("boom", data={"subtype": "error_during_execution",
                                    "terminal_reason": "api_error"})
    reason, subtype, errors = engine._abort_reason_for(exc, "buffer hint")
    assert reason == "buffer_overflow"
    assert subtype is None and errors is None


def test_result_error_upgrades_the_sdk_error_catch_all():
    exc = ResultError("boom", data={"subtype": "error_max_turns",
                                    "terminal_reason": "max_turns"})
    reason, subtype, _ = engine._abort_reason_for(exc, None)
    assert reason == "max_turns"
    assert subtype == "error_max_turns"


def test_a_result_error_without_a_terminal_reason_falls_back():
    exc = ResultError("boom", data={"subtype": "error_during_execution"})
    reason, subtype, _ = engine._abort_reason_for(exc, None)
    assert reason == "sdk_error"
    assert subtype == "error_during_execution"


def test_a_plain_process_error_keeps_the_old_reason():
    reason, subtype, errors = engine._abort_reason_for(ProcessError("nope", exit_code=1), None)
    assert (reason, subtype, errors) == ("sdk_error", None, None)


def test_abort_row_carries_the_structured_detail(monkeypatch):
    rows = []
    monkeypatch.setattr(engine, "_timeline_append_cb", lambda sk, row: rows.append(row))
    engine._record_turn_abort("s", "max_turns", "detail", sdk_subtype="error_max_turns",
                              sdk_errors=["a", "b"])
    assert rows == [{"kind": "turn_aborted", "reason": "max_turns", "detail": "detail",
                     "sdk_subtype": "error_max_turns", "sdk_errors": ["a", "b"]}]


def test_abort_row_stays_minimal_without_structured_detail(monkeypatch):
    """No CLI verdict = the row keeps exactly the shape older consumers parse."""
    rows = []
    monkeypatch.setattr(engine, "_timeline_append_cb", lambda sk, row: rows.append(row))
    engine._record_turn_abort("s", "terminated", "sigterm")
    assert rows == [{"kind": "turn_aborted", "reason": "terminated", "detail": "sigterm"}]


def test_abort_row_caps_the_error_list(monkeypatch):
    rows = []
    monkeypatch.setattr(engine, "_timeline_append_cb", lambda sk, row: rows.append(row))
    engine._record_turn_abort("s", "api_error", "d", sdk_errors=list(range(10)))
    assert rows[0]["sdk_errors"] == [0, 1, 2, 3, 4]


# ──────────────────────────── the clean interrupt leaves evidence ───────────────────────────────

def test_clean_interrupt_is_recorded_from_the_result_frame():
    """client.interrupt() never raises, so the result frame is the ONLY place it shows up."""
    src = open("engine.py").read()
    assert '_tr in ("aborted_streaming", "aborted_tools")' in src
    assert '_record_turn_abort(session_key, _tr, "clean interrupt' in src


def test_result_event_forwards_the_cli_verdict_fields():
    src = open("engine.py").read()
    for field in ("terminal_reason", "is_error", "origin", "errors"):
        assert f'"{field}": getattr(msg, "{field}", None)' in src, field

"""
Session rewind (docs/internal/sdk-feature-audit/04-session-rewind.md).

engine.rewind_conversation() forks a session at a transcript-entry uuid using
resume_session_at + resume_drops_turn + fork_session=True (mandatory), connecting WITHOUT
ever sending a prompt — it reads the new forked session id off the init SystemMessage
(data["session_id"], verified against the bundled CLI binary) and disconnects. No turn is
ever run, so this costs zero tokens.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
from claude_agent_sdk import ProcessError, SystemMessage


def _fake_client_with_messages(messages, captured: dict):
    """A fake ClaudeSDKClient that never accepts a query() call (asserts it is never
    called — rewind must be connect-only) and replays `messages` from receive_messages()."""

    class FakeClient:
        def __init__(self, options):
            captured["opts"] = options

        async def query(self, prompt):  # pragma: no cover - must never be called
            raise AssertionError("rewind_conversation must never send a query/prompt")

        async def receive_messages(self):
            for m in messages:
                yield m

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return FakeClient


def _fake_client_raising(exc, captured: dict):
    class FakeClient:
        def __init__(self, options):
            captured["opts"] = options
            captured["stderr_cb"] = options.stderr

        async def query(self, prompt):  # pragma: no cover
            raise AssertionError("rewind_conversation must never send a query/prompt")

        async def receive_messages(self):
            if captured.get("stderr_cb"):
                captured["stderr_cb"]("Resume rejected by --resume-drops-turn: turn boundary unclear")
            raise exc
            yield  # pragma: no cover - unreachable, makes this an async generator

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return FakeClient


@pytest.mark.asyncio
async def test_rewind_uses_fork_and_drop_turn_options(tmp_path):
    """Regression guard: fork_session=True and resume_drops_turn must ALWAYS be set
    whenever resume_session_at is set — the bare (non-fork) path is UNVERIFIED per the
    audit and must never be used."""
    captured: dict = {}
    init_msg = SystemMessage(subtype="init", data={"session_id": "new-forked-sid", "model": "claude-sonnet-5"})
    with patch.object(engine, "ClaudeSDKClient", _fake_client_with_messages([init_msg], captured)):
        new_sid = await engine.rewind_conversation(
            cwd=str(tmp_path),
            resume_session_id="old-sid",
            rewind_at_uuid="uuid-kept",
            rewind_drop_turn_uuid="uuid-dropped",
        )
    assert new_sid == "new-forked-sid"
    opts = captured["opts"]
    assert opts.resume == "old-sid"
    assert opts.resume_session_at == "uuid-kept"
    assert opts.resume_drops_turn == "uuid-dropped"
    assert opts.fork_session is True


@pytest.mark.asyncio
async def test_rewind_never_sends_a_query(tmp_path):
    """Connect-only: a query() call would mean the rewind spent real tokens on a
    turn. The fake client raises AssertionError if query() is ever invoked."""
    captured: dict = {}
    init_msg = SystemMessage(subtype="init", data={"session_id": "sid2"})
    with patch.object(engine, "ClaudeSDKClient", _fake_client_with_messages([init_msg], captured)):
        await engine.rewind_conversation(
            cwd=str(tmp_path), resume_session_id="old", rewind_at_uuid="a", rewind_drop_turn_uuid="b",
        )
    # No assertion needed beyond "didn't raise" — the fake's query() raises on any call.


@pytest.mark.asyncio
async def test_rewind_ignores_non_init_messages_before_init(tmp_path):
    """A stray message type before the init handshake must not crash or be mistaken
    for the session id source."""
    captured: dict = {}
    noise = SystemMessage(subtype="mirror_error", data={})
    init_msg = SystemMessage(subtype="init", data={"session_id": "sid3"})
    with patch.object(engine, "ClaudeSDKClient", _fake_client_with_messages([noise, init_msg], captured)):
        new_sid = await engine.rewind_conversation(
            cwd=str(tmp_path), resume_session_id="old", rewind_at_uuid="a", rewind_drop_turn_uuid="b",
        )
    assert new_sid == "sid3"


@pytest.mark.asyncio
async def test_rewind_times_out_without_init_message(tmp_path):
    captured: dict = {}
    with patch.object(engine, "ClaudeSDKClient", _fake_client_with_messages([], captured)):
        with pytest.raises(RuntimeError):
            await engine.rewind_conversation(
                cwd=str(tmp_path), resume_session_id="old", rewind_at_uuid="a", rewind_drop_turn_uuid="b",
                timeout=1.0,
            )


@pytest.mark.asyncio
async def test_rewind_process_error_carries_captured_stderr(tmp_path):
    """The SDK does not pipe stderr into ProcessError by default (only a placeholder
    string) — rewind_conversation must register a stderr callback and fold the captured
    text back into the re-raised exception so _rewind_refused_hint can match it."""
    captured: dict = {}
    orig_exc = ProcessError("Command failed with exit code 1", exit_code=1, stderr="Check stderr output for details")
    with patch.object(engine, "ClaudeSDKClient", _fake_client_raising(orig_exc, captured)):
        with pytest.raises(ProcessError) as excinfo:
            await engine.rewind_conversation(
                cwd=str(tmp_path), resume_session_id="old", rewind_at_uuid="a", rewind_drop_turn_uuid="b",
            )
    assert "Resume rejected by --resume-drops-turn:" in str(excinfo.value)


def test_rewind_refused_hint_matches_sdk_refusal_text():
    exc = ProcessError("boom", exit_code=1, stderr="Resume rejected by --resume-drops-turn: reason")
    hint = engine._rewind_refused_hint(exc)
    assert hint is not None
    assert "rewind refused" in hint


def test_rewind_refused_hint_none_for_unrelated_error():
    assert engine._rewind_refused_hint(RuntimeError("some other failure")) is None


def test_rewind_conversation_exposed_via_ctx():
    ctx = engine._build_ctx()
    assert ctx["rewind_conversation"] is engine.rewind_conversation

"""
Root-fix A1: SDK_MAX_BUFFER_BYTES must reach every ClaudeAgentOptions build site.

The SDK's default per-message JSON buffer is 1 MiB; a single inline image or large
tool_result exceeds it and the reader kills the whole subprocess mid-turn
("Fatal error in message reader: JSON message exceeded maximum buffer size").
There are FOUR option-build sites: engine.run_engine (main), engine.reconcile_board
(haiku helper), webapp handoff summarizer, webapp session-title helper.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
import webapp


def _fake_client_capturing(captured: dict):
    class FakeClient:
        def __init__(self, options):
            captured["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield  # async generator

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return FakeClient


def test_default_is_32mib():
    assert engine.SDK_MAX_BUFFER_BYTES == 32 * 1024 * 1024
    assert webapp._SDK_MAX_BUFFER_BYTES == engine.SDK_MAX_BUFFER_BYTES


@pytest.mark.asyncio
async def test_run_engine_options_carry_max_buffer_size(tmp_path):
    captured: dict = {}
    with patch.object(engine, "ClaudeSDKClient", _fake_client_capturing(captured)), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="sonnet",
        ):
            pass
    opts = captured.get("opts")
    assert opts is not None
    assert opts.max_buffer_size == engine.SDK_MAX_BUFFER_BYTES


def test_buffer_overflow_hint_recognizes_sdk_error():
    exc = RuntimeError(
        "Fatal error in message reader: JSON message exceeded maximum buffer size of 1048576 bytes"
    )
    hint = engine._buffer_overflow_hint(exc)
    assert hint is not None
    assert "SDK_MAX_BUFFER_BYTES" in hint


def test_buffer_overflow_hint_ignores_unrelated_errors():
    assert engine._buffer_overflow_hint(RuntimeError("connection reset")) is None


def test_webapp_helper_sites_carry_max_buffer_size():
    """The two aliased _ClaudeAgentOptions sites (handoff summarizer, session title)
    read the same env knob. Source-level assertion: each call site passes the kwarg —
    cheaper and more direct than driving the full handoff pipeline."""
    src = (ROOT / "webapp.py").read_text()
    count = src.count("max_buffer_size=_SDK_MAX_BUFFER_BYTES")
    assert count >= 2, f"expected both webapp option sites to pass max_buffer_size, found {count}"

"""
Tests for spec-058: Ultracode mode (max effort + sub-agent fan-out directive).

run_engine(ultracode=True) must:
  - append ULTRACODE_PROMPT to system_prompt["append"], and
  - pin ClaudeAgentOptions.effort to "max", overriding any effort arg.

run_engine(ultracode=False) (the default) must leave both untouched.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine


def _fake_client_capturing(captured: dict):
    """A ClaudeSDKClient stand-in that records the ClaudeAgentOptions it is built with."""

    class FakeClient:
        def __init__(self, options):
            captured["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield  # make it an async generator

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return FakeClient


async def _drain_run_engine(tmp_path, **kwargs):
    """Run run_engine with the SDK mocked out and return the captured ClaudeAgentOptions."""
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
            **kwargs,
        ):
            pass
    return captured.get("opts")


def _append_text(opts) -> str:
    sp = opts.system_prompt
    return sp.get("append", "") if isinstance(sp, dict) else str(sp)


def test_ultracode_prompt_constant_present():
    """ULTRACODE_PROMPT module constant exists and reads like an orchestration directive."""
    assert hasattr(engine, "ULTRACODE_PROMPT")
    low = engine.ULTRACODE_PROMPT.lower()
    assert "ultracode" in low
    assert "sub-agent" in low or "workflow" in low
    assert "verify" in low


@pytest.mark.asyncio
async def test_ultracode_on_injects_directive_and_pins_max(tmp_path):
    """ultracode=True → ULTRACODE_PROMPT appended AND effort forced to 'max'."""
    opts = await _drain_run_engine(tmp_path, ultracode=True)
    assert opts is not None
    assert engine.ULTRACODE_PROMPT in _append_text(opts), (
        f"ULTRACODE_PROMPT not found in system_prompt.append: {_append_text(opts)!r}"
    )
    assert opts.effort == "max", f"effort must be pinned to max, got {opts.effort!r}"


@pytest.mark.asyncio
async def test_ultracode_overrides_explicit_effort(tmp_path):
    """ultracode=True must override an explicit effort arg (e.g. 'low' → still 'max')."""
    opts = await _drain_run_engine(tmp_path, ultracode=True, effort="low")
    assert opts is not None
    assert opts.effort == "max", f"ultracode must override effort='low', got {opts.effort!r}"


@pytest.mark.asyncio
async def test_ultracode_off_is_noop(tmp_path):
    """ultracode=False (default) → no directive, and effort honours the passed value."""
    opts = await _drain_run_engine(tmp_path, ultracode=False, effort="high")
    assert opts is not None
    assert engine.ULTRACODE_PROMPT not in _append_text(opts), (
        f"ULTRACODE_PROMPT must NOT be injected when ultracode=False: {_append_text(opts)!r}"
    )
    assert opts.effort == "high", f"effort should be untouched, got {opts.effort!r}"


@pytest.mark.asyncio
async def test_ultracode_default_is_off(tmp_path):
    """Omitting ultracode entirely behaves like ultracode=False."""
    opts = await _drain_run_engine(tmp_path, effort="medium")
    assert opts is not None
    assert engine.ULTRACODE_PROMPT not in _append_text(opts)
    assert opts.effort == "medium"

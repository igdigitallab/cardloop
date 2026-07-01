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
    model = kwargs.pop("model", "sonnet")
    with patch.object(engine, "ClaudeSDKClient", _fake_client_capturing(captured)), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model=model,
            **kwargs,
        ):
            pass
    return captured.get("opts")


def _append_text(opts) -> str:
    sp = opts.system_prompt
    return sp.get("append", "") if isinstance(sp, dict) else str(sp)


def test_ultracode_prompt_constant_present():
    """ULTRACODE_PROMPT is a hard orchestration CONTRACT (not a soft preference): it names the
    Sonnet worker slots, states an explicit delegate/stay-solo posture, and asks for verification."""
    assert hasattr(engine, "ULTRACODE_PROMPT")
    low = engine.ULTRACODE_PROMPT.lower()
    assert "ultracode" in low
    assert "sub-agent" in low or "workflow" in low
    assert "verify" in low
    # spec-058 hardening (2026-07): the directive must be a real delegation contract, because
    # Opus 4.8 spawns few sub-agents by default and follows literal instructions — a soft invite
    # left Opus doing everything itself.
    assert "orchestrator" in low
    assert "executor" in low and "researcher" in low   # names the actual Sonnet worker slots
    assert "delegate" in low
    assert "task tool" in low or "task call" in low     # the delegation primitive


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


@pytest.mark.asyncio
async def test_ultracode_injects_for_opus_too(tmp_path):
    """ultracode is model-agnostic: an opus project also gets the orchestration contract (this is
    the exact case that was broken — Opus ran ultracode but did everything itself)."""
    opts = await _drain_run_engine(tmp_path, model="opus", ultracode=True)
    assert engine.ULTRACODE_PROMPT in _append_text(opts)
    assert opts.effort == "max"


@pytest.mark.asyncio
async def test_ultracode_on_fable_does_not_also_inject_conductor(tmp_path):
    """fable + ultracode → ULTRACODE_PROMPT is the sole orchestration contract; CONDUCTOR_PROMPT is
    NOT also injected (its ≤3–5 concurrent cap would fight ultracode's parallel fan-out)."""
    opts = await _drain_run_engine(tmp_path, model="fable", ultracode=True)
    txt = _append_text(opts)
    assert engine.ULTRACODE_PROMPT in txt
    assert engine.CONDUCTOR_PROMPT not in txt, (
        f"fable+ultracode must not also inject the conductor cap: {txt!r}"
    )


@pytest.mark.asyncio
async def test_conductor_still_injected_for_fable_without_ultracode(tmp_path):
    """Regression guard: plain fable (no ultracode) still gets CONDUCTOR_PROMPT and NOT ULTRACODE."""
    opts = await _drain_run_engine(tmp_path, model="fable")
    txt = _append_text(opts)
    assert engine.CONDUCTOR_PROMPT in txt
    assert engine.ULTRACODE_PROMPT not in txt

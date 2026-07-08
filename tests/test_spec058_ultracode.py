"""
Tests for spec-058 v2: Ultracode mode (native Claude Code ultracode via --settings).

run_engine(ultracode=True) must:
  - pass settings=ULTRACODE_SETTINGS ({"ultracode": true}) so the CLI activates its NATIVE
    ultracode machinery (Workflow contract + standing opt-in reminders + internal xhigh pin),
  - pass NO --effort (opts.effort is None) — a CLI effort flag would override the native pin,
  - append the thin ULTRACODE_PROMPT complement to system_prompt["append"].

run_engine(ultracode=False) (the default) must leave all three untouched.
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


def test_ultracode_settings_constant_is_native_switch():
    """ULTRACODE_SETTINGS is the inline JSON the CLI --settings flag accepts; it must flip the
    native `ultracode` settings key (the same switch as the interactive /effort ultracode).
    Guard: do NOT 'simplify' this to an effort value — headless mode rejects --effort ultracode."""
    import json
    assert hasattr(engine, "ULTRACODE_SETTINGS")
    parsed = json.loads(engine.ULTRACODE_SETTINGS)
    assert parsed == {"ultracode": True}


def test_ultracode_prompt_is_thin_complement():
    """ULTRACODE_PROMPT complements (never restates) the native contract: it names the local
    agent roster incl. the skeptic verifier, pushes Workflow-first orchestration with adversarial
    verification, and pins the cockpit reporting rule (full synthesis in the final message)."""
    assert hasattr(engine, "ULTRACODE_PROMPT")
    low = engine.ULTRACODE_PROMPT.lower()
    assert "ultracode" in low
    assert "workflow" in low
    assert "refute" in low or "verify" in low
    assert "executor" in low and "researcher" in low and "skeptic" in low
    assert "synthesis" in low
    # Must NOT reintroduce the old prompt-side contract bits that fight the native one.
    assert "~6 at once" not in low
    assert "you are in ultracode mode" not in low


def test_skeptic_agent_in_default_roster():
    """spec-058 v2 adds a read-only adversarial `skeptic` agent for workflow verify stages."""
    assert "skeptic" in engine.DEFAULT_AGENTS
    sk = engine.DEFAULT_AGENTS["skeptic"]
    assert "refute" in sk.prompt.lower()
    assert "Write" in (sk.disallowedTools or [])


@pytest.mark.asyncio
async def test_ultracode_on_activates_native_switch(tmp_path):
    """ultracode=True → settings=ULTRACODE_SETTINGS, NO effort flag, complement appended."""
    opts = await _drain_run_engine(tmp_path, ultracode=True)
    assert opts is not None
    assert opts.settings == engine.ULTRACODE_SETTINGS, (
        f"native ultracode settings not passed: {opts.settings!r}"
    )
    assert opts.effort is None, (
        f"ultracode must pass NO --effort (native pin wins), got {opts.effort!r}"
    )
    assert engine.ULTRACODE_PROMPT in _append_text(opts), (
        f"ULTRACODE_PROMPT not found in system_prompt.append: {_append_text(opts)!r}"
    )


@pytest.mark.asyncio
async def test_ultracode_suppresses_explicit_effort(tmp_path):
    """ultracode=True must suppress an explicit effort arg (e.g. 'low' → no --effort at all),
    otherwise the CLI-side effort flag would override the native xhigh pin."""
    opts = await _drain_run_engine(tmp_path, ultracode=True, effort="low")
    assert opts is not None
    assert opts.effort is None, f"effort arg must be suppressed under ultracode, got {opts.effort!r}"
    assert opts.settings == engine.ULTRACODE_SETTINGS


@pytest.mark.asyncio
async def test_ultracode_off_is_noop(tmp_path):
    """ultracode=False (default) → no settings payload, no directive, effort honoured."""
    opts = await _drain_run_engine(tmp_path, ultracode=False, effort="high")
    assert opts is not None
    assert opts.settings is None, f"settings must stay None when ultracode=False: {opts.settings!r}"
    assert engine.ULTRACODE_PROMPT not in _append_text(opts), (
        f"ULTRACODE_PROMPT must NOT be injected when ultracode=False: {_append_text(opts)!r}"
    )
    assert opts.effort == "high", f"effort should be untouched, got {opts.effort!r}"


@pytest.mark.asyncio
async def test_ultracode_default_is_off(tmp_path):
    """Omitting ultracode entirely behaves like ultracode=False."""
    opts = await _drain_run_engine(tmp_path, effort="medium")
    assert opts is not None
    assert opts.settings is None
    assert engine.ULTRACODE_PROMPT not in _append_text(opts)
    assert opts.effort == "medium"


@pytest.mark.asyncio
async def test_ultracode_activates_for_opus_too(tmp_path):
    """ultracode is model-agnostic: an opus project gets the native switch too (verified live:
    the Workflow tool is served to claude-opus-4-8 and executes under the settings flag)."""
    opts = await _drain_run_engine(tmp_path, model="opus", ultracode=True)
    assert opts.settings == engine.ULTRACODE_SETTINGS
    assert opts.effort is None
    assert engine.ULTRACODE_PROMPT in _append_text(opts)


@pytest.mark.asyncio
async def test_ultracode_on_fable_does_not_also_inject_conductor(tmp_path):
    """fable + ultracode → the native contract is the sole orchestration contract; CONDUCTOR_PROMPT
    is NOT also injected (its ≤3–5 concurrent cap would fight ultracode's workflow fan-out)."""
    opts = await _drain_run_engine(tmp_path, model="fable", ultracode=True)
    txt = _append_text(opts)
    assert engine.ULTRACODE_PROMPT in txt
    assert engine.CONDUCTOR_PROMPT not in txt, (
        f"fable+ultracode must not also inject the conductor cap: {txt!r}"
    )


@pytest.mark.asyncio
async def test_conductor_still_injected_for_fable_without_ultracode(tmp_path):
    """Regression guard: plain fable (no ultracode) still gets CONDUCTOR_PROMPT, no ultracode bits."""
    opts = await _drain_run_engine(tmp_path, model="fable")
    txt = _append_text(opts)
    assert engine.CONDUCTOR_PROMPT in txt
    assert engine.ULTRACODE_PROMPT not in txt
    assert opts.settings is None

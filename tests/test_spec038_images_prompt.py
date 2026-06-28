"""
Tests for spec-038 wiring: the inline-image instruction (IMAGES_PROMPT).

The cockpit-img helper, /media route and lightbox shipped long ago, but the agent was
never told the mechanism exists — so it fell back to Telegram or pasted a raw path/link
(neither renders). run_engine must now append IMAGES_PROMPT to system_prompt["append"]
whenever the media plumbing is live (env has COPS_MEDIA_DIR — set for cockpit chat + card
runs), and must NOT append it otherwise.
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


def test_images_prompt_constant_present():
    """IMAGES_PROMPT exists and names the helper + the attached-file fallback."""
    assert hasattr(engine, "IMAGES_PROMPT")
    low = engine.IMAGES_PROMPT.lower()
    assert "cockpit-img" in low
    assert "attached file" in low
    # Must steer away from the wrong channels the agent otherwise guesses.
    assert "telegram" in low


@pytest.mark.asyncio
async def test_images_prompt_injected_when_media_env_present(tmp_path):
    """COPS_MEDIA_DIR in env → IMAGES_PROMPT appended to system_prompt.append."""
    opts = await _drain_run_engine(tmp_path, env={"COPS_MEDIA_DIR": str(tmp_path)})
    assert opts is not None
    assert engine.IMAGES_PROMPT in _append_text(opts), (
        f"IMAGES_PROMPT not appended despite COPS_MEDIA_DIR: {_append_text(opts)!r}"
    )


@pytest.mark.asyncio
async def test_images_prompt_absent_without_media_env(tmp_path):
    """No COPS_MEDIA_DIR → no hint (don't advertise a mechanism that isn't wired)."""
    opts = await _drain_run_engine(tmp_path, env={})
    assert opts is not None
    assert engine.IMAGES_PROMPT not in _append_text(opts), (
        f"IMAGES_PROMPT must NOT appear without COPS_MEDIA_DIR: {_append_text(opts)!r}"
    )

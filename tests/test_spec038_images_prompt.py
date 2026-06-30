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


# ─────────────────────── card 2efd6a: arbitrary-file drop (FILES_PROMPT) ──────


def test_files_prompt_constant_present():
    """FILES_PROMPT exists and names the helper + steers away from Telegram."""
    assert hasattr(engine, "FILES_PROMPT")
    low = engine.FILES_PROMPT.lower()
    assert "cockpit-file" in low
    assert "attached file" in low
    assert "download" in low


@pytest.mark.asyncio
async def test_files_prompt_injected_when_media_env_present(tmp_path):
    """COPS_MEDIA_DIR in env → FILES_PROMPT appended alongside IMAGES_PROMPT."""
    opts = await _drain_run_engine(tmp_path, env={"COPS_MEDIA_DIR": str(tmp_path)})
    assert opts is not None
    assert engine.FILES_PROMPT in _append_text(opts), (
        f"FILES_PROMPT not appended despite COPS_MEDIA_DIR: {_append_text(opts)!r}"
    )


@pytest.mark.asyncio
async def test_files_prompt_absent_without_media_env(tmp_path):
    """No COPS_MEDIA_DIR → no file-drop hint either."""
    opts = await _drain_run_engine(tmp_path, env={})
    assert opts is not None
    assert engine.FILES_PROMPT not in _append_text(opts)


@pytest.mark.asyncio
async def test_tools_dir_prepended_to_path_when_media_env(tmp_path):
    """The repo tools/ dir is prepended to the agent PATH when media is live, so cockpit-img /
    cockpit-file resolve without a manual install (and shadow any stale hand-copied helper)."""
    opts = await _drain_run_engine(tmp_path, env={"COPS_MEDIA_DIR": str(tmp_path)})
    assert opts is not None
    tools_dir = str((ROOT / "tools").resolve())
    path_val = (opts.env or {}).get("PATH", "")
    assert path_val.split(":")[0] == tools_dir, (
        f"tools/ dir must be first on PATH, got {path_val!r}"
    )


@pytest.mark.asyncio
async def test_path_not_touched_without_media_env(tmp_path):
    """No COPS_MEDIA_DIR → run_engine must not inject a PATH override."""
    opts = await _drain_run_engine(tmp_path, env={})
    assert opts is not None
    assert "PATH" not in (opts.env or {})

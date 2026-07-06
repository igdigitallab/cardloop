"""
Unit tests for e2e_fake_engine.py (spec-072).

Fast, no browser, no subprocess — drives the async generator directly and checks
its event contract matches engine.run_engine's real schema (see engine.py's
"ENGINE (async event generator)" docstring): {type: text_delta|text|tool|result|error}.

Part of the default suite (not marked e2e) — this module ships production code
(wired into bot.py._build_ctx when E2E_FAKE_ENGINE=1) and deserves regular coverage,
independent of the (opt-in, browser-driven) tests/e2e/ Playwright suite.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import e2e_fake_engine  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch, tmp_path):
    """Shrinks the module's per-chunk/gap delays so this file doesn't slow down the
    default suite — the actual timing values aren't what these tests verify.

    Also sandboxes $HOME: run_engine's transcript bookkeeping (_append_transcript)
    writes to Path.home()/.claude/projects/... (the same place the real SDK CLI
    writes conversation history — see the module docstring). Path.home() reads the
    HOME env var, so without this override these tests would write fake transcript
    files into the OPERATOR'S REAL ~/.claude/projects/ on whatever machine runs the
    suite — exactly the kind of prod-data leak this project's tests must never do.
    """
    monkeypatch.setattr(e2e_fake_engine, "_DELTA_GAP_SEC", 0.01)
    monkeypatch.setattr(e2e_fake_engine, "_SLOW_GAP_SEC", 0.05)
    monkeypatch.setenv("HOME", str(tmp_path))


async def _collect(prompt: str, **kwargs):
    events = []
    kwargs.setdefault("cwd", "/tmp")
    async for event in e2e_fake_engine.run_engine(
        project_name="p", prompt=prompt, session_key="k", **kwargs
    ):
        events.append(event)
    return events


async def test_e2e_text_scenario():
    events = await _collect("e2e:text")
    types = [e["type"] for e in events]
    assert types == ["text_delta", "text_delta", "text_delta", "text", "result"]
    assert events[-2]["text"] == "Hello, this is a scripted e2e reply."
    assert events[-1]["type"] == "result"
    assert events[-1]["session_id"]


async def test_e2e_tool_scenario():
    events = await _collect("e2e:tool")
    types = [e["type"] for e in events]
    assert types == ["tool", "text_delta", "text", "result"]
    assert events[0]["name"] == "Bash"
    assert events[0]["input"]["command"] == "echo e2e"


async def test_e2e_slow_scenario_has_a_gap():
    import time
    t0 = time.monotonic()
    events = await _collect("e2e:slow")
    elapsed = time.monotonic() - t0
    types = [e["type"] for e in events]
    assert types == ["text_delta", "text_delta", "text", "result"]
    assert elapsed >= e2e_fake_engine._SLOW_GAP_SEC
    assert events[2]["text"] == "starting slow scenario... done after the long silence."


async def test_e2e_error_scenario():
    events = await _collect("e2e:error")
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert isinstance(events[0]["exc"], Exception)


async def test_e2e_default_scenario():
    events = await _collect("anything unscripted")
    types = [e["type"] for e in events]
    assert types == ["text_delta", "text", "result"]
    assert "anything unscripted" in events[1]["text"]


async def test_e2e_resume_session_id_is_reused():
    """Mirrors the real engine's resume semantics: passing resume_session_id echoes
    it back verbatim (the cockpit uses this to keep sid stable across a chat's turns)."""
    events = await _collect("e2e:text", resume_session_id="existing-sid-123")
    result = events[-1]
    assert result["session_id"] == "existing-sid-123"


async def test_e2e_fresh_session_id_when_no_resume():
    events_a = await _collect("e2e:text")
    events_b = await _collect("e2e:text")
    assert events_a[-1]["session_id"] != events_b[-1]["session_id"]


async def test_e2e_writes_a_real_shaped_transcript(tmp_path):
    """webapp.py:api_project_session_history reads ~/.claude/projects/<slug>/<sid>.jsonl
    directly (the real SDK CLI's transcript store) — a post-turn hydrate (queue drain,
    poll, reload) treats it as authoritative. Without this file the cockpit would wipe
    the visible chat to empty once the turn is no longer "live" in the SSE buffer."""
    project_cwd = str(tmp_path / "project")
    events = await _collect("e2e:tool", cwd=project_cwd)
    sid = events[-1]["session_id"]

    transcript = e2e_fake_engine._transcript_path(project_cwd, sid)
    assert transcript.is_file()
    lines = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    assert [l["type"] for l in lines] == ["user", "assistant"]
    assert lines[0]["message"]["content"] == "e2e:tool"
    blocks = lines[1]["message"]["content"]
    assert {"type": "tool_use", "name": "Bash"}.items() <= blocks[0].items()
    assert blocks[-1] == {"type": "text", "text": "e2e tool scenario done for k"}


async def test_e2e_accepts_full_run_engine_kwargs():
    """Same call shape as a real cockpit call site (webapp.py) — must not TypeError
    on any of the real run_engine's keyword arguments."""
    events = await _collect(
        "e2e:text",
        model="sonnet",
        system_prompt={"type": "preset", "preset": "claude_code"},
        env={"FOO": "bar"},
        resume_session_id=None,
        agents=None,
        skip_conductor_prompt=True,
        ctx={},
        ephemeral=False,
        output_format=None,
        effort="medium",
        ultracode=False,
        entrypoint="chat",
        disallowed_tools_extra=None,
    )
    assert events[-1]["type"] == "result"

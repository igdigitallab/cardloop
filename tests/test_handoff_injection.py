"""A rotation handoff is worthless if the next turn never sees it.

`api_project_chat` was the only path that injected the pending summary. A turn started from
the queue drain — a message typed while the bot was busy, or an auto-continue wake — opened a
fresh post-rotation session with no memory of the one it replaced. Worse, that turn's session
id then landed in ctx["sessions"], so every later chat turn resumed it: `resume_sid is None`
never came round again and the summary sat in pending_handoff forever.

Both paths now go through _inject_pending_handoff.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


@pytest.fixture
def ctx():
    saved = []
    return {
        "pending_handoff": {"proj:1": "## Where we stopped\nMid-refactor of engine.py."},
        "save_handoff": lambda: saved.append(1),
        "_saved": saved,
    }


def test_injects_into_a_fresh_session(ctx):
    out, injected = _webapp._inject_pending_handoff(ctx, "proj:1", "carry on", resume_sid=None)

    assert injected is True
    assert "<prior-session-summary>" in out
    assert "Mid-refactor of engine.py." in out
    assert out.endswith("carry on")


def test_summary_is_popped_and_persisted(ctx):
    _webapp._inject_pending_handoff(ctx, "proj:1", "go", resume_sid=None)

    assert "proj:1" not in ctx["pending_handoff"], "summary must be consumed"
    assert ctx["_saved"], "the pop must be persisted or a restart re-injects it"


def test_never_injects_twice(ctx):
    first, injected_1 = _webapp._inject_pending_handoff(ctx, "proj:1", "go", resume_sid=None)
    second, injected_2 = _webapp._inject_pending_handoff(ctx, "proj:1", "go again", resume_sid=None)

    assert injected_1 is True and injected_2 is False
    assert "<prior-session-summary>" in first
    assert second == "go again"


def test_resumed_session_is_left_alone(ctx):
    """A live session already holds the context; injecting would duplicate it."""
    out, injected = _webapp._inject_pending_handoff(ctx, "proj:1", "go", resume_sid="sess-abc")

    assert injected is False
    assert out == "go"
    assert "proj:1" in ctx["pending_handoff"], "summary must survive for the fresh session"


def test_no_pending_summary_is_a_noop(ctx):
    out, injected = _webapp._inject_pending_handoff(ctx, "other:9", "go", resume_sid=None)

    assert injected is False
    assert out == "go"


def test_missing_pending_handoff_key_does_not_raise():
    out, injected = _webapp._inject_pending_handoff({}, "proj:1", "go", resume_sid=None)

    assert injected is False
    assert out == "go"


def test_queue_drain_path_injects_the_handoff():
    """Regression: _chat_queue_execute must feed run_engine the handoff-wrapped prompt.

    Guards against a refactor silently reverting to `prompt=prompt` — the exact bug this fixes.
    """
    import inspect

    src = inspect.getsource(_webapp._chat_queue_execute)
    assert "_inject_pending_handoff" in src, "queued turns must inject the pending handoff"
    assert "prompt=effective_prompt" in src, "run_engine must receive the wrapped prompt"


def test_card_path_does_not_consume_the_handoff():
    """Cards run ephemeral in their own session; consuming the summary would starve the chat."""
    import inspect

    src = inspect.getsource(_webapp._run_card)
    assert "_inject_pending_handoff" not in src

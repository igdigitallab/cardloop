"""
Regression tests for "the project stopped answering and its chats look unbound".

A stored session_id whose transcript is gone (CLI retention cleanup, a wiped
~/.claude/projects/<slug>, a restored backup) makes the bundled CLI exit 1 on --resume:

    No conversation found with session ID: <sid>
    [live-client] setup failed ... falling back to fresh client

The fallback rebuilt the SAME ClaudeAgentOptions — resume id included — so it exited 1
again and the turn died as `sdk_error`. Result: every send in that chat vanished with no
error surfaced, permanently, until the id was cleared by hand. Observed 2026-08-08 on the
teleprompter project.

The engine now verifies the transcript before resuming and starts fresh when it is missing.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine as _engine
import webapp as _webapp


def test_transcript_exists_agrees_with_webapp_slug(tmp_path, monkeypatch):
    """The engine's slug rule must match webapp._sdk_sessions_dir — they encode the same folder."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cwd = "/home/user/my_vpn.app"
    sid = "11111111-2222-3333-4444-555555555555"

    d = _webapp._sdk_sessions_dir(cwd)
    assert d.name == "-home-user-my-vpn-app", d.name

    target = tmp_path / ".claude" / "projects" / d.name
    target.mkdir(parents=True)
    assert _engine._transcript_exists(cwd, sid) is False
    (target / f"{sid}.jsonl").write_text("{}\n")
    assert _engine._transcript_exists(cwd, sid) is True


def test_transcript_exists_false_when_folder_gone(tmp_path, monkeypatch):
    """A wiped project folder reads as missing, not as an error."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert _engine._transcript_exists("/home/user/teleprompter", "dead-sid") is False


def test_transcript_exists_no_sid_or_cwd():
    assert _engine._transcript_exists("", "sid") is False
    assert _engine._transcript_exists("/home/user/p", "") is False


def test_forget_dead_session_clears_and_persists():
    saved = []
    ctx = {
        "sessions": {"proj": "dead-sid", "other": "live-sid"},
        "save_sessions": lambda: saved.append(True),
    }
    _engine._forget_dead_session(ctx, "proj", "dead-sid")
    assert "proj" not in ctx["sessions"]
    assert ctx["sessions"]["other"] == "live-sid"  # untouched
    assert saved == [True]


def test_forget_dead_session_leaves_a_newer_id_alone():
    """If another turn already stored a fresh id, do not clobber it."""
    ctx = {"sessions": {"proj": "fresh-sid"}, "save_sessions": lambda: None}
    _engine._forget_dead_session(ctx, "proj", "dead-sid")
    assert ctx["sessions"]["proj"] == "fresh-sid"


def test_forget_dead_session_survives_missing_ctx():
    _engine._forget_dead_session(None, "proj", "dead-sid")  # must not raise
    _engine._forget_dead_session({}, "proj", "dead-sid")

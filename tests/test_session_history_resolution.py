"""
Regression tests for "chat history disappears" — two independent root causes:

1. `_sdk_sessions_dir` encoded the cwd with '/'-only → '-', but the Claude SDK replaces
   EVERY non-alphanumeric char (so '_' and '.' too). Any project whose path contained '_'
   (e.g. /home/igor/line_vpn_bot) resolved to a non-existent transcript folder → empty history.

2. session-history resolved the session id from the legacy ctx["sessions"] mirror only. That
   mirror is refreshed on a chat run but not persisted immediately, so a restart between runs
   left it stale while chats.json (spec-037 source of truth) stayed correct → empty history.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


def test_sdk_sessions_dir_encodes_all_nonalnum():
    """Underscores and dots in the cwd become dashes, matching the SDK's folder naming."""
    d = _webapp._sdk_sessions_dir("/home/user/line_vpn_bot")
    assert d.name == "-home-user-line-vpn-bot", d.name
    # Dots too (e.g. a domain-named project dir).
    d2 = _webapp._sdk_sessions_dir("/home/user/foo.bar_baz")
    assert d2.name == "-home-user-foo-bar-baz", d2.name
    # Plain paths (no '_' or '.') are unchanged — no regression for existing projects.
    d3 = _webapp._sdk_sessions_dir("/home/user/claude-ops-bot")
    assert d3.name == "-home-user-claude-ops-bot", d3.name


def test_active_chat_session_id_prefers_chats_json(tmp_path):
    """The resolver reads the active chat's session_id from chats.json (lock-free read)."""
    (tmp_path / "chats.json").write_text(json.dumps({
        "marketing": {
            "active": "433e0f",
            "chats": [{"id": "433e0f", "name": "Main", "session_id": "sid-abc123"}],
        }
    }), encoding="utf-8")
    ctx = {"DATA": tmp_path}
    assert _webapp._active_chat_session_id(ctx, "marketing") == "sid-abc123"


def test_active_chat_session_id_none_when_absent(tmp_path):
    """No chats.json entry (or no session_id) → None, so the caller falls back to legacy."""
    (tmp_path / "chats.json").write_text(json.dumps({
        "marketing": {"active": "433e0f", "chats": [{"id": "433e0f", "session_id": None}]}
    }), encoding="utf-8")
    ctx = {"DATA": tmp_path}
    assert _webapp._active_chat_session_id(ctx, "marketing") is None
    assert _webapp._active_chat_session_id(ctx, "unknown-project") is None

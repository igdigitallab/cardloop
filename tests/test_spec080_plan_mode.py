"""
spec-080: cockpit plan mode — engine-side option wiring + the approval gate callback.

The OFF path must stay byte-identical to pre-feature behavior (same assertion style the
ultracode tests use); the ON path connects in the CLI's native plan mode with the gate
callback wired and the custom roster dropped.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny


def _fake_client_capturing(captured: dict):
    class FakeClient:
        def __init__(self, options):
            captured["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return FakeClient


async def _drain(tmp_path, **kwargs):
    captured: dict = {}
    with patch.object(engine, "ClaudeSDKClient", _fake_client_capturing(captured)), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test", cwd=str(tmp_path), prompt="hi",
            session_key="c:t", model="sonnet", **kwargs,
        ):
            pass
    return captured.get("opts")


async def test_off_path_unchanged(tmp_path):
    opts = await _drain(tmp_path)
    assert opts.permission_mode == "bypassPermissions"
    assert opts.can_use_tool is None
    assert opts.agents == engine.DEFAULT_AGENTS
    assert "EnterPlanMode" in opts.disallowed_tools


async def test_plan_mode_options(tmp_path):
    opts = await _drain(tmp_path, plan_mode=True, chat_id="chat42")
    assert opts.permission_mode == "plan"
    assert callable(opts.can_use_tool)
    assert opts.agents is None            # custom roster dropped: built-in Explore/Plan drive
    assert "EnterPlanMode" in opts.disallowed_tools
    # Dispatcher state was primed for the gate
    assert engine._plan_turn_chat.get("c:t") == "chat42"


async def test_plan_suppresses_ultracode(tmp_path):
    """C4: plan wins — no ultracode settings flag, no ULTRACODE_PROMPT in the append."""
    opts = await _drain(tmp_path, plan_mode=True, ultracode=True)
    assert opts.permission_mode == "plan"
    assert opts.settings is None
    sp = opts.system_prompt
    append = sp.get("append", "") if isinstance(sp, dict) else ""
    assert "ultracode" not in append.lower()


# ─────────────────────────── the gate callback itself ─────────────────────────

@pytest.fixture
def gate_env(monkeypatch):
    monkeypatch.setattr(engine, "_plan_turn_chat", {"sk": "chatX"})
    monkeypatch.setattr(engine, "_plan_gate_approved", {})
    monkeypatch.setattr(engine, "_plan_write_paths", {})
    calls: dict = {"resolved": []}

    def _fake_create(ctx, session_key, chat_id, plan_text, plan_file_path=None):
        calls["created"] = {"session_key": session_key, "chat_id": chat_id,
                            "plan_text": plan_text, "plan_file_path": plan_file_path}
        fut = asyncio.get_event_loop().create_future()
        calls["future"] = fut
        return "abcd1234", fut

    def _fake_resolve(ctx, plan_id, decision, feedback=""):
        calls["resolved"].append((plan_id, decision, feedback))
        return True

    monkeypatch.setattr(engine, "_create_pending_plan_cb", _fake_create)
    monkeypatch.setattr(engine, "_resolve_plan_cb", _fake_resolve)
    return calls


async def test_gate_allows_non_exitplanmode(gate_env):
    cb = engine._make_plan_gate_cb("sk", None)
    res = await cb("Read", {"file_path": "x"}, None)
    assert isinstance(res, PermissionResultAllow)
    assert "created" not in gate_env


async def test_gate_approve_then_rubber_stamps(gate_env):
    """Approve → Allow; afterwards EVERY tool is stamped without a new pending plan
    (post-approval execution runs through the callback — no mode flip exists)."""
    cb = engine._make_plan_gate_cb("sk", None)
    task = asyncio.create_task(cb("ExitPlanMode", {"plan": "## P", "planFilePath": "/tmp/p.md"}, None))
    await asyncio.sleep(0)
    assert gate_env["created"]["chat_id"] == "chatX"      # dispatcher, not capture
    assert gate_env["created"]["plan_file_path"] == "/tmp/p.md"
    gate_env["future"].set_result({"decision": "approve", "feedback": ""})
    res = await task
    assert isinstance(res, PermissionResultAllow)
    gate_env.pop("created")
    res2 = await cb("Bash", {"command": "echo hi"}, None)  # post-approve rubber stamp
    assert isinstance(res2, PermissionResultAllow)
    assert "created" not in gate_env


async def test_gate_reject_carries_feedback(gate_env):
    cb = engine._make_plan_gate_cb("sk", None)
    task = asyncio.create_task(cb("ExitPlanMode", {"plan": "## P"}, None))
    await asyncio.sleep(0)
    gate_env["future"].set_result({"decision": "reject", "feedback": "add tests section"})
    res = await task
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "add tests section"
    assert engine._plan_gate_approved.get("sk") is not True


async def test_gate_cancel_resolves_cancelled(gate_env):
    cb = engine._make_plan_gate_cb("sk", None)
    task = asyncio.create_task(cb("ExitPlanMode", {"plan": "## P"}, None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate_env["resolved"] and gate_env["resolved"][0][:2] == ("abcd1234", "cancelled")


async def test_gate_no_webapp_defaults_to_allow(monkeypatch):
    monkeypatch.setattr(engine, "_create_pending_plan_cb", None)
    monkeypatch.setattr(engine, "_plan_gate_approved", {})
    cb = engine._make_plan_gate_cb("sk2", None)
    res = await cb("ExitPlanMode", {"plan": "p"}, None)
    assert isinstance(res, PermissionResultAllow)


# ─────────────────────────── C1 safeguard ─────────────────────────────────────

def test_fingerprint_guard_detects_deferred_reuse(monkeypatch):
    """A live entry whose fingerprint does not match the plan-opts fingerprint means the
    pinned client was reused ungated — the guard must flag it."""
    class Entry:
        fingerprint = "stale-bypass-fp"

    monkeypatch.setattr(engine, "_live_clients", {"sk": Entry()})
    monkeypatch.setattr(engine, "_compute_fingerprint",
                        lambda opts, **kw: "fresh-plan-fp")
    assert engine._plan_client_fingerprint_ok(None, "sk", object(), "", "", "") is False


def test_fingerprint_guard_ok_when_no_entry(monkeypatch):
    monkeypatch.setattr(engine, "_live_clients", {})
    assert engine._plan_client_fingerprint_ok(None, "sk", object(), "", "", "") is True

"""
spec-082 A: ask mode — per-tool approval from the phone.

Three layers, one file:
  1. engine option wiring (the "default", never bypassPermissions" invariant),
  2. the can_use_tool gate itself (allow / deny+feedback / always-allow / timeout / cancel),
  3. the webapp decision store + the /decision endpoints (auth, validation, idempotency).
"""
import asyncio
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
import webapp as _webapp
from webapp import _derive_token
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import (
    CanUseToolShadowedWarning,
    _warn_if_can_use_tool_shadowed,
)


# ─────────────────────────── 1. engine option wiring ──────────────────────────

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


async def test_off_path_still_bypasses(tmp_path):
    opts = await _drain(tmp_path)
    assert opts.permission_mode == "bypassPermissions"
    assert opts.can_use_tool is None
    assert engine._ask_turn_chat.get("c:t") is None


async def test_ask_mode_connects_in_default_with_gate(tmp_path):
    """THE invariant: bypassPermissions SHADOWS can_use_tool, so an ask turn must connect
    with permission_mode='default' or every tool runs ungated with no error at all."""
    opts = await _drain(tmp_path, ask_mode=True, chat_id="chat42")
    assert opts.permission_mode == "default"
    assert opts.can_use_tool is not None and callable(opts.can_use_tool)
    assert engine._ask_turn_chat.get("c:t") == "chat42"


async def test_ask_mode_options_are_not_shadowed(tmp_path):
    """The SDK's own shadow detector must stay silent for the ask-mode options we build —
    this is the machine-checkable half of "the gate is actually reached"."""
    opts = await _drain(tmp_path, ask_mode=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error", CanUseToolShadowedWarning)
        _warn_if_can_use_tool_shadowed(opts)  # must not raise

    # Control: the same options under bypassPermissions DO trip the detector.
    opts.permission_mode = "bypassPermissions"
    with warnings.catch_warnings():
        warnings.simplefilter("error", CanUseToolShadowedWarning)
        with pytest.raises(CanUseToolShadowedWarning):
            _warn_if_can_use_tool_shadowed(opts)


async def test_plan_wins_over_ask(tmp_path):
    opts = await _drain(tmp_path, plan_mode=True, ask_mode=True)
    assert opts.permission_mode == "plan"
    assert engine._ask_turn_chat.get("c:t") is None


# ─────────────────────────── 2. the gate callback ─────────────────────────────

@pytest.fixture
def ask_env(monkeypatch):
    monkeypatch.setattr(engine, "_ask_turn_chat", {"sk": "chatX"})
    calls: dict = {"resolved": []}

    def _fake_create(ctx, session_key, chat_id, tool_name, preview):
        if tool_name == "AlwaysOk":
            return None, None          # on the project's always-allow list
        calls["created"] = {"session_key": session_key, "chat_id": chat_id,
                            "tool_name": tool_name, "preview": preview}
        fut = asyncio.get_event_loop().create_future()
        calls["future"] = fut
        return "beef1234", fut

    def _fake_resolve(ctx, decision_id, decision, feedback=""):
        calls["resolved"].append((decision_id, decision, feedback))
        return True

    monkeypatch.setattr(engine, "_create_pending_tool_cb", _fake_create)
    monkeypatch.setattr(engine, "_resolve_plan_cb", _fake_resolve)
    return calls


async def test_gate_allows_readonly_tools_silently(ask_env):
    cb = engine._make_ask_gate_cb("sk", None)
    for tool in ("Read", "Glob", "Grep", "TodoWrite"):
        assert isinstance(await cb(tool, {"file_path": "x"}, None), PermissionResultAllow)
    assert "created" not in ask_env


async def test_gate_allow_once(ask_env):
    cb = engine._make_ask_gate_cb("sk", None)
    task = asyncio.create_task(cb("Bash", {"command": "echo hi"}, None))
    await asyncio.sleep(0)
    assert ask_env["created"]["chat_id"] == "chatX"      # dispatcher, not closure capture
    assert ask_env["created"]["preview"].startswith("echo hi")
    ask_env["future"].set_result({"decision": "allow", "feedback": ""})
    assert isinstance(await task, PermissionResultAllow)


async def test_gate_deny_carries_feedback_to_the_model(ask_env):
    cb = engine._make_ask_gate_cb("sk", None)
    task = asyncio.create_task(cb("Bash", {"command": "rm -rf /"}, None))
    await asyncio.sleep(0)
    ask_env["future"].set_result({"decision": "deny", "feedback": "never delete anything"})
    res = await task
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "never delete anything"


async def test_gate_deny_without_feedback_is_still_model_readable(ask_env):
    cb = engine._make_ask_gate_cb("sk", None)
    task = asyncio.create_task(cb("Edit", {"file_path": "/tmp/x"}, None))
    await asyncio.sleep(0)
    ask_env["future"].set_result({"decision": "deny", "feedback": ""})
    res = await task
    assert isinstance(res, PermissionResultDeny)
    assert "denied Edit" in res.message


async def test_gate_allow_always_is_an_allow(ask_env):
    cb = engine._make_ask_gate_cb("sk", None)
    task = asyncio.create_task(cb("Bash", {"command": "ls"}, None))
    await asyncio.sleep(0)
    ask_env["future"].set_result({"decision": "allow_always", "feedback": ""})
    assert isinstance(await task, PermissionResultAllow)


async def test_gate_skips_parking_for_always_allowed_tool(ask_env):
    """The store answers (None, None) for a tool on the project's list — no card, no wait."""
    cb = engine._make_ask_gate_cb("sk", None)
    assert isinstance(await cb("AlwaysOk", {}, None), PermissionResultAllow)
    assert "created" not in ask_env


async def test_gate_times_out_into_a_deny(ask_env, monkeypatch):
    """A parked decision must never hang the turn forever (patched timeout — no 15-min test)."""
    monkeypatch.setattr(engine, "ASK_GATE_TIMEOUT_SEC", 0.05)
    res = await engine._make_ask_gate_cb("sk", None)("Bash", {"command": "sleep 1"}, None)
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "no operator response within 0.05s — denied"
    assert ask_env["resolved"] and ask_env["resolved"][0][:2] == ("beef1234", "timeout")


async def test_gate_cancel_resolves_cancelled(ask_env):
    cb = engine._make_ask_gate_cb("sk", None)
    task = asyncio.create_task(cb("Bash", {"command": "x"}, None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ask_env["resolved"] and ask_env["resolved"][0][:2] == ("beef1234", "cancelled")


async def test_gate_without_cockpit_allows(monkeypatch):
    monkeypatch.setattr(engine, "_create_pending_tool_cb", None)
    res = await engine._make_ask_gate_cb("sk2", None)("Bash", {"command": "x"}, None)
    assert isinstance(res, PermissionResultAllow)


def test_preview_is_redacted_and_truncated():
    prev = engine._ask_tool_preview("Bash", {"command": "curl -H 'authorization: Bearer sk-ant-abcdefghijklmnop' x"})
    assert "abcdefghijklmnop" not in prev
    assert "[redacted]" in prev
    long = engine._ask_tool_preview("Write", {"file_path": "/tmp/a", "content": "z" * 50_000})
    assert len(long) <= engine._ASK_PREVIEW_CHARS
    assert engine._ask_tool_preview("WebFetch", {"url": "https://example.com"}) == "https://example.com"


# ─────────────────────── 3. decision store + endpoints ────────────────────────

@pytest.fixture
def ask_app_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "plans").mkdir(parents=True)
    monkeypatch.setattr(_webapp, "_PLANS_DIR", data / "plans")
    monkeypatch.setattr(_webapp, "_plan_records", {})
    monkeypatch.setattr(_webapp, "_pending_plan_futures", {})
    monkeypatch.setattr(_webapp, "_plan_pending_by_session", {})
    monkeypatch.setattr(_webapp, "_tool_pending_by_session", {})
    monkeypatch.setattr(_webapp, "_last_turn_options", {})
    monkeypatch.setattr(_webapp, "_crash_state_dirty", False)
    monkeypatch.setattr(_webapp, "_CRASH_STATE_FILE", data / "crash-recovery-state.json")

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    saved: dict = {"n": 0}
    ctx = {
        "topics": {"1001:42": {"project": "proj", "cwd": str(project_dir), "model": "sonnet"}},
        "sessions": {}, "running": {}, "cwd_locks": {},
        "password": "pw", "DATA": data, "HERE": ROOT,
        "VAULT_PROJECTS": None, "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: saved.__setitem__("n", saved["n"] + 1),
        "save_handoff": lambda: None, "run_engine": None, "ptb_app": None,
        "rate_limits": {}, "pending_handoff": {}, "context_warned": set(),
        "live_clients": {}, "evict_live_client": None,
    }
    ctx["_auth_token"] = _derive_token("pw")
    ctx["_saved_counter"] = saved
    monkeypatch.setattr(_webapp, "_WEBAPP_CTX", ctx)
    return ctx


def _make_app(ctx):
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/projects/{id}/plan/{plan_id}", _webapp.api_plan_get)
    app.router.add_post("/api/projects/{id}/plan/{plan_id}/decide", _webapp.api_plan_decide)
    app.router.add_get("/api/projects/{id}/decision/{decision_id}", _webapp.api_plan_get)
    app.router.add_post("/api/projects/{id}/decision/{decision_id}/decide",
                        _webapp.api_plan_decide)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_store_parks_and_resolves_a_tool_decision(ask_app_env):
    ctx = ask_app_env
    did, fut = _webapp.create_pending_tool_decision(ctx, "1001:42", "chatA", "Bash", "ls -la")
    assert _webapp._tool_pending_by_session["1001:42"] == [did]
    rec = _webapp._read_plan_meta(did)
    assert rec["kind"] == "tool" and rec["tool_name"] == "Bash"
    assert rec["status"] == "awaiting_approval"
    assert (_webapp._PLANS_DIR / f"{did}.json").exists()

    assert _webapp.resolve_decision(ctx, did, "allow") is True
    assert (await fut) == {"decision": "allow", "feedback": ""}
    assert _webapp._read_plan_meta(did)["status"] == "allowed"
    assert "1001:42" not in _webapp._tool_pending_by_session
    # Idempotent: a second decide (double tap / stale tab) is a no-op.
    assert _webapp.resolve_decision(ctx, did, "deny") is False


async def test_store_supports_parallel_gates(ask_app_env):
    """One assistant message can carry several tool_use blocks — the index is a list."""
    ctx = ask_app_env
    d1, _ = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "a")
    d2, _ = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Edit", "b")
    assert _webapp._tool_pending_by_session["1001:42"] == [d1, d2]
    _webapp.resolve_decision(ctx, d1, "deny", "no")
    assert _webapp._tool_pending_by_session["1001:42"] == [d2]


async def test_always_allow_short_circuits_the_store(ask_app_env):
    ctx = ask_app_env
    ctx["topics"]["1001:42"]["ask_always_allow"] = ["Bash"]
    assert _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "ls") == (None, None)
    # A different tool still parks.
    did, _ = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Edit", "x")
    assert did is not None


async def test_tool_ready_bus_event_is_bounded(ask_app_env, monkeypatch):
    ctx = ask_app_env
    events: list = []
    monkeypatch.setattr(_webapp, "_bus_publish",
                        lambda sk, ev, persist=True: events.append(ev))
    _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "x" * 100_000)
    ready = [e for e in events if e.get("kind") == "tool_ready"]
    assert ready and len(ready[0]["tool_preview"]) <= _webapp._ASK_PREVIEW_LIMIT
    assert ready[0]["tool_name"] == "Bash"


async def test_decision_endpoints_require_auth(aiohttp_client, ask_app_env):
    ctx = ask_app_env
    did, _ = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "ls")
    client = await aiohttp_client(_make_app(ctx))
    assert (await client.get(f"/api/projects/proj/decision/{did}")).status == 401
    r = await client.post(f"/api/projects/proj/decision/{did}/decide", json={"decision": "allow"})
    assert r.status == 401
    assert _webapp._read_plan_meta(did)["status"] == "awaiting_approval"  # untouched


async def test_decision_get_and_allow(aiohttp_client, ask_app_env):
    ctx = ask_app_env
    did, fut = _webapp.create_pending_tool_decision(ctx, "1001:42", "chatB", "Bash", "ls -la")
    client = await aiohttp_client(_make_app(ctx))

    rec = await (await client.get(f"/api/projects/proj/decision/{did}",
                                  headers=_auth(ctx))).json()
    assert rec["tool_name"] == "Bash" and rec["tool_preview"] == "ls -la"

    r = await client.post(f"/api/projects/proj/decision/{did}/decide",
                          headers=_auth(ctx), json={"decision": "allow"})
    assert r.status == 200 and (await r.json())["status"] == "allowed"
    assert (await fut)["decision"] == "allow"

    r2 = await client.post(f"/api/projects/proj/decision/{did}/decide",
                           headers=_auth(ctx), json={"decision": "deny"})
    assert (await r2.json()).get("noop") is True


async def test_decision_deny_with_feedback(aiohttp_client, ask_app_env):
    ctx = ask_app_env
    did, fut = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "rm -rf /")
    client = await aiohttp_client(_make_app(ctx))
    r = await client.post(f"/api/projects/proj/decision/{did}/decide", headers=_auth(ctx),
                          json={"decision": "deny", "feedback": "not that path"})
    assert (await r.json())["status"] == "denied"
    assert (await fut) == {"decision": "deny", "feedback": "not that path"}


async def test_decision_allow_always_persists_per_project(aiohttp_client, ask_app_env):
    ctx = ask_app_env
    did, fut = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "ls")
    client = await aiohttp_client(_make_app(ctx))
    r = await client.post(f"/api/projects/proj/decision/{did}/decide", headers=_auth(ctx),
                          json={"decision": "allow_always"})
    assert r.status == 200
    assert (await fut)["decision"] == "allow_always"
    assert ctx["topics"]["1001:42"]["ask_always_allow"] == ["Bash"]
    assert ctx["_saved_counter"]["n"] >= 1          # persisted, not only in memory
    # The next Bash call in this project is no longer gated.
    assert _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "ls") == (None, None)


async def test_decision_validates_id_and_verb(aiohttp_client, ask_app_env):
    ctx = ask_app_env
    client = await aiohttp_client(_make_app(ctx))
    r = await client.post("/api/projects/proj/decision/zzzz/decide",
                          headers=_auth(ctx), json={"decision": "allow"})
    assert r.status == 400
    did, _ = _webapp.create_pending_tool_decision(ctx, "1001:42", None, "Bash", "ls")
    r2 = await client.post(f"/api/projects/proj/decision/{did}/decide",
                           headers=_auth(ctx), json={"decision": "approve"})
    assert r2.status == 400          # plan vocabulary must not decide a tool request
    r3 = await client.get("/api/projects/proj/decision/00000000", headers=_auth(ctx))
    assert r3.status == 404


async def test_plan_routes_still_serve_plans(aiohttp_client, ask_app_env):
    """The shipped /plan routes keep working — same store, plan vocabulary intact."""
    ctx = ask_app_env
    plan_id, fut = _webapp.create_pending_plan(ctx, "1001:42", None, "## P")
    client = await aiohttp_client(_make_app(ctx))
    rec = await (await client.get(f"/api/projects/proj/plan/{plan_id}",
                                  headers=_auth(ctx))).json()
    assert rec["kind"] == "plan" and rec["plan_text"] == "## P"
    r = await client.post(f"/api/projects/proj/plan/{plan_id}/decide", headers=_auth(ctx),
                          json={"decision": "approve"})
    assert (await r.json())["status"] == "approved"
    assert (await fut)["decision"] == "approve"


async def test_settings_expose_and_revoke_always_allow(ask_app_env):
    ctx = ask_app_env
    project = {"cwd": ctx["topics"]["1001:42"]["cwd"], "ask_always_allow": ["Bash", "Edit"]}
    assert _webapp._project_settings_view(project)["ask_always_allow"] == ["Bash", "Edit"]
    _webapp._ask_always_allow_add(ctx, project, "Write")
    assert ctx["topics"]["1001:42"]["ask_always_allow"] == ["Bash", "Edit", "Write"]

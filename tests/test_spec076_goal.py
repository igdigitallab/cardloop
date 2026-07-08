"""
spec-076: Goal mode — the CLI's native session-goal machinery (prompt-type Stop hook).

run_engine(goal=...) must register the Stop hook via --settings (composed with the
ultracode switch), prepend a goal reminder to the prompt, and surface enforcement as
goal_status events (blocked attempts + a terminal verdict). The webapp stores the goal
ON the chat object in chats.json, exposes PUT/DELETE endpoints, resolves the active
goal for both the direct POST path and the queue-drain path, and applies goal_status
events back into the record (met auto-clear / capped / errored-keeps-active).
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp as _webapp
from webapp import (
    _chat_active_goal,
    _chat_goal_patch,
    _chat_goal_set,
    _derive_token,
    _ensure_chat_entry,
    _goal_apply_event,
    _load_chats,
)
from claude_agent_sdk import ResultMessage, TextBlock, UserMessage


# ─────────────────────────── engine: settings composition ───────────────────────────


def test_compose_settings_variants():
    """The inline --settings JSON composes the native ultracode switch and the goal
    Stop hook; both-off returns None (no --settings flag at all)."""
    assert engine._compose_settings(False, None) is None
    assert engine._compose_settings(True, None) == engine.ULTRACODE_SETTINGS
    goal_only = json.loads(engine._compose_settings(False, "tests pass"))
    assert "ultracode" not in goal_only
    assert goal_only["hooks"]["Stop"][0]["hooks"][0] == {"type": "prompt", "prompt": "tests pass"}
    both = json.loads(engine._compose_settings(True, "tests pass"))
    assert both["ultracode"] is True
    assert both["hooks"]["Stop"][0]["hooks"][0]["prompt"] == "tests pass"


def test_goal_max_len_in_sync():
    """webapp validates with its own constant — keep it in lockstep with the engine's."""
    assert _webapp._GOAL_MAX_LEN == engine.GOAL_MAX_LEN == 4000


def test_goal_block_cap_default_and_env(monkeypatch):
    assert engine._goal_block_cap() == 8
    monkeypatch.setenv("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "3")
    assert engine._goal_block_cap() == 3
    monkeypatch.setenv("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "junk")
    assert engine._goal_block_cap() == 8


# ─────────────────────────── engine: run_engine wiring ───────────────────────────


def _fake_client_capturing(captured: dict):
    class FakeClient:
        def __init__(self, options):
            captured["opts"] = options

        async def query(self, prompt):
            captured["prompt"] = prompt

        async def receive_response(self):
            return
            yield

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
        async for _ in engine.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:goal",
            model="opus",
            **kwargs,
        ):
            pass
    return captured


@pytest.mark.asyncio
async def test_goal_registers_stop_hook_and_wraps_prompt(tmp_path):
    cap = await _drain_run_engine(tmp_path, goal="the marker file exists", effort="high")
    opts = cap["opts"]
    settings = json.loads(opts.settings)
    assert settings["hooks"]["Stop"][0]["hooks"][0] == {
        "type": "prompt", "prompt": "the marker file exists"}
    assert "ultracode" not in settings, "goal alone must not flip ultracode"
    assert opts.effort == "high", "goal must not touch the effort ladder"
    assert cap["prompt"].startswith("<system-reminder>")
    assert "the marker file exists" in cap["prompt"]
    assert cap["prompt"].rstrip().endswith("hi")


@pytest.mark.asyncio
async def test_goal_composes_with_ultracode(tmp_path):
    cap = await _drain_run_engine(tmp_path, goal="done", ultracode=True)
    opts = cap["opts"]
    settings = json.loads(opts.settings)
    assert settings["ultracode"] is True
    assert settings["hooks"]["Stop"][0]["hooks"][0]["prompt"] == "done"
    assert opts.effort is None, "ultracode still pins effort natively (no --effort)"


@pytest.mark.asyncio
async def test_no_goal_is_noop(tmp_path):
    cap = await _drain_run_engine(tmp_path)
    assert cap["opts"].settings is None
    assert cap["prompt"] == "hi", "prompt must be untouched without a goal"


@pytest.mark.asyncio
async def test_goal_clamped_to_max_len(tmp_path):
    cap = await _drain_run_engine(tmp_path, goal="x" * (engine.GOAL_MAX_LEN + 500))
    settings = json.loads(cap["opts"].settings)
    assert len(settings["hooks"]["Stop"][0]["hooks"][0]["prompt"]) == engine.GOAL_MAX_LEN


@pytest.mark.asyncio
async def test_blank_goal_treated_as_none(tmp_path):
    cap = await _drain_run_engine(tmp_path, goal="   ")
    assert cap["opts"].settings is None
    assert cap["prompt"] == "hi"


# ─────────────────────────── engine: goal_status events ───────────────────────────


class _FakeTurnClient:
    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        for m in self._messages:
            yield m


def _result(session_id="sid"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id=session_id)


def _feedback(condition, reason):
    """The synthetic user message the CLI injects on a blocked stop attempt."""
    return UserMessage(
        content=[TextBlock(text=f"Stop hook feedback:\n[{condition}]: {reason}")],
        parent_tool_use_id=None,
    )


async def _run_with_messages(tmp_path, msgs, **kwargs):
    events = []
    with patch.object(engine, "ClaudeSDKClient", return_value=_FakeTurnClient(msgs)), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None):
        async for ev in engine.run_engine(
            project_name="t", cwd=str(tmp_path), prompt="hi",
            session_key="chat:spec076", model="opus", ctx=None, **kwargs,
        ):
            events.append(ev)
    return events


@pytest.mark.asyncio
async def test_goal_feedback_yields_block_events_and_terminal_met(tmp_path):
    goal = "the marker exists"
    msgs = [
        _feedback(goal, "insufficient evidence in transcript"),
        _feedback(goal, "still nothing"),
        _result(),
    ]
    events = await _run_with_messages(tmp_path, msgs, goal=goal)
    blocks = [e for e in events if e["type"] == "goal_status" and not e.get("terminal")]
    assert [b["iteration"] for b in blocks] == [1, 2]
    assert blocks[0]["reason"] == "insufficient evidence in transcript"
    assert blocks[0]["condition"] == goal
    terminal = [e for e in events if e["type"] == "goal_status" and e.get("terminal")]
    assert len(terminal) == 1
    assert terminal[0]["met"] is True
    assert terminal[0]["iterations"] == 2
    assert terminal[0]["capped"] is False
    # Terminal goal_status must precede the result event (consumers persist before turn-end).
    types = [e["type"] for e in events]
    assert types.index("goal_status") < types.index("result")


@pytest.mark.asyncio
async def test_goal_cap_reported_as_unmet(tmp_path):
    goal = "impossible"
    msgs = [_feedback(goal, f"attempt {i}") for i in range(8)] + [_result()]
    events = await _run_with_messages(tmp_path, msgs, goal=goal)
    terminal = next(e for e in events if e["type"] == "goal_status" and e.get("terminal"))
    assert terminal["met"] is False
    assert terminal["capped"] is True
    assert terminal["iterations"] == 8


@pytest.mark.asyncio
async def test_no_goal_ignores_hook_like_user_messages(tmp_path):
    """Without a goal, user messages (even hook-shaped ones) must not emit goal events."""
    msgs = [_feedback("x", "y"), _result()]
    events = await _run_with_messages(tmp_path, msgs)
    assert not [e for e in events if e["type"] == "goal_status"]


@pytest.mark.asyncio
async def test_goal_tool_result_user_messages_not_counted(tmp_path):
    """Ordinary in-turn user messages (tool results) don't match the feedback prefix."""
    msgs = [
        UserMessage(content=[TextBlock(text="some tool result payload")],
                    parent_tool_use_id=None),
        _result(),
    ]
    events = await _run_with_messages(tmp_path, msgs, goal="g")
    blocks = [e for e in events if e["type"] == "goal_status" and not e.get("terminal")]
    assert blocks == []
    terminal = next(e for e in events if e["type"] == "goal_status" and e.get("terminal"))
    assert terminal["met"] is True and terminal["iterations"] == 0


# ─────────────────────────── webapp: store helpers ───────────────────────────


@pytest.fixture
def fake_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(tmp_path / "myproject"),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "myproject").mkdir()
    return ctx


def test_chat_active_goal_only_for_active_status():
    assert _chat_active_goal(None) is None
    assert _chat_active_goal({}) is None
    assert _chat_active_goal({"goal": {"condition": "x", "status": "active"}}) == "x"
    assert _chat_active_goal({"goal": {"condition": "x", "status": "met"}}) is None
    assert _chat_active_goal({"goal": {"condition": "x", "status": "capped"}}) is None
    assert _chat_active_goal({"goal": {"condition": "  ", "status": "active"}}) is None


@pytest.mark.asyncio
async def test_goal_set_patch_and_apply_event_lifecycle(fake_ctx):
    pid, sk = "myproject", "1001:42"
    async with _webapp._chats_lock():
        cid = _ensure_chat_entry(fake_ctx, pid, sk)[pid]["active"]

    rec = {"condition": "tests green", "status": "active", "set_at": time.time(),
           "iterations": 0, "last_reason": None}
    stored = await _chat_goal_set(fake_ctx, pid, cid, sk, rec)
    assert stored == rec
    chat = _load_chats(fake_ctx)[pid]["chats"][0]
    assert chat["goal"]["condition"] == "tests green"
    assert _chat_active_goal(chat) == "tests green"

    # Non-terminal block → iterations + last verdict.
    await _goal_apply_event(fake_ctx, pid, cid, sk,
                            {"type": "goal_status", "met": False, "iteration": 2,
                             "reason": "not yet"})
    goal = _load_chats(fake_ctx)[pid]["chats"][0]["goal"]
    assert goal["iterations"] == 2 and goal["last_reason"] == "not yet"
    assert goal["status"] == "active"

    # Errored terminal → keeps enforcing (status stays active).
    await _goal_apply_event(fake_ctx, pid, cid, sk,
                            {"type": "goal_status", "met": False, "terminal": True,
                             "iterations": 3, "capped": False, "errored": True})
    goal = _load_chats(fake_ctx)[pid]["chats"][0]["goal"]
    assert goal["status"] == "active" and goal["iterations"] == 3

    # Met terminal → auto-clear enforcement (status "met"), reason wiped.
    await _goal_apply_event(fake_ctx, pid, cid, sk,
                            {"type": "goal_status", "met": True, "terminal": True,
                             "iterations": 4, "condition": "tests green"})
    chat = _load_chats(fake_ctx)[pid]["chats"][0]
    assert chat["goal"]["status"] == "met"
    assert chat["goal"]["last_reason"] is None
    assert _chat_active_goal(chat) is None, "met goal must no longer be enforced"

    # Capped terminal on a fresh goal → status "capped" (also not enforced).
    await _chat_goal_set(fake_ctx, pid, cid, sk, dict(rec))
    await _goal_apply_event(fake_ctx, pid, cid, sk,
                            {"type": "goal_status", "met": False, "terminal": True,
                             "iterations": 8, "capped": True})
    chat = _load_chats(fake_ctx)[pid]["chats"][0]
    assert chat["goal"]["status"] == "capped"
    assert _chat_active_goal(chat) is None

    # Clear.
    await _chat_goal_set(fake_ctx, pid, cid, sk, None)
    assert "goal" not in _load_chats(fake_ctx)[pid]["chats"][0]


@pytest.mark.asyncio
async def test_goal_apply_event_noops_without_ids_or_goal(fake_ctx):
    pid, sk = "myproject", "1001:42"
    async with _webapp._chats_lock():
        cid = _ensure_chat_entry(fake_ctx, pid, sk)[pid]["active"]
    # No chat_id / no project_id → silently ignored.
    await _goal_apply_event(fake_ctx, None, cid, sk, {"met": True, "terminal": True})
    await _goal_apply_event(fake_ctx, pid, None, sk, {"met": True, "terminal": True})
    # Chat without a goal → patch is a no-op.
    await _goal_apply_event(fake_ctx, pid, cid, sk, {"met": True, "terminal": True})
    assert "goal" not in _load_chats(fake_ctx)[pid]["chats"][0]


# ─────────────────────────── webapp: HTTP endpoint ───────────────────────────


@pytest.fixture
def goal_app(fake_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/projects/{id}/chats", _webapp.api_project_chats_list)
    app.router.add_put("/api/projects/{id}/chats/{chat_id}/goal", _webapp.api_project_chat_goal)
    app.router.add_delete("/api/projects/{id}/chats/{chat_id}/goal", _webapp.api_project_chat_goal)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_api_goal_put_get_delete(aiohttp_client, goal_app, fake_ctx):
    client = await aiohttp_client(goal_app)
    h = _auth(fake_ctx)

    res = await client.get("/api/projects/myproject/chats", headers=h)
    cid = (await res.json())["active"]

    res = await client.put(f"/api/projects/myproject/chats/{cid}/goal",
                           json={"condition": "deploy is green"}, headers=h)
    assert res.status == 200
    goal = (await res.json())["goal"]
    assert goal["condition"] == "deploy is green" and goal["status"] == "active"

    # The goal rides on the chats payload (frontend hydration path).
    res = await client.get("/api/projects/myproject/chats", headers=h)
    chats = (await res.json())["chats"]
    assert chats[0]["goal"]["condition"] == "deploy is green"

    res = await client.delete(f"/api/projects/myproject/chats/{cid}/goal", headers=h)
    assert res.status == 200
    res = await client.get("/api/projects/myproject/chats", headers=h)
    assert "goal" not in (await res.json())["chats"][0]


async def test_api_goal_validation(aiohttp_client, goal_app, fake_ctx):
    client = await aiohttp_client(goal_app)
    h = _auth(fake_ctx)
    res = await client.get("/api/projects/myproject/chats", headers=h)
    cid = (await res.json())["active"]

    res = await client.put(f"/api/projects/myproject/chats/{cid}/goal",
                           json={"condition": "   "}, headers=h)
    assert res.status == 400
    res = await client.put(f"/api/projects/myproject/chats/{cid}/goal",
                           json={"condition": "x" * (_webapp._GOAL_MAX_LEN + 1)}, headers=h)
    assert res.status == 400
    res = await client.put("/api/projects/myproject/chats/NOTHEX/goal",
                           json={"condition": "x"}, headers=h)
    assert res.status == 400
    res = await client.put(f"/api/projects/myproject/chats/{cid}/goal",
                           data=b"not json", headers=h)
    assert res.status == 400
    res = await client.put("/api/projects/nosuch/chats/abc123/goal",
                           json={"condition": "x"}, headers=h)
    assert res.status == 404
    res = await client.put("/api/projects/myproject/chats/abcdef/goal",
                           json={"condition": "x"}, headers=h)
    assert res.status == 404, "valid-format but unknown chat id"


# ─────────────────────────── webapp: drain-path parity ───────────────────────────


@pytest.mark.asyncio
async def test_chat_queue_execute_passes_active_goal(monkeypatch, tmp_path):
    """spec-071 parity class: the queue-drain path must resolve the chat's ACTIVE goal from
    chats.json and pass it to run_engine — otherwise queued/auto-continue turns would run
    unenforced AND flip the live-client fingerprint (settings mismatch → evict+SIGTERM)."""
    sk = "1001:42"
    pid = "myproject"
    calls = []

    async def fake_run_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "sid-goal"}

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (tmp_path / "myproject").mkdir()
    ctx = {
        "topics": {sk: {"project": pid, "cwd": str(tmp_path / "myproject"), "model": "sonnet"}},
        "sessions": {},
        "running": {sk: True},
        "run_engine": fake_run_engine,
        "save_sessions": lambda: None,
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
    }
    monkeypatch.setattr(_webapp, "_find_project_by_id",
                        lambda c, p: {"id": pid, "name": pid, "cwd": str(tmp_path / "myproject"),
                                      "model": "sonnet", "session_key": sk})
    monkeypatch.setattr(_webapp, "_secrets_read", lambda cwd: {})

    async def fake_resolve(s):
        return s

    monkeypatch.setattr(_webapp, "_resolve_secret_refs", fake_resolve)
    monkeypatch.setattr(_webapp, "_build_agents_kwargs", lambda c, a: {})

    async with _webapp._chats_lock():
        cid = _ensure_chat_entry(ctx, pid, sk)[pid]["active"]
    await _chat_goal_set(ctx, pid, cid, sk,
                         {"condition": "queued turns enforce too", "status": "active",
                          "set_at": time.time(), "iterations": 0, "last_reason": None})
    try:
        await _webapp._chat_queue_execute(ctx, sk, {"id": "i1", "text": "hello",
                                                    "created_at": 0, "project_id": pid})
        assert calls, "queued item must reach run_engine"
        assert calls[0]["goal"] == "queued turns enforce too"
    finally:
        _webapp._live_turns.pop(sk, None)
        _webapp._live_seq.pop(sk, None)

    # A met goal must NOT be enforced on the next drained turn.
    await _goal_apply_event(ctx, pid, cid, sk,
                            {"type": "goal_status", "met": True, "terminal": True,
                             "iterations": 1})
    calls.clear()
    ctx["running"][sk] = True
    try:
        await _webapp._chat_queue_execute(ctx, sk, {"id": "i2", "text": "again",
                                                    "created_at": 0, "project_id": pid})
        assert calls and calls[0]["goal"] is None
    finally:
        _webapp._live_turns.pop(sk, None)
        _webapp._live_seq.pop(sk, None)

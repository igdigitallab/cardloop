"""
Tests for spec-017: Fable conductor + executor sub-agents (Phase A + Phase B).

Phase A: model plumbing — fable alias, default model, allowed models.
Phase B: sub-agent event forwarding, conductor prompt injection, agents param wiring.
"""
import sys
import json
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import webapp as _webapp


# ═══════════════════════════════════════════════════════════════
# Phase A — Model plumbing
# ═══════════════════════════════════════════════════════════════


def test_model_fable_in_models_dict():
    """MODELS dict must include the 'fable' alias."""
    assert "fable" in bot.MODELS


def test_allowed_models_includes_fable():
    """_ALLOWED_MODELS set in webapp must include 'fable'."""
    assert "fable" in _webapp._ALLOWED_MODELS


def test_default_model_is_fable(monkeypatch):
    """When DEFAULT_MODEL env is unset the hard-coded default must be 'fable'."""
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    import importlib
    # Read the source default directly — we can't reload bot without side effects,
    # but we can verify the module constant reflects the env-or-default logic.
    # bot.DEFAULT_MODEL is already resolved at import time from the env;
    # assert the fallback in the source is 'fable'.
    import inspect, ast
    src = inspect.getsource(bot)
    # Find the os.environ.get("DEFAULT_MODEL", ...) call and check default value
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2):
            if (isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "DEFAULT_MODEL"):
                default_val = node.args[1].value if isinstance(node.args[1], ast.Constant) else None
                assert default_val == "fable", (
                    f"DEFAULT_MODEL fallback should be 'fable', got {default_val!r}"
                )
                return
    pytest.fail("Could not find os.environ.get('DEFAULT_MODEL', ...) in bot.py source")


def test_model_fable_accepted_by_settings(tmp_path):
    """POST /api/projects/{id}/settings with model=fable must return 200 and store it."""
    import webapp as _webapp
    from webapp import _derive_token
    from aiohttp import web
    import asyncio

    pdir = tmp_path / "proj"
    pdir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    ctx = {
        "topics": {
            "100:1": {"project": "proj", "cwd": str(pdir), "model": "sonnet"},
        },
        "sessions": {},
        "running": {},
        "password": "pw",
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
        "MODELS": bot.MODELS,
    }
    ctx["_auth_token"] = _derive_token("pw")
    _webapp._reset_settings_globals() if hasattr(_webapp, "_reset_settings_globals") else None

    async def run():
        app = web.Application(middlewares=[_webapp.auth_middleware])
        app["ctx"] = ctx
        app.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/projects/proj/settings",
                json={"model": "fable"},
                headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
            )
            assert resp.status == 200, f"Expected 200 for model=fable, got {resp.status}"
            # model should be stored
            entry = ctx["topics"].get("100:1", {})
            assert entry.get("model") == "fable", f"model not stored: {entry}"

    asyncio.run(run())


def test_model_fable_accepted_by_put_model(tmp_path):
    """PUT /api/projects/{id}/model with model=fable must return 200 and store it."""
    import webapp as _webapp
    from webapp import _derive_token
    from aiohttp import web
    import asyncio

    pdir = tmp_path / "proj2"
    pdir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    ctx = {
        "topics": {
            "100:2": {"project": "proj2", "cwd": str(pdir), "model": "sonnet"},
        },
        "sessions": {},
        "running": {},
        "password": "pw",
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
        "MODELS": bot.MODELS,
    }
    ctx["_auth_token"] = _derive_token("pw")

    async def run():
        app = web.Application(middlewares=[_webapp.auth_middleware])
        app["ctx"] = ctx
        app.router.add_post("/api/projects/{id}/model", _webapp.api_project_set_model)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/projects/proj2/model",
                json={"model": "fable"},
                headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
            )
            assert resp.status == 200, f"Expected 200 for model=fable, got {resp.status}"
            entry = ctx["topics"].get("100:2", {})
            assert entry.get("model") == "fable", f"model not stored: {entry}"

    asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# Phase B — Sub-agent events and conductor prompt
# ═══════════════════════════════════════════════════════════════


def test_conductor_prompt_constant_exists():
    """CONDUCTOR_PROMPT module constant must be non-empty and mention 'orchestrator'."""
    assert hasattr(bot, "CONDUCTOR_PROMPT")
    assert "orchestrator" in bot.CONDUCTOR_PROMPT.lower()


def test_default_agents_roster():
    """DEFAULT_AGENTS must have executor, researcher, quick entries."""
    assert "executor" in bot.DEFAULT_AGENTS
    assert "researcher" in bot.DEFAULT_AGENTS
    assert "quick" in bot.DEFAULT_AGENTS
    researcher = bot.DEFAULT_AGENTS["researcher"]
    assert researcher.disallowedTools is not None
    assert "Write" in researcher.disallowedTools
    assert "Edit" in researcher.disallowedTools
    assert "NotebookEdit" in researcher.disallowedTools


def _make_sdk_mocks():
    """Helper: returns patched TaskStarted/Progress/Notification message instances."""
    from claude_agent_sdk import (
        TaskStartedMessage,
        TaskProgressMessage,
        TaskNotificationMessage,
    )
    # TaskStartedMessage(subtype, data, task_id, description, uuid, session_id)
    started = TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id="tid-1",
        description="Run unit tests",
        uuid="uuid-1",
        session_id="sess-1",
    )
    progress = TaskProgressMessage(
        subtype="task_progress",
        data={},
        task_id="tid-1",
        description="Run unit tests",
        usage=MagicMock(),
        uuid="uuid-2",
        session_id="sess-1",
        last_tool_name="Bash",
    )
    notification = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="tid-1",
        status="completed",
        output_file="",
        summary="All tests passed",
        uuid="uuid-3",
        session_id="sess-1",
    )
    return started, progress, notification


def _make_fake_client(messages):
    """Build a fake ClaudeSDKClient async context manager that yields messages."""
    client = MagicMock()
    client.interrupt = AsyncMock()

    async def _receive():
        for m in messages:
            yield m

    client.receive_response = _receive
    client.query = AsyncMock()

    # async context manager
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_run_engine_yields_subagent_started(tmp_path):
    """run_engine must yield type=subagent subtype=started for TaskStartedMessage."""
    started, _, _ = _make_sdk_mocks()
    fake_client = _make_fake_client([started])

    with patch.object(bot, "ClaudeSDKClient", return_value=fake_client), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        events = []
        async for ev in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="sonnet",
        ):
            events.append(ev)

    subagent_events = [e for e in events if e.get("type") == "subagent"]
    assert len(subagent_events) >= 1, f"Expected subagent event, got: {events}"
    ev = subagent_events[0]
    assert ev["subtype"] == "started"
    assert ev["task_id"] == "tid-1"
    assert ev["description"] == "Run unit tests"
    assert ev["status"] is None


@pytest.mark.asyncio
async def test_run_engine_yields_subagent_notification(tmp_path):
    """run_engine must yield type=subagent subtype=notification with status and summary."""
    _, _, notification = _make_sdk_mocks()
    fake_client = _make_fake_client([notification])

    with patch.object(bot, "ClaudeSDKClient", return_value=fake_client), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        events = []
        async for ev in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="sonnet",
        ):
            events.append(ev)

    subagent_events = [e for e in events if e.get("type") == "subagent"]
    assert len(subagent_events) >= 1
    ev = subagent_events[0]
    assert ev["subtype"] == "notification"
    assert ev["status"] == "completed"
    assert ev["summary"] == "All tests passed"


@pytest.mark.asyncio
async def test_run_engine_passes_agents_to_opts(tmp_path):
    """run_engine must pass agents kwarg to ClaudeAgentOptions."""
    from claude_agent_sdk import AgentDefinition

    custom_agents = {
        "my_agent": AgentDefinition(
            description="test",
            prompt="test agent",
            model="haiku",
            permissionMode="bypassPermissions",
        )
    }

    captured_opts = {}

    class FakeClient:
        def __init__(self, options):
            captured_opts["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield  # make it an async generator

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch.object(bot, "ClaudeSDKClient", FakeClient), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="sonnet",
            agents=custom_agents,
        ):
            pass

    opts = captured_opts.get("opts")
    assert opts is not None, "ClaudeAgentOptions not captured"
    assert opts.agents == custom_agents, f"agents not passed: {opts.agents}"


@pytest.mark.asyncio
async def test_conductor_prompt_injected_for_fable(tmp_path):
    """When model=fable, run_engine must inject CONDUCTOR_PROMPT into system_prompt append."""
    captured_opts = {}

    class FakeClient:
        def __init__(self, options):
            captured_opts["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch.object(bot, "ClaudeSDKClient", FakeClient), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="fable",
        ):
            pass

    opts = captured_opts.get("opts")
    assert opts is not None
    # system_prompt passed as dict {type, preset, append}; the append field carries conductor text
    sp = opts.system_prompt
    if isinstance(sp, dict):
        append_text = sp.get("append", "")
    else:
        # If SDK serialised it, check str representation
        append_text = str(sp)
    assert bot.CONDUCTOR_PROMPT in append_text, (
        f"CONDUCTOR_PROMPT not found in system_prompt.append: {append_text!r}"
    )


@pytest.mark.asyncio
async def test_conductor_prompt_not_injected_for_sonnet(tmp_path):
    """When model=sonnet, run_engine must NOT inject CONDUCTOR_PROMPT."""
    captured_opts = {}

    class FakeClient:
        def __init__(self, options):
            captured_opts["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch.object(bot, "ClaudeSDKClient", FakeClient), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="sonnet",
        ):
            pass

    opts = captured_opts.get("opts")
    assert opts is not None
    sp = opts.system_prompt
    append_text = sp.get("append", "") if isinstance(sp, dict) else str(sp)
    assert bot.CONDUCTOR_PROMPT not in append_text, (
        f"CONDUCTOR_PROMPT must NOT be injected for sonnet: {append_text!r}"
    )


@pytest.mark.asyncio
async def test_non_task_system_messages_still_silenced(tmp_path):
    """Other SystemMessage subtypes (not Task*) must not produce any events."""
    from claude_agent_sdk import SystemMessage

    other_msg = SystemMessage(subtype="some_other_subtype", data={})
    fake_client = _make_fake_client([other_msg])

    with patch.object(bot, "ClaudeSDKClient", return_value=fake_client), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        events = []
        async for ev in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="sonnet",
        ):
            events.append(ev)

    # No subagent events should appear
    assert not any(e.get("type") == "subagent" for e in events), (
        f"Non-task SystemMessage must not yield subagent events: {events}"
    )


@pytest.mark.asyncio
async def test_chat_sse_forwards_subagent_events(aiohttp_client, tmp_path):
    """api_project_chat SSE stream must forward subagent events to the client."""
    from aiohttp import web
    from webapp import _derive_token

    pdir = tmp_path / "proj"
    pdir.mkdir()

    async def fake_engine(**kwargs):
        yield {
            "type": "subagent",
            "subtype": "started",
            "task_id": "t1",
            "description": "Run lint",
            "status": None,
            "summary": None,
            "last_tool_name": None,
        }
        yield {
            "type": "subagent",
            "subtype": "notification",
            "task_id": "t1",
            "description": "Run lint",
            "status": "completed",
            "summary": "Lint clean",
            "last_tool_name": None,
        }
        yield {"type": "result", "session_id": "s1"}

    ctx = {
        "topics": {"100:1": {"project": "proj", "cwd": str(pdir), "model": "fable"}},
        "sessions": {},
        "running": {},
        "password": "pw",
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "fable",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": fake_engine,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("pw")
    (tmp_path / "data").mkdir(exist_ok=True)

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)

    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/projects/proj/chat",
        json={"prompt": "Go"},
        headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
    )
    body = await resp.read()
    events = []
    for line in body.decode().splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass

    subagent_events = [e for e in events if e.get("type") == "subagent"]
    assert len(subagent_events) == 2, f"Expected 2 subagent SSE events, got: {subagent_events}"
    assert subagent_events[0]["subtype"] == "started"
    assert subagent_events[1]["subtype"] == "notification"
    assert subagent_events[1]["status"] == "completed"
    assert subagent_events[1]["summary"] == "Lint clean"


# ═══════════════════════════════════════════════════════════════
# Phase C — Per-project agents_config
# ═══════════════════════════════════════════════════════════════


def _make_ctx_with_project(tmp_path, project_key="100:1", agents_config=None):
    """Helper: minimal ctx dict for settings tests."""
    from webapp import _derive_token
    pdir = tmp_path / "proj"
    pdir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    entry = {"project": "proj", "cwd": str(pdir), "model": "sonnet"}
    if agents_config is not None:
        entry["agents_config"] = agents_config
    ctx = {
        "topics": {project_key: entry},
        "sessions": {},
        "running": {},
        "password": "pw",
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
        "MODELS": bot.MODELS,
        "_build_agents_kwargs": bot._build_agents_kwargs,
    }
    ctx["_auth_token"] = _derive_token("pw")
    return ctx


def _make_settings_app(ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    app.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    return app


def test_agents_config_partial_update(tmp_path):
    """POST settings with agents_config partial update → 200, persisted in topics."""
    import asyncio
    ctx = _make_ctx_with_project(tmp_path)

    async def run():
        from aiohttp.test_utils import TestClient, TestServer
        app = _make_settings_app(ctx)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/projects/proj/settings",
                json={"agents_config": {"executor_model": "haiku"}},
                headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
            )
            assert resp.status == 200, f"Expected 200, got {resp.status}: {await resp.text()}"
            data = await resp.json()
            assert data["ok"] is True
            assert data["settings"]["agents_config"]["executor_model"] == "haiku"
            # persisted in topics
            entry = ctx["topics"]["100:1"]
            assert entry.get("agents_config", {}).get("executor_model") == "haiku"

    asyncio.run(run())


def test_agents_config_invalid_model_rejected(tmp_path):
    """POST settings with unknown model in agents_config → 400."""
    import asyncio
    ctx = _make_ctx_with_project(tmp_path)

    async def run():
        from aiohttp.test_utils import TestClient, TestServer
        app = _make_settings_app(ctx)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/projects/proj/settings",
                json={"agents_config": {"executor_model": "gpt-4"}},
                headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
            )
            assert resp.status == 400, f"Expected 400 for invalid model, got {resp.status}"
            data = await resp.json()
            assert "error" in data

    asyncio.run(run())


def test_agents_config_unknown_key_rejected(tmp_path):
    """POST settings with unknown key in agents_config → 400."""
    import asyncio
    ctx = _make_ctx_with_project(tmp_path)

    async def run():
        from aiohttp.test_utils import TestClient, TestServer
        app = _make_settings_app(ctx)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/projects/proj/settings",
                json={"agents_config": {"bogus_key": "haiku"}},
                headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
            )
            assert resp.status == 400, f"Expected 400 for unknown key, got {resp.status}"

    asyncio.run(run())


def test_agents_config_conductor_prompt_toggle(tmp_path):
    """conductor_prompt: false stored and honoured in run_engine (skip_conductor_prompt=True)."""
    import asyncio

    # Test storage
    ctx = _make_ctx_with_project(tmp_path)

    async def run_store():
        from aiohttp.test_utils import TestClient, TestServer
        app = _make_settings_app(ctx)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/projects/proj/settings",
                json={"agents_config": {"conductor_prompt": False}},
                headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["settings"]["agents_config"]["conductor_prompt"] is False
            assert ctx["topics"]["100:1"]["agents_config"]["conductor_prompt"] is False

    asyncio.run(run_store())

    # Test that _build_agents_kwargs translates conductor_prompt=False to skip_conductor_prompt=True
    kwargs = bot._build_agents_kwargs({"conductor_prompt": False})
    assert kwargs.get("skip_conductor_prompt") is True, f"Expected skip_conductor_prompt=True, got {kwargs}"

    kwargs_on = bot._build_agents_kwargs({"conductor_prompt": True})
    assert kwargs_on.get("skip_conductor_prompt") is False or "skip_conductor_prompt" not in kwargs_on


@pytest.mark.asyncio
async def test_conductor_prompt_skipped_when_toggle_off(tmp_path):
    """run_engine with skip_conductor_prompt=True must NOT inject conductor directive even for fable."""
    captured_opts = {}

    class FakeClient:
        def __init__(self, options):
            captured_opts["opts"] = options

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch.object(bot, "ClaudeSDKClient", FakeClient), \
         patch.object(bot, "running", {}), \
         patch.object(bot, "audit", lambda *a: None):
        async for _ in bot.run_engine(
            project_name="test",
            cwd=str(tmp_path),
            prompt="hi",
            session_key="c:t",
            model="fable",
            skip_conductor_prompt=True,
        ):
            pass

    opts = captured_opts.get("opts")
    assert opts is not None
    sp = opts.system_prompt
    append_text = sp.get("append", "") if isinstance(sp, dict) else str(sp)
    assert bot.CONDUCTOR_PROMPT not in append_text, (
        f"CONDUCTOR_PROMPT must NOT be injected when skip_conductor_prompt=True: {append_text!r}"
    )


def test_build_agents_kwargs_model_override(tmp_path):
    """_build_agents_kwargs with executor_model='haiku' must return agents dict with overridden executor."""
    kwargs = bot._build_agents_kwargs({"executor_model": "haiku"})
    assert "agents" in kwargs
    agents = kwargs["agents"]
    assert "executor" in agents
    assert agents["executor"].model == "haiku"
    # researcher and quick should keep defaults
    assert agents.get("researcher") is not None
    assert agents.get("quick") is not None


def test_build_agents_kwargs_empty(tmp_path):
    """_build_agents_kwargs with empty config returns empty dict (use defaults)."""
    kwargs = bot._build_agents_kwargs({})
    assert kwargs == {}


def test_project_settings_view_includes_agents_config(tmp_path):
    """_project_settings_view must include agents_config key."""
    project = {
        "project": "proj",
        "cwd": str(tmp_path),
        "model": "fable",
        "agents_config": {"executor_model": "haiku", "conductor_prompt": True},
    }
    view = _webapp._project_settings_view(project)
    assert "agents_config" in view
    assert view["agents_config"]["executor_model"] == "haiku"
    assert view["agents_config"]["conductor_prompt"] is True


def test_project_settings_view_agents_config_absent():
    """_project_settings_view returns empty dict for agents_config when not set."""
    project = {"project": "proj", "cwd": "/tmp/x", "model": "sonnet"}
    view = _webapp._project_settings_view(project)
    assert view["agents_config"] == {}

"""Contract tests for the isolated Codex provider adapter."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import codex_engine


def _named(name: str, **attrs):
    instance = type(name, (), {})()
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


class _Dump:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **_kwargs):
        return self.data


def test_normalize_text_tool_subagent_error_and_rate_limit():
    text = _named("AgentMessageDeltaNotification", delta="hello")
    note = SimpleNamespace(payload=text)
    assert list(codex_engine.normalize_notification(note)) == [
        {"type": "text_delta", "text": "hello"}
    ]

    command = _named("CommandExecutionThreadItem", command="pwd", cwd=Path("/tmp"))
    started = _named("ItemStartedNotification", item=SimpleNamespace(root=command))
    assert list(codex_engine.normalize_notification(SimpleNamespace(payload=started)))[0]["name"] == "Bash"

    collab = _named(
        "CollabAgentToolCallThreadItem", receiver_thread_ids=["child-1"],
        status="completed", prompt="Inspect tests", id="call-1", tool="spawn_agent",
    )
    completed = _named("ItemCompletedNotification", item=SimpleNamespace(root=collab))
    subagent = list(codex_engine.normalize_notification(SimpleNamespace(payload=completed)))[0]
    assert subagent == {
        "type": "subagent", "subtype": "notification", "task_id": "child-1",
        "description": "Inspect tests", "status": "completed", "summary": None,
        "last_tool_name": None,
    }

    error = _named(
        "ErrorNotification", will_retry=False,
        error=SimpleNamespace(message="boom"),
    )
    err_event = list(codex_engine.normalize_notification(SimpleNamespace(payload=error)))[0]
    assert err_event["type"] == "error"
    assert str(err_event["exc"]) == "boom"

    snapshot = _Dump({"primary": {"usedPercent": 20}})
    rate = _named("AccountRateLimitsUpdatedNotification", rate_limits=snapshot)
    rate_event = list(codex_engine.normalize_notification(SimpleNamespace(payload=rate)))[0]
    assert rate_event["type"] == "rate_limit"
    assert rate_event["snapshot"]["primary"]["usedPercent"] == 20


class _FakeTurnHandle:
    def __init__(self, notifications):
        self.notifications = notifications
        self.interrupt_calls = 0

    async def stream(self):
        for notification in self.notifications:
            yield notification

    async def interrupt(self):
        self.interrupt_calls += 1


class _FakeThread:
    def __init__(self, thread_id, notifications):
        self.id = thread_id
        self.handle = _FakeTurnHandle(notifications)
        self.turn_kwargs = None

    async def turn(self, prompt, **kwargs):
        self.turn_kwargs = {"prompt": prompt, **kwargs}
        return self.handle


class _FakeCodex:
    instances = []
    notifications = []

    def __init__(self):
        self.started = None
        self.resumed = None
        self.closed = False
        self.thread = None
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def account(self):
        return _Dump({"account": {"type": "chatgpt", "planType": "team"}})

    async def thread_start(self, **kwargs):
        self.started = kwargs
        self.thread = _FakeThread("thread-new-123", self.notifications)
        return self.thread

    async def thread_resume(self, thread_id, **kwargs):
        self.resumed = {"thread_id": thread_id, **kwargs}
        self.thread = _FakeThread(thread_id, self.notifications)
        return self.thread

    async def close(self):
        self.closed = True


def _adapter_notifications():
    agent = _named("AgentMessageThreadItem", text="final answer")
    item = _named("ItemCompletedNotification", item=SimpleNamespace(root=agent))
    breakdown = SimpleNamespace(
        input_tokens=11, output_tokens=7, cached_input_tokens=3,
        reasoning_output_tokens=2, total_tokens=23,
    )
    usage = SimpleNamespace(last=breakdown, model_context_window=200_000)
    usage_note = _named("ThreadTokenUsageUpdatedNotification", token_usage=usage)
    completed = _named("TurnCompletedNotification", turn=SimpleNamespace(duration_ms=125))
    return [SimpleNamespace(payload=item), SimpleNamespace(payload=usage_note), SimpleNamespace(payload=completed)]


@pytest.mark.asyncio
async def test_run_start_resume_usage_and_interrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_ENABLED", "true")
    _FakeCodex.instances.clear()
    _FakeCodex.notifications = _adapter_notifications()
    monkeypatch.setattr(
        codex_engine, "_sdk",
        lambda: (_FakeCodex, SimpleNamespace(deny_all="deny_all"), SimpleNamespace(full_access="full", read_only="read")),
    )
    ctx = {"running": {}, "DATA": tmp_path}

    gen = codex_engine.run_codex_engine(
        project_name="demo", cwd=str(tmp_path), prompt="do it", session_key="chat:demo",
        model="gpt-test", effort="high", ctx=ctx,
    )
    first = await anext(gen)
    assert first == {"type": "text", "text": "final answer"}
    handle = ctx["running"]["chat:demo"]
    await handle.interrupt()
    events = [first]
    async for event in gen:
        events.append(event)

    instance = _FakeCodex.instances[-1]
    assert instance.started["approval_mode"] == "deny_all"
    assert instance.started["sandbox"] == "full"
    assert "$HOME/CLAUDE.md" in instance.started["developer_instructions"]
    assert handle.interrupt_calls == 1
    assert events[-1]["type"] == "result"
    assert events[-1]["thread_id"] == "thread-new-123"
    assert events[-1]["usage"]["total_tokens"] == 23
    assert instance.closed is True
    assert (tmp_path / "codex_usage.jsonl").is_file()

    resumed = [event async for event in codex_engine.run_codex_engine(
        project_name="demo", cwd=str(tmp_path), prompt="continue", session_key="chat:demo",
        resume_thread_id="thread-existing-9", ctx=ctx, plan_mode=True,
    )]
    resumed_instance = _FakeCodex.instances[-1]
    assert resumed_instance.resumed["thread_id"] == "thread-existing-9"
    assert resumed_instance.resumed["sandbox"] == "read"
    assert resumed[-1]["thread_id"] == "thread-existing-9"


@pytest.mark.asyncio
async def test_disabled_adapter_rejects_without_importing_sdk(monkeypatch):
    monkeypatch.setenv("CODEX_ENABLED", "false")
    monkeypatch.setattr(codex_engine, "_sdk", lambda: (_ for _ in ()).throw(AssertionError("must stay lazy")))
    events = [event async for event in codex_engine.run_codex_engine(
        project_name="demo", cwd="/tmp", prompt="hello", session_key="demo",
    )]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "CODEX_ENABLED=false" in str(events[0]["exc"])

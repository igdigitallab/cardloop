"""spec-089 §6 — dead-agent detector from `background_tasks_changed` (between turns only).

A child killed by TTL eviction / restart / OOM left its row `running` until the sweeper's
staleness flip. The CLI emits `background_tasks_changed` (plain SystemMessage, `.data.tasks[]`,
REPLACE semantics) after every change — between turns that list is the live set, so any running
stream-fed agent row missing from it is dead and flips stopped(stale) immediately.
"""
import asyncio

import pytest

import engine
import webapp
from claude_agent_sdk.types import SystemMessage, TaskStartedMessage


def _started(task_id, desc="agent"):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=task_id,
                              description=desc, uuid="u", session_id="sid",
                              tool_use_id="tu-" + task_id, task_type="local_agent")


def _changed(*ids, key="task_id"):
    return SystemMessage(subtype="background_tasks_changed",
                         data={"tasks": [{key: i, "type": "local_agent"} for i in ids]})


# ─────────────────────────── pure helper ───────────────────────────

def test_missing_rows_flip_stopped_stale(monkeypatch):
    monkeypatch.setattr(engine, "_running_stream_agents_cb",
                        lambda sk: {"b1": 100.0, "b2": 100.0})
    out = engine._gone_agent_deltas("s", _changed("b2"), now=200.0)
    assert out == [{"id": "b1", "status": "stopped", "stale": True,
                    "tail": "(gone from the CLI task list)"}]


def test_accepts_id_key_and_ignores_non_dict_entries(monkeypatch):
    monkeypatch.setattr(engine, "_running_stream_agents_cb", lambda sk: {"b1": 100.0})
    msg = SystemMessage(subtype="background_tasks_changed",
                        data={"tasks": [{"id": "b1"}, "junk", None]})
    assert engine._gone_agent_deltas("s", msg, now=200.0) == []


def test_no_task_list_means_no_verdict(monkeypatch):
    monkeypatch.setattr(engine, "_running_stream_agents_cb", lambda sk: {"b1": 100.0})
    assert engine._gone_agent_deltas("s", SystemMessage(subtype="background_tasks_changed",
                                                        data={}), now=200.0) == []
    assert engine._gone_agent_deltas("s", SystemMessage(subtype="background_tasks_changed",
                                                        data={"tasks": None}), now=200.0) == []
    # An EMPTY list is a real verdict: everything is gone.
    assert [d["id"] for d in engine._gone_agent_deltas("s", _changed(), now=200.0)] == ["b1"]


def test_no_snapshot_callback_is_a_noop(monkeypatch):
    monkeypatch.setattr(engine, "_running_stream_agents_cb", None)
    assert engine._gone_agent_deltas("s", _changed(), now=200.0) == []


def test_grace_window_protects_a_row_that_just_started(monkeypatch):
    monkeypatch.setattr(engine, "_running_stream_agents_cb",
                        lambda sk: {"young": 199.0, "old": 100.0})
    out = engine._gone_agent_deltas("s", _changed(), now=200.0)
    assert [d["id"] for d in out] == ["old"]


def test_helper_never_raises(monkeypatch):
    monkeypatch.setattr(engine, "_running_stream_agents_cb",
                        lambda sk: (_ for _ in ()).throw(RuntimeError("boom")))
    assert engine._gone_agent_deltas("s", _changed("x")) == []


# ─────────────────────────── registry snapshot ───────────────────────────

@pytest.fixture
def quiet_registry(monkeypatch):
    monkeypatch.setattr(webapp, "_bus_publish", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_crash_state_mark_dirty", lambda: None)
    monkeypatch.setattr(webapp, "_schedule_completion_wake", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_monitors", {})
    return webapp._monitors


def test_snapshot_only_running_stream_agent_rows(quiet_registry):
    sk = "s"
    webapp._monitor_update(sk, {"id": "b1", "kind": "agent", "status": "running",
                                "label": "x", "stream": True})
    webapp._monitor_update(sk, {"id": "hook", "kind": "agent", "status": "running", "label": "y"})
    webapp._monitor_update(sk, {"id": "wf", "kind": "workflow", "status": "running", "label": "z"})
    webapp._monitor_update(sk, {"id": "b2", "kind": "agent", "status": "running",
                                "label": "w", "stream": True})
    webapp._monitor_update(sk, {"id": "b2", "status": "done"}, only_existing=True)
    snap = webapp._running_stream_agents(sk)
    assert set(snap) == {"b1"}
    assert snap["b1"] == pytest.approx(quiet_registry[sk]["b1"]["started"])
    assert webapp._running_stream_agents("other") == {}


# ─────────────────────────── between turns (drain), end to end ───────────────────────────

class _FakeDrainClient:
    def __init__(self, messages):
        self._messages = messages
        self._blocker = asyncio.Event()

    async def receive_messages(self):
        for m in self._messages:
            yield m
        await self._blocker.wait()


@pytest.mark.asyncio
async def test_drain_flips_the_row_the_cli_dropped(quiet_registry, monkeypatch):
    # Real registry + real snapshot callback, grace disabled (rows were created milliseconds ago).
    monkeypatch.setattr(engine, "_monitor_update_cb", webapp._monitor_update)
    monkeypatch.setattr(engine, "_running_stream_agents_cb", webapp._running_stream_agents)
    monkeypatch.setattr(engine, "_GONE_AGENT_GRACE_SEC", 0.0)
    monkeypatch.setattr(engine, "_bg_run_cb", lambda *a, **k: None)
    wakes = []
    monkeypatch.setattr(webapp, "_schedule_completion_wake", lambda sk, rec: wakes.append(rec["id"]))
    msgs = [_started("b1"), _started("b2"), _changed("b2")]
    entry = engine._LiveEntry(client=_FakeDrainClient(msgs), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    b1, b2 = quiet_registry["s"]["b1"], quiet_registry["s"]["b2"]
    assert b1["status"] == "stopped" and b1["stale"] is True
    assert b1["tail"] == "(gone from the CLI task list)"
    assert b2["status"] == "running"
    # A stale stopped-flip is one the orchestrator never saw → it wakes (spec-088 rule).
    assert wakes == ["b1"]


@pytest.mark.asyncio
async def test_drain_never_creates_phantom_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_monitor_update_cb",
                        lambda sk, d, only_existing=False: calls.append((d, only_existing)))
    monkeypatch.setattr(engine, "_running_stream_agents_cb", lambda sk: {"ghost": 0.0})
    monkeypatch.setattr(engine, "_bg_run_cb", lambda *a, **k: None)
    entry = engine._LiveEntry(client=_FakeDrainClient([_changed("b9")]), fingerprint="f",
                              last_used=0.0, idle_task=None, session_key="s")
    engine._start_drain(entry, None)
    await asyncio.sleep(0.05)
    await engine._stop_drain(entry)
    assert calls == [({"id": "ghost", "status": "stopped", "stale": True,
                       "tail": "(gone from the CLI task list)"}, True)]


def test_in_turn_processing_never_diffs_the_task_list():
    """The list omits FOREGROUND agents — a mid-turn diff would kill live rows. Guard the
    wiring, not just the docstring: the helper is referenced from the drain only."""
    import inspect
    src = inspect.getsource(engine)
    body = src[src.index("async def _process_messages"):]
    body = body[:body.index("\nasync def ") if "\nasync def " in body[1:] else None]
    assert "_gone_agent_deltas" not in body

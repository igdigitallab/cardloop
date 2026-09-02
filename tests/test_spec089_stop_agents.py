"""
Tests for spec-089 §1: POST /api/projects/{id}/agents/stop ("⏹ Stop agents").

There is no server-side kill for a sub-agent/Workflow task — only the model's own TaskStop
tool reaches it. The endpoint optimistically flips every running agent/workflow monitor row
to 'stopping', then either steers a synthetic TaskStop instruction into the in-flight turn
or enqueues+drains it like the auto-continue wake-up. A 60s follow-up resolves any row the
CLI never confirms, without waking the orchestrator (a stop is not a completion).

Covers:
- no running rows -> {"ok": true, "stopped": [], "via": "none"} (second click is a no-op)
- a running turn on the live client -> rows flip to 'stopping', the steer receives the ids
  and the _AGENT_STOP_PREFIX text
- no running turn -> the instruction is enqueued (_AGENT_STOP_PREFIX-prefixed) and
  _chat_queue_drain_one is awaited
- the instruction id list is bounded to 40 in the text, but every row is still flipped/returned
- _display_prompt hides a _AGENT_STOP_PREFIX prompt whole (never a human bubble)
- _agents_stop_followup resolves a leftover 'stopping' row to 'stopped' with NO 'stale' flag
- _sweep_stale_monitors leaves a fresh 'stopping' row alone (but still catches one gone stale)
- _monitor_update: stopping -> done schedules a completion wake; stopping -> stopped does not
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── Fakes ────────────────────────────────────────────


class _FakeClient:
    """Stands in for a connected ClaudeSDKClient: records query() calls."""

    def __init__(self):
        self.queries: list = []

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)


class _FakeEntry:
    """Stands in for engine._LiveEntry — only .client is read by the steer guard."""

    def __init__(self, client):
        self.client = client


def _row(mid: str, kind: str = "agent", status: str = "running") -> dict:
    return {"id": mid, "kind": kind, "label": mid, "status": status,
            "started": 0.0, "ts": 0.0, "tail": "", "agent": None}


# ─────────────────────────── Fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate the chat queue / live turns / monitor registry between tests, and silence the
    side-effect hooks _monitor_update fans out to (mirrors quiet_registry in
    tests/test_spec088_p1_agent_rows.py + reset_state in tests/test_chat_steer.py)."""
    old_file = _webapp._CHAT_QUEUE_FILE
    old_queue = dict(_webapp._CHAT_QUEUE)
    old_turns = dict(_webapp._live_turns)
    old_monitors = dict(_webapp._monitors)
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = tmp_path / "chat-queue.json"
    _webapp._live_turns.clear()
    _webapp._monitors.clear()
    monkeypatch.setattr(_webapp, "_bus_publish", lambda *a, **k: None)
    monkeypatch.setattr(_webapp, "_crash_state_mark_dirty", lambda: None)
    monkeypatch.setattr(_webapp, "_schedule_completion_wake", lambda *a, **k: None)
    monkeypatch.setattr(_webapp, "_timeline_append", lambda sk, ev: None)
    yield
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old_queue)
    _webapp._CHAT_QUEUE_FILE = old_file
    _webapp._live_turns.clear()
    _webapp._live_turns.update(old_turns)
    _webapp._monitors.clear()
    _webapp._monitors.update(old_monitors)


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (tmp_path / "myproject").mkdir(exist_ok=True)
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
        "live_clients": {},
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    return ctx


@pytest.fixture
def stop_app(fake_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/agents/stop", _webapp.api_project_agents_stop)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _arm_live_turn(fake_ctx, session_key="1001:42"):
    """Puts the session into 'running on the live client' state. Returns the fake client."""
    client = _FakeClient()
    fake_ctx["running"][session_key] = client
    fake_ctx["live_clients"][session_key] = _FakeEntry(client)
    return client


# ─────────────────────────── POST /api/projects/{id}/agents/stop ──────────────


@pytest.mark.asyncio
async def test_no_running_rows_is_noop(aiohttp_client, fake_ctx, stop_app):
    client = await aiohttp_client(stop_app)
    resp = await client.post("/api/projects/myproject/agents/stop", headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "stopped": [], "via": "none"}


@pytest.mark.asyncio
async def test_unknown_project_404(aiohttp_client, fake_ctx, stop_app):
    client = await aiohttp_client(stop_app)
    resp = await client.post("/api/projects/nope/agents/stop", headers=_auth_headers(fake_ctx))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_busy_turn_steers_stop_instruction(aiohttp_client, fake_ctx, stop_app, monkeypatch):
    """A running turn on the live client -> rows flip to 'stopping', the steer carries the
    ids and the _AGENT_STOP_PREFIX text (spec-089 §1 step 5)."""
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda c: c.close())  # follow-up not under test here
    sk = "1001:42"
    _webapp._monitors[sk] = {"a1": _row("a1", kind="agent"), "wf1": _row("wf1", kind="workflow")}
    fake_client = _arm_live_turn(fake_ctx, sk)

    client = await aiohttp_client(stop_app)
    resp = await client.post("/api/projects/myproject/agents/stop", headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body["via"] == "steer"
    assert sorted(body["stopped"]) == ["a1", "wf1"]
    assert _webapp._monitors[sk]["a1"]["status"] == "stopping"
    assert _webapp._monitors[sk]["wf1"]["status"] == "stopping"
    assert len(fake_client.queries) == 1
    steered = fake_client.queries[0]
    assert steered.startswith(_webapp._AGENT_STOP_PREFIX)
    assert "a1" in steered and "wf1" in steered
    assert "TaskStop" in steered


@pytest.mark.asyncio
async def test_idle_enqueues_and_drains(aiohttp_client, fake_ctx, stop_app, monkeypatch):
    """No running turn -> the instruction is enqueued and _chat_queue_drain_one is awaited
    (spec-089 §1 step 6, the idle path _completion_wake_fire also uses)."""
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda c: c.close())
    sk = "1001:42"
    _webapp._monitors[sk] = {"a1": _row("a1", kind="agent")}
    drained = []

    async def _fake_drain(ctx, session_key):
        drained.append(session_key)
        return False

    monkeypatch.setattr(_webapp, "_chat_queue_drain_one", _fake_drain)

    client = await aiohttp_client(stop_app)
    resp = await client.post("/api/projects/myproject/agents/stop", headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body["via"] == "queue"
    assert body["stopped"] == ["a1"]
    assert drained == [sk]
    items = _webapp._chat_queue_get(sk)
    assert len(items) == 1
    assert items[0]["text"].startswith(_webapp._AGENT_STOP_PREFIX)
    assert "a1" in items[0]["text"]
    assert _webapp._monitors[sk]["a1"]["status"] == "stopping"


@pytest.mark.asyncio
async def test_instruction_text_bounded_to_40_ids(aiohttp_client, fake_ctx, stop_app, monkeypatch):
    """Every target row is flipped/returned regardless of count, but the model-facing
    instruction text names at most 40 ids (spec-089 §1 step 4)."""
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda c: c.close())
    sk = "1001:42"
    ids = [f"a{i}" for i in range(45)]
    _webapp._monitors[sk] = {i: _row(i, kind="agent") for i in ids}

    async def _fake_drain(ctx, session_key):
        return False

    monkeypatch.setattr(_webapp, "_chat_queue_drain_one", _fake_drain)

    client = await aiohttp_client(stop_app)
    resp = await client.post("/api/projects/myproject/agents/stop", headers=_auth_headers(fake_ctx))
    body = await resp.json()
    assert sorted(body["stopped"]) == sorted(ids)
    assert all(_webapp._monitors[sk][i]["status"] == "stopping" for i in ids)
    items = _webapp._chat_queue_get(sk)
    text = items[0]["text"]
    named = sum(1 for i in ids if i in text.split(": ", 1)[1].split(", "))
    assert named == 40


# ─────────────────────────── _display_prompt ──────────────────────────────────


def test_display_prompt_hides_agent_stop_prefix():
    text = f"{_webapp._AGENT_STOP_PREFIX} Call TaskStop for a1, a2, then stop."
    assert _webapp._display_prompt(text) == ""


def test_display_prompt_untouched_for_human_text():
    assert _webapp._display_prompt("please stop the agents") == "please stop the agents"


# ─────────────────────────── _agents_stop_followup ─────────────────────────────


@pytest.mark.asyncio
async def test_followup_resolves_unconfirmed_stopping_rows(monkeypatch):
    sk = "s"
    _webapp._monitors[sk] = {
        "a1": _row("a1", status="stopping"),
        "a2": _row("a2", status="done"),  # the CLI already confirmed something else — untouched
    }
    monkeypatch.setattr(_webapp, "_AGENTS_STOP_FOLLOWUP_SEC", 0)
    await _webapp._agents_stop_followup(sk, ["a1", "a2"])
    assert _webapp._monitors[sk]["a1"]["status"] == "stopped"
    assert _webapp._monitors[sk]["a1"].get("stale") is not True
    assert _webapp._monitors[sk]["a1"]["tail"] == "(stop requested — the CLI never confirmed)"
    assert _webapp._monitors[sk]["a2"]["status"] == "done"


@pytest.mark.asyncio
async def test_followup_does_not_wake(monkeypatch):
    """The follow-up's stopping -> stopped flip must not re-arm the completion wake."""
    sk = "s"
    woken = []
    monkeypatch.setattr(_webapp, "_schedule_completion_wake", lambda *a: woken.append(a))
    _webapp._monitors[sk] = {"a1": _row("a1", status="stopping")}
    monkeypatch.setattr(_webapp, "_AGENTS_STOP_FOLLOWUP_SEC", 0)
    await _webapp._agents_stop_followup(sk, ["a1"])
    assert woken == []


# ─────────────────────────── _sweep_stale_monitors ─────────────────────────────


def test_sweep_stale_leaves_fresh_stopping_row_alone():
    sk = "s"
    now = time.time()
    _webapp._monitors[sk] = {
        "a1": {**_row("a1", kind="agent", status="stopping"), "stream": True, "ts": now - 5},
    }
    _webapp._sweep_stale_monitors(now)
    assert _webapp._monitors[sk]["a1"]["status"] == "stopping"


def test_sweep_stale_still_catches_a_long_abandoned_stopping_row():
    """Safety net: if the 60s follow-up somehow never ran, the ordinary staleness sweep
    still resolves the row eventually (well past the follow-up's own window)."""
    sk = "s"
    now = time.time()
    _webapp._monitors[sk] = {
        "a1": {**_row("a1", kind="agent", status="stopping"), "stream": True,
               "ts": now - _webapp._MONITOR_STALE_SEC - 5},
    }
    _webapp._sweep_stale_monitors(now)
    assert _webapp._monitors[sk]["a1"]["status"] == "stopped"
    assert _webapp._monitors[sk]["a1"]["stale"] is True


# ─────────────────────────── _monitor_update wake semantics ────────────────────


def test_monitor_update_running_to_stopping_does_not_wake(monkeypatch):
    sk = "s"
    woken = []
    monkeypatch.setattr(_webapp, "_schedule_completion_wake", lambda *a: woken.append(a))
    _webapp._monitors[sk] = {"a1": _row("a1", status="running")}
    _webapp._monitor_update(sk, {"id": "a1", "status": "stopping", "tail": "stop requested"},
                            only_existing=True)
    assert woken == []
    assert _webapp._monitors[sk]["a1"]["status"] == "stopping"


def test_monitor_update_stopping_to_done_wakes(monkeypatch):
    """The agent finished on its own before the stop landed — a real result, keep it."""
    sk = "s"
    woken = []
    monkeypatch.setattr(_webapp, "_schedule_completion_wake", lambda *a: woken.append(a))
    _webapp._monitors[sk] = {"a1": _row("a1", status="stopping")}
    _webapp._monitor_update(sk, {"id": "a1", "status": "done"}, only_existing=True)
    assert len(woken) == 1


def test_monitor_update_stopping_to_stopped_does_not_wake(monkeypatch):
    sk = "s"
    woken = []
    monkeypatch.setattr(_webapp, "_schedule_completion_wake", lambda *a: woken.append(a))
    _webapp._monitors[sk] = {"a1": _row("a1", status="stopping")}
    _webapp._monitor_update(sk, {"id": "a1", "status": "stopped"}, only_existing=True)
    assert woken == []

"""
Tests for spec-035 Live Trace:
- L0: LiveTurn in-memory structure
- L1: api_project_chat publishes seq-tagged events to bus and live turn buffer
- L2: replay via Last-Event-ID / ?since= query param (buffer-level logic)
- L3: GET /api/projects/{id}/live snapshot endpoint
"""
import sys
import json
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


def _make_chat_ctx(tmp_path, project_dir, run_engine=None):
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": run_engine,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


def _make_app(ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    app.router.add_get("/api/projects/{id}/activity-stream", _webapp.api_project_activity_stream)
    app.router.add_get("/api/projects/{id}/live", _webapp.api_project_live)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── helpers ────────────────────────────


def _cleanup_live_turns():
    """Remove all live turns to avoid cross-test pollution."""
    _webapp._live_turns.clear()


# ─────────────────────────── test 1: bus receives seq-tagged events ───────────


async def test_live_trace_events_published_to_bus(aiohttp_client, tmp_path, project_dir):
    """Chat turn publishes seq-tagged events to the bus (spec-035 L1)."""
    _cleanup_live_turns()

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    session_key = "1001:42"
    q = _webapp._bus_subscribe(session_key)

    events_received = []
    try:
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth_headers(ctx)
        resp = await client.post("/api/projects/myproject/chat", json={"prompt": "hi"}, headers=h)
        await resp.read()
        while not q.empty():
            events_received.append(q.get_nowait())
    finally:
        _webapp._bus_unsubscribe(session_key, q)

    assert len(events_received) > 0, "Bus must receive events"
    # run_start / run_end are lifecycle bus events published outside the live-turn buffer;
    # they do not carry a seq. Only live-turn events (type=text/result/tool/…) must have seq.
    live_turn_events = [e for e in events_received if "seq" in e]
    assert len(live_turn_events) > 0, f"Must receive seq-tagged live-turn events: {events_received}"
    seqs = [e["seq"] for e in live_turn_events]
    assert seqs == sorted(seqs), "seq must be monotonically increasing"
    assert seqs == list(range(seqs[0], seqs[-1] + 1)), "seq must have no gaps"


# ─────────────────────────── test 2: replay via cursor ──────────────────────


async def test_live_trace_replay_on_reconnect(aiohttp_client, tmp_path, project_dir):
    """After a turn, the LiveTurn buffer contains all events with contiguous seqs (spec-035 L2)."""
    _cleanup_live_turns()

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "a"}
        yield {"type": "text", "text": "b"}
        yield {"type": "text", "text": "c"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    session_key = "1001:42"

    app = _make_app(ctx)
    client = await aiohttp_client(app)
    h = _auth_headers(ctx)

    # Run a full chat turn
    resp = await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)
    await resp.read()

    # LiveTurn should be retained after completion
    turn = _webapp._live_turns.get(session_key)
    assert turn is not None, "LiveTurn should be retained after completion"
    assert turn["status"] == "done"
    all_events = list(turn["events"])
    assert len(all_events) > 0

    # All events must have seq, no gaps
    seqs = [e["seq"] for e in all_events]
    assert seqs == sorted(seqs), "seqs must be sorted"
    assert seqs == list(range(seqs[0], seqs[-1] + 1)), f"No gaps in seqs: {seqs}"

    # Replay from cursor = first seq (should get all but first event)
    cursor = seqs[0]
    replayed = [e for e in all_events if e["seq"] > cursor]
    assert len(replayed) == len(all_events) - 1
    if len(replayed) > 0:
        assert replayed[0]["seq"] == seqs[1]


# ─────────────────────────── test 3: /live started_at stability ──────────────


async def test_live_snapshot_started_at_stable(aiohttp_client, tmp_path, project_dir):
    """GET /live returns stable started_at and turn_id within one turn (spec-035 L3)."""
    _cleanup_live_turns()

    engine_started = asyncio.Event()
    engine_can_finish = asyncio.Event()

    async def slow_engine(**kwargs):
        engine_started.set()
        await engine_can_finish.wait()
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=slow_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)
    h = _auth_headers(ctx)

    # Start the chat in background
    chat_task = asyncio.create_task(
        client.post("/api/projects/myproject/chat", json={"prompt": "test"}, headers=h)
    )

    # Wait for engine to start so the turn is active
    await engine_started.wait()

    # Poll /live twice; started_at and turn_id must be identical both calls
    resp1 = await client.get("/api/projects/myproject/live", headers=h)
    data1 = await resp1.json()

    await asyncio.sleep(0.05)

    resp2 = await client.get("/api/projects/myproject/live", headers=h)
    data2 = await resp2.json()

    engine_can_finish.set()
    resp_chat = await chat_task
    await resp_chat.read()

    assert data1["running"] is True, "turn should be running at first poll"
    assert data1["started_at"] is not None, "started_at must be set"
    assert data1["started_at"] == data2["started_at"], "started_at must be stable across calls"
    assert data1["turn_id"] == data2["turn_id"], "turn_id must be stable"


# ─────────────────────────── test 4: /live cold open returns full buffer ──────


async def test_live_snapshot_cold_open(aiohttp_client, tmp_path, project_dir):
    """GET /live after a completed turn returns full events + correct cursor (spec-035 L3)."""
    _cleanup_live_turns()

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "hello"}
        yield {"type": "tool", "name": "Bash", "input": {"command": "ls"}}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    app = _make_app(ctx)
    client = await aiohttp_client(app)
    h = _auth_headers(ctx)

    # Run the turn
    resp = await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)
    await resp.read()

    # Cold open snapshot
    resp_live = await client.get("/api/projects/myproject/live", headers=h)
    assert resp_live.status == 200
    data = await resp_live.json()

    assert data["started_at"] is not None, "started_at must be present"
    assert data["turn_id"] is not None, "turn_id must be present"
    assert "cursor" in data
    assert "events" in data
    assert len(data["events"]) > 0, "Must have buffered events"

    # cursor == max seq
    seqs = [e["seq"] for e in data["events"]]
    assert data["cursor"] == max(seqs), "cursor should be the latest seq"

    # No gaps in seqs
    assert seqs == list(range(min(seqs), max(seqs) + 1)), f"No seq gaps: {seqs}"


# ─────────────────────────── test 5: subagent events on bus ──────────────────


async def test_live_trace_subagent_events_on_bus(aiohttp_client, tmp_path, project_dir):
    """Subagent events from run_engine appear on the bus with seq during a chat turn (spec-035 L1)."""
    _cleanup_live_turns()

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "orchestrating..."}
        yield {
            "type": "subagent",
            "subtype": "started",
            "task_id": "task-1",
            "description": "Run tests",
            "status": "running",
            "last_tool_name": None,
        }
        yield {
            "type": "subagent",
            "subtype": "progress",
            "task_id": "task-1",
            "description": "Run tests",
            "status": "running",
            "last_tool_name": "Bash",
        }
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_chat_ctx(tmp_path, project_dir, run_engine=fake_engine)
    session_key = "1001:42"
    q = _webapp._bus_subscribe(session_key)

    bus_events = []
    try:
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth_headers(ctx)
        resp = await client.post("/api/projects/myproject/chat", json={"prompt": "do it"}, headers=h)
        await resp.read()
        while not q.empty():
            bus_events.append(q.get_nowait())
    finally:
        _webapp._bus_unsubscribe(session_key, q)

    subagent_events = [e for e in bus_events if e.get("type") == "subagent"]
    assert len(subagent_events) >= 2, f"Expected >=2 subagent events on bus: {subagent_events}"

    # All should have seq
    assert all("seq" in e for e in subagent_events), "All subagent events must have seq"

    # Check last_tool_name is carried through
    progress = next((e for e in subagent_events if e.get("subtype") == "progress"), None)
    assert progress is not None, "Should have a progress subagent event"
    assert progress.get("last_tool_name") == "Bash", f"last_tool_name mismatch: {progress}"


# ─────────────────────────── test 6: /live unknown project returns 404 ───────


async def test_live_snapshot_unknown_project(aiohttp_client, tmp_path, project_dir):
    """GET /live for unknown project returns 404 (spec-035 L3)."""
    _cleanup_live_turns()

    ctx = _make_chat_ctx(tmp_path, project_dir)
    app = _make_app(ctx)
    client = await aiohttp_client(app)
    h = _auth_headers(ctx)

    resp = await client.get("/api/projects/nonexistent/live", headers=h)
    assert resp.status == 404


# ─────────────────────────── test 7: /live no active turn returns zeros ──────


async def test_live_snapshot_no_turn(aiohttp_client, tmp_path, project_dir):
    """GET /live when no turn has run returns null fields + cursor=0 (spec-035 L3)."""
    _cleanup_live_turns()

    ctx = _make_chat_ctx(tmp_path, project_dir)
    app = _make_app(ctx)
    client = await aiohttp_client(app)
    h = _auth_headers(ctx)

    resp = await client.get("/api/projects/myproject/live", headers=h)
    assert resp.status == 200
    data = await resp.json()

    assert data["turn_id"] is None
    assert data["started_at"] is None
    assert data["cursor"] == 0
    assert data["events"] == []
    assert data["running"] is False

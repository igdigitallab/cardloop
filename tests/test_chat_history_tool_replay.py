"""
Tests for ops:51a612 — chat history tool replay (bug A) and queue persistence (bug B).

Bug A: tool events in the live turn buffer lacked the `kind` field, so replaying via
GET /live after a tab-switch or browser refresh rendered bare tool names instead of
rich detail (command, file path, diff, etc.).

Fix A: the api_project_chat handler now buffers the _format_tool result (rich, with
`kind`) rather than the raw engine event for tool events.  Live SSE and cold-open
replay are now identical: both carry {type, name, kind, ...} shaped events.

Bug B: _CHAT_QUEUE was in-memory only; queued messages were lost on browser refresh
(server persisted across refresh, so `GET /chat/queue` returned the surviving items —
but a server restart cleared the dict entirely).

Fix B: _chat_queue_init() loads DATA/chat-queue.json on startup; every mutation
(_chat_queue_enqueue / _pop / _edit / _delete) atomically flushes to disk.
"""
import json
import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── shared fixtures ─────────────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


def _make_ctx(tmp_path, project_dir, run_engine=None):
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
    app.router.add_get("/api/projects/{id}/live", _webapp.api_project_live)
    app.router.add_get("/api/projects/{id}/chat/queue", _webapp.api_chat_queue_list)
    app.router.add_post("/api/projects/{id}/chat/queue", _webapp.api_chat_queue_add)
    app.router.add_route("PATCH", "/api/projects/{id}/chat/queue/{msg_id}", _webapp.api_chat_queue_edit)
    app.router.add_delete("/api/projects/{id}/chat/queue/{msg_id}", _webapp.api_chat_queue_delete)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _cleanup():
    _webapp._live_turns.clear()
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = None


# ══════════════════════════════════════════════════════════════════════════════
# Bug A — tool event shape in the live turn buffer
# ══════════════════════════════════════════════════════════════════════════════


class TestBugAToolReplayShape:
    """The live turn buffer must store rich tool events (with `kind`) identical to SSE output."""

    def setup_method(self):
        _cleanup()

    # ── A1: Bash tool event buffered with kind='bash' ─────────────────────────

    async def test_bash_tool_event_has_kind_in_buffer(self, aiohttp_client, tmp_path, project_dir):
        """Bash tool events in the live buffer carry kind='bash' and cmd field."""
        async def fake_engine(**kwargs):
            yield {"type": "tool", "name": "Bash", "input": {"command": "ls -la", "description": "list"}}
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        resp = await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)
        await resp.read()

        turn = _webapp._live_turns.get("1001:42")
        assert turn is not None

        tool_events = [e for e in turn["events"] if e.get("type") == "tool"]
        assert len(tool_events) == 1, f"Expected 1 tool event, got: {tool_events}"
        ev = tool_events[0]

        # Must have kind — the field that drives ToolBlock rendering
        assert "kind" in ev, f"Tool event in live buffer must have 'kind', got: {ev}"
        assert ev["kind"] == "bash"
        assert "cmd" in ev, f"Bash tool event must have 'cmd', got: {ev}"
        assert ev["cmd"] == "ls -la"
        # Raw `input` dict must NOT be present (we store the formatted shape)
        assert "input" not in ev, f"Formatted tool event must not carry raw 'input': {ev}"

    # ── A2: Read tool event buffered with kind='read' ─────────────────────────

    async def test_read_tool_event_has_kind_in_buffer(self, aiohttp_client, tmp_path, project_dir):
        """Read tool events in the live buffer carry kind='read' and file field."""
        async def fake_engine(**kwargs):
            yield {"type": "tool", "name": "Read", "input": {"file_path": "/home/igor/TASKS.md"}}
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        resp = await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)
        await resp.read()

        turn = _webapp._live_turns.get("1001:42")
        assert turn is not None
        tool_events = [e for e in turn["events"] if e.get("type") == "tool"]
        assert len(tool_events) == 1
        ev = tool_events[0]

        assert ev["kind"] == "read"
        assert ev["file"] == "/home/igor/TASKS.md"
        assert "input" not in ev

    # ── A3: Edit tool event buffered with kind='edit' ─────────────────────────

    async def test_edit_tool_event_has_kind_in_buffer(self, aiohttp_client, tmp_path, project_dir):
        """Edit tool events in the live buffer carry kind='edit' with file/old/new fields."""
        async def fake_engine(**kwargs):
            yield {
                "type": "tool", "name": "Edit",
                "input": {
                    "file_path": "/tmp/x.py",
                    "old_string": "a = 1",
                    "new_string": "a = 2",
                },
            }
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        resp = await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)
        await resp.read()

        turn = _webapp._live_turns.get("1001:42")
        tool_events = [e for e in turn["events"] if e.get("type") == "tool"]
        assert len(tool_events) == 1
        ev = tool_events[0]

        assert ev["kind"] == "edit"
        assert ev["file"] == "/tmp/x.py"
        assert "input" not in ev

    # ── A4: GET /live returns formatted tool events ───────────────────────────

    async def test_live_endpoint_returns_formatted_tool_events(self, aiohttp_client, tmp_path, project_dir):
        """GET /live snapshot must return tool events with `kind` field (A = live SSE and replay are identical)."""
        async def fake_engine(**kwargs):
            yield {"type": "tool", "name": "Bash", "input": {"command": "echo hi"}}
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        await (await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)).read()

        resp = await client.get("/api/projects/myproject/live", headers=h)
        data = await resp.json()

        tool_events = [e for e in data["events"] if e.get("type") == "tool"]
        assert len(tool_events) >= 1
        ev = tool_events[0]
        assert "kind" in ev, f"GET /live must return tool events with 'kind': {ev}"
        assert ev["kind"] == "bash"
        assert "cmd" in ev

    # ── A5: Non-tool events still buffered as-is ──────────────────────────────

    async def test_non_tool_events_buffered_unchanged(self, aiohttp_client, tmp_path, project_dir):
        """text and result events must still reach the live buffer unchanged."""
        async def fake_engine(**kwargs):
            yield {"type": "text", "text": "hello world"}
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        await (await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)).read()

        turn = _webapp._live_turns.get("1001:42")
        assert turn is not None
        text_events = [e for e in turn["events"] if e.get("type") == "text"]
        assert len(text_events) == 1
        assert text_events[0]["text"] == "hello world"

    # ── A6: Mixed run with multiple tool types ────────────────────────────────

    async def test_mixed_run_all_tool_events_have_kind(self, aiohttp_client, tmp_path, project_dir):
        """All tool events in a multi-tool run must carry kind, regardless of tool type."""
        async def fake_engine(**kwargs):
            yield {"type": "text", "text": "starting"}
            yield {"type": "tool", "name": "Read", "input": {"file_path": "/etc/hosts"}}
            yield {"type": "tool", "name": "Bash", "input": {"command": "cat /etc/hosts"}}
            yield {"type": "tool", "name": "Write", "input": {"file_path": "/tmp/out.txt", "content": "done"}}
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        await (await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)).read()

        turn = _webapp._live_turns.get("1001:42")
        tool_events = [e for e in turn["events"] if e.get("type") == "tool"]
        assert len(tool_events) == 3
        for ev in tool_events:
            assert "kind" in ev, f"Tool event missing 'kind': {ev}"
            assert "input" not in ev, f"Tool event must not have raw 'input': {ev}"
        kinds = {ev["kind"] for ev in tool_events}
        assert kinds == {"read", "bash", "write"}

    # ── A7: Seq preserved on formatted tool events ────────────────────────────

    async def test_formatted_tool_events_keep_seq(self, aiohttp_client, tmp_path, project_dir):
        """Formatting must not discard the seq tag added by _live_turn_append."""
        async def fake_engine(**kwargs):
            yield {"type": "tool", "name": "Read", "input": {"file_path": "/tmp/x"}}
            yield {"type": "result", "session_id": "s1"}

        ctx = _make_ctx(tmp_path, project_dir, run_engine=fake_engine)
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        await (await client.post("/api/projects/myproject/chat", json={"prompt": "go"}, headers=h)).read()

        turn = _webapp._live_turns.get("1001:42")
        tool_events = [e for e in turn["events"] if e.get("type") == "tool"]
        assert len(tool_events) == 1
        assert "seq" in tool_events[0], "Formatted tool event must retain seq"


# ══════════════════════════════════════════════════════════════════════════════
# Bug B — chat queue persistence
# ══════════════════════════════════════════════════════════════════════════════


class TestBugBQueuePersistence:
    """Chat queue must survive browser reload (server stays up, queue file re-read on mount)."""

    def setup_method(self):
        _cleanup()

    # ── B1: _chat_queue_init loads existing file ──────────────────────────────

    def test_chat_queue_init_loads_from_disk(self, tmp_path):
        """_chat_queue_init reads an existing chat-queue.json and populates _CHAT_QUEUE."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "chat-queue.json"
        pre_queue = {
            "1001:42": [{"id": "abc", "text": "pending msg", "created_at": 1234567890.0}]
        }
        qfile.write_text(json.dumps(pre_queue), encoding="utf-8")

        ctx = {"DATA": data_dir}
        _webapp._CHAT_QUEUE.clear()
        _webapp._chat_queue_init(ctx)

        assert "1001:42" in _webapp._CHAT_QUEUE
        assert len(_webapp._CHAT_QUEUE["1001:42"]) == 1
        assert _webapp._CHAT_QUEUE["1001:42"][0]["text"] == "pending msg"

    # ── B2: init on missing file is a no-op ───────────────────────────────────

    def test_chat_queue_init_missing_file_is_noop(self, tmp_path):
        """_chat_queue_init with no existing file does not crash and leaves queue empty."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = {"DATA": data_dir}
        _webapp._CHAT_QUEUE.clear()
        _webapp._chat_queue_init(ctx)

        assert _webapp._CHAT_QUEUE == {}

    # ── B3: init with corrupted file is a no-op ───────────────────────────────

    def test_chat_queue_init_corrupted_file_is_noop(self, tmp_path):
        """_chat_queue_init with a corrupted JSON file does not crash."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "chat-queue.json").write_text("not valid json{{{", encoding="utf-8")
        ctx = {"DATA": data_dir}
        _webapp._CHAT_QUEUE.clear()
        _webapp._chat_queue_init(ctx)

        assert _webapp._CHAT_QUEUE == {}

    # ── B4: enqueue flushes to disk ───────────────────────────────────────────

    def test_enqueue_flushes_to_disk(self, tmp_path):
        """_chat_queue_enqueue persists the queue to disk immediately."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _webapp._CHAT_QUEUE_FILE = data_dir / "chat-queue.json"

        item = _webapp._chat_queue_enqueue("1001:42", "hello")
        assert item is not None

        assert _webapp._CHAT_QUEUE_FILE.exists(), "Queue file must be written after enqueue"
        on_disk = json.loads(_webapp._CHAT_QUEUE_FILE.read_text(encoding="utf-8"))
        assert "1001:42" in on_disk
        assert on_disk["1001:42"][0]["text"] == "hello"

    # ── B5: pop flushes to disk ───────────────────────────────────────────────

    def test_pop_flushes_to_disk(self, tmp_path):
        """_chat_queue_pop removes the item and persists the updated queue."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _webapp._CHAT_QUEUE_FILE = data_dir / "chat-queue.json"
        _webapp._chat_queue_enqueue("1001:42", "msg1")
        _webapp._chat_queue_enqueue("1001:42", "msg2")

        popped = _webapp._chat_queue_pop("1001:42")
        assert popped["text"] == "msg1"

        on_disk = json.loads(_webapp._CHAT_QUEUE_FILE.read_text(encoding="utf-8"))
        remaining = on_disk.get("1001:42", [])
        assert len(remaining) == 1
        assert remaining[0]["text"] == "msg2"

    # ── B6: edit flushes to disk ──────────────────────────────────────────────

    def test_edit_flushes_to_disk(self, tmp_path):
        """_chat_queue_edit persists the modified text."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _webapp._CHAT_QUEUE_FILE = data_dir / "chat-queue.json"
        item = _webapp._chat_queue_enqueue("1001:42", "original")

        updated = _webapp._chat_queue_edit("1001:42", item["id"], "edited")
        assert updated is not None
        assert updated["text"] == "edited"

        on_disk = json.loads(_webapp._CHAT_QUEUE_FILE.read_text(encoding="utf-8"))
        assert on_disk["1001:42"][0]["text"] == "edited"

    # ── B7: delete flushes to disk ────────────────────────────────────────────

    def test_delete_flushes_to_disk(self, tmp_path):
        """_chat_queue_delete persists the removal."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _webapp._CHAT_QUEUE_FILE = data_dir / "chat-queue.json"
        item = _webapp._chat_queue_enqueue("1001:42", "to delete")

        removed = _webapp._chat_queue_delete("1001:42", item["id"])
        assert removed is True

        on_disk = json.loads(_webapp._CHAT_QUEUE_FILE.read_text(encoding="utf-8"))
        assert on_disk.get("1001:42", []) == []

    # ── B8: queue survives simulated server restart ───────────────────────────

    def test_queue_survives_restart_roundtrip(self, tmp_path):
        """Full roundtrip: enqueue → flush → clear in-memory → reload → items intact."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _webapp._CHAT_QUEUE_FILE = data_dir / "chat-queue.json"

        _webapp._chat_queue_enqueue("sk:1", "first")
        _webapp._chat_queue_enqueue("sk:1", "second")

        # Simulate restart: clear in-memory state and reload
        _webapp._CHAT_QUEUE.clear()
        ctx = {"DATA": data_dir}
        _webapp._chat_queue_init(ctx)

        items = _webapp._chat_queue_get("sk:1")
        assert len(items) == 2
        assert items[0]["text"] == "first"
        assert items[1]["text"] == "second"

    # ── B9: queue GET endpoint returns items after reload ─────────────────────

    async def test_queue_api_returns_items_after_reload(self, aiohttp_client, tmp_path, project_dir):
        """GET /chat/queue returns surviving items after a simulated reload (server restart)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Pre-populate the queue file (simulating items enqueued before restart)
        pre = {
            "1001:42": [
                {"id": "id1", "text": "queued msg", "created_at": 1700000000.0}
            ]
        }
        (data_dir / "chat-queue.json").write_text(json.dumps(pre), encoding="utf-8")

        # Reload into global state (simulates _chat_queue_init in start())
        _webapp._CHAT_QUEUE.clear()
        ctx_init = {"DATA": data_dir}
        _webapp._chat_queue_init(ctx_init)

        ctx = _make_ctx(tmp_path, project_dir)
        ctx["DATA"] = data_dir
        app = _make_app(ctx)
        client = await aiohttp_client(app)
        h = _auth(ctx)

        resp = await client.get("/api/projects/myproject/chat/queue", headers=h)
        assert resp.status == 200
        data = await resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["text"] == "queued msg"

    # ── B10: FIFO order preserved across flush/reload ─────────────────────────

    def test_fifo_order_preserved_after_reload(self, tmp_path):
        """FIFO order is maintained after flush → reload → pop."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _webapp._CHAT_QUEUE_FILE = data_dir / "chat-queue.json"

        for i in range(5):
            _webapp._chat_queue_enqueue("sk:x", f"msg{i}")

        # Reload
        _webapp._CHAT_QUEUE.clear()
        _webapp._chat_queue_init({"DATA": data_dir})

        for i in range(5):
            item = _webapp._chat_queue_pop("sk:x")
            assert item is not None, f"Expected item {i}"
            assert item["text"] == f"msg{i}", f"FIFO broken at position {i}: {item}"

    # ── B11: flush with no file path is a no-op ───────────────────────────────

    def test_flush_without_init_is_noop(self):
        """_chat_queue_flush when _CHAT_QUEUE_FILE is None must not raise."""
        _webapp._CHAT_QUEUE_FILE = None
        _webapp._chat_queue_enqueue("1001:42", "msg")  # Should not raise even without file
        # If we reach here, it's a pass (no exception raised)

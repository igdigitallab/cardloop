"""
Timeline tests (Spec 008) — event bus persistence.

Covers:
- _timeline_path: stable slug from cwd; fallback to _unknown for unknown session_key
- _timeline_append: writes JSONL, adds ts, truncates text >2000, does not write env
- rotation at >5MB: rename → .jsonl.1, continues writing to a new file
- _bus_publish → write to timeline (integration)
- GET endpoint: 200 + events, before/limit pagination, 404 for non-existent project
- broken line in JSONL does not crash reading (graceful)
- env NEVER ends up in a timeline entry (even when passed in the event)
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _timeline_path,
    _timeline_append,
    _timeline_init,
    _timeline_slug_from_cwd,
    _timeline_read_events,
    _bus_publish,
    _derive_token,
    api_project_timeline,
    _TIMELINE_TEXT_LIMIT,
)

# ─────────────────────────── helpers ──────────────────────────────────────────

def _reset_timeline_state(data_dir: Path, topics: dict) -> None:
    """Initialise module-level timeline state for each test."""
    ctx = {
        "DATA": data_dir,
        "topics": topics,
    }
    _timeline_init(ctx)


# ─────────────────────────── unit: slug ───────────────────────────────────────

class TestTimelineSlug:
    def test_slug_replaces_slashes(self):
        """/home/youruser/myproject → -home-youruser-myproject."""
        slug = _timeline_slug_from_cwd("/home/youruser/myproject")
        assert slug == "-home-youruser-myproject"

    def test_slug_stable(self):
        """Same cwd → same slug on repeated calls."""
        cwd = "/home/youruser/some-project"
        assert _timeline_slug_from_cwd(cwd) == _timeline_slug_from_cwd(cwd)

    def test_slug_no_path_components(self):
        """Slug contains no '/' (no path-traversal through the filename)."""
        slug = _timeline_slug_from_cwd("/home/youruser/my/nested/project")
        assert "/" not in slug

    def test_slug_basename_project(self):
        """The basename part is present in the slug."""
        slug = _timeline_slug_from_cwd("/home/youruser/claude-ops-bot")
        assert "claude-ops-bot" in slug


# ─────────────────────────── unit: _timeline_path ─────────────────────────────

class TestTimelinePath:
    def test_path_known_session(self, tmp_path):
        """Known session_key → path based on project cwd."""
        cwd = str(tmp_path / "myproject")
        topics = {"42:100": {"project": "myproject", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("42:100")
        assert p is not None
        slug = _timeline_slug_from_cwd(cwd)
        assert p.name == f"{slug}.jsonl"

    def test_path_unknown_session_fallback(self, tmp_path):
        """Unknown session_key → _unknown or session-slug (not None, does not crash)."""
        _reset_timeline_state(tmp_path / "data", {})
        p = _timeline_path("unknown:999")
        assert p is not None
        # Acceptable names: either _unknown.jsonl or slug from session_key
        assert p.suffix == ".jsonl"

    def test_path_returns_none_before_init(self):
        """Before _timeline_init → returns None (does not crash)."""
        original = _webapp._TIMELINE_DATA_DIR
        try:
            _webapp._TIMELINE_DATA_DIR = None
            result = _timeline_path("any:key")
            assert result is None
        finally:
            _webapp._TIMELINE_DATA_DIR = original

    def test_path_in_data_timeline_dir(self, tmp_path):
        """Path always lives under DATA/timeline/."""
        data = tmp_path / "data"
        cwd = str(tmp_path / "proj")
        topics = {"1:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(data, topics)

        p = _timeline_path("1:1")
        assert p is not None
        assert p.parent == data / "timeline"


# ─────────────────────────── unit: _timeline_append ──────────────────────────

class TestTimelineAppend:
    def setup_method(self):
        """Called before each test by pytest."""

    def test_append_creates_file(self, tmp_path):
        """_timeline_append creates the JSONL file on first write."""
        cwd = str(tmp_path / "proj")
        topics = {"10:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:1", {"kind": "run_start", "run_id": "abc"})

        p = _timeline_path("10:1")
        assert p is not None and p.exists()

    def test_append_writes_valid_jsonl(self, tmp_path):
        """Each line is valid JSON."""
        cwd = str(tmp_path / "proj")
        topics = {"10:2": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:2", {"kind": "run_start", "run_id": "x1"})
        _timeline_append("10:2", {"kind": "text", "text": "hello"})

        p = _timeline_path("10:2")
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "kind" in obj

    def test_append_adds_ts(self, tmp_path):
        """Entry contains a ts (timestamp) field."""
        cwd = str(tmp_path / "proj")
        topics = {"10:3": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        before = time.time()
        _timeline_append("10:3", {"kind": "run_end"})
        after = time.time()

        p = _timeline_path("10:3")
        obj = json.loads(p.read_text().strip())
        assert "ts" in obj
        assert before <= obj["ts"] <= after

    def test_append_truncates_text_field(self, tmp_path):
        """Long text is truncated to _TIMELINE_TEXT_LIMIT chars."""
        cwd = str(tmp_path / "proj")
        topics = {"10:4": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        long_text = "A" * (_TIMELINE_TEXT_LIMIT + 500)
        _timeline_append("10:4", {"kind": "text", "text": long_text})

        p = _timeline_path("10:4")
        obj = json.loads(p.read_text().strip())
        assert len(obj["text"]) <= _TIMELINE_TEXT_LIMIT + 1  # +1 for ellipsis char
        assert obj["text"].endswith("…")

    def test_append_short_text_not_truncated(self, tmp_path):
        """Short text is not truncated."""
        cwd = str(tmp_path / "proj")
        topics = {"10:5": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:5", {"kind": "text", "text": "short"})
        p = _timeline_path("10:5")
        obj = json.loads(p.read_text().strip())
        assert obj["text"] == "short"

    def test_append_excludes_env_field(self, tmp_path):
        """The env field never ends up in the entry."""
        cwd = str(tmp_path / "proj")
        topics = {"10:6": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:6", {"kind": "run_start", "env": {"SECRET": "s3cr3t!"}})

        p = _timeline_path("10:6")
        obj = json.loads(p.read_text().strip())
        assert "env" not in obj
        assert "s3cr3t!" not in p.read_text()

    def test_append_no_crash_if_not_init(self):
        """_timeline_append silently returns None if DATA is not initialised."""
        original = _webapp._TIMELINE_DATA_DIR
        try:
            _webapp._TIMELINE_DATA_DIR = None
            # Must not raise
            _timeline_append("any:key", {"kind": "text"})
        finally:
            _webapp._TIMELINE_DATA_DIR = original

    def test_rotation_at_5mb(self, tmp_path):
        """Rotation: file >5MB → renamed to .jsonl.1, continues writing to a new file."""
        cwd = str(tmp_path / "proj")
        topics = {"20:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("20:1")
        assert p is not None

        # Create a file > 5MB
        p.parent.mkdir(parents=True, exist_ok=True)
        big_content = ("x" * 1023 + "\n") * (5 * 1024 + 10)  # slightly > 5MB
        p.write_text(big_content, encoding="utf-8")
        assert p.stat().st_size > 5 * 1024 * 1024

        # Append an event — rotation must occur
        _timeline_append("20:1", {"kind": "run_end"})

        backup = p.with_suffix(".jsonl.1")
        assert backup.exists(), "backup .jsonl.1 must exist after rotation"
        # Main file must contain only the new event
        assert p.exists()
        new_content = p.read_text()
        assert "run_end" in new_content
        # New file must be much smaller than 5MB
        assert p.stat().st_size < 5 * 1024 * 1024

    def test_rotation_overwrites_old_backup(self, tmp_path):
        """Old .jsonl.1 is overwritten on the next rotation."""
        cwd = str(tmp_path / "proj")
        topics = {"20:2": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("20:2")
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = p.with_suffix(".jsonl.1")

        # Create an old backup
        backup.write_text("old backup content\n", encoding="utf-8")

        # Create a file > 5MB
        big_content = ("y" * 1023 + "\n") * (5 * 1024 + 10)
        p.write_text(big_content, encoding="utf-8")

        _timeline_append("20:2", {"kind": "text"})

        # Old backup must be overwritten
        assert "old backup content" not in backup.read_text()


# ─────────────────────────── integration: _bus_publish → timeline ─────────────

class TestBusPublishIntegration:
    def test_bus_publish_writes_to_timeline(self, tmp_path):
        """_bus_publish automatically persists the event to timeline."""
        cwd = str(tmp_path / "proj")
        topics = {"5:10": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _bus_publish("5:10", {"kind": "run_start", "run_id": "card-x"})

        p = _timeline_path("5:10")
        assert p is not None and p.exists()
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        obj = json.loads(lines[0])
        assert obj["kind"] == "run_start"
        assert obj["run_id"] == "card-x"
        assert "ts" in obj

    def test_bus_publish_multiple_events(self, tmp_path):
        """Multiple _bus_publish calls → multiple lines in the JSONL."""
        cwd = str(tmp_path / "proj")
        topics = {"5:11": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _bus_publish("5:11", {"kind": "run_start"})
        _bus_publish("5:11", {"kind": "text", "text": "Hello"})
        _bus_publish("5:11", {"kind": "run_end", "outcome": "ok"})

        p = _timeline_path("5:11")
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == 3
        kinds = [json.loads(l)["kind"] for l in lines]
        assert kinds == ["run_start", "text", "run_end"]


# ─────────────────────────── unit: _timeline_read_events ─────────────────────

class TestTimelineReadEvents:
    def test_read_empty_returns_empty(self, tmp_path):
        """No file → empty list."""
        _reset_timeline_state(tmp_path / "data", {})
        events = _timeline_read_events("nonexistent:key", 200, None)
        assert events == []

    def test_read_events_chronological(self, tmp_path):
        """Events are returned in chronological order (oldest first)."""
        cwd = str(tmp_path / "proj")
        topics = {"30:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        # Write with increasing ts
        for i in range(5):
            _timeline_append("30:1", {"kind": "text", "ts_order": i})

        events = _timeline_read_events("30:1", 200, None)
        assert len(events) == 5
        ts_values = [e["ts"] for e in events]
        assert ts_values == sorted(ts_values), "Must be in chronological order"

    def test_read_limit(self, tmp_path):
        """limit=3 → no more than 3 most recent events."""
        cwd = str(tmp_path / "proj")
        topics = {"30:2": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        for _ in range(10):
            _timeline_append("30:2", {"kind": "text", "text": "x"})

        events = _timeline_read_events("30:2", 3, None)
        assert len(events) == 3

    def test_read_before_pagination(self, tmp_path):
        """before=<ts> → only events with ts < before."""
        cwd = str(tmp_path / "proj")
        topics = {"30:3": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        for i in range(5):
            _timeline_append("30:3", {"kind": "text"})

        events_all = _timeline_read_events("30:3", 200, None)
        # Take ts of the 3rd event (from the start)
        cutoff_ts = events_all[2]["ts"]
        events_before = _timeline_read_events("30:3", 200, cutoff_ts)
        # Must get only events with ts < cutoff_ts
        assert all(e["ts"] < cutoff_ts for e in events_before)
        assert len(events_before) <= 2  # ts may coincide — allow ≤ 2

    def test_read_graceful_broken_line(self, tmp_path):
        """A broken JSONL line does not crash reading — it is simply skipped."""
        cwd = str(tmp_path / "proj")
        topics = {"30:4": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("30:4")
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        # One valid + one broken + one valid line
        p.write_text(
            '{"ts": 1.0, "kind": "run_start"}\n'
            'THIS IS NOT JSON %%!!@@\n'
            '{"ts": 2.0, "kind": "run_end"}\n',
            encoding="utf-8",
        )

        events = _timeline_read_events("30:4", 200, None)
        # Broken line skipped — we get 2 events
        assert len(events) == 2
        kinds = [e["kind"] for e in events]
        assert "run_start" in kinds
        assert "run_end" in kinds

    def test_read_includes_backup_file(self, tmp_path):
        """Events are read from .jsonl.1 (backup) + the current file."""
        cwd = str(tmp_path / "proj")
        topics = {"30:5": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("30:5")
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = p.with_suffix(".jsonl.1")

        # Older events in backup
        backup.write_text('{"ts": 1.0, "kind": "run_start"}\n', encoding="utf-8")
        # Newer events in current file
        p.write_text('{"ts": 2.0, "kind": "run_end"}\n', encoding="utf-8")

        events = _timeline_read_events("30:5", 200, None)
        assert len(events) == 2
        assert events[0]["kind"] == "run_start"  # backup first
        assert events[1]["kind"] == "run_end"


# ─────────────────────────── API endpoint tests ───────────────────────────────

@pytest.fixture
def fake_ctx_with_project(tmp_path):
    """ctx with one project and configured timeline."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

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
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    # Initialise timeline
    _timeline_init(ctx)
    return ctx


@pytest.fixture
def timeline_app(fake_ctx_with_project):
    """aiohttp application with timeline routes."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/timeline", _webapp.api_project_timeline)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_api_timeline_empty(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline with an empty log → 200, events:[]."""
    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert "events" in data
    assert data["events"] == []


async def test_api_timeline_not_found(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline for a non-existent project → 404."""
    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/nonexistent/timeline", headers=h)
    assert resp.status == 404


async def test_api_timeline_unauthorized(aiohttp_client, timeline_app):
    """GET /timeline without authentication → 401."""
    client = await aiohttp_client(timeline_app)
    resp = await client.get("/api/projects/myproject/timeline")
    assert resp.status == 401


async def test_api_timeline_returns_events(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline returns events from the JSONL log."""
    # Publish events to the bus (they are automatically written to timeline)
    _bus_publish("1001:42", {"kind": "run_start", "run_id": "card-abc"})
    _bus_publish("1001:42", {"kind": "run_end", "outcome": "ok", "run_id": "card-abc"})

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline", headers=h)
    assert resp.status == 200
    data = await resp.json()
    events = data["events"]
    assert len(events) >= 2
    kinds = [e["kind"] for e in events]
    assert "run_start" in kinds
    assert "run_end" in kinds


async def test_api_timeline_limit_param(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline?limit=1 → no more than 1 event."""
    for _ in range(5):
        _bus_publish("1001:42", {"kind": "text", "text": "x"})

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline?limit=1", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert len(data["events"]) <= 1


async def test_api_timeline_before_param(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline?before=<ts> → only events earlier than ts."""
    _bus_publish("1001:42", {"kind": "run_start", "run_id": "r1"})
    # Small pause for different ts values
    import asyncio
    await asyncio.sleep(0.01)
    cutoff = time.time()
    await asyncio.sleep(0.01)
    _bus_publish("1001:42", {"kind": "run_end", "run_id": "r1"})

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get(f"/api/projects/myproject/timeline?before={cutoff}", headers=h)
    assert resp.status == 200
    data = await resp.json()
    # All events must be before the cutoff
    for e in data["events"]:
        assert e["ts"] < cutoff


async def test_api_timeline_env_not_in_response(aiohttp_client, timeline_app, fake_ctx_with_project):
    """env field must not appear in the endpoint response (even if it was in the event)."""
    # Publish an event with env — _timeline_append must filter it out
    _bus_publish("1001:42", {
        "kind": "run_start",
        "run_id": "env-test",
        "env": {"SECRET_KEY": "SUPER_SECRET_DO_NOT_LEAK"},
    })

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline", headers=h)
    assert resp.status == 200

    resp_text = await resp.text()
    assert "SUPER_SECRET_DO_NOT_LEAK" not in resp_text, \
        "Secret leaked into timeline API response!"
    data = await resp.json()
    for e in data["events"]:
        assert "env" not in e, "env field must never appear in timeline events"

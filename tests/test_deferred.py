"""
Tests for Spec 020: Deferred Runs.

Covers:
- _load_deferred / _save_deferred (file I/O)
- _utcnow_iso / _unix_to_iso / _new_deferred_id helpers
- api_deferred_create (POST /api/deferred) — validation, creation
- api_deferred_list (GET /api/deferred) — filters
- api_deferred_delete (DELETE /api/deferred/{id}) — cancel
- api_schedules_get merges deferred pending records (Spec 020 integration)
- _deferred_loop: fires at correct time, respects busy session, max attempts
- Phase D: auto-resume on rate-limit detection + loop guard + toggle
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, auth_middleware
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


# ─────────────────────────── Fixtures ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_deferred_file(tmp_path):
    """Reset _DEFERRED_FILE between tests."""
    old = _webapp._DEFERRED_FILE
    _webapp._DEFERRED_FILE = tmp_path / "data" / "deferred.json"
    (tmp_path / "data").mkdir(exist_ok=True)
    yield
    _webapp._DEFERRED_FILE = old


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    ctx["_auth_token"] = _derive_token("testpass")
    return ctx


@pytest.fixture
def deferred_app(fake_ctx):
    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/deferred", _webapp.api_deferred_create)
    app.router.add_get("/api/deferred", _webapp.api_deferred_list)
    app.router.add_delete("/api/deferred/{id}", _webapp.api_deferred_delete)
    app.router.add_get("/api/schedules", _webapp.api_schedules_get)
    return app


def auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _make_topic(project: str, cwd: str = "/tmp/proj") -> dict:
    return {"project": project, "cwd": cwd, "model": "sonnet"}


# ─────────────────────────── Helper tests ─────────────────────────────────────

def test_utcnow_iso_format():
    """_utcnow_iso returns ISO-8601 with Z suffix."""
    result = _webapp._utcnow_iso()
    assert result.endswith("Z")
    assert "T" in result


def test_unix_to_iso_roundtrip():
    """_unix_to_iso produces a parseable ISO string that _iso_to_unix can reverse."""
    ts = 1700000000.5
    iso = _webapp._unix_to_iso(ts)
    assert iso.endswith("Z")
    # _iso_to_unix is int-based; allow ±1s
    back = _webapp._iso_to_unix(iso)
    assert back is not None
    assert abs(back - int(ts)) <= 1


def test_new_deferred_id_format():
    """_new_deferred_id returns 'def-' + 8 hex chars."""
    id1 = _webapp._new_deferred_id()
    id2 = _webapp._new_deferred_id()
    assert id1.startswith("def-")
    assert len(id1) == 12  # "def-" + 8
    assert id1 != id2


# ─────────────────────────── File I/O tests ───────────────────────────────────

def test_load_deferred_empty_when_file_missing():
    """_load_deferred returns [] when file does not exist."""
    _webapp._DEFERRED_FILE = Path("/tmp/nonexistent_deferred_xyz.json")
    result = _webapp._load_deferred()
    assert result == []


def test_save_and_load_roundtrip(tmp_path):
    """_save_deferred then _load_deferred returns the same records."""
    records = [
        {"id": "def-aabb1122", "project": "myproj", "status": "pending"},
        {"id": "def-ccdd3344", "project": "other", "status": "fired"},
    ]
    _webapp._DEFERRED_FILE = tmp_path / "deferred.json"
    _webapp._save_deferred(records)
    loaded = _webapp._load_deferred()
    assert loaded == records


def test_save_deferred_atomic(tmp_path):
    """_save_deferred writes via a .tmp file (atomic write, no leftover .tmp)."""
    _webapp._DEFERRED_FILE = tmp_path / "deferred.json"
    _webapp._save_deferred([{"id": "x"}])
    assert (tmp_path / "deferred.json").exists()
    assert not (tmp_path / "deferred.json.tmp").exists()


def test_load_deferred_returns_empty_on_corrupt_file(tmp_path):
    """_load_deferred returns [] on JSON decode error."""
    f = tmp_path / "deferred.json"
    f.write_text("not valid json{")
    _webapp._DEFERRED_FILE = f
    result = _webapp._load_deferred()
    assert result == []


# ─────────────────────────── API: create ──────────────────────────────────────

@pytest.mark.asyncio
async def test_create_deferred_fire_at(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with fire_at → 201 + record saved."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    fire_at = _webapp._unix_to_iso(time.time() + 3600)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "run tests", "fire_at": fire_at},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "pending"
    assert body["id"].startswith("def-")

    records = _webapp._load_deferred()
    assert len(records) == 1
    assert records[0]["project"] == "myproject"
    assert records[0]["fire_at"] == fire_at
    assert records[0]["fire_on_reset"] is False


@pytest.mark.asyncio
async def test_create_deferred_fire_on_reset(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with fire_on_reset=true → 201."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "do something", "fire_on_reset": True},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 201
    records = _webapp._load_deferred()
    assert records[0]["fire_on_reset"] is True
    assert records[0]["fire_at"] is None
    # Button/endpoint-created fire_on_reset records are strict: they wait for the
    # real reset boundary and skip the util<10% free-window shortcut.
    assert records[0]["strict_reset"] is True


@pytest.mark.asyncio
async def test_create_deferred_stores_card_id(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with card_id stores it on the record."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "do something", "fire_on_reset": True, "card_id": "card-abc"},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 201
    records = _webapp._load_deferred()
    assert records[0]["card_id"] == "card-abc"
    assert records[0]["strict_reset"] is True


@pytest.mark.asyncio
async def test_create_deferred_fire_at_not_strict(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with fire_at does NOT set strict_reset (only fire_on_reset does)."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "do something", "fire_at": "2099-01-01T00:00:00Z"},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 201
    records = _webapp._load_deferred()
    assert "strict_reset" not in records[0]


@pytest.mark.asyncio
async def test_create_deferred_missing_project(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with missing project → 400."""
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"prompt": "do something", "fire_on_reset": True},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_deferred_unknown_project(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with unknown project → 400."""
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "ghost", "prompt": "do something", "fire_on_reset": True},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 400
    body = await resp.json()
    assert "unknown project" in body["error"]


@pytest.mark.asyncio
async def test_create_deferred_both_fire_options_rejected(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with both fire_at and fire_on_reset → 400."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={
            "project": "myproject",
            "prompt": "do something",
            "fire_at": _webapp._unix_to_iso(time.time() + 3600),
            "fire_on_reset": True,
        },
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_deferred_neither_fire_option_rejected(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred without fire_at or fire_on_reset → 400."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "do something"},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_deferred_invalid_fire_at(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with garbage fire_at → 400."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "do something", "fire_at": "not-a-date"},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_deferred_missing_prompt(aiohttp_client, deferred_app, fake_ctx):
    """POST /api/deferred with empty prompt → 400."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    resp = await client.post(
        "/api/deferred",
        json={"project": "myproject", "prompt": "", "fire_on_reset": True},
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 400


# ─────────────────────────── API: list ────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_deferred_empty(aiohttp_client, deferred_app, fake_ctx):
    """GET /api/deferred returns [] when no records."""
    client = await aiohttp_client(deferred_app)
    resp = await client.get("/api/deferred", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body == []


@pytest.mark.asyncio
async def test_list_deferred_with_records(aiohttp_client, deferred_app, fake_ctx):
    """GET /api/deferred returns all records."""
    _webapp._save_deferred([
        {"id": "def-001", "project": "p1", "status": "pending"},
        {"id": "def-002", "project": "p2", "status": "fired"},
    ])
    client = await aiohttp_client(deferred_app)
    resp = await client.get("/api/deferred", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert len(body) == 2


@pytest.mark.asyncio
async def test_list_deferred_filter_status(aiohttp_client, deferred_app, fake_ctx):
    """GET /api/deferred?status=pending returns only pending."""
    _webapp._save_deferred([
        {"id": "def-001", "project": "p1", "status": "pending"},
        {"id": "def-002", "project": "p2", "status": "fired"},
    ])
    client = await aiohttp_client(deferred_app)
    resp = await client.get("/api/deferred?status=pending", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "def-001"


@pytest.mark.asyncio
async def test_list_deferred_filter_project(aiohttp_client, deferred_app, fake_ctx):
    """GET /api/deferred?project=p1 returns only records for that project."""
    _webapp._save_deferred([
        {"id": "def-001", "project": "p1", "status": "pending"},
        {"id": "def-002", "project": "p2", "status": "pending"},
    ])
    client = await aiohttp_client(deferred_app)
    resp = await client.get("/api/deferred?project=p1", headers=auth_headers(fake_ctx))
    body = await resp.json()
    assert len(body) == 1
    assert body[0]["project"] == "p1"


# ─────────────────────────── API: delete/cancel ───────────────────────────────

@pytest.mark.asyncio
async def test_cancel_pending_deferred(aiohttp_client, deferred_app, fake_ctx):
    """DELETE /api/deferred/{id} cancels a pending record."""
    _webapp._save_deferred([
        {"id": "def-aabb1122", "project": "p1", "status": "pending"},
    ])
    client = await aiohttp_client(deferred_app)
    resp = await client.delete(
        "/api/deferred/def-aabb1122",
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["cancelled"] is True

    records = _webapp._load_deferred()
    assert records[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_not_found(aiohttp_client, deferred_app, fake_ctx):
    """DELETE /api/deferred/{id} on unknown id → 404."""
    client = await aiohttp_client(deferred_app)
    resp = await client.delete(
        "/api/deferred/def-nonexistent",
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_cancel_already_fired(aiohttp_client, deferred_app, fake_ctx):
    """DELETE /api/deferred/{id} on fired record → 409."""
    _webapp._save_deferred([
        {"id": "def-fired01", "project": "p1", "status": "fired"},
    ])
    client = await aiohttp_client(deferred_app)
    resp = await client.delete(
        "/api/deferred/def-fired01",
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 409


@pytest.mark.asyncio
async def test_cancel_already_failed(aiohttp_client, deferred_app, fake_ctx):
    """DELETE /api/deferred/{id} on failed record → 409."""
    _webapp._save_deferred([
        {"id": "def-fail01", "project": "p1", "status": "failed"},
    ])
    client = await aiohttp_client(deferred_app)
    resp = await client.delete(
        "/api/deferred/def-fail01",
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 409


# ─────────────────────────── Schedules integration ────────────────────────────

@pytest.mark.asyncio
async def test_schedules_get_merges_deferred_pending(aiohttp_client, deferred_app, fake_ctx):
    """GET /api/schedules includes pending deferred records merged in."""
    import schedules as _sched
    # Reset schedules module state
    old_cache = _sched._CACHE_PATH
    _sched._CACHE_PATH = fake_ctx["DATA"] / "schedules_cache.json"

    fire_at = _webapp._unix_to_iso(time.time() + 3600)
    _webapp._save_deferred([
        {
            "id": "def-aabb1122",
            "project": "myproject",
            "session_key": "100:10",
            "prompt": "do the thing",
            "fire_at": fire_at,
            "fire_on_reset": False,
            "status": "pending",
        }
    ])

    client = await aiohttp_client(deferred_app)
    resp = await client.get("/api/schedules", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    records = body["records"]

    deferred_records = [r for r in records if r.get("source") == "deferred"]
    assert len(deferred_records) == 1
    dr = deferred_records[0]
    assert dr["id"] == "def-aabb1122"
    assert dr["annotations"]["deferred_id"] == "def-aabb1122"
    assert "do the thing" in dr["purpose"]

    _sched._CACHE_PATH = old_cache


@pytest.mark.asyncio
async def test_schedules_get_does_not_merge_fired_deferred(aiohttp_client, deferred_app, fake_ctx):
    """GET /api/schedules does NOT include fired/cancelled deferred records."""
    import schedules as _sched
    old_cache = _sched._CACHE_PATH
    _sched._CACHE_PATH = fake_ctx["DATA"] / "schedules_cache.json"

    _webapp._save_deferred([
        {"id": "def-fired", "project": "p", "prompt": "x", "fire_at": None, "fire_on_reset": False, "status": "fired"},
        {"id": "def-cancelled", "project": "p", "prompt": "y", "fire_at": None, "fire_on_reset": False, "status": "cancelled"},
    ])

    client = await aiohttp_client(deferred_app)
    resp = await client.get("/api/schedules", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    deferred_records = [r for r in body["records"] if r.get("source") == "deferred"]
    assert len(deferred_records) == 0

    _sched._CACHE_PATH = old_cache


# ─────────────────────────── Loop logic tests ─────────────────────────────────

@pytest.mark.asyncio
async def test_deferred_loop_fires_at_correct_time(fake_ctx):
    """_deferred_loop fires a record when fire_at is in the past."""
    # Set up a record with fire_at in the past
    fire_at = _webapp._unix_to_iso(time.time() - 10)
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    _webapp._save_deferred([{
        "id": "def-loop01",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "run tests",
        "fire_at": fire_at,
        "fire_on_reset": False,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }])

    fired_records: list = []

    async def mock_execute(ctx, record):
        fired_records.append(record["id"])
        ctx["running"].pop(record["session_key"], None)

    with patch.object(_webapp, "_execute_deferred", side_effect=mock_execute), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock), \
         patch.object(_webapp, "_DEFERRED_POLL_SEC", 0):
        # Run one iteration of the loop (cancel after first sleep)
        async def run_one():
            task = asyncio.create_task(_webapp._deferred_loop(fake_ctx))
            await asyncio.sleep(0.05)  # let it start
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Skip the 15s startup delay
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # First call is the 15s delay, let it pass
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    records = _webapp._load_deferred()
    # Record should be marked "fired"
    assert any(r["id"] == "def-loop01" and r["status"] == "fired" for r in records)


@pytest.mark.asyncio
async def test_deferred_loop_skips_non_pending():
    """_deferred_loop does not re-fire already-fired records."""
    _webapp._save_deferred([{
        "id": "def-skip",
        "project": "p",
        "session_key": "100:10",
        "prompt": "x",
        "fire_at": _webapp._unix_to_iso(time.time() - 10),
        "fire_on_reset": False,
        "status": "fired",  # already fired
        "attempts": 0,
    }])

    executed: list = []

    async def mock_execute(ctx, record):
        executed.append(record["id"])

    fake_ctx = {"topics": {}, "sessions": {}, "running": {}, "ptb_app": None}
    with patch.object(_webapp, "_execute_deferred", side_effect=mock_execute), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    assert executed == []


@pytest.mark.asyncio
async def test_deferred_loop_reschedules_when_busy(fake_ctx):
    """When session is busy, loop increments attempts and reschedules."""
    fire_at = _webapp._unix_to_iso(time.time() - 10)
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    fake_ctx["running"]["100:10"] = True  # session is busy

    _webapp._save_deferred([{
        "id": "def-busy01",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "run tests",
        "fire_at": fire_at,
        "fire_on_reset": False,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }])

    executed: list = []

    async def mock_execute(ctx, record):
        executed.append(record["id"])

    with patch.object(_webapp, "_execute_deferred", side_effect=mock_execute), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    # Should NOT have been executed
    assert executed == []
    # Attempts should be incremented
    records = _webapp._load_deferred()
    assert records[0]["attempts"] == 1
    # Should still be pending (not failed yet)
    assert records[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_deferred_loop_fails_after_max_attempts(fake_ctx):
    """After _DEFERRED_MAX_ATTEMPTS busy sessions, record is marked failed."""
    fire_at = _webapp._unix_to_iso(time.time() - 10)
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    fake_ctx["running"]["100:10"] = True  # session is busy

    _webapp._save_deferred([{
        "id": "def-maxattempt",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "run tests",
        "fire_at": fire_at,
        "fire_on_reset": False,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": _webapp._DEFERRED_MAX_ATTEMPTS - 1,  # one more and it fails
    }])

    with patch.object(_webapp, "_execute_deferred", new_callable=AsyncMock), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    records = _webapp._load_deferred()
    assert records[0]["status"] == "failed"
    assert "busy" in records[0]["error"]


# ─────────────────────────── _deferred_loop fire_on_reset ────────────────────


@pytest.mark.asyncio
async def test_deferred_loop_fire_on_reset_free_window(fake_ctx):
    """fire_on_reset fires immediately when utilization < DEFERRED_FREE_THRESHOLD (mock 0.05)."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")

    _webapp._save_deferred([{
        "id": "def-for-free",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "fire on free window",
        "fire_at": None,
        "fire_on_reset": True,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }])

    free_usage = {"five_hour": {"utilization": 0.05, "resets_at": time.time() + 18000, "status": "allowed"}}

    spawned: list = []

    def mock_spawn_bg(coro):
        """Capture spawned coroutines without actually running them."""
        spawned.append(coro)
        # Close the coroutine to avoid ResourceWarning
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=free_usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=mock_spawn_bg), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    # Should have fired immediately (utilization 0.05 < DEFERRED_FREE_THRESHOLD 0.10)
    # The loop marks the record as "fired" and calls _spawn_bg(_execute_deferred(...))
    records = _webapp._load_deferred()
    fired = next(r for r in records if r["id"] == "def-for-free")
    assert fired["status"] == "fired", f"Expected 'fired' but got '{fired['status']}'"
    assert len(spawned) == 1, f"Expected _spawn_bg called once, got {len(spawned)}"


@pytest.mark.asyncio
async def test_deferred_loop_strict_reset_skips_free_window(fake_ctx):
    """A strict_reset record does NOT fire on the util<10% free window; it waits for resets_at."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")

    _webapp._save_deferred([{
        "id": "def-strict",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "explicit after-reset",
        "fire_at": None,
        "fire_on_reset": True,
        "strict_reset": True,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }])

    # Low utilization (would trigger the free-window shortcut for a non-strict record),
    # but resets_at is in the future → strict record must NOT fire yet.
    free_usage = {"five_hour": {"utilization": 0.05, "resets_at": time.time() + 18000, "status": "allowed"}}

    spawned: list = []

    def mock_spawn_bg(coro):
        spawned.append(coro)
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=free_usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=mock_spawn_bg), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    # Did NOT fire despite util 0.05 — strict records wait for the reset boundary.
    assert len(spawned) == 0, "Strict record must not early-fire on the free window"
    records = _webapp._load_deferred()
    pending = next(r for r in records if r["id"] == "def-strict")
    assert pending["status"] == "pending"


@pytest.mark.asyncio
async def test_deferred_loop_strict_reset_fires_at_resets_at(fake_ctx):
    """A strict_reset record fires once time >= resets_at + jitter, even with low utilization."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")

    now = time.time()
    _webapp._save_deferred([{
        "id": "def-strict-due",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "explicit after-reset due",
        "fire_at": None,
        "fire_on_reset": True,
        "strict_reset": True,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
        "_jitter": 30,  # fire_now = now >= (now-100) + 30 = True
    }])

    # Low utilization but resets_at already past → strict record fires at the boundary.
    past_usage = {"five_hour": {"utilization": 0.05, "resets_at": now - 100, "status": "allowed"}}

    spawned: list = []

    def mock_spawn_bg(coro):
        spawned.append(coro)
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=past_usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=mock_spawn_bg), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    records = _webapp._load_deferred()
    fired = next(r for r in records if r["id"] == "def-strict-due")
    assert fired["status"] == "fired"
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_deferred_loop_fire_on_reset_waits_for_resets_at(fake_ctx):
    """fire_on_reset does NOT fire when utilization=0.90; fires when time >= resets_at + jitter."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")

    _webapp._save_deferred([{
        "id": "def-for-wait",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "wait for reset",
        "fire_at": None,
        "fire_on_reset": True,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }])

    # High utilization, resets_at = now + 120s → should NOT fire yet
    now = time.time()
    high_usage = {"five_hour": {"utilization": 0.90, "resets_at": now + 120, "status": "allowed"}}

    spawned1: list = []

    def mock_spawn_bg1(coro):
        spawned1.append(coro)
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=high_usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=mock_spawn_bg1), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    # Should NOT have fired yet (resets_at is 120s in the future)
    assert len(spawned1) == 0, "Expected no _spawn_bg call (run not yet due)"
    records = _webapp._load_deferred()
    pending = next(r for r in records if r["id"] == "def-for-wait")
    assert pending["status"] == "pending"

    # Now simulate time past resets_at + jitter:
    # Assign stable jitter to the record (simulate what loop already persisted)
    past_usage = {"five_hour": {"utilization": 0.90, "resets_at": now - 100, "status": "allowed"}}
    pending["_jitter"] = 30  # fire_now = now >= (now-100) + 30 = True
    _webapp._save_deferred(records)

    spawned2: list = []

    def mock_spawn_bg2(coro):
        spawned2.append(coro)
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=past_usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=mock_spawn_bg2), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep2:
            call_count2 = 0
            async def controlled_sleep2(n):
                nonlocal call_count2
                call_count2 += 1
                if call_count2 >= 2:
                    raise asyncio.CancelledError()
            mock_sleep2.side_effect = controlled_sleep2
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    # Now it should have fired (resets_at + jitter is in the past)
    assert len(spawned2) == 1, f"Expected _spawn_bg called once after reset, got {len(spawned2)}"
    records = _webapp._load_deferred()
    fired = next(r for r in records if r["id"] == "def-for-wait")
    assert fired["status"] == "fired"


# ─────────────────────────── _deferred_loop reset_fallback ───────────────────


@pytest.mark.asyncio
async def test_deferred_loop_fires_via_reset_fallback_when_resets_at_unavailable(fake_ctx):
    """fire_on_reset record whose created is >6h ago and usage has no resets_at fires via fallback."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")

    # created more than DEFERRED_RESET_FALLBACK_SEC ago
    old_created = _webapp._unix_to_iso(time.time() - _webapp._DEFERRED_RESET_FALLBACK_SEC - 60)

    _webapp._save_deferred([{
        "id": "def-fallback01",
        "project": "myproject",
        "session_key": "100:10",
        "prompt": "fire via fallback",
        "fire_at": None,
        "fire_on_reset": True,
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
        "created": old_created,
    }])

    # usage returns five_hour dict but without resets_at (API can't determine reset boundary)
    no_resets_at_usage = {"five_hour": {"utilization": 0.90, "status": "allowed"}}

    spawned: list = []

    def mock_spawn_bg(coro):
        spawned.append(coro)
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=no_resets_at_usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=mock_spawn_bg), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0
            async def controlled_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            mock_sleep.side_effect = controlled_sleep
            try:
                await _webapp._deferred_loop(fake_ctx)
            except asyncio.CancelledError:
                pass

    records = _webapp._load_deferred()
    fired = next(r for r in records if r["id"] == "def-fallback01")
    assert fired["status"] == "fired", f"Expected 'fired' but got '{fired['status']}'"
    assert fired.get("fired_via") == "reset_fallback", f"Expected fired_via='reset_fallback', got {fired.get('fired_via')!r}"
    assert len(spawned) == 1, f"Expected _spawn_bg called once, got {len(spawned)}"


# ─────────────────────────── _deferred_init ───────────────────────────────────

def test_deferred_init_sets_file_path(tmp_path):
    """_deferred_init sets _DEFERRED_FILE to DATA/deferred.json."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    ctx = {"DATA": data}
    _webapp._deferred_init(ctx)
    assert _webapp._DEFERRED_FILE == data / "deferred.json"


# ─────────────────────────── _notify_operator ─────────────────────────────────

@pytest.mark.asyncio
async def test_notify_operator_sends_to_first_allowed_user():
    """_notify_operator sends to the first numeric user in ALLOWED_USERS."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_ptb = MagicMock()
    mock_ptb.bot = mock_bot
    ctx = {"ptb_app": mock_ptb}

    with patch.dict("os.environ", {"ALLOWED_USERS": "12345,67890"}):
        await _webapp._notify_operator(ctx, "hello")

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.kwargs.get("chat_id") == 12345 or call_kwargs.args[0] == 12345


@pytest.mark.asyncio
async def test_notify_operator_no_ptb_app():
    """_notify_operator silently returns when ptb_app is None."""
    ctx = {"ptb_app": None}
    # Should not raise
    await _webapp._notify_operator(ctx, "hello")


@pytest.mark.asyncio
async def test_notify_operator_no_allowed_users():
    """_notify_operator silently returns when ALLOWED_USERS is empty."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_ptb = MagicMock()
    mock_ptb.bot = mock_bot
    ctx = {"ptb_app": mock_ptb}

    with patch.dict("os.environ", {"ALLOWED_USERS": ""}):
        await _webapp._notify_operator(ctx, "hello")

    mock_bot.send_message.assert_not_called()


# ─────────────────────────── Prompt truncation ────────────────────────────────

@pytest.mark.asyncio
async def test_create_deferred_truncates_long_prompt(aiohttp_client, deferred_app, fake_ctx):
    """Prompt longer than 4096 chars is truncated to 4096."""
    fake_ctx["topics"]["100:10"] = _make_topic("myproject")
    client = await aiohttp_client(deferred_app)
    long_prompt = "x" * 5000
    fire_at = _webapp._unix_to_iso(time.time() + 3600)
    with patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        resp = await client.post(
            "/api/deferred",
            json={"project": "myproject", "prompt": long_prompt, "fire_at": fire_at},
            headers=auth_headers(fake_ctx),
        )
    assert resp.status == 201
    records = _webapp._load_deferred()
    assert len(records[0]["prompt"]) == 4096


# ─────────────────────────── Phase D: auto-resume on rate-limit ───────────────────────────


def _make_fake_ctx_with_topic(tmp_path, project="myproject", session_key="100:10"):
    """Minimal ctx for _maybe_auto_resume tests."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    _webapp._DEFERRED_FILE = data / "deferred.json"
    return {
        "topics": {session_key: {"project": project, "cwd": str(tmp_path), "model": "sonnet"}},
        "sessions": {session_key: "sess-abc123"},
        "running": {},
        "rate_limits": {},
        "ptb_app": None,
    }


@pytest.mark.asyncio
async def test_auto_resume_creates_deferred_on_429(tmp_path):
    """_maybe_auto_resume creates a fire_on_reset deferred record when api_error_status==429.
    Note: default is now OFF (spec-039); patch to 1 to test the function's internal logic."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "session_id": "sess-abc123", "api_error_status": 429}

    with patch.object(_webapp, "_AUTO_RESUME_ON_RATE_LIMIT", 1), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Run a big task",
            last_result_event=result_event,
            resume_session_id="sess-abc123",
        )

    records = _webapp._load_deferred()
    assert len(records) == 1
    r = records[0]
    assert r["status"] == "pending"
    assert r["fire_on_reset"] is True
    # Auto-resume records are NOT strict: they keep the util<10% free-window shortcut
    # so a rate-limited run resumes as soon as the window is mostly free.
    assert "strict_reset" not in r
    assert r["auto_resume"] is True
    assert r["auto_resume_count"] == 1
    assert r["resume_session_id"] == "sess-abc123"
    assert "Continue the interrupted task" in r["prompt"]
    assert "Run a big task" in r["prompt"]
    assert r["id"].startswith("def-")


@pytest.mark.asyncio
async def test_auto_resume_no_record_on_success(tmp_path):
    """_maybe_auto_resume does NOT create a record when api_error_status is None (success)."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "session_id": "sess-abc123", "api_error_status": None}

    with patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Run a task",
            last_result_event=result_event,
            resume_session_id="sess-abc123",
        )

    records = _webapp._load_deferred()
    assert records == []


@pytest.mark.asyncio
async def test_auto_resume_no_record_on_non_429(tmp_path):
    """_maybe_auto_resume does NOT create a record for non-rate-limit errors (e.g. 500)."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "session_id": "sess-abc123", "api_error_status": 500}

    with patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Run a task",
            last_result_event=result_event,
            resume_session_id="sess-abc123",
        )

    records = _webapp._load_deferred()
    assert records == []


@pytest.mark.asyncio
async def test_auto_resume_toggle_off_no_record(tmp_path):
    """When AUTO_RESUME_ON_RATE_LIMIT=0, _maybe_auto_resume creates no record."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "api_error_status": 429}

    with patch.object(_webapp, "_AUTO_RESUME_ON_RATE_LIMIT", 0), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock) as mock_notify:
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Run a task",
            last_result_event=result_event,
        )

    records = _webapp._load_deferred()
    assert records == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_auto_resume_loop_guard_notifies_not_creates(tmp_path):
    """When auto_resume_count >= AUTO_RESUME_MAX, sends TG warning instead of creating record.
    Note: default is now OFF (spec-039); patch to 1 to test the loop-guard logic."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "api_error_status": 429}

    notify_calls = []
    async def capture_notify(ctx, text):
        notify_calls.append(text)

    max_val = _webapp._AUTO_RESUME_MAX  # default 3

    with patch.object(_webapp, "_AUTO_RESUME_ON_RATE_LIMIT", 1), \
         patch.object(_webapp, "_notify_operator", side_effect=capture_notify):
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Run a task",
            last_result_event=result_event,
            parent_auto_resume_count=max_val,  # at or above limit
        )

    records = _webapp._load_deferred()
    assert records == []  # no new deferred record
    assert len(notify_calls) == 1
    assert "Manual restart required" in notify_calls[0] or "limit" in notify_calls[0].lower()


@pytest.mark.asyncio
async def test_auto_resume_counter_increments(tmp_path):
    """auto_resume_count is incremented correctly in the created record.
    Note: default is now OFF (spec-039); patch to 1 to test the counter logic."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "api_error_status": 429}

    with patch.object(_webapp, "_AUTO_RESUME_ON_RATE_LIMIT", 1), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock):
        # Simulate 2nd in chain (parent_count=2; max default=3 so still allowed)
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Original task text",
            last_result_event=result_event,
            parent_auto_resume_count=2,
        )

    records = _webapp._load_deferred()
    assert len(records) == 1
    assert records[0]["auto_resume_count"] == 3  # parent 2 + 1


@pytest.mark.asyncio
async def test_auto_resume_notification_sent(tmp_path):
    """_maybe_auto_resume sends a TG notification on successful auto-resume creation.
    Note: default is now OFF (spec-039); patch to 1 to test notification path."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "api_error_status": 429}

    notify_calls = []
    async def capture_notify(ctx, text):
        notify_calls.append(text)

    with patch.object(_webapp, "_AUTO_RESUME_ON_RATE_LIMIT", 1), \
         patch.object(_webapp, "_notify_operator", side_effect=capture_notify):
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Deploy to production",
            last_result_event=result_event,
        )

    assert len(notify_calls) == 1
    msg = notify_calls[0]
    assert "myproject" in msg
    assert "rate-limited" in msg or "⏸" in msg


@pytest.mark.asyncio
async def test_auto_resume_none_result_event_noop(tmp_path):
    """_maybe_auto_resume is a no-op when last_result_event is None."""
    ctx = _make_fake_ctx_with_topic(tmp_path)

    with patch.object(_webapp, "_notify_operator", new_callable=AsyncMock) as mock_notify:
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="100:10",
            original_prompt="Some task",
            last_result_event=None,
        )

    records = _webapp._load_deferred()
    assert records == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_auto_resume_unknown_session_key_noop(tmp_path):
    """_maybe_auto_resume is a no-op when session_key not in topics."""
    ctx = _make_fake_ctx_with_topic(tmp_path)
    result_event = {"type": "result", "api_error_status": 429}

    with patch.object(_webapp, "_notify_operator", new_callable=AsyncMock) as mock_notify:
        await _webapp._maybe_auto_resume(
            ctx=ctx,
            session_key="999:999",  # not in topics
            original_prompt="Some task",
            last_result_event=result_event,
        )

    records = _webapp._load_deferred()
    assert records == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_execute_deferred_uses_resume_session_id(tmp_path):
    """_execute_deferred passes record['resume_session_id'] to run_engine when present."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    _webapp._DEFERRED_FILE = data / "deferred.json"

    session_key = "100:10"
    record = {
        "id": "def-testresume",
        "project": "myproject",
        "session_key": session_key,
        "prompt": "Continue the interrupted task",
        "fire_at": None,
        "fire_on_reset": True,
        "created": _webapp._utcnow_iso(),
        "status": "fired",
        "fired_at": _webapp._utcnow_iso(),
        "error": None,
        "attempts": 0,
        "auto_resume": True,
        "auto_resume_count": 1,
        "resume_session_id": "interrupted-session-xyz",
        "original_prompt_preview": "Build the thing",
    }
    _webapp._save_deferred([record])

    run_engine_calls = []

    async def mock_run_engine(**kwargs):
        run_engine_calls.append(kwargs)
        # Yield a successful result with no rate-limit
        yield {"type": "result", "session_id": "new-session-456", "api_error_status": None}

    ctx = {
        "topics": {session_key: {"project": "myproject", "cwd": str(tmp_path), "model": "sonnet"}},
        "sessions": {},
        "running": {session_key: True},
        "rate_limits": {},
        "ptb_app": None,
        "run_engine": mock_run_engine,
        "save_sessions": lambda: None,
    }

    with patch.object(_webapp, "_notify_operator", new_callable=AsyncMock), \
         patch.object(_webapp, "_maybe_auto_resume", new_callable=AsyncMock), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        await _webapp._execute_deferred(ctx, record)

    assert len(run_engine_calls) == 1
    assert run_engine_calls[0]["resume_session_id"] == "interrupted-session-xyz"

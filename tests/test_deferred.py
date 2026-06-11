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

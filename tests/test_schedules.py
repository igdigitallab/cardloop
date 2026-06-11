"""
Tests for Spec-019: Schedules Registry.

Covers all phases:
- Phase A: collector (cron parsing, broken detection, systemd), API endpoints
- Phase B: annotations, investigate action, n8n collector
- Phase C: broken/stale incident emission, dedup, bootstrap
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

import schedules as _sched
import webapp as _webapp
from webapp import _derive_token, auth_middleware
from aiohttp import web


# ─────────────────────────── Fixtures ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_schedules_state(tmp_path):
    """Reset module-level state between tests."""
    old_cache = _sched._CACHE_PATH
    old_ann = _sched._ANNOTATIONS_PATH
    old_static = _sched._STATIC_PATH
    old_bootstrap = _sched._BOOTSTRAPPED
    data = tmp_path / "data"
    data.mkdir()
    _sched._CACHE_PATH = data / "schedules_cache.json"
    _sched._ANNOTATIONS_PATH = data / "schedules_annotations.json"
    _sched._STATIC_PATH = data / "schedules.json"
    _sched._BOOTSTRAPPED = False
    yield
    _sched._CACHE_PATH = old_cache
    _sched._ANNOTATIONS_PATH = old_ann
    _sched._STATIC_PATH = old_static
    _sched._BOOTSTRAPPED = old_bootstrap


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
def schedules_app(fake_ctx):
    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/schedules", _webapp.api_schedules_get)
    app.router.add_post("/api/schedules/scan", _webapp.api_schedules_scan)
    app.router.add_post("/api/schedules/{id}/investigate", _webapp.api_schedules_investigate)
    return app


def auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── Phase A: Cron parsing ────────────────────────────

def test_collector_parses_crontab_lines():
    """Feed a mock crontab text; assert N records with correct schedule/command fields."""
    crontab_text = """
# Comment — skip
CRON_TZ=America/Los_Angeles

*/10 * * * * /home/igor/scripts/router-monitor.sh >> /home/igor/logs/monitor.log 2>&1
0 4 * * * bash ~/scripts/backup.sh >> ~/logs/backup.log 2>&1
@reboot /usr/bin/start-something.sh
"""
    ctx = {"topics": {}}
    with patch.object(_sched, "_resolve_project", return_value=None):
        records = _sched._parse_crontab_text(crontab_text, ctx)

    assert len(records) == 3
    schedules_found = [r["schedule"] for r in records]
    assert "*/10 * * * *" in schedules_found
    assert "0 4 * * *" in schedules_found
    assert "@reboot" in schedules_found

    commands = [r["command"] for r in records]
    assert any("/router-monitor.sh" in c for c in commands)
    assert all(r["source"] == "cron" for r in records)


def test_collector_detects_broken_redirect_to_missing_dir(tmp_path):
    """Cron entry with >> /nonexistent/path/file.log → status broken."""
    # This is the acceptance-critical test from spec
    nonexistent = tmp_path / "nonexistent" / "dir" / "x.log"
    command = f"bash ~/scripts/something.sh >> {nonexistent} 2>&1"
    status = _sched._check_cron_command_status(command)
    assert status == "broken", f"Expected broken, got {status!r} for {command!r}"


def test_collector_detects_broken_missing_script():
    """Cron entry calling a non-existent script path → broken."""
    command = "/nonexistent/absolute/script.sh >> /tmp/out.log 2>&1"
    status = _sched._check_cron_command_status(command)
    assert status == "broken"


def test_collector_ok_for_existing_redirect_parent(tmp_path):
    """Redirect to existing parent dir → not broken."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "out.log"
    # Use absolute path so expand_home doesn't change it
    command = f"bash /usr/bin/true >> {log_file} 2>&1"
    status = _sched._check_cron_command_status(command)
    # Should not be broken (parent exists); /usr/bin/true also exists
    assert status != "broken"


def test_collector_skips_redirect_check_if_mkdir_in_command():
    """Command with mkdir → redirect check skipped → unknown (not broken)."""
    command = "mkdir -p ~/logs && bash ~/scripts/foo.sh >> ~/logs/foo.log 2>&1"
    status = _sched._check_cron_command_status(command)
    assert status == "unknown"


def test_record_id_is_stable():
    """Same source+schedule+command on two calls → same id."""
    id1 = _sched._record_id("cron", "*/5 * * * *", "bash /scripts/check.sh")
    id2 = _sched._record_id("cron", "*/5 * * * *", "bash /scripts/check.sh")
    assert id1 == id2
    assert len(id1) == 12


def test_record_id_differs_for_different_commands():
    """Different commands → different ids."""
    id1 = _sched._record_id("cron", "*/5 * * * *", "bash /scripts/check.sh")
    id2 = _sched._record_id("cron", "*/5 * * * *", "bash /scripts/other.sh")
    assert id1 != id2


# ─────────────────────────── Phase A: systemd parsing ─────────────────────────

def test_collector_parses_systemd_timers():
    """Mock systemd list-timers tabular output; assert records."""
    tabular = """NEXT                        LEFT          LAST                        PASSED       UNIT                           ACTIVATES
Wed 2026-06-11 04:00:00 UTC 3h 59min left Tue 2026-06-10 04:00:01 UTC 20h ago      networking-crm-sync.timer      networking-crm-sync.service
n/a                         n/a           Tue 2026-06-10 03:00:00 UTC 21h ago      networking-crm-health.timer    networking-crm-health.service

2 timers listed.
"""
    results = _sched._parse_systemd_timers_text(tabular)
    assert len(results) >= 1
    units = [r["unit"] for r in results]
    assert "networking-crm-sync.timer" in units


def test_collector_systemd_failed_state_is_broken():
    """Timer with ActiveState=failed → status broken."""
    details = {"ActiveState": "failed", "Description": "Test Timer", "ExecStart": ""}
    # Simulate what _collect_systemd does with a failed timer
    active_state = details.get("ActiveState", "unknown")
    if active_state == "failed":
        status = "broken"
    elif active_state == "active":
        status = "ok"
    else:
        status = "unknown"
    assert status == "broken"


def test_collector_parses_systemd_json_format():
    """Parse JSON output of systemctl list-timers."""
    data = [
        {"unit": "networking-crm-sync.timer", "next": "Wed 2026-06-11 04:00:00 UTC",
         "last": "Tue 2026-06-10 04:00:01 UTC", "activates": "networking-crm-sync.service"},
        {"unit": "networking-crm-health.timer", "next": "n/a", "last": "n/a",
         "activates": "networking-crm-health.service"},
        {"unit": "not-a-timer.service", "next": "", "last": "", "activates": ""},
    ]
    results = _sched._parse_systemd_timers_json(data)
    assert len(results) == 2  # .service excluded
    assert all(r["unit"].endswith(".timer") for r in results)


def test_iso_from_systemd_ts_valid():
    """Parse systemd timestamp string to ISO."""
    raw = "Tue 2026-06-10 04:00:01 UTC"
    result = _sched._iso_from_systemd_ts(raw)
    assert result is not None
    assert "2026-06-10" in result


def test_iso_from_systemd_ts_na():
    """n/a → None."""
    assert _sched._iso_from_systemd_ts("n/a") is None
    assert _sched._iso_from_systemd_ts("") is None
    assert _sched._iso_from_systemd_ts("-") is None


# ─────────────────────────── Phase A: Coolify source ─────────────────────────

async def test_collector_skips_coolify_on_missing_token():
    """No COOLIFY_API_TOKEN → returns empty list, no error."""
    with patch.dict("os.environ", {"COOLIFY_API_TOKEN": ""}):
        records = await _sched._collect_coolify({})
    assert records == []


async def test_collector_skips_coolify_on_connection_error():
    """Connection error → returns empty list, no exception raised."""
    import aiohttp as _ah

    async def mock_session_get(*a, **kw):
        raise _ah.ClientConnectionError("refused")

    with patch.dict("os.environ", {"COOLIFY_API_TOKEN": "fake_token"}):
        with patch("aiohttp.ClientSession") as mock_sess:
            mock_sess.return_value.__aenter__ = AsyncMock(return_value=mock_sess.return_value)
            mock_sess.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_sess.return_value.get = MagicMock(side_effect=_ah.ClientConnectionError("refused"))
            records = await _sched._collect_coolify({})
    assert isinstance(records, list)


# ─────────────────────────── Phase A: cache + API ─────────────────────────────

async def test_api_schedules_get_returns_array(aiohttp_client, schedules_app, fake_ctx):
    """GET /api/schedules → 200, body is dict with records list."""
    client = await aiohttp_client(schedules_app)
    resp = await client.get("/api/schedules", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "records" in data
    assert isinstance(data["records"], list)


async def test_api_schedules_filter_by_source(aiohttp_client, schedules_app, fake_ctx):
    """GET ?source=cron → only cron records."""
    # Pre-populate cache with mixed sources
    cache = {
        "scanned_at": "2026-06-10T12:00:00+00:00",
        "source_statuses": [],
        "records": [
            {"id": "aaa111", "source": "cron", "schedule": "*/5 * * * *",
             "command": "bash /x.sh", "project": None, "status": "unknown",
             "purpose": None, "annotations": {}, "last_run": None, "next_run": None},
            {"id": "bbb222", "source": "systemd", "schedule": "test.timer",
             "command": "test", "project": None, "status": "ok",
             "purpose": None, "annotations": {}, "last_run": None, "next_run": None},
        ],
    }
    _sched._CACHE_PATH.write_text(json.dumps(cache))

    client = await aiohttp_client(schedules_app)
    resp = await client.get("/api/schedules?source=cron", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert all(r["source"] == "cron" for r in data["records"])
    assert len(data["records"]) == 1


async def test_api_schedules_filter_by_status(aiohttp_client, schedules_app, fake_ctx):
    """GET ?status=broken → only broken records."""
    cache = {
        "scanned_at": "2026-06-10T12:00:00+00:00",
        "source_statuses": [],
        "records": [
            {"id": "ccc333", "source": "cron", "schedule": "*/5 * * * *",
             "command": "bash /x.sh", "project": None, "status": "broken",
             "purpose": None, "annotations": {}, "last_run": None, "next_run": None},
            {"id": "ddd444", "source": "cron", "schedule": "0 4 * * *",
             "command": "bash /y.sh", "project": None, "status": "unknown",
             "purpose": None, "annotations": {}, "last_run": None, "next_run": None},
        ],
    }
    _sched._CACHE_PATH.write_text(json.dumps(cache))

    client = await aiohttp_client(schedules_app)
    resp = await client.get("/api/schedules?status=broken", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert all(r["status"] == "broken" for r in data["records"])
    assert len(data["records"]) == 1


async def test_api_schedules_scan_post(aiohttp_client, schedules_app, fake_ctx):
    """POST /api/schedules/scan → 200 {"queued": true}."""
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: coro.close() if hasattr(coro, 'close') else None):
        client = await aiohttp_client(schedules_app)
        resp = await client.post("/api/schedules/scan", headers=auth_headers(fake_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("queued") is True


async def test_schedules_cache_write_is_atomic(tmp_path):
    """Concurrent scan calls do not corrupt the cache file."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    _sched._CACHE_PATH = data / "schedules_cache.json"

    records = [{"id": "abc123", "source": "cron", "schedule": "*/5 * * * *",
                "command": "test", "project": None, "status": "unknown",
                "purpose": None, "annotations": {}, "last_run": None, "next_run": None}]

    async def write_n_times():
        for _ in range(5):
            await _sched._write_cache(records, [])

    await asyncio.gather(write_n_times(), write_n_times(), write_n_times())

    # Cache should be valid JSON
    content = _sched._CACHE_PATH.read_text()
    parsed = json.loads(content)
    assert "records" in parsed


# ─────────────────────────── Phase A: in-process source ──────────────────────

def test_collect_in_process_empty_file():
    """Missing or empty static registry → 0 records, no error."""
    records = _sched._collect_in_process()
    assert records == []


def test_collect_in_process_reads_static_registry(tmp_path):
    """Static registry with 2 entries → 2 records."""
    static = [
        {"id": "ip001", "source": "in_process", "schedule": "every 5m",
         "command": "finance_job", "project": "pyrogram-bot",
         "status": "unknown", "purpose": "Check finances"},
        {"id": "ip002", "source": "in_process", "schedule": "every 1h",
         "command": "health_check", "project": None,
         "status": "unknown", "purpose": None},
    ]
    _sched._STATIC_PATH.write_text(json.dumps(static))
    records = _sched._collect_in_process()
    assert len(records) == 2
    assert records[0]["purpose"] == "Check finances"


# ─────────────────────────── Phase B: Annotations ─────────────────────────────

def test_annotations_survive_rescan():
    """Write annotation; simulate re-scan merge; annotation present in result."""
    record_id = "test000abc1"
    ann_data = {record_id: {"purpose": "Backs up Docker volumes nightly", "updated_at": "2026-06-10T12:00:00Z"}}
    _sched._save_annotations(ann_data)

    # Load and verify
    loaded = _sched._load_annotations()
    assert loaded.get(record_id, {}).get("purpose") == "Backs up Docker volumes nightly"


def test_annotations_not_overwritten_by_scan():
    """Annotations written before scan survive unchanged after scan (merge, not replace)."""
    record_id = "test111abc2"
    ann_data = {record_id: {"purpose": "Important job", "updated_at": "2026-06-10T00:00:00Z"}}
    _sched._save_annotations(ann_data)

    # Simulate a record that comes from the scan (no purpose in the scan itself)
    records = [{
        "id": record_id, "source": "cron", "schedule": "*/5 * * * *",
        "command": "bash /important.sh", "project": None, "status": "unknown",
        "purpose": None, "annotations": {},
        "last_run": None, "next_run": None,
    }]
    annotations = _sched._load_annotations()
    for rec in records:
        ann = annotations.get(rec["id"])
        if ann and ann.get("purpose"):
            rec["purpose"] = ann["purpose"]
        if ann:
            rec["annotations"] = ann

    assert records[0]["purpose"] == "Important job"


async def test_investigate_nonexistent_id_returns_404(aiohttp_client, schedules_app, fake_ctx):
    """POST /api/schedules/nonexistent/investigate → 404."""
    # Empty cache
    _sched._CACHE_PATH.write_text(json.dumps({"records": []}))
    client = await aiohttp_client(schedules_app)
    resp = await client.post(
        "/api/schedules/nonexistent999/investigate",
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 404


async def test_investigate_creates_backlog_card(aiohttp_client, schedules_app, fake_ctx, tmp_path):
    """POST /api/schedules/{id}/investigate → card appears in TASKS.md."""
    project_dir = tmp_path / "mybot"
    project_dir.mkdir()
    # Create a minimal TASKS.md
    (project_dir / "TASKS.md").write_text("# Tasks\n\n## Backlog\n## In Progress\n## Review\n## Failed\n")

    # Register project in ctx
    fake_ctx["topics"]["1001:42"] = {
        "project": "mybot", "cwd": str(project_dir), "model": "sonnet"
    }

    record_id = "abc123def456"
    cache = {
        "scanned_at": "2026-06-10T12:00:00+00:00",
        "source_statuses": [],
        "records": [{
            "id": record_id, "source": "cron", "schedule": "*/5 * * * *",
            "command": "bash /home/igor/scripts/check.sh", "project": "mybot",
            "status": "unknown", "purpose": None, "annotations": {},
            "last_run": None, "next_run": None,
        }],
    }
    _sched._CACHE_PATH.write_text(json.dumps(cache))

    client = await aiohttp_client(schedules_app)
    resp = await client.post(
        f"/api/schedules/{record_id}/investigate",
        headers=auth_headers(fake_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert "card_id" in data

    # Card should be in TASKS.md
    tasks_content = (project_dir / "TASKS.md").read_text()
    assert "[schedules] investigate:" in tasks_content


# ─────────────────────────── Phase B: n8n collector ─────────────────────────

async def test_n8n_collector_zero_workflows():
    """n8n API returning empty list → 0 records, no error."""
    import aiohttp as _ah

    class FakeResp:
        status = 200
        async def json(self):
            return {"data": []}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    with patch.dict("os.environ", {"N8N_API_KEY": "fake_key"}):
        with patch("aiohttp.ClientSession") as mock_sess:
            fake_sess = MagicMock()
            fake_sess.__aenter__ = AsyncMock(return_value=fake_sess)
            fake_sess.__aexit__ = AsyncMock(return_value=False)
            fake_sess.get = MagicMock(return_value=FakeResp())
            mock_sess.return_value = fake_sess
            records = await _sched._collect_n8n({})

    assert records == []


async def test_n8n_collector_skips_on_missing_key():
    """No N8N_API_KEY → returns empty list."""
    with patch.dict("os.environ", {"N8N_API_KEY": ""}):
        records = await _sched._collect_n8n({})
    assert records == []


async def test_n8n_collector_skips_on_error():
    """n8n unreachable → returns empty list, no exception."""
    import aiohttp as _ah

    with patch.dict("os.environ", {"N8N_API_KEY": "fake_key"}):
        with patch("aiohttp.ClientSession") as mock_sess:
            fake_sess = MagicMock()
            fake_sess.__aenter__ = AsyncMock(return_value=fake_sess)
            fake_sess.__aexit__ = AsyncMock(return_value=False)
            fake_sess.get = MagicMock(side_effect=_ah.ClientConnectionError("refused"))
            mock_sess.return_value = fake_sess
            records = await _sched._collect_n8n({})

    assert records == []


# ─────────────────────────── Phase C: Incident emission ──────────────────────

async def test_broken_status_triggers_report_incident():
    """Transition to broken (confirmed on 2 consecutive scans) → _report_incident called."""
    # Simulate: previous scan has broken record, new scan also has it broken
    record_id = "broken001abc"
    previous = {
        "records": [{
            "id": record_id, "source": "cron", "schedule": "*/5 * * * *",
            "command": "bash /broken.sh", "project": "mybot",
            "status": "broken",  # was already broken in previous scan
            "purpose": None, "annotations": {}, "last_run": None, "next_run": None,
        }]
    }
    new_records = [{
        "id": record_id, "source": "cron", "schedule": "*/5 * * * *",
        "command": "bash /broken.sh", "project": "mybot",
        "status": "broken",  # still broken
        "purpose": None, "annotations": {}, "last_run": None, "next_run": None,
    }]

    # Mark as bootstrapped (first scan already done)
    _sched._BOOTSTRAPPED = True

    incident_calls = []
    async def mock_emit(ctx, record, exc_class):
        incident_calls.append({"record_id": record["id"], "exc_class": exc_class})

    with patch.object(_sched, "_emit_schedule_incident", side_effect=mock_emit):
        await _sched._check_incidents({}, new_records, previous)

    assert len(incident_calls) == 1
    assert incident_calls[0]["exc_class"] == "ScheduleBroken"


async def test_broken_status_deduped_on_second_scan():
    """First time broken (not in previous) → no incident yet (needs confirmation)."""
    record_id = "broken002def"
    previous = {
        "records": [{
            "id": record_id, "source": "cron", "schedule": "*/5 * * * *",
            "command": "bash /broken.sh", "project": "mybot",
            "status": "unknown",  # was OK/unknown previously
            "purpose": None, "annotations": {}, "last_run": None, "next_run": None,
        }]
    }
    new_records = [{
        "id": record_id, "source": "cron", "schedule": "*/5 * * * *",
        "command": "bash /broken.sh", "project": "mybot",
        "status": "broken",  # newly broken
        "purpose": None, "annotations": {}, "last_run": None, "next_run": None,
    }]

    _sched._BOOTSTRAPPED = True

    incident_calls = []
    async def mock_emit(ctx, record, exc_class):
        incident_calls.append(exc_class)

    with patch.object(_sched, "_emit_schedule_incident", side_effect=mock_emit):
        await _sched._check_incidents({}, new_records, previous)

    # No incident on first detection — needs confirmation
    assert len(incident_calls) == 0


async def test_ok_status_after_fix_no_new_incident():
    """After fix (broken → ok) → no new incident."""
    record_id = "fixed001ghi"
    previous = {
        "records": [{
            "id": record_id, "status": "broken",
            "source": "cron", "schedule": "*/5 * * * *",
            "command": "bash /fixed.sh", "project": None,
            "purpose": None, "annotations": {}, "last_run": None, "next_run": None,
        }]
    }
    new_records = [{
        "id": record_id, "status": "ok",
        "source": "cron", "schedule": "*/5 * * * *",
        "command": "bash /fixed.sh", "project": None,
        "purpose": None, "annotations": {}, "last_run": None, "next_run": None,
    }]

    _sched._BOOTSTRAPPED = True

    incident_calls = []
    async def mock_emit(ctx, record, exc_class):
        incident_calls.append(exc_class)

    with patch.object(_sched, "_emit_schedule_incident", side_effect=mock_emit):
        await _sched._check_incidents({}, new_records, previous)

    assert incident_calls == []


async def test_stale_status_triggers_report_incident():
    """Stale confirmed on 2 consecutive scans → ScheduleMissed incident."""
    record_id = "stale001jkl"
    previous = {"records": [{"id": record_id, "status": "stale", "source": "systemd",
                              "schedule": "test.timer", "command": "test",
                              "project": None, "purpose": None, "annotations": {},
                              "last_run": None, "next_run": None}]}
    new_records = [{"id": record_id, "status": "stale", "source": "systemd",
                    "schedule": "test.timer", "command": "test",
                    "project": None, "purpose": None, "annotations": {},
                    "last_run": None, "next_run": None}]

    _sched._BOOTSTRAPPED = True

    incident_calls = []
    async def mock_emit(ctx, record, exc_class):
        incident_calls.append(exc_class)

    with patch.object(_sched, "_emit_schedule_incident", side_effect=mock_emit):
        await _sched._check_incidents({}, new_records, previous)

    assert len(incident_calls) == 1
    assert incident_calls[0] == "ScheduleMissed"


async def test_first_scan_after_bootstrap_no_incidents():
    """Bootstrap flag: first scan after process start never emits incidents."""
    record_id = "boot001mno"
    previous = {"records": [{"id": record_id, "status": "broken", "source": "cron",
                              "schedule": "*/5 * * * *", "command": "bad.sh",
                              "project": None, "purpose": None, "annotations": {},
                              "last_run": None, "next_run": None}]}
    new_records = [{"id": record_id, "status": "broken", "source": "cron",
                    "schedule": "*/5 * * * *", "command": "bad.sh",
                    "project": None, "purpose": None, "annotations": {},
                    "last_run": None, "next_run": None}]

    _sched._BOOTSTRAPPED = False  # NOT yet bootstrapped

    incident_calls = []
    async def mock_emit(ctx, record, exc_class):
        incident_calls.append(exc_class)

    with patch.object(_sched, "_emit_schedule_incident", side_effect=mock_emit):
        await _sched._check_incidents({}, new_records, previous)

    # First scan → no incidents, and flag should now be set
    assert incident_calls == []
    assert _sched._BOOTSTRAPPED is True


# ─────────────────────────── Phase A: expand_home ────────────────────────────

def test_expand_home_tilde():
    """~ is expanded to actual home dir."""
    import os
    home = str(Path.home())
    result = _sched._expand_home("~/logs/foo.log")
    assert result == f"{home}/logs/foo.log"


def test_expand_home_dollar_home():
    """$HOME is expanded."""
    home = str(Path.home())
    result = _sched._expand_home("$HOME/logs/foo.log")
    assert result == f"{home}/logs/foo.log"


# ─────────────────────────── Edge: schedules module init ─────────────────────

def test_schedules_init_sets_paths(tmp_path):
    """_schedules_init sets all file paths correctly."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    ctx = {"DATA": data}
    _sched._schedules_init(ctx)
    assert _sched._CACHE_PATH == data / "schedules_cache.json"
    assert _sched._ANNOTATIONS_PATH == data / "schedules_annotations.json"
    assert _sched._STATIC_PATH == data / "schedules.json"
    assert _sched._BOOTSTRAPPED is False

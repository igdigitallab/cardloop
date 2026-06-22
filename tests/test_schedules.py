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

*/10 * * * * /home/youruser/scripts/router-monitor.sh >> /home/youruser/logs/monitor.log 2>&1
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

    # Regression: command must be the FULL remainder including the redirect tail.
    # The original parser used split(None, 6) and kept only the first token
    # ("/home/youruser/scripts/router-monitor.sh" without ">> ... 2>&1"), which made
    # broken-redirect detection and the mtime last_run heuristic blind.
    monitor_cmd = next(c for c in commands if "/router-monitor.sh" in c)
    assert ">> /home/youruser/logs/monitor.log 2>&1" in monitor_cmd, monitor_cmd


def test_collector_cron_d_full_command_with_redirect():
    """cron.d format: command remainder after the user field is kept whole."""
    text = "30 4 * * * root /usr/local/bin/backup-volumes.sh >> /var/log/backup.log 2>&1\n"
    with patch.object(_sched, "_resolve_project", return_value=None):
        records = _sched._parse_crontab_d_text(text, {"topics": {}}, "/etc/cron.d/backup")
    assert len(records) == 1
    assert records[0]["command"] == "/usr/local/bin/backup-volumes.sh >> /var/log/backup.log 2>&1"
    assert "root" not in records[0]["command"].split(">>")[0].split("/")[0]


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


# ── Cron last_run heuristic (redirect-target mtime) ─────────────────────────

def test_cron_interval_minutes():
    """Rough interval estimation from common cron shapes."""
    assert _sched._cron_interval_minutes("*/10 * * * *") == 10
    assert _sched._cron_interval_minutes("*/5 * * * *") == 5
    assert _sched._cron_interval_minutes("30 * * * *") == 60
    assert _sched._cron_interval_minutes("0 4 * * *") == 24 * 60
    assert _sched._cron_interval_minutes("0 4 * * 0") == 7 * 24 * 60
    assert _sched._cron_interval_minutes("@reboot") is None
    assert _sched._cron_interval_minutes("0 9 */3 * *") is None  # complex → no guess


def test_cron_fresh_redirect_mtime_promotes_to_ok(tmp_path):
    """Redirect target with fresh mtime + known interval → status ok, last_run set."""
    log = tmp_path / "fresh.log"
    log.write_text("output\n")  # mtime = now
    command = f"/usr/bin/true >> {log} 2>&1"
    rec = _sched._cron_record("*/10 * * * *", command, {"topics": {}})
    assert rec["status"] == "ok", rec
    assert rec["last_run"] is not None


def test_cron_old_redirect_mtime_stays_unknown_not_stale(tmp_path):
    """CORE SEMANTICS: an old mtime must NOT demote to stale — `>>` with empty
    output does not update mtime, so an old mtime is not proof the job stopped."""
    import os
    log = tmp_path / "old.log"
    log.write_text("old output\n")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(log, (two_hours_ago, two_hours_ago))
    command = f"/usr/bin/true >> {log} 2>&1"
    rec = _sched._cron_record("*/10 * * * *", command, {"topics": {}})
    assert rec["status"] == "unknown", f"old mtime must stay unknown, got {rec['status']}"
    assert rec["last_run"] is not None  # mtime still recorded as best-effort last_run


def test_cron_dev_null_redirect_stays_unknown():
    """Redirect to /dev/null carries no run evidence → unknown, no last_run."""
    command = "/usr/bin/true >/dev/null 2>&1"
    rec = _sched._cron_record("*/10 * * * *", command, {"topics": {}})
    assert rec["status"] == "unknown"
    assert rec["last_run"] is None


def test_cron_missing_redirect_file_stays_unknown(tmp_path):
    """Redirect target absent but parent exists → unknown (no false broken/stale)."""
    log = tmp_path / "never-written.log"  # parent exists, file doesn't
    command = f"/usr/bin/true >> {log} 2>&1"
    rec = _sched._cron_record("*/10 * * * *", command, {"topics": {}})
    assert rec["status"] == "unknown"
    assert rec["last_run"] is None


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
    """Tabular fallback parser: unit names + ISO timestamps from header positions."""
    tabular = """NEXT                        LEFT          LAST                        PASSED       UNIT                           ACTIVATES
Wed 2026-06-11 04:00:00 UTC 3h 59min left Tue 2026-06-10 04:00:01 UTC 20h ago      networking-crm-sync.timer      networking-crm-sync.service
n/a                         n/a           Tue 2026-06-10 03:00:00 UTC 21h ago      networking-crm-health.timer    networking-crm-health.service

2 timers listed.
"""
    results = _sched._parse_systemd_timers_text(tabular)
    assert len(results) >= 1
    by_unit = {r["unit"]: r for r in results}
    assert "networking-crm-sync.timer" in by_unit
    sync = by_unit["networking-crm-sync.timer"]
    assert sync["next_iso"] is not None and "2026-06-11" in sync["next_iso"]
    assert sync["last_iso"] is not None and "2026-06-10" in sync["last_iso"]


def test_collector_systemd_failed_state_is_broken():
    """Timer with ActiveState=failed → status broken."""
    assert _sched._systemd_status("failed", None, None) == "broken"


# REAL fixture captured from this host (systemd 255, 2026-06-10):
# `systemctl list-timers --all --output=json` emits next/last as INTEGER
# microsecond unix timestamps — NOT strings. The original parser expected
# strings, got None timestamps, and the old status logic turned None into
# "stale" → 21 false ScheduleMissed incidents. These tests pin both fixes.
_REAL_LIST_TIMERS_JSON = [
    {"next": 1781150581843041, "left": 1781150581843041, "last": 1781150281833140,
     "passed": 2651253926487, "unit": "networking-crm-calls.timer",
     "activates": "networking-crm-calls.service"},
    {"next": 1781150700000000, "left": 1781150700000000, "last": 1781150401228954,
     "passed": 2651373322302, "unit": "networking-crm-ticktick-backfill.timer",
     "activates": "networking-crm-ticktick-backfill.service"},
    {"next": 1781150820000000, "left": 1781150820000000, "last": 1781149921855165,
     "passed": 2650893948512, "unit": "networking-crm-gcal-push.timer",
     "activates": "networking-crm-gcal-push.service"},
]


def test_systemd_json_real_fixture_microsecond_timestamps():
    """Regression: real list-timers JSON (int microseconds) parses into ISO timestamps."""
    results = _sched._parse_systemd_timers_json(_REAL_LIST_TIMERS_JSON)
    assert len(results) == 3
    for r in results:
        assert r["next_iso"] is not None, f"next_iso None for {r['unit']} — usec parse broken"
        assert r["last_iso"] is not None, f"last_iso None for {r['unit']} — usec parse broken"
        assert r["next_iso"].startswith("2026-06-1"), r["next_iso"]
        assert r["last_iso"].startswith("2026-06-1"), r["last_iso"]


def test_systemd_real_fixture_active_timer_is_ok():
    """Regression: a healthy timer from the real fixture must derive status=ok
    (when 'now' is within its schedule window), not stale."""
    from datetime import datetime, timezone
    results = _sched._parse_systemd_timers_json(_REAL_LIST_TIMERS_JSON)
    r = results[0]  # networking-crm-calls.timer
    # 'now' = the moment of its last trigger → next elapse is in the future
    now = datetime.fromtimestamp(1781150281833140 / 1e6, tz=timezone.utc)
    status = _sched._systemd_status("active", r["last_iso"], r["next_iso"], now=now)
    assert status == "ok"


def test_usec_to_iso_valid():
    """Int microseconds → ISO UTC string."""
    iso = _sched._usec_to_iso(1781150581843041)
    assert iso is not None and iso.startswith("2026-06-1")


def test_usec_to_iso_invalid():
    """0 / None / garbage → None (never a fake timestamp)."""
    assert _sched._usec_to_iso(0) is None
    assert _sched._usec_to_iso(None) is None
    assert _sched._usec_to_iso("not-a-number") is None
    assert _sched._usec_to_iso(-5) is None


# ── CORE SEMANTICS regression: unknown ≠ stale ────────────────────────────────

def test_systemd_status_unknown_next_is_unknown_not_stale():
    """THE regression test: active timer with unparseable/missing next_run
    must be 'unknown', NEVER 'stale'. The original bug flooded the board with
    21 false ScheduleMissed incidents for perfectly healthy timers."""
    assert _sched._systemd_status("active", None, None) == "unknown"
    assert _sched._systemd_status("active", "2026-06-10T04:00:00+00:00", None) == "unknown"


def test_systemd_status_future_next_is_ok():
    """Active timer with next elapse in the future → ok."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 11, 4, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(minutes=5)).isoformat()
    assert _sched._systemd_status("active", None, future, now=now) == "ok"


def test_systemd_status_past_next_beyond_grace_is_stale():
    """Active timer whose next elapse is in the past beyond grace → stale
    (positive evidence: it should have fired and didn't)."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 11, 4, 0, 0, tzinfo=timezone.utc)
    past = (now - timedelta(hours=2)).isoformat()
    assert _sched._systemd_status("active", None, past, now=now) == "stale"


def test_systemd_status_past_next_within_grace_is_ok():
    """Next elapse slightly in the past (within grace) → ok, not stale."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 11, 4, 0, 0, tzinfo=timezone.utc)
    just_past = (now - timedelta(seconds=30)).isoformat()
    assert _sched._systemd_status("active", None, just_past, now=now) == "ok"


def test_systemd_status_inactive_is_unknown():
    """Inactive/other states → unknown."""
    assert _sched._systemd_status("inactive", None, "2026-06-11T04:00:00+00:00") == "unknown"
    assert _sched._systemd_status("unknown", None, None) == "unknown"


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
            "command": "bash /home/youruser/scripts/check.sh", "project": "mybot",
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


# ─────────────────────────── Task 1: systemd project resolution ───────────────

def test_resolve_project_unit_name_prefix_matches(tmp_path):
    """Unit name prefix heuristic: networking-crm-sync.timer → networking-os."""
    project_dir = tmp_path / "networking-os"
    project_dir.mkdir()
    fake_ctx = {
        "topics": {
            "1:1": {"project": "Networking-OS", "cwd": str(project_dir), "model": "fable"},
        },
        "sessions": {},
        "running": {},
        "DATA": tmp_path,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    # Command is just /usr/bin/curl (no path match), but unit_name prefix matches
    result = _sched._resolve_project(fake_ctx, "/usr/bin/curl", unit_name="networking-crm-sync.timer")
    assert result == "networking-os", f"Expected networking-os, got {result!r}"


def test_resolve_project_exec_start_path_match(tmp_path):
    """ExecStart argv with project venv path resolves correctly."""
    project_dir = tmp_path / "networking-os"
    project_dir.mkdir()
    venv_python = str(project_dir / ".venv" / "bin" / "python")
    fake_ctx = {
        "topics": {
            "1:1": {"project": "Networking-OS", "cwd": str(project_dir), "model": "fable"},
        },
        "sessions": {},
        "running": {},
        "DATA": tmp_path,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    # Simulate full ExecStart passed as command (argv includes project path)
    exec_start = f"{{ path=/home/youruser/networking-os/.venv/bin/python ; argv[]={venv_python} -m networking.crm ; }}"
    result = _sched._resolve_project(fake_ctx, exec_start, unit_name="networking-crm-alert.timer")
    assert result == "networking-os", f"Expected networking-os via path match, got {result!r}"


def test_resolve_project_unit_name_no_match_for_system_units(tmp_path):
    """System units like apt-daily.timer do not match project networking-os."""
    project_dir = tmp_path / "networking-os"
    project_dir.mkdir()
    fake_ctx = {
        "topics": {
            "1:1": {"project": "Networking-OS", "cwd": str(project_dir), "model": "fable"},
        },
        "sessions": {},
        "running": {},
        "DATA": tmp_path,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    # 'apt' token does not match 'networking' token
    result = _sched._resolve_project(fake_ctx, "/usr/lib/apt/apt.systemd.daily", unit_name="apt-daily.timer")
    assert result is None, f"System unit should not match any project, got {result!r}"


def test_resolve_project_unit_name_strips_timer_suffix(tmp_path):
    """Unit name with .timer suffix is stripped before token comparison."""
    project_dir = tmp_path / "example-bot"
    project_dir.mkdir()
    fake_ctx = {
        "topics": {
            "1:2": {"project": "example-bot", "cwd": str(project_dir), "model": "fable"},
        },
        "sessions": {},
        "running": {},
        "DATA": tmp_path,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    # example-health.timer → tokens [example, health]; project example-bot → tokens [example, bot]
    # share 1 token 'example' → matches example-bot
    result = _sched._resolve_project(fake_ctx, "/usr/bin/true", unit_name="example-health.timer")
    assert result == "example-bot", f"Expected example-bot, got {result!r}"


# ─────────────────────────── Task 2: in-process static registry ──────────────

def test_collect_in_process_returns_all_entries(tmp_path):
    """Static registry with 11 in-process entries reads all of them."""
    static_path = tmp_path / "schedules.json"
    entries = [
        {"id": f"ip-test-{i:03d}", "source": "in_process", "schedule": "every 5m",
         "command": f"job_{i}", "project": "example-bot",
         "status": "unknown", "purpose": f"Test job {i}", "last_run": None, "next_run": None,
         "annotations": {}}
        for i in range(11)
    ]
    static_path.write_text(json.dumps(entries))
    _sched._STATIC_PATH = static_path
    records = _sched._collect_in_process()
    assert len(records) == 11
    assert all(r["source"] == "in_process" for r in records)
    assert all(r["status"] == "unknown" for r in records)
    assert all(r["last_run"] is None for r in records)


def test_collect_in_process_preserves_project_field(tmp_path):
    """Each in-process entry keeps its declared project field."""
    static_path = tmp_path / "schedules.json"
    entries = [
        {"id": "ip-example-1", "source": "in_process", "schedule": "every 4h",
         "command": "finance_job", "project": "example-bot",
         "status": "unknown", "purpose": "Finance categorization", "last_run": None, "next_run": None,
         "annotations": {}},
        {"id": "ip-example-2", "source": "in_process", "schedule": "every 60s",
         "command": "health_job", "project": "example-bot",
         "status": "unknown", "purpose": "Health check", "last_run": None, "next_run": None,
         "annotations": {}},
    ]
    static_path.write_text(json.dumps(entries))
    _sched._STATIC_PATH = static_path
    records = _sched._collect_in_process()
    projects = {r["project"] for r in records}
    assert "example-bot" in projects

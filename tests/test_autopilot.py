"""
Tests for autopilot.py (Phase 0 — inert foundation) and the corresponding
webapp.py endpoints.

Unit tests cover:
  - valid_mode / get_project_mode
  - load_state / save_state roundtrip + defaults when file missing
  - rollover_day
  - guardrail predicates (budget_ok, concurrency_ok, pending_ok, cooldown_ok, rate_limit_ok)
  - reserve_run atomicity + release_run
  - commit_trailer format
  - trajectory append / read roundtrip
  - fingerprint stability
  - detect_loop for each signal + no-signal baseline
  - detect_self_inflicted returns None on a non-git tmp dir

Endpoint tests (aiohttp):
  - PUT /api/projects/{id}/autopilot sets and persists mode
  - invalid mode → 400
  - GET /api/autopilot/status shape
  - POST /api/autopilot/global, /pause, /resume flip flags
  - GET /api/projects/{id}/settings includes autopilot field
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from features.autopilot import logic as autopilot
import webapp as _webapp
from webapp import _derive_token
from features.autopilot import routes as _ap_routes


# ═══════════════════════════════════════════════════════════════════
# Unit tests — autopilot module
# ═══════════════════════════════════════════════════════════════════


# ─────────────────────────── valid_mode ───────────────────────────

def test_valid_mode_off():
    assert autopilot.valid_mode("off") is True


def test_valid_mode_propose():
    assert autopilot.valid_mode("propose") is True


def test_valid_mode_auto():
    assert autopilot.valid_mode("auto") is True


def test_valid_mode_unknown_string():
    assert autopilot.valid_mode("turbo") is False


def test_valid_mode_empty():
    assert autopilot.valid_mode("") is False


def test_valid_mode_non_string():
    assert autopilot.valid_mode(None) is False
    assert autopilot.valid_mode(1) is False


# ─────────────────────────── get_project_mode ───────────────────────────

def test_get_project_mode_default():
    """Project dict with no autopilot key → default mode 'off'."""
    assert autopilot.get_project_mode({}) == "off"


def test_get_project_mode_set():
    assert autopilot.get_project_mode({"autopilot": "auto"}) == "auto"


def test_get_project_mode_invalid_value_falls_back_to_default():
    assert autopilot.get_project_mode({"autopilot": "turbo"}) == "off"


def test_get_project_mode_non_string_value_falls_back():
    assert autopilot.get_project_mode({"autopilot": 42}) == "off"


# ─────────────────────────── load_state / save_state ───────────────────────────

def test_load_state_returns_defaults_when_file_missing(tmp_path):
    state = autopilot.load_state(tmp_path)
    assert state["global_enabled"] is False
    assert state["paused"] is False
    assert isinstance(state["pending_by_project"], dict)
    assert isinstance(state["cooldowns"], dict)
    assert state["tokens_today"] == 0
    assert state["active_runs"] == 0


def test_save_and_load_state_roundtrip(tmp_path):
    state = autopilot.load_state(tmp_path)
    state["global_enabled"] = True
    state["tokens_today"] = 42000
    state["day"] = "2026-06-28"
    autopilot.save_state(tmp_path, state)

    loaded = autopilot.load_state(tmp_path)
    assert loaded["global_enabled"] is True
    assert loaded["tokens_today"] == 42000
    assert loaded["day"] == "2026-06-28"


def test_load_state_fills_missing_keys_from_older_file(tmp_path):
    """A file written without newer keys → missing keys filled from defaults."""
    p = tmp_path / "autopilot_state.json"
    p.write_text(json.dumps({"global_enabled": True}), encoding="utf-8")
    state = autopilot.load_state(tmp_path)
    assert state["global_enabled"] is True
    assert "paused" in state
    assert "pending_by_project" in state


def test_load_state_handles_corrupt_file(tmp_path):
    """Corrupt JSON file → returns defaults without raising."""
    p = tmp_path / "autopilot_state.json"
    p.write_text("NOT JSON {{{{", encoding="utf-8")
    state = autopilot.load_state(tmp_path)
    assert state["global_enabled"] is False  # default


def test_save_state_creates_file(tmp_path):
    state = autopilot.load_state(tmp_path)
    autopilot.save_state(tmp_path, state)
    assert (tmp_path / "autopilot_state.json").exists()


# ─────────────────────────── rollover_day ───────────────────────────

def test_rollover_day_resets_tokens_on_new_day(tmp_path):
    state = autopilot.load_state(tmp_path)
    state["day"] = "2026-01-01"
    state["tokens_today"] = 999_000
    autopilot.rollover_day(state, "2026-01-02")
    assert state["tokens_today"] == 0
    assert state["day"] == "2026-01-02"


def test_rollover_day_noop_on_same_day(tmp_path):
    state = autopilot.load_state(tmp_path)
    state["day"] = "2026-06-28"
    state["tokens_today"] = 50_000
    autopilot.rollover_day(state, "2026-06-28")
    assert state["tokens_today"] == 50_000


# ─────────────────────────── is_active ───────────────────────────

def test_is_active_false_by_default():
    state = autopilot.load_state("/dev/null")
    assert autopilot.is_active(state) is False


def test_is_active_true_when_enabled_and_not_paused():
    state = {"global_enabled": True, "paused": False}
    assert autopilot.is_active(state) is True


def test_is_active_false_when_paused():
    state = {"global_enabled": True, "paused": True}
    assert autopilot.is_active(state) is False


def test_is_active_false_when_disabled():
    state = {"global_enabled": False, "paused": False}
    assert autopilot.is_active(state) is False


# ─────────────────────────── budget_ok ───────────────────────────

def test_budget_ok_within_cap():
    state = {"tokens_today": 100_000}
    assert autopilot.budget_ok(state, 50_000, 2_000_000) is True


def test_budget_ok_at_cap_limit():
    state = {"tokens_today": 1_950_000}
    assert autopilot.budget_ok(state, 50_000, 2_000_000) is True


def test_budget_ok_exceeds_cap():
    state = {"tokens_today": 1_990_000}
    assert autopilot.budget_ok(state, 50_000, 2_000_000) is False


def test_budget_ok_zero_cap_is_unlimited():
    state = {"tokens_today": 999_999_999}
    assert autopilot.budget_ok(state, 1_000_000, 0) is True


# ─────────────────────────── concurrency_ok ───────────────────────────

def test_concurrency_ok_when_slot_free():
    state = {"active_runs": 0}
    assert autopilot.concurrency_ok(state, 1) is True


def test_concurrency_ok_when_at_limit():
    state = {"active_runs": 1}
    assert autopilot.concurrency_ok(state, 1) is False


def test_concurrency_ok_multi_slot():
    state = {"active_runs": 2}
    assert autopilot.concurrency_ok(state, 3) is True


# ─────────────────────────── pending_ok ───────────────────────────

def test_pending_ok_when_no_pending():
    state = {"pending_by_project": {}}
    assert autopilot.pending_ok(state, "proj-a") is True


def test_pending_ok_false_when_project_pending():
    state = {"pending_by_project": {"proj-a": time.time()}}
    assert autopilot.pending_ok(state, "proj-a") is False


def test_pending_ok_other_project_not_affected():
    state = {"pending_by_project": {"proj-b": time.time()}}
    assert autopilot.pending_ok(state, "proj-a") is True


# ─────────────────────────── cooldown_ok ───────────────────────────

def test_cooldown_ok_no_prior_run():
    state = {"cooldowns": {}}
    assert autopilot.cooldown_ok(state, "p", "card1", time.time(), 86400) is True


def test_cooldown_ok_when_cooldown_elapsed():
    now = time.time()
    state = {"cooldowns": {"p/card1": now - 90000}}  # 90000 > 86400
    assert autopilot.cooldown_ok(state, "p", "card1", now, 86400) is True


def test_cooldown_ok_false_within_cooldown():
    now = time.time()
    state = {"cooldowns": {"p/card1": now - 3600}}  # only 1h ago
    assert autopilot.cooldown_ok(state, "p", "card1", now, 86400) is False


# ─────────────────────────── rate_limit_ok ───────────────────────────

def test_rate_limit_ok_empty_dict():
    assert autopilot.rate_limit_ok({}, 0.2) is True


def test_rate_limit_ok_none():
    assert autopilot.rate_limit_ok(None, 0.2) is True


def test_rate_limit_ok_below_threshold():
    limits = {"primary": {"utilization": 0.5}}
    assert autopilot.rate_limit_ok(limits, 0.2) is True


def test_rate_limit_ok_exactly_at_threshold():
    # 0.2 reserve → threshold = 0.8; utilization 0.8 is NOT above 0.8
    limits = {"primary": {"utilization": 0.8}}
    assert autopilot.rate_limit_ok(limits, 0.2) is True


def test_rate_limit_ok_above_threshold():
    limits = {"primary": {"utilization": 0.85}}
    assert autopilot.rate_limit_ok(limits, 0.2) is False


def test_rate_limit_ok_one_bucket_over_fails():
    limits = {
        "primary": {"utilization": 0.5},
        "secondary": {"utilization": 0.95},
    }
    assert autopilot.rate_limit_ok(limits, 0.2) is False


def test_rate_limit_ok_missing_utilization_key():
    limits = {"primary": {"status": "ok"}}  # no utilization key
    assert autopilot.rate_limit_ok(limits, 0.2) is True


def test_rate_limit_ok_zero_reserve():
    limits = {"primary": {"utilization": 0.99}}
    # reserve=0 → threshold=1.0 → 0.99 is not > 1.0 → OK
    assert autopilot.rate_limit_ok(limits, 0.0) is True


# ─────────────────────────── reserve_run / release_run ───────────────────────────

def test_reserve_run_success():
    state = {"active_runs": 0, "tokens_today": 0, "pending_by_project": {}, "day": ""}
    ok = autopilot.reserve_run(state, "proj-a", 50_000, 2_000_000, 1, time.time())
    assert ok is True
    assert state["active_runs"] == 1
    assert state["tokens_today"] == 50_000
    assert "proj-a" in state["pending_by_project"]


def test_reserve_run_concurrency_blocked():
    state = {"active_runs": 1, "tokens_today": 0, "pending_by_project": {}, "day": ""}
    ok = autopilot.reserve_run(state, "proj-a", 50_000, 2_000_000, 1, time.time())
    assert ok is False
    assert state["active_runs"] == 1  # unchanged


def test_reserve_run_budget_blocked():
    state = {"active_runs": 0, "tokens_today": 1_990_000, "pending_by_project": {}, "day": ""}
    ok = autopilot.reserve_run(state, "proj-a", 50_000, 2_000_000, 1, time.time())
    assert ok is False
    assert state["tokens_today"] == 1_990_000  # unchanged


def test_reserve_run_pending_blocked():
    now = time.time()
    state = {"active_runs": 0, "tokens_today": 0, "pending_by_project": {"proj-a": now}, "day": ""}
    ok = autopilot.reserve_run(state, "proj-a", 50_000, 2_000_000, 1, time.time())
    assert ok is False


def test_release_run():
    now = time.time()
    state = {"active_runs": 1, "tokens_today": 0, "pending_by_project": {"proj-a": now}}
    autopilot.release_run(state, "proj-a")
    assert state["active_runs"] == 0
    assert "proj-a" not in state["pending_by_project"]


def test_release_run_floor_at_zero():
    state = {"active_runs": 0, "tokens_today": 0, "pending_by_project": {}}
    autopilot.release_run(state, "proj-a")  # nothing to release — must not go negative
    assert state["active_runs"] == 0


# ─────────────────────────── commit_trailer ───────────────────────────

def test_commit_trailer_format():
    trailer = autopilot.commit_trailer("abc123", "run-xyz")
    assert "X-Cardloop-Autopilot: card/abc123" in trailer
    assert "X-Cardloop-Run: run-xyz" in trailer
    # Must start with a blank line (separator from the commit body)
    assert trailer.startswith("\n\n")


def test_commit_trailer_contains_card_id():
    trailer = autopilot.commit_trailer("deadbeef", "r1")
    assert "card/deadbeef" in trailer


# ─────────────────────────── trajectory ───────────────────────────

def test_trajectory_roundtrip(tmp_path):
    record = {"ts": time.time(), "project": "proj-a", "verdict": "ok", "fingerprint": "aabbccddee11"}
    autopilot.append_trajectory(tmp_path, record)
    recs = autopilot.read_trajectory(tmp_path)
    assert len(recs) == 1
    assert recs[0]["project"] == "proj-a"
    assert recs[0]["verdict"] == "ok"


def test_trajectory_filter_by_project(tmp_path):
    autopilot.append_trajectory(tmp_path, {"ts": 1, "project": "a", "verdict": "ok"})
    autopilot.append_trajectory(tmp_path, {"ts": 2, "project": "b", "verdict": "fail"})
    recs = autopilot.read_trajectory(tmp_path, project_id="a")
    assert len(recs) == 1
    assert recs[0]["project"] == "a"


def test_trajectory_empty_when_no_file(tmp_path):
    recs = autopilot.read_trajectory(tmp_path)
    assert recs == []


def test_trajectory_limit(tmp_path):
    for i in range(10):
        autopilot.append_trajectory(tmp_path, {"ts": float(i), "project": "p"})
    recs = autopilot.read_trajectory(tmp_path, limit=5)
    assert len(recs) == 5


# ─────────────────────────── fingerprint ───────────────────────────

def test_fingerprint_stability():
    fp1 = autopilot.fingerprint("fix", ["a.py", "b.py"], "AssertionError")
    fp2 = autopilot.fingerprint("fix", ["b.py", "a.py"], "AssertionError")
    assert fp1 == fp2  # file order should not matter (sorted)


def test_fingerprint_different_action():
    fp1 = autopilot.fingerprint("fix", ["a.py"], "Error")
    fp2 = autopilot.fingerprint("refactor", ["a.py"], "Error")
    assert fp1 != fp2


def test_fingerprint_length():
    fp = autopilot.fingerprint("fix", [], "")
    assert len(fp) == 12  # first 12 hex chars of sha256


# ─────────────────────────── detect_self_inflicted ───────────────────────────

def test_detect_self_inflicted_returns_none_on_non_git_dir(tmp_path):
    """A plain temp dir (not a git repo) → should return None without raising."""
    result = autopilot.detect_self_inflicted(str(tmp_path), time.time() - 3600, ["a.py"])
    assert result is None


# ─────────────────────────── detect_loop ───────────────────────────

def _make_record(project: str, ts: float, files: list, verdict: str,
                 fp: str, retry_count: int = 0) -> dict:
    return {
        "project": project,
        "ts": ts,
        "files_changed": files,
        "verdict": verdict,
        "fingerprint": fp,
        "retry_count": retry_count,
    }


NOW = time.time()


def test_detect_loop_no_signal_empty():
    assert autopilot.detect_loop([], "proj-a") is None


def test_detect_loop_no_signal_not_enough_runs():
    traj = [
        _make_record("proj-a", NOW - 100, ["foo.py"], "ok", "aaa"),
        _make_record("proj-a", NOW - 200, ["foo.py"], "fail", "bbb"),
    ]
    assert autopilot.detect_loop(traj, "proj-a") is None


def test_detect_loop_file_thrash():
    """Same file in >=3 runs with at least one fail → file_thrash."""
    traj = [
        _make_record("proj-a", NOW - 3600, ["foo.py"], "ok", "fp1"),
        _make_record("proj-a", NOW - 7200, ["foo.py"], "fail", "fp2"),
        _make_record("proj-a", NOW - 10800, ["foo.py"], "ok", "fp3"),
    ]
    assert autopilot.detect_loop(traj, "proj-a") == "file_thrash"


def test_detect_loop_file_thrash_no_fail_is_not_thrash():
    """Same file >=3 times but all verdicts ok → NOT file_thrash."""
    traj = [
        _make_record("proj-a", NOW - 3600, ["foo.py"], "ok", "fp1"),
        _make_record("proj-a", NOW - 7200, ["foo.py"], "ok", "fp2"),
        _make_record("proj-a", NOW - 10800, ["foo.py"], "ok", "fp3"),
    ]
    assert autopilot.detect_loop(traj, "proj-a") != "file_thrash"


def test_detect_loop_fingerprint_repeat():
    """Same fingerprint >=3 times → fingerprint_repeat.
    Use distinct files per run so file_thrash does not trigger first.
    """
    traj = [
        _make_record("proj-a", NOW - 100, ["a.py"], "ok", "samefp"),
        _make_record("proj-a", NOW - 200, ["b.py"], "ok", "samefp"),
        _make_record("proj-a", NOW - 300, ["c.py"], "ok", "samefp"),
    ]
    assert autopilot.detect_loop(traj, "proj-a") == "fingerprint_repeat"


def test_detect_loop_retry_saturation():
    """>=3 records with retry_count>=2 in 24h → retry_saturation."""
    traj = [
        _make_record("proj-a", NOW - 3600, ["a.py"], "fail", "fp1", retry_count=2),
        _make_record("proj-a", NOW - 7200, ["b.py"], "fail", "fp2", retry_count=3),
        _make_record("proj-a", NOW - 10800, ["c.py"], "fail", "fp3", retry_count=2),
    ]
    assert autopilot.detect_loop(traj, "proj-a") == "retry_saturation"


def test_detect_loop_only_matches_correct_project():
    """Records for a different project do not trigger signals."""
    traj = [
        _make_record("proj-b", NOW - 3600, ["foo.py"], "fail", "samefp"),
        _make_record("proj-b", NOW - 7200, ["foo.py"], "fail", "samefp"),
        _make_record("proj-b", NOW - 10800, ["foo.py"], "fail", "samefp"),
    ]
    assert autopilot.detect_loop(traj, "proj-a") is None


def test_detect_loop_outside_window_not_counted():
    """Records older than window_sec are ignored."""
    old_ts = NOW - 200_000  # well outside the 48h window
    traj = [
        _make_record("proj-a", old_ts, ["foo.py"], "fail", "samefp"),
        _make_record("proj-a", old_ts - 1, ["foo.py"], "fail", "samefp"),
        _make_record("proj-a", old_ts - 2, ["foo.py"], "fail", "samefp"),
    ]
    assert autopilot.detect_loop(traj, "proj-a") is None


# ═══════════════════════════════════════════════════════════════════
# Endpoint tests — webapp.py autopilot routes
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def ap_ctx(tmp_path, project_dir):
    """Minimal ctx with one project and data dir for autopilot tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
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
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def ap_app(ap_ctx):
    """aiohttp app with autopilot + settings routes wired."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ap_ctx

    # Autopilot endpoints (now in features.autopilot.routes)
    app.router.add_put("/api/projects/{id}/autopilot", _ap_routes.api_autopilot_set_project_mode)
    app.router.add_get("/api/autopilot/status", _ap_routes.api_autopilot_status)
    app.router.add_post("/api/autopilot/global", _ap_routes.api_autopilot_global)
    app.router.add_post("/api/autopilot/pause", _ap_routes.api_autopilot_pause)
    app.router.add_post("/api/autopilot/resume", _ap_routes.api_autopilot_resume)
    # Settings (to verify autopilot field appears)
    app.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    app.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    # Login + auth
    app.router.add_post("/api/login", _webapp.api_login)

    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── PUT /api/projects/{id}/autopilot ───────────────────────────

async def test_put_autopilot_sets_mode(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.put(
        "/api/projects/myproject/autopilot",
        json={"mode": "propose"},
        headers=_auth(ap_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["mode"] == "propose"


async def test_put_autopilot_persists_to_topics(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    await client.put(
        "/api/projects/myproject/autopilot",
        json={"mode": "auto"},
        headers=_auth(ap_ctx),
    )
    # Verify it landed in topics dict
    topic = list(ap_ctx["topics"].values())[0]
    assert topic.get("autopilot") == "auto"


async def test_put_autopilot_off_clears_key(aiohttp_client, ap_app, ap_ctx):
    """Setting mode back to 'off' (the default) removes the key from topics (lean storage)."""
    ap_ctx["topics"]["1001:42"]["autopilot"] = "auto"
    client = await aiohttp_client(ap_app)
    await client.put(
        "/api/projects/myproject/autopilot",
        json={"mode": "off"},
        headers=_auth(ap_ctx),
    )
    topic = list(ap_ctx["topics"].values())[0]
    assert "autopilot" not in topic


async def test_put_autopilot_invalid_mode(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.put(
        "/api/projects/myproject/autopilot",
        json={"mode": "turbo"},
        headers=_auth(ap_ctx),
    )
    assert resp.status == 400


async def test_put_autopilot_unknown_project(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.put(
        "/api/projects/nonexistent/autopilot",
        json={"mode": "auto"},
        headers=_auth(ap_ctx),
    )
    assert resp.status == 404


# ─────────────────────────── GET /api/autopilot/status ───────────────────────────

async def test_get_autopilot_status_shape(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.get("/api/autopilot/status", headers=_auth(ap_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "global_enabled" in data
    assert "paused" in data
    assert "daily_cap" in data
    assert "tokens_today" in data
    assert "active_runs" in data
    assert "max_concurrent" in data
    assert "rl_reserve" in data
    assert "per_project" in data
    assert isinstance(data["per_project"], dict)


async def test_get_autopilot_status_default_disabled(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.get("/api/autopilot/status", headers=_auth(ap_ctx))
    data = await resp.json()
    assert data["global_enabled"] is False
    assert data["paused"] is False


async def test_get_autopilot_status_includes_project(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.get("/api/autopilot/status", headers=_auth(ap_ctx))
    data = await resp.json()
    assert "myproject" in data["per_project"]
    assert data["per_project"]["myproject"] == "off"


# ─────────────────────────── POST /api/autopilot/global ───────────────────────────

async def test_autopilot_global_enable(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.post(
        "/api/autopilot/global",
        json={"enabled": True},
        headers=_auth(ap_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["global_enabled"] is True


async def test_autopilot_global_disable(aiohttp_client, ap_app, ap_ctx):
    # Enable first
    await client_put_global(ap_app, ap_ctx, enabled=True)
    client = await aiohttp_client(ap_app)
    resp = await client.post(
        "/api/autopilot/global",
        json={"enabled": False},
        headers=_auth(ap_ctx),
    )
    data = await resp.json()
    assert data["global_enabled"] is False


async def client_put_global(app, ctx, enabled: bool):
    """Helper: set global_enabled without pytest aiohttp_client fixture."""
    from features.autopilot import logic as _ap
    state = _ap.load_state(ctx["DATA"])
    state["global_enabled"] = enabled
    _ap.save_state(ctx["DATA"], state)


async def test_autopilot_global_invalid_body(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.post(
        "/api/autopilot/global",
        json={"enabled": "yes"},  # not a bool
        headers=_auth(ap_ctx),
    )
    assert resp.status == 400


# ─────────────────────────── POST /api/autopilot/pause + /resume ───────────────────────────

async def test_autopilot_pause(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.post("/api/autopilot/pause", headers=_auth(ap_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["paused"] is True


async def test_autopilot_resume(aiohttp_client, ap_app, ap_ctx):
    # Pause first
    import autopilot as _ap
    state = _ap.load_state(ap_ctx["DATA"])
    state["paused"] = True
    _ap.save_state(ap_ctx["DATA"], state)

    client = await aiohttp_client(ap_app)
    resp = await client.post("/api/autopilot/resume", headers=_auth(ap_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["paused"] is False


async def test_autopilot_pause_then_resume_cycle(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    await client.post("/api/autopilot/pause", headers=_auth(ap_ctx))
    resp = await client.post("/api/autopilot/resume", headers=_auth(ap_ctx))
    data = await resp.json()
    assert data["paused"] is False


# ─────────────────────────── GET /api/projects/{id}/settings includes autopilot ───────────────────────────

async def test_project_settings_view_includes_autopilot(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.get(
        "/api/projects/myproject/settings",
        headers=_auth(ap_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert "autopilot" in data
    assert data["autopilot"] == "off"  # default


async def test_project_settings_post_sets_autopilot(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.post(
        "/api/projects/myproject/settings",
        json={"autopilot": "propose"},
        headers=_auth(ap_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["settings"]["autopilot"] == "propose"


async def test_project_settings_post_invalid_autopilot(aiohttp_client, ap_app, ap_ctx):
    client = await aiohttp_client(ap_app)
    resp = await client.post(
        "/api/projects/myproject/settings",
        json={"autopilot": "turbo"},
        headers=_auth(ap_ctx),
    )
    assert resp.status == 400

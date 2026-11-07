"""Subscription limits adapt to whatever windows the account actually has.

Plans differ: Pro and Max carry a 5-hour + a weekly window, Ultra (Max 20x renamed on
2026-10-30) lost the 5-hour window for verified accounts, Team/Enterprise may report spend
caps, an API-key install has no plan windows at all. Nothing downstream may assume a
`five_hour` key exists. Payloads below are trimmed from real `/api/oauth/usage` answers.
"""
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp  # noqa: E402
from webapp import _build_limits_dict, _lead_plan_window, _derive_token  # noqa: E402


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


NOW = time.time()

# Max 20x / Ultra, captured 2026-11-06: no 5-hour window anywhere in the answer.
ULTRA = {
    "five_hour": None,
    "seven_day": {"utilization": 16.0, "resets_at": _iso(NOW + 3 * 86400)},
    "seven_day_opus": None, "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False, "utilization": 0.0},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 16, "severity": "normal",
         "resets_at": _iso(NOW + 3 * 86400), "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 1, "severity": "normal",
         "resets_at": _iso(NOW + 3 * 86400),
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ],
}

PRO = {
    "five_hour": {"utilization": 40.0, "resets_at": _iso(NOW + 2 * 3600)},
    "seven_day": {"utilization": 20.0, "resets_at": _iso(NOW + 4 * 86400)},
    "limits": [
        {"kind": "session", "group": "session", "percent": 40, "resets_at": _iso(NOW + 2 * 3600)},
        {"kind": "weekly_all", "group": "weekly", "percent": 20, "resets_at": _iso(NOW + 4 * 86400)},
    ],
}


# ── what the server reports ───────────────────────────────────────────────────────────────

def test_every_window_says_what_it_gates_and_how_long_it_lasts():
    limits = _build_limits_dict(PRO)
    assert limits["five_hour"]["scope"] == "plan" and limits["five_hour"]["period"] == 5 * 3600
    assert limits["seven_day"]["scope"] == "plan" and limits["seven_day"]["period"] == 7 * 86400
    ultra = _build_limits_dict(ULTRA)
    assert set(ultra) == {"seven_day", "seven_day_fable"}
    assert ultra["seven_day_fable"]["scope"] == "model"


def test_a_window_reported_only_in_the_limits_array_is_not_lost():
    """If the top-level fields go away, limits[] still carries session/weekly_all."""
    raw = {"limits": PRO["limits"]}
    limits = _build_limits_dict(raw)
    assert limits["five_hour"]["utilization"] == pytest.approx(0.40)
    assert limits["seven_day"]["utilization"] == pytest.approx(0.20)
    assert limits["five_hour"]["scope"] == "plan"


def test_an_unknown_kind_of_limit_still_shows_up_as_a_plan_window():
    raw = {"limits": [{"kind": "monthly_all", "group": "monthly", "percent": 70,
                       "resets_at": _iso(NOW + 20 * 86400)}]}
    limits = _build_limits_dict(raw)
    assert limits["monthly_all"]["utilization"] == pytest.approx(0.70)
    assert limits["monthly_all"]["scope"] == "plan"
    assert limits["monthly_all"]["label"]


def test_a_spent_window_is_reported_as_rejected():
    raw = dict(ULTRA, seven_day={"utilization": 100.0, "resets_at": _iso(NOW + 86400)})
    assert _build_limits_dict(raw)["seven_day"]["status"] == "rejected"
    assert _build_limits_dict(PRO)["five_hour"]["status"] == "allowed"


# ── which window matters right now ────────────────────────────────────────────────────────

def test_lead_window_is_the_shortest_plan_window():
    assert _lead_plan_window(_build_limits_dict(PRO))[0] == "five_hour"
    assert _lead_plan_window(_build_limits_dict(ULTRA))[0] == "seven_day"


def test_a_spent_longer_window_leads_over_a_free_short_one():
    raw = dict(PRO, seven_day={"utilization": 100.0, "resets_at": _iso(NOW + 4 * 86400)})
    assert _lead_plan_window(_build_limits_dict(raw))[0] == "seven_day"


def test_per_model_buckets_and_credits_never_lead():
    limits = {"seven_day_fable": {"utilization": 1.0, "status": "rejected", "resets_at": NOW + 99,
                                  "scope": "model"},
              "overage": {"utilization": 0.9, "status": "allowed", "resets_at": None}}
    assert _lead_plan_window(limits) is None


def test_passive_snapshot_without_scope_is_classified_by_key():
    """ctx['rate_limits'] (from SDK events) carries no scope/period."""
    snap = {"seven_day": {"status": "allowed", "resets_at": NOW + 86400, "utilization": 0.3},
            "seven_day_opus": {"status": "allowed", "resets_at": NOW + 86400, "utilization": 0.9}}
    assert _lead_plan_window(snap)[0] == "seven_day"
    snap["five_hour"] = {"status": "allowed", "resets_at": NOW + 3600, "utilization": 0.1}
    assert _lead_plan_window(snap)[0] == "five_hour"


def test_reset_time_names_the_day_when_it_is_not_today():
    ctx = {"rate_limits": {"seven_day": {"status": "rejected", "resets_at": NOW + 3 * 86400,
                                         "utilization": 1.0}}}
    shown = _webapp._get_resets_at_display(ctx)
    assert len(shown.split()) == 2, shown          # "Mon 21:00", not a bare "21:00"
    ctx = {"rate_limits": {"five_hour": {"status": "rejected", "resets_at": NOW + 600,
                                         "utilization": 1.0}}}
    assert ":" in _webapp._get_resets_at_display(ctx)


# ── deferred "run after reset" ────────────────────────────────────────────────────────────

@pytest.fixture
def deferred_ctx(tmp_path):
    ctx = {"topics": {"100:10": {"project": "p", "cwd": str(tmp_path)}}, "sessions": {},
           "running": {}, "DATA": tmp_path, "rate_limits": {}}
    _webapp._deferred_init(ctx)
    return ctx


async def _one_sweep(ctx, usage):
    spawned = []

    def spawn(coro):
        spawned.append(coro)
        coro.close()

    calls = 0

    async def sleep(_n):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise asyncio.CancelledError()

    with patch.object(_webapp, "_get_cached_usage_data", new_callable=AsyncMock, return_value=usage), \
         patch.object(_webapp, "_spawn_bg", side_effect=spawn), \
         patch.object(_webapp, "_notify_operator", new_callable=AsyncMock), \
         patch("asyncio.sleep", side_effect=sleep):
        try:
            await _webapp._deferred_loop(ctx)
        except asyncio.CancelledError:
            pass
    return spawned


def _record(**kw):
    rec = {"id": "def-x", "project": "p", "session_key": "100:10", "prompt": "go",
           "fire_at": None, "fire_on_reset": True, "status": "pending", "fired_at": None,
           "error": None, "attempts": 0, "created": _webapp._utcnow_iso()}
    rec.update(kw)
    return rec


@pytest.mark.asyncio
async def test_ultra_spent_week_waits_for_the_weekly_reset(deferred_ctx):
    _webapp._save_deferred([_record()])
    usage = _build_limits_dict(dict(ULTRA, seven_day={"utilization": 100.0,
                                                      "resets_at": _iso(NOW + 2 * 86400)}))
    assert await _one_sweep(deferred_ctx, usage) == []
    assert _webapp._load_deferred()[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_ultra_with_room_left_runs_without_waiting_for_a_window_it_does_not_have(deferred_ctx):
    _webapp._save_deferred([_record()])
    usage = _build_limits_dict(dict(ULTRA, seven_day={"utilization": 5.0,
                                                      "resets_at": _iso(NOW + 2 * 86400)}))
    assert len(await _one_sweep(deferred_ctx, usage)) == 1
    assert _webapp._load_deferred()[0]["status"] == "fired"


@pytest.mark.asyncio
async def test_pro_spent_week_is_not_fooled_by_a_fresh_5_hour_window(deferred_ctx):
    _webapp._save_deferred([_record()])
    usage = _build_limits_dict(dict(PRO, five_hour={"utilization": 2.0, "resets_at": _iso(NOW + 5 * 3600)},
                                    seven_day={"utilization": 100.0, "resets_at": _iso(NOW + 86400)}))
    assert await _one_sweep(deferred_ctx, usage) == []


# ── API-key installs ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def usage_app(tmp_path):
    ctx = {"topics": {}, "sessions": {}, "running": {}, "password": "testpass",
           "DATA": tmp_path / "data", "HERE": ROOT, "rate_limits": {},
           "save_sessions": lambda: None, "save_topics": lambda: None,
           "run_engine": None, "ptb_app": None}
    ctx["_auth_token"] = _derive_token("testpass")
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/usage", _webapp.api_usage)
    return app


async def test_api_key_install_says_it_pays_per_token(aiohttp_client, usage_app, monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "api_key")
    monkeypatch.setattr(_webapp, "_usage_cache", {"data": None, "ts": 0.0, "account": "main"})
    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", AsyncMock(return_value=None))
    client = await aiohttp_client(usage_app)
    hdr = {"Cookie": f"cops_auth={usage_app['ctx']['_auth_token']}"}
    body = await (await client.get("/api/usage", headers=hdr)).json()
    assert body["billing"] == "api_key"


async def test_subscription_install_reports_subscription_billing(aiohttp_client, usage_app, monkeypatch):
    monkeypatch.delenv("CLAUDE_AUTH_MODE", raising=False)
    # A private cache: the module one would carry this reading into every later test.
    monkeypatch.setattr(_webapp, "_usage_cache", {"data": None, "ts": 0.0, "account": "main"})
    monkeypatch.setattr(_webapp, "_fetch_oauth_usage", AsyncMock(return_value=ULTRA))
    client = await aiohttp_client(usage_app)
    hdr = {"Cookie": f"cops_auth={usage_app['ctx']['_auth_token']}"}
    body = await (await client.get("/api/usage", headers=hdr)).json()
    assert body["billing"] == "subscription"
    assert "five_hour" not in body["limits"] and body["limits"]["seven_day"]["scope"] == "plan"


def test_a_short_window_that_already_reset_still_leads():
    """The cache can hold a 5-hour window whose reset has passed. It rolled over — the account
    is free again — so it must not hand the lead to the weekly window (that would make a
    deferred prompt wait days for a reset that is irrelevant)."""
    limits = {"five_hour": {"status": "allowed", "resets_at": NOW - 100, "utilization": 0.9},
              "seven_day": {"status": "allowed", "resets_at": NOW + 3 * 86400, "utilization": 0.2}}
    assert _lead_plan_window(limits, NOW)[0] == "five_hour"
    # ...while a window that reset is no longer "spent".
    limits["five_hour"]["status"] = "rejected"
    assert _lead_plan_window(limits, NOW)[0] == "five_hour"


@pytest.mark.asyncio
async def test_pro_deferred_fires_once_the_5_hour_window_has_reset(deferred_ctx):
    _webapp._save_deferred([_record()])
    usage = {"five_hour": {"status": "allowed", "resets_at": NOW - 200, "utilization": 0.9},
             "seven_day": {"status": "allowed", "resets_at": NOW + 3 * 86400, "utilization": 0.2}}
    assert len(await _one_sweep(deferred_ctx, usage)) == 1


# ── passive snapshot from the CLI's own rate-limit events ─────────────────────────────────

def test_engine_keeps_the_windows_the_cli_saw():
    """The CLI reads every window off the inference headers (`unifiedWindows`); the engine
    used to forward only the representative type, with no utilization on Ultra."""
    import engine
    from claude_agent_sdk.types import RateLimitInfo
    raw = {"status": "allowed", "resetsAt": 1794258000, "rateLimitType": "seven_day",
           "unifiedWindows": {"seven_day": {"utilization": 0.15, "resetsAt": 1794258000,
                                            "status": "allowed"}}}
    info = RateLimitInfo(status="allowed", resets_at=1794258000, rate_limit_type="seven_day", raw=raw)
    evt = engine._rate_limit_event(info)
    assert evt["type"] == "rate_limit" and evt["rate_limit_type"] == "seven_day"
    assert evt["windows"]["seven_day"]["utilization"] == 0.15


def test_passive_snapshot_records_every_window_with_its_utilization():
    snap: dict = {}
    _webapp._record_rate_limit_event(snap, {
        "rate_limit_type": "seven_day", "status": "allowed", "resets_at": 1794258000,
        "utilization": None,
        "windows": {"seven_day": {"utilization": 0.15, "resetsAt": 1794258000, "status": "allowed"},
                    "five_hour": {"utilization": 1.0, "resetsAt": 1794020000}},
    })
    assert snap["seven_day"]["utilization"] == 0.15
    assert snap["five_hour"]["status"] == "rejected" and snap["five_hour"]["resets_at"] == 1794020000
    # A CLI that sends no windows still leaves the representative row.
    snap2: dict = {}
    _webapp._record_rate_limit_event(snap2, {"rate_limit_type": "five_hour", "status": "rejected",
                                             "resets_at": 1, "utilization": None})
    assert snap2["five_hour"]["status"] == "rejected"

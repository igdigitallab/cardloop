"""
SDK release watch — is a newer claude-agent-sdk out than the one this venv runs?

Covers:
- _version_tuple ordering (incl. the 0.2.99 < 0.2.144 trap a string compare gets wrong)
- _sdk_info shape: behind / current / no data yet / SDK not importable
- _sdk_refresh: TTL throttle, forced refresh, a failed fetch keeps the last known answer
- SDK_UPDATE_CHECK=0 makes zero outbound calls
- GET /api/version carries the sdk block
- _sdk_watch_loop announces a release ONCE, not on every daily tick

PyPI is monkeypatched throughout — no test touches the network.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from aiohttp import web


def _ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    password = "secr3t"
    ctx = {"password": password, "DATA": data, "HERE": ROOT, "rate_limits": {}}
    ctx["_auth_token"] = _webapp._derive_token(password)
    return ctx


def _fake_pypi(version, calls=None):
    """Replacement for _sdk_fetch_latest: returns `version`, counting invocations."""
    async def _f():
        if calls is not None:
            calls.append(1)
        return version
    return _f


# ─────────────────────────────── version compare ───────────────────────────────

def test_version_tuple_orders_numerically():
    vt = _webapp._version_tuple
    # The trap: "0.2.99" > "0.2.144" as strings, but 99 < 144 as releases.
    assert vt("0.2.144") > vt("0.2.99")
    assert vt("0.2.144") > vt("0.2.143")
    assert vt("0.3.0") > vt("0.2.999")
    assert vt("0.2.143") == vt("v0.2.143")


def test_version_tuple_survives_junk():
    vt = _webapp._version_tuple
    assert vt("") == (0,)
    assert vt("not-a-version") == (0, 0, 0)


# ───────────────────────────────── _sdk_info ──────────────────────────────────

def test_sdk_info_flags_a_newer_release(monkeypatch):
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.143")
    info = _webapp._sdk_info({"latest": "0.2.144", "ts": 1000.0})
    assert info["installed"] == "0.2.143"
    assert info["latest"] == "0.2.144"
    assert info["update_available"] is True
    assert info["checked_at"] == 1000.0


def test_sdk_info_quiet_when_current(monkeypatch):
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.144")
    assert _webapp._sdk_info({"latest": "0.2.144"})["update_available"] is False


def test_sdk_info_quiet_before_first_check(monkeypatch):
    """No cached answer yet — report the installed version, claim nothing."""
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.143")
    info = _webapp._sdk_info({})
    assert info["latest"] is None
    assert info["update_available"] is False
    assert info["checked_at"] is None


def test_sdk_info_quiet_when_sdk_not_importable(monkeypatch):
    """A venv without the SDK must not render "None → 0.2.144"."""
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: None)
    assert _webapp._sdk_info({"latest": "0.2.144"})["update_available"] is False


# ──────────────────────────────── _sdk_refresh ────────────────────────────────

async def test_refresh_persists_and_throttles(tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", True)
    calls = []
    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144", calls))
    data = tmp_path / "data"; data.mkdir()

    state = await _webapp._sdk_refresh(data)
    assert state["latest"] == "0.2.144"
    assert json.loads((data / "sdk-version.json").read_text())["latest"] == "0.2.144"

    # Second call inside the TTL must not hit PyPI again.
    await _webapp._sdk_refresh(data)
    assert len(calls) == 1

    # ...but an explicit "check for updates" bypasses the throttle.
    await _webapp._sdk_refresh(data, force=True)
    assert len(calls) == 2


async def test_failed_fetch_keeps_last_known_answer(tmp_path, monkeypatch):
    """Offline must degrade to "last known", never to "no update available"."""
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", True)
    data = tmp_path / "data"; data.mkdir()
    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144"))
    await _webapp._sdk_refresh(data)

    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi(None))
    state = await _webapp._sdk_refresh(data, force=True)
    assert state["latest"] == "0.2.144"
    # ts still stamped, so an offline box doesn't retry on every page load
    assert state["ts"] > 0


async def test_disabled_makes_no_outbound_call(tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", False)
    calls = []
    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144", calls))
    data = tmp_path / "data"; data.mkdir()
    state = await _webapp._sdk_refresh(data, force=True)
    assert calls == []
    assert state == {}


def test_corrupt_state_file_is_not_fatal(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    (data / "sdk-version.json").write_text("{not json")
    assert _webapp._sdk_state_read(data) == {}


# ──────────────────────────────── /api/version ────────────────────────────────

def _app(ctx):
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/version", _webapp.api_version)
    return app


async def test_api_version_carries_sdk_block(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", False)  # no network from a request
    monkeypatch.setattr(_webapp, "_version_fetch_at", _webapp.time.time())
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.143")
    ctx = _ctx(tmp_path)
    (ctx["DATA"] / "sdk-version.json").write_text(json.dumps({"latest": "0.2.144", "ts": 5.0}))

    client = await aiohttp_client(_app(ctx))
    resp = await client.get("/api/version", headers={"Cookie": f"cops_auth={ctx['_auth_token']}"})
    assert resp.status == 200
    sdk = (await resp.json())["sdk"]
    assert sdk["name"] == "claude-agent-sdk"
    assert sdk["installed"] == "0.2.143"
    assert sdk["latest"] == "0.2.144"
    assert sdk["update_available"] is True


# ─────────────────────────────── the watch loop ───────────────────────────────

class _StopLoop(Exception):
    pass


async def _run_one_tick(monkeypatch, ctx):
    """Drive _sdk_watch_loop through exactly one iteration, then break out.

    The loop's shape is: sleep(settle) → tick → sleep(interval). Raising on the
    SECOND sleep leaves precisely one completed tick behind.
    """
    sleeps = []

    async def _fake_sleep(_secs):
        sleeps.append(_secs)
        if len(sleeps) >= 2:
            raise _StopLoop
    monkeypatch.setattr(_webapp.asyncio, "sleep", _fake_sleep)
    with pytest.raises(_StopLoop):
        await _webapp._sdk_watch_loop(ctx)


async def test_watch_announces_once_per_release(tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", True)
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.143")
    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144"))
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda coro: coro.close())

    notes = []
    async def _capture(_ctx, text):
        notes.append(text)
    monkeypatch.setattr(_webapp, "_notify_operator", _capture)

    ctx = _ctx(tmp_path)
    await _run_one_tick(monkeypatch, ctx)

    assert len(notes) == 1
    assert "0.2.144" in notes[0] and "0.2.143" in notes[0]
    # durable copy for an operator who missed the toast
    assert "0.2.144" in (ctx["DATA"] / "inbox" / "sdk-update-available.txt").read_text()
    assert json.loads((ctx["DATA"] / "sdk-version.json").read_text())["notified"] == "0.2.144"

    # A second daily tick on the SAME release must stay silent — a daily nag just
    # trains the operator to ignore it.
    await _run_one_tick(monkeypatch, ctx)
    assert len(notes) == 1


async def test_watch_announces_again_on_the_next_release(tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", True)
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.143")
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda coro: coro.close())
    notes = []
    async def _capture(_ctx, text):
        notes.append(text)
    monkeypatch.setattr(_webapp, "_notify_operator", _capture)
    ctx = _ctx(tmp_path)

    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144"))
    await _run_one_tick(monkeypatch, ctx)

    # Age the cache past its TTL — in production the 24h tick always outruns the 6h
    # freshness window, but two back-to-back ticks in a test would hit the throttle.
    state_file = ctx["DATA"] / "sdk-version.json"
    aged = json.loads(state_file.read_text()); aged["ts"] = 0
    state_file.write_text(json.dumps(aged))

    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.145"))
    await _run_one_tick(monkeypatch, ctx)

    assert len(notes) == 2
    assert "0.2.145" in notes[1]


async def test_watch_retracts_the_alert_once_upgraded(tmp_path, monkeypatch):
    """After the operator upgrades, the durable inbox copy must stop claiming an update."""
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", True)
    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144"))
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda coro: coro.close())
    async def _capture(_ctx, _text):
        pass
    monkeypatch.setattr(_webapp, "_notify_operator", _capture)
    ctx = _ctx(tmp_path)

    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.143")
    await _run_one_tick(monkeypatch, ctx)
    alert = ctx["DATA"] / "inbox" / "sdk-update-available.txt"
    assert alert.exists()

    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.144")
    await _run_one_tick(monkeypatch, ctx)
    assert not alert.exists()


async def test_watch_silent_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_SDK_CHECK_ENABLED", True)
    monkeypatch.setattr(_webapp, "_sdk_installed_version", lambda: "0.2.144")
    monkeypatch.setattr(_webapp, "_sdk_fetch_latest", _fake_pypi("0.2.144"))
    notes = []
    async def _capture(_ctx, text):
        notes.append(text)
    monkeypatch.setattr(_webapp, "_notify_operator", _capture)
    ctx = _ctx(tmp_path)
    await _run_one_tick(monkeypatch, ctx)
    assert notes == []

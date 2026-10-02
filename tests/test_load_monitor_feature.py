"""spec-094: alert policy (pure), the HTTP route, and the registration gate."""
import sys
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import load_monitor as lm
from features.load_monitor import alerts as al
from features.load_monitor.alerts import AlertState, decide
from features.load_monitor.routes import add_routes


def snap(level, crit=(), warn=()):
    sigs = [{"id": i, "level": "crit", "text": f"{i} is bad", "hint": "do X"} for i in crit]
    sigs += [{"id": i, "level": "warn", "text": f"{i} is high", "hint": "do Y"} for i in warn]
    return {"level": level, "signals": sigs}


# ───────────────────────────── alert policy ─────────────────────────────

def test_crit_must_persist_before_a_loud_alert():
    st = AlertState()
    assert decide(snap("crit", crit=["tmp"]), st, 0) is None
    assert decide(snap("crit", crit=["tmp"]), st, al.CRIT_AFTER_S - 1) is None
    a = decide(snap("crit", crit=["tmp"]), st, al.CRIT_AFTER_S)
    assert a and a.loud and "tmp is bad" in a.body and "do X" in a.body


def test_a_blip_that_recovers_never_alerts():
    st = AlertState()
    decide(snap("crit", crit=["mem"]), st, 0)
    decide(snap("ok"), st, 60)                                   # recovered: the clock resets
    assert decide(snap("crit", crit=["mem"]), st, 130) is None   # only 0 s into a NEW episode


def test_cooldown_then_a_new_signal_may_realert_after_the_min_gap():
    st = AlertState()
    decide(snap("crit", crit=["tmp"]), st, 0)
    assert decide(snap("crit", crit=["tmp"]), st, 130)           # first alert
    assert decide(snap("crit", crit=["tmp"]), st, 130 + 600) is None          # same set, cooling down
    assert decide(snap("crit", crit=["tmp", "oom"]), st, 130 + 600)           # new crit signal, gap > 5 min
    assert decide(snap("crit", crit=["tmp", "oom"]), st, 130 + 600 + 60) is None


def test_same_set_realerts_only_after_the_full_cooldown():
    st = AlertState()
    decide(snap("crit", crit=["tmp"]), st, 0)
    assert decide(snap("crit", crit=["tmp"]), st, 130)
    assert decide(snap("crit", crit=["tmp"]), st, 130 + al.COOLDOWN_S - 5) is None
    assert decide(snap("crit", crit=["tmp"]), st, 130 + al.COOLDOWN_S)


def test_warn_is_quiet_and_slow():
    st = AlertState()
    assert decide(snap("warn", warn=["mem"]), st, 0) is None
    assert decide(snap("warn", warn=["mem"]), st, al.WARN_AFTER_S - 1) is None
    a = decide(snap("warn", warn=["mem"]), st, al.WARN_AFTER_S)
    assert a and not a.loud                                       # inbox file only, no push/toast
    assert decide(snap("warn", warn=["mem"]), st, al.WARN_AFTER_S + 3600) is None


def test_unknown_and_missing_snapshot_never_alert():
    st = AlertState()
    assert decide(None, st, 0) is None
    assert decide(snap("unknown"), st, 10_000) is None


# ───────────────────────────── route ─────────────────────────────

def _app():
    app = web.Application()
    app["ctx"] = {}
    add_routes(app)
    return app


async def test_route_warming_up_before_the_first_sample(aiohttp_client, monkeypatch):
    monkeypatch.setattr(lm, "MONITOR", lm.Monitor())
    client = await aiohttp_client(_app())
    d = await (await client.get("/api/system-load")).json()
    assert d["level"] == "unknown" and d["warming_up"] is True and d["signals"] == []


async def test_route_returns_the_snapshot_with_a_server_side_age(aiohttp_client, monkeypatch):
    import time
    m = lm.Monitor()
    m._snap = {"level": "warn", "score": 61, "at": time.time() - 7, "chats": {"live": 3, "max": 8},
               "signals": [{"id": "mem", "level": "warn"}], "top": [], "host": {}}
    monkeypatch.setattr(lm, "MONITOR", m)
    client = await aiohttp_client(_app())
    d = await (await client.get("/api/system-load")).json()
    assert d["level"] == "warn" and d["score"] == 61
    assert 6.5 <= d["age_s"] <= 9                                 # computed on the server clock


# ───────────────────────────── registration gate ─────────────────────────────

def test_disabled_registers_nothing(monkeypatch):
    monkeypatch.setenv("LOAD_MONITOR", "0")
    from features.load_monitor import register
    app = web.Application()
    register(app, {})
    assert not any("system-load" in str(r.resource) for r in app.router.routes())


def test_module_is_in_the_registry_and_on_by_default():
    import modules
    mods = {m["id"]: m for m in modules.list_modules()} if hasattr(modules, "list_modules") else {}
    assert "load_monitor" in modules._BUILTIN_BY_ID
    assert modules._BUILTIN_BY_ID["load_monitor"]["default_enabled"] is True

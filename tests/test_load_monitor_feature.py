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


# ───────────────────────── review round 2 regressions ─────────────────────────

def test_memory_class_crit_alerts_fast():
    st = AlertState()
    decide(snap("crit", crit=["mem"]), st, 0)
    assert decide(snap("crit", crit=["mem"]), st, al.FAST_CRIT_AFTER_S - 1) is None
    assert decide(snap("crit", crit=["mem"]), st, al.FAST_CRIT_AFTER_S)         # an OOM can beat 120 s
    st2 = AlertState()
    decide(snap("crit", crit=["tmp"]), st2, 0)
    assert decide(snap("crit", crit=["tmp"]), st2, al.FAST_CRIT_AFTER_S) is None  # other signals keep 120 s


def test_a_short_dip_does_not_restart_the_crit_clock():
    st = AlertState()
    decide(snap("crit", crit=["tmp"]), st, 0)
    decide(snap("warn", warn=["tmp"]), st, 10)                    # one-sample dip
    decide(snap("crit", crit=["tmp"]), st, 15)
    assert decide(snap("crit", crit=["tmp"]), st, al.CRIT_AFTER_S)  # still counted from t=0


def test_a_real_recovery_does_restart_it():
    st = AlertState()
    decide(snap("crit", crit=["tmp"]), st, 0)
    decide(snap("ok"), st, 10)
    decide(snap("ok"), st, 10 + al.CRIT_GRACE_S + 5)              # gone longer than the grace
    decide(snap("crit", crit=["tmp"]), st, 100)
    assert decide(snap("crit", crit=["tmp"]), st, 100 + al.CRIT_AFTER_S - 1) is None


def test_recovering_from_crit_to_warn_does_not_instantly_send_the_quiet_notice():
    st = AlertState()
    decide(snap("crit", crit=["tmp"]), st, 0)
    assert decide(snap("crit", crit=["tmp"]), st, 200)            # the loud alert
    assert decide(snap("warn", warn=["tmp"]), st, 205) is None    # NOT an immediate "elevated" notice
    assert decide(snap("warn", warn=["tmp"]), st, 205 + al.WARN_AFTER_S - 5) is None
    assert decide(snap("warn", warn=["tmp"]), st, 205 + al.WARN_AFTER_S)


def test_the_alert_names_the_heaviest_consumers():
    s = snap("crit", crit=["mem"])
    s["top"] = [{"kind": "chat", "project": "alpha", "rss_mb": 1500}, {"kind": "chat", "project": "beta", "rss_mb": 900}]
    st = AlertState()
    decide(s, st, 0)
    a = decide(s, st, al.FAST_CRIT_AFTER_S)
    assert "alpha 1500 MB" in a.body and "beta 900 MB" in a.body


def test_cooldown_survives_a_restart(tmp_path):
    from features.load_monitor import loop as lp
    ctx = {"DATA": tmp_path}
    st = AlertState(last_loud=1234.5, last_quiet=99.0, last_loud_key=frozenset({"mem", "oom"}))
    lp._save_state(ctx, st)
    back = lp._load_state(ctx)
    assert back.last_loud == 1234.5 and back.last_quiet == 99.0 and back.last_loud_key == frozenset({"mem", "oom"})
    assert lp._load_state({"DATA": tmp_path / "missing"}).last_loud < 0       # no file -> fresh state


def test_an_empty_alert_body_cannot_break_delivery():
    from features.load_monitor.loop import _first_line
    assert _first_line("") == "" and _first_line("- disk is full\nHint: x") == "disk is full"


async def test_route_reports_a_failing_sampler_instead_of_measuring_forever(aiohttp_client, monkeypatch):
    m = lm.Monitor()
    m.note_error("RuntimeError: boom")
    monkeypatch.setattr(lm, "MONITOR", m)
    d = await (await (await aiohttp_client(_app())).get("/api/system-load")).json()
    assert d["warming_up"] is False and d["error"].startswith("RuntimeError")

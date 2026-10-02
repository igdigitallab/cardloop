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
    assert "alpha 1500 MiB" in a.body and "beta 900 MiB" in a.body


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


# ───────────────────────────── journal lines ─────────────────────────────

from features.load_monitor import journal as jr


def jsnap(level, **sig):
    """sig: id -> (level, value). Text is derived so the assertions can find it."""
    return {"level": level, "score": 40, "chats": {"live": 3, "max": 8},
            "top": [{"kind": "chat", "project": "alpha", "rss_mb": 580}],
            "signals": [{"id": i, "level": lv, "value": v, "text": f"{i} text", "hint": ""} for i, (lv, v) in sig.items()]}


def test_first_sample_says_why_the_meter_starts_amber_and_stays_quiet_when_green():
    lines, base = jr.level_changes(None, jsnap("ok", mem=("ok", "20%")))
    assert lines == ["[load-monitor] level start -> ok"]
    lines, base = jr.level_changes(None, jsnap("warn", mem=("ok", "20%"), disk=("warn", "89% · 3.7d")))
    assert lines[0] == "[load-monitor] level start -> warn (disk)"
    assert lines[1] == "[load-monitor] signal disk: start -> warn | disk text (value 89% · 3.7d)"
    assert len(lines) == 2 and base == {"": "warn", "mem": "ok", "disk": "warn"}


def test_only_changes_are_logged_and_recovery_is_logged_too():
    _, base = jr.level_changes(None, jsnap("ok", mem=("ok", "20%"), disk=("ok", "80%")))
    assert jr.level_changes(base, jsnap("ok", mem=("ok", "21%"), disk=("ok", "80%")))[0] == []   # values moving is not news
    lines, base = jr.level_changes(base, jsnap("crit", mem=("crit", "95%"), disk=("warn", "91%")))
    assert lines[0] == "[load-monitor] level ok -> crit (mem, disk)"
    assert "signal disk: ok -> warn" in lines[1] and "signal mem: ok -> crit" in lines[2]
    lines, base = jr.level_changes(base, jsnap("ok", mem=("ok", "30%"), disk=("ok", "80%")))
    assert lines[0] == "[load-monitor] level crit -> ok" and "signal mem: crit -> ok" in "".join(lines)


def test_a_signal_that_stops_being_measurable_is_not_silently_dropped():
    _, base = jr.level_changes(None, jsnap("warn", disk=("warn", "91%"), mem=("ok", "20%")))
    lines, base = jr.level_changes(base, jsnap("ok", mem=("ok", "20%")))
    assert "signal disk: warn -> not measurable" in "".join(lines)
    lines, base = jr.level_changes(base, jsnap("ok", mem=("ok", "20%"), cpu=("ok", "0%")))
    assert "signal cpu: new -> ok" in "".join(lines)


def test_alert_lines_carry_the_whole_message_on_one_physical_line():
    body = "- Temp dir /tmp is 96% full\n- Memory 91%\nHeaviest: alpha 580 MiB\nHint: clear it"
    line = jr.alert_line(True, "Cardloop: server overloaded", body)
    assert "\n" not in line and line.startswith("[load-monitor] ALERT (loud): Cardloop: server overloaded | ")
    assert "Temp dir /tmp is 96% full | Memory 91% | Heaviest: alpha 580 MiB | Hint: clear it" in line
    assert jr.alert_line(False, "x", "- y").startswith("[load-monitor] notice (quiet): x | y")
    assert jr.alert_line(True, "t", "") .endswith("t | ")


def test_status_line_lists_every_signal_in_a_stable_order():
    line = jr.status_line(jsnap("ok", mem=("ok", "22%"), disk=("ok", "89% · 22d")))
    assert line == ("[load-monitor] status ok (score 40), chats 3/8: disk 89% · 22d, mem 22%; heaviest: alpha 580 MiB")


def test_stall_lines_are_thresholded_rate_limited_and_a_real_freeze_is_never_swallowed():
    sl = jr.StallLog()
    assert sl.note(0.2, 0) is None                                   # jitter, not a stall
    assert sl.note(0.88, 1) == "[load-monitor] event loop stalled 0.88s"
    assert sl.note(0.7, 3) is None and sl.note(1.5, 5) is None       # inside the gap: counted, not printed
    assert sl.note(5.0, 6) == "[load-monitor] event loop stalled 5.00s (+2 more since the previous stall line, worst 1.50s)"
    assert sl.note(0.6, 7) is None                                   # the gap restarts after a printed line
    assert sl.note(0.6, 40) == "[load-monitor] event loop stalled 0.60s (+1 more since the previous stall line, worst 0.60s)"
    assert sl.note(0.6, 80) == "[load-monitor] event loop stalled 0.60s"   # the summary is not repeated


def test_a_flapping_signal_is_capped_and_resumes_after_it_settles():
    g = jr.FlapGuard()
    mk = lambda i: [jr.Line(f"[load-monitor] signal cpu: x{i}", "cpu")]
    out = []
    for i in range(20):                                              # 20 changes in 20 s
        out += g.filter(mk(i), float(i))
    assert len(out) == jr.FLAP_MAX_LINES + 1 and "signal cpu is changing level often" in out[-1]
    assert g.filter([jr.Line("[load-monitor] signal mem: y", "mem")], 21.0) == ["[load-monitor] signal mem: y"]   # per key
    assert g.filter(mk(99), 100.0) == []                             # still inside the window: still quiet
    assert g.filter(mk(100), 100.0 + jr.FLAP_WINDOW_S + 21) == [str(mk(100)[0])]   # calm for a whole window: back


def test_level_changes_lines_carry_their_key_for_the_flap_guard():
    lines, _ = jr.level_changes(None, jsnap("warn", disk=("warn", "91%")))
    assert [l.key for l in lines] == ["", "disk"]


def test_journal_writes_never_raise(monkeypatch):
    def boom(*a, **k):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", boom)
    jr.say("anything")                                               # EPIPE must not kill the heartbeat/sampler


async def test_delivery_outcome_is_journaled_for_every_leg(tmp_path, monkeypatch, capsys):
    import webapp as wa
    from features.load_monitor import loop as lp
    sent = []

    async def notify(ctx, text):
        sent.append(text)

    async def broadcast(payload):
        sent.append("push")

    monkeypatch.setattr(wa, "_notify_operator", notify)
    monkeypatch.setattr(wa, "_push_broadcast", broadcast)
    monkeypatch.setattr(wa, "_push_ensure_vapid_keys", lambda: None)
    monkeypatch.setattr(wa, "_PUSH_AVAILABLE", True)
    monkeypatch.setattr(wa, "_PUSH_LOCK", object())
    monkeypatch.setattr(wa, "_bus_global", {object(), object(), object()})
    monkeypatch.setattr(wa, "_PUSH_PRIV_KEY", "k", raising=False)
    monkeypatch.setattr(wa, "_PUSH_PUB_KEY", "k", raising=False)
    monkeypatch.setattr(wa, "_load_push_subs", lambda: [{"endpoint": "a"}, {"endpoint": "b"}])
    ctx = {"DATA": tmp_path}
    await lp._deliver(ctx, al.Alert(True, "Cardloop: server overloaded", "- disk is full"))
    out = capsys.readouterr().out
    assert "[load-monitor] delivered (loud): inbox load-alert-" in out
    assert "toast queued for 3 open tab(s), push attempted for 2 subscription(s), no delivery receipts" in out
    assert sent[-1] == "push"
    assert "delivered (loud)" in out and "sent to" not in out          # a hand-over, never a claim of delivery
    # nobody subscribed, and an unwritable inbox: both are said out loud, neither stops the rest
    monkeypatch.setattr(wa, "_load_push_subs", lambda: [])
    import shutil
    shutil.rmtree(tmp_path / "inbox")
    (tmp_path / "inbox").write_text("a file where the directory should be")
    await lp._deliver(ctx, al.Alert(True, "t", "- b"))
    out = capsys.readouterr().out
    assert "inbox FAILED" in out and "push no subscribers" in out and "toast queued for 3 open tab(s)" in out
    monkeypatch.setattr(wa, "_bus_global", set())
    monkeypatch.setattr(wa, "_PUSH_LOCK", None)
    await lp._deliver(ctx, al.Alert(True, "t", "- b"))
    out = capsys.readouterr().out
    assert "no open cockpit tab to show it" in out and "push not initialised yet" in out
    # a bus that raises is reported as FAILED, not swallowed
    async def boom(ctx, text):
        raise RuntimeError("bus down")

    monkeypatch.setattr(wa, "_notify_operator", boom)
    await lp._deliver(ctx, al.Alert(True, "t", "- b"))
    assert "toast FAILED RuntimeError('bus down')" in capsys.readouterr().out
    # quiet notices only touch the inbox
    (tmp_path / "inbox").unlink()
    await lp._deliver(ctx, al.Alert(False, "t", "- b"))
    out = capsys.readouterr().out
    assert "delivered (quiet): inbox load-alert-" in out and "toast" not in out and "push" not in out


async def test_sampler_loop_journals_transitions_status_and_alerts(tmp_path, monkeypatch, capsys):
    import asyncio
    from features.load_monitor import loop as lp

    snaps = [jsnap("ok", disk=("ok", "85%")),
             jsnap("ok", disk=("ok", "85%")),
             jsnap("crit", disk=("crit", "97%")),
             jsnap("crit", disk=("crit", "97%")),
             jsnap("crit", disk=("crit", "97%")),
             jsnap("crit", disk=("crit", "97%"))]
    it = iter(snaps)
    monkeypatch.setattr(lp._lm.MONITOR, "sample", lambda inputs: next(it))
    monkeypatch.setattr(lp._lm.MONITOR, "attach_disk_history", lambda path: None)
    monkeypatch.setattr(jr, "STATUS_EVERY_S", 0.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(lp.time, "time", lambda: clock["t"])
    monkeypatch.setattr(lp, "_inputs", lambda ctx: {})
    spawned = []
    monkeypatch.setattr(lp, "_spawn_bg", lambda coro: (spawned.append(coro), coro.close()))
    calls = {"n": 0}

    async def fake_sleep(sec):
        calls["n"] += 1
        clock["t"] += 200.0                                           # > CRIT_AFTER_S per tick
        if calls["n"] > len(snaps):
            raise asyncio.CancelledError

    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await lp._sampler_loop({"DATA": tmp_path})
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[load-monitor] level start -> ok"
    assert any(l.startswith("[load-monitor] level ok -> crit (disk)") for l in out)
    assert any("signal disk: ok -> crit | disk text (value 97%)" in l for l in out)
    assert sum(l.startswith("[load-monitor] status ") for l in out) == 6
    assert any(l.startswith("[load-monitor] ALERT (loud): Cardloop: server overloaded | disk text") for l in out)
    assert len(spawned) == 1                                          # one loud alert; ticks 5 and 6 are inside the cooldown
    assert sum(l.startswith("[load-monitor] ALERT") for l in out) == 1


async def test_heartbeat_feeds_the_monitor_and_journals_stalls(monkeypatch, capsys):
    import asyncio
    from features.load_monitor import loop as lp
    beats = []
    monkeypatch.setattr(lp._lm.MONITOR, "note_loop_lag", lambda lag: beats.append(lag))
    monkeypatch.setattr(jr, "STALL_MIN_S", -1.0)                       # any beat counts as a stall
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lp._heartbeat_loop(), 1.4)
    assert len(beats) == 1
    assert "[load-monitor] event loop stalled" in capsys.readouterr().out


async def test_a_journal_formatting_bug_never_stops_the_measuring(tmp_path, monkeypatch, capsys):
    import asyncio
    from features.load_monitor import loop as lp
    monkeypatch.setattr(lp._lm.MONITOR, "sample", lambda inputs: jsnap("ok", disk=("ok", "85%")))
    monkeypatch.setattr(lp._lm.MONITOR, "attach_disk_history", lambda path: (_ for _ in ()).throw(OSError("ro fs")))
    monkeypatch.setattr(jr, "level_changes", lambda prev, snap: (_ for _ in ()).throw(KeyError("level")))
    errors = []
    monkeypatch.setattr(lp._lm.MONITOR, "note_error", lambda e: errors.append(e))
    monkeypatch.setattr(lp, "_inputs", lambda ctx: {})
    calls = {"n": 0}

    async def fake_sleep(sec):
        calls["n"] += 1
        if calls["n"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await lp._sampler_loop({"DATA": tmp_path})
    out = capsys.readouterr().out
    assert "disk history unavailable, runway disabled" in out and "journal formatting failed" in out
    assert errors and all(e is None for e in errors)                  # the sampler kept reporting itself healthy


async def test_the_heartbeat_survives_a_broken_stdout(monkeypatch):
    import asyncio
    from features.load_monitor import loop as lp
    beats = []
    monkeypatch.setattr(lp._lm.MONITOR, "note_loop_lag", lambda lag: beats.append(lag))
    monkeypatch.setattr(jr, "STALL_MIN_S", -1.0)

    def boom(*a, **k):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", boom)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lp._heartbeat_loop(), 2.5)             # would have died on the first beat
    assert len(beats) >= 2


async def test_the_sampler_caps_the_journal_for_a_signal_that_flaps(tmp_path, monkeypatch, capsys):
    import asyncio
    from features.load_monitor import loop as lp
    snaps = [jsnap("warn", cpu=("warn", "50%")) if i % 2 else jsnap("ok", cpu=("ok", "1%")) for i in range(40)]
    it = iter(snaps)
    monkeypatch.setattr(lp._lm.MONITOR, "sample", lambda inputs: next(it))
    monkeypatch.setattr(lp._lm.MONITOR, "attach_disk_history", lambda path: None)
    monkeypatch.setattr(lp, "_inputs", lambda ctx: {})
    calls = {"n": 0}

    async def fake_sleep(sec):
        calls["n"] += 1
        if calls["n"] > len(snaps):
            raise asyncio.CancelledError

    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await lp._sampler_loop({"DATA": tmp_path})
    out = capsys.readouterr().out.splitlines()
    assert sum(l.startswith("[load-monitor] signal cpu:") for l in out) == jr.FLAP_MAX_LINES
    assert sum("signal cpu is changing level often" in l for l in out) == 1
    assert sum(l.startswith("[load-monitor] level ") for l in out) == jr.FLAP_MAX_LINES

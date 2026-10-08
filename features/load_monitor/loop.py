"""features/load_monitor/loop.py — heartbeat, sampler and alert delivery."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import engine as _engine
import load_monitor as _lm
from webapp import _spawn_bg
from features.load_monitor import journal as _jr
from features.load_monitor.alerts import Alert, AlertState, decide

SAMPLE_EVERY_S = 5.0


async def _heartbeat_loop() -> None:
    """Event-loop lag probe: how late does a 1 s sleep wake up? It runs on the loop it measures,
    so a frozen loop shows up as one huge sample the moment it thaws (the monitor keeps the max
    of the last minute, not an average that would hide it)."""
    loop = asyncio.get_running_loop()
    stalls = _jr.StallLog()
    while True:
        t0 = loop.time()
        await asyncio.sleep(1.0)
        lag = loop.time() - t0 - 1.0
        _lm.MONITOR.note_loop_lag(lag)
        line = stalls.note(lag, loop.time())
        if line:
            _jr.say(line)


def _inputs(ctx: dict) -> dict:
    from webapp import _live_agent_monitor_count
    try:
        bg = int(_live_agent_monitor_count())
    except Exception:
        bg = 0
    return {
        "live_max": _engine.LIVE_CLIENT_MAX,
        "guard": _engine.LIVE_CLIENT_MEM_GUARD if _engine.LIVE_CLIENT_MEM_GUARD > 0 else _lm.GUARD_DEFAULT,
        "running": len([k for k in (ctx.get("running") or {}) if k not in (ctx.get("live_clients") or {})]),
        "bg_agents": bg,
        "chats_live": len(ctx.get("live_clients") or {}),
        "data_dir": ctx.get("DATA"),
        "cockpit_pid": os.getpid(),
    }


def _first_line(body: str) -> str:
    return ((body.splitlines() or [""])[0]).lstrip("- ")


async def _deliver(ctx: dict, alert: Alert) -> None:
    """Toast (open cockpit tabs) + Web Push (where subscribed) + a durable inbox file. Each leg is
    best-effort and independent: a missing push stack must not swallow the toast. The outcome of
    every leg goes to the journal in one line: "the alert fired" and "someone was told" are not the
    same fact, and only the second one matters at 3 am."""
    import webapp as _wa
    text = f"{alert.title}\n{alert.body}"
    out: "dict[str, str]" = {}
    reached = {"inbox": False, "toast": False, "push": False}     # did this leg hand the alert to anything?
    push_subs = 0
    try:
        inbox = Path(ctx["DATA"]) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / f"load-alert-{int(time.time())}.txt"
        f.write_text(text + "\n", encoding="utf-8")
        out["inbox"] = f.name
        reached["inbox"] = True
    except Exception as exc:
        out["inbox"] = f"FAILED {exc!r}"
    if alert.loud:
        first = _first_line(alert.body)
        # Neither leg gives a receipt: `_notify_operator` swallows its own errors and the push helpers
        # swallow per-device failures. So the journal says what was HANDED OVER and to how many
        # recipients, never "delivered"/"sent" as if someone had read it.
        try:
            tabs = len(_wa._bus_global)             # every open cockpit tab listens on the activity stream
            await _wa._notify_operator(ctx, "[ERROR] " + alert.title + (" — " + first if first else ""))
            out["toast"] = f"queued for {tabs} open tab(s)" if tabs else "no open cockpit tab to show it"
            reached["toast"] = bool(tabs)
        except Exception as exc:
            out["toast"] = f"FAILED {exc!r}"
        try:
            if not _wa._PUSH_AVAILABLE:
                out["push"] = "unavailable (pywebpush not installed)"
            else:
                _wa._push_ensure_vapid_keys()
                if not (_wa._PUSH_PRIV_KEY and _wa._PUSH_PUB_KEY):
                    out["push"] = "no VAPID keys"
                elif _wa._PUSH_LOCK is None:
                    out["push"] = "push not initialised yet"
                else:
                    subs = len(_wa._load_push_subs())
                    if subs:
                        await _wa._push_broadcast(json.dumps({
                            "title": alert.title, "body": first or alert.title,
                            "icon": "/icons/icon-192.png", "tag": "load-alert", "data": {"url": "/"},
                        }))
                    out["push"] = (f"attempted for {subs} subscription(s), no delivery receipts"
                                   if subs else "no subscribers")
                    reached["push"] = bool(subs)
                    push_subs = subs
        except Exception as exc:
            out["push"] = f"FAILED {exc!r}"
    # The headline is the OBSERVED outcome. A loud alert exists to tell a person, so only a toast that
    # was queued for an open tab or a push handed to a subscriber counts as an attempt to reach one (a
    # file in the inbox is the record, not the notification); the quiet notice's only channel IS the
    # inbox file. "Delivered" is claimed only for the toast, which is queued onto a connection that is
    # open right now; a push has no receipt (the service swallows per-device failures, and a dead
    # subscription looks exactly like a live one), so it is reported as sent, not as delivered.
    if not alert.loud:
        headline = "delivered" if reached["inbox"] else "NOT delivered to anyone"
    elif reached["toast"]:
        headline = "delivered"
    elif reached["push"]:
        headline = f"push sent to {push_subs} subscriber(s), delivery not confirmed"
    else:
        headline = "NOT delivered to anyone"
    _jr.say(f"{_jr.PREFIX} {headline} ({'loud' if alert.loud else 'quiet'}): "
            + ", ".join(f"{k} {v}" for k, v in out.items()))


# Cooldowns survive a restart: a deploy (or a crash loop) under a persistent red level must not
# re-send the same push every time the process comes back.
def _state_path(ctx: dict) -> Path:
    return Path(ctx["DATA"]) / "load_alert_state.json"


def _load_state(ctx: dict) -> AlertState:
    st = AlertState()
    try:
        d = json.loads(_state_path(ctx).read_text(encoding="utf-8"))
        st.last_loud = float(d.get("last_loud", st.last_loud))
        st.last_quiet = float(d.get("last_quiet", st.last_quiet))
        st.last_loud_key = frozenset(d.get("last_loud_key", []))
    except Exception:
        pass
    return st


def _save_state(ctx: dict, st: AlertState) -> None:
    try:
        _state_path(ctx).write_text(json.dumps({
            "last_loud": st.last_loud, "last_quiet": st.last_quiet,
            "last_loud_key": sorted(st.last_loud_key),
        }), encoding="utf-8")
    except Exception as exc:
        _jr.say(f"{_jr.PREFIX} could not persist alert cooldowns: {exc!r}")


async def _sampler_loop(ctx: dict) -> None:
    await asyncio.sleep(3)  # let the service settle
    st = _load_state(ctx)
    try:
        await asyncio.to_thread(_lm.MONITOR.attach_disk_history, Path(ctx["DATA"]) / "load_disk_history.json")
    except Exception as exc:                  # the runway is optional; the meter is not
        _jr.say(f"{_jr.PREFIX} disk history unavailable, runway disabled: {exc!r}")
    prev: "dict[str, str] | None" = None
    flaps = _jr.FlapGuard()
    last_status = time.monotonic()
    while True:
        try:
            # /proc reads and the process walk are blocking: keep them off the loop we measure.
            snap = await asyncio.to_thread(_lm.MONITOR.sample, _inputs(ctx))
            _lm.MONITOR.note_error(None)
            try:                              # writing the journal must never flip the meter to "failing"
                lines, prev = _jr.level_changes(prev, snap)
                for line in flaps.filter(lines, time.monotonic()):
                    _jr.say(line)
                if time.monotonic() - last_status >= _jr.STATUS_EVERY_S:
                    last_status = time.monotonic()
                    _jr.say(_jr.status_line(snap))
            except Exception as exc:
                _jr.say(f"{_jr.PREFIX} journal formatting failed: {exc!r}")
            alert = decide(snap, st, time.time())
            if alert is not None:
                try:
                    _jr.say(_jr.alert_line(alert.loud, alert.title, alert.body))
                except Exception:
                    _jr.say(f"{_jr.PREFIX} ALERT: {alert.title}")
                _save_state(ctx, st)
                # Fire-and-forget: Web Push makes blocking-ish network calls per subscriber and must
                # not stall the sampler (a stalled sampler flips the meter to "stale" mid-incident).
                _spawn_bg(_deliver(ctx, alert))
        except Exception as exc:
            _jr.say(f"{_jr.PREFIX} tick failed: {exc!r}")
            _lm.MONITOR.note_error(f"{type(exc).__name__}: {exc}"[:160])
        await asyncio.sleep(SAMPLE_EVERY_S)

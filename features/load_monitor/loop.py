"""features/load_monitor/loop.py — heartbeat, sampler and alert delivery."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import engine as _engine
import load_monitor as _lm
from features.load_monitor.alerts import Alert, AlertState, decide

SAMPLE_EVERY_S = 5.0


async def _heartbeat_loop() -> None:
    """Event-loop lag probe: how late does a 1 s sleep wake up? It runs on the loop it measures,
    so a frozen loop shows up as one huge sample the moment it thaws (the monitor keeps the max
    of the last minute, not an average that would hide it)."""
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(1.0)
        _lm.MONITOR.note_loop_lag(loop.time() - t0 - 1.0)


def _inputs(ctx: dict) -> dict:
    from webapp import _live_agent_monitor_count
    try:
        bg = int(_live_agent_monitor_count())
    except Exception:
        bg = 0
    return {
        "live_max": _engine.LIVE_CLIENT_MAX,
        "guard": _engine.LIVE_CLIENT_MEM_GUARD if _engine.LIVE_CLIENT_MEM_GUARD > 0 else _lm.GUARD_DEFAULT,
        "running": len(ctx.get("running") or {}),
        "bg_agents": bg,
        "chats_live": len(ctx.get("live_clients") or {}),
        "data_dir": ctx.get("DATA"),
        "cockpit_pid": os.getpid(),
    }


async def _deliver(ctx: dict, alert: Alert) -> None:
    """Toast (open cockpit tabs) + Web Push (where subscribed) + a durable inbox file. Each leg is
    best-effort and independent: a missing push stack must not swallow the toast."""
    import webapp as _wa
    text = f"{alert.title}\n{alert.body}"
    try:
        inbox = Path(ctx["DATA"]) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"load-alert-{int(time.time())}.txt").write_text(text + "\n", encoding="utf-8")
    except Exception:
        pass
    if not alert.loud:
        return
    try:
        await _wa._notify_operator(ctx, "[ERROR] " + alert.title + " — " + alert.body.splitlines()[0].lstrip("- "))
    except Exception as exc:
        print(f"[load-monitor] toast failed: {exc!r}")
    try:
        if _wa._PUSH_AVAILABLE:
            _wa._push_ensure_vapid_keys()
            if _wa._PUSH_PRIV_KEY and _wa._PUSH_PUB_KEY:
                await _wa._push_broadcast(json.dumps({
                    "title": alert.title, "body": alert.body.splitlines()[0].lstrip("- "),
                    "icon": "/icons/icon-192.png", "tag": "load-alert", "data": {"url": "/"},
                }))
    except Exception as exc:
        print(f"[load-monitor] push failed: {exc!r}")


async def _sampler_loop(ctx: dict) -> None:
    await asyncio.sleep(3)  # let the service settle
    st = AlertState()
    while True:
        try:
            # /proc reads and the process walk are blocking: keep them off the loop we measure.
            snap = await asyncio.to_thread(_lm.MONITOR.sample, _inputs(ctx))
            alert = decide(snap, st, time.time())
            if alert is not None:
                print(f"[load-monitor] {'ALERT' if alert.loud else 'notice'}: {alert.title}")
                await _deliver(ctx, alert)
        except Exception as exc:
            print(f"[load-monitor] tick failed: {exc!r}")
        await asyncio.sleep(SAMPLE_EVERY_S)

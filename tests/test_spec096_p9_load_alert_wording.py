"""spec-096 P9 item G: the load-alert journal headline says what was observed.

A web push has no receipt (`_push_broadcast` swallows per-device failures; an expired subscription
looks exactly like a live one), so "push attempted for N subscribers" is not "delivered". Only a
toast queued onto an open cockpit tab - a connection that is alive right now - earns the word.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from features.load_monitor import alerts as al


def _wire(monkeypatch, *, tabs, subs):
    import webapp as wa

    async def notify(ctx, text):
        return None

    async def broadcast(payload):
        return None

    monkeypatch.setattr(wa, "_notify_operator", notify)
    monkeypatch.setattr(wa, "_push_broadcast", broadcast)
    monkeypatch.setattr(wa, "_push_ensure_vapid_keys", lambda: None)
    monkeypatch.setattr(wa, "_PUSH_AVAILABLE", True)
    monkeypatch.setattr(wa, "_PUSH_LOCK", object())
    monkeypatch.setattr(wa, "_bus_global", {object() for _ in range(tabs)})
    monkeypatch.setattr(wa, "_PUSH_PRIV_KEY", "k", raising=False)
    monkeypatch.setattr(wa, "_PUSH_PUB_KEY", "k", raising=False)
    monkeypatch.setattr(wa, "_load_push_subs", lambda: [{"endpoint": str(i)} for i in range(subs)])


async def _headline(tmp_path, capsys):
    from features.load_monitor import loop as lp
    await lp._deliver({"DATA": tmp_path}, al.Alert(True, "Cardloop: server overloaded", "- disk is full"))
    return capsys.readouterr().out


async def test_a_push_with_no_open_tab_is_reported_as_sent_not_as_delivered(tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, tabs=0, subs=2)
    out = await _headline(tmp_path, capsys)
    assert "[load-monitor] push sent to 2 subscriber(s), delivery not confirmed (loud):" in out
    assert "delivered (loud)" not in out
    assert "NOT delivered" not in out, "a push WAS handed over; the headline must not claim nobody was told"
    assert "toast no open cockpit tab to show it" in out and "push attempted for 2 subscription(s)" in out


async def test_a_toast_queued_for_an_open_tab_is_delivered(tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, tabs=1, subs=0)
    out = await _headline(tmp_path, capsys)
    assert "[load-monitor] delivered (loud):" in out and "toast queued for 1 open tab(s)" in out


async def test_an_open_tab_beats_the_push_in_the_headline(tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, tabs=3, subs=2)
    out = await _headline(tmp_path, capsys)
    assert "[load-monitor] delivered (loud):" in out and "delivery not confirmed" not in out


async def test_nobody_to_tell_is_still_not_delivered(tmp_path, monkeypatch, capsys):
    _wire(monkeypatch, tabs=0, subs=0)
    out = await _headline(tmp_path, capsys)
    assert "[load-monitor] NOT delivered to anyone (loud):" in out

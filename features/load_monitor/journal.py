"""features/load_monitor/journal.py — what the monitor writes to the service log (the journal).

Pure formatters, so the wording is unit-tested and the loop only prints. Every line starts with
`[load-monitor]`: `journalctl -u cardloop | grep load-monitor` is the whole story — what turned
amber or red and why, every alert with its full text, how it was delivered, event-loop stalls, and a
slow heartbeat of readings so a post-mortem has the minutes BEFORE the alert too.
"""
from __future__ import annotations

from typing import Any

PREFIX = "[load-monitor]"
STATUS_EVERY_S = 15 * 60.0
STALL_MIN_S = 0.5            # event-loop lag worth a line (the meter itself only warns at 1 s)
STALL_GAP_S = 10.0           # at most one stall line per this many seconds: a storm is one story
STALL_ALWAYS_S = 2.0         # ...except a real freeze, which is always written when it happens
FLAP_MAX_LINES = 8           # transition lines per key per window before it is declared flapping
FLAP_WINDOW_S = 600.0


def say(line: str) -> None:
    """Print one journal line. Never raises: a closed/broken stdout (EPIPE when the cockpit is run by
    hand through a pager, a lost journald stream) must not kill the heartbeat or the sampler."""
    try:
        print(line)
    except Exception:
        pass


class Line(str):
    """A journal line that remembers which signal ("" = the overall level) it is about."""
    key: str = ""

    def __new__(cls, text: str, key: str = "") -> "Line":
        obj = super().__new__(cls, text)
        obj.key = key
        return obj

_RANK = {"ok": 0, "warn": 1, "crit": 2}


def flat(text: str) -> str:
    """One physical line per journal entry: newlines would split an alert across entries."""
    return " | ".join(p.strip().lstrip("- ").strip() for p in str(text).splitlines() if p.strip())


def level_changes(prev: "dict[str, str] | None", snap: "dict[str, Any]") -> "tuple[list[str], dict[str, str]]":
    """Journal lines for everything that changed since the previous sample, and the new baseline.

    `prev` maps signal id -> level plus "" -> the overall level; None means "first sample since the
    process started", where every non-ok signal is reported (the journal must say why the meter
    starts amber, not just that a restart happened)."""
    sigs = {s["id"]: s for s in snap.get("signals", [])}
    cur = {i: s["level"] for i, s in sigs.items()}
    overall = str(snap.get("level", "unknown"))
    cur[""] = overall
    lines: "list[str]" = []
    before = prev if prev is not None else {}
    if before.get("", "start") != overall:
        bad = sorted((i for i, s in sigs.items() if s["level"] != "ok"),
                     key=lambda i: (-_RANK.get(sigs[i]["level"], 0), i))
        lines.append(Line(f"{PREFIX} level {before.get('', 'start')} -> {overall}" + (f" ({', '.join(bad)})" if bad else ""), ""))
    for i in sorted(cur):
        if i == "":
            continue
        old = before.get(i)
        if old == cur[i] or (old is None and cur[i] == "ok" and prev is None):
            continue
        s = sigs[i]
        was = old or ("start" if prev is None else "new")
        lines.append(Line(f"{PREFIX} signal {i}: {was} -> {cur[i]} | {s.get('text', '')} (value {s.get('value', '?')})", i))
    for i in sorted(set(before) - set(cur) - {""}):
        lines.append(Line(f"{PREFIX} signal {i}: {before[i]} -> not measurable", i))
    return lines, cur


def alert_line(loud: bool, title: str, body: str) -> str:
    return f"{PREFIX} {'ALERT (loud)' if loud else 'notice (quiet)'}: {flat(title)} | {flat(body)}"


def status_line(snap: "dict[str, Any]") -> str:
    sigs = sorted(snap.get("signals", []), key=lambda s: s["id"])
    chats = snap.get("chats") or {}
    top = ", ".join(f"{t['project']} {t['rss_mb']} MiB" for t in (snap.get("top") or [])[:3])
    parts = ", ".join(f"{s['id']} {s['value']}" for s in sigs)
    return (f"{PREFIX} status {snap.get('level', 'unknown')} (score {snap.get('score', 0)}), "
            f"chats {chats.get('live', 0)}/{chats.get('max', 0)}: {parts}" + (f"; heaviest: {top}" if top else ""))


class StallLog:
    """Rate-limited event-loop stall lines. Not thread-safe: the heartbeat task is its only user."""

    def __init__(self) -> None:
        self._last = -1e18
        self._worst = 0.0
        self._skipped = 0

    def note(self, lag_s: float, now: float) -> "str | None":
        if lag_s < STALL_MIN_S:
            return None
        if lag_s < STALL_ALWAYS_S and now - self._last < STALL_GAP_S:
            self._skipped += 1
            self._worst = max(self._worst, lag_s)
            return None
        extra = (f" (+{self._skipped} more since the previous stall line, worst {self._worst:.2f}s)"
                 if self._skipped else "")
        self._last, self._skipped, self._worst = now, 0, 0.0
        return f"{PREFIX} event loop stalled {lag_s:.2f}s{extra}"


class FlapGuard:
    """Caps transition lines per key: a signal hovering on a threshold would otherwise write
    thousands of lines a day and bury the alert lines this log exists for. After FLAP_MAX_LINES in
    FLAP_WINDOW_S it writes ONE "flapping" line and stays quiet until the key has been calm for a
    whole window. Only the transition lines go through it — alerts and status lines never do."""

    def __init__(self) -> None:
        self._seen: "dict[str, list[float]]" = {}
        self._muted: "set[str]" = set()

    def filter(self, lines: "list[Line]", now: float) -> "list[str]":
        out: "list[str]" = []
        for ln in lines:
            key = getattr(ln, "key", "")
            ts = [t for t in self._seen.get(key, []) if now - t < FLAP_WINDOW_S]
            if len(ts) >= FLAP_MAX_LINES:
                if key not in self._muted:
                    self._muted.add(key)
                    what = f"signal {key}" if key else "the overall level"
                    out.append(f"{PREFIX} {what} is changing level often ({FLAP_MAX_LINES}+ lines in "
                               f"{FLAP_WINDOW_S / 60:.0f} min); further changes are not logged until it settles")
                ts.append(now)                      # the window stays full for as long as it keeps flapping
            else:
                self._muted.discard(key)
                ts.append(now)
                out.append(str(ln))
            self._seen[key] = ts
        return out

"""features/board_janitor/loop.py — background sweep that gives Review an exit.

Import rule: feature -> core is safe (spec-068 IRON RULE); core never imports this.

What it does every tick:
  1. stamps Review cards that carry no review timestamp (grandfathering),
  2. auto-archives cards that pass the objective gate in logic.decide_card,
  3. writes ONE digest for everything it could not close, and pushes at most once a day.

What it deliberately does NOT do: ask a model whether the work looks finished.
The gate is evidence (run record + tests), never an opinion.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from pathlib import Path

from webapp import (
    _collect_projects,
    _load_board,
    _save_board,
    _get_board_lock,
    _done_path,
    _done_archive_line,
    _pop_card,
    _read_run_meta,
    _validate_diag_cmd,
    _emit_board_event,
)

from features.board_janitor import logic as _logic

# Project-level test verdicts are cached briefly: without this a fleet sweep would
# re-run every suite on every tick, which costs more than the janitor saves.
_TEST_CACHE_TTL_SEC = 900
_test_cache: dict = {}          # cwd -> (expires_at, verdict)
_last_push_day: str = ""


async def _test_signal(project: dict) -> "bool | None":
    """Tri-state test verdict for a project: True=green, False=red, None=no signal.

    Read-only: runs the operator-configured test_cmd and nothing else. An
    unconfigured or non-allowlisted command yields None, never True — "we did not
    check" must not be mistaken for "it passed".
    """
    cwd = project.get("cwd") or ""
    cmd_str = (project.get("test_cmd") or "").strip()
    if not cwd or not cmd_str:
        return None
    cached = _test_cache.get(cwd)
    if cached and cached[0] > time.time():
        return cached[1]
    if not _validate_diag_cmd(cmd_str):
        return None
    verdict: "bool | None" = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(cmd_str), cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        verdict = (proc.returncode or 0) == 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        verdict = None
    except Exception:
        verdict = None
    _test_cache[cwd] = (time.time() + _TEST_CACHE_TTL_SEC, verdict)
    return verdict


def _archive_card(cwd: str, name: str, preamble: str, cols: dict, card: dict) -> None:
    """Move one card into DONE.md, mirroring the manual move-to-done path exactly."""
    dp = _done_path(cwd)
    header = dp.read_text(encoding="utf-8") if dp.exists() else f"# Done — {name}\n"
    if not header.strip():
        header = f"# Done — {name}\n"
    stamp = time.strftime("%Y-%m-%d") + " · auto-accepted"
    dp.write_text(header.rstrip() + "\n" + _done_archive_line(card, stamp), encoding="utf-8")
    _save_board(cwd, name, preamble, cols)


async def _janitor_tick_once(ctx: dict) -> dict:
    """One sweep over every project. Returns a summary dict (also used by tests/API)."""
    if _logic.MODE == "off":
        return {"mode": "off", "accepted": 0, "parked": 0, "entries": []}
    if _logic.autonomy_paused(ctx["DATA"]):
        return {"mode": _logic.MODE, "paused": True, "accepted": 0, "parked": 0, "entries": []}

    now = time.time()
    entries: list = []
    accepted_total = 0

    for project in _collect_projects(ctx):
        cwd = project.get("cwd") or ""
        name = project.get("name") or project.get("id") or "project"
        if not cwd or not (Path(cwd) / "TASKS.md").exists():
            continue
        try:
            # ── pass 1: stamp unstamped cards, collect candidates (no tests yet) ──
            async with _get_board_lock(cwd):
                _, preamble, cols = _load_board(cwd)
                review = cols.get("review") or []
                if not review:
                    continue
                if _logic.stamp_missing_rt(review, now):
                    _save_board(cwd, name, preamble, cols)
                candidates = [
                    c for c in review
                    if (_logic.card_age_hours(c, now) or 0) >= min(_logic.ACCEPT_AFTER_H, _logic.DIGEST_AFTER_H)
                ]
            if not candidates:
                continue

            # Tests are only worth running when something could actually be accepted.
            wants_gate = _logic.MODE == "accept" and any(
                _logic.run_is_settled(_read_run_meta(ctx["DATA"], c["id"]))[0]
                and (_logic.card_age_hours(c, now) or 0) >= _logic.ACCEPT_AFTER_H
                for c in candidates
            )
            tests_green = await _test_signal(project) if wants_gate else None

            # ── pass 2: decide and apply ──
            async with _get_board_lock(cwd):
                _, preamble, cols = _load_board(cwd)
                for card in list(cols.get("review") or []):
                    run_meta = _read_run_meta(ctx["DATA"], card["id"])
                    action, reason = _logic.decide_card(card, run_meta, tests_green, now)
                    if action == "hold":
                        continue
                    entry = {
                        "project": name, "card_id": card["id"], "text": card.get("text", ""),
                        "action": action, "reason": reason,
                        "age_h": _logic.card_age_hours(card, now),
                    }
                    entries.append(entry)
                    if action == "accept":
                        popped = _pop_card(cols, card["id"])
                        if popped is not None:
                            _archive_card(cwd, name, preamble, cols, popped)
                            accepted_total += 1
            for e in entries:
                if e["action"] == "accept" and e["project"] == name:
                    _emit_board_event(
                        project.get("session_key") or "",
                        event="moved", card_id=e["card_id"], title=e["text"],
                        column_from="review", column_to="done", severity="info",
                        summary=f"Auto-accepted: {e['reason']}",
                    )
        except Exception as exc:  # one bad project must not stop the sweep
            print(f"[board-janitor] {name}: {exc}")

    parked = sum(1 for e in entries if e["action"] == "digest")
    if entries:
        await _write_digest(ctx, entries, accepted_total, parked)
    return {"mode": _logic.MODE, "accepted": accepted_total, "parked": parked, "entries": entries}


async def _write_digest(ctx: dict, entries: list, accepted: int, parked: int) -> None:
    """Write the digest to data/inbox/ and push at most once per calendar day."""
    global _last_push_day
    try:
        data: Path = ctx["DATA"]
        inbox = data / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        (inbox / f"board-digest-{day}.md").write_text(_logic.build_digest(entries), encoding="utf-8")
        if parked and _last_push_day != day:
            _last_push_day = day
            try:
                from webapp import _push_broadcast
                await _push_broadcast(json.dumps({
                    "title": "\U0001F5C2 Board digest",
                    "body": f"{accepted} auto-accepted, {parked} waiting on you",
                    "icon": "/icons/icon-192.png",
                    "tag": "board-digest",
                    "data": {"url": "/"},
                }))
            except Exception:
                pass
    except Exception as exc:
        print(f"[board-janitor] digest write failed: {exc}")


async def _board_janitor_loop(ctx: dict) -> None:
    """Background sweep. Never raises out — a janitor that dies silently is worse than none."""
    await asyncio.sleep(90)  # let the service settle before touching any board
    while True:
        try:
            summary = await _janitor_tick_once(ctx)
            if summary.get("accepted") or summary.get("parked"):
                print(f"[board-janitor] accepted={summary['accepted']} parked={summary['parked']}")
        except Exception as exc:
            print(f"[board-janitor] tick failed: {exc}")
        await asyncio.sleep(_logic.INTERVAL_SEC)

"""features/autopilot/loop.py — Shadow-mode background loop for Autopilot.

Contains the test-signal helper, the single tick function, and the
infinite background loop.  SHADOW-MODE INVARIANT: nothing here calls
run_engine, _run_card, _start_card_run, _drain_queue, _queue_enqueue,
or any function that mutates repos or spends model tokens.

Import rule: feature → core is safe (see spec-068 IRON RULE).
"""
from __future__ import annotations

import asyncio
import os
import shlex

from webapp import (
    _collect_projects,
    _load_board,
    _card_run_mode,
    _git_enabled,
    _validate_diag_cmd,
    _run_quality_gate,
)

from features.autopilot import logic as _autopilot


# ─────────────────────────────────────────────────────────────────────────────
# Test-signal helper (READ-ONLY)
# ─────────────────────────────────────────────────────────────────────────────

async def _autopilot_test_signal(project: dict) -> "tuple[bool | None, str]":
    """Shadow test signal (READ-ONLY): is this project's test suite failing?

    Prefers the operator-configured ``test_cmd`` (authoritative + allowlist-validated);
    falls back to auto-detection via ``_run_quality_gate`` when none is configured.
    Returns ``(tests_failing, summary)``: True = failing, False = passing,
    None = no/unsafe/unknown signal. Runs the tests only — mutates nothing.
    """
    cwd = project.get("cwd") or ""
    cmd_str = (project.get("test_cmd") or "").strip()
    if cmd_str:
        if not _validate_diag_cmd(cmd_str):
            return None, "configured test_cmd is not allowlisted"
        try:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(cmd_str), cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            rc = proc.returncode or 0
            tail = out.decode(errors="replace").strip()[-300:]
            if rc == 0:
                return False, f"tests passed ({cmd_str})"
            return True, f"tests failed ({cmd_str}): {tail}"
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return None, f"tests timed out ({cmd_str})"
        except Exception as exc:
            return None, f"test run error: {exc}"
    # Fallback: auto-detect via the quality gate (read-only).
    try:
        gate = await asyncio.wait_for(_run_quality_gate(cwd), timeout=120)
        verdict = gate.get("verdict", "unknown")
        info = gate.get("tests") or {}
        if verdict == "safe":
            return False, f"tests passed ({info.get('cmd')})"
        if verdict == "risky":
            return True, f"tests failed ({info.get('cmd')}): {(info.get('output') or '')[:300]}"
        return None, "no test command configured or detected"
    except Exception as exc:
        return None, f"test gate error: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Single tick (READ-ONLY)
# ─────────────────────────────────────────────────────────────────────────────

async def _autopilot_tick_once(ctx: dict) -> list[dict]:
    """Run one shadow-mode autopilot tick.  Returns list of intent dicts decided.

    READ-ONLY.  Never calls run_engine or any card-execution path.
    """
    import time as _time

    data_dir = ctx["DATA"]
    today = __import__("datetime").date.today().isoformat()
    state = _autopilot.load_state(data_dir)
    _autopilot.rollover_day(state, today)

    if not _autopilot.is_active(state):
        return []

    projects = _collect_projects(ctx)
    decisions: list[dict] = []

    for project in projects:
        if project.get("is_free"):
            continue
        if _autopilot.get_project_mode(project) not in ("propose", "auto"):
            continue

        cwd = project.get("cwd") or ""

        # ── gather signals (READ-ONLY) ──────────────────────────────────────

        # 1. Test signal (READ-ONLY): prefer the operator-configured test_cmd
        #    (authoritative), fall back to auto-detection. Runs tests only —
        #    never mutates the repo.
        tests_failing, test_summary = await _autopilot_test_signal(project)

        # 2. Count backlog cards (read-only board read)
        backlog_n = 0
        try:
            _, _, cols = _load_board(cwd)
            backlog_n = len(cols.get("backlog") or [])
        except Exception:
            backlog_n = 0

        signals = {
            "tests_failing": tests_failing,
            "test_summary": test_summary,
            "backlog_cards": backlog_n,
        }

        # ── decide (pure) ───────────────────────────────────────────────────
        intent = _autopilot.decide_intent(project, signals)

        # ── spec-067 v3 invariant #1: HARD-ABORT on a tree we cannot cleanly isolate ──
        #    An execution-class intent (one that would mutate code) is NEVER run in-place on a
        #    dirty / non-git tree. Recorded here in shadow so it's visible in the trajectory;
        #    the future auto-execute path reads `blocked` and routes to the human inbox instead
        #    of editing. This is the safety brick built BEFORE any execution capability exists.
        if intent.get("action") in ("fix_failing_tests", "run_backlog_card"):
            iso_mode = await _card_run_mode(
                cwd, git_enabled=_git_enabled(project), allow_legacy=False
            )
            intent["isolatable"] = iso_mode != "blocked"
            if iso_mode == "blocked":
                intent["blocked"] = "dirty_tree_no_isolation"

        # ── stamp and log (no execution) ────────────────────────────────────
        intent["ts"] = _time.time()
        intent["shadow"] = True
        intent["fingerprint"] = _autopilot.fingerprint(
            intent["action"], [], test_summary
        )

        _autopilot.append_trajectory(data_dir, intent)
        decisions.append(intent)

    return decisions


# ─────────────────────────────────────────────────────────────────────────────
# Background loop
# ─────────────────────────────────────────────────────────────────────────────

async def _autopilot_loop(ctx: dict) -> None:
    """Background shadow loop — decides intent per project, logs it, executes nothing."""
    await asyncio.sleep(20)  # startup grace period
    while True:
        try:
            await _autopilot_tick_once(ctx)
        except Exception as exc:
            print(f"[autopilot] shadow tick error: {exc}")
        await asyncio.sleep(int(os.environ.get("AUTOPILOT_TICK_SEC", "300")))

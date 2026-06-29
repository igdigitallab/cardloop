"""
autopilot.py — Inert foundation for the Cardloop Autopilot orchestrator (Phase 0).

This module contains ONLY pure helpers, state I/O, and guardrail predicates.
It performs NO autonomous actions: no background loop, no card execution, no
SDK calls.  All logic here is either:
  - PURE (no I/O) — guardrail predicates, state mutators, formatters
  - FILE I/O ONLY — load/save state, append/read trajectory

Phase 1 (the actual orchestrator loop) will import this module and call these
primitives.  Until then, nothing here runs automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import date, timezone
from pathlib import Path
from typing import Any


# ─────────────────────────── constants ───────────────────────────

MODES: tuple[str, ...] = ("off", "propose", "auto")
DEFAULT_MODE: str = "off"

# ── Env-configured limits (read once at import; no mutable state) ──────────
# AUTOPILOT_DAILY_TOKEN_CAP: daily token budget for autonomous runs.
# Default 2_000_000 (2M).  Set to 0 to effectively disable the budget guard.
DAILY_TOKEN_CAP: int = int(os.environ.get("AUTOPILOT_DAILY_TOKEN_CAP", "2000000"))

# AUTOPILOT_MAX_CONCURRENT: max simultaneous autonomous card runs.
# Default 1 — sequential to avoid churning the same repo concurrently.
MAX_CONCURRENT: int = int(os.environ.get("AUTOPILOT_MAX_CONCURRENT", "1"))

# AUTOPILOT_RL_RESERVE: fraction of rate-limit headroom reserved for interactive
# operator use.  0.2 means the autopilot backs off when ANY bucket's utilization
# exceeds 80% (1 - 0.2).
RL_RESERVE: float = float(os.environ.get("AUTOPILOT_RL_RESERVE", "0.2"))

# ── Default state schema ──────────────────────────────────────────────────
_STATE_DEFAULTS: dict[str, Any] = {
    "global_enabled": False,
    "paused": False,
    "day": "",          # "YYYY-MM-DD" of the current billing window
    "tokens_today": 0,
    "active_runs": 0,
    "pending_by_project": {},   # project_id -> reservation timestamp (float)
    "cooldowns": {},            # "<project_id>/<card_id>" -> last-run timestamp (float)
}


# ─────────────────────────── per-project flag helpers ───────────────────────────

def valid_mode(m: object) -> bool:
    """Return True if *m* is a recognised autopilot mode string."""
    return isinstance(m, str) and m in MODES


def get_project_mode(project: dict) -> str:
    """Return the autopilot mode for *project* (defaults to 'off')."""
    m = project.get("autopilot")
    return m if isinstance(m, str) and m in MODES else DEFAULT_MODE


# ─────────────────────────── state persistence ───────────────────────────

def _state_path(data_dir: "str | Path") -> Path:
    return Path(data_dir) / "autopilot_state.json"


def load_state(data_dir: "str | Path") -> dict:
    """Load autopilot global state from *data_dir*/autopilot_state.json.

    Returns a dict with all keys from _STATE_DEFAULTS.  Missing keys are
    filled from defaults so callers always get a complete state dict even when
    the file was written by an older version.
    """
    p = _state_path(data_dir)
    state: dict = dict(_STATE_DEFAULTS)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
        except Exception:
            pass  # corrupt file → fall back to defaults
    # Ensure nested dicts are present
    if not isinstance(state.get("pending_by_project"), dict):
        state["pending_by_project"] = {}
    if not isinstance(state.get("cooldowns"), dict):
        state["cooldowns"] = {}
    return state


def save_state(data_dir: "str | Path", state: dict) -> None:
    """Persist *state* to *data_dir*/autopilot_state.json (atomic write)."""
    p = _state_path(data_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ─────────────────────────── state queries ───────────────────────────

def is_active(state: dict) -> bool:
    """Return True when autopilot is globally enabled and not paused."""
    return bool(state.get("global_enabled")) and not bool(state.get("paused"))


def rollover_day(state: dict, today: str) -> None:
    """Reset token counter if *today* differs from the last recorded day.

    Mutates *state* in-place.  The caller is responsible for saving.
    """
    if state.get("day") != today:
        state["day"] = today
        state["tokens_today"] = 0


# ─────────────────────────── guardrail predicates (PURE) ───────────────────────────

def budget_ok(state: dict, est_tokens: int, daily_cap: int) -> bool:
    """True if adding *est_tokens* would not exceed *daily_cap*.

    A daily_cap of 0 is treated as unlimited (always True).
    """
    if daily_cap <= 0:
        return True
    return (state.get("tokens_today", 0) + est_tokens) <= daily_cap


def concurrency_ok(state: dict, max_concurrent: int) -> bool:
    """True if active_runs is below *max_concurrent*."""
    return state.get("active_runs", 0) < max_concurrent


def pending_ok(state: dict, project_id: str) -> bool:
    """True if no pending action is recorded for *project_id*."""
    return project_id not in (state.get("pending_by_project") or {})


def cooldown_ok(state: dict, project_id: str, card_id: str, now_ts: float,
                cooldown_sec: int = 86400) -> bool:
    """True if enough time has passed since the last run of *card_id* in *project_id*.

    Uses a composite key "<project_id>/<card_id>" in state["cooldowns"].
    """
    key = f"{project_id}/{card_id}"
    last = (state.get("cooldowns") or {}).get(key)
    if last is None:
        return True
    return (now_ts - float(last)) >= cooldown_sec


def rate_limit_ok(rate_limits: dict, reserve_frac: float) -> bool:
    """True if no rate-limit bucket's utilization exceeds (1 - reserve_frac).

    *rate_limits* has the shape ctx["rate_limits"]:
        {"<type>": {"utilization": float (0-1), ...}, ...}

    Tolerates missing/empty dict (returns True — no limits, no constraint).
    If *reserve_frac* <= 0, the guard is effectively disabled.
    """
    if not rate_limits or not isinstance(rate_limits, dict):
        return True
    threshold = 1.0 - max(0.0, float(reserve_frac))
    for entry in rate_limits.values():
        if not isinstance(entry, dict):
            continue
        util = entry.get("utilization")
        if util is None:
            continue
        try:
            if float(util) > threshold:
                return False
        except (TypeError, ValueError):
            continue
    return True


# ─────────────────────────── state mutators (synchronous) ───────────────────────────

def reserve_run(state: dict, project_id: str, est_tokens: int,
                daily_cap: int, max_concurrent: int, now_ts: float) -> bool:
    """Atomically check and reserve a run slot for *project_id*.

    Checks concurrency_ok AND budget_ok AND pending_ok.
    On success: increments active_runs, adds est_tokens to tokens_today,
    records project in pending_by_project.  Returns True.
    On any guard failure: state is unchanged, returns False.
    """
    if not concurrency_ok(state, max_concurrent):
        return False
    if not budget_ok(state, est_tokens, daily_cap):
        return False
    if not pending_ok(state, project_id):
        return False
    state["active_runs"] = state.get("active_runs", 0) + 1
    state["tokens_today"] = state.get("tokens_today", 0) + est_tokens
    pending = state.setdefault("pending_by_project", {})
    pending[project_id] = now_ts
    return True


def release_run(state: dict, project_id: str) -> None:
    """Release a run slot for *project_id*.

    Decrements active_runs (floor 0) and removes the pending_by_project entry.
    Safe to call even when there is no active reservation.
    """
    state["active_runs"] = max(0, state.get("active_runs", 0) - 1)
    (state.get("pending_by_project") or {}).pop(project_id, None)


# ─────────────────────────── observer primitives ───────────────────────────

def commit_trailer(card_id: str, run_id: str) -> str:
    """Return the git commit trailer block for an autopilot run.

    Starts with a blank line so the trailer is separated from the commit body.
    """
    return f"\n\nX-Cardloop-Autopilot: card/{card_id}\nX-Cardloop-Run: {run_id}"


def append_trajectory(data_dir: "str | Path", record: dict) -> None:
    """Append one JSON record (as a single line) to *data_dir*/autopilot_trajectory.jsonl.

    Creates the file if it does not exist.  Ignores write errors (non-critical).
    """
    p = Path(data_dir) / "autopilot_trajectory.jsonl"
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_trajectory(data_dir: "str | Path", project_id: str | None = None,
                    limit: int = 200) -> list[dict]:
    """Read autopilot trajectory records, most-recent last.

    If *project_id* is given, only records matching that project are returned.
    Returns at most *limit* records.
    """
    p = Path(data_dir) / "autopilot_trajectory.jsonl"
    if not p.exists():
        return []
    records: list[dict] = []
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not isinstance(r, dict):
                    continue
                if project_id is not None and r.get("project") != project_id:
                    continue
                records.append(r)
    except Exception:
        return []
    return records[-limit:]


def fingerprint(action: str, files: list, error_class: str) -> str:
    """Return a stable short hash for a (action, files, error_class) triple.

    Suitable as a loop-detection key.  Uses SHA-256 (first 12 hex chars).
    File list is sorted before hashing so order does not matter.
    """
    key = json.dumps(
        {"action": action, "files": sorted(str(f) for f in files), "error_class": error_class},
        sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def detect_self_inflicted(cwd: str, since_ts: float, files: list) -> dict | None:
    """Check *cwd* git history for autopilot commits that touched *files* since *since_ts*.

    Returns {"commit": <hash>, "run_id": <str or None>} for the most recent
    matching commit, or None if none found or git fails.

    Never raises.
    """
    try:
        file_args = [str(f) for f in files] if files else []
        cmd = [
            "git", "-C", cwd,
            "log",
            f"--since=@{int(since_ts)}",
            "--grep=X-Cardloop-Autopilot",
            "--pretty=format:%H\t%B",
            "--",
        ] + file_args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        # Each entry is "<hash>\t<body>" but --pretty=%H\t%B produces multi-line bodies.
        # We only need the first commit's hash + the run_id from its trailer.
        lines = result.stdout.strip().splitlines()
        if not lines:
            return None
        first_hash = lines[0].split("\t", 1)[0].strip()
        if not first_hash:
            return None
        # Try to extract run_id from the body lines
        run_id: str | None = None
        for line in lines:
            if line.startswith("X-Cardloop-Run:"):
                run_id = line.split(":", 1)[1].strip() or None
                break
        return {"commit": first_hash, "run_id": run_id}
    except Exception:
        return None


def decide_intent(project: dict, signals: dict) -> dict:
    """Pure decision function for shadow mode.  No I/O.

    Given a *project* dict (must have id/name/type/autopilot keys) and a
    *signals* dict with keys:
      - "tests_failing": bool | None  (True=failing, False=passing, None=unknown)
      - "test_summary": str           (human-readable summary from the gate)
      - "backlog_cards": int          (number of cards in backlog column, 0 on error)

    Returns ONE intent dict with:
      action, priority, rationale, project (id or name), mode.

    Priority ladder:
      P1  fix_failing_tests  — tests are failing (software archetype only)
      P3  run_backlog_card   — backlog has cards (any archetype)
      P4  scout              — tests pass, no backlog (any archetype)
      P5  none               — no test signal and no backlog
    """
    mode = get_project_mode(project)
    project_id = project.get("id") or project.get("name") or ""
    archetype = str(project.get("type") or "software").lower()
    is_software = archetype == "software"

    tests_failing: "bool | None" = signals.get("tests_failing")
    test_summary: str = str(signals.get("test_summary") or "")
    backlog_n: int = int(signals.get("backlog_cards") or 0)

    base = {"project": project_id, "mode": mode}

    # P1 — fix failing tests (software only)
    if is_software and tests_failing is True:
        return {**base, "action": "fix_failing_tests", "priority": "P1",
                "rationale": test_summary or "tests are failing"}

    # P3 — run a backlog card
    if backlog_n > 0:
        label = "1 runnable backlog card" if backlog_n == 1 else f"{backlog_n} runnable backlog cards"
        return {**base, "action": "run_backlog_card", "priority": "P3",
                "rationale": label}

    # P5 — no test signal, no backlog → nothing to do
    if tests_failing is None:
        return {**base, "action": "none", "priority": "P5",
                "rationale": "no test signal, no backlog"}

    # P4 — tests pass, no backlog → scout for improvement cards (deferred)
    return {**base, "action": "scout", "priority": "P4",
            "rationale": "idle — would propose improvement cards (deferred)"}


def detect_loop(trajectory: list[dict], project_id: str,
                window_sec: int = 172800) -> str | None:
    """Scan *trajectory* for loop signals within *window_sec* (default 48 h).

    Returns the first signal name found, or None:
      "file_thrash"       — same file in >=3 runs (with >=1 verdict=="fail") in window
      "fingerprint_repeat" — same fingerprint >=3 times in window
      "retry_saturation"  — >=3 records with retry_count>=2 in 24 h

    Records are expected to have these keys (all optional — missing → skipped):
      ts, project, files_changed (list), verdict, fingerprint, retry_count
    """
    now = time.time()
    window_start = now - window_sec
    day_start = now - 86400

    # Filter to this project within the window
    relevant = [
        r for r in trajectory
        if isinstance(r, dict)
        and r.get("project") == project_id
        and (r.get("ts") or 0) >= window_start
    ]
    if not relevant:
        return None

    # 1. file_thrash: same file in >=3 runs with at least one fail
    from collections import Counter
    file_run_counts: Counter[str] = Counter()
    file_has_fail: dict[str, bool] = {}
    for r in relevant:
        files = r.get("files_changed") or []
        verdict = r.get("verdict", "")
        for f in files:
            file_run_counts[f] += 1
            if verdict == "fail":
                file_has_fail[f] = True
    for f, cnt in file_run_counts.items():
        if cnt >= 3 and file_has_fail.get(f):
            return "file_thrash"

    # 2. fingerprint_repeat: same fingerprint >=3 times
    fp_counts: Counter[str] = Counter()
    for r in relevant:
        fp = r.get("fingerprint")
        if fp:
            fp_counts[fp] += 1
    if fp_counts and fp_counts.most_common(1)[0][1] >= 3:
        return "fingerprint_repeat"

    # 3. retry_saturation: >=3 records with retry_count>=2 in 24 h
    retry_high = sum(
        1 for r in relevant
        if (r.get("ts") or 0) >= day_start
        and (r.get("retry_count") or 0) >= 2
    )
    if retry_high >= 3:
        return "retry_saturation"

    return None

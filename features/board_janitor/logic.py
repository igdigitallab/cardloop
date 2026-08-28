"""features/board_janitor/logic.py — pure decision helpers for the board janitor.

The Review column has no way out: nothing in the cockpit ever moves a card from
Review to Done, so finished work piles up until the operator asks an agent to
"clean the board".  This module holds the decision half of the fix — pure
functions, no I/O — so the policy is testable without a running cockpit.

Policy in one line: a card is auto-accepted ONLY on objective evidence (its own
run succeeded, nothing is left unmerged, and the project's tests are green).
Everything else is surfaced in a digest for the operator instead of being
silently archived.  This mirrors the one automation pattern that is actually
proven in production (Renovate/Dependabot automerge on a green required check),
rather than letting a model grade its own homework.
"""
from __future__ import annotations

import os
import time
from typing import Any

# ── Env-configured thresholds (read once at import; no mutable state) ────────
# BOARD_JANITOR_MODE: off | digest | accept
#   off     — the loop does nothing at all
#   digest  — never archives; only reports what is parked in Review
#   accept  — archives cards that pass the objective gate, digests the rest
MODE: str = (os.environ.get("BOARD_JANITOR_MODE", "accept") or "accept").strip().lower()
MODES: tuple[str, ...] = ("off", "digest", "accept")

# Hours a card must sit in Review before auto-acceptance is even considered.
# Short enough to keep the board moving, long enough that the operator can look
# at fresh work before it is archived.
ACCEPT_AFTER_H: float = float(os.environ.get("BOARD_JANITOR_ACCEPT_AFTER_H", "24"))
# Hours after which a card that CANNOT be auto-accepted is reported to the operator.
DIGEST_AFTER_H: float = float(os.environ.get("BOARD_JANITOR_DIGEST_AFTER_H", "72"))
# Loop interval.
INTERVAL_SEC: int = int(os.environ.get("BOARD_JANITOR_INTERVAL_SEC", "1800"))


# ── Fleet-wide autonomy switch ───────────────────────────────────────────────
# One file, read by every autonomous loop, so the operator has a single place to
# stop ALL unattended activity. Per-feature switches (BOARD_JANITOR_MODE,
# autopilot's own pause) still work — this is the master cut-off above them.

def _autonomy_path(data_dir: "str | Any"):
    from pathlib import Path
    return Path(data_dir) / "autonomy.json"


def autonomy_paused(data_dir: "str | Any") -> bool:
    """True when the operator has pulled the fleet-wide autonomy switch."""
    import json
    try:
        p = _autonomy_path(data_dir)
        if not p.exists():
            return False
        return bool(json.loads(p.read_text(encoding="utf-8")).get("paused"))
    except Exception:
        return False  # an unreadable switch must not silently freeze the cockpit


def set_autonomy_paused(data_dir: "str | Any", paused: bool) -> dict:
    """Persist the fleet-wide autonomy switch (atomic write). Returns the new state."""
    import json
    state = {"paused": bool(paused), "changed_at": int(time.time())}
    p = _autonomy_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return state


def valid_mode(m: object) -> bool:
    """True if *m* is a recognised janitor mode string."""
    return isinstance(m, str) and m in MODES


def card_age_hours(card: dict, now: "float | None" = None) -> "float | None":
    """Hours since the card entered Review, or None when it carries no stamp.

    A missing stamp is not "age zero" — it is "unknown", and the caller must
    stamp it rather than treat an ancient card as brand new.
    """
    rt = card.get("rt")
    if not isinstance(rt, int) or rt <= 0:
        return None
    return max(0.0, ((now if now is not None else time.time()) - rt) / 3600.0)


def stamp_missing_rt(cards: list, now: "float | None" = None) -> int:
    """Stamp every card that has no review timestamp.  Returns how many were stamped.

    Grandfathering: cards that were already in Review before this feature existed
    have no stamp.  They are stamped on first sight, which means they wait out a
    full ACCEPT_AFTER_H window from now instead of being archived immediately on
    the janitor's first run.  Erring towards the operator seeing them once.
    """
    ts = int(now if now is not None else time.time())
    stamped = 0
    for c in cards:
        if not isinstance(c.get("rt"), int) or c.get("rt", 0) <= 0:
            c["rt"] = ts
            stamped += 1
    return stamped


def run_is_settled(run_meta: "dict | None") -> "tuple[bool, str]":
    """Did this card's own run finish cleanly with nothing left dangling?

    Returns (settled, reason).  A worktree run that produced changes which were
    never applied or discarded is NOT settled: archiving it would bury a diff the
    operator never merged (the C2 apply/discard gate is still open on it).
    """
    if not isinstance(run_meta, dict):
        return False, "no run record"
    if run_meta.get("outcome") != "ok":
        return False, f"run outcome={run_meta.get('outcome') or 'unknown'}"
    if run_meta.get("has_changes") and not (run_meta.get("applied") or run_meta.get("discarded")):
        return False, "worktree changes still unapplied"
    return True, "run ok"


def decide_card(
    card: dict,
    run_meta: "dict | None",
    tests_green: "bool | None",
    now: "float | None" = None,
    mode: "str | None" = None,
    accept_after_h: "float | None" = None,
    digest_after_h: "float | None" = None,
) -> "tuple[str, str]":
    """Decide what to do with one Review card: ('accept'|'digest'|'hold', reason).

    *tests_green* is a tri-state on purpose: True = the project's tests ran and
    passed, False = they failed, None = there is no trustworthy signal.  Only an
    explicit True can authorise archiving; "no tests configured" must never read
    as "safe to close", which is the failure mode that makes autonomy dangerous
    on the 20-odd projects that have no test suite at all.
    """
    m = (mode or MODE).strip().lower()
    if m == "off":
        return "hold", "janitor off"
    acc_h = ACCEPT_AFTER_H if accept_after_h is None else accept_after_h
    dig_h = DIGEST_AFTER_H if digest_after_h is None else digest_after_h

    age = card_age_hours(card, now)
    if age is None:
        return "hold", "no review timestamp yet"

    if m == "accept" and age >= acc_h:
        settled, why = run_is_settled(run_meta)
        if settled and tests_green is True:
            return "accept", f"{why}, tests green, {age:.0f}h in review"
        blocker = why if not settled else (
            "tests failing" if tests_green is False else "no trustworthy test signal"
        )
        if age >= dig_h:
            return "digest", blocker
        return "hold", blocker

    if age >= dig_h:
        return "digest", "parked in review" if m != "accept" else "awaiting operator"
    return "hold", "too fresh"


def build_digest(entries: list, now: "float | None" = None) -> str:
    """Render the operator-facing digest.

    One document for the whole fleet rather than a notification per card: a board
    that reports 40 separate times trains the operator to ignore it, which is the
    exact failure the digest exists to fix.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now if now is not None else time.time()))
    lines = [f"# Board digest — {stamp}", ""]
    if not entries:
        lines.append("Nothing parked. Every Review card is either fresh or already accepted.")
        return "\n".join(lines) + "\n"

    accepted = [e for e in entries if e.get("action") == "accept"]
    parked = [e for e in entries if e.get("action") == "digest"]

    if accepted:
        lines.append(f"## Auto-accepted ({len(accepted)})")
        lines.append("")
        lines.append("Archived to DONE.md — run succeeded, nothing unmerged, tests green.")
        lines.append("")
        for e in accepted:
            lines.append(f"- **{e.get('project', '?')}** — {e.get('text', '')} `{e.get('card_id', '')}`")
        lines.append("")

    if parked:
        lines.append(f"## Waiting on you ({len(parked)})")
        lines.append("")
        lines.append("These cannot be closed automatically — the reason is next to each.")
        lines.append("")
        by_project: dict = {}
        for e in parked:
            by_project.setdefault(e.get("project", "?"), []).append(e)
        for proj, items in sorted(by_project.items()):
            lines.append(f"### {proj}")
            for e in items:
                age = e.get("age_h")
                age_s = f"{age:.0f}h" if isinstance(age, (int, float)) else "?"
                lines.append(f"- {e.get('text', '')} — {e.get('reason', '')} ({age_s}) `{e.get('card_id', '')}`")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"

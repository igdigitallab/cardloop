"""features/load_monitor/alerts.py — when does a red meter become a notification?

Pure decision logic (no I/O) so the policy is unit-testable. The dot is passive and nobody stares
at it; this is what closes "I see nothing until my sessions drop".

Policy:
- crit for >= CRIT_AFTER_S  -> loud alert (toast + Web Push + inbox file). Memory-class signals
  (mem / mem_psi / oom) use FAST_CRIT_AFTER_S: an OOM can arrive within two minutes. A dip shorter than
  CRIT_GRACE_S does not restart the clock, so a host oscillating crit/warn is still reported.
  Global cooldown COOLDOWN_S; a NEW set of crit signals may re-alert after MIN_GAP_S.
- warn continuously for >= WARN_AFTER_S  -> quiet alert (inbox file only), at most every WARN_COOLDOWN_S;
  the warn clock starts when the level FIRST settles at warn (a crit episode does not pre-age it).
- never alert on "unknown" or on a level that has already recovered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CRIT_AFTER_S = 120.0
FAST_CRIT_AFTER_S = 20.0
FAST_IDS = frozenset({"mem", "mem_psi", "oom"})
CRIT_GRACE_S = 30.0
COOLDOWN_S = 30 * 60.0
MIN_GAP_S = 5 * 60.0
WARN_AFTER_S = 30 * 60.0
WARN_COOLDOWN_S = 6 * 3600.0


@dataclass
class AlertState:
    crit_since: "float | None" = None
    last_crit_seen: float = -1e18
    warn_since: "float | None" = None
    last_loud: float = -1e18
    last_loud_key: "frozenset[str]" = field(default_factory=frozenset)
    last_quiet: float = -1e18


@dataclass(frozen=True)
class Alert:
    loud: bool
    title: str
    body: str


def _describe(snap: "dict[str, Any]", level: str) -> str:
    bad = [s for s in snap.get("signals", []) if s["level"] == level]
    lines = [f"- {s['text']}" for s in bad[:4]]
    hint = next((s["hint"] for s in bad if s.get("hint")), "")
    top = snap.get("top") or []
    if top:
        lines.append("Heaviest: " + ", ".join(f"{t['project']} {t['rss_mb']} MiB" for t in top[:3]))
    return "\n".join(lines + ([f"Hint: {hint}"] if hint else []))


def decide(snap: "dict[str, Any] | None", st: AlertState, now: float) -> "Alert | None":
    level = (snap or {}).get("level")
    if level == "crit":
        st.last_crit_seen = now
        if st.crit_since is None:
            st.crit_since = now
    elif st.crit_since is not None and now - st.last_crit_seen > CRIT_GRACE_S:
        st.crit_since = None                       # a real recovery, not a one-sample dip
    # The warn clock only runs while the level sits at warn itself.
    if level != "warn":
        st.warn_since = None
    if snap is None or level not in ("warn", "crit"):
        return None
    if level == "warn" and st.warn_since is None:
        st.warn_since = now
    if level == "crit":
        crit_ids = frozenset(s["id"] for s in snap["signals"] if s["level"] == "crit")
        after = FAST_CRIT_AFTER_S if crit_ids & FAST_IDS else CRIT_AFTER_S
        if now - st.crit_since >= after:
            since = now - st.last_loud
            if since >= COOLDOWN_S or (crit_ids != st.last_loud_key and since >= MIN_GAP_S):
                st.last_loud, st.last_loud_key = now, crit_ids
                return Alert(True, "Cardloop: server overloaded", _describe(snap, "crit"))
        return None
    if now - st.warn_since >= WARN_AFTER_S and now - st.last_quiet >= WARN_COOLDOWN_S:
        st.last_quiet = now
        return Alert(False, "Cardloop: server load elevated", _describe(snap, "warn"))
    return None

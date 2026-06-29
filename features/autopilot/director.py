"""features/autopilot/director.py — Autopilot DIRECTOR v1 (plan-only).

PLAN-ONLY INVARIANT: _run_director NEVER edits project code, creates a
worktree, commits, calls _start_card_run/_drain_queue/_queue_enqueue, or
runs any worker/card.  It runs ONE reasoning turn (run_engine) that PRODUCES
a structured plan, then writes planning cards + a notebook note + a
trajectory log.  That is the complete extent of its side-effects.

Import rule: feature → core is safe (see spec-068 IRON RULE).
"""
from __future__ import annotations

from webapp import (
    _load_board,
    _save_board,
    _get_board_lock,
    _new_card_id,
    _bus_publish,
)

from features.autopilot import logic as _autopilot
from features.autopilot.loop import _autopilot_test_signal


# ─────────────────────────────────────────────────────────────────────────────
# Board summary helper
# ─────────────────────────────────────────────────────────────────────────────

def _board_summary_text(cwd: str) -> str:
    """Return a short plain-text summary of the board at *cwd* (read-only).

    Format: one line per column listing card titles.  Returns a fallback
    string on any error — the director run must never abort due to a missing
    TASKS.md.
    """
    try:
        _, _, cols = _load_board(cwd)
    except Exception as exc:
        return f"(board unavailable: {exc})"
    lines: list[str] = []
    for col_name, cards in cols.items():
        if not cards:
            continue
        titles = [c.get("text", "(untitled)") for c in cards]
        lines.append(f"**{col_name}** ({len(cards)}): " + "; ".join(titles[:10]))
        if len(cards) > 10:
            lines.append(f"  … and {len(cards) - 10} more")
    return "\n".join(lines) if lines else "(board is empty)"


# ─────────────────────────────────────────────────────────────────────────────
# Director dedup helper
# ─────────────────────────────────────────────────────────────────────────────

_DIRECTOR_PREFIX = "[director] "


def _open_card_titles(cwd: str) -> set[str]:
    """Return normalised titles of all open (non-done, non-archive) cards.

    Used for dedup: the director will not create a card whose normalised title
    matches an existing open card.  Director-created cards have a "[director] "
    prefix in their stored text; that prefix is stripped before normalisation so
    dedup works correctly on subsequent runs.
    """
    from engine import _norm_title  # noqa: PLC0415 (local import — engine is always available)
    try:
        _, _, cols = _load_board(cwd)
    except Exception:
        return set()
    skip_cols = {"done", "archive", "shipped", "cancelled"}
    titles: set[str] = set()
    for col_name, cards in cols.items():
        if col_name in skip_cols:
            continue
        for c in cards:
            t = c.get("text", "")
            if not t:
                continue
            # Strip the director prefix so repeated director runs deduplicate correctly.
            if t.startswith(_DIRECTOR_PREFIX):
                t = t[len(_DIRECTOR_PREFIX):]
            titles.add(_norm_title(t))
    return titles


# ─────────────────────────────────────────────────────────────────────────────
# Director run
# ─────────────────────────────────────────────────────────────────────────────

async def _run_director(ctx: dict, project: dict) -> dict:
    """Run the Autopilot Director v1 for *project* (plan-only — no code edits).

    1. Guard checks (master on, project enabled, rate-limit OK, not busy).
    2. Reserve running lock synchronously.
    3. Publish bus run_start so the cockpit live-trace shows the director run.
    4. Gather read-only context: board summary, test signal, notebook.
    5. Call run_engine (ephemeral=True, entrypoint="director") — ONE reasoning turn.
    6. Apply plan-only mutations: create proposed cards in backlog (dedup),
       append notebook note, append trajectory record.
    7. Finally: pop running lock, publish bus run_end.
    8. Return structured result dict.
    """
    import time as _time
    from engine import _norm_title  # noqa: PLC0415

    DATA = ctx["DATA"]

    # ── Guard 1: master autopilot must be active ──────────────────────────────
    state = _autopilot.load_state(DATA)
    if not _autopilot.is_active(state):
        return {"ok": False, "reason": "autopilot_inactive"}

    # ── Guard 2: project must be in propose or auto mode ─────────────────────
    if _autopilot.get_project_mode(project) not in ("propose", "auto"):
        return {"ok": False, "reason": "project_not_enabled"}

    # ── Guard 3: rate-limit headroom ──────────────────────────────────────────
    rate_limits = ctx.get("rate_limits") or {}
    if not _autopilot.rate_limit_ok(rate_limits, _autopilot.RL_RESERVE):
        return {"ok": False, "reason": "rate_limit_headroom"}

    project_id = project.get("id", "")
    cwd = project.get("cwd", "")
    name = project.get("name", project_id)

    # ── Guard 4: not already busy (synchronous check + reserve, NO await) ────
    session_key = project.get("session_key") or project.get("tg_thread", "")
    director_session_key = f"director:{session_key}" if session_key else f"director:{project_id}"

    if ctx["running"].get(director_session_key) is not None:
        return {"ok": False, "reason": "busy"}
    ctx["running"][director_session_key] = True
    # ── end of critical section ──

    # ── Guard 5: engine must be available (checked after state guards) ────────
    run_engine = ctx.get("run_engine")
    if run_engine is None:
        ctx["running"].pop(director_session_key, None)
        return {"ok": False, "reason": "no_engine"}

    ok = False
    result_payload: dict = {}

    try:
        # ── Publish run_start so live-trace shows the director ────────────────
        _bus_publish(director_session_key, {
            "kind": "run_start",
            "source": "director",
            "prompt": f"[Director] planning run for {name}",
            "run_id": f"director:{project_id}",
        })

        # ── Gather read-only context ──────────────────────────────────────────
        board_summary = _board_summary_text(cwd)
        tests_failing, test_summary = await _autopilot_test_signal(project)
        notebook = _autopilot.read_notebook(DATA, project_id)

        prompt = _autopilot.build_director_input(name, board_summary, test_summary, notebook)
        system_prompt = {
            "append": _autopilot.DIRECTOR_PROMPT.format(project_name=name),
        }

        # ── Run ONE reasoning turn (plan-only, ephemeral, structured output) ──
        structured: dict | None = None
        async for event in run_engine(
            name,
            cwd,
            prompt,
            director_session_key,
            model=_autopilot.director_model(),
            system_prompt=system_prompt,
            output_format=_autopilot.DIRECTOR_SCHEMA,
            entrypoint="director",
            ephemeral=True,
            disallowed_tools_extra=_autopilot.DIRECTOR_DISALLOWED_TOOLS,
            ctx=ctx,
        ):
            etype = event["type"]
            if etype == "result":
                structured = event.get("structured_output")
            elif etype == "error":
                raise event["exc"]
            # text/text_delta/tool events are silently consumed — the director
            # output is entirely captured in structured_output; prose is unused.

        if not isinstance(structured, dict):
            return {"ok": False, "reason": "no_structured_output"}

        # ── Plan-only mutations ───────────────────────────────────────────────

        # 1. Create proposed cards in backlog (dedup vs existing open cards).
        open_titles = _open_card_titles(cwd)
        cards_created = 0
        proposed = structured.get("proposed_cards") or []

        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            if "backlog" not in cols:
                cols["backlog"] = []
            for card_spec in proposed[:3]:  # cap at 3
                title = (card_spec.get("title") or "").strip()
                why = (card_spec.get("why") or "").strip()
                if not title:
                    continue
                if _norm_title(title) in open_titles:
                    continue  # deduplicate
                new_card: dict = {
                    "id": _new_card_id(),
                    "text": f"[director] {title}",
                }
                if why:
                    new_card["description"] = why
                cols["backlog"].append(new_card)
                # Track for dedup within this batch
                open_titles.add(_norm_title(title))
                cards_created += 1
            _save_board(cwd, name, preamble, cols)

        # 2. Append notebook note.
        now = _time.time()
        note = structured.get("notebook_note") or ""
        if note:
            _autopilot.append_notebook(DATA, project_id, note, now)

        # 3. Append trajectory record.
        _autopilot.append_trajectory(DATA, {
            "ts": now,
            "project": project_id,
            "action": "director_plan",
            "priority": structured.get("priority"),
            "rationale": structured.get("assessment"),
            "question": structured.get("question_for_operator"),
            "shadow": True,
        })

        ok = True
        result_payload = {
            "ok": True,
            "assessment": structured.get("assessment"),
            "priority": structured.get("priority"),
            "focus": structured.get("focus"),
            "proposed_cards": proposed,
            "question_for_operator": structured.get("question_for_operator"),
            "notebook_note": note,
            "cards_created": cards_created,
        }

    except Exception as exc:
        result_payload = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    finally:
        _bus_publish(director_session_key, {
            "kind": "run_end",
            "outcome": "ok" if ok else "fail",
            "run_id": f"director:{project_id}",
        })
        ctx["running"].pop(director_session_key, None)

    return result_payload

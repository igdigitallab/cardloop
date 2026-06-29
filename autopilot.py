"""autopilot.py — compatibility shim (spec-068 migration).

The real implementation now lives in ``features/autopilot/logic.py``.
This shim re-exports everything so that existing ``import autopilot``
references in ``webapp.py`` (core functions like ``_collect_projects`` and
``_project_settings_view`` that use pure helpers) continue to work without
any top-level ``import features.*`` in core.

DO NOT add business logic here.  All logic lives in logic.py.
"""
from features.autopilot.logic import *  # noqa: F401, F403
from features.autopilot.logic import (  # noqa: F401 (explicit re-export for type checkers)
    MODES,
    DEFAULT_MODE,
    DAILY_TOKEN_CAP,
    MAX_CONCURRENT,
    RL_RESERVE,
    DIRECTOR_PROMPT,
    DIRECTOR_SCHEMA,
    DIRECTOR_DISALLOWED_TOOLS,
    valid_mode,
    get_project_mode,
    load_state,
    save_state,
    is_active,
    rollover_day,
    budget_ok,
    concurrency_ok,
    pending_ok,
    cooldown_ok,
    rate_limit_ok,
    reserve_run,
    release_run,
    commit_trailer,
    append_trajectory,
    read_trajectory,
    fingerprint,
    detect_self_inflicted,
    decide_intent,
    detect_loop,
    director_model,
    read_notebook,
    append_notebook,
    build_director_input,
)

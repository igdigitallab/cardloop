"""spec-078 Phase 3a — one canonical brain per project.

Two memory systems overlap. The CLI's native auto-memory (~/.claude/projects/<slug>/memory/)
only ingests — it appends what a session learned and never prunes, so its MEMORY.md index, loaded
verbatim on every bootstrap, rots and grows (claude-ops-bot: 88 files, 17 KB index, ~4.3k tokens
per session, paid forever). The curated ./.claude-ops/memory/ is deliberate, linted, and capped by
the context pack before it reaches the prompt.

`agents_config.memory = "project"` turns the native one off, leaving the curated wiki as the
project's only brain. Default stays "auto" — no behaviour change until a project opts in.

The load-bearing subtlety: the switch rides in `env`, and `env` is deliberately EXCLUDED from the
live-client fingerprint (it carries per-turn noise like TG_CHAT_ID). Without threading the mode in
explicitly, flipping it would leave a persistent subprocess running the old brain.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
from claude_agent_sdk import ClaudeAgentOptions


# ─────────────────────────── env switch ───────────────────────────────────────


def test_project_mode_disables_native_auto_memory():
    """The env var the bundled CLI actually reads (process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY)."""
    assert engine._memory_env_overrides("project") == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def test_auto_mode_touches_nothing():
    assert engine._memory_env_overrides("auto") == {}


@pytest.mark.parametrize("mode", [None, "", "typo", "PROJECT", 0])
def test_unknown_mode_degrades_to_auto(mode):
    """A typo in agents_config must not take a project's sessions down."""
    assert engine._memory_env_overrides(mode) == {}


def test_default_mode_is_auto():
    """Opt-in only: an existing project must not silently lose its auto-memory on upgrade."""
    assert engine._DEFAULT_MEMORY_MODE == "auto"
    assert engine._memory_env_overrides(engine._DEFAULT_MEMORY_MODE) == {}


# ─────────────────────────── agents_config wiring ─────────────────────────────


def test_agents_config_maps_memory_to_run_engine_kwarg():
    kwargs = engine._build_agents_kwargs({"memory": "project"})
    assert kwargs["project_memory"] == "project"


def test_absent_memory_key_yields_no_kwarg():
    """Omitting the key must not pin the mode — run_engine falls back to its own default."""
    assert "project_memory" not in engine._build_agents_kwargs({"skills": ["a"]})


def test_empty_agents_config_yields_nothing():
    assert engine._build_agents_kwargs({}) == {}


# ─────────────────────────── live-client fingerprint ──────────────────────────


def _opts():
    return ClaudeAgentOptions(model="opus", cwd="/tmp/x", permission_mode="bypassPermissions")


def test_flipping_memory_mode_changes_the_fingerprint():
    """Otherwise the persistent subprocess keeps the old brain until an idle eviction.

    `env` is excluded from the fingerprint on purpose, so the mode must be passed explicitly.
    """
    auto = engine._compute_fingerprint(_opts(), memory_mode="auto")
    project = engine._compute_fingerprint(_opts(), memory_mode="project")

    assert auto != project


def test_same_memory_mode_keeps_the_fingerprint_stable():
    """A stable fingerprint is what lets the live client survive between turns."""
    a = engine._compute_fingerprint(_opts(), memory_mode="project")
    b = engine._compute_fingerprint(_opts(), memory_mode="project")

    assert a == b


def test_env_alone_still_does_not_move_the_fingerprint():
    """Pins the reason the explicit param exists: putting the flag only in env would be a no-op."""
    bare = ClaudeAgentOptions(model="opus", cwd="/tmp/x", permission_mode="bypassPermissions")
    with_env = ClaudeAgentOptions(model="opus", cwd="/tmp/x", permission_mode="bypassPermissions",
                                  env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})

    assert engine._compute_fingerprint(bare) == engine._compute_fingerprint(with_env)


# ─────────────────────────── API boundary ─────────────────────────────────────


def test_webapp_accepts_exactly_the_engine_modes():
    """webapp cannot `from engine import _MEMORY_MODES` (engine imports webapp — a cycle), so the
    validator hardcodes the tuple. Pin them together or they drift apart silently."""
    import inspect

    import webapp as _webapp

    src = inspect.getsource(_webapp.api_project_settings_post)
    for mode in engine._MEMORY_MODES:
        assert f'"{mode}"' in src, f"webapp validator must accept engine mode {mode!r}"
    assert 'cfg_val not in ("auto", "project")' in src


# ── internal helpers must never write a project's wiki ────────────────────────
#
# allowed_tools=[] blocks Edit/Write, but the CLI's own memory-extraction pass uses internal
# tooling that the allowlist does not gate, and it INHERITS the helper's model. On 2026-06-23 a
# haiku helper running from the ops-scratch cwd wrote four articles into two project wikis — one
# of them a pure progress ledger. The env flag is the only thing that actually stops it.


def test_reconcile_helper_disables_auto_memory():
    import inspect

    src = inspect.getsource(engine.reconcile_board)
    assert '_memory_env_overrides("project")' in src or "CLAUDE_CODE_DISABLE_AUTO_MEMORY" in src


def test_handoff_and_title_helpers_disable_auto_memory():
    import inspect

    import webapp as _webapp

    for fn in (_webapp._build_handoff_inner, _webapp._build_session_title):
        src = inspect.getsource(fn)
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" in src, f"{fn.__name__} may write a project wiki"

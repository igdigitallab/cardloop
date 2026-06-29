"""
tests/test_autopilot_shadow.py — Shadow-mode autopilot (Phase 2).

Covers:
  Unit tests for autopilot.decide_intent (pure function):
    - Failing tests → P1 fix_failing_tests (software archetype)
    - Passing tests + backlog → P3 run_backlog_card
    - Failing tests + no backlog (software) → P1
    - Passing tests + no backlog → P4 scout
    - Unknown test signal + no backlog → P5 none
    - Content/ops archetype never returns fix_failing_tests

  Integration tests for _autopilot_tick_once:
    - Global disabled → returns [] and appends nothing
    - Active, mode=propose, tests failing → returns one P1 intent;
      run_engine / _start_card_run are never called (monkeypatched to raise)

  Endpoint tests:
    - POST /api/autopilot/tick (manual trigger)
    - GET /api/autopilot/decisions?limit=N
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import autopilot
import webapp as _webapp
from webapp import _derive_token


# ═══════════════════════════════════════════════════════════════════
# Unit tests — autopilot.decide_intent
# ═══════════════════════════════════════════════════════════════════

def _sw(extra=None) -> dict:
    """Minimal software project dict."""
    base = {"id": "proj-sw", "name": "My SW", "type": "software", "cwd": "/tmp/sw",
            "autopilot": "propose", "test_cmd": None}
    if extra:
        base.update(extra)
    return base


def _content(extra=None) -> dict:
    """Minimal content project dict."""
    base = {"id": "proj-content", "name": "My Content", "type": "content", "cwd": "/tmp/c",
            "autopilot": "propose", "test_cmd": None}
    if extra:
        base.update(extra)
    return base


# ─── Failing tests ────────────────────────────────────────────────


def test_decide_failing_tests_returns_p1_software():
    signals = {"tests_failing": True, "test_summary": "2 tests failed", "backlog_cards": 0}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["action"] == "fix_failing_tests"
    assert intent["priority"] == "P1"
    assert "2 tests failed" in intent["rationale"]
    assert intent["project"] == "proj-sw"
    assert intent["mode"] == "propose"


def test_decide_failing_tests_includes_project_and_mode():
    signals = {"tests_failing": True, "test_summary": "oops", "backlog_cards": 3}
    intent = autopilot.decide_intent(_sw({"id": "my-id", "autopilot": "auto"}), signals)
    assert intent["project"] == "my-id"
    assert intent["mode"] == "auto"


def test_decide_failing_uses_test_summary_as_rationale():
    summary = "AssertionError in test_foo"
    signals = {"tests_failing": True, "test_summary": summary, "backlog_cards": 0}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["rationale"] == summary


def test_decide_failing_empty_summary_gets_default_rationale():
    signals = {"tests_failing": True, "test_summary": "", "backlog_cards": 0}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["rationale"]  # non-empty fallback


# ─── Backlog cards ────────────────────────────────────────────────


def test_decide_passing_with_backlog_returns_p3():
    signals = {"tests_failing": False, "test_summary": "all pass", "backlog_cards": 2}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["action"] == "run_backlog_card"
    assert intent["priority"] == "P3"
    assert "2" in intent["rationale"]


def test_decide_backlog_one_card_singular_label():
    signals = {"tests_failing": False, "test_summary": "", "backlog_cards": 1}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["action"] == "run_backlog_card"
    assert "1" in intent["rationale"]


def test_decide_unknown_tests_with_backlog_returns_p3():
    """Unknown test signal but there IS backlog → P3 (backlog takes priority over P5)."""
    signals = {"tests_failing": None, "test_summary": "", "backlog_cards": 5}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["action"] == "run_backlog_card"
    assert intent["priority"] == "P3"


# ─── Scout / idle ─────────────────────────────────────────────────


def test_decide_passing_no_backlog_returns_p4_scout():
    signals = {"tests_failing": False, "test_summary": "all pass", "backlog_cards": 0}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["action"] == "scout"
    assert intent["priority"] == "P4"


# ─── No test signal, no backlog ───────────────────────────────────


def test_decide_unknown_tests_no_backlog_returns_p5_none():
    signals = {"tests_failing": None, "test_summary": "", "backlog_cards": 0}
    intent = autopilot.decide_intent(_sw(), signals)
    assert intent["action"] == "none"
    assert intent["priority"] == "P5"


def test_decide_missing_signals_treated_as_unknown():
    """Empty signals dict → all None/0 → P5 none."""
    intent = autopilot.decide_intent(_sw(), {})
    assert intent["action"] == "none"
    assert intent["priority"] == "P5"


# ─── Archetype: content/ops never fix_failing_tests ──────────────


def test_decide_content_archetype_failing_tests_skips_p1():
    signals = {"tests_failing": True, "test_summary": "failing!", "backlog_cards": 0}
    intent = autopilot.decide_intent(_content(), signals)
    assert intent["action"] != "fix_failing_tests"


def test_decide_content_archetype_failing_tests_no_backlog_returns_p5():
    signals = {"tests_failing": True, "test_summary": "fail", "backlog_cards": 0}
    intent = autopilot.decide_intent(_content(), signals)
    # No fix_failing_tests, no backlog, tests_failing is not None → scout
    # (falling through: failing but non-software → not P1; backlog=0 → not P3;
    #  tests_failing is True (not None) → not P5 → P4 scout)
    assert intent["action"] in ("scout", "none")
    assert intent["action"] != "fix_failing_tests"


def test_decide_ops_archetype_failing_tests_no_backlog_returns_scout():
    signals = {"tests_failing": True, "test_summary": "fail", "backlog_cards": 0}
    intent = autopilot.decide_intent(
        {"id": "p", "type": "ops", "autopilot": "propose"}, signals
    )
    assert intent["action"] != "fix_failing_tests"


def test_decide_content_archetype_backlog_returns_p3():
    signals = {"tests_failing": True, "test_summary": "fail", "backlog_cards": 3}
    intent = autopilot.decide_intent(_content(), signals)
    assert intent["action"] == "run_backlog_card"
    assert intent["priority"] == "P3"


def test_decide_content_archetype_passing_no_backlog_returns_scout():
    signals = {"tests_failing": False, "test_summary": "pass", "backlog_cards": 0}
    intent = autopilot.decide_intent(_content(), signals)
    assert intent["action"] == "scout"
    assert intent["priority"] == "P4"


# ═══════════════════════════════════════════════════════════════════
# Integration tests — _autopilot_tick_once
# ═══════════════════════════════════════════════════════════════════

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def tick_tmp(tmp_path):
    """Temp data + project dir for tick tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    return data_dir, proj_dir


def _make_tick_ctx(data_dir: Path, proj_dir: Path, *, enabled: bool = True,
                   mode: str = "propose") -> dict:
    """Minimal ctx for _autopilot_tick_once."""
    return {
        "DATA": data_dir,
        "topics": {
            "999:1": {
                "project": "shadow-proj",
                "cwd": str(proj_dir),
                "model": "sonnet",
                "autopilot": mode,
                "type": "software",
            }
        },
        "sessions": {},
        "running": {},
        "password": "x",
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
        "_auth_token": _derive_token("x"),
        # Mark global_enabled in state file
        "_test_enabled": enabled,
    }


def _set_state(data_dir: Path, *, enabled: bool, paused: bool = False) -> None:
    state = autopilot.load_state(data_dir)
    state["global_enabled"] = enabled
    state["paused"] = paused
    autopilot.save_state(data_dir, state)


# ── Global disabled → immediate no-op ─────────────────────────────


@pytest.mark.asyncio
async def test_tick_once_global_disabled_returns_empty(tick_tmp):
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=False)
    _set_state(data_dir, enabled=False)

    result = await _webapp._autopilot_tick_once(ctx)
    assert result == []


@pytest.mark.asyncio
async def test_tick_once_global_disabled_appends_nothing(tick_tmp):
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=False)
    _set_state(data_dir, enabled=False)

    await _webapp._autopilot_tick_once(ctx)
    records = autopilot.read_trajectory(data_dir)
    assert records == []


@pytest.mark.asyncio
async def test_tick_once_paused_returns_empty(tick_tmp):
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True)
    _set_state(data_dir, enabled=True, paused=True)

    result = await _webapp._autopilot_tick_once(ctx)
    assert result == []


# ── Active tick with failing tests ────────────────────────────────


@pytest.mark.asyncio
async def test_tick_once_active_failing_tests_returns_p1(tick_tmp):
    """Active, mode=propose, tests failing → one intent with action=fix_failing_tests."""
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)

    # _run_quality_gate reports "risky" (tests failing)
    fake_gate = {"verdict": "risky", "tests": {"cmd": "pytest", "output": "1 failed", "detected": True, "ok": False, "exit_code": 1, "timed_out": False}, "lint": None}

    # Monkeypatch: run_engine and _start_card_run must NEVER be called
    execution_guard_called = []

    async def _forbidden_run_engine(*a, **kw):
        execution_guard_called.append("run_engine")
        raise AssertionError("run_engine must not be called in shadow mode")

    async def _forbidden_start_card_run(*a, **kw):
        execution_guard_called.append("_start_card_run")
        raise AssertionError("_start_card_run must not be called in shadow mode")

    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)), \
         patch.object(_webapp, "run_engine", new=_forbidden_run_engine, create=True), \
         patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run, create=True):
        result = await _webapp._autopilot_tick_once(ctx)

    assert len(result) == 1
    intent = result[0]
    assert intent["action"] == "fix_failing_tests"
    assert intent["priority"] == "P1"
    assert intent["shadow"] is True
    assert "fingerprint" in intent
    assert "ts" in intent
    # No execution functions were called
    assert execution_guard_called == []


# ── invariant #1 (spec-067 v3): execution-class intents blocked on a non-isolatable tree ──

def _git_init_commit(path: Path) -> None:
    """Turn *path* into a git repo with one baseline commit (clean tree)."""
    import subprocess
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=str(path), check=True, capture_output=True)


_FAIL_GATE = {"verdict": "risky", "tests": {"cmd": "pytest", "output": "1 failed",
              "detected": True, "ok": False, "exit_code": 1, "timed_out": False}, "lint": None}


@pytest.mark.asyncio
async def test_tick_blocks_execution_on_non_git_tree(tick_tmp):
    """Non-git project + failing tests → P1 intent flagged blocked=dirty_tree_no_isolation."""
    data_dir, proj_dir = tick_tmp  # proj_dir is NOT a git repo
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=_FAIL_GATE)):
        result = await _webapp._autopilot_tick_once(ctx)
    assert len(result) == 1
    intent = result[0]
    assert intent["action"] == "fix_failing_tests"
    assert intent["isolatable"] is False
    assert intent["blocked"] == "dirty_tree_no_isolation"


@pytest.mark.asyncio
async def test_tick_blocks_execution_on_dirty_git_tree(tick_tmp):
    """Dirty git tree + failing tests → P1 intent blocked (cannot cleanly isolate)."""
    data_dir, proj_dir = tick_tmp
    _git_init_commit(proj_dir)
    (proj_dir / "dirty.txt").write_text("uncommitted\n")
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=_FAIL_GATE)):
        result = await _webapp._autopilot_tick_once(ctx)
    assert result[0]["isolatable"] is False
    assert result[0]["blocked"] == "dirty_tree_no_isolation"


@pytest.mark.asyncio
async def test_tick_allows_execution_on_clean_git_tree(tick_tmp):
    """Clean git tree + failing tests → P1 intent isolatable, NOT blocked."""
    data_dir, proj_dir = tick_tmp
    _git_init_commit(proj_dir)
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=_FAIL_GATE)):
        result = await _webapp._autopilot_tick_once(ctx)
    intent = result[0]
    assert intent["action"] == "fix_failing_tests"
    assert intent["isolatable"] is True
    assert "blocked" not in intent


@pytest.mark.asyncio
async def test_tick_non_execution_intent_is_not_isolation_checked(tick_tmp):
    """A P4 scout (passing tests, no backlog) is NOT execution-class → no isolatable/blocked keys."""
    data_dir, proj_dir = tick_tmp  # non-git, but irrelevant for a non-execution intent
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)
    passing_gate = {"verdict": "safe", "tests": {"cmd": "pytest", "output": "ok",
                    "detected": True, "ok": True, "exit_code": 0, "timed_out": False}, "lint": None}
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=passing_gate)):
        result = await _webapp._autopilot_tick_once(ctx)
    intent = result[0]
    assert intent["action"] == "scout"
    assert "isolatable" not in intent
    assert "blocked" not in intent


@pytest.mark.asyncio
async def test_tick_once_active_failing_appends_to_trajectory(tick_tmp):
    """Intent is written to the trajectory log."""
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)

    fake_gate = {"verdict": "risky", "tests": {"cmd": "pytest", "output": "fail", "detected": True, "ok": False, "exit_code": 1, "timed_out": False}, "lint": None}

    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)):
        await _webapp._autopilot_tick_once(ctx)

    records = autopilot.read_trajectory(data_dir)
    assert len(records) == 1
    assert records[0]["action"] == "fix_failing_tests"
    assert records[0]["shadow"] is True


@pytest.mark.asyncio
async def test_tick_once_run_engine_not_present_in_ctx(tick_tmp):
    """run_engine is None in ctx — the tick must not call it and not crash."""
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)
    ctx["run_engine"] = None  # explicitly None

    fake_gate = {"verdict": "safe", "tests": {"cmd": "pytest", "output": "passed", "detected": True, "ok": True, "exit_code": 0, "timed_out": False}, "lint": None}

    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)):
        result = await _webapp._autopilot_tick_once(ctx)

    # Should return an intent (scout or no-backlog) without crashing
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_tick_once_mode_off_skips_project(tick_tmp):
    """A project with mode='off' is skipped — no intent logged."""
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="off")
    _set_state(data_dir, enabled=True)

    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(side_effect=AssertionError("should not be called"))):
        result = await _webapp._autopilot_tick_once(ctx)

    assert result == []


@pytest.mark.asyncio
async def test_tick_once_free_chat_skipped(tick_tmp):
    """Free chat projects (is_free=True) are skipped by the tick.

    We patch _collect_projects to return only a free-chat project so
    the is_free guard is exercised without needing real free-chat storage.
    """
    data_dir, proj_dir = tick_tmp
    ctx = _make_tick_ctx(data_dir, proj_dir, enabled=True, mode="propose")
    _set_state(data_dir, enabled=True)

    fake_free_project = {
        "id": "free-abc",
        "name": "Free Chat",
        "cwd": str(proj_dir),
        "is_free": True,
        "autopilot": "propose",
        "type": "software",
    }

    with patch.object(_webapp, "_collect_projects", return_value=[fake_free_project]), \
         patch.object(_webapp, "_run_quality_gate", new=AsyncMock(side_effect=AssertionError("should not be called"))):
        result = await _webapp._autopilot_tick_once(ctx)

    assert result == []


# ═══════════════════════════════════════════════════════════════════
# Endpoint tests — POST /api/autopilot/tick + GET /api/autopilot/decisions
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def shadow_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    password = "testpw"
    ctx = {
        "DATA": data_dir,
        "topics": {
            "800:1": {
                "project": "shadow-ep-proj",
                "cwd": str(proj_dir),
                "model": "sonnet",
                "autopilot": "propose",
                "type": "software",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def shadow_app(shadow_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = shadow_ctx

    app.router.add_post("/api/autopilot/tick", _webapp.api_autopilot_tick)
    app.router.add_get("/api/autopilot/decisions", _webapp.api_autopilot_decisions)
    app.router.add_post("/api/login", _webapp.api_login)

    return app


def _auth_h(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ── POST /api/autopilot/tick ──────────────────────────────────────


async def test_tick_endpoint_returns_ran_true(aiohttp_client, shadow_app, shadow_ctx):
    fake_gate = {"verdict": "unknown", "tests": {"detected": False, "ok": False, "cmd": None, "exit_code": None, "output": "", "timed_out": False}, "lint": None}
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)):
        client = await aiohttp_client(shadow_app)
        resp = await client.post("/api/autopilot/tick", headers=_auth_h(shadow_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["ran"] is True
    assert "active" in data
    assert "decisions" in data
    assert isinstance(data["decisions"], list)


async def test_tick_endpoint_active_false_when_not_enabled(aiohttp_client, shadow_app, shadow_ctx):
    # global_enabled is False by default (no state file)
    fake_gate = {"verdict": "unknown", "tests": {"detected": False, "ok": False, "cmd": None, "exit_code": None, "output": "", "timed_out": False}, "lint": None}
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)):
        client = await aiohttp_client(shadow_app)
        resp = await client.post("/api/autopilot/tick", headers=_auth_h(shadow_ctx))
    data = await resp.json()
    assert data["active"] is False
    assert data["decisions"] == []


async def test_tick_endpoint_decisions_populated_when_active(aiohttp_client, shadow_app, shadow_ctx):
    # Enable globally
    _set_state(shadow_ctx["DATA"], enabled=True)
    fake_gate = {"verdict": "risky", "tests": {"cmd": "pytest", "output": "fail", "detected": True, "ok": False, "exit_code": 1, "timed_out": False}, "lint": None}
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)):
        client = await aiohttp_client(shadow_app)
        resp = await client.post("/api/autopilot/tick", headers=_auth_h(shadow_ctx))
    data = await resp.json()
    assert data["active"] is True
    assert len(data["decisions"]) == 1
    assert data["decisions"][0]["action"] == "fix_failing_tests"


async def test_tick_endpoint_requires_auth(aiohttp_client, shadow_app):
    client = await aiohttp_client(shadow_app)
    resp = await client.post("/api/autopilot/tick")
    assert resp.status in (401, 403)


# ── GET /api/autopilot/decisions ──────────────────────────────────


async def test_decisions_endpoint_empty_when_no_trajectory(aiohttp_client, shadow_app, shadow_ctx):
    client = await aiohttp_client(shadow_app)
    resp = await client.get("/api/autopilot/decisions", headers=_auth_h(shadow_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["decisions"] == []


async def test_decisions_endpoint_returns_most_recent_first(aiohttp_client, shadow_app, shadow_ctx):
    """After two ticks the decisions endpoint returns newest first."""
    _set_state(shadow_ctx["DATA"], enabled=True)
    fake_gate_fail = {"verdict": "risky", "tests": {"cmd": "pytest", "output": "fail", "detected": True, "ok": False, "exit_code": 1, "timed_out": False}, "lint": None}
    fake_gate_pass = {"verdict": "safe", "tests": {"cmd": "pytest", "output": "pass", "detected": True, "ok": True, "exit_code": 0, "timed_out": False}, "lint": None}

    # Tick 1: failing
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate_fail)):
        await _webapp._autopilot_tick_once(shadow_ctx)
    # Tick 2: passing, no backlog
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate_pass)):
        await _webapp._autopilot_tick_once(shadow_ctx)

    client = await aiohttp_client(shadow_app)
    resp = await client.get("/api/autopilot/decisions", headers=_auth_h(shadow_ctx))
    data = await resp.json()
    assert len(data["decisions"]) == 2
    # Most recent (scout/P4) should be first
    assert data["decisions"][0]["action"] in ("scout", "none")
    assert data["decisions"][1]["action"] == "fix_failing_tests"


async def test_decisions_endpoint_limit_param(aiohttp_client, shadow_app, shadow_ctx):
    """?limit=1 returns at most 1 record."""
    _set_state(shadow_ctx["DATA"], enabled=True)
    fake_gate = {"verdict": "risky", "tests": {"cmd": "pytest", "output": "fail", "detected": True, "ok": False, "exit_code": 1, "timed_out": False}, "lint": None}
    with patch.object(_webapp, "_run_quality_gate", new=AsyncMock(return_value=fake_gate)):
        await _webapp._autopilot_tick_once(shadow_ctx)
        await _webapp._autopilot_tick_once(shadow_ctx)

    client = await aiohttp_client(shadow_app)
    resp = await client.get("/api/autopilot/decisions?limit=1", headers=_auth_h(shadow_ctx))
    data = await resp.json()
    assert len(data["decisions"]) == 1


async def test_decisions_endpoint_requires_auth(aiohttp_client, shadow_app):
    client = await aiohttp_client(shadow_app)
    resp = await client.get("/api/autopilot/decisions")
    assert resp.status in (401, 403)


# ═══════════════════════════════════════════════════════════════════
# Unit tests — _autopilot_test_signal (configured test_cmd vs fallback)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_test_signal_configured_failing(tmp_path):
    """A configured, allowlisted test_cmd that FAILS → (True, 'failed…')."""
    (tmp_path / "test_x.py").write_text("def test_fail():\n    assert 1 == 2\n")
    proj = {"cwd": str(tmp_path), "test_cmd": "python3 -m pytest -q test_x.py"}
    failing, summary = await _webapp._autopilot_test_signal(proj)
    assert failing is True
    assert "failed" in summary


@pytest.mark.asyncio
async def test_test_signal_configured_passing(tmp_path):
    """A configured, allowlisted test_cmd that PASSES → (False, 'passed…')."""
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert 1 == 1\n")
    proj = {"cwd": str(tmp_path), "test_cmd": "python3 -m pytest -q test_x.py"}
    failing, summary = await _webapp._autopilot_test_signal(proj)
    assert failing is False
    assert "passed" in summary


@pytest.mark.asyncio
async def test_test_signal_not_allowlisted_is_none():
    """A non-allowlisted test_cmd is refused (None) — never executed."""
    proj = {"cwd": "/tmp", "test_cmd": "rm -rf /"}
    failing, summary = await _webapp._autopilot_test_signal(proj)
    assert failing is None
    assert "allowlist" in summary

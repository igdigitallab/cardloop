"""
tests/test_autopilot_director.py — Autopilot DIRECTOR v1 (plan-only).

PLAN-ONLY INVARIANT: the director MUST NOT call _start_card_run, _drain_queue,
_queue_enqueue, or any worker/card execution path.  Every test that exercises
_run_director patches those functions to raise, asserting they are never reached.

Coverage:
  Unit tests (autopilot.py pure helpers):
    - director_model() returns "sonnet" by default; reads env override
    - read_notebook() returns "" when file missing; returns content after write
    - append_notebook() creates dir + file; appends subsequent entries with ISO timestamps
    - build_director_input() produces ## Board / ## Test signal / ## Your notebook sections

  Integration tests (_run_director):
    - Guard: master autopilot inactive → ok=False, reason=autopilot_inactive
    - Guard: project mode=off → ok=False, reason=project_not_enabled
    - Guard: rate_limit headroom exhausted → ok=False, reason=rate_limit_headroom
    - Guard: already busy → ok=False, reason=busy
    - Happy path: cards created in backlog, notebook written, trajectory appended
    - Dedup: second run with same card titles → cards_created=0 (no duplicates)
    - execution guard: _start_card_run / _drain_queue patched to raise — never called

  Endpoint tests:
    - POST /api/autopilot/director/{id} 404 on unknown project
    - POST /api/autopilot/director/{id} returns mocked _run_director result
    - Endpoint requires auth (401 without cookie)
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from features.autopilot import logic as autopilot
import webapp as _webapp
from webapp import _derive_token
from features.autopilot import director as _ap_director
from features.autopilot import routes as _ap_routes


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_valid_structured() -> dict:
    """A valid director structured_output dict."""
    return {
        "assessment": "Tests are green; backlog has one card worth advancing.",
        "priority": "P3",
        "focus": "Advance top backlog card",
        "proposed_cards": [
            {"title": "Add retry logic to sync worker", "why": "Reduces flaky failures"},
            {"title": "Write integration test for retry", "why": "Locks in the behaviour"},
        ],
        "question_for_operator": "Should the retry cap be 3 or 5?",
        "notebook_note": "Board is healthy; retry work is the clear next step.",
    }


def _make_ctx(tmp_path: Path, *, enabled: bool = True, mode: str = "propose",
              rate_limits: dict | None = None) -> dict:
    """Minimal ctx for _run_director tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir(exist_ok=True)
    password = "testpw"
    ctx = {
        "DATA": data_dir,
        "topics": {
            "100:1": {
                "project": "test-project",
                "cwd": str(proj_dir),
                "model": "sonnet",
                "autopilot": mode,
                "type": "software",
                "session_key": "100:1",
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
        "run_engine": None,  # overridden per-test
        "ptb_app": None,
        "rate_limits": rate_limits if rate_limits is not None else {},
        "_auth_token": _derive_token(password),
    }
    # Set global_enabled in state
    state = autopilot.load_state(data_dir)
    state["global_enabled"] = enabled
    autopilot.save_state(data_dir, state)
    return ctx


def _project_from_ctx(ctx: dict) -> dict:
    """Build a project dict mirroring what _find_project_by_id would return."""
    topic = list(ctx["topics"].values())[0]
    return {
        "id": "test-project",
        "name": "Test Project",
        "cwd": topic["cwd"],
        "autopilot": topic["autopilot"],
        "type": topic["type"],
        "session_key": topic["session_key"],
        "test_cmd": None,
    }


def _mock_run_engine_factory(structured: dict):
    """Return a run_engine factory that yields one result event with structured_output."""
    def _factory(*args, **kwargs):
        async def _gen():
            yield {"type": "result", "structured_output": structured,
                   "session_id": "sess-test", "cost_usd": 0.001,
                   "context_tokens": 1000, "api_error_status": None,
                   "cache_read_tokens": 0, "fresh_tokens": 1000,
                   "prompt_tokens": 900, "cache_hit_pct": 0.0, "duration_ms": 500}
        return _gen()
    return _factory


# ═══════════════════════════════════════════════════════════════════
# Unit tests — autopilot.py director helpers
# ═══════════════════════════════════════════════════════════════════


# ─────────────────────────── director_model ───────────────────────────

def test_director_model_default():
    env_backup = os.environ.pop("AUTOPILOT_DIRECTOR_MODEL", None)
    try:
        assert autopilot.director_model() == "sonnet"
    finally:
        if env_backup is not None:
            os.environ["AUTOPILOT_DIRECTOR_MODEL"] = env_backup


def test_director_model_env_override(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DIRECTOR_MODEL", "opus")
    assert autopilot.director_model() == "opus"


def test_director_model_never_hardcodes_fable(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_DIRECTOR_MODEL", raising=False)
    assert autopilot.director_model() != "fable"


# ─────────────────────────── read_notebook ───────────────────────────

def test_read_notebook_missing_returns_empty(tmp_path):
    assert autopilot.read_notebook(tmp_path, "no-such-project") == ""


def test_read_notebook_returns_content(tmp_path):
    nb_dir = tmp_path / "autopilot"
    nb_dir.mkdir()
    (nb_dir / "my-proj-notebook.md").write_text("hello world", encoding="utf-8")
    result = autopilot.read_notebook(tmp_path, "my-proj")
    assert result == "hello world"


def test_read_notebook_roundtrip_via_append(tmp_path):
    autopilot.append_notebook(tmp_path, "proj-x", "first note", time.time())
    result = autopilot.read_notebook(tmp_path, "proj-x")
    assert "first note" in result


# ─────────────────────────── append_notebook ───────────────────────────

def test_append_notebook_creates_dir(tmp_path):
    assert not (tmp_path / "autopilot").exists()
    autopilot.append_notebook(tmp_path, "proj-a", "note text", 1_700_000_000.0)
    assert (tmp_path / "autopilot" / "proj-a-notebook.md").exists()


def test_append_notebook_contains_iso_timestamp(tmp_path):
    ts = 1_700_000_000.0  # 2023-11-14T22:13:20Z
    autopilot.append_notebook(tmp_path, "proj-b", "ts test", ts)
    content = (tmp_path / "autopilot" / "proj-b-notebook.md").read_text()
    assert "2023-11-14T22:13:20Z" in content


def test_append_notebook_multiple_entries(tmp_path):
    t = time.time()
    autopilot.append_notebook(tmp_path, "proj-c", "note one", t)
    autopilot.append_notebook(tmp_path, "proj-c", "note two", t + 1)
    content = (tmp_path / "autopilot" / "proj-c-notebook.md").read_text()
    assert "note one" in content
    assert "note two" in content


def test_append_notebook_different_projects_isolated(tmp_path):
    t = time.time()
    autopilot.append_notebook(tmp_path, "proj-d", "alpha", t)
    autopilot.append_notebook(tmp_path, "proj-e", "beta", t)
    d_content = (tmp_path / "autopilot" / "proj-d-notebook.md").read_text()
    e_content = (tmp_path / "autopilot" / "proj-e-notebook.md").read_text()
    assert "alpha" in d_content and "beta" not in d_content
    assert "beta" in e_content and "alpha" not in e_content


# ─────────────────────────── build_director_input ───────────────────────────

def test_build_director_input_contains_board_section():
    result = autopilot.build_director_input("MyApp", "backlog: card A", "passing", "")
    assert "## Board" in result
    assert "card A" in result


def test_build_director_input_contains_test_signal_section():
    result = autopilot.build_director_input("MyApp", "empty board", "2 tests failed", "")
    assert "## Test signal" in result
    assert "2 tests failed" in result


def test_build_director_input_contains_notebook_section():
    result = autopilot.build_director_input("MyApp", "b", "t", "my memory")
    assert "## Your notebook" in result
    assert "my memory" in result


def test_build_director_input_empty_notebook_shows_placeholder():
    result = autopilot.build_director_input("MyApp", "b", "t", "")
    assert "## Your notebook" in result
    assert "no notebook entries" in result


def test_build_director_input_whitespace_notebook_shows_placeholder():
    result = autopilot.build_director_input("X", "b", "t", "   \n  ")
    assert "no notebook entries" in result


# ─────────────────────────── DIRECTOR_PROMPT and DIRECTOR_SCHEMA ──────────────

def test_director_prompt_contains_plan_only_invariant():
    assert "PLAN-ONLY" in autopilot.DIRECTOR_PROMPT


def test_director_schema_has_required_fields():
    required = autopilot.DIRECTOR_SCHEMA["required"]
    for field in ("assessment", "priority", "proposed_cards", "question_for_operator", "notebook_note"):
        assert field in required, f"Missing required field: {field}"


def test_director_schema_priority_enum_values():
    enum = autopilot.DIRECTOR_SCHEMA["properties"]["priority"]["enum"]
    assert set(enum) == {"P1", "P3", "P4", "P5"}


# ═══════════════════════════════════════════════════════════════════
# Integration tests — _run_director
# ═══════════════════════════════════════════════════════════════════


def _forbidden_start_card_run(*a, **kw):
    raise AssertionError("_start_card_run must NEVER be called by the director")


def _forbidden_drain_queue(*a, **kw):
    raise AssertionError("_drain_queue must NEVER be called by the director")


# ── Guard: master autopilot inactive ────────────────────────────────

@pytest.mark.asyncio
async def test_run_director_guard_master_inactive(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=False)
    project = _project_from_ctx(ctx)
    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
    ):
        result = await _ap_director._run_director(ctx, project)
    assert result["ok"] is False
    assert result["reason"] == "autopilot_inactive"


# ── Guard: project mode off ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_director_guard_project_off(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="off")
    project = _project_from_ctx(ctx)
    project["autopilot"] = "off"
    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
    ):
        result = await _ap_director._run_director(ctx, project)
    assert result["ok"] is False
    assert result["reason"] == "project_not_enabled"


# ── Guard: rate-limit headroom ───────────────────────────────────────

@pytest.mark.asyncio
async def test_run_director_guard_rate_limit(tmp_path):
    # Simulate all headroom consumed (utilization 1.0 — no reserve left)
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose",
                    rate_limits={"token": {"utilization": 1.0}})
    project = _project_from_ctx(ctx)
    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
    ):
        result = await _ap_director._run_director(ctx, project)
    assert result["ok"] is False
    assert result["reason"] == "rate_limit_headroom"


# ── Guard: already busy ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_director_guard_busy(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)
    # Pre-occupy the director session key
    director_key = f"director:{project['session_key']}"
    ctx["running"][director_key] = True
    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
    ):
        result = await _ap_director._run_director(ctx, project)
    assert result["ok"] is False
    assert result["reason"] == "busy"
    # Make sure the pre-occupied key is untouched
    assert ctx["running"][director_key] is True


# ── Happy path: structured plan applied ─────────────────────────────

@pytest.mark.asyncio
async def test_run_director_happy_path_creates_cards(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)
    structured = _make_valid_structured()

    async def mock_run_engine(*args, **kwargs):
        yield {"type": "result", "structured_output": structured}

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "tests passed"))),
    ):
        result = await _ap_director._run_director(ctx, project)

    assert result["ok"] is True
    assert result["cards_created"] == 2  # two proposed cards
    assert result["priority"] == "P3"
    assert result["question_for_operator"] == structured["question_for_operator"]


@pytest.mark.asyncio
async def test_run_director_happy_path_cards_in_backlog(tmp_path):
    """Director cards must land in the backlog column."""
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)
    structured = _make_valid_structured()

    async def mock_run_engine(*args, **kwargs):
        yield {"type": "result", "structured_output": structured}

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "tests passed"))),
    ):
        await _ap_director._run_director(ctx, project)

    from board import _load_board
    _, _, cols = _load_board(project["cwd"])
    backlog_titles = [c["text"] for c in cols.get("backlog", [])]
    assert any("retry" in t.lower() for t in backlog_titles)


@pytest.mark.asyncio
async def test_run_director_happy_path_notebook_written(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)
    structured = _make_valid_structured()

    async def mock_run_engine(*args, **kwargs):
        yield {"type": "result", "structured_output": structured}

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "tests passed"))),
    ):
        await _ap_director._run_director(ctx, project)

    content = autopilot.read_notebook(ctx["DATA"], project["id"])
    assert structured["notebook_note"] in content


@pytest.mark.asyncio
async def test_run_director_happy_path_trajectory_appended(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)
    structured = _make_valid_structured()

    async def mock_run_engine(*args, **kwargs):
        yield {"type": "result", "structured_output": structured}

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "tests passed"))),
    ):
        await _ap_director._run_director(ctx, project)

    records = autopilot.read_trajectory(ctx["DATA"])
    director_records = [r for r in records if r.get("action") == "director_plan"]
    assert len(director_records) == 1
    assert director_records[0]["priority"] == "P3"
    assert director_records[0]["shadow"] is True


@pytest.mark.asyncio
async def test_run_director_running_lock_released_on_success(tmp_path):
    """The director session key must be popped from running after a successful run."""
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)

    async def mock_run_engine(*args, **kwargs):
        yield {"type": "result", "structured_output": _make_valid_structured()}

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "ok"))),
    ):
        await _ap_director._run_director(ctx, project)

    director_key = f"director:{project['session_key']}"
    assert director_key not in ctx["running"]


# ── Dedup: second run produces no duplicate cards ────────────────────

@pytest.mark.asyncio
async def test_run_director_dedup_no_duplicate_cards(tmp_path):
    """Running the director twice with the same proposed card titles must not create duplicates."""
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)
    structured = _make_valid_structured()

    # mock_run_engine must be a factory — called twice, yields once each
    def mock_run_engine(*args, **kwargs):
        async def _gen():
            yield {"type": "result", "structured_output": structured}
        return _gen()

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "ok"))) as _p,
    ):
        r1 = await _ap_director._run_director(ctx, project)
    assert r1["cards_created"] == 2

    ctx["run_engine"] = mock_run_engine  # factory is reusable
    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "ok"))) as _p2,
    ):
        r2 = await _ap_director._run_director(ctx, project)
    assert r2["cards_created"] == 0  # all titles already exist


# ── Mode=auto also passes the project guard ──────────────────────────

@pytest.mark.asyncio
async def test_run_director_mode_auto_allowed(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="auto")
    project = _project_from_ctx(ctx)
    project["autopilot"] = "auto"
    structured = _make_valid_structured()

    async def mock_run_engine(*args, **kwargs):
        yield {"type": "result", "structured_output": structured}

    ctx["run_engine"] = mock_run_engine

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(None, "unknown"))),
    ):
        result = await _ap_director._run_director(ctx, project)

    assert result["ok"] is True


# ── run_engine error propagates and releases lock ────────────────────

@pytest.mark.asyncio
async def test_run_director_engine_error_releases_lock(tmp_path):
    ctx = _make_ctx(tmp_path, enabled=True, mode="propose")
    project = _project_from_ctx(ctx)

    async def mock_run_engine_error(*args, **kwargs):
        yield {"type": "error", "exc": RuntimeError("SDK exploded")}

    ctx["run_engine"] = mock_run_engine_error

    with (
        patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run),
        patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue),
        patch.object(_ap_director, "_autopilot_test_signal", new=AsyncMock(return_value=(False, "ok"))),
    ):
        result = await _ap_director._run_director(ctx, project)

    assert result["ok"] is False
    director_key = f"director:{project['session_key']}"
    assert director_key not in ctx["running"]


# ═══════════════════════════════════════════════════════════════════
# Endpoint tests — POST /api/autopilot/director/{id}
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def director_ctx(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # cwd basename = project id; use "ep-proj" as the dir name so _project_id returns "ep-proj"
    proj_dir = tmp_path / "ep-proj"
    proj_dir.mkdir()
    password = "testpw"
    ctx = {
        "DATA": data_dir,
        "topics": {
            "700:1": {
                "project": "EP Project",
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
def director_app(director_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = director_ctx

    app.router.add_post("/api/autopilot/director/{id}", _ap_routes.api_autopilot_director)
    app.router.add_post("/api/login", _webapp.api_login)

    return app


def _auth_h(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ── 404 on unknown project ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_director_endpoint_404_unknown(aiohttp_client, director_app, director_ctx):
    client = await aiohttp_client(director_app)
    resp = await client.post("/api/autopilot/director/no-such-project",
                             headers=_auth_h(director_ctx))
    assert resp.status == 404


# ── Requires auth ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_director_endpoint_requires_auth(aiohttp_client, director_app):
    client = await aiohttp_client(director_app)
    resp = await client.post("/api/autopilot/director/ep-proj")
    assert resp.status in (401, 403)


# ── Mocked _run_director returns expected payload ────────────────────

@pytest.mark.asyncio
async def test_director_endpoint_returns_run_director_result(aiohttp_client, director_app, director_ctx):
    fake_result = {
        "ok": True,
        "assessment": "All good.",
        "priority": "P4",
        "focus": "Scout improvements",
        "proposed_cards": [{"title": "Fix thing", "why": "because"}],
        "question_for_operator": None,
        "notebook_note": "Noted.",
        "cards_created": 1,
    }
    with patch.object(_ap_routes, "_run_director", new=AsyncMock(return_value=fake_result)):
        client = await aiohttp_client(director_app)
        resp = await client.post("/api/autopilot/director/ep-proj",
                                 headers=_auth_h(director_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["priority"] == "P4"
    assert data["cards_created"] == 1


@pytest.mark.asyncio
async def test_director_endpoint_propagates_error_reason(aiohttp_client, director_app, director_ctx):
    fake_result = {"ok": False, "reason": "autopilot_inactive"}
    with patch.object(_ap_routes, "_run_director", new=AsyncMock(return_value=fake_result)):
        client = await aiohttp_client(director_app)
        resp = await client.post("/api/autopilot/director/ep-proj",
                                 headers=_auth_h(director_ctx))
    assert resp.status == 400
    data = await resp.json()
    assert data["reason"] == "autopilot_inactive"


# ═══════════════════════════════════════════════════════════════════
# Hard read-only guarantee — the director is engine-blocked from mutation
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_director_is_engine_blocked_from_mutating_tools(tmp_path):
    """PLAN-ONLY is a HARD guard, not a prompt hope: _run_director must call
    run_engine with disallowed_tools_extra blocking Write/Edit/Bash etc."""
    captured: dict = {}

    def _capture_factory(*args, **kwargs):
        captured.update(kwargs)

        async def _gen():
            yield {"type": "result",
                   "structured_output": {
                       "assessment": "a", "priority": "P1", "focus": "f",
                       "proposed_cards": [], "question_for_operator": None,
                       "notebook_note": "n"},
                   "session_id": "s", "cost_usd": 0.0, "context_tokens": 1,
                   "cache_read_tokens": 0, "fresh_tokens": 1, "prompt_tokens": 1,
                   "cache_hit_pct": 0.0, "duration_ms": 1, "api_error_status": None}
        return _gen()

    ctx = _make_ctx(tmp_path)
    ctx["run_engine"] = _capture_factory
    project = _project_from_ctx(ctx)
    with patch.object(_webapp, "_start_card_run", new=_forbidden_start_card_run), \
         patch.object(_webapp, "_drain_queue", new=_forbidden_drain_queue), \
         patch.object(_ap_director, "_autopilot_test_signal",
                      new=AsyncMock(return_value=(True, "tests failed"))):
        await _ap_director._run_director(ctx, project)

    extra = captured.get("disallowed_tools_extra") or []
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        assert tool in extra, f"director must be blocked from {tool}"

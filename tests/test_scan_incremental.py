"""
Spec-012 Phase 0 — tests for the incremental scanner (Tasks A, B, C).

Task A: high-water-mark fingerprint
  - Second scan of the same output → 0 new errors (fingerprint works)
  - Adding a new error line → exactly one new error
  - First scan: only the last 50 lines are parsed, fingerprint is saved

Task B: dismissed-incidents TTL
  - _dismissed_add(h) → _ingest_errors_to_board does not create a card for h (within TTL)
  - After TTL expiry (injected old ts) card is created again
  - Deleting an err-card (DELETE) → hash written to dismissed
  - Moving an err-card to done (PATCH move to="done") → hash written to dismissed

Task C: scan interval
  - _SCAN_INTERVAL_SEC default = 60 (not 300)

Corrupt/missing state: helpers return {} and do not crash the scanner
"""
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp
from webapp import (
    _dismissed_add,
    _dismissed_is_active,
    _dismissed_load,
    _dismissed_save,
    _hash6,
    _ingest_errors_to_board,
    _load_board,
    _norm_msg,
    _SCAN_INTERVAL_SEC,
    _scan_state_init,
    _scan_state_load,
    _scan_state_save,
    _tasks_path,
    _derive_token,
)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _setup_state_paths(tmp_path: Path):
    """Initializes _SCAN_STATE_PATH and _DISMISSED_PATH to the real tmp_path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    ctx = {
        "DATA": data_dir,
        # other fields not required by _scan_state_init
    }
    _scan_state_init(ctx)
    return data_dir


def _make_empty_board(cwd: Path, name: str = "testproj") -> None:
    _tasks_path(str(cwd)).write_text(
        f"# Tasks — {name}\n\n## Backlog\n\n## In Progress\n\n## Review\n\n## Failed\n",
        encoding="utf-8",
    )


def _make_error(msg: str, etype: str = "TestError", source: str = "log") -> dict:
    h = _hash6(_norm_msg(f"{etype}: {msg}"))
    return {
        "hash": h,
        "type": etype,
        "message": msg,
        "source": source,
        "excerpt": f"{etype}: {msg}",
    }


def _fp(line: str) -> str:
    """Fingerprint of a line — sha1."""
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()


# ─────────────────────────────────────────────────────────────────
# Task C: scan interval default
# ─────────────────────────────────────────────────────────────────


def test_scan_interval_default_is_60():
    """_SCAN_INTERVAL_SEC default = 60 (lowered in Spec-012 Phase 0). Unconditional: if
    env-override is set — value must match it, otherwise — default 60."""
    import os
    override = os.environ.get("ERROR_SCAN_INTERVAL")
    expected = int(override) if override else 60
    assert _SCAN_INTERVAL_SEC == expected, (
        f"Expected {expected}, got {_SCAN_INTERVAL_SEC}. Spec-012 Phase 0: default=60 (was 300)."
    )


# ─────────────────────────────────────────────────────────────────
# Task A helpers: _scan_state_load / _scan_state_save
# ─────────────────────────────────────────────────────────────────


def test_scan_state_load_missing_file(tmp_path):
    """File is missing → return {}."""
    _setup_state_paths(tmp_path)
    result = _scan_state_load()
    assert result == {}


def test_scan_state_load_corrupt_file(tmp_path):
    """Corrupt JSON → return {} (no crash)."""
    data_dir = _setup_state_paths(tmp_path)
    (data_dir / "scan_state.json").write_text("NOT JSON{{", encoding="utf-8")
    result = _scan_state_load()
    assert result == {}


def test_scan_state_save_and_load_round_trip(tmp_path):
    """Save → load → same dict."""
    _setup_state_paths(tmp_path)
    state = {"/home/proj": {"last_line": "abc123", "last_scan_ts": 1234567890.0}}
    _scan_state_save(state)
    loaded = _scan_state_load()
    assert loaded == state


def test_scan_state_save_with_none_path_no_crash():
    """_scan_state_save(state) when _SCAN_STATE_PATH=None → silent skip."""
    orig = webapp._SCAN_STATE_PATH
    try:
        webapp._SCAN_STATE_PATH = None
        _scan_state_save({"key": "val"})  # must not raise
    finally:
        webapp._SCAN_STATE_PATH = orig


def test_scan_state_load_with_none_path_returns_empty():
    """_scan_state_load() when _SCAN_STATE_PATH=None → {}."""
    orig = webapp._SCAN_STATE_PATH
    try:
        webapp._SCAN_STATE_PATH = None
        assert _scan_state_load() == {}
    finally:
        webapp._SCAN_STATE_PATH = orig


# ─────────────────────────────────────────────────────────────────
# Task B helpers: _dismissed_load / _dismissed_save / _dismissed_add / _dismissed_is_active
# ─────────────────────────────────────────────────────────────────


def test_dismissed_load_missing_file(tmp_path):
    """Missing dismissed file → {}."""
    _setup_state_paths(tmp_path)
    assert _dismissed_load() == {}


def test_dismissed_load_corrupt_file(tmp_path):
    """Corrupt JSON → {} (no crash)."""
    data_dir = _setup_state_paths(tmp_path)
    (data_dir / "dismissed_incidents.json").write_text("BADJSON", encoding="utf-8")
    assert _dismissed_load() == {}


def test_dismissed_add_and_is_active(tmp_path):
    """After _dismissed_add(h) → _dismissed_is_active(h, now) = True."""
    _setup_state_paths(tmp_path)
    h = "abc123"
    _dismissed_add(h)
    assert _dismissed_is_active(h, time.time()) is True


def test_dismissed_is_active_returns_false_after_ttl(tmp_path):
    """Entry with ts far in the past (>TTL) → _dismissed_is_active = False."""
    data_dir = _setup_state_paths(tmp_path)
    h = "deadbeef"
    old_ts = time.time() - webapp._DISMISS_TTL - 1  # beyond TTL
    (data_dir / "dismissed_incidents.json").write_text(
        json.dumps({h: old_ts}), encoding="utf-8"
    )
    assert _dismissed_is_active(h, time.time()) is False


def test_dismissed_is_active_unknown_hash(tmp_path):
    """Unknown hash → False."""
    _setup_state_paths(tmp_path)
    assert _dismissed_is_active("nothash", time.time()) is False


def test_dismissed_add_prunes_old_entries(tmp_path):
    """_dismissed_add prunes entries older than TTL."""
    data_dir = _setup_state_paths(tmp_path)
    h_old = "oldentry"
    h_new = "newentry"
    old_ts = time.time() - webapp._DISMISS_TTL - 100
    (data_dir / "dismissed_incidents.json").write_text(
        json.dumps({h_old: old_ts}), encoding="utf-8"
    )
    _dismissed_add(h_new)
    data = _dismissed_load()
    assert h_old not in data, "Old entries must be pruned by _dismissed_add"
    assert h_new in data


def test_dismissed_save_with_none_path_no_crash():
    """_dismissed_save when _DISMISSED_PATH=None → silent skip."""
    orig = webapp._DISMISSED_PATH
    try:
        webapp._DISMISSED_PATH = None
        _dismissed_save({"k": 1.0})  # must not raise
    finally:
        webapp._DISMISSED_PATH = orig


def test_dismissed_add_with_none_path_no_crash():
    """_dismissed_add when _DISMISSED_PATH=None → silent skip."""
    orig = webapp._DISMISSED_PATH
    try:
        webapp._DISMISSED_PATH = None
        _dismissed_add("abc")  # must not raise
    finally:
        webapp._DISMISSED_PATH = orig


# ─────────────────────────────────────────────────────────────────
# Task A: fingerprint in _scan_project_errors
# ─────────────────────────────────────────────────────────────────


async def test_fingerprint_second_scan_same_output_yields_no_new_errors(tmp_path):
    """Second scan of the same lines → 0 new errors (fingerprint works)."""
    _setup_state_paths(tmp_path)

    log_lines = [
        "INFO server started",
        "ERROR: database connection lost",
        "Traceback (most recent call last):",
        "  File 'app.py', line 1",
        "KeyError: 'missing'",
    ]
    log_text = "\n".join(log_lines)

    project = {"cwd": str(tmp_path / "proj"), "log_cmd": "dummy_cmd"}

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        # First scan — establishes fingerprint
        errors1 = await webapp._scan_project_errors(project)

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        # Second scan of the same lines — fingerprint found, nothing after it
        errors2 = await webapp._scan_project_errors(project)

    assert errors2 == [], (
        f"Second scan of same lines must yield 0 errors, got {len(errors2)}"
    )


async def test_fingerprint_repeated_line_does_not_skip_new_error(tmp_path):
    """Regression BLOCKER (block-fingerprint): a repeated line (heartbeat) at
    the end of the previous scan must NOT hide a new error that appeared BETWEEN
    two of its copies. Single-line fingerprint took the last occurrence → lost the error."""
    _setup_state_paths(tmp_path)
    project = {"cwd": str(tmp_path / "hb"), "log_cmd": "dummy"}

    scan1 = ["INFO a", "INFO b", "INFO c", "INFO d", "INFO e", "heartbeat ping"]
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(scan1))):
        await webapp._scan_project_errors(project)

    # New error appeared, then the same heartbeat line again
    scan2 = scan1 + ["ERROR: disk full", "heartbeat ping"]
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(scan2))):
        errors = await webapp._scan_project_errors(project)

    msgs = [e.get("message", "") for e in errors]
    assert any("disk full" in m for m in msgs), (
        f"New error between two heartbeats must NOT be lost. Errors: {errors}"
    )


async def test_delete_then_rescan_does_not_resurrect_e2e(tmp_path):
    """E2E identity: ingest creates card err-<h> → take hash FROM card id
    (as api_delete_task does: card_id[4:]) → dismiss → re-ingest of the same
    error does NOT resurrect it. Proves card_id[4:] == err['hash']."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "e2e"
    cwd.mkdir()
    _make_empty_board(cwd)
    err = _make_error("kaboom", etype="ValueError")

    added, _ = await _ingest_errors_to_board(str(cwd), "e2e", [err])
    assert added == 1

    # Take the created card's id and extract hash using the same slice as the delete route
    _, _, cols = _load_board(str(cwd))
    card_id = cols["failed"][0]["id"]
    assert card_id.startswith("err-")
    _dismissed_add(card_id[4:])           # same as api_delete_task on an err-card

    # Remove card from board (like a delete), then re-ingest the same error
    cols["failed"].clear()
    webapp._save_board(str(cwd), "e2e", "", cols)
    added2, _ = await _ingest_errors_to_board(str(cwd), "e2e", [err])
    assert added2 == 0, "dismissed incident must not resurrect (card_id[4:] == hash)"


# ─────────────────────────────────────────────────────────────────
# Spec-012 Phase 1: in-process push of cockpit errors (_report_incident)
# ─────────────────────────────────────────────────────────────────


async def test_report_incident_creates_card_and_dedups_with_scanner(tmp_path, monkeypatch):
    """Phase 1: _report_incident creates a card immediately; its hash MATCHES what
    the log scanner produces for the line `UNHANDLED exc_class=.. path=..` →
    dedup (scanner does not duplicate, only bumps seen)."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "cops"
    cwd.mkdir()
    _make_empty_board(cwd)
    fake_proj = {"cwd": str(cwd), "name": "claude-ops-bot"}
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda *a, **k: fake_proj)
    webapp._REPORT_DEBOUNCE.clear()

    # In-process report (as from error_middleware)
    await webapp._report_incident({}, "ValueError", "/api/x")
    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 1, "card must be created immediately"
    card_id = cols["failed"][0]["id"]

    # Scanner parses THE SAME UNHANDLED line → same hash → dedup
    scanner_errs = webapp._parse_log_errors(
        "2026-06-04 ERROR root UNHANDLED exc_class=ValueError path=/api/x request_id=ab12",
        source="log",
    )
    assert len(scanner_errs) == 1, f"expected 1 UNHANDLED error, got {scanner_errs}"
    assert f"err-{scanner_errs[0]['hash']}" == card_id, "hash must match the scanner"

    added, updated = await _ingest_errors_to_board(str(cwd), "claude-ops-bot", scanner_errs)
    assert added == 0 and updated == 1, "scanner does not duplicate — only bump seen"


async def test_report_incident_no_project_is_silent(tmp_path, monkeypatch):
    """If the project cannot be resolved — silent, no exception."""
    _setup_state_paths(tmp_path)
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda *a, **k: None)
    webapp._REPORT_DEBOUNCE.clear()
    await webapp._report_incident({}, "ValueError", "/api/x")  # must not raise


async def test_report_incident_debounce_collapses_flood(tmp_path, monkeypatch):
    """Phase 1 hardening: the same incident is not written more than once per debounce
    window in-process → an endpoint that fails on every request does not cause an I/O storm
    in TASKS.md."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "deb"
    cwd.mkdir()
    _make_empty_board(cwd)
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda *a, **k: {"cwd": str(cwd), "name": "deb"})
    webapp._REPORT_DEBOUNCE.clear()

    for _ in range(5):  # 5 rapid reports of the same error
        await webapp._report_incident({}, "FloodError", "/api/flood")

    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 1, "debounce: one card, not 5 entries"


async def test_fingerprint_appended_line_yields_new_error(tmp_path):
    """If a new error line appears after the fingerprint → exactly one new error."""
    _setup_state_paths(tmp_path)

    base_lines = [
        "INFO server started",
        "INFO all good",
        "INFO nothing wrong",
    ]
    new_error_line = "ERROR: out of memory"

    project = {"cwd": str(tmp_path / "proj2"), "log_cmd": "dummy_cmd"}

    # First scan (establishes fingerprint on base_lines)
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(base_lines))):
        await webapp._scan_project_errors(project)

    # Second scan: a new error line is added
    extended_lines = base_lines + [new_error_line]
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(extended_lines))):
        errors2 = await webapp._scan_project_errors(project)

    # Must be EXACTLY one error — only the new line (high-water-mark does not
    # re-parse base_lines).
    assert len(errors2) == 1, f"Expected exactly 1 new error, got {errors2}"
    assert "out of memory" in errors2[0].get("message", ""), (
        f"New error 'out of memory' must be in results: {errors2}"
    )


async def test_first_scan_uses_last_50_lines(tmp_path):
    """First scan: only the last 50 lines of the tail are used, not the full output."""
    _setup_state_paths(tmp_path)

    # 200 lines, errors only in the first 100 (NOT in the last 50)
    old_error = "ERROR: ancient error that should not be scanned"
    new_info = "INFO: recent normal line"

    lines = [old_error] * 100 + [new_info] * 100
    log_text = "\n".join(lines)

    project = {"cwd": str(tmp_path / "proj3"), "log_cmd": "dummy_cmd"}

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        errors = await webapp._scan_project_errors(project)

    # Last 50 lines contain only INFO — no errors
    assert len(errors) == 0, (
        f"First scan must use only the last 50 lines of the tail, "
        f"where there are no ERRORs. Got {len(errors)} errors."
    )


async def test_first_scan_saves_fingerprint(tmp_path):
    """After the first scan the fingerprint is saved in scan_state.json."""
    _setup_state_paths(tmp_path)

    lines = ["INFO line 1", "INFO line 2", "INFO final line"]
    log_text = "\n".join(lines)

    project = {"cwd": str(tmp_path / "proj4"), "log_cmd": "dummy_cmd"}

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        await webapp._scan_project_errors(project)

    state = _scan_state_load()
    assert str(tmp_path / "proj4") in state, "fingerprint must be saved by cwd"
    proj_state = state[str(tmp_path / "proj4")]
    assert "block" in proj_state
    assert "last_scan_ts" in proj_state

    # Block fingerprint = sha1 of the last N lines (here all 3, since there are < N=6)
    assert proj_state["block"] == [_fp("INFO line 1"), _fp("INFO line 2"), _fp("INFO final line")]


async def test_fingerprint_rotation_fallback(tmp_path):
    """If fingerprint is not found in new output (rotation) → fallback to 500 lines."""
    _setup_state_paths(tmp_path)

    project = {"cwd": str(tmp_path / "proj5"), "log_cmd": "dummy_cmd"}

    # Set a block fingerprint that will NOT appear in the new output (rotation)
    state = {str(tmp_path / "proj5"): {"block": [_fp("old rotated line A"), _fp("old rotated line B")], "last_scan_ts": 1.0}}
    _scan_state_save(state)

    # New output does not contain the old block
    new_lines = ["INFO new server start", "ERROR: new error after rotation"]
    log_text = "\n".join(new_lines)

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        errors = await webapp._scan_project_errors(project)

    # Fallback: full output (≤500) is parsed → specific error is found
    assert any("new error after rotation" in e.get("message", "") for e in errors), (
        f"After rotation there must be a fallback parse of the full output. Errors: {errors}"
    )


# ─────────────────────────────────────────────────────────────────
# Task B: dismissed in _ingest_errors_to_board
# ─────────────────────────────────────────────────────────────────


async def test_dismissed_hash_not_recreated_in_ingest(tmp_path):
    """Dismissed hash → _ingest_errors_to_board does NOT create a card."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "projb1"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("dismissed error", "DismissedError")
    h = err["hash"]

    # Record as dismissed
    _dismissed_add(h)

    # Try to create a card
    added, updated = await _ingest_errors_to_board(str(cwd), "projb1", [err])

    assert added == 0, f"dismissed hash must not be added to the board, added={added}"
    assert updated == 0


async def test_dismissed_hash_recreated_after_ttl(tmp_path):
    """After TTL expiry a dismissed hash creates a card again."""
    data_dir = _setup_state_paths(tmp_path)
    cwd = tmp_path / "projb2"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("ttl expired error", "ExpiredError")
    h = err["hash"]

    # Write dismissed with a timestamp beyond the TTL
    old_ts = time.time() - webapp._DISMISS_TTL - 10
    (data_dir / "dismissed_incidents.json").write_text(
        json.dumps({h: old_ts}), encoding="utf-8"
    )

    added, updated = await _ingest_errors_to_board(str(cwd), "projb2", [err])

    assert added == 1, f"After TTL a card must be created, added={added}"


async def test_existing_card_not_affected_by_dismissed(tmp_path):
    """If a card already exists on the board (not dismissed), update seen — do not block."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "projb3"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("existing card error", "ExistingError")
    h = err["hash"]

    # First ingest — creates the card
    await _ingest_errors_to_board(str(cwd), "projb3", [err])

    # Add hash to dismissed
    _dismissed_add(h)

    # Second ingest — card EXISTS, dismissed does NOT block update
    added, updated = await _ingest_errors_to_board(str(cwd), "projb3", [err])

    assert updated == 1, "Existing card must be updated (seen++) even if hash is dismissed"
    assert added == 0


# ─────────────────────────────────────────────────────────────────
# Task B: dismiss via API (DELETE and move-to-done)
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def project_dir_b(tmp_path):
    pdir = tmp_path / "testproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_dismissed(tmp_path, project_dir_b):
    """ctx with one project; _SCAN_STATE_PATH/_DISMISSED_PATH initialized."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _scan_state_init({"DATA": data_dir})
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "testproject",
                "cwd": str(project_dir_b),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
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
def board_app_dismissed(fake_ctx_dismissed):
    from aiohttp import web
    import webapp as _webapp

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_dismissed

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    app.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    app.router.add_delete("/api/projects/{id}/tasks/{card}", _webapp.api_delete_task)

    return app


def _make_tasks_md_with_err_card(cwd: Path, err_hash: str, name: str = "testproject") -> None:
    """Creates TASKS.md with one err-card in Failed."""
    card_id = f"err-{err_hash}"
    content = (
        f"# Tasks — {name}\n\n"
        "## Backlog\n\n"
        "## In Progress\n\n"
        "## Review\n\n"
        f"## Failed\n"
        f"- [ ] [ERR] Test error <!--ops:{card_id}-->\n"
    )
    _tasks_path(str(cwd)).write_text(content, encoding="utf-8")


async def test_delete_err_card_records_dismissed(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """DELETE err-card → hash written to dismissed_incidents."""
    err_hash = "ab12cd"
    _make_tasks_md_with_err_card(project_dir_b, err_hash)

    client = await aiohttp_client(board_app_dismissed)
    auth = {"Cookie": f"cops_auth={fake_ctx_dismissed['_auth_token']}"}

    resp = await client.delete(f"/api/projects/testproject/tasks/err-{err_hash}", headers=auth)
    assert resp.status == 200

    # Verify hash is written to dismissed
    assert _dismissed_is_active(err_hash, time.time()), (
        f"After DELETE err-card hash {err_hash!r} must be in dismissed"
    )


async def test_move_err_card_to_done_records_dismissed(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """MOVE err-card to=done → hash written to dismissed_incidents."""
    err_hash = "ef34ab"
    _make_tasks_md_with_err_card(project_dir_b, err_hash)

    client = await aiohttp_client(board_app_dismissed)
    auth = {"Cookie": f"cops_auth={fake_ctx_dismissed['_auth_token']}"}

    resp = await client.post(
        f"/api/projects/testproject/tasks/err-{err_hash}/move",
        headers=auth,
        json={"to": "done"},
    )
    assert resp.status == 200

    assert _dismissed_is_active(err_hash, time.time()), (
        f"After move-to-done err-card hash {err_hash!r} must be in dismissed"
    )


async def test_move_regular_card_to_done_does_not_affect_dismissed(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """Regular (non-err) card move-to-done → dismissed is not changed."""
    # Create a regular card
    from webapp import _tasks_path as tp
    content = (
        "# Tasks — testproject\n\n"
        "## Backlog\n"
        "- [ ] Regular task <!--ops:aabbcc-->\n"
        "## In Progress\n## Review\n## Failed\n"
    )
    tp(str(project_dir_b)).write_text(content, encoding="utf-8")

    client = await aiohttp_client(board_app_dismissed)
    auth = {"Cookie": f"cops_auth={fake_ctx_dismissed['_auth_token']}"}

    resp = await client.post(
        "/api/projects/testproject/tasks/aabbcc/move",
        headers=auth,
        json={"to": "done"},
    )
    assert resp.status == 200

    # dismissed for 'aabbcc' was not written (it is not an err-card)
    assert not _dismissed_is_active("aabbcc", time.time()), (
        "Regular card must not end up in dismissed"
    )


async def test_delete_nonexistent_card_returns_404(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """DELETE non-existent card → 404, dismissed not changed."""
    # Create an empty board
    _make_empty_board(project_dir_b)

    client = await aiohttp_client(board_app_dismissed)
    auth = {"Cookie": f"cops_auth={fake_ctx_dismissed['_auth_token']}"}

    resp = await client.delete(
        "/api/projects/testproject/tasks/err-ffffff", headers=auth
    )
    assert resp.status == 404
    # dismissed NOT written — there was no card
    assert not _dismissed_is_active("ffffff", time.time())


def test_format_incident_desc_strips_unicode_line_seps():
    """BLOCKER-regression: U+2028/U+2029 in excerpt must not produce extra lines in
    description (otherwise splitlines() on the board injects a section/card)."""
    meta = {"source": "log", "seen": "1", "excerpt": "boom  ## Done  - [ ] evil"}
    desc = webapp._format_incident_desc(meta)
    assert " " not in desc and " " not in desc
    excerpt_lines = [l for l in desc.splitlines() if l.startswith("excerpt=")]
    assert len(excerpt_lines) == 1, f"excerpt must be a single line: {desc!r}"


async def test_report_incident_debounce_is_per_project(tmp_path, monkeypatch):
    """Debounce is per-(project,hash): the same error in DIFFERENT projects does not
    suppress each other (path normalizes to /PATH → shared hash, but different projects)."""
    _setup_state_paths(tmp_path)
    cwd_a = tmp_path / "A"; cwd_a.mkdir(); _make_empty_board(cwd_a)
    cwd_b = tmp_path / "B"; cwd_b.mkdir(); _make_empty_board(cwd_b)
    projs = {"A": {"cwd": str(cwd_a), "name": "A"}, "B": {"cwd": str(cwd_b), "name": "B"}}
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda ctx, pid="claude-ops-bot": projs.get(pid))
    webapp._REPORT_DEBOUNCE.clear()

    await webapp._report_incident({}, "ValueError", "/x", project_id="A")
    await webapp._report_incident({}, "ValueError", "/x", project_id="B")  # same hash, different project

    _, _, cols_a = _load_board(str(cwd_a))
    _, _, cols_b = _load_board(str(cwd_b))
    assert len(cols_a["failed"]) == 1, "project A got its card"
    assert len(cols_b["failed"]) == 1, "project B must NOT be suppressed by project A's debounce"

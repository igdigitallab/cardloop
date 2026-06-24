"""
Tests G — Card Queue (sequential per-project card queue).

Covers:
- _queue_enqueue / _queue_remove / _queue_for: dedup, FIFO order, corrupted file → {}
- _start_card_run: busy → busy (no lock reservation, no spawn); free → starts
- batch endpoint: enqueue N valid → queued==N; invalid/missing/in_progress are skipped; 404
- _drain_queue: busy → None; free + queued → runs first, removes from queue;
  stale entry (not on board) → dropped
- enqueue-on-busy: api_move_task to in_progress when busy → card in queue, 200
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _queue_flush,
    _queue_enqueue,
    _queue_remove,
    _queue_for,
    _start_card_run,
    _drain_queue,
    _tasks_path,
    _save_board,
    _load_board,
    BOARD_COLUMNS,
    _derive_token,
)


# ─────────────────────────── helpers ───────────────────────────

def _set_queue_path(tmp_path: Path) -> Path:
    """Initialises in-memory _QUEUE + _QUEUE_PATH via _scan_state_init.
    Clears _QUEUE → test isolation: tests do not see each other's queues."""
    p = tmp_path / "data" / "card_queue.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    # _scan_state_init: _QUEUE.clear() + load file (if present) → canonical dict
    _webapp._scan_state_init({"DATA": tmp_path / "data"})
    return p


def _make_tasks_with_cards(cwd: Path, backlog=None, in_progress=None, review=None, failed=None):
    """Creates TASKS.md with the given cards."""
    def _line(card):
        return f"- [ ] {card['text']} <!--ops:{card['id']}-->"

    lines = ["# Tasks\n", "## Backlog\n"]
    for c in (backlog or []):
        lines.append(_line(c))
    lines += ["\n## In Progress\n"]
    for c in (in_progress or []):
        lines.append(_line(c))
    lines += ["\n## Review\n"]
    for c in (review or []):
        lines.append(_line(c))
    lines += ["\n## Failed\n"]
    for c in (failed or []):
        lines.append(_line(c))
    _tasks_path(str(cwd)).write_text("\n".join(lines), encoding="utf-8")


async def _noop_engine(**kw):
    """Async generator stub engine (not a lambda) — for ctx where the engine must not actually run."""
    if False:
        yield {"type": "text", "text": ""}


def _make_project(cwd: Path, session_key: str = "1001:42") -> dict:
    return {
        "id": cwd.name,
        "name": cwd.name,
        "cwd": str(cwd),
        "model": "sonnet",
        "session_key": session_key,
        "is_free": False,
        "git_enabled": False,
    }


def _make_ctx(tmp_path: Path, run_engine=None) -> dict:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": "testpass",
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": run_engine,
        "ptb_app": None,
        "_aiohttp_app": None,
    }


# ─────────────────────────── B: queue helpers ───────────────────────────


def test_queue_enqueue_dedup(tmp_path):
    """Re-enqueueing the same card does not create a duplicate."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aabbcc")
    _queue_enqueue("1001:42", "aabbcc")
    q = _queue_for("1001:42")
    assert q == ["aabbcc"], f"Expected ['aabbcc'], got {q}"


def test_queue_enqueue_fifo_order(tmp_path):
    """Cards are queued in insertion order."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aaa111")
    _queue_enqueue("1001:42", "bbb222")
    _queue_enqueue("1001:42", "ccc333")
    q = _queue_for("1001:42")
    assert q == ["aaa111", "bbb222", "ccc333"]


def test_queue_remove(tmp_path):
    """_queue_remove removes a card from the queue."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aabbcc")
    _queue_enqueue("1001:42", "ddeeff")
    _queue_remove("1001:42", "aabbcc")
    q = _queue_for("1001:42")
    assert q == ["ddeeff"]


def test_queue_remove_nonexistent(tmp_path):
    """_queue_remove of a non-existent card is silent, no error raised."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aabbcc")
    _queue_remove("1001:42", "ffffff")  # not present — must not raise
    q = _queue_for("1001:42")
    assert q == ["aabbcc"]


def test_queue_corrupt_file(tmp_path):
    """Corrupt card_queue.json on init → _QUEUE is empty, no crash."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "card_queue.json").write_text("not-json{{{", encoding="utf-8")
    # init reads the corrupt file → swallows it, _QUEUE stays empty
    _webapp._scan_state_init({"DATA": data_dir})
    assert _webapp._QUEUE == {}
    assert _queue_for("1001:42") == []


def test_queue_missing_file(tmp_path):
    """Missing card_queue.json on init → _queue_for returns []."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # File not created
    _webapp._scan_state_init({"DATA": data_dir})
    q = _queue_for("no:session")
    assert q == []


def test_queue_path_none_memory_only(tmp_path):
    """_QUEUE_PATH is None → enqueue works in memory, flush does not crash."""
    _webapp._QUEUE.clear()
    _webapp._QUEUE_PATH = None
    assert _queue_enqueue("1001:42", "aabbcc") is True
    assert _queue_for("1001:42") == ["aabbcc"]
    # re-enqueue — dedup
    assert _queue_enqueue("1001:42", "aabbcc") is False


def test_queue_restart_resume(tmp_path):
    """Queue survives restart: enqueue → flush to disk → new init loads it back."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aaa111")
    _queue_enqueue("1001:42", "bbb222")
    # 'restart' — re-init from the same DATA
    _webapp._scan_state_init({"DATA": tmp_path / "data"})
    assert _queue_for("1001:42") == ["aaa111", "bbb222"]


def test_queue_enqueue_returns_bool(tmp_path):
    """_queue_enqueue → True on actual addition, False on dedup."""
    _set_queue_path(tmp_path)
    assert _queue_enqueue("1001:42", "aabbcc") is True
    assert _queue_enqueue("1001:42", "aabbcc") is False  # dedup
    assert _queue_enqueue("1001:42", "ddeeff") is True


def test_queue_separate_sessions(tmp_path):
    """Queues for different session_keys are independent."""
    _set_queue_path(tmp_path)
    _queue_enqueue("proj1:1", "aaa111")
    _queue_enqueue("proj2:2", "bbb222")
    assert _queue_for("proj1:1") == ["aaa111"]
    assert _queue_for("proj2:2") == ["bbb222"]


# ─────────────────────────── A: _start_card_run ───────────────────────────


async def test_start_card_run_busy(tmp_path):
    """If running[session_key] is busy → busy, lock is NOT reserved again."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)
    ctx["running"]["1001:42"] = True  # busy

    _make_tasks_with_cards(cwd, backlog=[{"id": "aabbcc", "text": "Task"}])

    result = await _start_card_run(ctx, None, project, "aabbcc")
    assert result == {"started": False, "reason": "busy"}
    # running still True (not overwritten)
    assert ctx["running"]["1001:42"] is True


async def test_start_card_run_no_engine(tmp_path):
    """run_engine absent → no_engine, lock not reserved."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=None)

    _make_tasks_with_cards(cwd, backlog=[{"id": "aabbcc", "text": "Task"}])

    result = await _start_card_run(ctx, None, project, "aabbcc")
    assert result == {"started": False, "reason": "no_engine"}
    assert "1001:42" not in ctx["running"]


async def test_start_card_run_not_found(tmp_path):
    """Card not found on the board → not_found, lock released."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)

    async def fake_engine(**kw):
        yield {"type": "text", "text": "x"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    _make_tasks_with_cards(cwd)  # empty board

    result = await _start_card_run(ctx, None, project, "ffffff")
    assert result == {"started": False, "reason": "not_found"}
    # Lock must be released
    assert "1001:42" not in ctx["running"]


async def test_start_card_run_success(tmp_path):
    """Free project + card on board → started=True, lock reserved, _spawn_bg called."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    data_dir = tmp_path / "data"

    spawned = []

    async def fake_engine(**kw):
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "s1"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    ctx["DATA"] = data_dir

    _make_tasks_with_cards(cwd, backlog=[{"id": "aabbcc", "text": "Task"}])

    # Mock _spawn_bg to avoid running the real task
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: spawned.append(coro) or MagicMock()) as mock_spawn:
        with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
            result = await _start_card_run(ctx, MagicMock(), project, "aabbcc")

    assert result["started"] is True
    assert result["card_id"] == "aabbcc"
    # Lock reserved synchronously
    assert ctx["running"].get("1001:42") is True
    assert mock_spawn.called


async def test_start_card_run_moves_card_to_in_progress(tmp_path):
    """_start_card_run moves the card to in_progress on the board."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    data_dir = tmp_path / "data"

    async def fake_engine(**kw):
        yield {"type": "text", "text": "done"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    ctx["DATA"] = data_dir

    _make_tasks_with_cards(cwd, backlog=[{"id": "aabbcc", "text": "Task"}])

    with patch.object(_webapp, "_spawn_bg", return_value=MagicMock()):
        with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
            await _start_card_run(ctx, MagicMock(), project, "aabbcc")

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["in_progress"])
    assert not any(c["id"] == "aabbcc" for c in cols["backlog"])


# ─────────────────────────── D: _drain_queue ───────────────────────────


async def test_drain_queue_busy(tmp_path):
    """_drain_queue when project is busy → None, queue untouched."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)
    ctx["running"]["1001:42"] = True

    _queue_enqueue("1001:42", "aabbcc")
    _make_tasks_with_cards(cwd, backlog=[{"id": "aabbcc", "text": "Task"}])

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None
    # Queue unchanged
    assert _queue_for("1001:42") == ["aabbcc"]


async def test_drain_queue_starts_first(tmp_path):
    """_drain_queue when project is free → runs first card, removes it from queue."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    data_dir = tmp_path / "data"

    async def fake_engine(**kw):
        yield {"type": "text", "text": "done"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    ctx["DATA"] = data_dir

    _queue_enqueue("1001:42", "aabbcc")
    _queue_enqueue("1001:42", "ddeeff")
    _make_tasks_with_cards(cwd, backlog=[
        {"id": "aabbcc", "text": "Task 1"},
        {"id": "ddeeff", "text": "Task 2"},
    ])

    with patch.object(_webapp, "_spawn_bg", return_value=MagicMock()):
        with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
            result = await _drain_queue(ctx, MagicMock(), project)

    assert result == "aabbcc"
    # First card removed from queue, second remains
    assert _queue_for("1001:42") == ["ddeeff"]


async def test_drain_queue_stale_entry(tmp_path):
    """_drain_queue: card in queue but not on board (stale) → dropped, not started."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)

    _queue_enqueue("1001:42", "ffffff")  # not on board
    _make_tasks_with_cards(cwd)  # empty board

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None
    # Stale entry removed
    assert _queue_for("1001:42") == []


async def test_drain_queue_empty(tmp_path):
    """_drain_queue with empty queue → None, no errors."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)

    _make_tasks_with_cards(cwd)

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None


async def test_drain_queue_stale_first_does_not_block_valid(tmp_path):
    """Fix 7b: queue ['stale','valid'] — stale first does not block valid.
    _drain_queue drops stale, continues, starts valid."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    data_dir = tmp_path / "data"

    async def fake_engine(**kw):
        yield {"type": "text", "text": "done"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    ctx["DATA"] = data_dir

    # "stale" not on board, "valid" in backlog
    _queue_enqueue("1001:42", "stale1")
    _queue_enqueue("1001:42", "valid1")
    _make_tasks_with_cards(cwd, backlog=[{"id": "valid1", "text": "Valid task"}])

    with patch.object(_webapp, "_spawn_bg", return_value=MagicMock()):
        with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
            result = await _drain_queue(ctx, MagicMock(), project)

    assert result == "valid1", f"valid1 should be started, got {result}"
    # Both entries removed from queue (stale dropped, valid started)
    assert _queue_for("1001:42") == []


async def test_drain_queue_orphan_in_progress_guard(tmp_path):
    """Fix 3: if in_progress has an orphan (after restart, running-lock lost) →
    _drain_queue must not start a second card."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)
    # running is empty (lock lost), but a card is stuck in in_progress
    _queue_enqueue("1001:42", "valid1")
    _make_tasks_with_cards(
        cwd,
        backlog=[{"id": "valid1", "text": "Queued task"}],
        in_progress=[{"id": "orphan", "text": "Orphan running"}],
    )

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None, "Orphan in in_progress must block start"
    # Queue untouched
    assert _queue_for("1001:42") == ["valid1"]


# ─────────────────────────── C: batch endpoint ───────────────────────────


def _make_board_app(ctx, project_dir):
    """Creates an aiohttp application with the required routes."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    app.router.add_post("/api/projects/{id}/tasks", _webapp.api_create_task)
    app.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    app.router.add_delete("/api/projects/{id}/tasks/{card}", _webapp.api_delete_task)
    app.router.add_post("/api/projects/{id}/cards/run-batch", _webapp.api_run_batch)

    return app


def _make_ctx_with_project(tmp_path, project_dir, run_engine=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": project_dir.name,
                "cwd": str(project_dir),
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
        "run_engine": run_engine,
        "ptb_app": None,
        "_aiohttp_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_run_batch_enqueues_valid_cards(aiohttp_client, tmp_path):
    """POST /run-batch with 2 valid cards → queued=2."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[
        {"id": "aabbcc", "text": "Task 1"},
        {"id": "ddeeff", "text": "Task 2"},
    ])

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/cards/run-batch",
        json={"card_ids": ["aabbcc", "ddeeff"]},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["queued"] == 2


async def test_run_batch_skips_invalid_ids(aiohttp_client, tmp_path):
    """POST /run-batch: invalid ids are skipped."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[
        {"id": "aabbcc", "text": "Task 1"},
    ])

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/cards/run-batch",
        json={"card_ids": ["aabbcc", "../etc/passwd", "!!!bad!!!"]},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queued"] == 1  # only aabbcc


async def test_run_batch_skips_nonexistent_cards(aiohttp_client, tmp_path):
    """POST /run-batch: non-existent cards are skipped."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[
        {"id": "aabbcc", "text": "Task 1"},
    ])

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/cards/run-batch",
        json={"card_ids": ["aabbcc", "ffffff"]},  # ffffff not on board
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queued"] == 1  # only aabbcc


async def test_run_batch_skips_in_progress_cards(aiohttp_client, tmp_path):
    """POST /run-batch: cards in in_progress are skipped (not in runnable set)."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir,
        backlog=[{"id": "aabbcc", "text": "Task 1"}],
        in_progress=[{"id": "ddeeff", "text": "Running"}],
    )

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/cards/run-batch",
        json={"card_ids": ["aabbcc", "ddeeff"]},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queued"] == 1  # ddeeff skipped


async def test_run_batch_unknown_project(aiohttp_client, tmp_path):
    """POST /run-batch for a non-existent project → 404."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/projects/ghost-project/cards/run-batch",
        json={"card_ids": ["aabbcc"]},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 404


async def test_run_batch_bad_body(aiohttp_client, tmp_path):
    """POST /run-batch without card_ids → 400."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir)

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/cards/run-batch",
        json={"card_ids": "not-a-list"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 400


# ─────────────────────────── F: enqueue-on-busy via api_move_task ───────────────────────────


async def test_move_to_in_progress_busy_enqueues_card(aiohttp_client, tmp_path):
    """api_move_task to in_progress when run_engine is busy → card in queue, 200."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])

    async def fake_engine(**kw):
        yield {"type": "text", "text": "x"}

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=fake_engine)
    ctx["running"]["1001:42"] = True  # project is busy

    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/tasks/aabbcc/move",
        json={"to": "in_progress"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    data = await resp.json()
    # Fix 6: enqueued flag (board["queued"] — a list — is NOT overwritten)
    assert data.get("enqueued") is True, f"Expected enqueued=True: {data}"
    assert isinstance(data.get("queued"), list), f"queued must be a list: {data}"
    assert "aabbcc" in data["queued"], f"board queued must contain the card: {data}"

    # Card must be in the in-memory queue
    q = _queue_for("1001:42")
    assert "aabbcc" in q, f"Card must be in queue, queue: {q}"


async def test_move_to_in_progress_not_busy_starts_immediately(aiohttp_client, tmp_path):
    """api_move_task to in_progress when project is free with run_engine → 200, lock acquired."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])

    async def fake_engine(**kw):
        await asyncio.sleep(100)  # long-running so lock is not released before response
        yield {"type": "text", "text": "x"}

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=fake_engine)

    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
        resp = await client.post(
            f"/api/projects/{project_dir.name}/tasks/aabbcc/move",
            json={"to": "in_progress"},
            headers=_auth_headers(ctx),
        )

    assert resp.status == 200
    # Card must NOT be in the queue (it was started immediately)
    q = _queue_for("1001:42")
    assert "aabbcc" not in q


# ─────────────────────────── F: queue exposed in GET /tasks ───────────────────────────


async def test_get_tasks_includes_queued(aiohttp_client, tmp_path):
    """GET /tasks returns a 'queued' field with card_ids from the queue."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])
    _queue_enqueue("1001:42", "aabbcc")

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.get(
        f"/api/projects/{project_dir.name}/tasks",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert "queued" in data, f"Field 'queued' must be in response: {data.keys()}"
    assert "aabbcc" in data["queued"]


async def test_get_tasks_queued_empty_by_default(aiohttp_client, tmp_path):
    """GET /tasks with no queue → queued=[]."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir)

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.get(
        f"/api/projects/{project_dir.name}/tasks",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("queued") == []


# ─────────────────────────── F: queue cleanup on delete/move ───────────────────────────


async def test_delete_task_removes_from_queue(aiohttp_client, tmp_path):
    """DELETE card → card is removed from the queue."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])
    _queue_enqueue("1001:42", "aabbcc")
    assert "aabbcc" in _queue_for("1001:42")

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.delete(
        f"/api/projects/{project_dir.name}/tasks/aabbcc",
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    assert "aabbcc" not in _queue_for("1001:42")


async def test_move_away_removes_from_queue(aiohttp_client, tmp_path):
    """Moving a card (backlog→review) → removes it from the queue."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])
    _queue_enqueue("1001:42", "aabbcc")
    assert "aabbcc" in _queue_for("1001:42")

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=None)
    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/tasks/aabbcc/move",
        json={"to": "review"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    assert "aabbcc" not in _queue_for("1001:42")

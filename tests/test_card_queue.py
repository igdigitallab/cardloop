"""
Тесты G — Card Queue (последовательная очередь карточек per-project).

Покрывает:
- _queue_enqueue / _queue_remove / _queue_for: dedup, порядок FIFO, корruptedfile → {}
- _start_card_run: занятый → busy (не резервирует, не спавнит); свободный → запускает
- batch endpoint: enqueue N valid → queued==N; invalid/missing/in_progress пропускаются; 404
- _drain_queue: занятый → None; свободный + queued → запуск первой, удаление из очереди;
  устаревшая запись (нет на доске) → dropped
- enqueue-on-busy: api_move_task to in_progress при занятом → карточка в очереди, 200
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
    """Инициализирует in-memory _QUEUE + _QUEUE_PATH через _scan_state_init.
    Чистит _QUEUE → изоляция: тесты не видят чужую очередь."""
    p = tmp_path / "data" / "card_queue.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    # _scan_state_init: _QUEUE.clear() + загрузка файла (если есть) → канонический dict
    _webapp._scan_state_init({"DATA": tmp_path / "data"})
    return p


def _make_tasks_with_cards(cwd: Path, backlog=None, in_progress=None, review=None, failed=None):
    """Создаёт TASKS.md с заданными карточками."""
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
    """Async-генератор-заглушка движка (не лямбда) — для ctx, где движок не должен реально гонять."""
    if False:
        yield {"type": "text", "text": ""}


def _make_project(cwd: Path, session_key: str = "1001:42") -> dict:
    return {
        "id": cwd.name,
        "name": cwd.name,
        "cwd": str(cwd),
        "model": "sonnet",
        "tg_thread": session_key,
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
    """Повторная постановка той же карточки — не дублируется."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aabbcc")
    _queue_enqueue("1001:42", "aabbcc")
    q = _queue_for("1001:42")
    assert q == ["aabbcc"], f"Ожидали ['aabbcc'], получили {q}"


def test_queue_enqueue_fifo_order(tmp_path):
    """Карточки ставятся в очередь в порядке добавления."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aaa111")
    _queue_enqueue("1001:42", "bbb222")
    _queue_enqueue("1001:42", "ccc333")
    q = _queue_for("1001:42")
    assert q == ["aaa111", "bbb222", "ccc333"]


def test_queue_remove(tmp_path):
    """_queue_remove убирает карточку из очереди."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aabbcc")
    _queue_enqueue("1001:42", "ddeeff")
    _queue_remove("1001:42", "aabbcc")
    q = _queue_for("1001:42")
    assert q == ["ddeeff"]


def test_queue_remove_nonexistent(tmp_path):
    """_queue_remove несуществующей карточки — тихо, без ошибки."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aabbcc")
    _queue_remove("1001:42", "ffffff")  # нет такой — не должно бросить
    q = _queue_for("1001:42")
    assert q == ["aabbcc"]


def test_queue_corrupt_file(tmp_path):
    """Битый файл card_queue.json при init → _QUEUE пустой, без падения."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "card_queue.json").write_text("not-json{{{", encoding="utf-8")
    # init читает битый файл → проглатывает, _QUEUE остаётся пустым
    _webapp._scan_state_init({"DATA": data_dir})
    assert _webapp._QUEUE == {}
    assert _queue_for("1001:42") == []


def test_queue_missing_file(tmp_path):
    """Отсутствующий card_queue.json при init → _queue_for возвращает []."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Файл не создаём
    _webapp._scan_state_init({"DATA": data_dir})
    q = _queue_for("no:session")
    assert q == []


def test_queue_path_none_memory_only(tmp_path):
    """_QUEUE_PATH is None → enqueue работает в памяти, flush не падает."""
    _webapp._QUEUE.clear()
    _webapp._QUEUE_PATH = None
    assert _queue_enqueue("1001:42", "aabbcc") is True
    assert _queue_for("1001:42") == ["aabbcc"]
    # повторно — дедуп
    assert _queue_enqueue("1001:42", "aabbcc") is False


def test_queue_restart_resume(tmp_path):
    """Очередь переживает рестарт: enqueue → flush на диск → новый init загружает её."""
    _set_queue_path(tmp_path)
    _queue_enqueue("1001:42", "aaa111")
    _queue_enqueue("1001:42", "bbb222")
    # «рестарт» — повторный init из того же DATA
    _webapp._scan_state_init({"DATA": tmp_path / "data"})
    assert _queue_for("1001:42") == ["aaa111", "bbb222"]


def test_queue_enqueue_returns_bool(tmp_path):
    """_queue_enqueue → True при реальном добавлении, False при дедупе."""
    _set_queue_path(tmp_path)
    assert _queue_enqueue("1001:42", "aabbcc") is True
    assert _queue_enqueue("1001:42", "aabbcc") is False  # дедуп
    assert _queue_enqueue("1001:42", "ddeeff") is True


def test_queue_separate_sessions(tmp_path):
    """Очереди разных session_key независимы."""
    _set_queue_path(tmp_path)
    _queue_enqueue("proj1:1", "aaa111")
    _queue_enqueue("proj2:2", "bbb222")
    assert _queue_for("proj1:1") == ["aaa111"]
    assert _queue_for("proj2:2") == ["bbb222"]


# ─────────────────────────── A: _start_card_run ───────────────────────────


async def test_start_card_run_busy(tmp_path):
    """Если running[session_key] занят → busy, lock НЕ резервируется повторно."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)
    ctx["running"]["1001:42"] = True  # занято

    _make_tasks_with_cards(cwd, backlog=[{"id": "aabbcc", "text": "Task"}])

    result = await _start_card_run(ctx, None, project, "aabbcc")
    assert result == {"started": False, "reason": "busy"}
    # running все ещё True (не перезаписан)
    assert ctx["running"]["1001:42"] is True


async def test_start_card_run_no_engine(tmp_path):
    """run_engine отсутствует → no_engine, lock не резервируется."""
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
    """Карточка не найдена на доске → not_found, lock освобождается."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)

    async def fake_engine(**kw):
        yield {"type": "text", "text": "x"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    _make_tasks_with_cards(cwd)  # пустая доска

    result = await _start_card_run(ctx, None, project, "ffffff")
    assert result == {"started": False, "reason": "not_found"}
    # Lock должен быть освобождён
    assert "1001:42" not in ctx["running"]


async def test_start_card_run_success(tmp_path):
    """Свободный проект + карточка на доске → started=True, lock зарезервирован, _spawn_bg вызван."""
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

    # Мокируем _spawn_bg чтобы не запускать реальный прогон
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: spawned.append(coro) or MagicMock()) as mock_spawn:
        with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
            result = await _start_card_run(ctx, MagicMock(), project, "aabbcc")

    assert result["started"] is True
    assert result["card_id"] == "aabbcc"
    # Lock зарезервирован синхронно
    assert ctx["running"].get("1001:42") is True
    assert mock_spawn.called


async def test_start_card_run_moves_card_to_in_progress(tmp_path):
    """_start_card_run перемещает карточку в in_progress на доске."""
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
    """_drain_queue при занятом проекте → None, очередь не тронута."""
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
    # Очередь не изменилась
    assert _queue_for("1001:42") == ["aabbcc"]


async def test_drain_queue_starts_first(tmp_path):
    """_drain_queue при свободном проекте → запускает первую карточку, убирает из очереди."""
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
    # Первая карточка удалена из очереди, вторая осталась
    assert _queue_for("1001:42") == ["ddeeff"]


async def test_drain_queue_stale_entry(tmp_path):
    """_drain_queue: карточка в очереди но не на доске (устаревшая) → дропается, не запускается."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)

    _queue_enqueue("1001:42", "ffffff")  # нет на доске
    _make_tasks_with_cards(cwd)  # пустая доска

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None
    # Устаревшая запись удалена
    assert _queue_for("1001:42") == []


async def test_drain_queue_empty(tmp_path):
    """_drain_queue при пустой очереди → None, без ошибок."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)

    _make_tasks_with_cards(cwd)

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None


async def test_drain_queue_stale_first_does_not_block_valid(tmp_path):
    """Fix 7b: очередь ['stale','valid'] — устаревшая первая не блокирует валидную.
    _drain_queue дропает stale, продолжает, стартует valid."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    data_dir = tmp_path / "data"

    async def fake_engine(**kw):
        yield {"type": "text", "text": "done"}

    ctx = _make_ctx(tmp_path, run_engine=fake_engine)
    ctx["DATA"] = data_dir

    # "stale" нет на доске, "valid" в backlog
    _queue_enqueue("1001:42", "stale1")
    _queue_enqueue("1001:42", "valid1")
    _make_tasks_with_cards(cwd, backlog=[{"id": "valid1", "text": "Valid task"}])

    with patch.object(_webapp, "_spawn_bg", return_value=MagicMock()):
        with patch.object(_webapp, "_card_run_mode", new=AsyncMock(return_value="legacy")):
            result = await _drain_queue(ctx, MagicMock(), project)

    assert result == "valid1", f"Должна стартовать valid1, получили {result}"
    # обе записи ушли из очереди (stale дропнут, valid запущен)
    assert _queue_for("1001:42") == []


async def test_drain_queue_orphan_in_progress_guard(tmp_path):
    """Fix 3: если в in_progress кто-то висит (orphan после рестарта, running-lock потерян) →
    _drain_queue не стартует вторую карточку."""
    _set_queue_path(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    project = _make_project(cwd)
    ctx = _make_ctx(tmp_path, run_engine=_noop_engine)
    # running пуст (lock потерян), но карточка висит в in_progress
    _queue_enqueue("1001:42", "valid1")
    _make_tasks_with_cards(
        cwd,
        backlog=[{"id": "valid1", "text": "Queued task"}],
        in_progress=[{"id": "orphan", "text": "Orphan running"}],
    )

    result = await _drain_queue(ctx, MagicMock(), project)
    assert result is None, "orphan в in_progress должен блокировать старт"
    # очередь не тронута
    assert _queue_for("1001:42") == ["valid1"]


# ─────────────────────────── C: batch endpoint ───────────────────────────


def _make_board_app(ctx, project_dir):
    """Создаёт aiohttp-приложение с нужными маршрутами."""
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
    """POST /run-batch с 2 валидными картами → queued=2."""
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
    """POST /run-batch: невалидные id пропускаются."""
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
    assert data["queued"] == 1  # только aabbcc


async def test_run_batch_skips_nonexistent_cards(aiohttp_client, tmp_path):
    """POST /run-batch: несуществующие карточки пропускаются."""
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
        json={"card_ids": ["aabbcc", "ffffff"]},  # ffffff нет на доске
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queued"] == 1  # только aabbcc


async def test_run_batch_skips_in_progress_cards(aiohttp_client, tmp_path):
    """POST /run-batch: карточки в in_progress пропускаются (не в runnable)."""
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
    assert data["queued"] == 1  # ddeeff пропущен


async def test_run_batch_unknown_project(aiohttp_client, tmp_path):
    """POST /run-batch на несуществующий проект → 404."""
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
    """POST /run-batch без card_ids → 400."""
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
    """api_move_task to in_progress при run_engine занятом → карточка в очереди, 200."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])

    async def fake_engine(**kw):
        yield {"type": "text", "text": "x"}

    ctx = _make_ctx_with_project(tmp_path, project_dir, run_engine=fake_engine)
    ctx["running"]["1001:42"] = True  # проект занят

    app = _make_board_app(ctx, project_dir)
    client = await aiohttp_client(app)

    resp = await client.post(
        f"/api/projects/{project_dir.name}/tasks/aabbcc/move",
        json={"to": "in_progress"},
        headers=_auth_headers(ctx),
    )
    assert resp.status == 200, f"Ожидали 200, получили {resp.status}"
    data = await resp.json()
    # Fix 6: флаг enqueued (board["queued"] — список — НЕ затирается)
    assert data.get("enqueued") is True, f"Должно быть enqueued=True: {data}"
    assert isinstance(data.get("queued"), list), f"queued должен быть списком: {data}"
    assert "aabbcc" in data["queued"], f"board queued должен содержать карточку: {data}"

    # Карточка должна быть в очереди (in-memory)
    q = _queue_for("1001:42")
    assert "aabbcc" in q, f"Карточка должна быть в очереди, очередь: {q}"


async def test_move_to_in_progress_not_busy_starts_immediately(aiohttp_client, tmp_path):
    """api_move_task to in_progress при свободном проекте с run_engine → 200, lock занят."""
    _set_queue_path(tmp_path)
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    _make_tasks_with_cards(project_dir, backlog=[{"id": "aabbcc", "text": "Task"}])

    async def fake_engine(**kw):
        await asyncio.sleep(100)  # долгий, чтобы lock не снялся до ответа
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
    # Карточки НЕ должно быть в очереди (она сразу запущена)
    q = _queue_for("1001:42")
    assert "aabbcc" not in q


# ─────────────────────────── F: queue exposed in GET /tasks ───────────────────────────


async def test_get_tasks_includes_queued(aiohttp_client, tmp_path):
    """GET /tasks возвращает поле 'queued' с card_id из очереди."""
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
    assert "queued" in data, f"Поле 'queued' должно быть в ответе: {data.keys()}"
    assert "aabbcc" in data["queued"]


async def test_get_tasks_queued_empty_by_default(aiohttp_client, tmp_path):
    """GET /tasks без очереди → queued=[]."""
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
    """DELETE карточки → карточка удаляется из очереди."""
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
    """Перенос карточки (backlog→review) → убирает из очереди."""
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

"""
Spec-012 Ф0 — тесты инкрементального сканера (Tasks A, B, C).

Task A: high-water-mark fingerprint
  - Второй скан того же вывода → 0 новых ошибок (fingerprint работает)
  - Добавление новой строки ошибки → ровно одна новая ошибка
  - Первый скан: парсятся только последние 50 строк, fingerprint сохраняется

Task B: dismissed-incidents TTL
  - _dismissed_add(h) → _ingest_errors_to_board не создаёт карточку для h (в пределах TTL)
  - После истечения TTL (инъекция старого ts) карточка создаётся снова
  - Удаление err-карточки (DELETE) → hash пишется в dismissed
  - Перенос err-карточки в done (PATCH move to="done") → hash пишется в dismissed

Task C: scan interval
  - _SCAN_INTERVAL_SEC дефолт = 60 (не 300)

Corrupt/missing state: helpers возвращают {} и не крашат сканер
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
    """Инициализирует _SCAN_STATE_PATH и _DISMISSED_PATH в реальную tmp_path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    ctx = {
        "DATA": data_dir,
        # остальные поля для _scan_state_init не нужны
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
    """Fingerprint строки — sha1."""
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()


# ─────────────────────────────────────────────────────────────────
# Task C: scan interval default
# ─────────────────────────────────────────────────────────────────


def test_scan_interval_default_is_60():
    """_SCAN_INTERVAL_SEC дефолт = 60 (снижен со Spec-012 Ф0). Безусловно: если
    env-override задан — значение должно совпасть с ним, иначе — дефолт 60."""
    import os
    override = os.environ.get("ERROR_SCAN_INTERVAL")
    expected = int(override) if override else 60
    assert _SCAN_INTERVAL_SEC == expected, (
        f"Ожидали {expected}, получили {_SCAN_INTERVAL_SEC}. Spec-012 Ф0: default=60 (было 300)."
    )


# ─────────────────────────────────────────────────────────────────
# Task A helpers: _scan_state_load / _scan_state_save
# ─────────────────────────────────────────────────────────────────


def test_scan_state_load_missing_file(tmp_path):
    """Файл отсутствует → возвращаем {}."""
    _setup_state_paths(tmp_path)
    result = _scan_state_load()
    assert result == {}


def test_scan_state_load_corrupt_file(tmp_path):
    """Битый JSON → возвращаем {} (не крашим)."""
    data_dir = _setup_state_paths(tmp_path)
    (data_dir / "scan_state.json").write_text("NOT JSON{{", encoding="utf-8")
    result = _scan_state_load()
    assert result == {}


def test_scan_state_save_and_load_round_trip(tmp_path):
    """Сохранить → загрузить → тот же dict."""
    _setup_state_paths(tmp_path)
    state = {"/home/proj": {"last_line": "abc123", "last_scan_ts": 1234567890.0}}
    _scan_state_save(state)
    loaded = _scan_state_load()
    assert loaded == state


def test_scan_state_save_with_none_path_no_crash():
    """_scan_state_save(state) при _SCAN_STATE_PATH=None → тихий пропуск."""
    orig = webapp._SCAN_STATE_PATH
    try:
        webapp._SCAN_STATE_PATH = None
        _scan_state_save({"key": "val"})  # не должно бросить исключение
    finally:
        webapp._SCAN_STATE_PATH = orig


def test_scan_state_load_with_none_path_returns_empty():
    """_scan_state_load() при _SCAN_STATE_PATH=None → {}."""
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
    """Файл dismissed отсутствует → {}."""
    _setup_state_paths(tmp_path)
    assert _dismissed_load() == {}


def test_dismissed_load_corrupt_file(tmp_path):
    """Битый JSON → {} (не крашим)."""
    data_dir = _setup_state_paths(tmp_path)
    (data_dir / "dismissed_incidents.json").write_text("BADJSON", encoding="utf-8")
    assert _dismissed_load() == {}


def test_dismissed_add_and_is_active(tmp_path):
    """После _dismissed_add(h) → _dismissed_is_active(h, now) = True."""
    _setup_state_paths(tmp_path)
    h = "abc123"
    _dismissed_add(h)
    assert _dismissed_is_active(h, time.time()) is True


def test_dismissed_is_active_returns_false_after_ttl(tmp_path):
    """Запись с ts далеко в прошлом (>TTL) → _dismissed_is_active = False."""
    data_dir = _setup_state_paths(tmp_path)
    h = "deadbeef"
    old_ts = time.time() - webapp._DISMISS_TTL - 1  # за пределами TTL
    (data_dir / "dismissed_incidents.json").write_text(
        json.dumps({h: old_ts}), encoding="utf-8"
    )
    assert _dismissed_is_active(h, time.time()) is False


def test_dismissed_is_active_unknown_hash(tmp_path):
    """Неизвестный hash → False."""
    _setup_state_paths(tmp_path)
    assert _dismissed_is_active("nothash", time.time()) is False


def test_dismissed_add_prunes_old_entries(tmp_path):
    """_dismissed_add прунит записи старше TTL."""
    data_dir = _setup_state_paths(tmp_path)
    h_old = "oldentry"
    h_new = "newentry"
    old_ts = time.time() - webapp._DISMISS_TTL - 100
    (data_dir / "dismissed_incidents.json").write_text(
        json.dumps({h_old: old_ts}), encoding="utf-8"
    )
    _dismissed_add(h_new)
    data = _dismissed_load()
    assert h_old not in data, "Старые записи должны быть вычищены при _dismissed_add"
    assert h_new in data


def test_dismissed_save_with_none_path_no_crash():
    """_dismissed_save при _DISMISSED_PATH=None → тихий пропуск."""
    orig = webapp._DISMISSED_PATH
    try:
        webapp._DISMISSED_PATH = None
        _dismissed_save({"k": 1.0})  # не должно бросить
    finally:
        webapp._DISMISSED_PATH = orig


def test_dismissed_add_with_none_path_no_crash():
    """_dismissed_add при _DISMISSED_PATH=None → тихий пропуск."""
    orig = webapp._DISMISSED_PATH
    try:
        webapp._DISMISSED_PATH = None
        _dismissed_add("abc")  # не должно бросить
    finally:
        webapp._DISMISSED_PATH = orig


# ─────────────────────────────────────────────────────────────────
# Task A: fingerprint в _scan_project_errors
# ─────────────────────────────────────────────────────────────────


async def test_fingerprint_second_scan_same_output_yields_no_new_errors(tmp_path):
    """Второй скан тех же строк → 0 новых ошибок (fingerprint работает)."""
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
        # Первый скан — устанавливает fingerprint
        errors1 = await webapp._scan_project_errors(project)

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        # Второй скан тех же строк — fingerprint нашёлся, после него ничего нет
        errors2 = await webapp._scan_project_errors(project)

    assert errors2 == [], (
        f"Второй скан тех же строк должен дать 0 ошибок, получили {len(errors2)}"
    )


async def test_fingerprint_repeated_line_does_not_skip_new_error(tmp_path):
    """Регрессия BLOCKER (block-fingerprint): повторяющаяся строка (heartbeat) как
    конец прошлого скана НЕ должна прятать новую ошибку, появившуюся МЕЖДУ двумя её
    копиями. Single-line fingerprint брал последнее вхождение → терял ошибку."""
    _setup_state_paths(tmp_path)
    project = {"cwd": str(tmp_path / "hb"), "log_cmd": "dummy"}

    scan1 = ["INFO a", "INFO b", "INFO c", "INFO d", "INFO e", "heartbeat ping"]
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(scan1))):
        await webapp._scan_project_errors(project)

    # Новая ошибка появилась, затем СНОВА та же heartbeat-строка
    scan2 = scan1 + ["ERROR: disk full", "heartbeat ping"]
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(scan2))):
        errors = await webapp._scan_project_errors(project)

    msgs = [e.get("message", "") for e in errors]
    assert any("disk full" in m for m in msgs), (
        f"Новая ошибка между двумя heartbeat НЕ должна теряться. Errors: {errors}"
    )


async def test_delete_then_rescan_does_not_resurrect_e2e(tmp_path):
    """E2E идентичность: ingest создаёт карточку err-<h> → берём hash ИЗ id карточки
    (как делает api_delete_task: card_id[4:]) → dismiss → повторный ingest той же
    ошибки НЕ воскрешает. Доказывает card_id[4:] == err['hash']."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "e2e"
    cwd.mkdir()
    _make_empty_board(cwd)
    err = _make_error("kaboom", etype="ValueError")

    added, _ = await _ingest_errors_to_board(str(cwd), "e2e", [err])
    assert added == 1

    # Берём id созданной карточки и извлекаем hash тем же срезом, что и delete-роут
    _, _, cols = _load_board(str(cwd))
    card_id = cols["failed"][0]["id"]
    assert card_id.startswith("err-")
    _dismissed_add(card_id[4:])           # то же, что api_delete_task на err-карточке

    # Убираем карточку с доски (как удаление), затем повторно ingest той же ошибки
    cols["failed"].clear()
    webapp._save_board(str(cwd), "e2e", "", cols)
    added2, _ = await _ingest_errors_to_board(str(cwd), "e2e", [err])
    assert added2 == 0, "dismissed-инцидент не должен воскреснуть (card_id[4:] == hash)"


# ─────────────────────────────────────────────────────────────────
# Spec-012 Ф1: in-process push своих ошибок кокпита (_report_incident)
# ─────────────────────────────────────────────────────────────────


async def test_report_incident_creates_card_and_dedups_with_scanner(tmp_path, monkeypatch):
    """Ф1: _report_incident создаёт карточку мгновенно; её hash СОВПАДАЕТ с тем, что
    даёт лог-сканер на строке `UNHANDLED exc_class=.. path=..` → дедуп (сканер не
    задваивает, только бампит seen)."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "cops"
    cwd.mkdir()
    _make_empty_board(cwd)
    fake_proj = {"cwd": str(cwd), "name": "claude-ops-bot"}
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda *a, **k: fake_proj)
    webapp._REPORT_DEBOUNCE.clear()

    # In-process репорт (как из error_middleware)
    await webapp._report_incident({}, "ValueError", "/api/x")
    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 1, "карточка должна быть создана мгновенно"
    card_id = cols["failed"][0]["id"]

    # Сканер парсит ТУ ЖЕ строку UNHANDLED → тот же hash → дедуп
    scanner_errs = webapp._parse_log_errors(
        "2026-06-04 ERROR root UNHANDLED exc_class=ValueError path=/api/x request_id=ab12",
        source="log",
    )
    assert len(scanner_errs) == 1, f"ожидали 1 UNHANDLED-ошибку, получили {scanner_errs}"
    assert f"err-{scanner_errs[0]['hash']}" == card_id, "hash должен совпасть со сканером"

    added, updated = await _ingest_errors_to_board(str(cwd), "claude-ops-bot", scanner_errs)
    assert added == 0 and updated == 1, "сканер не задваивает — только bump seen"


async def test_report_incident_no_project_is_silent(tmp_path, monkeypatch):
    """Если проект не резолвится — тихо, без исключения."""
    _setup_state_paths(tmp_path)
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda *a, **k: None)
    webapp._REPORT_DEBOUNCE.clear()
    await webapp._report_incident({}, "ValueError", "/api/x")  # не должно бросить


async def test_report_incident_debounce_collapses_flood(tmp_path, monkeypatch):
    """Ф1 hardening: один и тот же инцидент чаще раза в дебаунс-окно in-process не
    пишется → эндпоинт, падающий на каждом запросе, не устроит I/O-шторм в TASKS.md."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "deb"
    cwd.mkdir()
    _make_empty_board(cwd)
    monkeypatch.setattr(webapp, "_find_project_by_id", lambda *a, **k: {"cwd": str(cwd), "name": "deb"})
    webapp._REPORT_DEBOUNCE.clear()

    for _ in range(5):  # 5 быстрых репортов одной ошибки
        await webapp._report_incident({}, "FloodError", "/api/flood")

    _, _, cols = _load_board(str(cwd))
    assert len(cols["failed"]) == 1, "дебаунс: одна карточка, не 5 записей"


async def test_fingerprint_appended_line_yields_new_error(tmp_path):
    """Если после fingerprint появилась новая строка с ошибкой → ровно одна новая ошибка."""
    _setup_state_paths(tmp_path)

    base_lines = [
        "INFO server started",
        "INFO all good",
        "INFO nothing wrong",
    ]
    new_error_line = "ERROR: out of memory"

    project = {"cwd": str(tmp_path / "proj2"), "log_cmd": "dummy_cmd"}

    # Первый скан (устанавливает fingerprint на base_lines)
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(base_lines))):
        await webapp._scan_project_errors(project)

    # Второй скан: добавлена новая строка ошибки
    extended_lines = base_lines + [new_error_line]
    with patch("webapp._run_log_cmd", new=AsyncMock(return_value="\n".join(extended_lines))):
        errors2 = await webapp._scan_project_errors(project)

    # Должна быть РОВНО одна ошибка — только новая строка (high-water-mark не
    # перепарсивает base_lines).
    assert len(errors2) == 1, f"Ожидали ровно 1 новую ошибку, получили {errors2}"
    assert "out of memory" in errors2[0].get("message", ""), (
        f"Новая ошибка 'out of memory' должна быть в results: {errors2}"
    )


async def test_first_scan_uses_last_50_lines(tmp_path):
    """Первый скан: берём только 50 строк хвоста, не весь вывод."""
    _setup_state_paths(tmp_path)

    # Создаём 200 строк, ошибки только в первых 100 (НЕ в хвосте 50)
    old_error = "ERROR: ancient error that should not be scanned"
    new_info = "INFO: recent normal line"

    lines = [old_error] * 100 + [new_info] * 100
    log_text = "\n".join(lines)

    project = {"cwd": str(tmp_path / "proj3"), "log_cmd": "dummy_cmd"}

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        errors = await webapp._scan_project_errors(project)

    # В последних 50 строках — только INFO, ошибок нет
    assert len(errors) == 0, (
        f"Первый скан должен брать только хвост 50 строк, "
        f"где нет ERROR. Получили {len(errors)} ошибок."
    )


async def test_first_scan_saves_fingerprint(tmp_path):
    """После первого скана fingerprint сохранён в scan_state.json."""
    _setup_state_paths(tmp_path)

    lines = ["INFO line 1", "INFO line 2", "INFO final line"]
    log_text = "\n".join(lines)

    project = {"cwd": str(tmp_path / "proj4"), "log_cmd": "dummy_cmd"}

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        await webapp._scan_project_errors(project)

    state = _scan_state_load()
    assert str(tmp_path / "proj4") in state, "fingerprint должен быть сохранён по cwd"
    proj_state = state[str(tmp_path / "proj4")]
    assert "block" in proj_state
    assert "last_scan_ts" in proj_state

    # Блок-отпечаток = sha1 последних N строк (здесь все 3, т.к. их < N=6)
    assert proj_state["block"] == [_fp("INFO line 1"), _fp("INFO line 2"), _fp("INFO final line")]


async def test_fingerprint_rotation_fallback(tmp_path):
    """Если fingerprint не найден в новом выводе (ротация) → fallback 500 строк."""
    _setup_state_paths(tmp_path)

    project = {"cwd": str(tmp_path / "proj5"), "log_cmd": "dummy_cmd"}

    # Устанавливаем блок-отпечаток, которого НЕ будет в новом выводе (ротация)
    state = {str(tmp_path / "proj5"): {"block": [_fp("old rotated line A"), _fp("old rotated line B")], "last_scan_ts": 1.0}}
    _scan_state_save(state)

    # Новый вывод не содержит старый блок
    new_lines = ["INFO new server start", "ERROR: new error after rotation"]
    log_text = "\n".join(new_lines)

    with patch("webapp._run_log_cmd", new=AsyncMock(return_value=log_text)):
        errors = await webapp._scan_project_errors(project)

    # Fallback: весь вывод (≤500) парсится → конкретная ошибка найдена
    assert any("new error after rotation" in e.get("message", "") for e in errors), (
        f"После ротации должен быть fallback-парсинг всего вывода. Errors: {errors}"
    )


# ─────────────────────────────────────────────────────────────────
# Task B: dismissed в _ingest_errors_to_board
# ─────────────────────────────────────────────────────────────────


async def test_dismissed_hash_not_recreated_in_ingest(tmp_path):
    """Dismissed hash → _ingest_errors_to_board НЕ создаёт карточку."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "projb1"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("dismissed error", "DismissedError")
    h = err["hash"]

    # Записываем как dismissed
    _dismissed_add(h)

    # Пытаемся создать карточку
    added, updated = await _ingest_errors_to_board(str(cwd), "projb1", [err])

    assert added == 0, f"dismissed hash не должен добавляться в доску, added={added}"
    assert updated == 0


async def test_dismissed_hash_recreated_after_ttl(tmp_path):
    """После истечения TTL dismissed-hash снова создаёт карточку."""
    data_dir = _setup_state_paths(tmp_path)
    cwd = tmp_path / "projb2"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("ttl expired error", "ExpiredError")
    h = err["hash"]

    # Пишем dismissed с timestamp за пределами TTL
    old_ts = time.time() - webapp._DISMISS_TTL - 10
    (data_dir / "dismissed_incidents.json").write_text(
        json.dumps({h: old_ts}), encoding="utf-8"
    )

    added, updated = await _ingest_errors_to_board(str(cwd), "projb2", [err])

    assert added == 1, f"После TTL должна быть создана карточка, added={added}"


async def test_existing_card_not_affected_by_dismissed(tmp_path):
    """Если карточка уже существует на доске (not dismissed), обновляем seen — не блокируем."""
    _setup_state_paths(tmp_path)
    cwd = tmp_path / "projb3"
    cwd.mkdir()
    _make_empty_board(cwd)

    err = _make_error("existing card error", "ExistingError")
    h = err["hash"]

    # Первый ingest — создаёт карточку
    await _ingest_errors_to_board(str(cwd), "projb3", [err])

    # Добавляем hash в dismissed
    _dismissed_add(h)

    # Второй ingest — карточка СУЩЕСТВУЕТ, dismissed НЕ блокирует update
    added, updated = await _ingest_errors_to_board(str(cwd), "projb3", [err])

    assert updated == 1, "Existing card must be updated (seen++) even if hash is dismissed"
    assert added == 0


# ─────────────────────────────────────────────────────────────────
# Task B: dismiss через API (DELETE и move-to-done)
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def project_dir_b(tmp_path):
    pdir = tmp_path / "testproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_dismissed(tmp_path, project_dir_b):
    """ctx с одним проектом; _SCAN_STATE_PATH/_DISMISSED_PATH инициализированы."""
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
    """Создаёт TASKS.md с одной err-карточкой в Failed."""
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
    """DELETE err-карточки → hash пишется в dismissed_incidents."""
    err_hash = "ab12cd"
    _make_tasks_md_with_err_card(project_dir_b, err_hash)

    client = await aiohttp_client(board_app_dismissed)
    auth = {"Cookie": f"cops_auth={fake_ctx_dismissed['_auth_token']}"}

    resp = await client.delete(f"/api/projects/testproject/tasks/err-{err_hash}", headers=auth)
    assert resp.status == 200

    # Проверяем что hash записан в dismissed
    assert _dismissed_is_active(err_hash, time.time()), (
        f"После DELETE err-карточки hash {err_hash!r} должен быть в dismissed"
    )


async def test_move_err_card_to_done_records_dismissed(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """MOVE err-карточки to=done → hash пишется в dismissed_incidents."""
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
        f"После move-to-done err-карточки hash {err_hash!r} должен быть в dismissed"
    )


async def test_move_regular_card_to_done_does_not_affect_dismissed(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """Обычная (не err-) карточка move-to-done → dismissed НЕ меняется."""
    # Создаём обычную карточку
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

    # dismissed для 'aabbcc' не был записан (это не err-карточка)
    assert not _dismissed_is_active("aabbcc", time.time()), (
        "Обычная карточка не должна попадать в dismissed"
    )


async def test_delete_nonexistent_card_returns_404(
    aiohttp_client, board_app_dismissed, fake_ctx_dismissed, project_dir_b
):
    """DELETE несуществующей карточки → 404, dismissed не меняется."""
    # Создаём пустую доску
    _make_empty_board(project_dir_b)

    client = await aiohttp_client(board_app_dismissed)
    auth = {"Cookie": f"cops_auth={fake_ctx_dismissed['_auth_token']}"}

    resp = await client.delete(
        "/api/projects/testproject/tasks/err-ffffff", headers=auth
    )
    assert resp.status == 404
    # dismissed НЕ записан — карточки не было
    assert not _dismissed_is_active("ffffff", time.time())

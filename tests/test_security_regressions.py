"""
Регрессии безопасности — закрепляем поведение:

1. Невалидный card_id (../path, слишком длинный, спецсимволы) → 400
   на эндпоинтах: api_card_run, api_move_task, api_delete_task, api_update_task.
2. Rate-limit /api/login → 429 после 5 неудачных попыток с одного IP.
3. _valid_card_id: юнит на граничные значения.

Дополняет test_security.py (path-traversal в _resolve_safe) без дублирования.
"""
import sys
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _valid_card_id, _login_attempts, _tasks_path


# ─────────────────────────── юнит: _valid_card_id ───────────────────────────


@pytest.mark.parametrize("card_id", [
    "aabb",          # 4 символа — минимум
    "aabbcc",        # обычный hex
    "12345678",      # цифры
    "aabbcc-1234",   # с дефисом
    "a-b-c-d-e-f",  # много дефисов
    "1234567890abcd", # 14 символов
    "a" * 20,        # 20 символов — максимум
    "err-9b37ae",    # инцидент-карточка: префикс err- + hash6
    "err-aabbcc",    # инцидент-карточка
])
def test_valid_card_id_valid(card_id: str):
    """Валидные card_id должны проходить."""
    assert _valid_card_id(card_id), f"card_id {card_id!r} должен быть валидным"


@pytest.mark.parametrize("card_id,reason", [
    ("",                  "пустая строка"),
    ("abc",               "3 символа — меньше минимума 4"),
    ("a" * 21,            "21 символ — больше максимума 20"),
    ("../etc/passwd",     "path traversal с ../"),
    ("../../root",        "path traversal с ../../"),
    ("abc!def",           "спецсимвол !"),
    ("abc def",           "пробел"),
    ("abc/def",           "слеш"),
    ("abc\\def",          "обратный слеш"),
    ("ABCDEF",            "заглавные буквы (вне [a-f0-9-])"),
    ("xyz123",            "буквы g-z не в hex"),
    ("abc\ndef",          "перенос строки"),
    ("err-../x",          "err- + traversal"),
    ("err-ABCDEF",        "err- + заглавные"),
])
def test_valid_card_id_invalid(card_id: str, reason: str):
    """Невалидные card_id должны быть отклонены."""
    assert not _valid_card_id(card_id), (
        f"card_id {card_id!r} должен быть НЕВАЛИДНЫМ ({reason})"
    )


# ─────────────────────────── fixtures для API тестов ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "secproj"
    pdir.mkdir()
    return pdir


@pytest.fixture
def sec_ctx(tmp_path, project_dir):
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "secproj",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
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
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


@pytest.fixture
def sec_app(sec_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = sec_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/tasks/{card}/run", _webapp.api_card_run)
    app.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    app.router.add_delete("/api/projects/{id}/tasks/{card}", _webapp.api_delete_task)
    app.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", _webapp.api_update_task)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _make_empty_board(project_dir: Path) -> None:
    _tasks_path(str(project_dir)).write_text(
        "# Tasks\n\n## Backlog\n\n## In Progress\n\n## Review\n\n## Failed\n",
        encoding="utf-8",
    )


# ─────────────────────────── невалидные card_id → 400 ───────────────────────────


INVALID_CARD_IDS = [
    "toolongcardidthatexceedslimit",  # > 20 символов
    "INVALID",                         # заглавные буквы (вне hex)
    # Note: "abc!@#" не тестируем через HTTP — aiohttp routing strips '#' как URL-fragment,
    # что даёт 405 от роутера, а не 400 от хендлера. Это корректно: URL с '#' недостижим.
    # Юнит-тест _valid_card_id уже покрывает спецсимволы напрямую.
]


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_card_run_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """GET /tasks/{card}/run с невалидным card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.get(f"/api/projects/secproj/tasks/{bad_id}/run", headers=h)
    assert resp.status == 400, (
        f"card_id {bad_id!r} должен дать 400 в /run, получили: {resp.status}"
    )


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_delete_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """DELETE /tasks/{card} с невалидным card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.delete(f"/api/projects/secproj/tasks/{bad_id}", headers=h)
    assert resp.status == 400, (
        f"card_id {bad_id!r} должен дать 400 в DELETE, получили: {resp.status}"
    )


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_move_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """POST /tasks/{card}/move с невалидным card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.post(
        f"/api/projects/secproj/tasks/{bad_id}/move",
        json={"to": "review"},
        headers=h,
    )
    assert resp.status == 400, (
        f"card_id {bad_id!r} должен дать 400 в /move, получили: {resp.status}"
    )


@pytest.mark.parametrize("bad_id", INVALID_CARD_IDS)
async def test_update_bad_card_id_returns_400(aiohttp_client, sec_app, sec_ctx, project_dir, bad_id):
    """PATCH /tasks/{card} с невалидным card_id → 400."""
    _make_empty_board(project_dir)
    client = await aiohttp_client(sec_app)
    h = _auth_headers(sec_ctx)

    resp = await client.patch(
        f"/api/projects/secproj/tasks/{bad_id}",
        json={"text": "something"},
        headers=h,
    )
    assert resp.status == 400, (
        f"card_id {bad_id!r} должен дать 400 в PATCH, получили: {resp.status}"
    )


# ─────────────────────────── rate-limit /api/login ───────────────────────────


@pytest.fixture(autouse=False)
def clean_login_attempts():
    """Очищаем глобальный словарь попыток перед и после теста."""
    _login_attempts.clear()
    yield
    _login_attempts.clear()


async def test_login_rate_limit_triggers_after_5_fails(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """5 неудачных попыток с одного IP → 6-я даёт 429."""
    client = await aiohttp_client(sec_app)

    # 5 неудачных попыток
    for i in range(5):
        resp = await client.post("/api/login", json={"password": f"wrong{i}"})
        assert resp.status == 401, f"Попытка {i+1} должна вернуть 401, получили: {resp.status}"

    # 6-я должна быть заблокирована
    resp = await client.post("/api/login", json={"password": "anythingwrong"})
    assert resp.status == 429, f"После 5 неудач должен вернуть 429, получили: {resp.status}"
    data = await resp.json()
    assert "too many" in data.get("error", "").lower() or "429" in str(resp.status)


async def test_login_rate_limit_correct_password_still_blocked(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """После 5 неудач даже правильный пароль заблокирован (rate-limit по IP)."""
    client = await aiohttp_client(sec_app)

    # 5 неудачных попыток
    for i in range(5):
        await client.post("/api/login", json={"password": f"wrong{i}"})

    # Правильный пароль тоже заблокирован
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 429, (
        "После 5 неудач rate-limit должен блокировать даже правильный пароль"
    )


async def test_login_rate_limit_not_triggered_before_5_fails(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """До 5 неудач rate-limit не срабатывает."""
    client = await aiohttp_client(sec_app)

    # 4 неудачные попытки
    for i in range(4):
        resp = await client.post("/api/login", json={"password": f"wrong{i}"})
        assert resp.status == 401, f"Попытка {i+1}: ожидали 401, получили {resp.status}"

    # 5-я попытка — ещё не заблокирована (достигает счётчик 5, но 6-я блокируется)
    resp = await client.post("/api/login", json={"password": "wrong4"})
    assert resp.status == 401, f"5-я попытка ещё должна дать 401 (не 429), получили: {resp.status}"


async def test_login_success_does_not_trigger_rate_limit(aiohttp_client, sec_app, sec_ctx, clean_login_attempts):
    """Успешная попытка не считается как неудачная — после неё новые 5 неудач нужны для бана."""
    client = await aiohttp_client(sec_app)

    # Успешный логин
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 200

    # 4 неудачных попытки (rate-limit не должен сработать)
    for i in range(4):
        resp = await client.post("/api/login", json={"password": f"wrong{i}"})
        assert resp.status == 401


# ─────────────────────────── без авторизации ───────────────────────────


async def test_api_requires_auth(aiohttp_client, sec_app, sec_ctx, project_dir):
    """Все /api/* эндпоинты кроме /health и /login требуют авторизацию."""
    client = await aiohttp_client(sec_app)

    # Без cookie → 401
    resp = await client.get("/api/projects/secproj/tasks/aabbcc/run")
    assert resp.status == 401

    resp = await client.post("/api/projects/secproj/tasks/aabbcc/move", json={"to": "review"})
    assert resp.status == 401

    resp = await client.delete("/api/projects/secproj/tasks/aabbcc")
    assert resp.status == 401

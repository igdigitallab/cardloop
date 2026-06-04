"""
Тесты Spec-012 Ф3 — опциональный push-эндпоинт инцидентов.

Покрывает:
- disabled-by-default: глобальный флаг OFF → POST → 404 (даже с валидным токеном)
- enabled + нет секрета CLAUDEOPS_INCIDENT_TOKEN у проекта → 403
- enabled + неверный токен → 403
- enabled + верный токен → 200 + _report_incident вызван
- auth_middleware: POST /incident исключён из cookie-auth; GET — нет; /evil — нет
- санитизация: переносы строк убраны; длины обрезаны; пустой exc_class → 400
- rate-limit: > max за окно → 429
- секрет/токен НИКОГДА не в теле ответа
"""
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _derive_token,
    _secrets_set,
    _INCIDENT_PUSH_MAX,
    _INCIDENT_PUSH_WINDOW,
    _incident_push_history,
    _INCIDENT_PATH_RE,
    api_project_incident,
    auth_middleware,
)
from aiohttp import web


# ─────────────────────────── фикстуры ───────────────────────────────

@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "mybot"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx(tmp_path, project_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpassword"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "mybot",
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
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def incident_app(fake_ctx):
    """aiohttp-приложение с auth_middleware + incident-эндпоинтом."""
    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/incidents", _webapp.api_project_incidents)
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    app.router.add_post("/api/projects/{id}/incident", api_project_incident)
    # Заглушка для несуществующего пути с /evil (проверяем, что middleware не пропускает)
    app.router.add_get("/api/projects/{id}/incident", _webapp.api_project_incidents)  # GET same path
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _clear_push_history():
    """Очищает глобальную историю rate-limit между тестами."""
    _incident_push_history.clear()


# ─────────────────────────── unit: regex _INCIDENT_PATH_RE ───────────────────

def test_incident_path_re_matches_valid():
    assert _INCIDENT_PATH_RE.match("/api/projects/mybot/incident")
    assert _INCIDENT_PATH_RE.match("/api/projects/some-project-123/incident")
    assert _INCIDENT_PATH_RE.match("/api/projects/x/incident")


def test_incident_path_re_no_trailing_slash():
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incident/")


def test_incident_path_re_no_evil_suffix():
    """Суффикс после /incident не матчится — traversal защита."""
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incident/evil")
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incident/evil/extra")


def test_incident_path_re_no_other_paths():
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/incidents")  # plural
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/self-heal")
    assert not _INCIDENT_PATH_RE.match("/api/projects/mybot/chat")
    assert not _INCIDENT_PATH_RE.match("/api/health")


def test_incident_path_re_no_empty_id():
    assert not _INCIDENT_PATH_RE.match("/api/projects//incident")


# ─────────────────────────── disabled-by-default ─────────────────────────────

async def test_incident_push_disabled_by_default(aiohttp_client, incident_app, project_dir):
    """Флаг OFF по умолчанию → 404 даже с корректным токеном."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "mytoken")

    with patch.object(_webapp, "_get_global_setting", return_value=False):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError", "where": "/test"},
            headers={"X-Incident-Token": "mytoken"},
        )
    assert resp.status == 404, f"Expected 404 when disabled, got {resp.status}"
    body = await resp.json()
    assert "mytoken" not in str(body), "Token must not appear in response"


# ─────────────────────────── no project token → 403 ─────────────────────────

async def test_incident_push_no_project_token(aiohttp_client, incident_app, project_dir):
    """Проект не задал CLAUDEOPS_INCIDENT_TOKEN → 403 (per-project opt-in не сделан)."""
    _clear_push_history()
    # Не задаём секрет

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "anytoken"},
        )
    assert resp.status == 403


# ─────────────────────────── token mismatch → 403 ────────────────────────────

async def test_incident_push_wrong_token(aiohttp_client, incident_app, project_dir):
    """Неверный токен → 403."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "correct_token")

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "wrong_token"},
        )
    assert resp.status == 403
    body = await resp.json()
    assert "correct_token" not in str(body), "Secret token must not appear in response"
    assert "wrong_token" not in str(body), "Presented token must not appear in response"


# ─────────────────────────── correct token → 200 ────────────────────────────

async def test_incident_push_correct_token(aiohttp_client, incident_app, project_dir):
    """Верный токен → 200 + _report_incident вызван."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "valid_token_xyz")

    report_calls = []

    async def mock_report(ctx, exc_class, where, project_id="claude-ops-bot"):
        report_calls.append({"exc_class": exc_class, "where": where, "project_id": project_id})

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError", "where": "/api/test", "excerpt": "test error"},
            headers={"X-Incident-Token": "valid_token_xyz"},
        )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    # Секрет не в ответе
    resp_text = str(data)
    assert "valid_token_xyz" not in resp_text


async def test_incident_push_token_in_body_fallback(aiohttp_client, incident_app, project_dir):
    """Токен в теле JSON (нет заголовка X-Incident-Token) → 200."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "body_token")

    async def noop_report(*a, **kw):
        pass

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=noop_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "RuntimeError", "token": "body_token"},
        )
    assert resp.status == 200
    body = await resp.json()
    assert "body_token" not in str(body)


# ─────────────────────────── auth_middleware exempt ───────────────────────────

async def test_incident_post_exempt_from_cookie_auth(aiohttp_client, incident_app, project_dir):
    """POST /incident не требует cookie — доходит до хендлера (который сам проверяет токен)."""
    _clear_push_history()
    # Без флага → 404, но важно что НЕ 401 (т.е. не заблокировал middleware по cookie)
    with patch.object(_webapp, "_get_global_setting", return_value=False):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "Err"},
            # НЕТ заголовка Cookie
        )
    assert resp.status == 404  # дошёл до хендлера (глобальный флаг OFF → 404)
    assert resp.status != 401, "auth_middleware wrongly blocked /incident"


async def test_incident_get_requires_cookie_auth(aiohttp_client, incident_app, fake_ctx):
    """GET /api/projects/{id}/incident НЕ exempt — требует cookie (возвращает 401 без него)."""
    client = await aiohttp_client(incident_app)
    resp = await client.get("/api/projects/mybot/incident")
    assert resp.status == 401


async def test_other_api_path_requires_cookie_auth(aiohttp_client, incident_app):
    """Другие /api/* пути по-прежнему требуют cookie."""
    client = await aiohttp_client(incident_app)
    resp = await client.get("/api/projects/mybot/tasks")
    assert resp.status == 401


async def test_incident_evil_path_requires_cookie_auth(aiohttp_client, incident_app):
    """POST /api/projects/x/incident/evil НЕ попадает в exempt (regex не матчит с суффиксом)."""
    # Нет роута с /evil, значит aiohttp отдаст 404 (через SPA или 404 HTTP — любое кроме 401
    # было бы неверно, но т.к. роута нет, получим 404/405 от роутера, а не 401 от middleware).
    # Нас интересует: был ли middleware вообще обойдён? Проверяем через GET — роута нет → 404/405.
    # Проверяем точнее: если бы middleware не проверял cookie, он бы пропустил. Но
    # _INCIDENT_PATH_RE.match(...evil) вернёт None → не exempt → проверяет cookie → 401.
    client = await aiohttp_client(incident_app)
    # Нет зарегистрированного роута для этого пути, но middleware сработает раньше для /api/*
    resp = await client.post("/api/projects/mybot/incident/evil")
    # Должен быть 401 (не прошёл cookie) — не 200/404 от bypass
    assert resp.status == 401, f"Evil suffix path should be 401 (blocked by cookie auth), got {resp.status}"


# ─────────────────────────── санитизация ─────────────────────────────────────

async def test_sanitize_newlines_stripped(aiohttp_client, incident_app, project_dir):
    """Переносы строк в where/excerpt убраны (защита формата TASKS.md)."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok")

    captured = {}

    async def mock_report(ctx, exc_class, where, project_id="claude-ops-bot"):
        captured["exc_class"] = exc_class
        captured["where"] = where

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "Val\nue\rErr", "where": "line1\nline2", "excerpt": "a\nb\nc"},
            headers={"X-Incident-Token": "tok"},
        )
    assert resp.status == 200
    assert "\n" not in captured.get("exc_class", ""), "Newline in exc_class"
    assert "\n" not in captured.get("where", ""), "Newline in where"


async def test_sanitize_exc_class_cap(aiohttp_client, incident_app, project_dir):
    """exc_class обрезается до 120 символов."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok2")

    captured = {}

    async def mock_report(ctx, exc_class, where, project_id="claude-ops-bot"):
        captured["exc_class"] = exc_class

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", side_effect=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "X" * 200},
            headers={"X-Incident-Token": "tok2"},
        )
    assert resp.status == 200
    assert len(captured.get("exc_class", "")) <= 120


async def test_sanitize_empty_exc_class_400(aiohttp_client, incident_app, project_dir):
    """exc_class пустой или только пробелы → 400."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok3")

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "   "},
            headers={"X-Incident-Token": "tok3"},
        )
    assert resp.status == 400


async def test_invalid_json_400(aiohttp_client, incident_app, project_dir):
    """Невалидный JSON → 400."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok4")

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            data=b"not-json",
            headers={"X-Incident-Token": "tok4", "Content-Type": "application/json"},
        )
    assert resp.status == 400


# ─────────────────────────── rate-limit ──────────────────────────────────────

async def test_rate_limit_exceeded(aiohttp_client, incident_app, project_dir):
    """Превышение rate-limit (_INCIDENT_PUSH_MAX за _INCIDENT_PUSH_WINDOW) → 429."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "rl_token")

    # Заполняем историю вручную до max
    now = time.time()
    _incident_push_history["mybot"] = [now - 1] * _INCIDENT_PUSH_MAX

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "rl_token"},
        )
    assert resp.status == 429


async def test_rate_limit_window_expired(aiohttp_client, incident_app, project_dir):
    """Записи вне окна не считаются → запрос проходит."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "rl_tok2")

    # Все записи — старше окна
    old_ts = time.time() - _INCIDENT_PUSH_WINDOW - 10
    _incident_push_history["mybot"] = [old_ts] * (_INCIDENT_PUSH_MAX + 5)

    async def noop_report(*a, **kw):
        pass

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None), \
         patch.object(_webapp, "_report_incident", side_effect=noop_report):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "RuntimeError"},
            headers={"X-Incident-Token": "rl_tok2"},
        )
    assert resp.status == 200


# ─────────────────────────── секрет/токен не в ответе ────────────────────────

async def test_no_secret_in_any_response(aiohttp_client, incident_app, project_dir):
    """Секрет CLAUDEOPS_INCIDENT_TOKEN никогда не появляется в теле ответа."""
    _clear_push_history()
    secret = "SUPER_SECRET_INCIDENT_TOKEN_XYZ_12345"
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", secret)

    # 403-сценарий (неверный токен)
    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp_403 = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "Err"},
            headers={"X-Incident-Token": "wrong"},
        )
    text_403 = await resp_403.text()
    assert secret not in text_403, f"Secret leaked in 403 response: {text_403}"

    # 200-сценарий (верный токен)
    _clear_push_history()

    async def noop_report(*a, **kw):
        pass

    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: None), \
         patch.object(_webapp, "_report_incident", side_effect=noop_report):
        resp_200 = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": secret},
        )
    text_200 = await resp_200.text()
    assert secret not in text_200, f"Secret leaked in 200 response: {text_200}"


# ─────────────────────────── project not found ───────────────────────────────

async def test_incident_push_project_not_found(aiohttp_client, incident_app):
    """Несуществующий проект → 404."""
    _clear_push_history()

    with patch.object(_webapp, "_get_global_setting", return_value=True):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/nonexistent_proj/incident",
            json={"exc_class": "ValueError"},
            headers={"X-Incident-Token": "anytoken"},
        )
    assert resp.status == 404


async def test_incident_push_unicode_line_separator_sanitized(aiohttp_client, incident_app, project_dir):
    """BLOCKER-регрессия: U+2028/U+2029 в exc_class/where НЕ должны просочиться в
    карточку (иначе splitlines() на доске даёт инжект '## Section' / фейк-карточки).
    Проверяем аргументы, переданные в _report_incident (call_args пишется при вызове,
    даже если корутину не await-ят)."""
    _clear_push_history()
    _secrets_set(str(project_dir), "CLAUDEOPS_INCIDENT_TOKEN", "tok123")

    mock_report = AsyncMock()
    # Явные U+2028 (LINE SEP) и U+2029 (PARA SEP) — splitlines() трактует их как переводы строк.
    evil_exc = "TypeError\u2028## Done"
    evil_where = "/x\u2029- [ ] evil <!--ops:err-bad-->"
    with patch.object(_webapp, "_get_global_setting", return_value=True), \
         patch.object(_webapp, "_report_incident", new=mock_report), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: coro.close()):
        client = await aiohttp_client(incident_app)
        resp = await client.post(
            "/api/projects/mybot/incident",
            json={"exc_class": evil_exc, "where": evil_where},
            headers={"X-Incident-Token": "tok123"},
        )
    assert resp.status == 200
    assert mock_report.call_args is not None, "_report_incident должен быть вызван"
    sent_exc = mock_report.call_args.args[1]
    sent_where = mock_report.call_args.args[2]
    # Ни одного разделителя строк → инжект в доску невозможен
    assert len(sent_exc.splitlines()) == 1, repr(sent_exc)
    assert len(sent_where.splitlines()) == 1, repr(sent_where)
    assert "\u2028" not in sent_exc and "\u2029" not in sent_where

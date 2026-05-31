"""
Минимальные smoke-тесты веб-роутов через aiohttp.test_utils.

Тестируем только публичные/анонимные эндпоинты и auth-механизм.
SDK и PTB не инициализируются — только aiohttp-приложение с fake ctx.
"""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _make_token(password: str) -> str:
    return hashlib.sha256((password + "cops").encode()).hexdigest()


# ─── Пробуем собрать aiohttp-приложение из webapp.start ──────────────────────
# webapp.start() регистрирует роуты и возвращает app.
# Чтобы не поднимать реальный сервер — используем aiohttp.test_utils.TestClient.

@pytest.fixture
def fake_ctx_for_app(tmp_path):
    """ctx, достаточный для создания aiohttp-приложения."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
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
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }


@pytest.fixture
def web_app(fake_ctx_for_app):
    """Создаёт aiohttp.web.Application с роутами webapp без поднятия сервера."""
    import webapp
    from aiohttp import web

    app = web.Application(middlewares=[webapp.auth_middleware])
    app["ctx"] = fake_ctx_for_app

    # Регистрируем минимальный набор роутов вручную
    # (вместо вызова webapp.start() который требует event loop и настройки static)
    app.router.add_get("/api/health", webapp.api_health)
    app.router.add_post("/api/login", webapp.api_login)
    app.router.add_get("/api/projects", webapp.api_projects)
    app.router.add_get("/api/me", webapp.api_me)

    return app


# ─── тесты ───────────────────────────────────────────────────────────────────

async def test_health_no_auth(aiohttp_client, web_app):
    """GET /api/health без cookie → 200 (не требует auth)."""
    client = await aiohttp_client(web_app)
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True


async def test_projects_without_cookie_returns_401(aiohttp_client, web_app):
    """GET /api/projects без cookie → 401 Unauthorized."""
    client = await aiohttp_client(web_app)
    resp = await client.get("/api/projects")
    assert resp.status == 401


async def test_login_correct_password(aiohttp_client, web_app):
    """POST /api/login с правильным паролем → 200 + cookie cops_auth."""
    client = await aiohttp_client(web_app)
    resp = await client.post("/api/login", json={"password": "testpass"})
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True

    # Проверяем что cookie выставлена
    cookies = resp.cookies
    assert "cops_auth" in cookies, f"Cookie cops_auth должна быть в ответе, cookies={dict(cookies)}"
    expected_token = _make_token("testpass")
    assert cookies["cops_auth"].value == expected_token


async def test_login_wrong_password(aiohttp_client, web_app):
    """POST /api/login с неправильным паролем → 401."""
    client = await aiohttp_client(web_app)
    resp = await client.post("/api/login", json={"password": "wrongpass"})
    assert resp.status == 401


async def test_projects_with_valid_cookie(aiohttp_client, web_app):
    """GET /api/projects с валидным cookie → 200 + список проектов."""
    client = await aiohttp_client(web_app)

    # Сначала логинимся, чтобы получить cookie
    login_resp = await client.post("/api/login", json={"password": "testpass"})
    assert login_resp.status == 200

    # Теперь запрашиваем проекты с тем же клиентом (cookie сохраняется автоматически)
    resp = await client.get("/api/projects")
    assert resp.status == 200
    data = await resp.json()
    assert "projects" in data
    # ctx["topics"] пустой → projects пустой список
    assert data["projects"] == []


async def test_me_with_valid_cookie(aiohttp_client, web_app):
    """GET /api/me после логина → 200 {authed: true}."""
    client = await aiohttp_client(web_app)
    await client.post("/api/login", json={"password": "testpass"})
    resp = await client.get("/api/me")
    assert resp.status == 200
    data = await resp.json()
    assert data.get("authed") is True

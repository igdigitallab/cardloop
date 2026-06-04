"""
Регрессия error_middleware (spec-011 Ф0 + фикс SSE-disconnect).

Контекст: глобальный error_middleware ловит необработанные исключения хендлеров,
логирует стандартной строкой `UNHANDLED exc_class=... path=...` и отдаёт JSON 500.
Эту строку парсит сканер инцидентов (Ф1) → карточка в Failed.

Баг (пойман самим мониторингом): клиент закрывает SSE-вкладку → следующий
heartbeat-write в _sse_stream падает ClientConnectionResetError (subclass
ConnectionResetError). Раньше он утекал в error_middleware → логировался как
UNHANDLED → плодил ложные инциденты (124+ за ночь по всем activity-stream).

Фикс: error_middleware пробрасывает ConnectionResetError/ConnectionAbortedError
как benign (клиент отвалился — НЕ инцидент), а не превращает в 500+лог.
Эти тесты фиксируют контракт, чтобы регрессия не вернулась.
"""
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from aiohttp.test_utils import make_mocked_request

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


async def test_connection_reset_is_reraised_not_logged():
    """Benign disconnect: ConnectionResetError пробрасывается, НЕ становится 500."""
    req = make_mocked_request("GET", "/api/activity-stream")

    async def handler(_req):
        raise ConnectionResetError("Cannot write to closing transport")

    with pytest.raises(ConnectionResetError):
        await _webapp.error_middleware(req, handler)


async def test_aiohttp_client_connection_reset_is_reraised():
    """Конкретный класс из логов (ClientConnectionResetError) — тоже benign.
    Это подкласс ConnectionResetError; ловится тем же except."""
    assert issubclass(ClientConnectionResetError, ConnectionResetError)
    req = make_mocked_request("GET", "/api/projects/some/activity-stream")

    async def handler(_req):
        raise ClientConnectionResetError("Cannot write to closing transport")

    with pytest.raises(ConnectionResetError):
        await _webapp.error_middleware(req, handler)


async def test_connection_aborted_is_reraised():
    req = make_mocked_request("GET", "/api/activity-stream")

    async def handler(_req):
        raise ConnectionAbortedError("aborted")

    with pytest.raises(ConnectionAbortedError):
        await _webapp.error_middleware(req, handler)


async def test_real_exception_still_becomes_500():
    """Настоящая ошибка хендлера по-прежнему → JSON 500 (наблюдаемость не потеряна)."""
    req = make_mocked_request("GET", "/api/projects/x/health")

    async def handler(_req):
        raise ValueError("boom")

    resp = await _webapp.error_middleware(req, handler)
    assert resp.status == 500
    assert resp.content_type == "application/json"


async def test_http_exception_passes_through():
    """web.HTTPException (редиректы/401/404/нормальные ответы) НЕ глотается в 500."""
    req = make_mocked_request("GET", "/api/projects/x")

    async def handler(_req):
        raise web.HTTPNotFound()

    with pytest.raises(web.HTTPNotFound):
        await _webapp.error_middleware(req, handler)


async def test_ok_response_unchanged():
    """Happy path: успешный ответ хендлера проходит как есть."""
    req = make_mocked_request("GET", "/api/health")

    async def handler(_req):
        return web.json_response({"ok": True})

    resp = await _webapp.error_middleware(req, handler)
    assert resp.status == 200

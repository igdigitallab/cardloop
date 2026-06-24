"""
Regression tests for error_middleware (spec-011 Phase 0 + SSE-disconnect fix).

Context: the global error_middleware catches unhandled exceptions from handlers,
logs a standard line `UNHANDLED exc_class=... path=...` and returns JSON 500.
That line is parsed by the incident scanner (Phase 1) → card in Failed.

Bug (caught by the monitoring itself): client closes the SSE tab → next
heartbeat write in _sse_stream raises ClientConnectionResetError (subclass of
ConnectionResetError). Previously it leaked into error_middleware → was logged
as UNHANDLED → produced false incidents (124+ overnight across all activity-streams).

Fix: error_middleware re-raises ConnectionResetError/ConnectionAbortedError
as benign (client disconnected — NOT an incident) instead of turning them into
500 + log.
These tests fix the contract so the regression cannot come back.
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
    """Benign disconnect: ConnectionResetError is re-raised, NOT turned into 500."""
    req = make_mocked_request("GET", "/api/activity-stream")

    async def handler(_req):
        raise ConnectionResetError("Cannot write to closing transport")

    with pytest.raises(ConnectionResetError):
        await _webapp.error_middleware(req, handler)


async def test_aiohttp_client_connection_reset_is_reraised():
    """Specific class from logs (ClientConnectionResetError) — also benign.
    It is a subclass of ConnectionResetError; caught by the same except."""
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
    """A real handler error still → JSON 500 (observability not lost)."""
    req = make_mocked_request("GET", "/api/projects/x/health")

    async def handler(_req):
        raise ValueError("boom")

    resp = await _webapp.error_middleware(req, handler)
    assert resp.status == 500
    assert resp.content_type == "application/json"


async def test_http_exception_passes_through():
    """web.HTTPException (redirects/401/404/normal responses) is NOT swallowed into 500."""
    req = make_mocked_request("GET", "/api/projects/x")

    async def handler(_req):
        raise web.HTTPNotFound()

    with pytest.raises(web.HTTPNotFound):
        await _webapp.error_middleware(req, handler)


async def test_ok_response_unchanged():
    """Happy path: successful handler response passes through unchanged."""
    req = make_mocked_request("GET", "/api/health")

    async def handler(_req):
        return web.json_response({"ok": True})

    resp = await _webapp.error_middleware(req, handler)
    assert resp.status == 200

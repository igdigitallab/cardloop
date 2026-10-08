"""spec-096 P3.3 — every WebSocket route refuses a foreign Origin BEFORE the upgrade.

The cookie is SameSite=Lax, so a same-site page (another service on the same host, a sibling
subdomain) still sends it on a WebSocket handshake; /api/terminal/ws is a PTY shell.
"""
import inspect
import logging
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from multidict import CIMultiDict

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp  # noqa: E402
from webapp import _derive_token, auth_middleware  # noqa: E402

WS_PATHS = ["/api/terminal/ws", "/api/browser/ws", "/api/browser/input-ws"]


class _Req:
    """The slice of web.Request the Origin check reads."""

    def __init__(self, headers, remote="203.0.113.7", path="/api/terminal/ws"):
        self.headers = CIMultiDict(headers)
        self.remote = remote
        self.host = self.headers.get("Host", "")
        self.path = path


def allowed(headers, **kw) -> bool:
    return _webapp._ws_origin_allowed(_Req(headers, **kw))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("WS_ALLOWED_ORIGINS", raising=False)


# ── the decision, without a server ───────────────────────────────────────────

def test_no_origin_header_is_allowed_for_non_browser_clients():
    assert allowed({"Host": "cockpit.example:8787"})            # tools/pane-press-hold.py, curl


@pytest.mark.parametrize("host,origin", [
    ("cockpit.example:8787", "http://cockpit.example:8787"),
    ("127.0.0.1:8787", "http://127.0.0.1:8787"),
    ("localhost:8787", "http://LOCALHOST:8787"),                # host is case-insensitive
    ("[::1]:8787", "http://[::1]:8787"),
    ("cockpit.example", "https://cockpit.example"),             # default 443 on both sides
    ("cockpit.example", "https://cockpit.example:443"),
    ("cockpit.example:443", "https://cockpit.example"),
    ("cockpit.example", "http://cockpit.example"),
])
def test_same_origin_is_allowed(host, origin):
    assert allowed({"Host": host, "Origin": origin})


@pytest.mark.parametrize("host,origin", [
    ("cockpit.example:8787", "https://evil.example"),                 # other site
    ("cockpit.example:8787", "https://cockpit.example"),              # same host, different port
    ("cockpit.example:8787", "http://cockpit.example:9999"),          # same host, sibling service
    ("cockpit.example", "https://evil.cockpit.example"),              # sibling subdomain
    ("cockpit.example", "https://cockpit.example.evil.example"),
    ("cockpit.example", "https://cockpit.example:8443"),
    ("127.0.0.1:8787", "http://localhost:8787"),                      # same machine, other name
    ("localhost:8787", "https://localhost"),                          # the Capacitor shell origin
    ("cockpit.example:8787", "null"),                                 # sandboxed iframe / file://
    ("cockpit.example:8787", "file://"),
    ("cockpit.example:8787", "chrome-extension://abcdef"),
    ("cockpit.example:8787", ""),
    ("cockpit.example:8787", "http://cockpit.example:8787/some/path"),
    ("cockpit.example:8787", "http://user@cockpit.example:8787"),
    ("cockpit.example:8787", "http://cockpit.example:notaport"),
])
def test_foreign_or_malformed_origin_is_refused(host, origin):
    assert not allowed({"Host": host, "Origin": origin})


def test_two_origin_headers_are_refused():
    req = _Req({"Host": "h:1"})
    req.headers.add("Origin", "http://h:1")
    req.headers.add("Origin", "http://h:1")
    assert not _webapp._ws_origin_allowed(req)


def test_ws_allowed_origins_env_adds_exact_origins(monkeypatch):
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "https://app.example, https://localhost ,http://10.0.0.5:3000/")
    base = {"Host": "cockpit.example:8787"}
    assert allowed({**base, "Origin": "https://app.example"})
    assert allowed({**base, "Origin": "https://localhost"})           # opted in explicitly
    assert allowed({**base, "Origin": "http://10.0.0.5:3000"})
    assert not allowed({**base, "Origin": "https://app.example:8443"})
    assert not allowed({**base, "Origin": "https://other.example"})
    assert not allowed({**base, "Origin": "http://app.example"})      # scheme is part of the origin


def test_ws_allowed_origins_ignores_garbage_and_wildcards(monkeypatch):
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "*, not a url, ,ftp://x.example")
    assert not allowed({"Host": "h:1", "Origin": "https://anything.example"})
    assert not allowed({"Host": "h:1", "Origin": "ftp://x.example"})


# ── behind a reverse proxy / tunnel ──────────────────────────────────────────

def test_tls_terminating_proxy_without_trusted_proxies_keeps_working():
    """Browser on https, hop to us is http, Host preserved: the tunnel / default proxy case."""
    assert allowed({"Host": "abc.trycloudflare.com", "Origin": "https://abc.trycloudflare.com"},
                   remote="127.0.0.1")
    assert not allowed({"Host": "abc.trycloudflare.com", "Origin": "https://evil.example"},
                       remote="127.0.0.1")


def test_trusted_proxy_forwarded_host_is_honoured(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    hdrs = {"Host": "127.0.0.1:8787", "X-Forwarded-Host": "cockpit.example",
            "X-Forwarded-Proto": "https", "Origin": "https://cockpit.example"}
    assert allowed(hdrs, remote="127.0.0.1")
    assert not allowed({**hdrs, "Origin": "https://evil.example"}, remote="127.0.0.1")


def test_forwarded_host_from_an_untrusted_peer_is_ignored(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.1")
    hdrs = {"Host": "127.0.0.1:8787", "X-Forwarded-Host": "evil.example", "Origin": "https://evil.example"}
    assert not allowed(hdrs, remote="203.0.113.9")        # peer is not a trusted proxy
    assert not allowed(hdrs, remote="")                   # unknown peer is not trusted either


def test_trusted_proxy_saying_https_blocks_an_http_origin(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    hdrs = {"Host": "cockpit.example", "X-Forwarded-Proto": "https", "Origin": "http://cockpit.example"}
    assert not allowed(hdrs, remote="127.0.0.1")          # http page on the same host vs https cockpit
    assert allowed({**hdrs, "Origin": "https://cockpit.example"}, remote="127.0.0.1")


def test_trusted_proxy_saying_http_does_not_block_an_https_origin(monkeypatch):
    """A Caddy-style proxy in front of an IP-only https entrance may state X-Forwarded-Proto: http
    on purpose (so the cookie does not turn Secure on a plain-http fallback entrance); the
    operator's terminal and browser pane must keep working through it."""
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1,10.0.0.0/8,172.16.0.0/12")
    hdrs = {"Host": "198.51.100.7", "X-Forwarded-Host": "198.51.100.7", "X-Forwarded-Proto": "http",
            "Origin": "https://198.51.100.7"}
    assert allowed(hdrs, remote="172.18.0.2")
    assert not allowed({**hdrs, "Origin": "https://198.51.100.8"}, remote="172.18.0.2")


def test_forwarded_proto_from_an_untrusted_peer_is_ignored(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    hdrs = {"Host": "cockpit.example", "X-Forwarded-Proto": "https", "Origin": "http://cockpit.example"}
    assert allowed(hdrs, remote="127.0.0.1")              # header not trusted -> not used


# ── the refusal itself ───────────────────────────────────────────────────────

def test_refusal_is_a_403_with_a_log_line_and_none_when_allowed(caplog):
    ok = _Req({"Host": "h:1", "Origin": "http://h:1"})
    assert _webapp._ws_origin_refusal(ok) is None
    bad = _Req({"Host": "h:1", "Origin": "https://evil.example"})
    with caplog.at_level(logging.WARNING):
        resp = _webapp._ws_origin_refusal(bad)
    assert resp.status == 403
    assert "evil.example" in caplog.text and "[ws-origin]" in caplog.text


# ── every real WS route, over a real socket ──────────────────────────────────

def test_every_websocket_handler_is_in_this_test_list():
    """A new WebSocketResponse handler must call the check AND be added to WS_PATHS."""
    handlers = sorted(n for n, f in inspect.getmembers(_webapp, inspect.iscoroutinefunction)
                      if "WebSocketResponse(" in inspect.getsource(f))
    assert handlers == ["api_browser_input_ws", "api_browser_ws", "api_terminal_ws"]
    for name in handlers:
        src = inspect.getsource(getattr(_webapp, name))
        assert "_ws_origin_refusal(req)" in src
        assert src.index("_ws_origin_refusal(req)") < src.index("ws.prepare(req)")


@pytest.fixture
def ws_app(tmp_path, monkeypatch):
    async def no_session(req):
        return None, "no browser in this test"
    monkeypatch.setattr(_webapp, "_resolve_browser_session", no_session)
    ctx = {"password": "pw", "DATA": tmp_path, "HERE": ROOT, "topics": {}, "sessions": {}, "running": {}}
    ctx["_auth_token"] = _derive_token("pw")
    app = web.Application(middlewares=[auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/terminal/ws", _webapp.api_terminal_ws)
    app.router.add_get("/api/browser/ws", _webapp.api_browser_ws)
    app.router.add_get("/api/browser/input-ws", _webapp.api_browser_input_ws)
    return app


def _hdrs(ctx, origin=None):
    h = {"Cookie": f"cops_auth={ctx['_auth_token']}"}
    if origin is not None:
        h["Origin"] = origin
    return h


@pytest.mark.parametrize("path", WS_PATHS)
async def test_cross_origin_upgrade_is_refused_before_the_upgrade(aiohttp_client, ws_app, path):
    client = await aiohttp_client(ws_app)
    ctx = ws_app["ctx"]
    for evil in ("https://evil.example", f"http://127.0.0.1:{client.port + 1}", "null"):
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await client.ws_connect(path, headers=_hdrs(ctx, evil))
        assert err.value.status == 403


@pytest.mark.parametrize("path", WS_PATHS)
async def test_same_origin_and_no_origin_upgrade_succeeds(aiohttp_client, ws_app, path):
    client = await aiohttp_client(ws_app)
    ctx = ws_app["ctx"]
    own = f"http://127.0.0.1:{client.port}"
    for origin in (own, None):                   # a browser on the cockpit page / pane-press-hold.py
        ws = await client.ws_connect(path, headers=_hdrs(ctx, origin))
        await ws.close()


@pytest.mark.parametrize("path", WS_PATHS)
async def test_capacitor_shell_origin_is_refused_unless_listed(aiohttp_client, ws_app, monkeypatch, path):
    """The native shell navigates to the instance, so its WS is same-origin; its own
    https://localhost origin must NOT be blanket-allowed (any local https service has it)."""
    client = await aiohttp_client(ws_app)
    ctx = ws_app["ctx"]
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await client.ws_connect(path, headers=_hdrs(ctx, "https://localhost"))
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "https://localhost")
    ws = await client.ws_connect(path, headers=_hdrs(ctx, "https://localhost"))
    await ws.close()


@pytest.mark.parametrize("path", WS_PATHS)
async def test_proxy_case_over_a_real_socket(aiohttp_client, ws_app, monkeypatch, path):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    client = await aiohttp_client(ws_app)
    ctx = ws_app["ctx"]
    hdrs = {**_hdrs(ctx, "https://cockpit.example"), "Host": "cockpit.example",
            "X-Forwarded-Host": "cockpit.example", "X-Forwarded-Proto": "https"}
    ws = await client.ws_connect(path, headers=hdrs)
    await ws.close()
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await client.ws_connect(path, headers={**hdrs, "Origin": "https://evil.example"})

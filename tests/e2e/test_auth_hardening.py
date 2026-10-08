"""
spec-096 P3 — what the login hardening looks like through a REAL browser against a REAL cockpit.

  - P3.3: a page on ANOTHER origin of the same host (a sibling service on another port; the
    session cookie is SameSite=Lax and cookies ignore ports, so the browser DOES attach it) cannot
    open the PTY / browser-pane WebSockets, while the cockpit's own page can.
  - P3.1: the cockpit booted without a WEB_COOKIE_SALT generated and persisted a private one.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import http.server
import stat
import threading

import pytest

pytestmark = pytest.mark.e2e

_OPEN_WS = """(url) => new Promise((resolve) => {
  let opened = false;
  const ws = new WebSocket(url);
  ws.onopen = () => { opened = true; ws.close(); };
  ws.onclose = () => resolve(opened);
  ws.onerror = () => {};
  setTimeout(() => resolve('timeout'), 8000);
})"""


def _ws_base(server):
    return server["base_url"].replace("http://", "ws://")


def test_the_cockpit_page_can_open_the_terminal_websocket(logged_in_page, e2e_server):
    opened = logged_in_page.evaluate(_OPEN_WS, _ws_base(e2e_server) + "/api/terminal/ws")
    assert opened is True


class _Blank(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body>a sibling service on the same host</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def sibling_origin():
    """A REAL second web server on the same host, other port (a routed fake page is classified
    differently by Chromium's local-network checks and would block the WebSocket by itself)."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Blank)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("path", ["/api/terminal/ws", "/api/browser/ws?project=e2e-text",
                                  "/api/browser/input-ws?project=e2e-text"])
def test_a_sibling_origin_cannot_open_any_websocket(logged_in_page, e2e_server, sibling_origin, path):
    """Same host, other port: SameSite=Lax lets the cookie through, the Origin check must not."""
    sibling = logged_in_page.context.new_page()     # same browser context = same cookie jar
    try:
        sibling.goto(sibling_origin)
        assert sibling.evaluate("location.origin") + "/" == sibling_origin
        opened = sibling.evaluate(_OPEN_WS, _ws_base(e2e_server) + path)
        assert opened is False
    finally:
        sibling.close()


def test_a_cockpit_without_a_configured_salt_persists_a_private_one(e2e_server):
    f = e2e_server["app_dir"] / "data" / "cookie_salt"
    assert f.is_file()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert len(f.read_text().strip()) >= 32
    log = (e2e_server["app_dir"] / "server.log").read_text(errors="replace")
    assert f.read_text().strip() not in log          # never printed

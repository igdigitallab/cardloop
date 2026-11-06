"""
The connection drops in the middle of a turn — and the turn finishes on the server meanwhile.

A phone that sleeps or switches networks resets the chat's POST stream; the server logs "client
disconnected, task continues in background" and writes the whole answer. The bubble used to
finalise with the browser's own "network error", which reads as a dead agent, and the operator
re-asked a question whose answer was already done. Proven here, through a TCP proxy that resets
every connection the way the phone's network does:
  - while the connection is down the bubble says the connection was lost and the agent is still
    working — never the bare "network error";
  - once the connection is back the finished reply appears without any action by the operator,
    and the lost-connection note is gone.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import select
import socket
import struct
import threading
import time
from urllib.parse import urlparse

import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e

_RST = struct.pack("ii", 1, 0)  # SO_LINGER on, 0 s: close() sends RST, like a dropped mobile link


class _CutProxy:
    """A TCP proxy in front of the cockpit that can reset every open connection and refuse new
    ones until restored."""

    def __init__(self, target_port: int):
        self._target = ("127.0.0.1", target_port)
        self._ls = socket.socket()
        self._ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ls.bind(("127.0.0.1", 0))
        self._ls.listen(64)
        self.port = self._ls.getsockname()[1]
        self._down = False
        self._gen = 0
        threading.Thread(target=self._accept, daemon=True).start()

    @staticmethod
    def _reset(s: socket.socket) -> None:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _RST)
            s.close()
        except OSError:
            pass

    def _pipe(self, a: socket.socket, b: socket.socket, gen: int) -> None:
        try:
            while self._gen == gen:
                if not select.select([a], [], [], 0.05)[0]:
                    continue
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        self._reset(a)
        self._reset(b)

    def _accept(self) -> None:
        while True:
            c, _ = self._ls.accept()
            if self._down:
                self._reset(c)
                continue
            u = socket.create_connection(self._target)
            for a, b in ((c, u), (u, c)):
                threading.Thread(target=self._pipe, args=(a, b, self._gen), daemon=True).start()

    def cut(self) -> None:
        self._down = True
        self._gen += 1
        time.sleep(0.2)  # every pipe notices within one select tick and resets its sockets

    def restore(self) -> None:
        self._down = False


def test_a_dropped_connection_still_gets_the_reply(page, e2e_server):
    proxy = _CutProxy(urlparse(e2e_server["base_url"]).port)
    (e2e_server["app_dir"] / "data" / "ui_state.json").unlink(missing_ok=True)
    page.goto(f"http://127.0.0.1:{proxy.port}")
    page.fill("#password", e2e_server["password"])
    page.click("button.btn-primary[type=submit]")
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, "e2e-netdrop")
    feed = page.locator(".chat-feed:visible")

    send_chat(page, "e2e:slow while the phone sleeps")
    page.wait_for_selector("text=starting slow scenario", timeout=10_000)
    proxy.cut()
    # The scripted turn finishes ~3 s later on the server, inside this outage.
    page.wait_for_selector(".chat-msg-error:has-text('Connection lost')", timeout=10_000)
    assert "network error" not in feed.inner_text().lower()
    page.wait_for_timeout(6_000)
    proxy.restore()

    page.wait_for_selector(".chat-feed:visible >> text=done after the long silence", timeout=20_000)
    page.wait_for_function(
        "() => ![...document.querySelectorAll('.chat-msg-error')].some(e => e.offsetParent)",
        timeout=10_000)
    text = feed.inner_text()
    assert text.count("done after the long silence") == 1
    assert "Connection lost" not in text and "network error" not in text.lower()

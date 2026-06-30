"""spec-065 Phase B/C — browser pane unit tests.

Pure-logic coverage (input routing, clamping, registry) without launching a real
Chromium: a fake CDP session records the dispatched calls.
"""
import asyncio

import browser_pane
from browser_pane import BrowserSession, VIEWPORT


class _FakeCDP:
    def __init__(self):
        self.calls = []

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))


def _session_with_fake_cdp() -> BrowserSession:
    s = BrowserSession("k")
    s._started = True
    s._cdp = _FakeCDP()
    return s


def test_clamp_bounds_and_bad_input():
    assert BrowserSession._clamp(-5, VIEWPORT["width"]) == 0.0
    assert BrowserSession._clamp(99999, VIEWPORT["width"]) == float(VIEWPORT["width"])
    assert BrowserSession._clamp(640, VIEWPORT["width"]) == 640.0
    assert BrowserSession._clamp("nope", VIEWPORT["width"]) == 0.0  # non-numeric → 0


def test_mouse_down_maps_to_pressed():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "mouse", "action": "down", "x": 100, "y": 50, "button": "left"}))
    assert ("Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": 100.0, "y": 50.0, "button": "left", "clickCount": 1}) in s._cdp.calls


def test_mouse_move_is_clamped():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "mouse", "action": "move", "x": 99999, "y": -3}))
    assert ("Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": float(VIEWPORT["width"]), "y": 0.0}) in s._cdp.calls


def test_wheel_dispatch():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "wheel", "x": 10, "y": 20, "dx": 1, "dy": -2}))
    method, params = s._cdp.calls[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mouseWheel" and params["deltaY"] == -2.0


def test_key_down_carries_text():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "key", "action": "down", "key": "a", "text": "a"}))
    method, params = s._cdp.calls[-1]
    assert method == "Input.dispatchKeyEvent"
    assert params["type"] == "keyDown" and params["key"] == "a" and params["text"] == "a"


def test_unknown_input_is_noop():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "bogus"}))
    assert s._cdp.calls == []


def test_registry_dedup_and_close(monkeypatch):
    async def _noop_start(self):
        self._started = True
    monkeypatch.setattr(browser_pane.BrowserSession, "start", _noop_start)

    async def go():
        a = await browser_pane.get_or_create("PROJ")
        b = await browser_pane.get_or_create("PROJ")
        assert a is b, "same key must reuse the session"
        await browser_pane.close_session("PROJ")
        assert "PROJ" not in browser_pane._SESSIONS

    asyncio.run(go())


# ── late-subscriber frame replay (the "blank pane on a static page" fix) ──────

import base64 as _b64


class _FakeWS:
    def __init__(self):
        self.sent_json = []
        self.sent_bytes = []

    async def send_json(self, obj):
        self.sent_json.append(obj)

    async def send_bytes(self, b):
        self.sent_bytes.append(b)


class _FakeCDPScreenshot:
    async def send(self, method, params=None):
        if method == "Page.captureScreenshot":
            return {"data": _b64.b64encode(b"CAPTURED").decode()}
        return {}


def test_on_frame_caches_last_frame_without_subscribers():
    # The screencast emits frames even with nobody watching; they must be cached
    # so the next subscriber to join a now-static page is primed immediately.
    s = BrowserSession("k")
    s._on_frame({"data": _b64.b64encode(b"JPEGDATA").decode(), "sessionId": None})
    assert s._last_frame == b"JPEGDATA"


def test_prime_replays_cached_frame_to_late_subscriber():
    async def go():
        s = BrowserSession("k")
        s._last_frame = b"FRAME"
        ws = _FakeWS()
        await s._prime(ws)
        assert ws.sent_bytes == [b"FRAME"], "late subscriber must receive the current frame"
    asyncio.run(go())


def test_prime_captures_when_no_cached_frame():
    async def go():
        s = BrowserSession("k")
        s._cdp = _FakeCDPScreenshot()
        ws = _FakeWS()
        await s._prime(ws)
        assert ws.sent_bytes == [b"CAPTURED"]
        assert s._last_frame == b"CAPTURED", "captured frame should also be cached"
    asyncio.run(go())


def test_disconnected_retires_session_for_rebuild():
    async def go():
        s = BrowserSession("k")
        s._started = True
        browser_pane._SESSIONS["k"] = s
        s._on_disconnected(None)
        assert s._closed is True
        assert s._is_alive() is False, "a disconnected browser is not alive"
        await asyncio.sleep(0)  # let the scheduled close_session run
        assert "k" not in browser_pane._SESSIONS
    asyncio.run(go())


def test_close_session_identity_guard():
    async def go():
        s_old = BrowserSession("k")
        s_new = BrowserSession("k")
        browser_pane._SESSIONS["k"] = s_new
        # A dying old session must not evict the fresh replacement under the key.
        await browser_pane.close_session("k", s_old)
        assert browser_pane._SESSIONS.get("k") is s_new
        browser_pane._SESSIONS.pop("k", None)
    asyncio.run(go())


# ── tabs (multi-page) ─────────────────────────────────────────────────────────


class _FakePage:
    """Minimal Playwright Page stand-in for tab-logic tests (no real Chromium)."""
    def __init__(self, url="about:blank", title="T"):
        self._url = url
        self._title = title
        self.closed = False
        self._handlers: dict = {}

    @property
    def url(self):
        return self._url

    async def title(self):
        return self._title

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def remove_listener(self, event, fn):
        if fn in self._handlers.get(event, []):
            self._handlers[event].remove(fn)

    async def close(self):
        self.closed = True
        for fn in list(self._handlers.get("close", [])):
            fn(self)


def test_adopt_page_assigns_sequential_ids_and_is_idempotent():
    s = BrowserSession("k")
    p1, p2 = _FakePage(), _FakePage()
    a = s._adopt_page(p1)
    b = s._adopt_page(p2)
    assert (a, b) == ("t1", "t2")
    assert s._adopt_page(p1) == "t1", "re-adopting the same page returns its existing id"
    assert s._id_of(p2) == "t2"
    assert len(s._tabs) == 2


def test_tabs_payload_shape_and_active_flag():
    async def go():
        s = BrowserSession("k")
        p1, p2 = _FakePage(url="https://a.test", title="A"), _FakePage(url="https://b.test", title="B")
        s._adopt_page(p1); s._adopt_page(p2)
        s._active_id = "t2"
        payload = await s._tabs_payload()
        assert payload["type"] == "tabs" and payload["activeId"] == "t2"
        by_id = {t["id"]: t for t in payload["tabs"]}
        assert by_id["t1"] == {"id": "t1", "url": "https://a.test", "title": "A", "active": False}
        assert by_id["t2"]["active"] is True
    asyncio.run(go())


def test_tabs_payload_falls_back_to_url_when_titleless():
    async def go():
        s = BrowserSession("k")
        s._adopt_page(_FakePage(url="https://x.test", title=""))
        s._active_id = "t1"
        assert (await s._tabs_payload())["tabs"][0]["title"] == "https://x.test"
    asyncio.run(go())


def test_closing_active_tab_switches_to_remaining(monkeypatch):
    async def go():
        s = BrowserSession("k")
        p1, p2 = _FakePage(), _FakePage()
        s._adopt_page(p1); s._adopt_page(p2)
        s._active_id = "t1"
        # Avoid real CDP: stub the active-tab binding to just record the new active id.
        async def fake_bind(page, *, prime=True):
            s._active_id = s._id_of(page)
        monkeypatch.setattr(s, "_bind_active", fake_bind)
        await s._handle_tab_closed("t1")
        assert "t1" not in s._tabs
        assert s._active_id == "t2", "closing the active tab activates a remaining one"
    asyncio.run(go())


def test_close_tab_refuses_to_close_the_last_tab():
    async def go():
        s = BrowserSession("k")
        p1 = _FakePage()
        s._adopt_page(p1)
        await s.close_tab("t1")
        assert p1.closed is False and "t1" in s._tabs, "the only tab must stay open"
    asyncio.run(go())


def test_close_tab_closes_page_when_more_than_one(monkeypatch):
    async def go():
        s = BrowserSession("k")
        p1, p2 = _FakePage(), _FakePage()
        s._adopt_page(p1); s._adopt_page(p2)
        s._active_id = "t1"
        async def fake_bind(page, *, prime=True):
            s._active_id = s._id_of(page)
        monkeypatch.setattr(s, "_bind_active", fake_bind)
        await s.close_tab("t1")        # closes p1 → fires its close handler → _handle_tab_closed
        await asyncio.sleep(0)         # let the scheduled _handle_tab_closed run
        assert p1.closed is True
        assert "t1" not in s._tabs and s._active_id == "t2"
    asyncio.run(go())


def test_handle_input_routes_tab_controls_without_cdp(monkeypatch):
    async def go():
        s = BrowserSession("k")
        s._cdp = None  # tab controls must work even with no active CDP session
        seen = {}
        async def fake_activate(tid):
            seen["activate"] = tid
        monkeypatch.setattr(s, "activate_tab", fake_activate)
        await s.handle_input({"t": "tab.activate", "id": "t3"})
        assert seen.get("activate") == "t3"
    asyncio.run(go())

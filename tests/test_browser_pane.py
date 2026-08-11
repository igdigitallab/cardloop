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
    method, params = s._cdp.calls[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params["type"] == "mousePressed" and params["x"] == 100.0 and params["y"] == 50.0
    assert params["button"] == "left" and params["clickCount"] == 1


def test_mouse_move_is_clamped():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "mouse", "action": "move", "x": 99999, "y": -3}))
    _, params = s._cdp.calls[-1]
    assert params["type"] == "mouseMoved"
    assert params["x"] == float(VIEWPORT["width"]) and params["y"] == 0.0


def test_move_with_a_held_button_is_a_drag():
    """Text selection: a move carrying buttons=1 must reach Chromium as a left-button
    drag, not a hover — otherwise dragging across text selects nothing."""
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "mouse", "action": "move", "x": 10, "y": 10, "buttons": 1}))
    _, params = s._cdp.calls[-1]
    assert params["buttons"] == 1 and params["button"] == "left"
    # …and a plain hover stays a hover
    asyncio.run(s.handle_input({"t": "mouse", "action": "move", "x": 10, "y": 10}))
    _, hover = s._cdp.calls[-1]
    assert hover["buttons"] == 0 and hover["button"] == "none"


def test_click_count_and_modifiers_are_forwarded():
    """clickCount 2/3 is how Chromium selects a word / a line; Shift+click extends
    a selection — both need these fields."""
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "mouse", "action": "down", "x": 5, "y": 5,
                                "button": "left", "clickCount": 2, "mods": 8}))
    _, params = s._cdp.calls[-1]
    assert params["clickCount"] == 2 and params["modifiers"] == 8
    # out-of-range click counts are clamped, junk falls back to a single click
    asyncio.run(s.handle_input({"t": "mouse", "action": "down", "x": 5, "y": 5, "clickCount": 99}))
    assert s._cdp.calls[-1][1]["clickCount"] == 3
    asyncio.run(s.handle_input({"t": "mouse", "action": "down", "x": 5, "y": 5, "clickCount": "x"}))
    assert s._cdp.calls[-1][1]["clickCount"] == 1


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


def test_non_printable_keys_carry_a_virtual_key_code():
    """The bug that made the pane un-editable: Chromium derives the editing command
    (delete a char, move the caret) from the virtual key code, NOT from `key`. Without
    it Backspace/Delete/arrows fire a JS keydown and do nothing else."""
    for key, vk in (("Backspace", 8), ("Delete", 46), ("ArrowLeft", 37), ("Enter", 13), ("Tab", 9)):
        s = _session_with_fake_cdp()
        asyncio.run(s.handle_input({"t": "key", "action": "down", "key": key, "text": ""}))
        method, params = s._cdp.calls[-1]
        assert method == "Input.dispatchKeyEvent"
        assert params["windowsVirtualKeyCode"] == vk, key
        assert params["nativeVirtualKeyCode"] == vk, key
        assert params["code"] == key
        # No stray text on a non-printable key
        assert "text" not in params


def test_printable_key_keeps_text_and_gains_a_key_code():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "key", "action": "down", "key": "b", "text": "b"}))
    _, params = s._cdp.calls[-1]
    assert params["type"] == "keyDown" and params["text"] == "b"
    assert params["windowsVirtualKeyCode"] == ord("B") and params["code"] == "KeyB"


def test_modifiers_are_forwarded_and_shortcuts_send_no_text():
    """Ctrl+A must reach the page as a command, not insert the letter 'a'."""
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "key", "action": "down", "key": "a", "text": "a", "mods": 2}))
    _, params = s._cdp.calls[-1]
    assert params["modifiers"] == 2
    assert params["type"] == "rawKeyDown"
    assert "text" not in params


def test_shift_char_reports_unmodified_text():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "key", "action": "down", "key": "A", "text": "A", "mods": 8}))
    _, params = s._cdp.calls[-1]
    assert params["text"] == "A" and params["unmodifiedText"] == "a" and params["modifiers"] == 8


def test_char_event_only_inserts_text():
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "key", "action": "char", "text": "5"}))
    assert s._cdp.calls[-1] == ("Input.dispatchKeyEvent", {"type": "char", "text": "5"})


def test_paste_uses_insert_text():
    """The remote Chromium has its own empty clipboard — Ctrl+V alone pastes nothing."""
    s = _session_with_fake_cdp()
    asyncio.run(s.handle_input({"t": "paste", "text": "P@ssw0rd"}))
    assert s._cdp.calls[-1] == ("Input.insertText", {"text": "P@ssw0rd"})
    s2 = _session_with_fake_cdp()
    asyncio.run(s2.handle_input({"t": "paste", "text": ""}))
    assert s2._cdp.calls == []


# ── agent type_text / snapshot: real keystrokes, not a bulk value-set ──────────
# A split-digit code field (several maxlength=1 boxes with a JS keydown listener
# that auto-advances focus) only reacts to genuine keydown events. Page.fill()
# writes .value directly — fires oninput but no keydown, so the page's own JS never
# advances focus. type_text must click (real focus) then dispatch real keystrokes.

class _FakeKeyboard:
    def __init__(self):
        self.typed = []

    async def type(self, text, delay=None):
        self.typed.append(text)


class _FakeInteractivePage:
    def __init__(self):
        self.url = "https://example.test"
        self.clicks = []
        self.goto_calls = []
        self.keyboard = _FakeKeyboard()
        self._eval_result = []
        # When set, every call below raises this instead of doing its normal thing —
        # used to simulate a broken CDP session (or a plain bad-selector timeout).
        self.fail_with: "Exception | None" = None

    async def title(self):
        if self.fail_with:
            raise self.fail_with
        return "T"

    async def click(self, selector, timeout=None, click_count=1):
        if self.fail_with:
            raise self.fail_with
        self.clicks.append((selector, click_count))

    async def goto(self, url, wait_until=None):
        if self.fail_with:
            raise self.fail_with
        self.goto_calls.append(url)

    async def inner_text(self, selector, timeout=None):
        return "body text"

    async def evaluate(self, script):
        return self._eval_result


def _session_with_fake_page() -> "tuple[BrowserSession, _FakeInteractivePage]":
    s = BrowserSession("k")
    s._started = True
    page = _FakeInteractivePage()
    s._page = page
    return s, page


def test_type_text_with_selector_clicks_then_types_real_keystrokes():
    s, page = _session_with_fake_page()
    asyncio.run(s.type_text("581702", selector="#otp-0"))
    # Triple-click selects any existing value (fill()'s replace semantics) before typing.
    assert page.clicks == [("#otp-0", 3)]
    assert page.keyboard.typed == ["581702"]


def test_type_text_without_selector_just_types_focused_element():
    s, page = _session_with_fake_page()
    asyncio.run(s.type_text("hello"))
    assert page.clicks == []
    assert page.keyboard.typed == ["hello"]


def test_snapshot_includes_formatted_interactive_elements():
    s, page = _session_with_fake_page()
    page._eval_result = [
        {"tag": "input", "type": "tel", "id": "otp-0", "maxlength": "1", "visible": True},
        {"tag": "button", "aria-label": "Next", "visible": True},
    ]
    snap = asyncio.run(s.snapshot())
    assert '[0] input type="tel" id="otp-0" maxlength="1"' in snap["elements"]
    assert '[1] button aria-label="Next"' in snap["elements"]


def test_snapshot_elements_empty_when_evaluate_fails():
    s, page = _session_with_fake_page()
    async def _boom(script):
        raise RuntimeError("no CDP")
    page.evaluate = _boom
    snap = asyncio.run(s.snapshot())
    assert snap["elements"] == ""


# ── agent-tool self-healing: classify + auto-retire on a dead CDP session ──────
# 2026-08-11 follow-up: handle_input (the operator's raw input path) already
# self-heals; navigate/click/type_text/snapshot (the agent's selector-based path)
# did not, so browser_navigate/browser_snapshot kept re-hitting the same corpse and
# failing identically ("Connection closed while reading from the driver") for the
# rest of a run. These four must retire the session on a DEAD-CONNECTION failure
# and re-raise, but stay silent (no retirement) on an ordinary usage error like a
# bad selector — retiring for that would just discard a perfectly healthy session.

def test_looks_like_dead_connection_matches_known_markers():
    from browser_pane import looks_like_dead_connection
    assert looks_like_dead_connection(RuntimeError("Connection closed while reading from the driver"))
    assert looks_like_dead_connection(Exception("Target closed"))
    assert looks_like_dead_connection(Exception("Target page, context or browser has been closed"))


def test_looks_like_dead_connection_does_not_match_a_selector_timeout():
    from browser_pane import looks_like_dead_connection
    assert not looks_like_dead_connection(Exception('Timeout 10000ms exceeded waiting for selector "#nope"'))
    assert not looks_like_dead_connection(Exception("element not found"))


def test_navigate_retires_session_on_dead_connection_and_reraises():
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        page.fail_with = RuntimeError("Connection closed while reading from the driver")
        try:
            await s.navigate("https://x.test")
            assert False, "must re-raise"
        except RuntimeError:
            pass
        assert s._closed is True
        assert "k" not in browser_pane._SESSIONS
    asyncio.run(go())


def test_click_does_not_retire_session_on_a_plain_selector_timeout():
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        page.fail_with = Exception('Timeout 10000ms exceeded waiting for selector "#nope"')
        try:
            await s.click("#nope")
            assert False, "must re-raise"
        except Exception:
            pass
        assert s._closed is False, "a bad selector is a usage error, not a dead session"
        assert browser_pane._SESSIONS.get("k") is s
        browser_pane._SESSIONS.pop("k", None)
    asyncio.run(go())


def test_type_text_retires_session_on_dead_connection():
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        page.fail_with = RuntimeError("Target closed")
        try:
            await s.type_text("581702", selector="#otp-0")
            assert False, "must re-raise"
        except RuntimeError:
            pass
        assert s._closed is True
        assert "k" not in browser_pane._SESSIONS
    asyncio.run(go())


def test_snapshot_retires_session_on_dead_connection():
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        page.fail_with = RuntimeError("Browser has been closed")
        try:
            await s.snapshot()
            assert False, "must re-raise"
        except RuntimeError:
            pass
        assert s._closed is True
        assert "k" not in browser_pane._SESSIONS
    asyncio.run(go())


def test_status_reports_backend_and_liveness():
    s, page = _session_with_fake_page()
    s.backend = "external-cdp"
    st = s.status()
    assert st["backend"] == "external-cdp"
    assert st["started"] is True
    assert st["closed"] is False
    assert st["alive"] is True
    assert st["url"] == "https://example.test"


def test_format_interactive_elements_marks_hidden_and_text():
    from browser_pane import _format_interactive_elements
    out = _format_interactive_elements([
        {"tag": "a", "href": "https://x.test", "text": "Sign in", "visible": False},
    ])
    assert out == '[0] a href="https://x.test" (hidden) — "Sign in"'


# ── a broken CDP session must not fail silently ─────────────────────────────────
# 2026-08-11: after a service restart, get_or_create() discarded a dead session's
# Python object without closing it — the local Playwright driver subprocess and its
# CDP connection leaked instead of shutting down, and handle_input() swallowed every
# resulting dispatch failure, so the pane kept showing its last cached frame forever:
# alive-looking, deaf to every click. Both fixed below.

class _BoomCDP:
    """A CDP session whose every command fails, like a dead local driver pipe."""
    async def send(self, method, params=None):
        raise RuntimeError("Connection closed while reading from the driver")


def test_handle_input_failure_notifies_subscribers_and_retires_session():
    async def go():
        s = BrowserSession("PROJ_BOOM")
        s._started = True
        s._cdp = _BoomCDP()
        browser_pane._SESSIONS["PROJ_BOOM"] = s
        ws = _FakeWS()
        s._subs.add(ws)
        await s.handle_input({"t": "mouse", "action": "down", "x": 1, "y": 1}, ws)
        assert ws.sent_json and ws.sent_json[-1]["type"] == "error"
        await asyncio.sleep(0)  # let the scheduled close_session run
        assert s._closed is True
        assert "PROJ_BOOM" not in browser_pane._SESSIONS
    asyncio.run(go())


def test_handle_input_failure_is_reported_only_once_per_session():
    """A burst of failing messages (several queued mouse events on a dead session)
    must not spam the client with repeated error broadcasts."""
    async def go():
        s = BrowserSession("PROJ_BOOM2")
        s._started = True
        s._cdp = _BoomCDP()
        ws = _FakeWS()
        s._subs.add(ws)
        await s.handle_input({"t": "mouse", "action": "down", "x": 1, "y": 1}, ws)
        s._closed = True  # simulate the scheduled close_session having already run
        await s.handle_input({"t": "mouse", "action": "up", "x": 1, "y": 1}, ws)
        assert len(ws.sent_json) == 1
    asyncio.run(go())


def test_get_or_create_closes_a_stale_session_before_replacing_it(monkeypatch):
    closed = []

    class _DeadSession(BrowserSession):
        def _is_alive(self):
            return False

        async def close(self):
            closed.append(self.key)

    async def _noop_start(self):
        self._started = True
    monkeypatch.setattr(browser_pane.BrowserSession, "start", _noop_start)

    async def go():
        old = _DeadSession("PROJ_STALE")
        browser_pane._SESSIONS["PROJ_STALE"] = old
        new = await browser_pane.get_or_create("PROJ_STALE")
        assert new is not old
        assert closed == ["PROJ_STALE"], "the dead session's driver/CDP connection must be torn down, not just dereferenced"
        await browser_pane.close_session("PROJ_STALE")
    asyncio.run(go())


def test_history_controls_drive_the_page():
    calls = []

    class _FakePage:
        async def go_back(self, **kw): calls.append("back")
        async def go_forward(self, **kw): calls.append("forward")
        async def reload(self, **kw): calls.append("reload")

    s = _session_with_fake_cdp()
    s._page = _FakePage()
    for act in ("back", "forward", "reload"):
        asyncio.run(s.handle_input({"t": act}))
    assert calls == ["back", "forward", "reload"]


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


# ───────────── shared Manager profile: one tab per project (spec-079) ─────────────
#
# Several projects may now resolve to the SAME Cloak Manager profile. They share the
# browser context (that is where the logins live) but must NOT share a tab: adopting
# another project's page means one pane streams — and navigates away — another's.

class _SharedFakePage:
    def __init__(self, opener=None):
        self._opener = opener
        self.closed = False
        self.handlers = {}

    async def opener(self):
        return self._opener

    def on(self, event, cb):
        self.handlers[event] = cb

    async def close(self):
        self.closed = True


def _shared_session(pages) -> BrowserSession:
    s = BrowserSession("k")
    s._started = True
    s._shared_ctx = True
    s._tabs = dict(pages)
    s._tab_seq = len(pages)
    return s


def test_shared_context_ignores_a_foreign_new_page():
    """A tab opened by ANOTHER project in the same profile must be left alone."""
    mine = _SharedFakePage()
    s = _shared_session({"t1": mine})
    foreign = _SharedFakePage(opener=None)          # no opener → not ours
    asyncio.run(s._handle_new_page(foreign))
    assert list(s._tabs.values()) == [mine]


def test_shared_context_adopts_our_own_popup():
    """An OAuth/login popup opened FROM our page is ours — losing it would break
    exactly the Google sign-in flows a shared profile exists for."""
    mine = _SharedFakePage()
    s = _shared_session({"t1": mine})
    s._bind_active = lambda *a, **k: _noop()
    popup = _SharedFakePage(opener=mine)
    asyncio.run(s._handle_new_page(popup))
    assert popup in s._tabs.values()


async def _noop():
    return None


def test_unshared_context_adopts_every_new_page():
    """Unchanged behaviour when the browser is ours alone."""
    s = BrowserSession("k")
    s._started = True
    s._bind_active = lambda *a, **k: _noop()
    page = _SharedFakePage(opener=None)
    asyncio.run(s._handle_new_page(page))
    assert page in s._tabs.values()


def test_teardown_closes_our_tab_but_not_a_borrowed_browser():
    """We opened the tab inside someone else's browser, so we close it — otherwise a
    shared profile grows a dead tab on every session restart. The browser and its
    cookies must survive."""
    page = _SharedFakePage()
    browser = _SharedFakePage()   # stands in for a browser handle; .close would set closed
    s = BrowserSession("k")
    s._owns_browser = False
    s._owns_page = True
    s._page = page
    s._browser = browser
    s._ctx = _SharedFakePage()
    s._pw = None
    asyncio.run(s._teardown())
    assert page.closed is True
    assert browser.closed is False


def test_teardown_leaves_a_page_we_did_not_open():
    page = _SharedFakePage()
    s = BrowserSession("k")
    s._owns_browser = False
    s._owns_page = False
    s._page = page
    s._browser = _SharedFakePage()
    s._ctx = _SharedFakePage()
    s._pw = None
    asyncio.run(s._teardown())
    assert page.closed is False

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


def test_mouse_down_sends_a_click_ack_only_after_a_successful_dispatch():
    """The operator's only signal that a click actually landed (vs. the pane just
    looking alive while frozen) — must fire ONLY once the CDP dispatch above
    genuinely succeeded, never optimistically."""
    async def go():
        s = _session_with_fake_cdp()
        ws = _FakeWS()
        await s.handle_input({"t": "mouse", "action": "down", "x": 100, "y": 50, "button": "left"}, ws)
        assert ws.sent_json == [{"type": "click_ack", "x": 100, "y": 50}]
    asyncio.run(go())


def test_mouse_move_does_not_send_a_click_ack():
    async def go():
        s = _session_with_fake_cdp()
        ws = _FakeWS()
        await s.handle_input({"t": "mouse", "action": "move", "x": 10, "y": 10}, ws)
        assert ws.sent_json == []
    asyncio.run(go())


def test_mouse_down_failure_sends_no_click_ack():
    async def go():
        s = _session_with_fake_cdp()

        async def _boom(method, params=None):
            raise RuntimeError("dispatch failed")
        s._cdp.send = _boom
        ws = _FakeWS()
        s._subs.add(ws)
        await s.handle_input({"t": "mouse", "action": "down", "x": 1, "y": 1}, ws)
        # The existing session-lost error IS expected here (a different, already
        # tested behavior) — but no click_ack must be mixed in among it.
        assert {"type": "click_ack", "x": 1, "y": 1} not in ws.sent_json
    asyncio.run(go())


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


class _FakeLocator:
    """A stand-in for a Playwright Locator — the strict-by-default API that
    replaced page.click(selector)/page.select_option(selector)/... everywhere in
    browser_pane.py precisely because the OLD page-level methods silently acted on
    the first of several matches with no error. Resolution (match count, ambiguity)
    is delegated back to the owning fake page/frame so both _FakeInteractivePage and
    _FakeFrame can share one implementation."""

    def __init__(self, owner, selector, nth=None):
        self._owner = owner
        self._selector = selector
        self._nth = nth

    def nth(self, index):
        return _FakeLocator(self._owner, self._selector, index)

    async def click(self, timeout=None, click_count=1):
        await self._owner._resolve(self._selector, self._nth, timeout)
        self._owner.clicks.append((self._selector, click_count))

    async def set_input_files(self, path, timeout=None):
        await self._owner._resolve(self._selector, self._nth, timeout)
        self._owner.uploads.append((self._selector, path))

    async def select_option(self, value=None, label=None, timeout=None):
        await self._owner._resolve(self._selector, self._nth, timeout)
        if value is not None and not self._owner.select_value_ok:
            raise Exception(f"No option matches value {value!r}")
        self._owner.selects.append((self._selector, value, label))


class _FakeInteractivePage:
    def __init__(self):
        self.url = "https://example.test"
        self.clicks = []
        self.goto_calls = []
        self.uploads = []
        self.selects = []
        self.keyboard = _FakeKeyboard()
        self._eval_result = []
        # When set, every call below raises this instead of doing its normal thing —
        # used to simulate a broken CDP session (or a plain bad-selector timeout).
        self.fail_with: "Exception | None" = None
        # None = any selector "matches" (existing tests' default expectation); a
        # test exercising the iframe fallback sets this to a restricted set so the
        # main-frame click/upload/select genuinely fails and the fallback kicks in.
        self.clickable_selectors: "set[str] | None" = None
        # Selectors that must simulate MULTIPLE matches — Locator raises Playwright's
        # own "strict mode violation" wording so browser_pane's classifier matches it.
        self.ambiguous_selectors: "set[str]" = set()
        # False forces select_option(value=...) to always fail, so a test can
        # exercise the value->label fallback without needing a real <option> list.
        self.select_value_ok: bool = True
        # No iframes by default: a page IS its own only "frame" here, so the
        # snapshot/fallback frame-scan loop skips it (frame == main_frame) and
        # every existing test's behavior is unchanged. Tests that care about
        # iframes replace this list with real _FakeFrame stand-ins.
        self.main_frame = self
        self.frames = [self]
        self.inner_text_value = "body text"
        # Captcha tests need evaluate() to answer DIFFERENTLY per script (detect vs
        # inject) and to see the argument passed alongside. When set to a callable
        # (script, arg) -> result it takes over; unset keeps the old single-result
        # behavior every other test relies on.
        self.eval_router = None
        self.eval_calls = []

    async def title(self):
        if self.fail_with:
            raise self.fail_with
        return "T"

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def _resolve(self, selector, nth, timeout):
        if self.fail_with:
            raise self.fail_with
        if selector in self.ambiguous_selectors and nth is None:
            raise Exception(f'Locator.click: Error: strict mode violation: locator("{selector}") resolved to 2 elements')
        if self.clickable_selectors is not None and selector not in self.clickable_selectors:
            raise TimeoutError(f'Timeout {timeout}ms exceeded waiting for locator("{selector}")')

    async def goto(self, url, wait_until=None):
        if self.fail_with:
            raise self.fail_with
        self.goto_calls.append(url)

    async def inner_text(self, selector, timeout=None):
        return self.inner_text_value

    async def evaluate(self, script, arg=None):
        self.eval_calls.append((script, arg))
        if self.eval_router is not None:
            return self.eval_router(script, arg)
        return self._eval_result


class _FakeFrame:
    """A stand-in for a Playwright Frame (an iframe) — same shape as
    _FakeInteractivePage's relevant methods, tracked separately so tests can tell
    a main-frame action from a fallen-back-to-iframe one."""

    def __init__(self, url, elements=None, text="", clickable_selectors=None):
        self.url = url
        self._eval_result = elements or []
        self._text = text
        # Only these selectors succeed on .click()/.set_input_files() — everything
        # else times out, like a real Frame would for an element that isn't there.
        self._clickable = set(clickable_selectors or [])
        self.ambiguous_selectors: "set[str]" = set()
        self.select_value_ok = True
        self.clicks = []
        self.uploads = []
        self.selects = []

    async def evaluate(self, script, arg=None):
        return self._eval_result

    async def inner_text(self, selector, timeout=None):
        return self._text

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def _resolve(self, selector, nth, timeout):
        if selector in self.ambiguous_selectors and nth is None:
            raise Exception(f'Locator.click: Error: strict mode violation: locator("{selector}") resolved to 2 elements')
        if selector not in self._clickable:
            raise TimeoutError(f'Timeout {timeout}ms exceeded waiting for locator("{selector}")')


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


# ── text truncation: a clean boundary + an explicit notice, not a silent mid-word ──
# cut (a 7-row transaction table read as "...FLC Enrol..." with no sign there was
# more, costing three narrowing round trips to actually see the rest).

def test_truncate_text_leaves_short_text_untouched():
    from browser_pane import _truncate_text
    assert _truncate_text("short", 4000) == "short"


def test_truncate_text_cuts_at_a_word_boundary_and_notes_it():
    from browser_pane import _truncate_text
    text = "aaaa bbbb cccc dddd eeee"
    out = _truncate_text(text, 12)
    assert out.startswith("aaaa bbbb")
    assert "cccc" not in out
    assert "truncated: 9 of 24 chars shown" in out
    assert "larger max_chars" in out


def test_snapshot_passes_max_chars_through_to_truncation():
    s, page = _session_with_fake_page()
    page.inner_text_value = "word " * 2000
    snap = asyncio.run(s.snapshot(max_chars=50))
    assert len(snap["text"]) < 200  # truncated notice + ~50 chars, not the full 10000
    assert "truncated" in snap["text"]


# ── iframes: a Google Sign-In button or a CAPTCHA checkbox is invisible to a ────
# page-level inner_text()/evaluate() (they only ever look at the main document),
# even though it's right there on screen and Playwright itself can reach it.

def test_snapshot_includes_elements_and_text_from_an_iframe():
    s, page = _session_with_fake_page()
    google_frame = _FakeFrame(
        "https://accounts.google.com/gsi/button",
        elements=[{"tag": "div", "role": "button", "aria-label": "Sign in with Google", "visible": True}],
        text="Sign in with Google",
    )
    page.frames = [page, google_frame]
    snap = asyncio.run(s.snapshot())
    assert "[iframe https://accounts.google.com/gsi/button]" in snap["elements"]
    assert 'aria-label="Sign in with Google"' in snap["elements"]
    assert "Sign in with Google" in snap["text"]


def test_snapshot_skips_empty_and_blank_iframes():
    s, page = _session_with_fake_page()
    empty_frame = _FakeFrame("https://ads.example/frame", elements=[], text="")
    blank_frame = _FakeFrame("about:blank", elements=[{"tag": "button", "visible": True}], text="")
    page.frames = [page, empty_frame, blank_frame]
    snap = asyncio.run(s.snapshot())
    assert "ads.example" not in snap["elements"]
    assert "about:blank" not in snap["elements"]


def test_click_falls_back_to_an_iframe_when_the_main_frame_does_not_have_it():
    async def go():
        s, page = _session_with_fake_page()
        recaptcha_frame = _FakeFrame(
            "https://www.google.com/recaptcha/api2/anchor",
            clickable_selectors={"#recaptcha-anchor"},
        )
        page.frames = [page, recaptcha_frame]
        page.clickable_selectors = set()  # not on the main frame — force the fallback
        await s.click("#recaptcha-anchor")
        assert page.clicks == [], "must not have matched anything in the main frame"
        assert recaptcha_frame.clicks == [("#recaptcha-anchor", 1)]
    asyncio.run(go())


def test_click_prefers_the_main_frame_when_present_there_too():
    async def go():
        s, page = _session_with_fake_page()
        other_frame = _FakeFrame("https://x.test/iframe", clickable_selectors={"#btn"})
        page.frames = [page, other_frame]
        await s.click("#btn")
        assert page.clicks == [("#btn", 1)]
        assert other_frame.clicks == [], "the main frame already had it — no need to fall back"
    asyncio.run(go())


def test_click_raises_the_original_error_when_no_frame_has_the_selector():
    async def go():
        s, page = _session_with_fake_page()
        other_frame = _FakeFrame("https://x.test/iframe", clickable_selectors={"#somewhere-else"})
        page.frames = [page, other_frame]
        page.clickable_selectors = set()
        try:
            await s.click("#nowhere")
            assert False, "must re-raise"
        except TimeoutError as e:
            assert "#nowhere" in str(e)
    asyncio.run(go())


# ── ambiguous selectors: refuse, never silently act on the first match ─────────
# Verified empirically against real Playwright: page.click(selector) silently picks
# the first of several matches (this is how "a.ps-button:visible, button:visible"
# once clicked "Exit" instead of the intended button and reset a wizard).
# Locator.click()/select_option()/set_input_files() are strict by default and raise
# a "strict mode violation" instead — browser_pane now uses those exclusively.

def test_is_strict_mode_violation_matches_playwrights_own_wording():
    from browser_pane import _is_strict_mode_violation
    assert _is_strict_mode_violation(Exception('strict mode violation: locator(".x") resolved to 2 elements'))
    assert not _is_strict_mode_violation(TimeoutError('Timeout 3000ms exceeded waiting for locator("#x")'))


def test_click_refuses_an_ambiguous_selector_on_the_main_frame():
    async def go():
        s, page = _session_with_fake_page()
        page.ambiguous_selectors = {".ps-button"}
        try:
            await s.click(".ps-button")
            assert False, "must refuse instead of clicking the first match"
        except Exception as e:
            assert "strict mode violation" in str(e)
        assert page.clicks == []
    asyncio.run(go())


def test_click_ambiguity_is_not_masked_by_the_iframe_fallback():
    """An ambiguous selector in the main frame must error immediately — falling
    back to an iframe could land on a coincidental, unrelated single match there,
    which is arguably worse than just refusing."""
    async def go():
        s, page = _session_with_fake_page()
        page.ambiguous_selectors = {".ps-button"}
        other_frame = _FakeFrame("https://x.test/iframe", clickable_selectors={".ps-button"})
        page.frames = [page, other_frame]
        try:
            await s.click(".ps-button")
            assert False, "must refuse, not fall back"
        except Exception as e:
            assert "strict mode violation" in str(e)
        assert other_frame.clicks == [], "must never have tried the iframe"
    asyncio.run(go())


def test_click_with_nth_disambiguates():
    async def go():
        s, page = _session_with_fake_page()
        page.ambiguous_selectors = {".ps-button"}
        await s.click(".ps-button", nth=1)
        assert page.clicks == [(".ps-button", 1)]
    asyncio.run(go())


def test_select_option_refuses_an_ambiguous_selector():
    async def go():
        s, page = _session_with_fake_page()
        page.ambiguous_selectors = {".dup-select"}
        try:
            await s.select_option(".dup-select", "NA")
            assert False, "must refuse"
        except Exception as e:
            assert "strict mode violation" in str(e)
        assert page.selects == []
    asyncio.run(go())


def test_upload_file_refuses_an_ambiguous_selector(tmp_path):
    async def go():
        s, page = _session_with_fake_page()
        f = tmp_path / "logo.png"
        f.write_bytes(b"x")
        page.ambiguous_selectors = {".dup-file"}
        try:
            await s.upload_file(".dup-file", str(f))
            assert False, "must refuse"
        except Exception as e:
            assert "strict mode violation" in str(e)
        assert page.uploads == []
    asyncio.run(go())


def test_click_does_not_fall_back_to_frames_on_a_dead_connection():
    """A dead session should fail fast and retire — searching every iframe for a
    selector that was never going to be found on a broken connection just wastes
    time before the (correct) retirement kicks in."""
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        other_frame = _FakeFrame("https://x.test/iframe", clickable_selectors={"#btn"})
        page.frames = [page, other_frame]
        page.fail_with = RuntimeError("Connection closed while reading from the driver")
        try:
            await s.click("#btn")
            assert False, "must re-raise"
        except RuntimeError:
            pass
        assert other_frame.clicks == [], "must not have tried the iframe at all"
        assert s._closed is True
    asyncio.run(go())


def test_type_text_selector_also_falls_back_to_an_iframe():
    async def go():
        s, page = _session_with_fake_page()
        otp_frame = _FakeFrame("https://login.example/otp", clickable_selectors={"#code-0"})
        page.frames = [page, otp_frame]
        page.clickable_selectors = set()
        await s.type_text("581702", selector="#code-0")
        assert otp_frame.clicks == [("#code-0", 3)]
        assert page.keyboard.typed == ["581702"], "keyboard.type() is frame-agnostic — dispatched at the page level"
    asyncio.run(go())


# ── file upload: clicking an "Upload" button only opens the OS's native file ───
# picker, which is invisible to click()/type() alike, and a file input refuses
# programmatic/keyboard text entry. upload_file() must set the file directly.

def test_upload_file_sets_files_on_the_main_frame(tmp_path):
    async def go():
        s, page = _session_with_fake_page()
        f = tmp_path / "logo.png"
        f.write_bytes(b"fake-png")
        await s.upload_file("#logo-input", str(f))
        assert page.uploads == [("#logo-input", str(f))]
    asyncio.run(go())


def test_upload_file_rejects_a_path_that_does_not_exist_on_this_server():
    async def go():
        s, page = _session_with_fake_page()
        try:
            await s.upload_file("#logo-input", "/nowhere/does-not-exist.png")
            assert False, "must raise before ever touching the page"
        except FileNotFoundError:
            pass
        assert page.uploads == [], "must not have attempted the CDP call at all"
    asyncio.run(go())


def test_upload_file_falls_back_to_an_iframe(tmp_path):
    async def go():
        s, page = _session_with_fake_page()
        f = tmp_path / "logo.png"
        f.write_bytes(b"fake-png")
        embed_frame = _FakeFrame("https://uploads.example/embed", clickable_selectors={"#file"})
        page.frames = [page, embed_frame]
        page.clickable_selectors = set()  # not on the main frame — force the fallback
        await s.upload_file("#file", str(f))
        assert page.uploads == []
        assert embed_frame.uploads == [("#file", str(f))]
    asyncio.run(go())


def test_upload_file_retires_session_on_dead_connection(tmp_path):
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        f = tmp_path / "logo.png"
        f.write_bytes(b"fake-png")
        page.fail_with = RuntimeError("Connection closed while reading from the driver")
        try:
            await s.upload_file("#logo-input", str(f))
            assert False, "must re-raise"
        except RuntimeError:
            pass
        assert s._closed is True
        assert "k" not in browser_pane._SESSIONS
    asyncio.run(go())


# ── native <select>: clicking pops OS/browser-native list UI outside the page ──
# (same class of problem as a file input's OS picker) — select_option() sets the
# value directly via CDP instead.

def test_select_option_uses_value_first():
    async def go():
        s, page = _session_with_fake_page()
        await s.select_option("#employees", "freelancer")
        assert page.selects == [("#employees", "freelancer", None)]
    asyncio.run(go())


def test_select_option_falls_back_to_label_when_value_does_not_match():
    async def go():
        s, page = _session_with_fake_page()
        page.select_value_ok = False  # no <option> has this literal value attribute
        await s.select_option("#employees", "Freelancer")
        assert page.selects == [("#employees", None, "Freelancer")]
    asyncio.run(go())


def test_select_option_falls_back_to_an_iframe():
    async def go():
        s, page = _session_with_fake_page()
        embed_frame = _FakeFrame("https://embed.example/form", clickable_selectors={"#rate"})
        page.frames = [page, embed_frame]
        page.clickable_selectors = set()  # not on the main frame — force the fallback
        await s.select_option("#rate", "NA")
        assert page.selects == []
        assert embed_frame.selects == [("#rate", "NA", None)]
    asyncio.run(go())


def test_select_option_retires_session_on_dead_connection():
    async def go():
        s, page = _session_with_fake_page()
        browser_pane._SESSIONS["k"] = s
        page.fail_with = RuntimeError("Target closed")
        try:
            await s.select_option("#employees", "freelancer")
            assert False, "must re-raise"
        except RuntimeError:
            pass
        assert s._closed is True
        assert "k" not in browser_pane._SESSIONS
    asyncio.run(go())


def test_format_interactive_elements_shows_select_options_and_invalid_marker():
    from browser_pane import _format_interactive_elements
    out = _format_interactive_elements([
        {
            "tag": "select", "id": "employees", "invalid": True,
            "selected": "", "options": [
                {"value": "", "label": "Select..."},
                {"value": "freelancer", "label": "Freelancer"},
            ],
        },
    ])
    assert '[0] select id="employees" ⚠INVALID' in out
    assert 'options: *="Select...",  freelancer="Freelancer"' in out


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


# ── browser_screenshot: full resolution, not the live pane's downscaled feed ───

class _FakeCDPCaptureRecording:
    def __init__(self):
        self.calls = []

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Page.captureScreenshot":
            import base64 as _b64_local
            return {"data": _b64_local.b64encode(b"FULLRES-JPEG-BYTES").decode()}
        return {}


def test_screenshot_uses_full_quality_not_the_stream_downscale():
    async def go():
        s, page = _session_with_fake_page()
        s._cdp = _FakeCDPCaptureRecording()
        data = await s.screenshot()
        assert data == b"FULLRES-JPEG-BYTES"
        method, params = s._cdp.calls[-1]
        assert method == "Page.captureScreenshot"
        assert params["quality"] == 85, "must NOT reuse STREAM's low quality (45) used for the live pane"
        assert "maxWidth" not in params, "no downscale — full VIEWPORT resolution"
    asyncio.run(go())


def test_screenshot_raises_without_an_active_cdp_session():
    async def go():
        s, page = _session_with_fake_page()
        s._cdp = None
        try:
            await s.screenshot()
            assert False, "must raise"
        except RuntimeError as e:
            assert "CDP" in str(e)
    asyncio.run(go())


def test_format_interactive_elements_marks_hidden_and_text():
    from browser_pane import _format_interactive_elements
    out = _format_interactive_elements([
        {"tag": "a", "href": "https://x.test", "text": "Sign in", "visible": False},
    ])
    assert out == '[0] a href="https://x.test" (hidden) — "Sign in"'


def test_format_interactive_elements_shows_checked_state():
    """Mirrors the <select> '*' marker — a checkbox toggled via its <label> (not
    the input itself) had no visible confirmation anywhere short of a screenshot."""
    from browser_pane import _format_interactive_elements
    out = _format_interactive_elements([
        {"tag": "input", "type": "checkbox", "id": "agree", "checked": True},
        {"tag": "input", "type": "checkbox", "id": "newsletter", "checked": False},
        {"tag": "div", "role": "checkbox", "id": "custom", "checked": "mixed"},
        {"tag": "button", "id": "save"},  # no checked state at all — no prefix
    ])
    lines = out.split("\n")
    assert lines[0] == '[0] [x] input type="checkbox" id="agree"'
    assert lines[1] == '[1] [ ] input type="checkbox" id="newsletter"'
    assert lines[2] == '[2] [mixed] div id="custom" role="checkbox"'
    assert lines[3] == '[3] button id="save"'


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
        self.closed = False

    async def send_json(self, obj):
        self.sent_json.append(obj)

    async def send_bytes(self, b):
        self.sent_bytes.append(b)

    async def close(self):
        self.closed = True


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


# ── screencast self-heal ─────────────────────────────────────────────────────
# 2026-08-27: the manual CDP session backing Page.startScreencast (+ the operator's
# raw mouse/key input) can die WITHOUT the browser itself disconnecting — a renderer
# crash, a cross-process navigation target swap, a blip on a remote external-cdp host.
# Playwright's own Page API (goto/click/type_text/... — everything the agent's tools
# in browser_tools.py call) is backed by its OWN, separately managed session and rides
# straight through this; only the operator's screencast goes dark, silently, because
# nothing raises when an event stream just stops being fed. These tests cover the
# re-arm that closes that gap.

class _FakeCDPRearm:
    """A CDP session that records Page.startScreencast/captureScreenshot calls and
    lets a test decide whether THIS attempt succeeds, without a real Chromium."""
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.handlers: "dict[str, list]" = {}
        self.detached = False

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    async def detach(self):
        self.detached = True

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Page.startScreencast" and self.fail:
            raise RuntimeError("Target closed")
        if method == "Page.captureScreenshot":
            return {"data": _b64.b64encode(b"REARMED").decode()}
        return {}


class _FakeCtxRearm:
    """context.new_cdp_session(page) stand-in — one fake CDP session per call, whose
    startScreencast succeeds or fails per the next entry of `outcomes` (True = fails)."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sessions: "list[_FakeCDPRearm]" = []

    async def new_cdp_session(self, page):
        fail = self.outcomes.pop(0) if self.outcomes else False
        cdp = _FakeCDPRearm(fail=fail)
        self.sessions.append(cdp)
        return cdp


def _rearm_session(outcomes) -> "tuple[BrowserSession, _FakeCtxRearm]":
    s = BrowserSession("k")
    s._started = True
    s._page = _FakePage()
    ctx = _FakeCtxRearm(outcomes)
    s._ctx = ctx
    return s, ctx


def test_bind_active_wires_a_close_listener_and_bumps_the_generation():
    """Every (re)arm of the screencast must register for the CDPSession's own "close"
    event (Playwright: fires when the target closes OR detach() is called) and bump
    _cdp_gen — the generation guard that tells a stale close event apart from a live
    one."""
    async def go():
        s, ctx = _rearm_session([])
        page = s._page
        await s._bind_active(page, prime=False)
        assert s._cdp_gen == 1
        assert s._cdp.handlers.get("close"), "must listen for the CDPSession's own close event"
        assert ("Page.startScreencast", {
            "format": "jpeg", "quality": browser_pane.STREAM["quality"],
            "maxWidth": browser_pane.STREAM["width"], "maxHeight": browser_pane.STREAM["height"],
            "everyNthFrame": 1,
        }) in s._cdp.calls
    asyncio.run(go())


def test_cdp_session_closed_rearms_only_for_the_current_generation():
    """A "close" event from a session we already intentionally replaced (a normal tab
    switch also fires this same event, per Playwright's docs) must NOT trigger a
    needless re-arm — only a close for the session that is STILL current does."""
    async def go():
        s = BrowserSession("k")
        rearmed = []
        async def fake_rearm(reason):
            rearmed.append(reason)
        s._rearm_screencast = fake_rearm
        s._cdp_gen = 5
        s._on_cdp_session_closed(object(), 3)   # stale — belongs to a superseded session
        await asyncio.sleep(0)
        assert rearmed == []
        s._on_cdp_session_closed(object(), 5)   # current — the live session actually died
        await asyncio.sleep(0)
        assert rearmed == ["the live-view connection dropped"]
    asyncio.run(go())


def test_cdp_session_closed_is_a_noop_once_the_session_is_closed():
    s = BrowserSession("k")
    rearmed = []
    s._rearm_screencast = lambda reason: rearmed.append(reason)
    s._closed = True
    s._cdp_gen = 1
    s._on_cdp_session_closed(object(), 1)
    assert rearmed == []


def test_rearm_screencast_recreates_the_session_and_reprimes_subscribers():
    """The success path: a fresh CDP session is armed on the SAME page and subscribers
    get a frame immediately (no waiting for the next on-change screencast event) — and
    no error reaches them, since the operator should never see a flicker for a
    recovery that happened within the bounded retry budget."""
    async def go():
        s, ctx = _rearm_session([])  # first attempt succeeds
        ws = _FakeWS()
        s._subs.add(ws)
        await s._rearm_screencast("the live-view connection dropped")
        await asyncio.sleep(0)  # let the fire-and-forget _send_frame task run
        assert len(ctx.sessions) == 1
        assert s._cdp is ctx.sessions[0]
        assert s._cdp_gen == 1
        assert ws.sent_bytes == [b"REARMED"], "subscriber must be re-primed with a fresh frame"
        assert ws.sent_json == [], "a recovery within budget must be invisible, not flash an error"
        assert s._rearming is False
    asyncio.run(go())


def test_rearm_screencast_retries_with_bounded_backoff_then_recovers(monkeypatch):
    """Two failed attempts, then a third that succeeds — bounded and retried, not
    instant-give-up and not an infinite loop."""
    async def go():
        monkeypatch.setattr(browser_pane, "_REARM_DELAYS", (0.0, 0.0, 0.0, 0.0))
        s, ctx = _rearm_session([True, True, False])  # fail, fail, succeed
        await s._rearm_screencast("the live-view connection dropped")
        assert len(ctx.sessions) == 3
        assert s._cdp is ctx.sessions[-1]
    asyncio.run(go())


def test_rearm_screencast_gives_up_and_broadcasts_a_visible_error(monkeypatch):
    """Every attempt in the bounded budget fails — the pane must never sit on a stale
    frame with no signal anything is wrong; a visible error reaches every subscriber
    instead."""
    async def go():
        monkeypatch.setattr(browser_pane, "_REARM_DELAYS", (0.0, 0.0, 0.0, 0.0))
        s, ctx = _rearm_session([True, True, True, True])  # every attempt fails
        ws = _FakeWS()
        s._subs.add(ws)
        await s._rearm_screencast("the live-view connection dropped")
        assert len(ctx.sessions) == 4
        assert ws.sent_json and ws.sent_json[-1]["type"] == "error"
        assert "live-view connection dropped" in ws.sent_json[-1]["message"]
        assert s._rearming is False
    asyncio.run(go())


def test_rearm_screencast_does_not_overlap_with_itself():
    """A second trigger firing while a re-arm is already in flight must be a no-op —
    otherwise a burst of "close" events (or a crash immediately followed by another)
    would stack overlapping CDP session churn."""
    async def go():
        s, ctx = _rearm_session([])
        s._rearming = True
        await s._rearm_screencast("should not run")
        assert ctx.sessions == [], "an overlapping call must not touch the browser at all"
    asyncio.run(go())


def test_on_crash_schedules_a_rearm_instead_of_an_unconditional_error():
    """A renderer crash routes through the SAME re-arm as a closed CDP session — one
    consistent story (silent recovery within budget, a visible error only once every
    retry fails) instead of an unconditional "it's broken" message a fast recovery
    would have no way to take back."""
    async def go():
        s = BrowserSession("k")
        seen = []
        async def fake_rearm(reason):
            seen.append(reason)
        s._rearm_screencast = fake_rearm
        s._on_crash(None)
        await asyncio.sleep(0)
        assert seen and "crashed" in seen[0]
    asyncio.run(go())


def test_capture_frame_failure_triggers_a_rearm_on_a_dead_connection():
    """_prime() is how a freshly (re)joined subscriber gets its first frame — if the
    session is already dead this must not just swallow the failure and leave that
    subscriber blank forever."""
    async def go():
        s = BrowserSession("k")
        class _DeadCDP:
            async def send(self, method, params=None):
                raise RuntimeError("Target closed")
        s._cdp = _DeadCDP()
        rearmed = []
        async def fake_rearm(reason):
            rearmed.append(reason)
        s._rearm_screencast = fake_rearm
        frame = await s._capture_frame()
        assert frame is None
        await asyncio.sleep(0)
        assert rearmed
    asyncio.run(go())


def test_capture_frame_failure_does_not_rearm_on_a_plain_error():
    """A transient, non-connection screenshot error (e.g. mid-navigation) must not
    retire and rebuild a perfectly healthy session."""
    async def go():
        s = BrowserSession("k")
        class _FlakyCDP:
            async def send(self, method, params=None):
                raise RuntimeError("Cannot take screenshot: no viewport")
        s._cdp = _FlakyCDP()
        rearmed = []
        async def fake_rearm(reason):
            rearmed.append(reason)
        s._rearm_screencast = fake_rearm
        frame = await s._capture_frame()
        assert frame is None
        await asyncio.sleep(0)
        assert rearmed == []
    asyncio.run(go())


def test_close_notifies_and_closes_open_subscribers():
    """A subscriber's WebSocket resolves to one BrowserSession object for its whole
    life (see webapp.py's api_browser_ws) and is never re-resolved — once THIS session
    retires, an already-open WS must be told and closed, or it would sit open and
    silent forever while a fresh session (rebuilt under the same project key) quietly
    takes over driving the page for the agent."""
    async def go():
        s = BrowserSession("k")
        s._started = True
        ws = _FakeWS()
        s._subs.add(ws)
        await s.close()
        assert ws.closed is True
        assert ws.sent_json and ws.sent_json[-1]["type"] == "error"
        assert s._subs == set()
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
    s._tabs = {"t1": page}
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
    s._tabs = {"t1": page}
    s._browser = _SharedFakePage()
    s._ctx = _SharedFakePage()
    s._pw = None
    asyncio.run(s._teardown())
    assert page.closed is False


def test_teardown_closes_every_tracked_tab_not_just_the_active_one():
    """Found via a live incident: a shared Cloak Manager profile ('google')
    accumulated 74 renderer processes / 4.4GB RSS over 6 days because teardown only
    ever closed the currently-ACTIVE tab. A multi-tab session (agent-opened popups,
    operator '+' tabs) leaked every other tab as a permanent zombie on every
    restart/module-disable, eventually making the shared profile's CDP endpoint
    intermittently unreachable for every project using it."""
    async def go():
        active, bg1, bg2 = _SharedFakePage(), _SharedFakePage(), _SharedFakePage()
        s = BrowserSession("k")
        s._owns_browser = False
        s._owns_page = True
        s._page = active
        s._tabs = {"t1": active, "t2": bg1, "t3": bg2}
        s._browser = _SharedFakePage()
        s._ctx = _SharedFakePage()
        s._pw = None
        await s._teardown()
        assert active.closed is True
        assert bg1.closed is True, "a background tab must not leak just because it wasn't active"
        assert bg2.closed is True
    asyncio.run(go())


# ── captcha: detection + token injection ───────────────────────────────────────
# The network half (2captcha API) is covered in test_captcha_solver.py; these
# tests pin the page-side contract: what we detect, what we refuse, and that the
# token actually reaches the page instead of being solved and dropped on the floor.

def _captcha_session(detect_result, inject_result=None):
    """A session whose evaluate() answers the detect script and the inject script
    differently — matched by content, since both are module-level JS constants."""
    s, page = _session_with_fake_page()
    calls = {"inject_arg": None}

    def router(script, arg):
        if "challengePage" in script:
            return detect_result
        calls["inject_arg"] = arg
        return inject_result or {"fields": [], "callbacks": [], "notes": []}
    page.eval_router = router
    return s, page, calls


def _stub_solver(monkeypatch, solution=None, capture=None):
    import captcha_solver as cs

    async def _solve(task, budget=180.0):
        if capture is not None:
            capture["task"] = task
            capture["budget"] = budget
        return solution or {"gRecaptchaResponse": "TOKEN123", "_cost": "0.003", "_seconds": 6.0}
    monkeypatch.setattr(cs, "solve", _solve)
    return cs


def test_detect_finds_a_recaptcha_widget_div():
    s, page, _ = _captcha_session({
        "url": "https://x.test/login", "challengePage": False, "hasResponseField": True,
        "widgets": [{"kind": "recaptcha_v2", "sitekey": "6Lc-KEY", "callback": "onSolved", "source": "widget"}],
    })
    info = asyncio.run(s.detect_captcha())
    assert info["widgets"][0]["kind"] == "recaptcha_v2"
    assert info["widgets"][0]["sitekey"] == "6Lc-KEY"


def test_detect_is_free_and_makes_no_api_call(monkeypatch):
    """Detection must never spend balance — it's the cheap 'is there even a captcha'
    check an agent is told to run before paying for a solve."""
    import captcha_solver as cs

    async def _boom(*a, **k):
        raise AssertionError("detect_captcha must not touch the 2captcha API")
    monkeypatch.setattr(cs, "solve", _boom)
    monkeypatch.setattr(cs, "_post", _boom)
    s, page, _ = _captcha_session({"url": "https://x.test", "challengePage": False, "widgets": []})
    assert asyncio.run(s.detect_captcha())["widgets"] == []


def test_solve_injects_the_token_and_reports_the_field_and_callback(monkeypatch):
    capture = {}
    _stub_solver(monkeypatch, capture=capture)
    s, page, calls = _captcha_session(
        {"url": "https://x.test/login", "challengePage": False, "hasResponseField": True,
         "widgets": [{"kind": "recaptcha_v2", "sitekey": "6Lc-KEY", "callback": "onSolved"}]},
        inject_result={"fields": ["#g-recaptcha-response"], "callbacks": ["grecaptcha.callback"], "notes": []},
    )
    res = asyncio.run(s.solve_captcha())

    assert capture["task"] == {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": "https://x.test/login",
        "websiteKey": "6Lc-KEY",
    }
    # The solved token must actually be handed to the page, with the widget's own
    # data-callback name, or the site never learns the captcha passed.
    assert calls["inject_arg"] == ["TOKEN123", "recaptcha_v2", "onSolved"]
    assert res["injected"] is True
    assert res["callbacks"] == ["grecaptcha.callback"]
    assert res["cost"] == "0.003"


def test_solve_marks_not_injected_when_no_response_field_was_found(monkeypatch):
    """Paying for a token and finding nowhere to put it is a FAILURE the agent has
    to see — reporting success here is what would make it submit a dead form."""
    _stub_solver(monkeypatch)
    s, page, _ = _captcha_session(
        {"url": "https://x.test", "challengePage": False,
         "widgets": [{"kind": "hcaptcha", "sitekey": "KEY"}]},
        inject_result={"fields": [], "callbacks": [], "notes": ["no response field and no callback found"]},
    )
    res = asyncio.run(s.solve_captcha())
    assert res["injected"] is False
    assert res["callbacks"] == []
    assert res["notes"]


def test_invisible_recaptcha_sets_the_is_invisible_flag(monkeypatch):
    capture = {}
    _stub_solver(monkeypatch, capture=capture)
    s, page, _ = _captcha_session({
        "url": "https://x.test", "challengePage": False,
        "widgets": [{"kind": "recaptcha_v2", "sitekey": "KEY", "size": "invisible"}],
    })
    asyncio.run(s.solve_captcha())
    assert capture["task"]["isInvisible"] is True


def test_recaptcha_v3_forwards_the_page_action(monkeypatch):
    capture = {}
    _stub_solver(monkeypatch, capture=capture)
    s, page, _ = _captcha_session({
        "url": "https://x.test", "challengePage": False,
        "widgets": [{"kind": "recaptcha_v3", "sitekey": "KEY", "action": "login"}],
    })
    asyncio.run(s.solve_captcha())
    assert capture["task"]["pageAction"] == "login"


def test_full_page_cloudflare_challenge_is_refused_before_spending_money(monkeypatch):
    """A full-page interstitial needs action/cData/chlPageData AND an IP-bound token,
    so a proxyless solve is worthless here. Refuse BEFORE calling the API — a paid
    token that cannot possibly be accepted is the worst of both outcomes."""
    import captcha_solver as cs

    async def _boom(*a, **k):
        raise AssertionError("must not pay for a challenge-page token")
    monkeypatch.setattr(cs, "solve", _boom)
    s, page, _ = _captcha_session({"url": "https://x.test", "challengePage": True, "widgets": []})
    try:
        asyncio.run(s.solve_captcha())
        assert False, "expected a refusal"
    except RuntimeError as e:
        assert "full-page Cloudflare challenge" in str(e)
        assert "by hand" in str(e)


def test_no_captcha_on_the_page_is_a_clear_error_not_a_silent_solve(monkeypatch):
    import captcha_solver as cs

    async def _boom(*a, **k):
        raise AssertionError("nothing to solve — must not call the API")
    monkeypatch.setattr(cs, "solve", _boom)
    s, page, _ = _captcha_session({"url": "https://x.test/ok", "challengePage": False, "widgets": []})
    try:
        asyncio.run(s.solve_captcha())
        assert False, "expected an error"
    except RuntimeError as e:
        assert "No captcha widget found" in str(e)


def test_image_captcha_returns_text_and_injects_nothing(monkeypatch):
    """A distorted-text captcha has no response field — the answer comes back as
    text for the agent to type, and claiming it was 'injected' would be a lie."""
    capture = {}
    _stub_solver(monkeypatch, solution={"text": "3XZ9K", "_cost": "0.0005", "_seconds": 4.0}, capture=capture)
    s, page, _ = _captcha_session({"url": "https://x.test", "challengePage": False, "widgets": []})

    class _ShotLocator:
        async def screenshot(self, timeout=None):
            return b"\xff\xd8fake-jpeg"
    page.locator = lambda sel: _ShotLocator()

    res = asyncio.run(s.solve_captcha(image_selector="#captcha-img"))
    assert res["mode"] == "image"
    assert res["text"] == "3XZ9K"
    assert res["injected"] is False
    assert capture["task"]["type"] == "ImageToTextTask"
    assert capture["task"]["body"]  # base64 of the element screenshot


def test_solve_reports_extra_widgets_instead_of_pretending_the_page_is_clear(monkeypatch):
    _stub_solver(monkeypatch)
    s, page, _ = _captcha_session({
        "url": "https://x.test", "challengePage": False,
        "widgets": [{"kind": "recaptcha_v2", "sitekey": "A"}, {"kind": "hcaptcha", "sitekey": "B"}],
    })
    res = asyncio.run(s.solve_captcha())
    assert res["extra_widgets"] == 1


def test_teardown_hands_the_manager_profile_back_so_it_can_be_stopped():
    """The other half of the idle-profile fix: browser_backends can only refcount a
    profile if the session actually releases it. Without this the profile stays
    'attached' forever and the idle-stop never fires — which is exactly how two
    profiles idled ~10h and cooked the host CPU."""
    async def go():
        released = []

        async def _release(profile, key):
            released.append((profile, key))
        orig = browser_pane._backends.release_profile
        browser_pane._backends.release_profile = _release
        try:
            page = _SharedFakePage()
            s = BrowserSession("/proj/a")
            s._owns_browser = False
            s._owns_page = True
            s._page = page
            s._tabs = {"t1": page}
            s._profile = "d33e103d"
            s._pw = None
            await s._teardown()
            assert released == [("d33e103d", "/proj/a")]
            # …and only once: a second teardown must not double-release
            await s._teardown()
            assert released == [("d33e103d", "/proj/a")]
        finally:
            browser_pane._backends.release_profile = orig
    asyncio.run(go())


def test_teardown_releases_nothing_for_a_non_manager_backend():
    async def go():
        released = []

        async def _release(profile, key):
            released.append(profile)
        orig = browser_pane._backends.release_profile
        browser_pane._backends.release_profile = _release
        try:
            s = BrowserSession("/proj/a")
            s._owns_browser = True
            s._profile = ""          # builtin / cloakbrowser
            s._pw = None
            await s._teardown()
            assert released == []
        finally:
            browser_pane._backends.release_profile = orig
    asyncio.run(go())


# ── operator page zoom (−/+ buttons, Ctrl+wheel) ──────────────────────────────


class _ZoomPage:
    """Records page.evaluate() calls; enough for the zoom path."""

    def __init__(self):
        self.evaluated = []
        self.url = "https://example.com/"

    async def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))

    async def title(self):
        return "Example"


def _zoom_session() -> BrowserSession:
    s = _session_with_fake_cdp()
    s._page = _ZoomPage()
    s._tabs = {"t1": s._page}
    s._active_id = "t1"
    return s


def test_zoom_ladder_steps_in_and_out_from_the_default():
    step = BrowserSession._zoom_target
    assert step(1.0, {"dir": "in"}) == 1.1
    assert step(1.0, {"dir": "out"}) == 0.9
    assert step(1.1, {"dir": "in"}) == 1.25


def test_zoom_ladder_clamps_at_both_ends():
    step = BrowserSession._zoom_target
    assert step(browser_pane.ZOOM_STEPS[-1], {"dir": "in"}) == browser_pane.ZOOM_STEPS[-1]
    assert step(browser_pane.ZOOM_STEPS[0], {"dir": "out"}) == browser_pane.ZOOM_STEPS[0]


def test_zoom_reset_and_absolute_factor_snaps_to_the_ladder():
    step = BrowserSession._zoom_target
    assert step(2.0, {"dir": "reset"}) == 1.0
    # An off-ladder request snaps to the nearest rung, so the next −/+ does not jump.
    assert step(1.0, {"factor": 1.27}) == 1.25
    assert step(1.0, {"factor": "nonsense"}) == 1.0


def test_zoom_off_ladder_current_value_recovers_to_the_default_rung():
    """A factor that somehow left the ladder must not make −/+ dead."""
    assert BrowserSession._zoom_target(1.37, {"dir": "in"}) == 1.1


def test_zoom_applies_css_zoom_and_broadcasts_the_new_factor():
    s = _zoom_session()
    sent = []
    s.broadcast_json = lambda obj: sent.append(obj) or _done()
    asyncio.run(s.handle_input({"t": "zoom", "dir": "in"}))
    assert s._zoom == 1.1
    script, arg = s._page.evaluated[-1]
    assert "documentElement.style.zoom" in script and arg == 1.1
    assert sent[-1] == {"type": "zoom", "factor": 1.1}


def test_zoom_of_100_percent_clears_the_style_instead_of_writing_1():
    """`zoom: 1` still creates a containing block on some sites — reset must remove it."""
    s = _zoom_session()
    s.broadcast_json = lambda obj: _done()
    s._zoom = 1.25
    asyncio.run(s.handle_input({"t": "zoom", "dir": "reset"}))
    script, arg = s._page.evaluated[-1]
    assert arg == 1.0 and "z === 1 ? ''" in script


def test_zoom_survives_navigation():
    """CSS zoom lives on the document, so a fresh one starts at 100%."""
    s = _zoom_session()
    s.broadcast_json = lambda obj: _done()
    s._broadcast_tabs = lambda: _done()
    s._zoom = 1.5
    asyncio.run(s._broadcast_nav())
    assert s._page.evaluated[-1][1] == 1.5


def test_default_zoom_does_not_touch_the_page_on_every_navigation():
    s = _zoom_session()
    s.broadcast_json = lambda obj: _done()
    s._broadcast_tabs = lambda: _done()
    asyncio.run(s._broadcast_nav())
    assert s._page.evaluated == []


def test_status_reports_the_current_zoom():
    s = _zoom_session()
    s._zoom = 1.75
    assert s.status()["zoom"] == 1.75


async def _done():
    return None

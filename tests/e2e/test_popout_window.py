"""
Pop-out project window (web/src/lib/popout.ts, components/PopoutApp.tsx).

The operator spreads the cockpit over several monitors: ⧉ opens ONE project — left pane
plus chat with the draggable divider — in its own window. The contract proved here is the
one that keeps the main window's mechanics intact:
  - the pop-out renders the project without the sidebar, and its chat works;
  - a turn sent from the pop-out also shows live in the main window;
  - the pop-out never writes the shared server layout (data/ui_state.json) — it is a
    single "default" namespace, so one write would wipe the main window's open tabs;
  - dragging the pop-out's divider does not move the main window's divider;
  - a second ⧉ click focuses the open pop-out instead of reloading it.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import json
import time

import pytest
from playwright.sync_api import expect

from .conftest import send_chat

pytestmark = pytest.mark.e2e


def _ui_state(app_dir):
    p = app_dir / "data" / "ui_state.json"
    return json.loads(p.read_text()) if p.exists() else None


def _wait_ui_state(app_dir, pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _ui_state(app_dir)
        if st is not None and pred(st):
            return st
        time.sleep(0.2)
    raise AssertionError(f"ui_state never matched; last: {_ui_state(app_dir)!r}")


def _open(page, project_id):
    """conftest.open_project waits for the FIRST .chat-textarea, which is the hidden one once
    a second project tab is mounted — wait for the visible one instead."""
    page.click(f".project-item:has-text('{project_id}')")
    page.wait_for_selector(".chat-textarea:visible", timeout=10_000)


def _layout(st):
    """The part of the saved layout a pop-out must never touch."""
    st = st.get("default", st)  # {namespace: state}; single-tenant "default"
    return {"open": st.get("open"), "active": st.get("active")}


def test_popout_window_keeps_main_layout(e2e_server, logged_in_page):
    page = logged_in_page
    app_dir = e2e_server["app_dir"]

    _open(page, "e2e-popout-a")
    _open(page, "e2e-popout-b")
    # Project ids come from the cwd folder name (e2e-proj-popout-a0), not the topic key.
    before = _wait_ui_state(
        app_dir,
        lambda st: len(_layout(st)["open"] or []) == 2
        and "popout-a" in _layout(st)["open"][0]
        and "popout-b" in (_layout(st)["active"] or ""),
    )
    popout_id = _layout(before)["active"]
    main_width = page.evaluate("localStorage.getItem('cops.chatWidth')")

    # ⧉ in the active project's header opens the pop-out.
    with page.context.expect_page() as popup_info:
        page.locator(".main-area .popout-open-btn:visible").click()
    popup = popup_info.value
    # The window opens as about:blank (open-by-name, see openProjectWindow) and navigates a
    # moment later — waiting for "load" alone can still see about:blank.
    popup.wait_for_url(lambda u: "popout=" in u, timeout=10_000)
    assert f"popout={popout_id}" in popup.url

    # One project, no sidebar, the same split layout with its divider.
    popup.wait_for_selector(".popout-bar", timeout=10_000)
    expect(popup.locator(".popout-bar-title")).to_contain_text("popout-b")
    expect(popup.locator(".sidebar")).to_have_count(0)
    expect(popup.locator(".chat-textarea")).to_have_count(1)
    expect(popup.locator(".project-split-divider")).to_have_count(1)
    # The browser module is off in this harness: the pop-out falls back to the board
    # instead of an empty browser pane.
    expect(popup.locator(".tab-btn.active")).to_have_text("Board")

    # Drag the pop-out's divider 200 px left: the pop-out gets its own width, the main
    # window's stays where it was.
    width_before = popup.evaluate("localStorage.getItem('cops.chatWidth.popout')")
    box = popup.locator(".project-split-divider").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    popup.mouse.move(x, y)
    popup.mouse.down()
    popup.mouse.move(x - 100, y, steps=5)
    popup.mouse.move(x - 200, y, steps=5)
    popup.mouse.up()
    popout_width = popup.evaluate("localStorage.getItem('cops.chatWidth.popout')")
    assert popout_width is not None, "the pop-out's divider width was not persisted under its own key"
    # ProjectView writes the key on mount, so presence alone proves nothing — the drag must move it.
    assert popout_width != width_before, "dragging the pop-out's divider did not change its width"
    assert page.evaluate("localStorage.getItem('cops.chatWidth')") == main_width

    # A turn sent from the pop-out renders there AND live in the main window.
    send_chat(popup, "e2e:text")
    reply = ".chat-msg-assistant .chat-msg-body:has-text('a scripted e2e reply.')"
    popup.wait_for_selector(reply, timeout=15_000)
    page.wait_for_selector(reply, timeout=15_000)

    # A second ⧉ click brings the existing pop-out forward without reloading it.
    popup.evaluate("window.__popoutMarker = 42")
    pages_before = len(page.context.pages)
    page.locator(".main-area .popout-open-btn:visible").click()
    page.wait_for_timeout(1500)
    assert len(page.context.pages) == pages_before, "a second click opened another window"
    assert popup.evaluate("window.__popoutMarker") == 42, "the open pop-out was reloaded"

    # Well past the 800 ms layout-save debounce: the server layout is still the main
    # window's two tabs.
    page.wait_for_timeout(2000)
    assert _layout(_ui_state(app_dir)) == _layout(before)
    popup.close()

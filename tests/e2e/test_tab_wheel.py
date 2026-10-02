"""
Mouse wheel over the open-tabs bar scrolls the strip sideways (web/src/lib/tabWheel.ts,
components/ProjectTabBar.tsx) — like a browser's tab strip.

Proved in a real browser against a real cockpit:
  - with overflowing tabs, a vertical wheel over the bar moves the strip's scrollLeft;
  - the wheel does NOT scroll the strip when the tabs fit (the wheel is left alone);
  - Ctrl+wheel (browser zoom) is not hijacked.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import pytest

pytestmark = pytest.mark.e2e

PROJECTS = ["e2e-text", "e2e-tool", "e2e-slow", "e2e-busy", "e2e-multiblock"]


def _open_tabs(page):
    for pid in PROJECTS:
        page.click(f".project-item:has-text('{pid}')")
        page.wait_for_selector(".chat-textarea:visible", timeout=10_000)


def _scroll_left(page):
    return page.evaluate("document.querySelector('.ptab-list').scrollLeft")


def _over_bar(page):
    box = page.locator(".project-tabbar").bounding_box()
    page.mouse.move(box["x"] + 200, box["y"] + box["height"] / 2)


def test_wheel_scrolls_overflowing_tabs_and_leaves_fitting_ones_alone(e2e_server, logged_in_page):
    page = logged_in_page
    page.set_viewport_size({"width": 1400, "height": 800})
    _open_tabs(page)
    assert page.locator(".ptab-list .ptab").count() >= 5

    # Fitting: wide strip, nothing to scroll -> the wheel must not move anything.
    fits = page.evaluate("(() => { const l = document.querySelector('.ptab-list'); return l.scrollWidth <= l.clientWidth })()")
    assert fits, "precondition: five tabs fit a 1400px bar"
    _over_bar(page)
    page.mouse.wheel(0, 300)
    page.wait_for_timeout(150)
    assert _scroll_left(page) == 0

    # Overflowing: squeeze the strip, then the wheel moves it sideways.
    page.evaluate("document.querySelector('.ptab-list').style.maxWidth = '180px'")
    overflow = page.evaluate("(() => { const l = document.querySelector('.ptab-list'); return l.scrollWidth > l.clientWidth })()")
    assert overflow
    page.evaluate("document.querySelector('.ptab-list').scrollLeft = 0")
    _over_bar(page)
    page.mouse.wheel(0, 120)
    page.wait_for_timeout(200)
    after_down = _scroll_left(page)
    assert after_down > 0, "vertical wheel over the bar did not scroll the tab strip"
    page.mouse.wheel(0, -60)
    page.wait_for_timeout(200)
    assert 0 <= _scroll_left(page) < after_down, "wheel up did not scroll back"

    # Ctrl+wheel is zoom: never hijacked.
    before = _scroll_left(page)
    page.keyboard.down("Control")
    page.mouse.wheel(0, 200)
    page.keyboard.up("Control")
    page.wait_for_timeout(150)
    assert _scroll_left(page) == before

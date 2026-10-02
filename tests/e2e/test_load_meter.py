"""
Load meter (web/src/lib/loadStatus.ts, components/LoadMeter.tsx, GET /api/system-load).

The contract proved against a REAL cockpit and a real browser:
  - the vertical meter renders next to the rate-limit pill and reports a verdict;
  - hovering it opens the detail panel (verdict + host line + normal signals);
  - it reads the real host through the real endpoint — no mocked numbers;
  - when the cockpit stops answering the meter does not keep showing the last green reading.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_meter_renders_and_opens_detail(e2e_server, logged_in_page):
    page = logged_in_page
    meter = page.locator(".load-meter").first
    expect(meter).to_be_visible(timeout=20_000)
    # settles on a real verdict (not stuck on "measuring")
    expect(meter).not_to_have_class("load-meter is-loading", timeout=20_000)
    assert page.locator(".load-seg").count() >= 5
    assert page.locator(".load-meter.is-ok .load-seg.on, .load-meter.is-warn .load-seg.on, "
                        ".load-meter.is-crit .load-seg.on, .load-meter.is-unknown").count() >= 1

    meter.hover()
    pop = page.locator(".load-pop")
    expect(pop).to_be_visible()
    expect(pop.locator(".load-pop-head")).to_contain_text("Server")
    expect(pop).to_contain_text("chats live")


def test_endpoint_is_authenticated_and_shaped(e2e_server, logged_in_page):
    page = logged_in_page
    data = page.evaluate("fetch('/api/system-load').then(r => r.json())")
    assert data["level"] in ("ok", "warn", "crit", "unknown")
    assert isinstance(data["signals"], list) and "chats" in data and "age_s" in data
    # every signal carries what the panel renders
    for s in data["signals"]:
        assert {"id", "level", "pressure", "value", "text", "hint"} <= set(s)
    # a caller with no session cookie gets 401, never host details
    anon = page.context.browser.new_context()
    try:
        r = anon.request.get(f"{e2e_server['base_url']}/api/system-load")
        assert r.status == 401
    finally:
        anon.close()


def test_meter_does_not_stay_green_when_the_server_stops_answering(e2e_server, logged_in_page):
    page = logged_in_page
    expect(page.locator(".load-meter").first).to_be_visible(timeout=20_000)
    # Cut the endpoint at the network layer: the page keeps running, the poll just fails.
    page.route("**/api/system-load", lambda route: route.abort())
    expect(page.locator(".load-meter.is-down")).to_be_visible(timeout=40_000)
    page.locator(".load-meter.is-down").hover()
    expect(page.locator(".load-pop-head")).to_contain_text("not responding")

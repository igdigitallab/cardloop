"""
A cold load mounts only the project tab in front of the operator.

Every open tab used to mount its whole ProjectView at once — chat, sessions, board, health — so a
saved layout of a dozen tabs fired ~280 requests before the visible chat could show. Now a tab
mounts the first time it is opened and stays mounted after that. The tab bar's busy and
reply-ready cues come from the global activity stream and the project list, so a tab that was
never opened must still light up when its agent runs and finishes.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import send_chat

pytestmark = pytest.mark.e2e


def _api_id(server, project: str) -> str:
    topics = json.loads((server["app_dir"] / "data" / "topics.json").read_text())
    return Path(topics[project]["cwd"]).name


def _activate(page, project: str) -> None:
    page.click(f".project-item:has-text('{project}')")
    expect(page.locator(".ptab", has_text=project)).to_have_class(re.compile(r"\bactive\b"), timeout=10_000)


def test_hidden_tabs_cost_nothing_until_opened_and_still_light_up(logged_in_page, e2e_server):
    page = logged_in_page
    tool, slow, text = (_api_id(e2e_server, p) for p in ("e2e-lazy-probe", "e2e-slow", "e2e-text"))

    _activate(page, "e2e-lazy-probe")
    send_chat(page, "lazy-tab-probe")
    page.wait_for_selector("text=lazy-tab-probe", timeout=10_000)
    _activate(page, "e2e-slow")
    _activate(page, "e2e-text")  # the tab in front after the reload
    # The layout is saved to the server 800 ms after it changes; a reload before that restores the
    # previous active tab from the server copy, and both tabs would mount.
    page.wait_for_timeout(1500)

    urls: list[str] = []
    page.on("request", lambda r: urls.append(r.url))
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    expect(page.locator(".ptab", has_text="e2e-text")).to_have_class(re.compile(r"\bactive\b"), timeout=10_000)
    page.wait_for_selector(".chat-textarea", timeout=10_000)
    page.wait_for_timeout(2500)  # every request a mounted tab makes on load has gone out by now

    assert page.locator(".ptab").count() == 3, "every saved tab is still in the tab bar"
    assert any(f"/api/projects/{text}/" in u for u in urls), "the visible tab loads"
    for hidden in (tool, slow):
        assert not [u for u in urls if f"/api/projects/{hidden}/" in u], f"hidden tab {hidden} loaded"
    api_calls = [u for u in urls if "/api/" in u]
    assert len(api_calls) <= 40, f"{len(api_calls)} API requests on a three-tab cold load"

    # Opening a hidden tab mounts it: its chat shows.
    page.locator(".ptab", has_text="e2e-lazy-probe").click()
    page.wait_for_selector(".project-tab-slot:visible >> text=lazy-tab-probe", timeout=10_000)
    assert any(f"/api/projects/{tool}/session-history" in u for u in urls)
    # …and it stays mounted: switching away and back keeps the very same view (its streams,
    # scroll and unsent text), not a fresh one that loads again.
    page.evaluate("document.querySelector('.project-tab-slot:not([style*=\"none\"])').__keepAlive = 1")
    page.locator(".ptab", has_text="e2e-text").click()
    expect(page.locator(".ptab", has_text="e2e-text")).to_have_class(re.compile(r"\bactive\b"))
    page.locator(".ptab", has_text="e2e-lazy-probe").click()
    expect(page.locator(".project-tab-slot:visible").get_by_text("lazy-tab-probe", exact=True)).to_be_visible()
    assert page.evaluate(
        "document.querySelector('.project-tab-slot:not([style*=\"none\"])').__keepAlive") == 1


def test_a_tab_never_opened_still_shows_its_agent_working_and_done(logged_in_page, e2e_server):
    page = logged_in_page
    _activate(page, "e2e-lazy-busy")
    _activate(page, "e2e-text")
    page.wait_for_timeout(1500)  # the layout save (see above)
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    expect(page.locator(".ptab", has_text="e2e-text")).to_have_class(re.compile(r"\bactive\b"), timeout=10_000)
    assert page.locator(".ptab", has_text="e2e-lazy-busy").count() == 1

    # A turn in the unmounted tab, started from outside the page (another device, a card, a wake).
    busy = _api_id(e2e_server, "e2e-lazy-busy")
    page.evaluate("""([pid]) => { fetch(`/api/projects/${pid}/chat`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt: 'e2e:slow'})}) }""",
                  [busy])
    busy_tab = page.locator(".ptab", has_text="e2e-lazy-busy")
    expect(busy_tab.locator(".ptab-working")).to_have_count(1, timeout=10_000)
    expect(busy_tab).to_have_class(re.compile(r"\bptab-awaiting\b"), timeout=30_000)

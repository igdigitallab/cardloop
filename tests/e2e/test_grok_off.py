"""
With GROK_ENABLED unset the whole UI is what it was before Grok existed (spec-095 P5b, (g)).

Runs on the PLAIN e2e cockpit (`e2e_server`: GROK_* stripped from its environment). Every surface
Grok touches is opened and the page text must not mention Grok anywhere: not as a picker option, a
runtime row, a Settings row, a board option, a usage chip or card.
"""
import pytest
from playwright.sync_api import expect

from .conftest import open_project

pytestmark = pytest.mark.e2e

PROJECT = "e2e-text"


def no_grok(page, where: str) -> None:
    text = page.locator("body").inner_text().lower()
    assert "grok" not in text, f"{where}: the page mentions Grok while GROK_ENABLED is off"
    assert page.locator("[data-provider=grok], [data-testid^=grok]").count() == 0, f"{where}: a Grok hook is in the DOM"


def test_no_registry_row_and_no_gate_for_grok(e2e_server, logged_in_page):
    page = logged_in_page
    r = page.request.get(e2e_server["base_url"] + "/api/agent-providers")
    assert r.ok
    assert "grok" not in [p["provider"] for p in r.json()["providers"]]
    # A Grok chat cannot be made through the API either.
    r = page.request.fetch(e2e_server["base_url"] + f"/api/projects/{PROJECT}/chats", method="POST",
                           data='{"provider":"grok"}', headers={"Content-Type": "application/json"})
    assert r.status >= 400, r.status


def test_no_surface_offers_grok(e2e_server, logged_in_page):
    page = logged_in_page
    open_project(page, PROJECT)
    no_grok(page, "project view")

    # New agent chat dialog: the providers that ARE listed, and only those.
    page.click(".chat-named-tab-new")
    page.wait_for_selector("text=New agent chat")
    assert page.locator(".provider-pick > button").count() >= 1
    no_grok(page, "new chat dialog")
    page.keyboard.press("Escape")
    page.locator("text=New agent chat").wait_for(state="hidden")

    # Model menu + runtime pill.
    page.click(".composer-modelthink-btn")
    expect(page.locator(".composer-modelthink-menu")).to_be_visible()
    no_grok(page, "model menu")
    page.keyboard.press("Escape")
    page.locator(".usage-badge").first.hover()
    expect(page.locator(".usage-dropdown")).to_be_visible()
    no_grok(page, "runtime pill dropdown")
    page.mouse.move(2, 2)

    # Board: the card editor's provider select.
    page.click(".tab-btn:has-text('Board')")
    box = page.locator("textarea[placeholder^='New task']")
    box.fill("a card with only listed engines")
    box.press("Enter")
    page.locator(".board-card:has-text('a card with only listed engines') .board-card-text").dblclick()
    page.wait_for_selector("text=Provider")
    options = page.locator("select:near(:text('Provider'))").first.locator("option").evaluate_all(
        "els => els.map(e => e.value)")
    assert "grok" not in options and "claude" in options, options
    no_grok(page, "board card editor")
    page.keyboard.press("Escape")

    # Settings: no privacy toggle, no Grok model row, no Grok board provider.
    page.click(".tab-btn:has-text('Settings')")
    page.wait_for_selector("[data-testid=board-provider]")
    expect(page.locator("[data-testid=grok-allowed]")).to_have_count(0)
    expect(page.locator("[data-testid=grok-board-model]")).to_have_count(0)
    opts = page.locator("[data-testid=board-provider] option").evaluate_all("els => els.map(e => e.value)")
    assert "grok" not in opts, opts
    no_grok(page, "settings")


def test_usage_tab_and_free_chat_dialog_do_not_mention_grok(e2e_server, logged_in_page):
    page = logged_in_page
    open_project(page, PROJECT)
    page.locator(".usage-badge").first.click()
    page.wait_for_selector(".usage-container")
    no_grok(page, "usage tab")
    expect(page.locator(".usage-card[data-provider=grok]")).to_have_count(0)
    page.click(".ptab-new")
    page.wait_for_selector("text=New free chat")
    no_grok(page, "free chat dialog")

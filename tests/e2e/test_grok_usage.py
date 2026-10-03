"""
E2E for the Usage tab's Grok card (spec-095 P5b, (h)) — real `/api/usage/dashboard` over the real
`data/grok_usage.jsonl` ledger: two rows pre-seeded by the fixture (tests/e2e/grok_support.py) plus
whatever the Grok turns of the other e2e files appended.

Grok runs on a flat subscription: the card shows turns and tokens, labels the API-list-price
figure "API-equivalent ... not spend", and never shows a Grok cost. The runtime pill row for Grok is
tested in test_grok_provider.py (muted, no bar).
"""
import re

import pytest
from playwright.sync_api import expect

from . import grok_support as gs
from .conftest import open_project
from .grok_ui import api

pytestmark = pytest.mark.e2e

if not gs.webapp_serves("_grok_usage.summary"):   # pragma: no cover - the wiring has landed
    pytestmark = [pytest.mark.e2e, pytest.mark.skip(
        reason="UNWIRED: /api/usage/dashboard does not serve providers.grok yet (spec-095 P4 wiring)")]


def _keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}".lstrip("."), k
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v, path)


def _open_usage(page):
    page.locator(".usage-badge").first.click()
    page.wait_for_selector(".usage-container")


def test_the_dashboard_carries_a_grok_block_with_no_spend_in_it(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    status, body = api(page, srv, "/api/usage/dashboard?days=30")
    assert status == 200, body
    grok = body["providers"]["grok"]
    assert grok["turns"] >= 2 and grok["input"] >= 20000 and grok["output"] >= 4500, grok
    assert {"grok-4.7", "grok-4.6"} <= set(grok["by_model"]), grok["by_model"]      # a RECORD keyed by model
    assert grok.get("limits") is None
    bad = [p for p, k in _keys(grok) if re.search(r"cost|spend", k, re.I)]
    assert not bad, f"a flat subscription must not report cost/spend: {bad}"
    # The Claude and Codex blocks the page already read are still there, untouched.
    assert "turns" in body["providers"]["claude"] and "turns" in body["providers"]["codex"]


def test_the_usage_tab_shows_a_grok_card_that_matches_the_api(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    _, body = api(page, srv, "/api/usage/dashboard?days=30")
    grok = body["providers"]["grok"]
    open_project(page, "g-text")
    _open_usage(page)

    chip = page.locator(".usage-seg button[data-provider=grok]")
    expect(chip).to_be_visible()
    card = page.locator(".usage-card[data-provider=grok]")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Grok subscription usage")
    labels = [t.strip().lower() for t in card.locator(".usage-stat .lbl").all_inner_texts()]
    assert labels == ["turns", "output tokens", "cached input", "api-equivalent"], labels   # no "cost", no "spend"
    expect(card).to_contain_text("not spend · SuperGrok subscription")
    # The numbers on screen are the API's, not a second computation.
    turns = card.locator(".usage-stat", has_text="Turns").locator(".val").inner_text()
    assert int(turns.replace(",", "")) == grok["turns"], (turns, grok["turns"])
    rows = card.locator("table.usage-table tbody tr")
    models = [r.locator("td").first.inner_text() for r in rows.all()]
    assert set(models) == set(grok["by_model"]), (models, grok["by_model"])
    # No dollar figure other than the labelled API-equivalent tile.
    other = card.locator(".usage-stat:not(:has-text('API-equivalent'))").all_inner_texts()
    assert not any("$" in t for t in other), other


def test_the_grok_filter_chip_narrows_the_tab_to_grok(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-text")
    _open_usage(page)
    claude_section = page.locator("text=/No usage indexed yet|Daily cost/")
    expect(claude_section.first).to_be_visible()                      # "All providers" shows Claude's overview
    page.locator(".usage-seg button[data-provider=grok]").click()
    expect(page.locator(".usage-card[data-provider=grok]")).to_be_visible()
    expect(claude_section).to_have_count(0)                           # ... the Grok filter does not
    expect(page.locator(".usage-card[data-provider=codex]")).to_have_count(0)

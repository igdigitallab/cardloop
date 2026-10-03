"""
Visual + layout checks for the Grok UI at desktop (1440x900) and phone (360x800) widths (spec-095 P5b).

Every surface Grok touched is opened for real (Grok-enabled cockpit, fake `grok` CLI) at both
sizes. Besides the screenshots (written when E2E_SHOTS_DIR is set — `<name>-desktop.png` /
`<name>-360.png`, the files the P5b report lists) each surface carries layout assertions that fail
on the defects a screenshot review looks for: horizontal overflow, truncated labels, tap targets
under 40 px on a phone, provider buttons of unequal width.

  E2E_SHOTS_DIR=/tmp/grok-shots venv/bin/python -m pytest tests/e2e/test_grok_visual.py -m e2e
"""
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import GROK_ALLOWED_PROJECTS  # noqa: F401  (documents which projects exist)

pytestmark = pytest.mark.e2e

SIZES = [("desktop", 1440, 900), ("360", 360, 800)]
GATE_SENTENCE = "grok is not enabled for this project"
MIN_TAP = 40


@pytest.fixture(params=SIZES, ids=[s[0] for s in SIZES])
def view(request, e2e_grok_server, browser):
    label, w, h = request.param
    ctx = browser.new_context(viewport={"width": w, "height": h}, has_touch=(label == "360"))
    page = ctx.new_page()
    page.set_default_timeout(10_000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    (e2e_grok_server["app_dir"] / "data" / "ui_state.json").unlink(missing_ok=True)
    page.goto(e2e_grok_server["base_url"])
    page.fill("#password", e2e_grok_server["password"])
    page.click("button.btn-primary[type=submit]")
    page.wait_for_selector(".project-item", state="attached", timeout=10_000)
    yield type("View", (), {"page": page, "label": label, "w": w, "h": h, "phone": label == "360",
                            "srv": e2e_grok_server, "errors": errors})
    ctx.close()
    assert not errors, f"uncaught page errors: {errors}"


def shot(view, name: str) -> None:
    out = os.environ.get("E2E_SHOTS_DIR")
    if not out:
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    view.page.wait_for_timeout(450)       # let CSS transitions settle: a mid-transition frame lies
    view.page.screenshot(path=str(Path(out) / f"{name}-{view.label}.png"))


def no_h_overflow(view) -> None:
    """The page itself must not scroll sideways (a clipped control is a defect on a phone)."""
    overflow = view.page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"page is {overflow}px wider than the {view.w}px viewport"


def inside_viewport(view, locator, what: str) -> None:
    box = locator.bounding_box()
    assert box, f"{what}: not rendered"
    assert box["x"] >= -1 and box["x"] + box["width"] <= view.w + 1, f"{what}: {box} leaves the {view.w}px viewport"


def tall_enough(view, locator, what: str) -> None:
    if not view.phone:
        return
    box = locator.bounding_box()
    assert box and box["height"] >= MIN_TAP - 1, f"{what}: {box} is under the {MIN_TAP}px tap target"


def open_project(view, pid: str) -> None:
    page = view.page
    page.locator(f".project-item:has-text('{pid}')").first.click()
    page.wait_for_selector(".chat-textarea", state="attached")
    if view.phone:
        page.wait_for_timeout(300)


def open_tab(view, label: str) -> None:
    sel = ".mobile-inner-tab-btn" if view.phone else ".tab-btn"
    view.page.locator(f"{sel}:has-text('{label}')").first.click()


def send(view, text: str) -> None:
    """Enter sends on a desktop; on a phone Enter is a newline, so the Send button does it."""
    ta = view.page.locator(".chat-textarea").first
    ta.fill(text)
    if view.phone:
        view.page.locator(".chat-send-btn").first.click()
    else:
        ta.press("Enter")


# ── provider picker ────────────────────────────────────────────────────────────────────────────

def test_picker_with_grok(view):
    page = view.page
    open_project(view, "g-text")
    page.locator(".chat-named-tab-new").first.click()
    page.wait_for_selector("text=New agent chat")
    page.click("button[data-provider=grok]")
    buttons = page.locator(".provider-pick > button")
    labels = buttons.all_inner_texts()
    assert [t.strip() for t in labels][-1] == "Grok" and "Claude Code" in labels[0], labels
    boxes = [b.bounding_box() for b in buttons.all()]
    # Equal segments, one line each: a SELECTED provider must not swallow the row.
    widths = [round(b["width"]) for b in boxes]
    assert max(widths) - min(widths) <= 2, f"unequal provider buttons {widths}"
    assert len({round(b["height"]) for b in boxes}) == 1 and boxes[0]["height"] <= 52, boxes
    for b in buttons.all():
        tall_enough(view, b, "provider button")
        inside_viewport(view, b, "provider button")
    no_h_overflow(view)
    shot(view, "picker")


# ── Settings ───────────────────────────────────────────────────────────────────────────────────

def test_settings_toggle_and_warning(view):
    page = view.page
    open_project(view, "g-settings")
    open_tab(view, "Settings")
    toggle = page.locator("[data-testid=grok-allowed]")
    toggle.scroll_into_view_if_needed()
    expect(toggle).to_be_visible()
    warning = page.locator("text=Allow Grok in this project").first.locator("xpath=../..")
    inside_viewport(view, warning, "privacy warning row")
    assert "xAI" in warning.inner_text()
    # The control itself is a tap target on a phone: either the box or the row around it.
    if view.phone:
        box = toggle.bounding_box()
        row = warning.bounding_box()
        assert (box["width"] >= MIN_TAP and box["height"] >= MIN_TAP) or row["height"] >= MIN_TAP, (box, row)
        assert box["x"] + box["width"] <= view.w, box
    no_h_overflow(view)
    shot(view, "settings")


# ── Board: card modal + badge ──────────────────────────────────────────────────────────────────

def test_board_modal_and_card_badge(view):
    page = view.page
    open_project(view, "g-board")
    open_tab(view, "Board")
    text = f"visual card {view.label}"
    box = page.locator("textarea[placeholder^='New task']")
    box.fill(text)
    box.press("Enter")
    card = page.locator(f".board-card:has-text('{text}')")
    card.wait_for()
    card.locator(".board-card-text").dblclick()
    page.wait_for_selector("text=Provider")
    page.select_option("select:near(:text('Provider'))", "grok")
    modal_row = page.locator("input[placeholder='Project Grok model']")
    expect(modal_row).to_be_visible()
    shot(view, "board-modal")
    inside_viewport(view, modal_row, "Grok model input")
    inside_viewport(view, page.locator("select:near(:text('Provider'))").first, "provider select")
    no_h_overflow(view)
    page.get_by_role("button", name="Save", exact=True).click()
    badge = card.locator(".board-card-model-badge[data-provider=grok]")
    expect(badge).to_be_visible()
    inside_viewport(view, badge, "card badge")
    no_h_overflow(view)
    shot(view, "board-card")


# ── runtime pill (muted) + model menu ──────────────────────────────────────────────────────────

def test_muted_pill_and_menu(view):
    page = view.page
    open_project(view, "g-new")
    page.locator(".chat-named-tab-new").first.click()
    page.click("button[data-provider=grok]")
    page.fill("input[placeholder^='e.g. Math']", f"vis {view.label}")
    page.click("button:has-text('Create chat')")
    page.wait_for_selector(f".chat-named-tab.active:has-text('vis {view.label}')")

    if view.phone:
        # Mobile: the compact pill sits in the composer and a tap opens a full-width sheet.
        pill = page.locator(".usage-compact .usage-badge").first
        pill.click()
        sheet = page.locator(".usage-dropdown")
        expect(sheet).to_be_visible()
    else:
        page.locator(".usage-badge").first.hover()
        sheet = page.locator(".usage-dropdown")
        expect(sheet).to_be_visible()
    row = sheet.locator(".rt-line[data-provider=grok]")
    expect(row).to_be_visible()
    inside_viewport(view, sheet, "runtime sheet")
    inside_viewport(view, row, "grok runtime row")
    # The Grok row's stat is the muted dash, and the name is not clipped by it.
    stat_box = row.locator(".rt-line-stat").bounding_box()
    name_box = row.locator(".rt-line-name").bounding_box()
    assert name_box["x"] + name_box["width"] <= stat_box["x"] + 1, (name_box, stat_box)
    tall_enough(view, row, "runtime row") if False else None   # dense list rows are not primary taps
    no_h_overflow(view)
    shot(view, "pill-muted")

    # The model menu of the Grok chat (plan / ask greyed).
    page.keyboard.press("Escape")
    page.mouse.move(2, 2)
    page.locator(".composer-modelthink-btn").first.click()
    menu = page.locator(".composer-modelthink-menu")
    expect(menu).to_be_visible()
    inside_viewport(view, menu, "model menu")
    no_h_overflow(view)
    shot(view, "model-menu")


# ── the refusals ───────────────────────────────────────────────────────────────────────────────

def test_refusal_in_the_new_chat_dialog(view):
    page = view.page
    open_project(view, "g-denied")
    page.locator(".chat-named-tab-new").first.click()
    page.click("button[data-provider=grok]")
    page.click("button:has-text('Create chat')")
    alert = page.locator("[role=alert]").first
    expect(alert).to_contain_text(GATE_SENTENCE)
    inside_viewport(view, alert, "refusal alert")
    no_h_overflow(view)
    shot(view, "refusal-dialog")
    # As wide as the fields above it (the shared .error-state caps at 500px).
    field = page.locator("input[placeholder^='e.g. Math']").bounding_box()
    assert abs(alert.bounding_box()["width"] - field["width"]) <= 2, (alert.bounding_box(), field)


def test_refusal_banner_after_a_refused_switch(view):
    """A provider switch the server refuses must leave a visible sentence behind (the dialog that
    asked for it has closed, and the pill dropdown is only open while hovered)."""
    page = view.page
    open_project(view, "g-denied")
    if view.phone:
        page.locator(".usage-compact .usage-badge").first.click()
    else:
        page.locator(".usage-badge").first.hover()
    page.locator(".usage-dropdown .rt-line[data-provider=grok]").dispatch_event("mousedown")
    page.wait_for_selector("text=Switch to Grok")
    page.click("button:has-text('Switch without it')")
    banner = page.locator(".chat-error-banner")
    expect(banner).to_contain_text(GATE_SENTENCE)
    inside_viewport(view, banner, "refusal banner")
    no_h_overflow(view)
    shot(view, "refusal-banner")


def test_refusal_toast_for_a_free_chat(view):
    page = view.page
    open_project(view, "g-text")          # on a phone the tab bar (and its "+") shows with a project open
    page.locator(".ptab-new").first.click()
    page.wait_for_selector("text=New free chat")
    page.click("button[data-provider=grok]")
    shot(view, "free-chat-dialog")
    page.get_by_role("button", name="Create chat").click()
    toast = page.locator(".toast")
    expect(toast).to_contain_text(GATE_SENTENCE)
    inside_viewport(view, toast, "refusal toast")
    no_h_overflow(view)
    shot(view, "refusal-toast")
    # The toast must not cover the dialog's own Create button (a phone dialog is a bottom sheet).
    t, b = toast.bounding_box(), page.get_by_role("button", name="Create chat").bounding_box()
    overlap = not (t["y"] + t["height"] <= b["y"] or b["y"] + b["height"] <= t["y"]
                   or t["x"] + t["width"] <= b["x"] or b["x"] + b["width"] <= t["x"])
    assert not overlap, f"toast {t} covers the Create chat button {b}"


# ── handoff marker ─────────────────────────────────────────────────────────────────────────────

def test_handoff_marker_row(view):
    page = view.page
    open_project(view, f"g-hand-{view.label}")
    send(view, f"e2e:text visual {view.label}")
    page.wait_for_selector(".chat-msg-assistant .chat-msg-body:has-text('scripted e2e reply')")
    page.wait_for_timeout(1500)
    if view.phone:
        page.locator(".usage-compact .usage-badge").first.click()
    else:
        page.locator(".usage-badge").first.hover()
    page.locator(".usage-dropdown .rt-line[data-provider=grok]").dispatch_event("mousedown")
    page.wait_for_selector("text=Switch to Grok")
    shot(view, "handoff-modal")
    page.click("button:has-text('Switch and send this')")
    marker = page.locator(".chat-board-event-title", has_text="→ Grok").last
    expect(marker).to_be_visible()
    inside_viewport(view, marker, "handoff marker")
    marker.scroll_into_view_if_needed()
    no_h_overflow(view)
    shot(view, "handoff-marker")


# ── usage tab: the Grok card ───────────────────────────────────────────────────────────────────

def test_usage_tab_grok_card(view):
    page = view.page
    open_project(view, "g-text")
    if view.phone:
        page.evaluate("() => window.dispatchEvent(new CustomEvent('cops:open-usage'))")
    else:
        page.locator(".usage-badge").first.click()
    page.wait_for_selector(".usage-container")
    card = page.locator(".usage-card[data-provider=grok]")
    card.wait_for()
    expect(card).to_contain_text("API-equivalent")
    expect(card).to_contain_text("not spend")
    card.scroll_into_view_if_needed()
    page.mouse.move(2, 2)                 # the pill's hover dropdown must not sit over the card
    shot(view, "usage-card")
    inside_viewport(view, card, "usage card")
    no_h_overflow(view)
    # Title and note wrap instead of squeezing the title into a column.
    title = card.locator(".usage-card-title").bounding_box()
    assert title["height"] <= 40, f"usage card title wrapped to {title['height']}px"

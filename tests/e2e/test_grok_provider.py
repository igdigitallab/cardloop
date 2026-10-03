"""
E2E for Grok as the third provider (spec-095 P5b), against a REAL cockpit with GROK_ENABLED=true.

The cockpit runs the real `grok_engine` (probe, ACP client, process group, usage ledger) against a
fake `grok` CLI that replays recorded wire fixtures — see tests/e2e/grok_support.py. So a "Grok
turn" below is the whole production path with only the model missing.

What each test pins (every one fails when its feature is removed — mutation record in
/tmp/cardloop-scratch/p5b-grok-e2e.md):
  a  Grok is offered; any project runs a turn on it (no per-project flag); the G tag renders
  b  nothing configures a project for Grok: a plain project, a free chat and Settings need no opt-in
  c  "Ask me" / plan are greyed for Grok, with the reason
  d  the runtime pill row for Grok is the muted "limits not reported" state, never a bar
  e  board: the card editor offers Grok and the card badge shows it
  f  Settings: there is no per-project privacy toggle (choosing the provider is the consent)
  j  a failed Grok turn is a visible error, k  a Grok tool call renders, l  provider handoff marker

Not asserted here: what the chat shows AFTER a turn finishes (that is the history endpoint's job;
see test_grok_history.py). The live stream is observed with a MutationObserver instead.
Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import json
import re

import pytest
from playwright.sync_api import expect

from .conftest import open_project, send_chat
from .grok_ui import (api, chats_of, claude_transcripts, create_chat, open_model_menu,
                      open_new_chat_dialog, open_tab, usage_rows, wait_seen, watch_text)

pytestmark = pytest.mark.e2e

DIM = re.compile(r"usage-dim")
COLOURED = re.compile(r"usage-(green|yellow|red)")


# ── (a) picker + a scripted turn ───────────────────────────────────────────────────────────────

def test_grok_is_offered_and_a_project_runs_a_turn(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    open_project(page, "g-text")
    before = len(usage_rows(srv))

    open_new_chat_dialog(page)
    grok = page.locator("button[data-provider=grok]")
    expect(grok).to_be_visible()
    expect(grok).to_be_enabled()
    expect(grok).to_have_text("Grok")
    grok.click()
    # The model list is the registry's, not Claude's.
    options = page.locator("select.input option").all_inner_texts()
    assert "grok-4.7" in options and "Opus" not in options, options
    page.fill("input[placeholder^='e.g. Math']", "Grok lane")
    page.click("button:has-text('Create chat')")
    page.wait_for_selector(".chat-named-tab.active:has-text('Grok lane')")

    watch_text(page, "Hello world")
    send_chat(page, "say hello")
    # The recorded turn streams "Hel" "lo " "world" and ends with the assembled text.
    wait_seen(page, "Hello world")

    # The G tag sits on the chat tab and on the model pill.
    expect(page.locator(".chat-named-tab.active .rt-tag-tab")).to_have_text("G")
    expect(page.locator(".composer-modelthink-btn .rt-tag").first).to_have_text("G")

    # Server side: the chat is pinned to Grok and holds Grok's continuity id, not Claude's.
    mine = [c for c in chats_of(page, srv, "g-text") if c["name"] == "Grok lane"]
    assert len(mine) == 1 and mine[0]["provider"] == "grok", mine
    assert mine[0]["grok_session_id"] and not mine[0].get("session_id"), mine[0]
    # ... and the real engine wrote its usage ledger row, on the Grok model.
    rows = usage_rows(srv)
    assert len(rows) == before + 1, rows[before:]
    assert rows[-1]["project"] == "g-text" and rows[-1]["model"] == "grok-4.7"
    # Nothing ran on Claude.
    assert claude_transcripts(srv, "g-text") == []


def test_a_grok_chat_keeps_its_tag_across_a_reload(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-tool")
    create_chat(page, "grok", "Grok tools")
    page.reload()
    page.wait_for_selector(".chat-textarea", timeout=10_000)
    expect(page.locator(".chat-named-tab.active")).to_contain_text("Grok tools")
    expect(page.locator(".chat-named-tab.active .rt-tag-tab")).to_have_text("G")
    expect(page.locator(".composer-modelthink-btn .rt-tag").first).to_have_text("G")


def test_a_grok_tool_call_renders_in_the_feed(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-tool")
    create_chat(page, "grok", "tool lane")
    watch_text(page, "echo hi")
    watch_text(page, "Done: hi")
    send_chat(page, "run it")
    wait_seen(page, "Done: hi")
    # The recorded `run_terminal_command` reached the feed as the cockpit's own bash tool row
    # (map_tool turned it into Claude's shape), not as an unmapped/blank block.
    wait_seen(page, "echo hi")


def test_a_failed_grok_turn_is_a_visible_error(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-fail")
    create_chat(page, "grok", "fail lane")
    watch_text(page, "e2e scripted grok failure")
    send_chat(page, "this will fail")
    wait_seen(page, "e2e scripted grok failure")


# ── (b) no opt-in anywhere: choosing Grok in the picker is the consent ──────────────────────────────────────────────────────

def test_a_project_nobody_configured_for_grok_takes_a_chat_and_runs_a_turn(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    open_project(page, "g-plain")                    # no flag of any kind on this project record
    before = len(usage_rows(srv))
    create_chat(page, "grok", "plain lane")
    watch_text(page, "Hello world")
    send_chat(page, "say hello")
    wait_seen(page, "Hello world")
    mine = [c for c in chats_of(page, srv, "g-plain") if c["name"] == "plain lane"]
    assert len(mine) == 1 and mine[0]["provider"] == "grok" and mine[0]["grok_session_id"], mine
    assert len(usage_rows(srv)) == before + 1 and claude_transcripts(srv, "g-plain") == []


def test_the_api_accepts_grok_on_every_selection_path_without_any_flag(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    status, body = api(page, srv, "/api/projects/g-plain/chats", "POST", {"provider": "grok", "name": "api lane"})
    assert status in (200, 201), (status, body)
    status, body = api(page, srv, "/api/projects/g-plain/tasks", "POST", {"text": "a grok card", "provider": "grok"})
    assert status in (200, 201), (status, body)
    status, body = api(page, srv, "/api/projects/g-plain/settings", "POST", {"board_provider": "grok"})
    assert status == 200, (status, body)
    status, body = api(page, srv, "/api/projects/g-plain/settings", "POST", {"board_provider": "claude"})
    assert status == 200, (status, body)


def test_a_free_chat_can_be_started_on_grok(e2e_grok_server, grok_page):
    """A free chat is rooted at $HOME: it used to be refused. Choosing Grok in the dialog is the consent now."""
    page, srv = grok_page, e2e_grok_server
    page.click(".ptab-new")
    page.wait_for_selector("text=New free chat")
    page.click("button[data-provider=grok]")
    page.get_by_role("button", name="Create chat").click()
    expect(page.locator(".toast")).to_have_count(0)                                # nothing was refused
    expect(page.locator(".chat-named-tab.active .rt-tag-tab")).to_have_text("G")      # the new free chat is a Grok chat
    expect(page.locator(".composer-modelthink-btn .rt-tag").first).to_have_text("G")


def test_settings_offers_the_board_model_for_grok_but_no_privacy_toggle(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-settings")
    open_tab(page, "Settings")
    page.wait_for_selector("[data-testid=board-provider]")
    expect(page.locator("[data-testid=grok-allowed]")).to_have_count(0)
    expect(page.locator("text=Allow Grok in this project")).to_have_count(0)
    expect(page.locator("[data-testid=grok-board-model]")).to_be_visible()      # the model row stays
    opts = page.locator("[data-testid=board-provider] option").evaluate_all("els => els.map(e => e.value)")
    assert "grok" in opts, opts









# ── (c) capabilities ───────────────────────────────────────────────────────────────────────────

def test_ask_me_and_plan_are_greyed_for_grok_with_the_reason(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-new")
    create_chat(page, "grok", "caps")
    open_model_menu(page)
    menu = page.locator(".composer-modelthink-menu")
    expect(menu).to_contain_text("Grok model (this chat)")

    plan = menu.locator("[data-testid=plan-row]")
    ask = menu.locator("[data-testid=ask-row]")
    expect(plan).to_have_attribute("title", "Plan mode is not available on Grok — turn it off to send.")
    expect(ask).to_have_attribute("title", "Ask me is a Claude-only gate — this chat runs on Grok.")
    for row in (plan, ask):
        expect(row).to_have_css("pointer-events", "none")
        assert float(row.evaluate("e => getComputedStyle(e).opacity")) < 0.6
        expect(row).to_have_attribute("aria-selected", "false")
    # A forced event on the inert rows changes nothing.
    ask.dispatch_event("mousedown")
    plan.dispatch_event("mousedown")
    expect(ask).to_have_attribute("aria-selected", "false")
    expect(plan).to_have_attribute("aria-selected", "false")
    # Grok's own subagents are the multi-agent row (the Claude ultracode text must not leak into it).
    multi = menu.locator("[data-testid=multi-agent-row]")
    expect(multi).to_have_attribute("title", re.compile(r"Native Grok subagents"))
    expect(multi).not_to_have_attribute("title", re.compile(r"ultracode", re.I))


def test_claude_chats_keep_ask_and_plan_enabled_when_grok_is_enabled(e2e_grok_server, grok_page):
    """The greying is capability-driven: it must not leak onto the Claude chat next door."""
    page = grok_page
    open_project(page, "g-claude")
    open_model_menu(page)
    menu = page.locator(".composer-modelthink-menu")
    for tid in ("plan-row", "ask-row"):
        expect(menu.locator(f"[data-testid={tid}]")).not_to_have_css("pointer-events", "none")


# ── (d) the muted pill ─────────────────────────────────────────────────────────────────────────

def test_the_runtime_row_for_grok_is_muted_never_a_bar(e2e_grok_server, grok_page):
    page = grok_page
    open_project(page, "g-new")
    create_chat(page, "grok", "pill")

    badge = page.locator(".usage-badge").first
    expect(badge).to_contain_text("G")
    expect(badge).to_have_class(DIM)
    expect(badge).not_to_have_class(COLOURED)
    assert "limits not reported" in (badge.get_attribute("title") or "")

    badge.hover()
    row = page.locator(".usage-dropdown .rt-line[data-provider=grok]")
    expect(row).to_be_visible()
    stat = row.locator(".rt-line-stat")
    expect(stat).to_have_text("—")
    expect(stat).to_have_class(DIM)
    expect(stat).not_to_have_class(COLOURED)
    assert "limits not reported" in (row.get_attribute("title") or "")
    assert "%" not in row.inner_text()                              # no number to mistake for headroom
    assert row.locator(".usage-barfill, .usage-bartrack").count() == 0
    expect(row).to_have_attribute("aria-selected", "true")           # this chat IS on Grok
    expect(page.locator(".usage-dropdown")).to_contain_text("Grok · limits not reported")


# ── (e) board ──────────────────────────────────────────────────────────────────────────────────

def test_a_board_card_can_be_pinned_to_grok_and_shows_the_badge(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    open_project(page, "g-board")
    open_tab(page, "Board")
    box = page.locator("textarea[placeholder^='New task']")
    box.fill("port the e2e parser")
    box.press("Enter")
    card = page.locator(".board-card:has-text('port the e2e parser')")
    card.wait_for()
    card.locator(".board-card-text").dblclick()
    select = page.locator("select:near(:text('Provider'))").first
    values = select.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert "grok" in values, values
    select.select_option("grok")
    # The model slot turns into the provider-native free-text input.
    expect(page.locator("input[placeholder='Project Grok model']")).to_be_visible()
    page.get_by_role("button", name="Save", exact=True).click()
    badge = card.locator(".board-card-model-badge[data-provider=grok]")
    expect(badge).to_be_visible()
    expect(badge).to_have_text("Grok")
    # And it survives a reload (it is on the card, not in the browser).
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    if not page.locator(".board-card").count():
        open_project(page, "g-board")
        open_tab(page, "Board")
    expect(page.locator(".board-card:has-text('port the e2e parser') .board-card-model-badge[data-provider=grok]")).to_be_visible()


# ── (f) Settings: the privacy toggle ───────────────────────────────────────────────────────────



# ── (l) switching a chat to Grok: the handoff and its marker ───────────────────────────────────

def test_switching_a_claude_chat_to_grok_passes_a_handoff_and_leaves_a_marker(e2e_grok_server, grok_page):
    page, srv = grok_page, e2e_grok_server
    open_project(page, "g-handoff")
    create_chat(page, "claude", "handoff src")        # a fresh Claude chat: the project may hold Grok ones
    send_chat(page, "e2e:text first")
    page.wait_for_selector(".chat-msg-assistant .chat-msg-body:has-text('scripted e2e reply')", timeout=15_000)
    page.wait_for_timeout(1500)     # let the post-turn reconcile settle before switching

    page.locator(".usage-badge").first.hover()
    page.locator(".usage-dropdown .rt-line[data-provider=grok]").dispatch_event("mousedown")
    page.wait_for_selector("text=Switch to Grok")
    handoff = page.locator("textarea.input").first.input_value()
    assert "Handoff: Claude" in handoff and "→ Grok" in handoff and "e2e:text first" in handoff, handoff
    page.click("button:has-text('Switch and send this')")

    marker = page.locator(".chat-board-event-title", has_text="→ Grok")
    expect(marker).to_have_text("Claude · Main → Grok · new thread · handoff passed")
    # The chat is now Grok's.
    mine = [c for c in chats_of(page, srv, "g-handoff") if c["name"] == "handoff src"]
    assert mine and mine[0]["provider"] == "grok", mine
    expect(page.locator(".usage-badge").first).to_contain_text("G")






# ── a failed history read must not erase the canvas ────────────────────────────────────────────

def test_a_failed_history_read_keeps_the_streamed_reply_on_screen(e2e_grok_server, grok_page):
    """After a turn the chat re-reads history to reconcile. When that read fails (a provider whose
    reader is down, a network blip) the live-streamed answer must stay: it used to be wiped, so a
    finished reply vanished the moment the turn ended."""
    page = grok_page
    open_project(page, "g-claude")
    page.route("**/session-history*", lambda route: route.fulfill(status=500, body='{"error":"boom"}',
                                                                content_type="application/json"))
    send_chat(page, "e2e:text keep me")
    page.wait_for_selector(".chat-msg-assistant .chat-msg-body:has-text('scripted e2e reply')", timeout=15_000)
    page.wait_for_timeout(3500)   # the completion reconcile fires ~1.2 s after the turn and fails
    expect(page.locator(".chat-msg-assistant .chat-msg-body:has-text('scripted e2e reply')")).to_be_visible()
    expect(page.locator(".chat-msg-user:has-text('keep me'), .chat-msg:has-text('e2e:text keep me')").first).to_be_visible()
    page.unroute("**/session-history*")

"""
spec-072 Part 1 — E2E smoke suite.

Drives a REAL cockpit (subprocess, fake engine — see conftest.py/e2e_fake_engine.py)
through a real headless browser. Covers the four scenarios from the spec:
  1. plain streaming text (no duplicate/chopped bubbles)
  2. a tool call renders + final text
  3. mid-run reload re-attaches to the still-running turn
  4. busy path: two sends back-to-back — the second is queued, then drains

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import pytest
from playwright.sync_api import expect

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e


def test_text_streaming(logged_in_page):
    """e2e:text — 3 deltas + final text render as exactly one assistant bubble."""
    page = logged_in_page
    open_project(page, "e2e-text")
    send_chat(page, "e2e:text")

    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('a scripted e2e reply.')",
        timeout=15_000,
    )

    user_bubbles = page.locator(".chat-msg-user")
    assistant_bubbles = page.locator(".chat-msg-assistant")
    expect(user_bubbles).to_have_count(1)
    expect(assistant_bubbles).to_have_count(1)
    assert "Hello, this is a scripted e2e reply." in assistant_bubbles.inner_text()


def test_tool_render(logged_in_page):
    """e2e:tool — a Bash-shaped tool row renders, followed by the final text."""
    page = logged_in_page
    open_project(page, "e2e-tool")
    send_chat(page, "e2e:tool")

    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('e2e tool scenario done')",
        timeout=15_000,
    )
    # ToolBlock renders the raw Bash command in a <pre class="chat-tool-cmd">.
    expect(page.locator(".chat-tool-cmd", has_text="echo e2e")).to_have_count(1)


def test_mid_run_reload_reattaches(logged_in_page):
    """e2e:slow — reloading mid-turn re-attaches to the still-running turn on the
    server (no frozen canvas) and the final answer lands after reload."""
    page = logged_in_page
    open_project(page, "e2e-slow")
    send_chat(page, "e2e:slow")

    # First delta lands quickly — confirms the turn actually started before we reload.
    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('starting slow scenario')",
        timeout=10_000,
    )

    page.reload()

    # localStorage (cops.activeProject) restores the same project on reload; the
    # composer reappearing proves the SPA re-mounted into the same project/chat.
    page.wait_for_selector(".chat-textarea", timeout=10_000)

    # The fake engine is still asleep server-side for up to E2E_SLOW_GAP_SEC — the
    # cold-open hydrate (GET /live) + SSE reconnect must surface the eventual result
    # without the operator sending anything else.
    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('done after the long silence.')",
        timeout=20_000,
    )
    expect(page.locator(".chat-msg-assistant")).to_have_count(1)


def test_busy_path_queues_second_send(logged_in_page):
    """Two sends back-to-back: the second lands while the first still streams and
    must be queued (server is the single authority on busy — see sendMessage's
    comment in ChatTab.tsx), then drains and renders once the first turn ends."""
    page = logged_in_page
    open_project(page, "e2e-busy")
    send_chat(page, "e2e:text")

    # The user bubble for send #1 is appended synchronously in sendMessage(), before
    # the fetch() even starts — waiting for it guarantees client-side `streaming`
    # is already true, so the very next send is deterministically queued, not raced.
    page.wait_for_selector(".chat-msg-user", timeout=5_000)

    send_chat(page, "queued message")

    # Queued ack: the server-backed queue panel shows the pending second message
    # while the first turn is still in flight.
    page.wait_for_selector(".chat-queue-panel .chat-queue-text", timeout=5_000)
    assert "queued message" in page.locator(".chat-queue-panel").inner_text()

    # First turn finishes...
    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('a scripted e2e reply.')",
        timeout=15_000,
    )
    # ...then the queue drains and the second turn's reply (fake engine's default
    # ack branch, since "queued message" matches no e2e: marker) renders too.
    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('e2e fake ack: queued message')",
        timeout=20_000,
    )

    expect(page.locator(".chat-msg-user")).to_have_count(2)
    expect(page.locator(".chat-msg-assistant")).to_have_count(2)

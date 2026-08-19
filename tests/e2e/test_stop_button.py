"""
E2E specs for the ONE invariant the composer must never break:

    while a turn is running, the operator can always interrupt it.

Both scenarios below reproduce an operator report ("the session is clearly running,
but there is no Stop button — my message just goes into the queue and I cannot cut
the run off"):

  1. text in the composer used to REPLACE Stop with Queue — the only documented way
     back to Stop was "clear the input", which nobody discovers mid-run.
  2. after that Queue press, the {type:"queued"} ack tore `run` down client-side while
     the server kept running; the /live poll then refused to restore it (its restore
     was gated on busActiveRef, which the ack never cleared), so the composer stayed
     on "Send" — with no Stop — for the REST of the run.

Both drive a real headless browser against a real cockpit (fake engine, no tokens).
Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import pytest
from playwright.sync_api import expect

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e


def _start_adopted_run(page, project_id: str) -> None:
    """Starts an e2e:hold turn, then reloads mid-run.

    The reload is the point: after it, this client no longer owns the POST/SSE stream
    (`streaming` is false) and only knows the turn from hydrate + /live + the activity
    bus — the exact 'adopted run' state in which the operator's report happens (card
    runs, TG runs, queue drains, auto-continue wakes and any post-reload turn all land
    here). The turn stays alive server-side for E2E_HOLD_SEC.
    """
    open_project(page, project_id)
    send_chat(page, "e2e:hold")
    page.wait_for_selector(
        ".chat-msg-assistant .chat-msg-body:has-text('holding the turn open')",
        timeout=10_000,
    )
    page.reload()
    page.wait_for_selector(".chat-textarea", timeout=10_000)
    # The run bar renders iff the client believes a run is active (`run != null`).
    page.wait_for_selector(".chat-status-bar", timeout=10_000)


def test_stop_stays_reachable_while_typing(logged_in_page):
    """Typing into the composer must not take Stop away from a running turn."""
    page = logged_in_page
    _start_adopted_run(page, "e2e-hold")

    expect(page.locator(".chat-send-btn-stop")).to_be_visible()

    page.locator(".chat-textarea").fill("a follow-up I typed while it works")

    # The primary button flips to Queue (correct — the server IS busy), but Stop must
    # remain on screen: it is the only way to interrupt the run.
    expect(page.locator(".chat-send-btn:has-text('Queue')")).to_be_visible()
    expect(page.locator(".chat-send-btn-stop")).to_be_visible()


def test_stop_survives_queueing_a_message(logged_in_page):
    """Queueing a message mid-run must not leave the composer stuck on Send."""
    page = logged_in_page
    _start_adopted_run(page, "e2e-hold-queue")

    page.locator(".chat-textarea").fill("queue me while it runs")
    page.locator(".chat-send-btn:has-text('Queue')").click()

    # Server-side queue accepted it (the run is genuinely still in flight)...
    page.wait_for_selector(".chat-queue-panel .chat-queue-text", timeout=10_000)

    # ...and the run indicator + Stop must survive the {type:"queued"} ack. Poll the
    # steady state past the 5s /live tick: the regression was NOT a flicker, it was a
    # permanent flip to "Send" with no way back until the run ended on its own.
    page.wait_for_timeout(6_000)
    expect(page.locator(".chat-status-bar")).to_be_visible()
    expect(page.locator(".chat-send-btn-stop")).to_be_visible()
    expect(page.locator(".chat-send-btn:has-text('Send')")).to_have_count(0)

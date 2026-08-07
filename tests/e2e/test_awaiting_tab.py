"""
E2E: the tab of a project whose run finished in the background must announce
itself — a blinking green tab + reply-ready dot — and go quiet when opened.

The cue is easy to break silently: it depends on App's run_end SSE handler
classifying the run as "background" (project id != active tab), on the
replyReadyIds set reaching ProjectTabBar, and on clearUnread dropping the id
when the operator activates the tab. A unit test can cover none of that chain.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import re

import pytest
from playwright.sync_api import expect

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e

AWAITING = re.compile(r"\bptab-awaiting\b")


def test_background_run_end_marks_its_tab(logged_in_page):
    """Start a slow run, walk away to another project, come back to a lit-up tab."""
    page = logged_in_page

    open_project(page, "e2e-slow")
    send_chat(page, "e2e:slow")

    # Leave while the turn is still streaming — this is what makes its run_end
    # "background" for the App handler. (Not conftest.open_project: with a second
    # project open there are two .chat-textarea nodes and its wait resolves to the
    # hidden one.)
    page.click(".project-item:has-text('e2e-text')")
    expect(page.locator('.ptab', has_text="e2e-text")).to_have_class(
        re.compile(r"\bactive\b"), timeout=10_000)

    # Tabs carry the project's display name; data-tab-id is the cwd-derived id,
    # which the harness randomises per run — match on the label instead.
    slow_tab = page.locator('.ptab', has_text="e2e-slow")
    expect(slow_tab).to_have_class(AWAITING, timeout=25_000)
    # The dot rides in the same trailing slot (running → reply-ready → unread).
    expect(slow_tab.locator(".ptab-reply-ready")).to_have_count(1)

    # The tab the operator is actually looking at must stay quiet.
    expect(page.locator('.ptab', has_text="e2e-text")).not_to_have_class(AWAITING)

    # Opening the tab acknowledges it — the cue clears (and stays cleared).
    slow_tab.click()
    expect(slow_tab).not_to_have_class(AWAITING, timeout=10_000)
    expect(slow_tab.locator(".ptab-reply-ready")).to_have_count(0)

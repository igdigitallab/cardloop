"""
spec-080 — plan-approval card E2E (fake engine, zero SDK).

`e2e:plan` parks a REAL pending plan through the same webapp store the real engine's
gate uses, so this exercises the full stack: bus plan_ready → card render → decide POST
→ Future resolution → chat resumption, plus reload-durability mid-wait.

Locale-independent selectors: the card is the role=alertdialog containing 'Fake plan';
buttons are addressed positionally within it.
"""
import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e

CARD = "[role='alertdialog']:has-text('Fake plan')"


def _wait_card(page):
    page.wait_for_selector(CARD, timeout=15_000)
    return page.locator(CARD)


def test_plan_reject_with_feedback_then_approve(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-plan")

    # Round 1: reject with feedback
    send_chat(page, "e2e:plan")
    card = _wait_card(page)
    card.locator("button").nth(1).click()          # "Reject…" opens the feedback textarea
    card.locator("textarea").fill("needs-changes-xyz")
    card.locator("button").nth(0).click()          # "Send feedback & reject"
    page.wait_for_selector(
        ".chat-msg-assistant:has-text('PLAN_REJECTED_ACK')", timeout=15_000)
    assert "needs-changes-xyz" in page.locator(
        ".chat-msg-assistant:has-text('PLAN_REJECTED_ACK')").inner_text()
    # Card is gone after the decision
    assert page.locator(CARD).count() == 0

    # Round 2: approve
    send_chat(page, "e2e:plan")
    card = _wait_card(page)
    card.locator("button").nth(0).click()          # "Approve & execute"
    page.wait_for_selector(
        ".chat-msg-assistant:has-text('PLAN_EXEC_DONE')", timeout=15_000)
    assert page.locator(CARD).count() == 0


def test_plan_card_survives_reload(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-plan-reload")
    send_chat(page, "e2e:plan")
    _wait_card(page)

    # Mid-wait reload: the chats.json plan_id pointer + refreshPlanPrompt poll-on-mount
    # must re-pin the card without any bus event.
    page.reload()
    open_project(page, "e2e-plan-reload")
    card = _wait_card(page)

    card.locator("button").nth(0).click()          # approve
    page.wait_for_selector(
        ".chat-msg-assistant:has-text('PLAN_EXEC_DONE')", timeout=15_000)

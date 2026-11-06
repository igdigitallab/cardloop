"""
Sidebar version badge (web/src/components/VersionBadge.tsx).

The contract: a client sees ONLY the version until origin has something this checkout
lacks. No "up to date" pill, no SDK release row — even when the server reports a newer
claude-agent-sdk. Commits behind → an Update button; behind but unable to self-update →
"update blocked" with the reason in the title.

/api/version is fulfilled by page.route, so each state is exercised without touching
git or the network.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

_SDK_NEWER = {"name": "claude-agent-sdk", "installed": "0.2.176", "latest": "0.2.178",
              "update_available": True, "checked_at": 1.0}


def _payload(**over):
    base = {"current": "v0.18.0-57-g782ba94", "latest": None, "behind": 0,
            "update_available": False, "channel": "master", "can_self_update": True,
            "reason": None, "update_status": None, "sdk": _SDK_NEWER}
    base.update(over)
    return base


def _serve_version(page, payload):
    page.route("**/api/version*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))
    page.reload()
    page.wait_for_selector(".version-badge", timeout=10_000)
    return page.locator(".version-badge")


def test_up_to_date_shows_only_the_version(logged_in_page):
    badge = _serve_version(logged_in_page, _payload())
    expect(badge).to_contain_text("Cardloop v0.18.0-57-g782ba94")
    expect(badge.locator("button")).to_have_count(0)
    expect(badge).not_to_contain_text("up to date")
    # The SDK row is gone even though the payload announces a newer SDK.
    expect(logged_in_page.locator(".version-sdk")).to_have_count(0)
    expect(logged_in_page.locator("text=0.2.178")).to_have_count(0)


def test_commits_behind_show_the_update_button(logged_in_page):
    badge = _serve_version(logged_in_page, _payload(behind=3, update_available=True))
    btn = badge.locator(".version-update-btn")
    expect(btn).to_have_count(1)
    # No tag newer than HEAD → a bare "Update", never an old tag name.
    expect(btn).to_have_text("⬆ Update")
    expect(btn).to_have_attribute("title", "3 new commit(s) on master")


def test_behind_but_blocked_says_so(logged_in_page):
    reason = "working tree has local changes — commit or stash first"
    badge = _serve_version(logged_in_page, _payload(behind=2, can_self_update=False, reason=reason))
    expect(badge.locator(".version-update-btn")).to_have_count(0)
    blocked = badge.locator(".version-check-btn--disabled")
    expect(blocked).to_have_text("update blocked")
    expect(blocked).to_have_attribute("title", reason)


def test_not_behind_with_a_reason_stays_silent(logged_in_page):
    badge = _serve_version(logged_in_page, _payload(can_self_update=False, reason="no systemd"))
    expect(badge).to_contain_text("Cardloop v0.18.0-57-g782ba94")
    expect(badge.locator(".version-check-btn")).to_have_count(0)
    expect(badge).not_to_contain_text("self-update off")

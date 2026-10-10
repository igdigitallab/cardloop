"""
Policy rules panel (Agents tab, docs/RULES.md) against a real cockpit subprocess.

Covers what the unit tests cannot see: the rows render from the real /api/projects/{id}/rules,
a broken file shows its diagnostic, a rule file that the repository tracks is listed as "not
trusted" (and ignored) until the per-project opt-in is switched on in the panel, and the
layout holds at phone width. Rule HITS need a live Claude turn and are covered by
tests/test_policy_rules.py (the fake e2e engine runs no SDK hooks).

Run with:  venv/bin/python -m pytest tests/e2e -m e2e -k policy_rules
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import open_project

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("E2E_SHOTS_DIR")      # optional: keep screenshots of the panel for review


def _git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def _project_cwd(e2e_server, pid: str) -> Path:
    topics = json.loads((e2e_server["app_dir"] / "data" / "topics.json").read_text())
    return Path(next(b["cwd"] for b in topics.values() if b["project"] == pid))


def _open_agents_tab(page):
    if page.locator(".agents-tab").count() == 0:
        page.click("button.tab-btn:has-text('Agents')")
        page.wait_for_selector(".agents-tab", timeout=10_000)


def _rule(name, action, body, pattern="rm"):
    return f"---\nname: {name}\nevent: bash\naction: {action}\npattern: {pattern}\n---\n{body}\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_policy_rules_panel_lists_rules_diagnostics_and_the_trust_opt_in(logged_in_page, e2e_server):
    page = logged_in_page
    cwd = _project_cwd(e2e_server, "e2e-rules")
    rules = cwd / ".claude-ops" / "rules"
    rules.mkdir(parents=True)
    (rules / "tracked-rule.md").write_text(_rule("tracked-rule", "block", "Do not rm."), encoding="utf-8")
    _git(cwd, "init", "-q")
    _git(cwd, "add", ".claude-ops/rules/tracked-rule.md")
    _git(cwd, "commit", "-qm", "add rule")
    (rules / "local-warn.md").write_text(_rule("local-warn", "warn", "Careful with rm."), encoding="utf-8")
    (rules / "broken.md").write_text("no frontmatter here", encoding="utf-8")
    glob = e2e_server["home"] / ".claude-ops" / "rules"
    glob.mkdir(parents=True)
    (glob / "global-block.md").write_text(_rule("global-block", "block", "No curl.", "curl"), encoding="utf-8")

    open_project(page, "e2e-rules")
    _open_agents_tab(page)
    panel = page.locator(".rules-panel")
    expect(panel).to_be_visible(timeout=10_000)
    row = lambda name: panel.locator(".rules-row").filter(has=page.locator(".agents-role-name", has_text=name))  # noqa: E731

    expect(row("local-warn")).to_contain_text("warn")
    expect(row("local-warn")).to_contain_text("0 hits")
    expect(row("global-block")).to_contain_text("block")
    expect(row("global-block")).to_contain_text("global")
    expect(row("broken")).to_contain_text("invalid")
    expect(row("broken")).to_contain_text("frontmatter")
    expect(row("tracked-rule")).to_contain_text("not trusted")
    expect(row("tracked-rule")).to_contain_text("rules_trust_tracked")
    if SHOTS:
        panel.screenshot(path=os.path.join(SHOTS, "rules-desktop.png"))

    trust = panel.locator(".rules-trust input[type=checkbox]")
    expect(trust).not_to_be_checked()
    trust.click()
    expect(trust).to_be_checked(timeout=10_000)
    expect(row("tracked-rule")).not_to_contain_text("not trusted", timeout=10_000)
    expect(row("tracked-rule")).to_contain_text("block")

    # the opt-in is a real project setting, and survives a reload of the panel
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, "e2e-rules")
    _open_agents_tab(page)
    expect(page.locator(".rules-trust input[type=checkbox]")).to_be_checked(timeout=10_000)

    page.locator(".rules-trust input[type=checkbox]").click()
    expect(row("tracked-rule")).to_contain_text("not trusted", timeout=10_000)


def test_policy_rules_panel_fits_a_phone_screen(e2e_server, browser):
    cwd = _project_cwd(e2e_server, "e2e-rules")
    rules = cwd / ".claude-ops" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    long_name = "a-very-long-rule-name-for-a-phone-screen-check"
    (rules / f"{long_name}.md").write_text(
        _rule(long_name, "block", "No.", "sudo\\s+rm\\s+-rf\\s+/srv/client-files/some/deep/path"), encoding="utf-8")
    ctx = browser.new_context(viewport={"width": 360, "height": 800}, has_touch=True)
    page = ctx.new_page()
    page.set_default_timeout(10_000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        (e2e_server["app_dir"] / "data" / "ui_state.json").unlink(missing_ok=True)
        page.goto(e2e_server["base_url"])
        page.fill("#password", e2e_server["password"])
        page.click("button.btn-primary[type=submit]")
        page.wait_for_selector(".project-item", state="attached", timeout=10_000)
        page.locator(".project-item:has-text('e2e-rules')").first.click()
        page.wait_for_selector(".chat-textarea", state="attached")
        page.wait_for_timeout(300)
        page.locator(".mobile-inner-tab-btn:has-text('Agents')").first.click()
        panel = page.locator(".rules-panel")
        expect(panel).to_be_visible(timeout=10_000)
        expect(panel.locator(".agents-role-name", has_text=long_name)).to_be_visible()
        box = panel.bounding_box()
        assert box and box["x"] >= -1 and box["x"] + box["width"] <= 361, box
        overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"page scrolls sideways by {overflow}px"
        if SHOTS:
            page.wait_for_timeout(450)
            panel.screenshot(path=os.path.join(SHOTS, "rules-phone.png"))
    finally:
        ctx.close()
    assert not errors, errors

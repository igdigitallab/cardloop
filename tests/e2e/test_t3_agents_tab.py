"""
T3 final-gate live coverage for spec-091's Agents tab — the round-2 specifics that had
NO e2e coverage before this pass (FX4 explicitly flagged this gap). Drives a real
headless browser against a real cockpit subprocess (fake engine, no tokens).

FX5 (tab-merge) added coverage for two decisions on top of this:
  - Decision 1: the standalone "CLAUDE.md" tab is gone — Agents is now the FIRST tab and
    embeds CLAUDE.md itself (the same EditableMarkdown/api.claudeMd the old tab used) at
    the top, above the sub-agent role list.
  - Decision 2: EVERY role opens a real, always-editable editor now (no more read-only
    "view" mode for builtin roles). Saving a builtin role's edit forks it into the project
    scope; if a project override already exists for that name, the SAME 409→confirm→
    overwrite=true path "+ New role" uses fires, naming the file.

Pre-FX5 coverage, still valid and unchanged in intent:
  - I1k frontend half: a "+ New role" save that collides with an existing project file
    now gets a 409 from the real backend, which opens a ConfirmModal naming the file;
    confirming retries with overwrite=true and the file is genuinely replaced on disk.
  - N5 item 2: toggling a GLOBAL-scope role's enabled switch requires a cockpit-wide
    confirmation before the write fires; cancel must NOT write.
  - N5 item 1: a builtin row shadowed by a project override is no longer hard-disabled
    (the old regression) — its checkbox is clickable. (Its OWN "Copy to project" button no
    longer exists post-FX5 — see the updated N5 item-1 test below for why: Edit+Save now
    covers exactly that case, so keeping a second, redundant control on the builtin row
    would just be two paths to the same write.)

conftest.py's _build_app_copy was extended (T3, test-only) to also symlink `roles/`
into the e2e app copy — roles.BUILTIN_DIR resolves relative to roles.py's own copied
location, so without that symlink every scenario here would see zero builtin roles.

Pre-existing, NOT spec-091-caused quirk worked around here (see T3-final-gate.md
"remaining defects"): closing ANY modal while a non-default tab is active bounces the
active tab back to "Board" (useBackDismiss popstate double-catch, reproduced by T2 on
MemoryTab too) — so every helper below re-asserts the Agents tab is active after a
modal closes instead of assuming it stayed put.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e -k t3_agents
"""
import pytest
from playwright.sync_api import expect

from .conftest import open_project

pytestmark = pytest.mark.e2e


def _open_agents_tab(page):
    if page.locator(".agents-tab").count() == 0:
        page.click("button.tab-btn:has-text('Agents')")
        page.wait_for_selector(".agents-tab", timeout=10_000)


def _create_role(page, name: str, body: str, scope: str = "project"):
    _open_agents_tab(page)
    page.click("button:has-text('+ New role')")
    page.wait_for_selector(".run-modal-body", timeout=5_000)
    page.fill("input[placeholder='reviewer-perf']", name)
    if scope == "global":
        page.locator("input[type=radio][name='agents-new-role-scope']").nth(1).check()
    page.fill(".run-modal-body textarea", body)
    page.locator(".run-modal-body button:has-text('Save')").click()


def _role_row(page, name: str, scope_class: str):
    """Disambiguates a role row by name AND scope — a shadowed name legitimately appears
    twice (once per scope), so `has_text=name` alone is not enough (as N5's own test found
    when a description happened to contain the word "builtin")."""
    return page.locator(".agents-role-row").filter(
        has=page.locator(".agents-role-name", has_text=name)
    ).filter(has=page.locator(f".agents-role-scope-badge--{scope_class}"))


def test_t3_agents_i1k_new_role_collision_is_409_then_confirmed_overwrite(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-text")

    role_name = "t3probe"
    _create_role(page, role_name,
                 f"---\nname: {role_name}\ndescription: Use this when t3 probes overwrite "
                 "handling.\n---\nOriginal body v1.\n")
    page.wait_for_selector(".run-modal-body", state="detached", timeout=10_000)
    _open_agents_tab(page)  # modal-close snaps to Board (pre-existing quirk) — come back
    expect(page.locator(".agents-role-name", has_text=role_name)).to_have_count(1)

    # Same name/scope again, no overwrite flag sent by a fresh "+ New role" flow — the
    # backend's existence guard must 409, and the UI must turn that into a named confirm,
    # not a silent clobber and not a crash.
    _create_role(page, role_name,
                 f"---\nname: {role_name}\ndescription: Use this when t3 probes overwrite "
                 "handling (v2).\n---\nReplacement body v2.\n")

    confirm = page.locator(".run-modal", has_text="Replace existing role file?")
    expect(confirm).to_have_count(1, timeout=5_000)
    expect(confirm).to_contain_text(f"{role_name}.md")

    confirm.locator("button:has-text('Yes, replace')").click()
    expect(confirm).to_have_count(0, timeout=5_000)
    # confirmOverwrite() also closes the underlying editor for kind='new' — nothing left open.
    expect(page.locator(".run-modal-body")).to_have_count(0)

    _open_agents_tab(page)
    page.locator(f".agents-role-row:has-text('{role_name}')").locator(
        "button[title='Edit role']").click()
    page.wait_for_selector(".run-modal-body textarea", timeout=5_000)
    content = page.locator(".run-modal-body textarea").input_value()
    assert "Replacement body v2." in content
    assert "Original body v1." not in content
    page.locator(".run-modal-body button:has-text('Cancel')").click()


def test_t3_agents_n5_global_toggle_requires_confirmation(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-tool")

    role_name = "t3globalprobe"
    _create_role(page, role_name,
                 f"---\nname: {role_name}\ndescription: Use this when t3 probes the global "
                 "toggle confirm.\n---\nGlobal body.\n", scope="global")
    page.wait_for_selector(".run-modal-body", state="detached", timeout=10_000)
    _open_agents_tab(page)

    row = page.locator(f".agents-role-row:has-text('{role_name}')")
    expect(row).to_have_count(1)
    checkbox = row.locator("input[type=checkbox]")
    expect(checkbox).to_be_checked()  # new role template defaults enabled: true

    checkbox.click()
    confirm = page.locator(".run-modal", has_text="Change this for every project?")
    expect(confirm).to_have_count(1, timeout=5_000)

    # Cancel must NOT write — the checkbox (a controlled input) stays checked, and no
    # network call to setRoleEnabled happens at all (doToggle is unreachable from Cancel).
    confirm.locator("button:has-text('Cancel')").click()
    expect(confirm).to_have_count(0)
    _open_agents_tab(page)
    row = page.locator(f".agents-role-row:has-text('{role_name}')")
    expect(row.locator("input[type=checkbox]")).to_be_checked()

    # Confirming DOES flip it for real.
    row.locator("input[type=checkbox]").click()
    confirm2 = page.locator(".run-modal", has_text="Change this for every project?")
    expect(confirm2).to_have_count(1, timeout=5_000)
    confirm2.locator("button:has-text('Confirm')").click()
    expect(confirm2).to_have_count(0, timeout=5_000)
    _open_agents_tab(page)
    row = page.locator(f".agents-role-row:has-text('{role_name}')")
    expect(row.locator("input[type=checkbox]")).not_to_be_checked(timeout=5_000)


def test_t3_agents_n5_shadowed_builtin_row_controls_are_not_disabled(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-slow")

    # Shadow the builtin "quick" role with a project override.
    _create_role(page, "quick",
                 "---\nname: quick\ndescription: Use this when t3 needs a project override "
                 "for the shared default lookup role.\n---\nShadowing project body.\n")
    page.wait_for_selector(".run-modal-body", state="detached", timeout=10_000)
    _open_agents_tab(page)

    # Scope badge CLASS, not loose text — a description could legitimately mention the
    # word "builtin" and defeat a has_text filter (as it did during authoring this test).
    builtin_row = _role_row(page, "quick", "builtin")

    expect(builtin_row).to_have_count(1)
    expect(builtin_row).to_contain_text("shadowed by")

    checkbox = builtin_row.locator("input[type=checkbox]")
    # N5 item 1 (FX4): the checkbox must NOT be hard-disabled by shadow state any more —
    # only a genuinely in-flight request (`busy`) disables it.
    assert checkbox.is_disabled() is False
    # FX5: "Copy to project" is gone from a builtin row (Edit → Save now covers the exact
    # same fork-into-project outcome, so a second control for it would be redundant) — its
    # own Edit button is the one that must be live instead.
    expect(builtin_row.locator("button[title*='Copy this role']")).to_have_count(0)
    edit_btn = builtin_row.locator("button[title='Edit role']")
    expect(edit_btn).to_have_count(1)
    assert edit_btn.is_disabled() is False


def test_t3_agents_fx5_tab_merge_claude_md_is_first_tab_and_embedded(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-hold")

    # Decision 1: no separate "CLAUDE.md" tab exists any more, anywhere in the tab bar.
    expect(page.locator("nav.tabs button.tab-btn", has_text="CLAUDE.md")).to_have_count(0)
    # ...and "Agents" is the FIRST tab (took the slot claude-md used to occupy).
    tab_buttons = page.locator("nav.tabs button.tab-btn")
    expect(tab_buttons.first).to_have_text("Agents")

    tab_buttons.first.click()
    page.wait_for_selector(".agents-tab", timeout=10_000)

    # The main-agent block embeds CLAUDE.md itself — this fresh e2e project has none yet,
    # so EditableMarkdown's own empty state renders (proves the SAME api.claudeMd/
    # saveClaudeMd wiring the old standalone tab used, not a stub).
    claude_block = page.locator(".agents-claude-md-block")
    expect(claude_block).to_have_count(1)
    expect(claude_block).to_contain_text("No CLAUDE.md for this project")

    claude_block.locator(".doc-create-btn").click()
    page.wait_for_selector(".agents-claude-md-block textarea.doc-textarea", timeout=5_000)
    page.fill(".agents-claude-md-block textarea.doc-textarea",
              "# T3 CLAUDE.md\n\nWritten from the merged Agents tab.\n")
    page.locator(".agents-claude-md-block button:has-text('Save')").click()
    expect(page.locator(".agents-claude-md-block .markdown-wrap")).to_contain_text(
        "Written from the merged Agents tab", timeout=5_000)


def test_t3_agents_fx5_edit_unshadowed_builtin_forks_to_project_without_confirm(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-busy")
    _open_agents_tab(page)

    row = _role_row(page, "docs-writer", "builtin")
    expect(row).to_have_count(1)
    expect(row).not_to_contain_text("shadowed by")  # no project override yet

    row.locator("button[title='Edit role']").click()
    page.wait_for_selector(".run-modal-body textarea", timeout=5_000)

    textarea = page.locator(".run-modal-body textarea")
    # Decision 2: builtin roles now open a REAL editor — no `readonly` attribute at all.
    assert textarea.get_attribute("readonly") is None
    # Decision 2: "before saving, where the save will land" — a visible scope badge plus
    # an explanatory note, since a builtin edit is silently redirected to project scope.
    expect(page.locator(".run-modal-body .agents-editor-target-row")).to_contain_text("project")
    expect(page.locator(".run-modal-body")).to_contain_text("builtin file itself is never changed")

    textarea.fill(textarea.input_value() + "\nT3 fork marker.\n")
    page.locator(".run-modal-body button:has-text('Save')").click()
    # No existing project file for this name yet → no 409, the save goes straight through.
    expect(page.locator(".run-modal-body")).to_have_count(0, timeout=5_000)

    _open_agents_tab(page)
    project_row = _role_row(page, "docs-writer", "project")
    expect(project_row).to_have_count(1)
    expect(_role_row(page, "docs-writer", "builtin")).to_contain_text("shadowed by")

    project_row.locator("button[title='Edit role']").click()
    page.wait_for_selector(".run-modal-body textarea", timeout=5_000)
    assert "T3 fork marker." in page.locator(".run-modal-body textarea").input_value()
    page.locator(".run-modal-body button:has-text('Cancel')").click()


def test_t3_agents_fx5_edit_shadowed_builtin_forks_via_409_confirm(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-multiblock")

    # Pre-seed a project-scope override for "architect" via the (already-covered) "+ New
    # role" path, independent of the edit-to-fork flow under test, so the builtin row is
    # shadowed before we ever open its editor.
    _create_role(page, "architect",
                 "---\nname: architect\ndescription: Use this when t3 pre-seeds a project "
                 "override to collide with an edit-to-fork.\n---\nPre-seeded project body.\n")
    page.wait_for_selector(".run-modal-body", state="detached", timeout=10_000)
    _open_agents_tab(page)

    builtin_row = _role_row(page, "architect", "builtin")
    expect(builtin_row).to_contain_text("shadowed by")
    builtin_row.locator("button[title='Edit role']").click()
    page.wait_for_selector(".run-modal-body textarea", timeout=5_000)

    textarea = page.locator(".run-modal-body textarea")
    textarea.fill(textarea.input_value() + "\nT3 fork-collision marker.\n")
    page.locator(".run-modal-body button:has-text('Save')").click()

    # A project file for "architect" already exists (the pre-seed above) — editing the
    # BUILTIN row must go through the exact same 409→confirm path "+ New role" uses, not
    # silently clobber it and not crash.
    confirm = page.locator(".run-modal", has_text="Replace existing role file?")
    expect(confirm).to_have_count(1, timeout=5_000)
    expect(confirm).to_contain_text("architect.md")

    confirm.locator("button:has-text('Yes, replace')").click()
    expect(confirm).to_have_count(0, timeout=5_000)
    expect(page.locator(".run-modal-body")).to_have_count(0)

    _open_agents_tab(page)
    project_row = _role_row(page, "architect", "project")
    project_row.locator("button[title='Edit role']").click()
    page.wait_for_selector(".run-modal-body textarea", timeout=5_000)
    content = page.locator(".run-modal-body textarea").input_value()
    assert "T3 fork-collision marker." in content
    assert "Pre-seeded project body." not in content
    page.locator(".run-modal-body button:has-text('Cancel')").click()

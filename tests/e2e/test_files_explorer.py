"""
Files tab explorer (web/src/components/FileExplorer.tsx + fs_browser.py), driven in a real browser.

What the operator asked for, one test each: a draggable / hideable divider, several files open
as tabs, editing markdown, leaving the project folder, and jumping to a pasted path — plus the
two ways a save can go wrong (the agent rewrote the file meanwhile).

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(e2e_server):
    """(home, project, other-project, scratch): fresh files for every test."""
    home = e2e_server["home"]
    proj = home / "work" / "e2e-files"
    other = home / "work" / "other"
    scratch = e2e_server["scratch"]
    (proj / "docs").mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    (proj / "README.md").write_text("# Readme\n\nhello\n")
    (proj / "notes.md").write_text("# Notes\n\nfirst\n")
    (proj / "docs" / "guide.md").write_text("# Guide\n")
    (proj / "app.py").write_text("print('hi')\n")
    (other / "plan.md").write_text("# Other plan\n")
    (scratch / "report.md").write_text("# Scratch report\n")
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAA\n")
    (home / ".bashrc").write_text("export X=1\n")
    return {"home": home, "proj": proj, "other": other, "scratch": scratch}


def _open_files(page):
    page.click(".project-item:has-text('e2e-files')")
    page.wait_for_selector(".chat-textarea:visible", timeout=10_000)
    page.locator(".tab-btn", has_text="Files").click()
    page.wait_for_selector(".files-explorer .file-tree-row", timeout=10_000)


def _row(page, name):
    return page.locator(f".file-tree-row[data-path$='/{name}']").first


def _tree_width(page):
    return page.locator(".files-tree-pane").bounding_box()["width"]


def _crumb(page):
    return page.locator(".files-crumb.current")


def _paste(page, selector, text):
    """A real clipboard is not available headless: dispatch the event the browser would."""
    page.evaluate(
        """([sel, text]) => {
          const el = document.querySelector(sel)
          const dt = new DataTransfer()
          dt.setData('text', text)
          el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }))
        }""",
        [selector, text],
    )


def test_several_files_open_as_tabs_and_markdown_is_editable(logged_in_page, world):
    page = logged_in_page
    _open_files(page)

    _row(page, "README.md").click()
    expect(page.locator(".markdown-wrap h1")).to_have_text("Readme")
    _row(page, "notes.md").click()
    expect(page.locator(".files-tab")).to_have_count(2)
    expect(page.locator(".markdown-wrap h1")).to_have_text("Notes")

    page.locator("button:has-text('Edit')").click()
    ta = page.locator(".file-edit-textarea")
    ta.fill("# Notes\n\nedited by hand\n")
    expect(page.locator(".files-tab.active .files-tab-dirty")).to_be_visible()

    # The draft survives a trip to another tab.
    page.locator(".files-tab", has_text="README.md").click()
    expect(page.locator(".markdown-wrap h1")).to_have_text("Readme")
    page.locator(".files-tab", has_text="notes.md").click()
    expect(page.locator(".file-edit-textarea")).to_have_value("# Notes\n\nedited by hand\n")

    page.locator(".file-edit-textarea").press("Control+s")
    expect(page.locator(".files-tab-dirty")).to_have_count(0)
    assert (world["proj"] / "notes.md").read_text() == "# Notes\n\nedited by hand\n"
    expect(page.locator(".markdown-wrap")).to_contain_text("edited by hand")


def test_closing_a_dirty_tab_asks_first(logged_in_page, world):
    page = logged_in_page
    _open_files(page)
    _row(page, "notes.md").click()
    page.locator("button:has-text('Edit')").click()
    page.locator(".file-edit-textarea").fill("changed")
    page.locator(".files-tab.active .files-tab-close").click()
    expect(page.get_by_text("Discard unsaved changes?")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(page.locator(".files-tab")).to_have_count(1)
    page.locator(".files-tab.active .files-tab-close").click()
    page.get_by_role("dialog").get_by_role("button", name="Discard").click()
    expect(page.locator(".files-tab")).to_have_count(0)
    assert (world["proj"] / "notes.md").read_text() == "# Notes\n\nfirst\n"


def test_divider_drags_resets_and_the_explorer_hides(logged_in_page, world):
    page = logged_in_page
    _open_files(page)
    w0 = _tree_width(page)
    layout = page.locator(".files-layout").bounding_box()["width"]
    handle = page.locator(".files-split-handle")
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def drag(dx):
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + dx, y, steps=6)
        page.mouse.up()

    drag(40)
    assert abs(_tree_width(page) - (w0 + 40)) < 4

    # Remembered across a reload.
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    _open_files(page)
    assert abs(_tree_width(page) - (w0 + 40)) < 4

    page.locator(".files-split-handle").dblclick()
    assert abs(_tree_width(page) - 220) < 4

    # Keyboard: arrows resize.
    page.locator(".files-split-handle").focus()
    page.keyboard.press("ArrowRight")
    assert abs(_tree_width(page) - 236) < 4

    # A huge drag stops where the viewer would fall under its minimum, not past it.
    box = page.locator(".files-split-handle").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    drag(2000)
    viewer = page.locator(".files-viewer-pane").bounding_box()["width"]
    assert viewer >= 230, viewer
    assert _tree_width(page) < layout

    # Hide, and bring back with Ctrl+B.
    page.locator("button[title^='Hide explorer']").click()
    expect(page.locator(".files-tree-pane")).to_have_count(0)
    expect(page.locator(".files-split-handle")).to_have_count(0)
    page.locator("button[title^='Show explorer']").focus()
    page.keyboard.press("Control+b")
    expect(page.locator(".files-tree-pane")).to_have_count(1)


def test_leave_the_project_folder_and_jump_to_pasted_paths(logged_in_page, world):
    page = logged_in_page
    _open_files(page)
    expect(_crumb(page)).to_have_text("e2e-files")

    # Up one folder: the project's siblings appear.
    page.locator("button[title='Up one folder']").click()
    expect(_crumb(page)).to_have_text("work")
    expect(_row(page, "other")).to_be_visible()
    expect(_row(page, "e2e-files")).to_be_visible()

    # $HOME never lists dot entries (.ssh, .bashrc); it is also the ceiling.
    page.locator(".files-crumb", has_text="~").click()
    expect(_row(page, "work")).to_be_visible()
    assert page.locator(".file-tree-row .file-tree-name", has_text=".ssh").count() == 0
    assert page.locator(".file-tree-row .file-tree-name", has_text=".bashrc").count() == 0
    expect(page.locator("button[title='Up one folder']")).to_be_disabled()

    # Type a path the way an agent prints it: `backticks` and :line.
    page.locator(".files-crumb-edit").click()
    page.locator(".files-path-input").fill(f"`{world['other']}/plan.md:3`")
    page.locator(".files-path-input").press("Enter")
    expect(page.locator(".files-tab.active")).to_contain_text("plan.md")
    expect(page.locator(".markdown-wrap h1")).to_have_text("Other plan")
    # The file is below the current root (~): the root stays, the tree opens down to it.
    expect(_crumb(page)).to_have_text("~")
    expect(_row(page, "plan.md")).to_be_visible()

    # Paste over the selected field: goes at once, no Enter.
    page.locator(".files-crumb-edit").click()
    _paste(page, ".files-path-input", str(world["proj"] / "docs" / "guide.md"))
    expect(page.locator(".files-tab.active")).to_contain_text("guide.md")
    expect(_row(page, "guide.md")).to_be_visible()

    # A bare Ctrl+V in the explorer jumps too — into another root (the scratch folder).
    _paste(page, ".file-tree-row", str(world["scratch"] / "report.md"))
    expect(page.locator(".files-tab.active")).to_contain_text("report.md")
    expect(page.locator(".files-tabs .files-tab")).to_have_count(3)

    # Refusals say why, and never open anything.
    page.locator(".files-crumb-edit").click()
    page.locator(".files-path-input").fill(str(world["home"] / ".ssh" / "authorized_keys"))
    page.locator(".files-path-input").press("Enter")
    expect(page.locator(".files-note")).to_contain_text("Outside the folders")
    expect(page.locator(".files-tabs .files-tab")).to_have_count(3)

    page.locator(".files-crumb-edit").click()
    page.locator(".files-path-input").fill(str(world["other"] / "nope.md"))
    page.locator(".files-path-input").press("Enter")
    expect(page.locator(".files-note")).to_contain_text("Not found")
    expect(_crumb(page)).to_have_text("other")


def test_a_save_after_the_agent_rewrote_the_file_is_blocked_not_lost(logged_in_page, world):
    page = logged_in_page
    _open_files(page)
    notes = world["proj"] / "notes.md"
    _row(page, "notes.md").click()
    page.locator("button:has-text('Edit')").click()
    page.locator(".file-edit-textarea").fill("# Notes\n\nmine\n")

    notes.write_text("# Notes\n\nthe agent wrote a much longer version in the meantime\n")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".files-banner")).to_contain_text("changed on disk")
    assert "the agent wrote" in notes.read_text()  # nothing was clobbered
    expect(page.locator(".file-edit-textarea")).to_have_value("# Notes\n\nmine\n")  # nor was the draft

    page.get_by_role("button", name="Overwrite").click()
    expect(page.locator(".files-banner")).to_have_count(0)
    assert notes.read_text() == "# Notes\n\nmine\n"

    # And the other way out: drop mine, take the disk.
    page.locator("button:has-text('Edit')").click()
    page.locator(".file-edit-textarea").fill("# Notes\n\nthird\n")
    notes.write_text("# Notes\n\nanother agent rewrite, longer than the draft\n")
    page.get_by_role("button", name="Save", exact=True).click()
    page.get_by_role("button", name="Reload (drop mine)").click()
    expect(page.locator(".file-edit-textarea")).to_have_count(0)
    expect(page.locator(".markdown-wrap")).to_contain_text("another agent rewrite")


def test_open_files_and_folder_come_back_after_a_reload(logged_in_page, world):
    page = logged_in_page
    _open_files(page)
    _row(page, "README.md").click()
    _row(page, "notes.md").click()
    page.locator("button[title='Up one folder']").click()
    expect(_crumb(page)).to_have_text("work")

    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    _open_files(page)
    expect(page.locator(".files-tab")).to_have_count(2)
    expect(page.locator(".files-tab.active")).to_contain_text("notes.md")
    expect(_crumb(page)).to_have_text("work")
    expect(page.locator(".markdown-wrap h1")).to_have_text("Notes")


def test_unsaved_edits_survive_leaving_the_files_tab_and_a_reload(logged_in_page, world):
    """Switching to another project tab unmounts the explorer; the draft must come back."""
    page = logged_in_page
    _open_files(page)
    notes = world["proj"] / "notes.md"
    _row(page, "notes.md").click()
    page.locator("button:has-text('Edit')").click()
    page.locator(".file-edit-textarea").fill("# Notes\n\nnot saved yet\n")

    page.locator(".tab-btn", has_text="Board").click()
    page.wait_for_selector(".files-explorer", state="detached")
    page.locator(".tab-btn", has_text="Files").click()
    page.wait_for_selector(".files-explorer .files-tab")
    expect(page.locator(".file-edit-textarea")).to_have_value("# Notes\n\nnot saved yet\n")
    expect(page.locator(".files-tab.active .files-tab-dirty")).to_be_visible()
    assert notes.read_text() == "# Notes\n\nfirst\n"  # nothing was written behind the operator's back

    # ...and across a reload (drafts are flushed to storage, not held only in memory).
    page.wait_for_timeout(600)
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    _open_files(page)
    expect(page.locator(".file-edit-textarea")).to_have_value("# Notes\n\nnot saved yet\n")

    # Saved for real: the stored draft is gone, so it does not resurrect later.
    page.locator(".file-edit-textarea").press("Control+s")
    expect(page.locator(".files-tab-dirty")).to_have_count(0)
    page.locator(".tab-btn", has_text="Board").click()
    page.locator(".tab-btn", has_text="Files").click()
    page.wait_for_selector(".files-explorer .files-tab")
    expect(page.locator(".file-edit-textarea")).to_have_count(0)
    expect(page.locator(".markdown-wrap")).to_contain_text("not saved yet")


def test_a_restored_draft_against_a_file_the_agent_changed_conflicts_instead_of_clobbering(logged_in_page, world):
    page = logged_in_page
    _open_files(page)
    notes = world["proj"] / "notes.md"
    _row(page, "notes.md").click()
    page.locator("button:has-text('Edit')").click()
    page.locator(".file-edit-textarea").fill("# Notes\n\nmy draft\n")
    page.locator(".tab-btn", has_text="Board").click()
    notes.write_text("# Notes\n\nthe agent changed this while I was away, and made it longer\n")
    page.locator(".tab-btn", has_text="Files").click()
    page.wait_for_selector(".files-explorer .files-tab")
    expect(page.locator(".file-edit-textarea")).to_have_value("# Notes\n\nmy draft\n")
    expect(page.locator(".files-banner")).to_contain_text("changed on disk")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".files-banner")).to_contain_text("Not saved")
    assert "the agent changed this" in notes.read_text()

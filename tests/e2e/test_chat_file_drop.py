"""
Real-browser check that an agent's file drop becomes download cards, and that an image card
can be saved, not only viewed.

An agent hands the operator a file by printing `attached file: /api/projects/<id>/media/<f>`
(tools/cockpit-file). When the same reply also ended with an ```options picker, the picker
branch rendered those lines as plain markdown: one glued line of text, no link, nothing to
download. Measured 2026-10-13: 135 of 260 agent replies that dropped a file ended that way.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e

_BUBBLE = ".chat-msg-assistant"


def _assert_cards_and_picker(page):
    pdf = page.locator(f"{_BUBBLE} a.chat-att-download")
    pdf.first.wait_for(timeout=30_000)
    # The live bubble and the transcript-hydrated one can overlap for a moment as the turn ends.
    page.wait_for_function(
        "document.querySelectorAll('.chat-msg-assistant a.chat-att-download').length === 1",
        timeout=15_000,
    )
    href = pdf.first.get_attribute("href")
    assert href.startswith("/api/projects/") and href.endswith("/media/1791912799_Statement.pdf"), href
    assert pdf.first.get_attribute("download") == "Statement.pdf"

    img = page.locator(f"{_BUBBLE} img.chat-msg-img")
    img.first.wait_for(timeout=15_000)
    page.wait_for_function(
        "() => { const i = document.querySelector('.chat-msg-assistant img.chat-msg-img');"
        " return i && i.complete && i.naturalWidth > 0 }",
        timeout=15_000,
    )

    # The picker still renders, after the cards.
    assert page.locator(f"{_BUBBLE} .option-picker__label").all_inner_texts() == [
        "Fix the domain myself", "Do it through the browser",
    ]
    bubble_text = page.locator(_BUBBLE).last.inner_text()
    assert "attached file:" not in bubble_text, f"file lines leaked as text: {bubble_text!r}"
    assert "Upload these two:" in bubble_text and "Then pick one:" in bubble_text


def test_file_lines_in_a_reply_with_a_picker_become_download_cards(logged_in_page):
    page = logged_in_page
    open_project(page, "e2e-filedrop")
    send_chat(page, "e2e:filedrop")
    _assert_cards_and_picker(page)

    # The card's link really downloads the file the agent dropped.
    href = page.locator(f"{_BUBBLE} a.chat-att-download").first.get_attribute("href")
    resp = page.request.get(page.evaluate("h => new URL(h, location.href).href", href))
    assert resp.status == 200
    assert resp.headers.get("content-disposition", "").startswith("attachment")
    assert resp.body().startswith(b"%PDF")

    # An image card opens the lightbox, and its download control saves the file itself.
    page.locator(f"{_BUBBLE} img.chat-msg-img").first.click()
    dl_link = page.locator(".lightbox-overlay a.lightbox-download")
    dl_link.wait_for(timeout=10_000)
    with page.expect_download(timeout=15_000) as dl:
        dl_link.click()
    assert dl.value.suggested_filename == "License.png"
    assert open(dl.value.path(), "rb").read().startswith(b"\x89PNG")
    page.locator(".lightbox-close").click()

    # Same layout when the reply comes back from the transcript after a reload.
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, "e2e-filedrop")
    _assert_cards_and_picker(page)

"""browser_snapshot's in-page collectors, run in a REAL headless Chromium.

The fake page in test_browser_pane.py answers every evaluate() with a canned list, so
it can never tell whether the JS itself ranks on-screen controls first or finds the
text that is on screen. These tests load synthetic pages (no network) and call the
real BrowserSession.snapshot() against them. Skipped when Playwright's Chromium is
not installed on this machine.
"""

import asyncio

import pytest

import browser_pane
from browser_pane import BrowserSession

VIEWPORT = {"width": 1280, "height": 800}


def _run(html: str, scroll_to: "str | None", check, max_chars: int = 4000, scroll_y: "int | None" = None):
    async def main():
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            pytest.skip("playwright is not installed")
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as e:  # browsers not downloaded on this machine
                pytest.skip(f"chromium unavailable: {e}")
            try:
                page = await browser.new_page(viewport=VIEWPORT)
                await page.set_content(html)
                if scroll_to:
                    await page.evaluate(f"document.querySelector({scroll_to!r}).scrollIntoView()")
                if scroll_y is not None:
                    await page.evaluate(f"window.scrollTo(0, {scroll_y})")
                s = BrowserSession("k")
                s._started = True
                s._page = page
                snap = await s.snapshot(max_chars=max_chars)
                return await check(page, snap)
            finally:
                await browser.close()

    return asyncio.run(main())


def _article(n: int = 300) -> str:
    paras = "".join(
        f"<p id='p{i}'>Paragraph number {i} talks about subject {i * 7919 % 10007} in some detail.</p>"
        for i in range(n)
    )
    return f"<html><body><h1>Long article</h1>{paras}</body></html>"


def test_scrolled_page_puts_the_screen_first():
    async def check(page, snap):
        text = snap["text"]
        assert text.startswith("[On screen now]")
        # p200 sits at the top of the viewport; the old top-of-body text never reached it.
        assert "Paragraph number 200 " in text.split("[Page text from the top]")[0]
        assert "Paragraph number 5 " not in text.split("[Page text from the top]")[0]
        assert len(text) < 4000 + 300  # the cut still applies (+ its notice)

    _run(_article(), "#p200", check)


def test_top_of_page_text_is_unchanged():
    async def check(page, snap):
        assert "[On screen now]" not in snap["text"]
        assert snap["text"].startswith("Long article")

    _run(_article(), None, check)


def test_sticky_header_does_not_hide_a_scroll():
    html = _article().replace(
        "<h1>Long article</h1>",
        "<header style='position:sticky;top:0;background:#fff'>"
        "Sticky site header that repeats on every scroll position</header><h1>Long article</h1>",
    )

    async def check(page, snap):
        assert snap["text"].startswith("[On screen now]")
        assert "Paragraph number 150 " in snap["text"].split("[Page text from the top]")[0]

    _run(html, "#p150", check)


def test_on_screen_controls_outrank_rendered_ones_above():
    # 120 nav links rendered ABOVE the fold, then a scroll down to a small form. Before
    # the fix, the cap filled with the nav links in DOM order and the form vanished.
    nav = "".join(f"<a href='/n{i}'>Nav link {i}</a><br>" for i in range(120))
    filler = "<div style='height:3000px'></div>"
    form = ("<div id='form'><input id='email' placeholder='Email'>"
            "<input id='pw' type='password'><button id='go'>Sign in</button></div>")
    html = f"<html><body>{nav}{filler}{form}{filler}</body></html>"

    async def check(page, snap):
        lines = snap["elements"].splitlines()
        assert 'id="email"' in lines[0]
        assert 'id="pw"' in lines[1]
        assert 'id="go"' in lines[2]

    _run(html, "#form", check)


def _element_lines(snap):
    return [ln for ln in snap["elements"].splitlines() if ln.startswith("[")]


def test_cap_stretches_when_the_screen_holds_more_than_60_controls():
    grid = "".join(f"<button id='b{i}' style='width:60px;height:20px'>B{i}</button>" for i in range(90))
    below = "<div style='height:3000px'></div>" + "".join(f"<a href='/x{i}'>x{i}</a>" for i in range(50))
    html = f"<html><body>{grid}{below}</body></html>"

    async def check(page, snap):
        n = sum(1 for ln in snap["elements"].splitlines() if ln.startswith("["))
        assert n == 90
        assert 'id="b89"' in snap["elements"]
        assert "/x0" not in snap["elements"]

    _run(html, None, check)


def test_screen_text_joins_a_line_split_by_inline_markup():
    html = "<html><body><p>Read the <a href='/doc'>full documentation</a> before you start.</p></body></html>"

    async def check(page, snap):
        screen = await page.evaluate(browser_pane._SCREEN_TEXT_JS)
        assert screen == "Read the full documentation before you start."

    _run(html, None, check)


def test_larger_max_chars_still_leads_with_the_screen():
    async def check(page, snap):
        assert snap["text"].startswith("[On screen now]")

    _run(_article(), "#p200", check, max_chars=60000)


def test_fixed_sidebar_does_not_hide_a_scroll():
    nav = "".join(f"<div>Documentation section entry {i}</div>" for i in range(30))
    paras = "".join(f"<p id='p{i}'>Paragraph number {i} talks about subject {i * 7919 % 10007}.</p>"
                    for i in range(300))
    html = (f"<html><body><aside style='position:fixed;left:0;top:0;width:260px'>{nav}</aside>"
            f"<main style='margin-left:300px'>{paras}</main></body></html>")

    async def check(page, snap):
        assert snap["text"].startswith("[On screen now]")
        screen = snap["text"].split("[Page text from the top]")[0]
        assert "Paragraph number 150 " in screen
        # the sidebar and the paragraph beside it are separate lines, not glued together
        assert not any("entry" in ln and "Paragraph" in ln for ln in screen.splitlines())

    _run(html, "#p150", check)


def test_huge_single_text_node_is_clipped_to_the_screen():
    log = "\n".join(f"LOGLINE {i} some event happened here" for i in range(4000))
    html = f"<html><body><pre id='log'>{log}</pre><span id='mid'></span></body></html>"

    async def check(page, snap):
        screen = await page.evaluate(browser_pane._SCREEN_TEXT_JS)
        assert len(screen) <= 8000
        assert "LOGLINE 2000 " in screen
        assert "LOGLINE 0 " not in screen and "LOGLINE 3999 " not in screen
        assert snap["text"].startswith("[On screen now]")

    # each <pre> line is ~15px tall; land in the middle of the log
    _run(html, None, check, scroll_y=2000 * 15)


def test_invisible_text_on_screen_is_not_reported():
    html = ("<html><body><p>Plainly visible sentence on the screen.</p>"
            "<p style='visibility:hidden'>Hidden by visibility sentence.</p>"
            "<p style='opacity:0'>Hidden by opacity sentence here.</p></body></html>")

    async def check(page, snap):
        screen = await page.evaluate(browser_pane._SCREEN_TEXT_JS)
        assert "Plainly visible" in screen
        assert "visibility sentence" not in screen and "opacity sentence" not in screen

    _run(html, None, check)


def test_tall_editor_scrolled_past_its_middle_stays_listed():
    toolbar = "".join(f"<button style='position:fixed;top:0;left:{i * 15}px;width:14px'>t{i}</button>"
                      for i in range(80))
    html = (f"<html><body>{toolbar}<div id='ed' contenteditable='true' "
            f"style='height:4000px;margin-top:40px'>Body of the draft</div></body></html>")

    async def check(page, snap):
        assert 'id="ed"' in snap["elements"]

    _run(html, None, check, scroll_y=2500)


def test_controls_under_a_modal_overlay_do_not_crowd_out_the_dialog():
    links = "".join(f"<a href='/l{i}' style='display:inline-block;width:60px'>L{i}</a>" for i in range(120))
    html = (f"<html><body>{links}"
            "<div style='position:fixed;inset:0;background:rgba(0,0,0,.6)'></div>"
            "<div role='dialog' style='position:fixed;top:40%;left:40%;background:#fff;padding:20px'>"
            "We use cookies <button id='accept'>Accept</button></div></body></html>")

    async def check(page, snap):
        assert 'id="accept"' in _element_lines(snap)[0]

    _run(html, None, check)


def test_off_screen_controls_still_fill_the_list():
    html = ("<html><body><button id='top'>Top</button><div style='height:5000px'></div>"
            "<input id='far' placeholder='Far below'></body></html>")

    async def check(page, snap):
        lines = _element_lines(snap)
        assert 'id="top"' in lines[0]
        assert any('id="far"' in ln for ln in lines)

    _run(html, None, check)


def test_password_field_below_a_huge_toolbar_is_not_lost():
    toolbar = "".join(f"<button style='width:40px;height:20px'>b{i}</button>" for i in range(110))
    html = (f"<html><body><div style='position:sticky;top:0'>{toolbar}</div>"
            "<div style='height:3000px'></div>"
            "<input id='user'><input id='pw' type='password'><button id='go'>Sign in</button></body></html>")

    async def check(page, snap):
        assert 'id="pw"' in snap["elements"]
        assert 'id="user"' in snap["elements"]

    _run(html, None, check)


def test_iframe_element_list_keeps_the_old_cap():
    links = "".join(f"<a href='/ad{i}'>ad {i}</a> " for i in range(300))
    html = (f"<html><body><p>Top of the page</p><div style='height:4000px'></div>"
            f"<iframe srcdoc=\"<html><body>{links}</body></html>\" width='600' height='400'></iframe>"
            "</body></html>")

    async def check(page, snap):
        frame_section = snap["elements"].split("[iframe ")[1]
        n = sum(1 for ln in frame_section.splitlines() if ln.startswith("["))
        assert n == 60

    _run(html, None, check)


def test_screen_text_keeps_side_by_side_columns_apart():
    html = ("<html><body><div style='display:flex;gap:120px'>"
            "<div>Left column navigation item</div><div>Right column article sentence</div>"
            "</div></body></html>")

    async def check(page, snap):
        screen = await page.evaluate(browser_pane._SCREEN_TEXT_JS)
        assert screen.splitlines() == ["Left column navigation item", "Right column article sentence"]

    _run(html, None, check)


# Oracle: which whitespace-separated tokens of one text node are really in the viewport.
_TOKENS_JS = """
(id) => {
    const node = document.getElementById(id).firstChild, raw = node.textContent;
    const range = document.createRange(), res = [], re = /\\S+/g;
    let m;
    while ((m = re.exec(raw))) {
        range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
        const b = range.getBoundingClientRect();
        res.push([m[0], b.width > 0 && b.height > 0 && b.bottom > 0 && b.top < innerHeight &&
                        b.right > 0 && b.left < innerWidth]);
    }
    return res;
}
"""


async def _screen_vs_oracle(page, node_id):
    screen = await page.evaluate(browser_pane._SCREEN_TEXT_JS)
    got = set(screen.split())
    tokens = await page.evaluate(_TOKENS_JS, node_id)
    visible = {t for t, v in tokens if v}
    hidden = {t for t, v in tokens if not v}
    recall = len(visible & got) / max(1, len(visible))
    leaked = len(hidden & got) / max(1, len(got))
    return recall, leaked


def test_multi_column_text_node_is_read_column_by_column():
    words = " ".join(f"w{i}" for i in range(6000))
    html = f"<html><body><div id='mc' style='column-count:3;column-gap:60px;width:1200px'>{words}</div></body></html>"

    async def check(page, snap):
        recall, leaked = await _screen_vs_oracle(page, "mc")
        assert recall >= 0.95, recall
        assert leaked <= 0.1, leaked

    _run(html, None, check, scroll_y=600)


def test_blank_line_runs_do_not_shift_the_clip():
    rows = [f"L{i} event" for i in range(1500)]
    rows[700:700] = [""] * 12
    html = f"<html><body><pre id='log'>{chr(10).join(rows)}</pre></body></html>"

    async def check(page, snap):
        recall, leaked = await _screen_vs_oracle(page, "log")
        assert recall >= 0.95, recall
        assert leaked <= 0.1, leaked

    _run(html, None, check, scroll_y=10000)


def test_rtl_sentence_with_inline_link_stays_one_line():
    html = ("<html><body><p dir='rtl' style='font-size:20px'>اقرأ <a href='/d'>الوثائق الكاملة</a> "
            "قبل البدء الآن</p></body></html>")

    async def check(page, snap):
        screen = await page.evaluate(browser_pane._SCREEN_TEXT_JS)
        assert "\n" not in screen, screen

    _run(html, None, check)


def test_rows_sharing_a_long_prefix_still_detect_the_scroll():
    rows = "".join(f"<div id='r{i}'>Sep 22 10:33:21 ops cardloop[1234]: request handled id={i} status=200</div>"
                   for i in range(400))
    html = f"<html><body>{rows}</body></html>"

    async def check(page, snap):
        assert snap["text"].startswith("[On screen now]")
        assert "id=266 " in snap["text"].split("[Page text from the top]")[0]

    _run(html, "#r266", check)


def test_stretched_link_cards_keep_their_title_links():
    cards = "".join(
        f"<div style='position:relative;display:inline-block;width:140px;height:60px'>"
        f"<a id='ti{i}' href='/t{i}'>Product title {i}</a>"
        f"<a id='st{i}' href='/s{i}' style='position:absolute;inset:0'></a></div>"
        for i in range(80))
    html = f"<html><body>{cards}</body></html>"

    async def check(page, snap):
        titles = sum(1 for ln in _element_lines(snap) if "Product title" in ln)
        assert titles >= 40, titles

    _run(html, None, check)


def test_tall_editor_with_something_at_its_visible_centre_stays_listed():
    toolbar = "".join(f"<button style='position:fixed;top:0;left:{i * 15}px;width:14px'>t{i}</button>"
                      for i in range(80))
    helper = "<div style='position:fixed;left:540px;top:350px;width:220px;height:120px;background:#eee'>hint</div>"
    html = (f"<html><body>{toolbar}{helper}<div id='ed' contenteditable='true' "
            f"style='height:4000px;margin-top:40px'>Body of the draft</div></body></html>")

    async def check(page, snap):
        assert 'id="ed"' in snap["elements"]

    _run(html, None, check, scroll_y=2500)


def test_sliver_of_a_tall_control_at_the_screen_edge_ranks_on_screen():
    # Only the top 60px of a 3000px link is visible; its raw centre is far below the
    # screen, so only a hit test on the VISIBLE part can see it.
    # The link comes FIRST in the DOM, so on rank 3 it leads the list; demoted to rank 2
    # it falls behind 100 on-screen buttons and out of the 100 cap.
    buttons = "".join(f"<button style='width:40px;height:20px'>b{i}</button>" for i in range(100))
    html = ("<html><body><a id='tall' href='/tall' style='position:absolute;top:740px;left:0;"
            "display:block;width:300px;height:3000px'>Tall card</a>"
            f"{buttons}</body></html>")

    async def check(page, snap):
        assert 'id="tall"' in snap["elements"]

    _run(html, None, check)


def test_on_screen_field_past_the_cap_and_form_submit_are_kept():
    buttons = "".join(f"<button style='width:40px;height:20px'>b{i}</button>" for i in range(110))
    html = (f"<html><body><div>{buttons}</div>"
            "<form><input id='pw2' type='password'><button id='login'>Log in</button></form>"
            "<div style='height:3000px'></div>"
            "<div id='notes' contenteditable='true'>Notes</div></body></html>")

    async def check(page, snap):
        for el_id in ("pw2", "login", "notes"):
            assert f'id="{el_id}"' in snap["elements"], el_id

    _run(html, None, check)


def _fixed_grid(n):
    return "".join(f"<button style='position:fixed;top:{(i // 60) * 22}px;left:{(i % 60) * 20}px;"
                   f"width:18px;height:20px'>g{i}</button>" for i in range(n))


def test_tall_non_field_control_with_a_box_at_its_centre_keeps_its_rank():
    # A tall link (not a form field, so the reserved tail cannot rescue it) comes first
    # in the DOM. 120 fixed buttons make the 100 cap bind; a plain box sits at the
    # centre of the link's visible part. Only a multi-point hit test keeps it on rank 3.
    helper = "<div style='position:fixed;left:540px;top:350px;width:220px;height:120px;background:#eee'>hint</div>"
    html = ("<html><body><a id='card' href='/card' style='display:block;height:4000px;margin-top:60px'>"
            f"Card</a>{_fixed_grid(120)}{helper}</body></html>")

    async def check(page, snap):
        assert 'id="card"' in snap["elements"]

    _run(html, None, check, scroll_y=2500)


def test_a_transparent_full_screen_layer_does_not_shrink_the_list():
    # Some sites lay an invisible click-catcher over everything. Every control then
    # hit-tests as covered (rank 2); they are still on screen and must all be listed.
    buttons = "".join(f"<button style='width:40px;height:20px'>b{i}</button>" for i in range(90))
    html = (f"<html><body>{buttons}"
            "<div style='position:fixed;inset:0;background:transparent'></div></body></html>")

    async def check(page, snap):
        assert len(_element_lines(snap)) == 90

    _run(html, None, check)

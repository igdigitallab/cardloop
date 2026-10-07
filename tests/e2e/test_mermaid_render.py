"""
Real-browser render check for the markdown diagram stack (spec-096 P1).

mermaid, its katex (math labels) and its dompurify (label sanitising) all ship in the
production bundle, and dependency bumps change them silently. A unit test cannot see a
bundling or runtime break (a katex major, a vite major), so this drives the BUILT cockpit:
a chat reply with two ```mermaid blocks must become two SVGs, the math label must be
typeset by KaTeX, and the page must log no error.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e


def test_mermaid_diagrams_render_with_katex_and_no_console_errors(logged_in_page):
    page = logged_in_page
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
    )

    open_project(page, "e2e-mermaid")
    send_chat(page, "e2e:mermaid")

    # Both diagrams (flowchart + sequence) become SVG inside the assistant bubble.
    page.wait_for_function(
        "document.querySelectorAll('.chat-msg-assistant .mermaid-svg svg').length >= 2",
        timeout=30_000,
    )
    # The math label was typeset by KaTeX (mermaid lazy-loads it), not left as raw `$$...$$`.
    page.wait_for_selector(".chat-msg-assistant .mermaid-svg .katex", timeout=15_000)
    svg_text = page.locator(".chat-msg-assistant .mermaid-svg").first.inner_text()
    assert "$$" not in svg_text, f"math label rendered as raw text: {svg_text!r}"

    # A syntax error would fall back to the source in <pre class="mermaid-error">.
    assert page.locator(".chat-msg-assistant pre.mermaid-error").count() == 0
    assert errors == [], "browser logged errors while rendering diagrams:\n" + "\n".join(errors)

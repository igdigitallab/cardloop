"""
Scrolling a long chat back to its very first message.

The feed used to hold the last 100 messages of the session; scrolling up simply ran out. Now
reaching the top fetches the page above (`session-history?before=<idx>`) and prepends it without
moving the view, and a later turn extends the window (`since=`) instead of dropping the pages.

The session is seeded on disk (520 feed messages) and bound to the chat through the ordinary
session picker endpoint, so the server path is the real one; only the engine is the e2e fake.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e

PROJECT = "e2e-history"
SID = "e2e-history-0000-4000-8000-000000000001"
PAIRS = 260  # 520 feed messages: the window (100) + 5 pages, the last one partial


def _seed(server) -> str:
    """Writes the transcript; returns the project's API id (the basename of its cwd)."""
    topics = json.loads((server["app_dir"] / "data" / "topics.json").read_text())
    cwd = topics[PROJECT]["cwd"]
    path = Path(server["home"]) / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(PAIRS):
            ts = (t0 + timedelta(minutes=i)).isoformat()
            f.write(json.dumps({"type": "user", "uuid": f"u-{i}", "timestamp": ts,
                                "message": {"role": "user", "content": f"hist-q-{i:04d}"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {
                "role": "assistant", "content": [{"type": "text", "text": f"hist-a-{i:04d}"}]}}) + "\n")
    return Path(cwd).name


# Holds every scroll-back page response for a while, so the test can measure the settled view
# BEFORE the page lands (otherwise it races a sub-millisecond local fetch).
_DELAY_PAGES = """
(() => {
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const res = await origFetch(input, init);
    if (url.includes('session-history') && url.includes('before=')) await new Promise(r => setTimeout(r, 400));
    return res;
  };
})();
"""


def _user_texts(page) -> list[str]:
    return page.evaluate("""() => [...document.querySelectorAll('.chat-feed .chat-msg.chat-msg-user')]
        .map(e => (e.textContent || '').match(/hist-q-\\d{4}|after-scroll-back/))
        .filter(Boolean).map(m => m[0])""")


def _top_of(page, text: str) -> float:
    """Where the message carrying `text` sits relative to the top of the feed's viewport (px)."""
    return page.evaluate("""(text) => {
        const feed = document.querySelector('.chat-feed');
        const el = [...feed.querySelectorAll('.chat-msg')].find(e => (e.textContent || '').includes(text));
        return el.getBoundingClientRect().top - feed.getBoundingClientRect().top;
    }""", text)


def test_scrolling_up_reaches_the_first_message_without_jumps(logged_in_page, e2e_server):
    page = logged_in_page
    pid = _seed(e2e_server)
    resp = page.request.post(f"{e2e_server['base_url']}/api/projects/{pid}/session",
                             data={"action": "resume", "session_id": SID})
    assert resp.ok, resp.text()
    page.add_init_script(_DELAY_PAGES)
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, PROJECT)
    page.wait_for_selector("text=hist-q-0259", timeout=10_000)

    texts = _user_texts(page)
    assert texts[-1] == "hist-q-0259" and "hist-q-0000" not in texts
    assert len(texts) == 50  # the last 100 feed messages

    for _ in range(12):
        texts = _user_texts(page)
        if texts[0] == "hist-q-0000":
            break
        top_text = texts[0]
        n = page.evaluate("document.querySelectorAll('.chat-feed .chat-msg').length")
        page.evaluate("document.querySelector('.chat-feed').scrollTop = 0")
        page.wait_for_selector(".chat-older-loading", state="attached", timeout=5_000)  # in flight
        page.wait_for_timeout(150)  # the view has settled; the page lands 400 ms after its fetch
        before = _top_of(page, top_text)
        page.wait_for_function("(n) => document.querySelectorAll('.chat-feed .chat-msg').length > n",
                               arg=n, timeout=10_000)
        page.wait_for_selector(".chat-older-loading", state="detached", timeout=10_000)
        # The page landed ABOVE the view: the message that was on top did not move.
        assert abs(_top_of(page, top_text) - before) <= 2, "the view jumped when a page was prepended"

    texts = _user_texts(page)
    assert texts == [f"hist-q-{i:04d}" for i in range(PAIRS)], "every message once, in order"

    # A new turn while scrolled back: the window extends instead of sliding away from the pages.
    send_chat(page, "after-scroll-back")
    page.wait_for_selector("text=e2e fake ack: after-scroll-back", timeout=15_000)
    page.wait_for_timeout(1500)  # the completion hydrate
    texts = _user_texts(page)
    assert texts == [f"hist-q-{i:04d}" for i in range(PAIRS)] + ["after-scroll-back"]

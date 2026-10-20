"""
Scrolling a chat back across a context reset into the session before it.

A reset (↺, auto-rotation at the context limit) starts a new session, and the feed reads one
transcript per session — so scrolling up used to stop dead at the reset. Now the feed carries on
into the previous session under a "New session" marker that says what reset the context and, when
the new session started from a handoff summary, shows that summary.

Two paths: a reset made through the cockpit (the link is recorded when the first new turn names
its session), and an older session that predates the link record (recognised from the handoff its
first prompt carries). Only the engine is the e2e fake; transcripts are seeded on disk.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from .conftest import open_project, send_chat

pytestmark = pytest.mark.e2e


def _sessions_dir(server, project: str) -> "tuple[Path, str]":
    topics = json.loads((server["app_dir"] / "data" / "topics.json").read_text())
    cwd = topics[project]["cwd"]
    d = Path(server["home"]) / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d, Path(cwd).name


def _write(path: Path, pairs: "list[tuple[str, str]]", t0: datetime) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, (q, a) in enumerate(pairs):
            ts = (t0 + timedelta(minutes=i)).isoformat()
            f.write(json.dumps({"type": "user", "uuid": f"u-{path.stem}-{i}", "timestamp": ts,
                                "message": {"role": "user", "content": q}}) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {
                "role": "assistant", "content": [{"type": "text", "text": a}]}}) + "\n")


_COUNT_HISTORY_FETCHES = """
(() => {
  window.__historyFetches = [];
  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url.includes('session-history')) window.__historyFetches.push(url);
    return origFetch(input, init);
  };
})();
"""


def _feed_order(page) -> list[str]:
    """User bubbles and reset markers, top to bottom."""
    return page.evaluate("""() => [...document.querySelectorAll(
            '.chat-feed .chat-msg.chat-msg-user, .chat-feed .chat-session-break')]
        .map(e => e.classList.contains('chat-session-break') ? 'BREAK'
             : ((e.textContent || '').match(/old-q-\\d{4}|new-q-\\d|first-after-reset/) || [''])[0])
        .filter(Boolean)""")


def _scroll_to_top_until(page, text: str, tries: int = 10) -> None:
    for _ in range(tries):
        if page.locator(f".chat-feed :text('{text}')").count():
            return
        page.evaluate("document.querySelector('.chat-feed').scrollTop = 0")
        page.wait_for_timeout(500)
    page.wait_for_selector(f"text={text}", timeout=5_000)


def test_a_reset_made_in_the_cockpit_keeps_the_old_session_above_the_new_one(logged_in_page, e2e_server):
    page = logged_in_page
    sdk, pid = _sessions_dir(e2e_server, "e2e-reset")
    old_sid = "e2e-reset-old-0000-4000-8000-000000000001"
    _write(sdk / f"{old_sid}.jsonl", [(f"old-q-{i:04d}", f"old-a-{i:04d}") for i in range(130)],
           datetime(2026, 10, 1, tzinfo=timezone.utc))
    base = e2e_server["base_url"]
    # Opening the project creates its chat record, which is what a reset marks.
    open_project(page, "e2e-reset")
    assert page.request.get(f"{base}/api/projects/{pid}/chats").json()["chats"]
    assert page.request.post(f"{base}/api/projects/{pid}/session",
                             data={"action": "resume", "session_id": old_sid}).ok
    rot = page.request.post(f"{base}/api/projects/{pid}/rotate", data={"handoff": False})
    assert rot.ok and rot.json()["reset"] is True, rot.text()
    hist = page.request.get(f"{base}/api/projects/{pid}/session-history").json()
    assert hist["prev_session"]["session_id"] == old_sid, hist

    page.add_init_script(_COUNT_HISTORY_FETCHES)
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, "e2e-reset")
    # The new session is empty: the feed fills itself with the session the reset ended.
    page.wait_for_selector(".chat-session-break", timeout=10_000)
    page.wait_for_selector("text=old-q-0129", timeout=10_000)
    marker = page.locator(".chat-session-break").inner_text()
    assert "New session" in marker and "Session reset by hand" in marker
    assert "nothing carried over" in marker
    assert _feed_order(page)[-2:] == ["old-q-0129", "BREAK"]

    # The first turn names the new session; the old one stays above it — kept, not dropped and
    # fetched again (which would blank it for a moment and lose the reader's place).
    fetched = page.evaluate(f"window.__historyFetches.filter(u => u.includes('{old_sid}')).length")
    send_chat(page, "first-after-reset")
    page.wait_for_selector("text=e2e fake ack: first-after-reset", timeout=15_000)
    page.wait_for_timeout(1500)  # the completion hydrate
    assert _feed_order(page)[-3:] == ["old-q-0129", "BREAK", "first-after-reset"]
    assert page.evaluate(
        f"window.__historyFetches.filter(u => u.includes('{old_sid}')).length") == fetched

    # And the old session pages back to its very first message.
    _scroll_to_top_until(page, "old-q-0000")
    order = _feed_order(page)
    assert order == [f"old-q-{i:04d}" for i in range(130)] + ["BREAK", "first-after-reset"]

    # The link survives a reload: it is on disk, not in the page.
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, "e2e-reset")
    page.wait_for_selector(".chat-session-break", timeout=10_000)
    assert _feed_order(page)[-3:] == ["old-q-0129", "BREAK", "first-after-reset"]


def test_an_older_session_finds_its_predecessor_and_shows_the_handoff(logged_in_page, e2e_server):
    page = logged_in_page
    sdk, pid = _sessions_dir(e2e_server, "e2e-reset-old")
    prev_sid = "e2e-reset-prev-0000-4000-8000-000000000002"
    new_sid = "e2e-reset-next-0000-4000-8000-000000000003"
    t0 = datetime(2026, 10, 2, tzinfo=timezone.utc)
    last_q = "old-q-0019 — make the history scroll past the reset please"
    last_a = "Done: the reset no longer ends the feed."
    _write(sdk / f"{prev_sid}.jsonl",
           [(f"old-q-{i:04d}", f"old-a-{i:04d}") for i in range(19)] + [(last_q, last_a)], t0)
    handoff = ("## Where we stopped\nScroll-back across resets.\n\n---\n"
               f"Last instruction (operator): {last_q}\n"
               f"Where we stopped (agent's last message):\n{last_a}")
    first = ("<prior-session-summary>\nThe previous session was rotated to stay lean. Summary of "
             "where we left off below.\nContinue this work if the new message relates to it; "
             "ignore this block if starting something unrelated.\n\n" + handoff
             + "\n</prior-session-summary>\n\nnew-q-0")
    _write(sdk / f"{new_sid}.jsonl", [(first, "new-a-0"), ("new-q-1", "new-a-1")],
           t0 + timedelta(hours=3))
    assert page.request.post(f"{e2e_server['base_url']}/api/projects/{pid}/session",
                             data={"action": "resume", "session_id": new_sid}).ok

    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, "e2e-reset-old")
    page.wait_for_selector(".chat-session-break", timeout=10_000)
    page.wait_for_selector("text=old-q-0000", timeout=10_000)
    order = _feed_order(page)
    assert order == [f"old-q-{i:04d}" for i in range(20)] + ["BREAK", "new-q-0", "new-q-1"]
    marker = page.locator(".chat-session-break")
    assert "handoff summary carried over" in marker.inner_text()
    # The service block never shows as the operator's own words.
    assert "prior-session-summary" not in page.locator(".chat-feed").inner_text()
    marker.get_by_role("button", name="Show handoff").click()
    page.wait_for_selector(".chat-session-break-handoff >> text=Scroll-back across resets.", timeout=5_000)

"""
spec-098 P4: "Forget from here" on an assistant reply.

Three seeded turns (transcript + turn marks), the chat switched to that session, two windows
open. Clicking the action on turn 2's reply -> dialog (dry run) -> "Forget forever" cuts turns 2
and 3 everywhere: this window's feed keeps turn 1 and the composer holds turn 2's prompt, the
other window's feed loses turns 2 and 3 (bus `chat_forgotten`), the transcript on disk is exactly
the bytes before turn 2's offset, and the sentinels are gone from the whole data dir.
Only the engine is the e2e fake; the transcript and the marks are seeded on disk.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from .conftest import open_project

pytestmark = pytest.mark.e2e

PROJECT = "e2e-forget-turn"
SENTINEL_2 = "FORGETTURNSENTINEL2-4471"
SENTINEL_3 = "FORGETTURNSENTINEL3-4472"


def _sessions_dir(server) -> Path:
    topics = json.loads((server["app_dir"] / "data" / "topics.json").read_text())
    cwd = topics[PROJECT]["cwd"]
    d = Path(server["home"]) / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed(server, sid: str, texts: "list[tuple[str, str]]", t0: datetime, chat_id: str):
    """Write the transcript (user + assistant line per turn) and a start/end mark per turn.
    Returns (path, bytes, the byte offset where each turn's first line starts)."""
    path = _sessions_dir(server) / f"{sid}.jsonl"
    marks = server["app_dir"] / "data" / "turn_marks"
    marks.mkdir(parents=True, exist_ok=True)
    offsets: "list[int]" = []
    mark_rows: "list[dict]" = []
    body = b""
    for i, (q, a) in enumerate(texts):
        when = t0 + timedelta(minutes=i)
        offsets.append(len(body))
        for line in (
            {"type": "user", "uuid": f"u-{sid}-{i}", "timestamp": when.isoformat(), "sessionId": sid,
             "message": {"role": "user", "content": q}},
            {"type": "assistant", "timestamp": (when + timedelta(seconds=1)).isoformat(), "sessionId": sid,
             "message": {"role": "assistant", "content": [{"type": "text", "text": a}]}},
        ):
            body += (json.dumps(line) + "\n").encode()
        ts = when.timestamp()
        run_id = f"e2e-run-{i}"
        mark_rows.append({"run_id": run_id, "chat_id": chat_id, "session_key": f"{PROJECT}:{chat_id}",
                          "source": "chat", "session_id": sid, "offset": offsets[-1], "ts_start": ts - 1})
        mark_rows.append({"run_id": run_id, "ts_end": ts + 5})
    path.write_bytes(body)
    (marks / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in mark_rows))
    return path, body, offsets


def test_forget_from_here_cuts_the_turn_everywhere(logged_in_page, e2e_server):
    page = logged_in_page
    sid = "e2e-forget-turn-0000-4000-8000-000000000001"
    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)

    open_project(page, PROJECT)
    chats = page.request.get(f"{e2e_server['base_url']}/api/projects/{PROJECT}/chats").json()
    chat_id = chats.get("active") or chats["chats"][0]["id"]

    path, body, offsets = _seed(e2e_server, sid, [
        ("forget-turn-q-1 keep me", "forget-turn-a-1 keep me"),
        (f"forget-turn-q-2 {SENTINEL_2}", f"forget-turn-a-2 {SENTINEL_2}"),
        (f"forget-turn-q-3 {SENTINEL_3}", f"forget-turn-a-3 {SENTINEL_3}"),
    ], t0, chat_id)

    # switch the chat to the seeded session like a user would
    page.click(".session-selector-btn")
    page.locator(".session-dropdown-item", has_text="forget-turn-q-1").first.click()
    page.get_by_text("forget-turn-a-3").first.wait_for(timeout=10_000)

    second = page.context.new_page()
    second.goto(e2e_server["base_url"])
    second.wait_for_selector(".project-item", timeout=10_000)
    open_project(second, PROJECT)
    second.get_by_text("forget-turn-a-3").first.wait_for(timeout=10_000)
    # the bus only reaches a window that is already subscribed
    second.wait_for_timeout(2_000)

    # the action sits on turn 2's reply (the second assistant group)
    group = page.locator(".chat-msg-group", has_text="forget-turn-a-2")
    group.hover()
    group.locator("[data-testid='forget-from-here']").click()
    page.get_by_text("Goes back to the composer").wait_for(timeout=10_000)
    assert page.locator(".forget-prompt").inner_text().startswith("forget-turn-q-2")
    assert path.read_bytes() == body  # a dry run changes nothing
    page.get_by_role("button", name=re.compile("Forget forever")).click()

    page.get_by_text(re.compile("Forgot 2 turns")).first.wait_for(timeout=30_000)
    for p in (page, second):
        p.get_by_text("forget-turn-a-1").first.wait_for(timeout=10_000)
        p.wait_for_function(
            "() => !document.body.innerText.includes('forget-turn-a-3')"
            " && !document.body.innerText.includes('forget-turn-a-2')", timeout=15_000)

    # the prompt went back to the composer of the window that executed - and only that one
    assert page.locator(".chat-textarea").first.input_value().startswith("forget-turn-q-2")
    assert SENTINEL_2 not in second.locator(".chat-textarea").first.input_value()

    # the transcript is exactly the bytes before turn 2
    assert path.read_bytes() == body[:offsets[1]]

    # nothing of turns 2/3 is left anywhere the cockpit keeps data
    data_dir = e2e_server["app_dir"] / "data"
    leftovers = [str(f) for f in data_dir.rglob("*") if f.is_file()
                 and (SENTINEL_2.encode() in f.read_bytes() or SENTINEL_3.encode() in f.read_bytes())]
    assert leftovers == []

"""
A native compaction renders as a "Context compacted" divider, not as a message from the operator.

The CLI stores the compaction summary as a `type:user` line (`isCompactSummary`) and a manual
`/compact` adds `<local-command-stdout>Compacted</local-command-stdout>` — both used to show up as
the operator's own bubbles. The transcript is seeded on disk in the shape CLI 2.1.316 writes and
bound through the ordinary session picker, so the server path is the real one.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import json
import re
from pathlib import Path

import pytest

from .conftest import open_project

pytestmark = pytest.mark.e2e

PROJECT = "e2e-compact"
SID = "e2e-compact-0000-4000-8000-000000000001"
SUMMARY = ("This session is being continued from a previous conversation that ran out of context. "
           "The summary below covers the earlier portion of the conversation.\n\n"
           "<system-reminder>\n<tools-used-before-summary></tools-used-before-summary>\n</system-reminder>\n"
           "Summary:\n1. Primary Request and Intent:\n   - Remember the codeword compact-codeword-PELICAN.\n")


def _row(i, typ, content, **extra):
    return {"type": typ, "uuid": f"c{i}", "timestamp": f"2026-11-02T16:10:{i:02d}Z",
            "message": {"role": typ, "content": content}, **extra}


def _seed(server) -> str:
    topics = json.loads((server["app_dir"] / "data" / "topics.json").read_text())
    cwd = topics[PROJECT]["cwd"]
    path = Path(server["home"]) / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        _row(1, "user", "compact-before-q"),
        _row(2, "assistant", [{"type": "text", "text": "compact-before-a"}]),
        {"type": "system", "subtype": "compact_boundary", "uuid": "c3", "timestamp": "2026-11-02T16:10:03Z",
         "compactMetadata": {"trigger": "manual", "preTokens": 16675}},
        _row(4, "user", SUMMARY, isCompactSummary=True, isVisibleInTranscriptOnly=True),
        _row(5, "user", "<command-name>/compact</command-name>\n<command-message>compact</command-message>"),
        _row(6, "user", "<local-command-stdout>Compacted </local-command-stdout>"),
        _row(7, "user", "compact-after-q"),
        _row(8, "assistant", [{"type": "text", "text": "compact-after-a"}]),
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return Path(cwd).name


def test_a_compaction_is_a_divider_with_the_summary_behind_a_click(logged_in_page, e2e_server):
    page = logged_in_page
    pid = _seed(e2e_server)
    resp = page.request.post(f"{e2e_server['base_url']}/api/projects/{pid}/session",
                             data={"action": "resume", "session_id": SID})
    assert resp.ok, resp.text()
    page.reload()
    page.wait_for_selector(".project-item", timeout=10_000)
    open_project(page, PROJECT)
    page.wait_for_selector("text=compact-after-a", timeout=10_000)

    users = page.evaluate("""() => [...document.querySelectorAll('.chat-feed .chat-msg.chat-msg-user')]
        .map(e => e.textContent || '')""")
    assert any("compact-before-q" in u for u in users) and any("compact-after-q" in u for u in users)
    assert not any("being continued" in u or "local-command-stdout" in u or "Compacted" in u for u in users)

    row = page.locator(".chat-feed .chat-compact-row")
    assert row.count() == 1
    assert "Context compacted" in row.inner_text()
    assert "compact-codeword-PELICAN" not in row.inner_text()   # collapsed until asked
    row.get_by_role("button", name="Show summary").click()
    page.wait_for_selector(".chat-compact-row .chat-session-break-handoff", timeout=5_000)
    body = row.inner_text()
    assert "compact-codeword-PELICAN" in body and "system-reminder" not in body
    page.screenshot(path=str(Path(e2e_server["home"]) / "compact-row.png"))

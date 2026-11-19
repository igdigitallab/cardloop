"""
spec-098 A: deleting a whole session from the session list.

The 🗑 next to ✎ opens the forget dialog (a dry run), "Delete forever" executes it, and the row
disappears in EVERY window showing that project's session list (the `session_deleted` bus event
makes them refetch). The transcript is gone from disk and its text from the cockpit's data dir.
Only the engine is the e2e fake; the old session's transcript is seeded on disk.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e   (needs web/dist: cd web && npm run build)
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from .conftest import open_project

pytestmark = pytest.mark.e2e

PROJECT = "e2e-forget"


def _sessions_dir(server) -> Path:
    topics = json.loads((server["app_dir"] / "data" / "topics.json").read_text())
    cwd = topics[PROJECT]["cwd"]
    d = Path(server["home"]) / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, pairs: "list[tuple[str, str]]", t0: datetime) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, (q, a) in enumerate(pairs):
            ts = (t0 + timedelta(minutes=i)).isoformat()
            f.write(json.dumps({"type": "user", "uuid": f"u-{path.stem}-{i}", "timestamp": ts,
                                "sessionId": path.stem,
                                "message": {"role": "user", "content": q}}) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": path.stem,
                                "message": {"role": "assistant",
                                            "content": [{"type": "text", "text": a}]}}) + "\n")


def _row(page, text: str):
    return page.locator(".session-dropdown-row", has_text=text)


def test_delete_session_removes_the_row_in_every_window_and_the_text_on_disk(logged_in_page, e2e_server):
    page = logged_in_page
    sdk = _sessions_dir(e2e_server)
    t0 = datetime.now(timezone.utc) - timedelta(days=2)
    doomed = "e2e-forget-doomed-0000-4000-8000-000000000001"
    kept = "e2e-forget-kept-00000-4000-8000-000000000002"
    secret = "FORGETE2ESENTINEL7731 the operator wants this gone"
    _write(sdk / f"{doomed}.jsonl", [("forget-doomed-q-0 " + secret, "forget-doomed-a-0 " + secret)], t0)
    _write(sdk / f"{kept}.jsonl", [("forget-kept-q-0", "forget-kept-a-0")], t0 + timedelta(hours=1))

    open_project(page, PROJECT)
    second = page.context.new_page()
    second.goto(e2e_server["base_url"])
    second.wait_for_selector(".project-item", timeout=10_000)
    open_project(second, PROJECT)
    # the bus only reaches a window that is already subscribed; its project stream is a fetch behind
    # the service worker, which Playwright's request events do not see, so give it a moment
    second.wait_for_timeout(2_000)

    page.click(".session-selector-btn")
    _row(page, "forget-doomed-q-0").wait_for(timeout=10_000)
    _row(page, "forget-doomed-q-0").locator("button[aria-label='Delete session']").click()
    # the dialog's dry run lists what goes before anything is deleted
    page.get_by_text("Delete session forever").wait_for(timeout=10_000)
    assert (sdk / f"{doomed}.jsonl").exists()
    page.get_by_role("button", name=re.compile("Delete forever")).click()

    # the residue scan's verdict reaches EVERY window over the bus, not just the one that deleted
    for p in (page, second):
        p.get_by_text(re.compile("Deleted — ")).first.wait_for(timeout=30_000)

    for p in (page, second):
        p.click(".session-selector-btn")
        _row(p, "forget-kept-q-0").wait_for(timeout=10_000)
        assert _row(p, "forget-doomed-q-0").count() == 0

    assert not (sdk / f"{doomed}.jsonl").exists()
    assert (sdk / f"{kept}.jsonl").exists()
    data_dir = e2e_server["app_dir"] / "data"
    leftovers = [str(f) for f in data_dir.rglob("*") if f.is_file()
                 and b"FORGETE2ESENTINEL7731" in f.read_bytes()]
    assert leftovers == []

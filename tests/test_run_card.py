"""
Unit tests for _write_sidecar, _move_card_after_run and status routing
after a card run.

The engine is mocked via run_engine=None or a fake async generator.
"""
import sys
from pathlib import Path
import asyncio

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _write_sidecar,
    _move_card_after_run,
    _tasks_path,
    _load_board,
    _save_board,
    _parse_tasks,
    AppCtx,
)


# ─────────────────────────── _write_sidecar ───────────────────────────


def test_write_sidecar_creates_file(tmp_path):
    """_write_sidecar creates the file at data_dir/runs/<card_id>.md."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="aabbcc",
        name="testproject",
        prompt="Fix the bug",
        answer_text="Fixed!",
        ok=True,
        exc_info=None,
        diff_stat="1 file changed",
        diff_full="diff --git ...",
    )
    sidecar = tmp_path / "runs" / "aabbcc.md"
    assert sidecar.exists(), "Sidecar file must be created"


def test_write_sidecar_content_ok(tmp_path):
    """Sidecar contains prompt, answer_text, and ok outcome."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="aabbcc",
        name="testproject",
        prompt="Do the thing",
        answer_text="I did the thing",
        ok=True,
        exc_info=None,
        diff_stat="",
        diff_full="",
    )
    content = (tmp_path / "runs" / "aabbcc.md").read_text(encoding="utf-8")
    assert "Do the thing" in content
    assert "I did the thing" in content
    assert "ok" in content


def test_write_sidecar_content_fail_with_exc(tmp_path):
    """When ok=False with exc_info — the error appears in the 'Error' section."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="ddeecc",
        name="proj",
        prompt="Broken task",
        answer_text="",
        ok=False,
        exc_info="RuntimeError: something went wrong\n...",
        diff_stat="",
        diff_full="",
    )
    content = (tmp_path / "runs" / "ddeecc.md").read_text(encoding="utf-8")
    assert "fail" in content
    assert "RuntimeError" in content
    assert "Error" in content


def test_write_sidecar_creates_runs_dir(tmp_path):
    """_write_sidecar automatically creates the runs/ directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # runs/ does not yet exist
    assert not (data_dir / "runs").exists()
    _write_sidecar(
        data_dir=data_dir,
        card_id="aaaaaa",
        name="p",
        prompt="x",
        answer_text="y",
        ok=True,
        exc_info=None,
        diff_stat="",
        diff_full="",
    )
    assert (data_dir / "runs" / "aaaaaa.md").exists()


def test_write_sidecar_diff_stat_included(tmp_path):
    """diff_stat and diff_full are included in the sidecar when non-empty."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="ffffff",
        name="p",
        prompt="q",
        answer_text="a",
        ok=True,
        exc_info=None,
        diff_stat="2 files changed, 10 insertions(+)",
        diff_full="diff --git a/x.py b/x.py",
    )
    content = (tmp_path / "runs" / "ffffff.md").read_text(encoding="utf-8")
    assert "2 files changed" in content
    assert "diff --git" in content


def test_write_sidecar_no_diff_when_empty(tmp_path):
    """When diffs are empty — diff sections are not added."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="cccccc",
        name="p",
        prompt="q",
        answer_text="a",
        ok=True,
        exc_info=None,
        diff_stat="",
        diff_full="",
    )
    content = (tmp_path / "runs" / "cccccc.md").read_text(encoding="utf-8")
    assert "diff" not in content.lower() or "Git diff" not in content


# ─────────────────────────── _move_card_after_run ───────────────────────────


def _setup_board(cwd: Path, card_id: str, col: str = "in_progress") -> dict:
    """Creates TASKS.md with one card in the given column. Returns the card dict."""
    card = {"id": card_id, "text": "Task to run"}
    lines = [
        "# Tasks",
        "## Backlog",
        "## In Progress",
    ]
    if col == "in_progress":
        lines.append(f"- [ ] Task to run <!--ops:{card_id}-->")
    lines += [
        "## Review",
    ]
    if col == "review":
        lines.append(f"- [ ] Task to run <!--ops:{card_id}-->")
    lines += [
        "## Failed",
    ]
    if col == "failed":
        lines.append(f"- [ ] Task to run <!--ops:{card_id}-->")
    _tasks_path(str(cwd)).write_text("\n".join(lines), encoding="utf-8")
    return card


async def test_move_card_after_run_ok_goes_to_review(tmp_path):
    """ok=True → card moves to Review."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    card = _setup_board(cwd, "aabbcc", "in_progress")

    ctx: dict = {}  # _move_card_after_run only uses _get_board_lock internally — ctx not needed
    await _move_card_after_run(ctx, str(cwd), "proj", card, "aabbcc", ok=True)

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "After ok=True card must be in Review"
    assert not any(c["id"] == "aabbcc" for c in cols["in_progress"])


async def test_move_card_after_run_fail_goes_to_failed(tmp_path):
    """ok=False → card moves to Failed."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    card = _setup_board(cwd, "aabbcc", "in_progress")

    ctx: dict = {}
    await _move_card_after_run(ctx, str(cwd), "proj", card, "aabbcc", ok=False)

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["failed"]), "After ok=False card must be in Failed"
    assert not any(c["id"] == "aabbcc" for c in cols["in_progress"])


async def test_move_card_after_run_card_already_gone(tmp_path):
    """If the card is not on the board (agent may have removed it) — it is still added to the target col.
    _move_card_after_run uses the original card-dict when pop returns None."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    # Board without the card
    _tasks_path(str(cwd)).write_text(
        "# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n",
        encoding="utf-8",
    )
    card = {"id": "aabbcc", "text": "Phantom card"}
    ctx: dict = {}
    # Must not raise an exception
    await _move_card_after_run(ctx, str(cwd), "proj", card, "aabbcc", ok=True)

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "Phantom card must be added to Review"


# ─────────────────────────── _run_card: e2e with mock engine ───────────────────────────


async def test_run_card_ok_moves_to_review_writes_sidecar(tmp_path):
    """_run_card with mock engine ok=True → Review + sidecar."""
    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Mock task"}
    _setup_board(cwd, "aabbcc", "in_progress")

    # Fake run_engine: emits one text event and a result
    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "Mock answer"}
        yield {"type": "result", "session_id": "sess-123"}

    ctx = {
        "sessions": {},
        "running": {"1001:42": True},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "run_engine": fake_engine,
        "ptb_app": None,
    }

    project = {"cwd": str(cwd), "name": "proj", "model": "sonnet"}
    session_key = "1001:42"

    # Simple stub application for webapp_app
    class FakeApp:
        def __getitem__(self, k):
            return None

    await _run_card(ctx, FakeApp(), project, card, session_key)

    # After completion running[session_key] must be released
    assert "1001:42" not in ctx["running"], "running lock must be released after completion"

    # Card in Review
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "Card must be in Review"

    # Sidecar created
    sidecar = data_dir / "runs" / "aabbcc.md"
    assert sidecar.exists(), "Sidecar must be created"
    content = sidecar.read_text(encoding="utf-8")
    assert "Mock answer" in content


async def test_run_card_fail_moves_to_failed(tmp_path):
    """_run_card with engine that raises an error → Failed + sidecar with exc_info."""
    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Failing task"}
    _setup_board(cwd, "aabbcc", "in_progress")

    async def failing_engine(**kwargs):
        yield {"type": "text", "text": "Starting..."}
        raise RuntimeError("Engine exploded")

    ctx = {
        "sessions": {},
        "running": {"1001:42": True},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "run_engine": failing_engine,
        "ptb_app": None,
    }

    project = {"cwd": str(cwd), "name": "proj", "model": "sonnet"}

    class FakeApp:
        def __getitem__(self, k):
            return None

    await _run_card(ctx, FakeApp(), project, card, "1001:42")

    # Card in Failed
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["failed"]), "Card must be in Failed"

    # Sidecar contains the error
    sidecar = data_dir / "runs" / "aabbcc.md"
    assert sidecar.exists()
    content = sidecar.read_text(encoding="utf-8")
    assert "fail" in content
    assert "RuntimeError" in content

    # running lock released
    assert "1001:42" not in ctx["running"]


# ─────────────────────────── Spec-029 item 3: structured output ───────────────────────────


async def test_run_card_structured_output_used_when_valid(tmp_path, monkeypatch):
    """When STRUCTURED_CARDS=1 and structured_output is valid, sidecar uses structured summary."""
    import webapp
    monkeypatch.setattr(webapp, "STRUCTURED_CARDS", True)

    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Structured task"}
    _setup_board(cwd, "aabbcc", "in_progress")

    structured = {
        "summary": "Implemented the feature successfully",
        "status": "done",
        "changes": ["src/main.py", "tests/test_main.py"],
    }

    async def engine_with_structured(**kwargs):
        yield {"type": "text", "text": "Prose answer from agent"}
        yield {
            "type": "result",
            "session_id": "sess-123",
            "structured_output": structured,
        }

    ctx = {
        "sessions": {},
        "running": {"1001:42": True},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "run_engine": engine_with_structured,
        "ptb_app": None,
    }
    project = {"cwd": str(cwd), "name": "proj", "model": "sonnet"}

    class FakeApp:
        def __getitem__(self, k):
            return None

    await _run_card(ctx, FakeApp(), project, card, "1001:42")

    sidecar = data_dir / "runs" / "aabbcc.md"
    content = sidecar.read_text(encoding="utf-8")
    # Structured summary must appear; raw prose must NOT replace it
    assert "Implemented the feature successfully" in content, "Structured summary should be in sidecar"
    assert "[DONE]" in content, "Status prefix should appear"
    assert "src/main.py" in content, "Changes list should appear"
    # Card still moves to review (ok=True, exception-based)
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"])


async def test_run_card_structured_output_absent_falls_back_to_prose(tmp_path, monkeypatch):
    """When STRUCTURED_CARDS=1 but structured_output is absent/None, prose path is used."""
    import webapp
    monkeypatch.setattr(webapp, "STRUCTURED_CARDS", True)

    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Fallback task"}
    _setup_board(cwd, "aabbcc", "in_progress")

    async def engine_no_structured(**kwargs):
        yield {"type": "text", "text": "Plain prose answer"}
        # structured_output absent from result event (or None)
        yield {"type": "result", "session_id": "sess-456", "structured_output": None}

    ctx = {
        "sessions": {},
        "running": {"1001:42": True},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "run_engine": engine_no_structured,
        "ptb_app": None,
    }
    project = {"cwd": str(cwd), "name": "proj", "model": "sonnet"}

    class FakeApp:
        def __getitem__(self, k):
            return None

    await _run_card(ctx, FakeApp(), project, card, "1001:42")

    sidecar = data_dir / "runs" / "aabbcc.md"
    content = sidecar.read_text(encoding="utf-8")
    # Prose path used
    assert "Plain prose answer" in content, "Prose fallback should appear in sidecar"
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"])


async def test_run_card_structured_output_malformed_falls_back_to_prose(tmp_path, monkeypatch):
    """When STRUCTURED_CARDS=1 but structured_output is malformed, prose path is used."""
    import webapp
    monkeypatch.setattr(webapp, "STRUCTURED_CARDS", True)

    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Malformed structured task"}
    _setup_board(cwd, "aabbcc", "in_progress")

    async def engine_malformed(**kwargs):
        yield {"type": "text", "text": "Prose from malformed run"}
        # structured_output present but missing required fields
        yield {
            "type": "result",
            "session_id": "sess-789",
            "structured_output": {"unexpected_key": "no summary or status"},
        }

    ctx = {
        "sessions": {},
        "running": {"1001:42": True},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "run_engine": engine_malformed,
        "ptb_app": None,
    }
    project = {"cwd": str(cwd), "name": "proj", "model": "sonnet"}

    class FakeApp:
        def __getitem__(self, k):
            return None

    await _run_card(ctx, FakeApp(), project, card, "1001:42")

    sidecar = data_dir / "runs" / "aabbcc.md"
    content = sidecar.read_text(encoding="utf-8")
    # Prose fallback must be used — no crash, no structured prefix
    assert "Prose from malformed run" in content, "Prose fallback should appear when structured_output malformed"
    assert "[DONE]" not in content and "[PARTIAL]" not in content and "[FAILED]" not in content
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"])


# ─────────────────────────── Guard: project without "id" ───────────────────────────


async def test_run_card_no_project_id_does_not_crash(tmp_path):
    """_run_card must not raise when project dict has no 'id' key.

    Spec-038 media-dir injection must be skipped silently; the run must
    complete normally and write a sidecar.
    """
    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Task without project id"}
    _setup_board(cwd, "aabbcc", "in_progress")

    async def fake_engine(**kwargs):
        yield {"type": "text", "text": "Done without id"}
        yield {"type": "result", "session_id": "sess-noid"}

    ctx = {
        "sessions": {},
        "running": {"1001:99": True},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "run_engine": fake_engine,
        "ptb_app": None,
    }

    # Project dict intentionally omits "id" — this is the bug trigger.
    project = {"cwd": str(cwd), "name": "proj", "model": "sonnet"}

    class FakeApp:
        def __getitem__(self, k):
            return None

    # Must not raise KeyError: 'id'
    await _run_card(ctx, FakeApp(), project, card, "1001:99")

    # running lock released
    assert "1001:99" not in ctx["running"]

    # Sidecar written — run completed normally
    sidecar = data_dir / "runs" / "aabbcc.md"
    assert sidecar.exists(), "Sidecar must be written even when project has no 'id'"
    assert "Done without id" in sidecar.read_text(encoding="utf-8")

    # Card moved to review
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "Card must reach Review"

    # Media dir must NOT have been created (no id — injection skipped)
    assert not (data_dir / "chat-media").exists(), "chat-media dir must not be created when project has no id"

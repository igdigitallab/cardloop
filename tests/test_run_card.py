"""
Юнит-тесты для _write_sidecar, _move_card_after_run и маршрутизации статусов
после прогона карточки.

Движок мокируем через run_engine=None или фейковый генератор.
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
    """_write_sidecar создаёт файл в data_dir/runs/<card_id>.md."""
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
    assert sidecar.exists(), "Файл сайдкара должен быть создан"


def test_write_sidecar_content_ok(tmp_path):
    """Сайдкар содержит prompt, answer_text, исход ok."""
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
    """При ok=False с exc_info — ошибка попадает в раздел 'Ошибка'."""
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
    """_write_sidecar автоматически создаёт папку runs/."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # runs/ ещё не существует
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
    """diff_stat и diff_full включаются в сайдкар если непустые."""
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
    """Если diff пустые — разделы diff не добавляются."""
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
    """Создаёт TASKS.md с одной карточкой в заданной колонке. Возвращает card-dict."""
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
    """ok=True → карточка переходит в Review."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    card = _setup_board(cwd, "aabbcc", "in_progress")

    ctx: dict = {}  # _move_card_after_run использует только _get_board_lock внутри — ctx не нужен
    await _move_card_after_run(ctx, str(cwd), "proj", card, "aabbcc", ok=True)

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "После ok=True карточка должна быть в Review"
    assert not any(c["id"] == "aabbcc" for c in cols["in_progress"])


async def test_move_card_after_run_fail_goes_to_failed(tmp_path):
    """ok=False → карточка переходит в Failed."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    card = _setup_board(cwd, "aabbcc", "in_progress")

    ctx: dict = {}
    await _move_card_after_run(ctx, str(cwd), "proj", card, "aabbcc", ok=False)

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["failed"]), "После ok=False карточка должна быть в Failed"
    assert not any(c["id"] == "aabbcc" for c in cols["in_progress"])


async def test_move_card_after_run_card_already_gone(tmp_path):
    """Если карточки нет на доске (агент мог убрать) — она всё равно добавляется в target col.
    _move_card_after_run при pops None использует оригинальный card-dict."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    # Доска без карточки
    _tasks_path(str(cwd)).write_text(
        "# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n",
        encoding="utf-8",
    )
    card = {"id": "aabbcc", "text": "Phantom card"}
    ctx: dict = {}
    # Не должно бросить исключение
    await _move_card_after_run(ctx, str(cwd), "proj", card, "aabbcc", ok=True)

    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "Phantom card должна добавиться в Review"


# ─────────────────────────── _run_card: e2e с мок-движком ───────────────────────────


async def test_run_card_ok_moves_to_review_writes_sidecar(tmp_path):
    """_run_card с мок-движком ok=True → Review + сайдкар."""
    from webapp import _run_card

    cwd = tmp_path / "proj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    card = {"id": "aabbcc", "text": "Mock task"}
    _setup_board(cwd, "aabbcc", "in_progress")

    # Фейковый run_engine: возвращает один text-event и result
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

    # Простое приложение-заглушка для webapp_app
    class FakeApp:
        def __getitem__(self, k):
            return None

    await _run_card(ctx, FakeApp(), project, card, session_key)

    # После завершения running[session_key] должен быть снят
    assert "1001:42" not in ctx["running"], "running-замок должен быть снят после завершения"

    # Карточка в Review
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["review"]), "Карточка должна быть в Review"

    # Сайдкар создан
    sidecar = data_dir / "runs" / "aabbcc.md"
    assert sidecar.exists(), "Сайдкар должен быть создан"
    content = sidecar.read_text(encoding="utf-8")
    assert "Mock answer" in content


async def test_run_card_fail_moves_to_failed(tmp_path):
    """_run_card с движком бросающим ошибку → Failed + сайдкар с exc_info."""
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

    # Карточка в Failed
    _, _, cols = _load_board(str(cwd))
    assert any(c["id"] == "aabbcc" for c in cols["failed"]), "Карточка должна быть в Failed"

    # Сайдкар содержит ошибку
    sidecar = data_dir / "runs" / "aabbcc.md"
    assert sidecar.exists()
    content = sidecar.read_text(encoding="utf-8")
    assert "fail" in content
    assert "RuntimeError" in content

    # running-замок снят
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

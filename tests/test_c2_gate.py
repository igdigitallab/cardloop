"""
Тесты C2-gate: worktree-режим карточек (apply/discard/режим-детектор).

Все worktree-операции выполняются только на tmp git-репо.
НЕ затрагивают боевые проекты Игоря.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _card_run_mode,
    _card_worktree_setup,
    _commit_in_worktree,
    _diff_from_worktree,
    _run_card,
    _write_run_meta,
    _read_run_meta,
    _write_sidecar,
    _tasks_path,
    _load_board,
    _save_board,
    _done_path,
)


# ─────────────────────────── фикстуры ───────────────────────────

@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """Временный git-репо с baseline-коммитом. Возвращает Path к корню репо."""
    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(cwd), check=True, capture_output=True)
    (cwd / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(cwd), check=True, capture_output=True)
    return cwd


@pytest.fixture
def tmp_no_git(tmp_path: Path) -> Path:
    """Временная директория БЕЗ git-репо."""
    cwd = tmp_path / "norepo"
    cwd.mkdir()
    return cwd


@pytest.fixture
def fake_ctx_with_data(tmp_path: Path) -> dict:
    """ctx с реальным DATA-каталогом."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return {
        "topics": {},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }


def _make_board(cwd: Path, card_id: str, col: str = "review") -> dict:
    """Создаёт TASKS.md с карточкой в указанной колонке."""
    card = {"id": card_id, "text": "Test task"}
    lines = [
        "# Tasks",
        "## Backlog",
        "## In Progress",
        "## Review",
    ]
    if col == "review":
        lines.append(f"- [ ] Test task <!--ops:{card_id}-->")
    lines += ["## Failed"]
    _tasks_path(str(cwd)).write_text("\n".join(lines), encoding="utf-8")
    return card


# ─────────────────────────── режим-детектор ───────────────────────────

async def test_mode_detector_worktree_clean_git(tmp_git):
    """git+чистое дерево → worktree"""
    mode = await _card_run_mode(str(tmp_git))
    assert mode == "worktree"


async def test_mode_detector_legacy_no_git(tmp_no_git):
    """Не git-репо → legacy"""
    mode = await _card_run_mode(str(tmp_no_git))
    assert mode == "legacy"


async def test_mode_detector_legacy_dirty_tree(tmp_git):
    """git+грязное дерево → legacy"""
    (tmp_git / "dirty.txt").write_text("uncommitted change\n")
    mode = await _card_run_mode(str(tmp_git))
    assert mode == "legacy"


# ─────────────────────────── worktree setup ───────────────────────────

async def test_worktree_setup_creates_worktree(tmp_git):
    """_card_worktree_setup создаёт worktree и ветку card-<id>."""
    info = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info is not None, "setup должен вернуть dict, не None"
    assert "wt_path" in info
    assert "base_branch" in info
    wt = Path(info["wt_path"])
    assert wt.exists(), "Директория worktree должна существовать"
    # Проверяем что ветка создана
    result = subprocess.run(
        ["git", "branch"], cwd=str(tmp_git), capture_output=True, text=True
    )
    assert "card-aabbcc" in result.stdout


async def test_worktree_setup_cleans_existing(tmp_git):
    """Повторный setup чистит старый worktree и пересоздаёт."""
    info1 = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info1 is not None
    # Пишем файл в worktree — после cleanup он должен исчезнуть
    old_wt = Path(info1["wt_path"])
    (old_wt / "canary.txt").write_text("old")
    info2 = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info2 is not None
    # Старый worktree удалён, новый создан
    new_wt = Path(info2["wt_path"])
    assert new_wt.exists()
    assert not (new_wt / "canary.txt").exists(), "Canary из старого worktree не должен быть в новом"


async def test_worktree_setup_no_git(tmp_no_git):
    """Не git-репо → None (деградация)."""
    info = await _card_worktree_setup(str(tmp_no_git), "aabbcc")
    assert info is None


# ─────────────────────────── _commit_in_worktree ───────────────────────────

async def test_commit_in_worktree_with_changes(tmp_git):
    """Если в worktree есть изменения — коммит делается, возвращает True."""
    info = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info is not None
    wt_path = info["wt_path"]
    # Пишем файл в worktree
    (Path(wt_path) / "new_file.py").write_text("# created by agent\n")
    result = await _commit_in_worktree(wt_path, "aabbcc", "Add new file")
    assert result is True
    # Проверяем что коммит появился
    log = subprocess.run(["git", "log", "--oneline"], cwd=wt_path, capture_output=True, text=True)
    assert "card aabbcc" in log.stdout


async def test_commit_in_worktree_no_changes(tmp_git):
    """Нет изменений → нет коммита, возвращает False."""
    info = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info is not None
    result = await _commit_in_worktree(info["wt_path"], "aabbcc", "No changes")
    assert result is False


# ─────────────────────────── JSON meta ───────────────────────────

def test_write_read_run_meta(tmp_path):
    """_write_run_meta / _read_run_meta round-trip."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": "aabbcc",
        "mode": "worktree",
        "branch": "card-aabbcc",
        "base_branch": "main",
        "wt_path": "/tmp/wt",
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, "aabbcc", meta)
    loaded = _read_run_meta(data_dir, "aabbcc")
    assert loaded == meta


def test_read_run_meta_missing(tmp_path):
    """_read_run_meta возвращает None если нет файла."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert _read_run_meta(data_dir, "nonexistent") is None


# ─────────────────────────── _write_sidecar с режимом ───────────────────────────

def test_write_sidecar_worktree_mode_writes_json(tmp_path):
    """_write_sidecar в worktree-режиме записывает и .md и .json."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="aabbcc",
        name="proj",
        prompt="Test prompt",
        answer_text="Test answer",
        ok=True,
        exc_info=None,
        diff_stat="1 file",
        diff_full="diff --git ...",
        run_mode="worktree",
        wt_branch="card-aabbcc",
        base_branch="main",
        wt_path="/tmp/.worktrees/card-aabbcc",
        has_changes=True,
    )
    md = tmp_path / "runs" / "aabbcc.md"
    js = tmp_path / "runs" / "aabbcc.json"
    assert md.exists()
    assert js.exists()
    meta = json.loads(js.read_text())
    assert meta["mode"] == "worktree"
    assert meta["has_changes"] is True
    assert meta["applied"] is False
    assert meta["discarded"] is False


def test_write_sidecar_legacy_mode_writes_json(tmp_path):
    """_write_sidecar в legacy-режиме тоже пишет JSON с mode=legacy."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="cccccc",
        name="proj",
        prompt="x",
        answer_text="y",
        ok=True,
        exc_info=None,
        diff_stat="",
        diff_full="",
    )
    js = tmp_path / "runs" / "cccccc.json"
    assert js.exists()
    meta = json.loads(js.read_text())
    assert meta["mode"] == "legacy"


# ─────────────────────────── apply success ───────────────────────────

async def test_apply_success(tmp_git, tmp_path):
    """apply: merge --no-ff успешный → карточка Done, worktree удалён."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply, _tasks_path

    # Setup: создаём worktree с коммитом
    card_id = "aabbcc"
    info = await _card_worktree_setup(str(tmp_git), card_id)
    assert info is not None
    (Path(info["wt_path"]) / "feature.py").write_text("x = 1\n")
    await _commit_in_worktree(info["wt_path"], card_id, "Add feature")

    # DATA и meta
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": info["base_branch"],
        "wt_path": info["wt_path"],
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    # Доска с карточкой в Review
    _make_board(tmp_git, card_id, "review")

    # ctx и app
    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))

    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    data = json.loads(resp.body)
    assert resp.status == 200, f"Expected 200, got {resp.status}: {data}"
    assert data["applied"] is True

    # worktree удалён
    assert not Path(info["wt_path"]).exists(), "Worktree должен быть удалён после apply"

    # Карточка в Done
    dp = _done_path(str(tmp_git))
    assert dp.exists()
    done_content = dp.read_text()
    assert card_id in done_content or "Test task" in done_content

    # meta обновлена
    updated_meta = _read_run_meta(data_dir, card_id)
    assert updated_meta["applied"] is True


async def test_apply_conflict(tmp_git, tmp_path):
    """apply при конфликте → 409, карточка в Review, worktree жив."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    card_id = "aabbcc"
    info = await _card_worktree_setup(str(tmp_git), card_id)
    assert info is not None

    # В worktree пишем изменение в README.md
    (Path(info["wt_path"]) / "README.md").write_text("# Modified in branch\n")
    await _commit_in_worktree(info["wt_path"], card_id, "Modify README")

    # В main дереве тоже меняем README.md → конфликт
    (tmp_git / "README.md").write_text("# Modified on main\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_git), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main change"],
        cwd=str(tmp_git), check=True, capture_output=True
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": info["base_branch"],
        "wt_path": info["wt_path"],
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)
    _make_board(tmp_git, card_id, "review")

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 409, f"Expected 409 (conflict), got {resp.status}"
    data = json.loads(resp.body)
    assert "error" in data

    # Worktree должен быть жив
    assert Path(info["wt_path"]).exists(), "Worktree должен остаться после конфликта"

    # meta не помечена applied
    updated = _read_run_meta(data_dir, card_id)
    assert updated["applied"] is False


async def test_discard(tmp_git, tmp_path):
    """discard → ветка/worktree удалены, карточка в Backlog."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_discard

    card_id = "aabbcc"
    info = await _card_worktree_setup(str(tmp_git), card_id)
    assert info is not None

    # Делаем коммит чтобы ветка не была пустой
    (Path(info["wt_path"]) / "new.py").write_text("x = 1\n")
    await _commit_in_worktree(info["wt_path"], card_id, "Add file")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": info["base_branch"],
        "wt_path": info["wt_path"],
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)
    _make_board(tmp_git, card_id, "review")

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/discard",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_discard(req)
    data = json.loads(resp.body)
    assert resp.status == 200, f"Expected 200, got {resp.status}: {data}"
    assert data["discarded"] is True

    # Worktree удалён
    assert not Path(info["wt_path"]).exists(), "Worktree должен быть удалён после discard"

    # Ветка удалена
    branches = subprocess.run(
        ["git", "branch"], cwd=str(tmp_git), capture_output=True, text=True
    ).stdout
    assert f"card-{card_id}" not in branches

    # Карточка в Backlog
    _, _, cols = _load_board(str(tmp_git))
    assert any(c["id"] == card_id for c in cols["backlog"]), "Карточка должна вернуться в Backlog"

    # meta обновлена
    updated = _read_run_meta(data_dir, card_id)
    assert updated["discarded"] is True


async def test_apply_legacy_returns_400(tmp_path):
    """apply для legacy-карточки → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    # Пишем legacy-мета
    _write_run_meta(data_dir, card_id, {
        "card_id": card_id,
        "mode": "legacy",
        "branch": None,
        "base_branch": None,
        "wt_path": None,
        "has_changes": True,
        "applied": False,
        "discarded": False,
    })

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 400, f"Ожидали 400, получили {resp.status}"


async def test_discard_legacy_returns_400(tmp_path):
    """discard для legacy-карточки → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_discard

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    _write_run_meta(data_dir, card_id, {
        "card_id": card_id,
        "mode": "legacy",
        "branch": None,
        "base_branch": None,
        "wt_path": None,
        "has_changes": False,
        "applied": False,
        "discarded": False,
    })

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/discard",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_discard(req)
    assert resp.status == 400


async def test_apply_bad_card_id(tmp_path):
    """apply с bad card_id → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/../../etc/passwd/apply",
        match_info={"id": pid, "card": "../../etc/passwd"},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 400


async def test_discard_bad_card_id(tmp_path):
    """discard с bad card_id → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_discard

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/bad!id/discard",
        match_info={"id": pid, "card": "bad!id"},
        app=app,
    )

    resp = await api_card_discard(req)
    assert resp.status == 400


async def test_apply_no_meta_returns_400(tmp_path):
    """apply без JSON-мета (нет файла) → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 400


# ─────────────────────────── _diff_from_worktree ───────────────────────────

async def test_diff_from_worktree_with_changes(tmp_git):
    """worktree с коммитом → diff_full содержит изменение, diff_stat непустой."""
    info = await _card_worktree_setup(str(tmp_git), "dddddd")
    assert info is not None
    wt_path = info["wt_path"]
    base_branch = info["base_branch"]

    # Пишем файл в worktree и коммитим
    (Path(wt_path) / "agent_output.py").write_text("result = 42\n")
    committed = await _commit_in_worktree(wt_path, "dddddd", "Add agent output")
    assert committed is True

    diff_full, diff_stat = await _diff_from_worktree(wt_path, base_branch)

    assert "agent_output.py" in diff_full, "diff_full должен содержать имя изменённого файла"
    assert diff_stat != "", "diff_stat не должен быть пустым при наличии изменений"


async def test_diff_from_worktree_no_changes(tmp_git):
    """worktree без изменений → diff пустой."""
    info = await _card_worktree_setup(str(tmp_git), "eeeeee")
    assert info is not None
    wt_path = info["wt_path"]
    base_branch = info["base_branch"]

    # Нет коммита — worktree идентичен base
    diff_full, diff_stat = await _diff_from_worktree(wt_path, base_branch)

    assert diff_full == "", "diff_full должен быть пустым без изменений"
    assert diff_stat == "", "diff_stat должен быть пустым без изменений"


async def test_diff_from_worktree_invalid_path():
    """Невалидный путь → возвращает ('', '') без исключения."""
    diff_full, diff_stat = await _diff_from_worktree("/nonexistent/path/xyz", "main")
    assert diff_full == "", "diff_full должен быть '' при невалидном пути"
    assert diff_stat == "", "diff_stat должен быть '' при невалидном пути"


# ─────────────────────────── e2e _run_card (worktree + legacy) ───────────────────────────

def _make_fake_card(card_id: str, text: str = "Test task") -> dict:
    return {"id": card_id, "text": text}


def _make_ctx_for_run_card(data_dir: Path, cwd: str, run_engine_factory) -> dict:
    """ctx достаточный для _run_card: include run_engine, save_sessions, sessions."""
    pid = Path(cwd.rstrip("/")).name
    return {
        "topics": {
            f"0:{pid}": {"cwd": cwd, "project": pid, "name": pid, "tg_thread": f"0:{pid}"},
        },
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": run_engine_factory,
        "ptb_app": None,
    }


async def test_run_card_worktree_isolation(tmp_git, tmp_path):
    """Интеграционный тест: агент пишет в worktree, НЕ в рабочее дерево проекта."""
    card_id = "ffffff"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cwd = str(tmp_git)

    # Создаём TASKS.md с карточкой в In Progress (откуда агент её берёт)
    _make_board(tmp_git, card_id, "review")

    # Подготавливаем worktree
    wt_info = await _card_worktree_setup(cwd, card_id)
    assert wt_info is not None, "worktree setup должен успешно создать worktree"
    wt_path = wt_info["wt_path"]

    # Мок run_engine: пишет файл в cwd, который ему передан, и завершается
    async def fake_run_engine(**kwargs):
        engine_cwd = kwargs.get("cwd", "")
        # Пишем файл в то cwd, которое передал _run_card — это должен быть wt_path
        (Path(engine_cwd) / "agent_created.py").write_text("x = 1\n")
        yield {"type": "text", "text": "Done"}
        yield {"type": "result", "session_id": "fake-sid-001"}

    project = {"name": "testrepo", "cwd": cwd, "model": "sonnet"}
    session_key = f"0:{Path(cwd).name}"

    ctx = _make_ctx_for_run_card(data_dir, cwd, fake_run_engine)
    ctx["running"][session_key] = True  # имитируем резерв замка

    await _run_card(ctx, None, project, _make_fake_card(card_id), session_key,
                    run_mode="worktree", wt_info=wt_info)

    # ИНВАРИАНТ 1: файл создан в worktree, НЕ в project cwd
    assert (Path(wt_path) / "agent_created.py").exists(), \
        "Файл агента должен быть в worktree"
    assert not (tmp_git / "agent_created.py").exists(), \
        "Файл агента НЕ должен быть в рабочем дереве проекта"

    # ИНВАРИАНТ 2: TRACKED-файлы в рабочем дереве проекта НЕ изменены
    # (untracked-файлы вроде TASKS.md или .worktrees/ — допустимы)
    diff_tracked = subprocess.run(
        ["git", "diff", "HEAD"], cwd=cwd, capture_output=True, text=True
    )
    assert diff_tracked.stdout.strip() == "", \
        f"Tracked-файлы рабочего дерева должны быть чистыми: {diff_tracked.stdout.strip()!r}"
    # И файл агента НЕ виден как untracked в tracked-дереве
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True
    )
    # agent_created.py не должен быть в статусе tracked-дерева
    assert "agent_created.py" not in status.stdout, \
        f"agent_created.py не должен быть в статусе рабочего дерева: {status.stdout!r}"

    # ИНВАРИАНТ 3: карточка ушла в Review, JSON-мета записана
    meta = _read_run_meta(data_dir, card_id)
    assert meta is not None, "JSON-мета должна быть записана"
    assert meta["mode"] == "worktree", f"mode должен быть 'worktree', получили {meta['mode']!r}"
    assert meta["has_changes"] is True, "has_changes должен быть True (агент создал файл)"

    # ИНВАРИАНТ 4: worktree НЕ удалён, ветка существует
    assert Path(wt_path).exists(), "Worktree НЕ должен быть удалён после прогона"
    branches = subprocess.run(
        ["git", "branch"], cwd=cwd, capture_output=True, text=True
    ).stdout
    assert f"card-{card_id}" in branches, f"Ветка card-{card_id} должна существовать"

    # ИНВАРИАНТ 5: замок снят
    assert session_key not in ctx["running"], "running[session_key] должен быть снят в finally"

    # ИНВАРИАНТ 6: карточка в Review (переехала из backlog)
    _, _, cols = _load_board(cwd)
    assert any(c["id"] == card_id for c in cols["review"]), \
        "Карточка должна быть в Review после успешного прогона"


async def test_run_card_legacy_mode(tmp_no_git, tmp_path):
    """Legacy-режим: файл создан в project cwd, мета mode=legacy."""
    card_id = "bbbbbb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cwd = str(tmp_no_git)

    # Создаём TASKS.md с карточкой
    _make_board(tmp_no_git, card_id, "review")

    async def fake_run_engine_legacy(**kwargs):
        engine_cwd = kwargs.get("cwd", "")
        (Path(engine_cwd) / "legacy_output.txt").write_text("legacy result\n")
        yield {"type": "text", "text": "Legacy done"}
        yield {"type": "result", "session_id": "fake-sid-002"}

    project = {"name": "norepo", "cwd": cwd, "model": "sonnet"}
    session_key = f"0:{Path(cwd).name}"

    ctx = _make_ctx_for_run_card(data_dir, cwd, fake_run_engine_legacy)
    ctx["running"][session_key] = True

    # В legacy-режиме wt_info=None
    await _run_card(ctx, None, project, _make_fake_card(card_id), session_key,
                    run_mode="legacy", wt_info=None)

    # Файл создан в project cwd
    assert (tmp_no_git / "legacy_output.txt").exists(), \
        "Файл агента должен быть в project cwd в legacy-режиме"

    # Мета записана с mode=legacy
    meta = _read_run_meta(data_dir, card_id)
    assert meta is not None, "JSON-мета должна быть записана"
    assert meta["mode"] == "legacy", f"mode должен быть 'legacy', получили {meta['mode']!r}"

    # Замок снят
    assert session_key not in ctx["running"], "running[session_key] должен быть снят в finally"

    # Карточка в Review
    _, _, cols = _load_board(cwd)
    assert any(c["id"] == card_id for c in cols["review"]), \
        "Карточка должна быть в Review после успешного прогона (legacy)"


# ─────────────────────────── утилита для тестов ───────────────────────────

def _project_id(cwd: str) -> str:
    return Path(cwd.rstrip("/")).name


def _make_ctx_with_project(data_dir: Path, cwd: str) -> dict:
    """Создаёт минимальный ctx с проектом по cwd, достаточный для api_card_apply/discard."""
    pid = _project_id(cwd)
    return {
        "topics": {
            f"0:{pid}": {"cwd": cwd, "project": pid, "name": pid, "tg_thread": f"0:{pid}"},
        },
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }

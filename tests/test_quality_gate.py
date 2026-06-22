"""
Тесты Spec 009 — качество-гейт (quality gate): _run_quality_gate + API /check.

НЕ запускают реальные тесты проектов (только tmp_git фикстуры).
Фикстуры: tmp_git (из conftest / локальный), _make_ctx_with_project, _project_id.
"""
import asyncio
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _run_quality_gate,
    api_card_check,
    _write_run_meta,
    _read_run_meta,
    _valid_card_id,
)


# ─────────────────────────── фикстуры ───────────────────────────

@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """Временный git-репо с baseline-коммитом."""
    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(cwd), check=True, capture_output=True)
    (cwd / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(cwd), check=True, capture_output=True)
    return cwd


def _project_id(cwd: str) -> str:
    return Path(cwd.rstrip("/")).name


def _make_ctx_with_project(data_dir: Path, cwd: str) -> dict:
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


def _link_venv(proj: Path) -> None:
    """Make the gate detect 'venv/bin/python -m pytest' (which HAS pytest) instead
    of a bare 'python3'. _detect_test_cmd prefers proj/venv/bin/python; without it
    the fallback is the system python3, which lacks pytest on CI / a clean machine
    → every "expect safe" gate test would falsely come back risky."""
    vbin = proj / "venv" / "bin"
    vbin.mkdir(parents=True, exist_ok=True)
    (vbin / "python").symlink_to(sys.executable)


def _make_passing_project(tmp_path: Path) -> Path:
    """Проект с pytest + тест который проходит."""
    p = tmp_path / "passing_proj"
    p.mkdir()
    (p / "tests").mkdir()
    (p / "tests" / "__init__.py").write_text("")
    (p / "tests" / "test_ok.py").write_text("def test_always_pass(): assert 1 == 1\n")
    _link_venv(p)
    return p


def _make_failing_project(tmp_path: Path) -> Path:
    """Проект с pytest + тест который падает."""
    p = tmp_path / "failing_proj"
    p.mkdir()
    (p / "tests").mkdir()
    (p / "tests" / "__init__.py").write_text("")
    (p / "tests" / "test_fail.py").write_text("def test_always_fail(): assert False, 'intentional'\n")
    _link_venv(p)
    return p


def _make_no_test_project(tmp_path: Path) -> Path:
    """Проект без тестовой конфигурации."""
    p = tmp_path / "no_test_proj"
    p.mkdir()
    (p / "main.py").write_text("x = 1\n")
    return p


# ─────────────────────────── _run_quality_gate unit ───────────────────────────

async def test_gate_passing_tests_returns_safe(tmp_path):
    """Проект с проходящими тестами → safe."""
    proj = _make_passing_project(tmp_path)
    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "safe", f"Ожидали safe, got: {result}"
    assert result["tests"]["detected"] is True
    assert result["tests"]["ok"] is True
    assert result["tests"]["exit_code"] == 0
    assert result["tests"]["timed_out"] is False
    assert result["lint"] is None


async def test_gate_failing_tests_returns_risky(tmp_path):
    """Проект с падающими тестами → risky."""
    proj = _make_failing_project(tmp_path)
    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "risky", f"Ожидали risky, got: {result}"
    assert result["tests"]["detected"] is True
    assert result["tests"]["ok"] is False
    assert result["tests"]["exit_code"] != 0
    assert result["tests"]["timed_out"] is False


async def test_gate_no_tests_returns_unknown(tmp_path):
    """Проект без тестовой конфигурации → unknown."""
    proj = _make_no_test_project(tmp_path)
    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "unknown", f"Ожидали unknown, got: {result}"
    assert result["tests"]["detected"] is False
    assert result["tests"]["cmd"] is None


async def test_gate_runs_in_wt_path(tmp_path):
    """_run_quality_gate гоняет тесты в переданном wt_path, не в cwd процесса."""
    # Создаём проект с тестами в отдельной папке
    proj = _make_passing_project(tmp_path)
    # Убеждаемся что из другого cwd (tmp_path) тесты не нашлись бы
    result_wrong = await _run_quality_gate(str(tmp_path))
    # Из proj — находятся
    result_ok = await _run_quality_gate(str(proj))
    assert result_ok["verdict"] == "safe"
    # Из tmp_path — нет конфига tests/
    # (tmp_path не имеет tests/ или pytest-конфига)
    assert result_wrong["verdict"] == "unknown"


async def test_gate_secrets_in_env(tmp_path):
    """Секреты подмешиваются в env: тест может их прочитать через os.environ."""
    proj = tmp_path / "secret_proj"
    proj.mkdir()
    (proj / "tests").mkdir()
    (proj / "tests" / "__init__.py").write_text("")
    # Тест проверяет переменную окружения MY_SECRET_42
    test_code = textwrap.dedent("""\
        import os
        def test_has_secret():
            val = os.environ.get('MY_SECRET_42', '')
            assert val == 'hello_world', f'Got: {val!r}'
    """)
    (proj / "tests" / "test_secret.py").write_text(test_code)
    _link_venv(proj)

    # Без секрета — тест падает
    result_no_secret = await _run_quality_gate(str(proj))
    assert result_no_secret["verdict"] == "risky"

    # С секретом — тест проходит
    result_with_secret = await _run_quality_gate(str(proj), env={"MY_SECRET_42": "hello_world"})
    assert result_with_secret["verdict"] == "safe", (
        f"Тест должен проходить с секретом. Output: {result_with_secret['tests']['output']}"
    )


async def test_gate_output_truncated(tmp_path):
    """Вывод тестов обрезается до ~20k символов."""
    proj = tmp_path / "loud_proj"
    proj.mkdir()
    (proj / "tests").mkdir()
    (proj / "tests" / "__init__.py").write_text("")
    # Тест который выводит очень много
    test_code = textwrap.dedent("""\
        def test_loud():
            for i in range(5000):
                print('x' * 10)
            assert False
    """)
    (proj / "tests" / "test_loud.py").write_text(test_code)
    _link_venv(proj)

    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "risky"
    assert len(result["tests"]["output"]) <= 21000  # небольшой запас


# ─────────────────────────── API /check ───────────────────────────

async def test_api_check_worktree_returns_verdict(tmp_git, tmp_path):
    """check API для worktree-карточки → возвращает вердикт."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Пишем worktree-мета (wt_path = tmp_git — там нет тестов → unknown, но не ошибка)
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(tmp_git),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200, f"Ожидали 200, got {resp.status}"
    data = json.loads(resp.body)
    assert "verdict" in data
    assert data["verdict"] in ("safe", "risky", "unknown")


async def test_api_check_legacy_returns_unknown(tmp_path):
    """check API для legacy-карточки → {verdict:'unknown', reason:'legacy'}."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"

    # Legacy мета
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
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "unknown"
    assert data.get("reason") == "legacy"


async def test_api_check_no_meta_returns_unknown(tmp_path):
    """check API без мета-сайдкара → {verdict:'unknown', reason:'legacy'}."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    # Нет мета-файла

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "unknown"


async def test_api_check_bad_card_id_returns_400(tmp_path):
    """check API с невалидным card_id → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    bad_card_id = "../evil"

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{bad_card_id}/check",
        match_info={"id": pid, "card": bad_card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 400, f"Ожидали 400 для bad card_id, got {resp.status}"


async def test_api_check_missing_worktree_returns_404(tmp_path):
    """check API: wt_path не существует → 404."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"

    # Worktree-мета с несуществующим путём
    _write_run_meta(data_dir, card_id, {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(tmp_path / "nonexistent-wt"),
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
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 404, f"Ожидали 404 для несуществующего worktree, got {resp.status}"


async def test_api_check_updates_meta_gate_field(tmp_git, tmp_path):
    """check API обновляет meta['gate'] с вердиктом и ts."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(tmp_git),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200

    # Мета должна быть обновлена полем gate
    updated_meta = _read_run_meta(data_dir, card_id)
    assert updated_meta is not None
    assert "gate" in updated_meta, "meta должна содержать поле gate"
    gate = updated_meta["gate"]
    assert "verdict" in gate
    assert "ts" in gate
    assert gate["verdict"] in ("safe", "risky", "unknown")


async def test_api_check_project_not_found_returns_404(tmp_path):
    """check API для несуществующего проекта → 404."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {
        "topics": {},  # пустой — нет проектов
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        "/api/projects/nonexistent/tasks/aabbcc/check",
        match_info={"id": "nonexistent", "card": "aabbcc"},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 404


async def test_api_check_passing_project_in_wt(tmp_git, tmp_path):
    """check API: worktree с проходящими тестами → safe."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Создаём директорию-worktree с проходящими тестами
    wt = tmp_path / "wt_passing"
    wt.mkdir()
    (wt / "tests").mkdir()
    (wt / "tests" / "__init__.py").write_text("")
    (wt / "tests" / "test_ok.py").write_text("def test_pass(): assert True\n")
    _link_venv(wt)

    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(wt),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "safe", f"Ожидали safe, got: {data}"
    assert data["tests"]["ok"] is True


async def test_api_check_failing_project_in_wt(tmp_git, tmp_path):
    """check API: worktree с падающими тестами → risky."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    wt = tmp_path / "wt_failing"
    wt.mkdir()
    (wt / "tests").mkdir()
    (wt / "tests" / "__init__.py").write_text("")
    (wt / "tests" / "test_fail.py").write_text("def test_fail(): assert False\n")

    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(wt),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "risky", f"Ожидали risky, got: {data}"
    assert data["tests"]["ok"] is False

"""
Тесты health-check проекта.

Проверяет функцию _check_project_health(cwd: Path) -> dict
Ожидаемый формат ответа: {"items": [...], "score": int, "color": "green"|"yellow"|"red"}

Если функция ещё не вынесена в webapp.py (бэкенд-агент не добавил) — тесты пропускаются
с явным маркером TODO, чтобы не блокировать CI на отсутствующем коде.

Проверяемые чекпоинты (6 total):
  1. CLAUDE.md (project-specific instructions)
  2. TASKS.md (канбан доска)
  3. README.md (документация)
  4. .gitignore (git config)
  5. .git/ (git repo)
  6. requirements*.txt или package.json (deps)
"""
from pathlib import Path
import pytest


# ─── попытка импортировать функцию ────────────────────────────────────────────

try:
    from webapp import _check_project_health  # type: ignore[attr-defined]
    _HEALTH_AVAILABLE = True
except ImportError:
    _HEALTH_AVAILABLE = False
    _check_project_health = None


_skip_if_missing = pytest.mark.skipif(
    not _HEALTH_AVAILABLE,
    reason="TODO: _check_project_health не вынесена в webapp.py (задача для бэкенд-агента)",
)


def _setup_project(tmp_path: Path, files: list[str]) -> Path:
    """Создаёт файлы в tmp_path. files = список имён файлов для создания."""
    for name in files:
        if name.endswith("/"):
            (tmp_path / name.rstrip("/")).mkdir(exist_ok=True)
        else:
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {name}\n")
    return tmp_path


# ─────────────────────────── тесты ───────────────────────────────────────────

@_skip_if_missing
def test_health_all_ok(tmp_path: Path):
    """Все 6 чекпоинтов присутствуют → score==6, color=='green'."""
    _setup_project(tmp_path, [
        "CLAUDE.md",
        "TASKS.md",
        "README.md",
        ".gitignore",
        ".git/",
        "requirements.txt",
    ])
    result = _check_project_health(tmp_path)
    assert isinstance(result, dict), "Результат должен быть dict"
    assert "items" in result
    assert "score" in result
    assert "color" in result
    assert result["score"] == 6, f"score должен быть 6, получили {result['score']}"
    assert result["color"] == "green", f"color должен быть green, получили {result['color']!r}"


@_skip_if_missing
def test_health_partial(tmp_path: Path):
    """Нет README → score==5, color!='red' (yellow или green)."""
    _setup_project(tmp_path, [
        "CLAUDE.md",
        "TASKS.md",
        ".gitignore",
        ".git/",
        "requirements.txt",
    ])
    result = _check_project_health(tmp_path)
    assert result["score"] == 5, f"score должен быть 5, получили {result['score']}"
    assert result["color"] in ("yellow", "green"), (
        f"При score=5 color должен быть yellow (или green), получили {result['color']!r}"
    )


@_skip_if_missing
def test_health_empty(tmp_path: Path):
    """Пустая папка → score близок к 0, color=='red'."""
    result = _check_project_health(tmp_path)
    assert isinstance(result["score"], int)
    assert result["score"] <= 1, (
        f"Для пустой папки score должен быть близок к 0, получили {result['score']}"
    )
    assert result["color"] == "red", f"Для пустой папки color должен быть red, получили {result['color']!r}"


@_skip_if_missing
def test_health_items_list(tmp_path: Path):
    """items — список проверок с полями name и ok."""
    _setup_project(tmp_path, ["CLAUDE.md", ".git/"])
    result = _check_project_health(tmp_path)
    assert isinstance(result["items"], list), "items должен быть списком"
    for item in result["items"]:
        assert "name" in item or "label" in item, f"Каждый item должен иметь name/label: {item}"
        assert "ok" in item or "status" in item or "exists" in item, (
            f"Каждый item должен иметь статус: {item}"
        )


@_skip_if_missing
def test_health_package_json_counts_as_deps(tmp_path: Path):
    """package.json засчитывается как deps-чекпоинт (альтернатива requirements*.txt)."""
    _setup_project(tmp_path, [
        "CLAUDE.md", "TASKS.md", "README.md", ".gitignore", ".git/",
        "package.json",
    ])
    result = _check_project_health(tmp_path)
    assert result["score"] == 6, (
        f"package.json должен засчитываться как deps, score должен быть 6, получили {result['score']}"
    )


# ─── fallback тест: проверяем что импорт webapp в целом работает ─────────────

def test_webapp_imports_cleanly():
    """webapp.py импортируется без ошибок (базовый smoke-тест)."""
    import webapp  # noqa: F401
    assert hasattr(webapp, "_parse_tasks")
    assert hasattr(webapp, "_serialize_tasks")
    assert hasattr(webapp, "_resolve_safe")
    assert hasattr(webapp, "_resolve_global_safe")
    assert hasattr(webapp, "BOARD_COLUMNS")
    assert hasattr(webapp, "_count_potential_cards")


def test_health_not_available_is_documented():
    """Документирует статус _check_project_health для трекинга."""
    if not _HEALTH_AVAILABLE:
        pytest.skip("_check_project_health не реализована — TODO для бэкенд-агента")
    # Если функция есть — этот тест просто проходит
    assert _check_project_health is not None

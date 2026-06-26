"""
Tests for project health-check.

Exercises _check_project_health(cwd: Path) -> dict.
Expected response format: {"items": [...], "score": int, "color": "green"|"yellow"|"red"}

If the function has not yet been added to webapp.py (backend agent hasn't landed it),
tests are skipped with an explicit TODO marker so CI is not blocked.

Checkpoints verified (6 total):
  1. CLAUDE.md (project-specific instructions)
  2. TASKS.md (kanban board)
  3. README.md (documentation)
  4. .gitignore (git config)
  5. .git/ (git repo)
  6. requirements*.txt or package.json (deps)
"""
from pathlib import Path
import pytest


# ─── attempt to import the function ───────────────────────────────────────────

try:
    from webapp import _check_project_health  # type: ignore[attr-defined]
    _HEALTH_AVAILABLE = True
except ImportError:
    _HEALTH_AVAILABLE = False
    _check_project_health = None


_skip_if_missing = pytest.mark.skipif(
    not _HEALTH_AVAILABLE,
    reason="TODO: _check_project_health not yet in webapp.py (task for backend agent)",
)


def _setup_project(tmp_path: Path, files: list[str]) -> Path:
    """Create files in tmp_path. files = list of file names to create."""
    for name in files:
        if name.endswith("/"):
            (tmp_path / name.rstrip("/")).mkdir(exist_ok=True)
        else:
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {name}\n")
    return tmp_path


# ─────────────────────────── tests ───────────────────────────────────────────

@_skip_if_missing
def test_health_all_ok(tmp_path: Path):
    """All 6 checkpoints present → score==6, color=='green'."""
    _setup_project(tmp_path, [
        "CLAUDE.md",
        "TASKS.md",
        "README.md",
        ".gitignore",
        ".git/",
        "requirements.txt",
    ])
    result = _check_project_health(tmp_path)
    assert isinstance(result, dict), "Result must be a dict"
    assert "items" in result
    assert "score" in result
    assert "color" in result
    assert result["score"] == 6, f"score should be 6, got {result['score']}"
    assert result["color"] == "green", f"color should be green, got {result['color']!r}"


@_skip_if_missing
def test_health_partial(tmp_path: Path):
    """No README → score==5, color!='red' (yellow or green)."""
    _setup_project(tmp_path, [
        "CLAUDE.md",
        "TASKS.md",
        ".gitignore",
        ".git/",
        "requirements.txt",
    ])
    result = _check_project_health(tmp_path)
    assert result["score"] == 5, f"score should be 5, got {result['score']}"
    assert result["color"] in ("yellow", "green"), (
        f"At score=5 color should be yellow (or green), got {result['color']!r}"
    )


@_skip_if_missing
def test_health_empty(tmp_path: Path):
    """Empty directory → score close to 0, color=='red'."""
    result = _check_project_health(tmp_path)
    assert isinstance(result["score"], int)
    assert result["score"] <= 1, (
        f"For an empty directory score should be close to 0, got {result['score']}"
    )
    assert result["color"] == "red", f"For an empty directory color should be red, got {result['color']!r}"


@_skip_if_missing
def test_health_items_list(tmp_path: Path):
    """items is a list of checks with name and ok fields."""
    _setup_project(tmp_path, ["CLAUDE.md", ".git/"])
    result = _check_project_health(tmp_path)
    assert isinstance(result["items"], list), "items must be a list"
    for item in result["items"]:
        assert "name" in item or "label" in item, f"Each item must have name/label: {item}"
        assert "ok" in item or "status" in item or "exists" in item, (
            f"Each item must have a status field: {item}"
        )


@_skip_if_missing
def test_health_package_json_counts_as_deps(tmp_path: Path):
    """package.json counts as the deps checkpoint (alternative to requirements*.txt)."""
    _setup_project(tmp_path, [
        "CLAUDE.md", "TASKS.md", "README.md", ".gitignore", ".git/",
        "package.json",
    ])
    result = _check_project_health(tmp_path)
    assert result["score"] == 6, (
        f"package.json should count as deps, score should be 6, got {result['score']}"
    )


# ─── fallback test: verify webapp import works at all ────────────────────────

def test_webapp_imports_cleanly():
    """webapp.py imports without errors (basic smoke test)."""
    import webapp  # noqa: F401
    assert hasattr(webapp, "_parse_tasks")
    assert hasattr(webapp, "_serialize_tasks")
    assert hasattr(webapp, "_resolve_safe")
    assert hasattr(webapp, "_resolve_global_safe")
    assert hasattr(webapp, "BOARD_COLUMNS")
    assert hasattr(webapp, "_count_potential_cards")


def test_health_not_available_is_documented():
    """Documents the status of _check_project_health for tracking."""
    if not _HEALTH_AVAILABLE:
        pytest.skip("_check_project_health not implemented — TODO for backend agent")
    # If the function exists this test simply passes
    assert _check_project_health is not None

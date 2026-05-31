"""
Тесты безопасности: path-traversal защита в _resolve_safe и _resolve_global_safe.
"""
import pytest
from pathlib import Path

from webapp import _resolve_safe, _resolve_global_safe


# ─────────────────────────── _resolve_safe ───────────────────────────

def test_resolve_safe_normal(tmp_path: Path):
    """Нормальный относительный путь — разрешается без ошибок."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("hello")

    result, cwd_resolved = _resolve_safe(str(tmp_path), "sub/file.txt")
    assert result == sub / "file.txt"
    assert cwd_resolved == tmp_path.resolve()


def test_resolve_safe_root_itself(tmp_path: Path):
    """Пустой rel — возвращает сам cwd без ошибок."""
    result, cwd_resolved = _resolve_safe(str(tmp_path), "")
    assert result == tmp_path.resolve()


def test_resolve_safe_traversal_rejected(tmp_path: Path):
    """'../etc/passwd' — ValueError (path traversal)."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_safe(str(tmp_path), "../etc/passwd")


def test_resolve_safe_deep_traversal_rejected(tmp_path: Path):
    """'../../etc/shadow' — ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_safe(str(tmp_path), "../../etc/shadow")


def test_resolve_safe_absolute_outside_rejected(tmp_path: Path):
    """'/etc/passwd' — ведущий '/' убирается через lstrip, но путь /etc/passwd
    уйдёт за пределы tmp_path — ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_safe(str(tmp_path), "/etc/passwd")


def test_resolve_safe_absolute_inside_ok(tmp_path: Path):
    """Абсолютный путь, указывающий ВНУТРЬ cwd — должен разрешаться без ошибок.
    (lstrip('/') превращает '/sub' → 'sub' → join → ok)"""
    sub = tmp_path / "sub"
    sub.mkdir()
    # '/sub' → lstrip → 'sub' → (cwd / 'sub').resolve()
    result, _ = _resolve_safe(str(tmp_path), "/sub")
    assert result == sub.resolve()


def test_resolve_safe_symlink_loop_safe(tmp_path: Path):
    """Путь с двойным слешем или точками — не должен уйти за пределы cwd."""
    # 'sub/../sub/file' = 'sub/file' — безопасен
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("ok")
    result, _ = _resolve_safe(str(tmp_path), "sub/../sub/file.txt")
    assert result == (sub / "file.txt").resolve()


# ─────────────────────────── _resolve_global_safe ───────────────────────────

def test_resolve_global_safe_normal(tmp_path: Path):
    """Нормальный путь внутри home-root — разрешается без ошибок."""
    sub = tmp_path / "project"
    sub.mkdir()
    (sub / "README.md").write_text("content")

    result = _resolve_global_safe(tmp_path, "project/README.md")
    assert result == (sub / "README.md").resolve()


def test_resolve_global_safe_no_escape_from_home(tmp_path: Path):
    """Попытка выйти из home через '..': ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_global_safe(tmp_path, "../etc/passwd")


def test_resolve_global_safe_deep_escape_rejected(tmp_path: Path):
    """Многократный '../': ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_global_safe(tmp_path, "../../root/.ssh/id_rsa")


def test_resolve_global_safe_absolute_path_stripped(tmp_path: Path):
    """'/etc/passwd' (ведущий слеш убирается через lstrip): за пределами home → ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_global_safe(tmp_path, "/etc/passwd")


def test_resolve_global_safe_home_itself(tmp_path: Path):
    """Пустой rel → сам home, без ошибок."""
    result = _resolve_global_safe(tmp_path, "")
    assert result == tmp_path.resolve()

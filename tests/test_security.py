"""
Security tests: path-traversal protection in _resolve_safe and _resolve_global_safe.
"""
import pytest
from pathlib import Path

from webapp import _resolve_safe, _resolve_global_safe


# ─────────────────────────── _resolve_safe ───────────────────────────

def test_resolve_safe_normal(tmp_path: Path):
    """A normal relative path resolves without error."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("hello")

    result, cwd_resolved = _resolve_safe(str(tmp_path), "sub/file.txt")
    assert result == sub / "file.txt"
    assert cwd_resolved == tmp_path.resolve()


def test_resolve_safe_root_itself(tmp_path: Path):
    """Empty rel path returns cwd itself without error."""
    result, cwd_resolved = _resolve_safe(str(tmp_path), "")
    assert result == tmp_path.resolve()


def test_resolve_safe_traversal_rejected(tmp_path: Path):
    """'../etc/passwd' raises ValueError (path traversal)."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_safe(str(tmp_path), "../etc/passwd")


def test_resolve_safe_deep_traversal_rejected(tmp_path: Path):
    """'../../etc/shadow' raises ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_safe(str(tmp_path), "../../etc/shadow")


def test_resolve_safe_absolute_normalized_into_cwd(tmp_path: Path):
    """'/etc/passwd' — lstrip('/') turns it into 'etc/passwd', which is INSIDE cwd.
    Safe: the real file does not exist so a 404 follows, not a leak."""
    result, cwd_resolved = _resolve_safe(str(tmp_path), "/etc/passwd")
    # target must be strictly inside cwd, not outside
    assert str(result).startswith(str(cwd_resolved))
    assert result == (tmp_path / "etc" / "passwd").resolve()


def test_resolve_safe_absolute_inside_ok(tmp_path: Path):
    """An absolute path pointing INSIDE cwd should resolve without error.
    (lstrip('/') turns '/sub' → 'sub' → join → ok)"""
    sub = tmp_path / "sub"
    sub.mkdir()
    # '/sub' → lstrip → 'sub' → (cwd / 'sub').resolve()
    result, _ = _resolve_safe(str(tmp_path), "/sub")
    assert result == sub.resolve()


def test_resolve_safe_symlink_loop_safe(tmp_path: Path):
    """A path with double slashes or dots must not escape cwd."""
    # 'sub/../sub/file' = 'sub/file' — safe
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("ok")
    result, _ = _resolve_safe(str(tmp_path), "sub/../sub/file.txt")
    assert result == (sub / "file.txt").resolve()


# ─────────────────────────── _resolve_global_safe ───────────────────────────

def test_resolve_global_safe_normal(tmp_path: Path):
    """A normal path inside home-root resolves without error."""
    sub = tmp_path / "project"
    sub.mkdir()
    (sub / "README.md").write_text("content")

    result = _resolve_global_safe(tmp_path, "project/README.md")
    assert result == (sub / "README.md").resolve()


def test_resolve_global_safe_no_escape_from_home(tmp_path: Path):
    """Attempt to escape home via '..': raises ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_global_safe(tmp_path, "../etc/passwd")


def test_resolve_global_safe_deep_escape_rejected(tmp_path: Path):
    """Multiple '../': raises ValueError."""
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_global_safe(tmp_path, "../../root/.ssh/id_rsa")


def test_resolve_global_safe_absolute_normalized_into_home(tmp_path: Path):
    """'/etc/passwd' — lstrip turns it into 'etc/passwd' inside home. Safe: does not exist → 404."""
    result = _resolve_global_safe(tmp_path, "/etc/passwd")
    assert str(result).startswith(str(tmp_path.resolve()))
    assert result == (tmp_path / "etc" / "passwd").resolve()


def test_resolve_global_safe_home_itself(tmp_path: Path):
    """Empty rel path returns home itself without error."""
    result = _resolve_global_safe(tmp_path, "")
    assert result == tmp_path.resolve()

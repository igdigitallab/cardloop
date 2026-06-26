"""
Tests for the janitor-quarantine script (server-janitor project).

All tests redirect the trash dir to a temp path via the JANITOR_TRASH env var
so they never touch the real ~/.janitor-trash.

The script path is resolved via the JANITOR_SCRIPT env var, or defaults to
a well-known relative location. Tests are skipped if the script is not found.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_SCRIPT_DEFAULT = Path("/home/youruser/server-janitor/janitor-quarantine")
SCRIPT = Path(os.environ.get("JANITOR_SCRIPT", str(_SCRIPT_DEFAULT)))

if not SCRIPT.exists():
    pytest.skip(
        f"janitor-quarantine script not found at {SCRIPT} — "
        "set JANITOR_SCRIPT env var to the correct path",
        allow_module_level=True,
    )


def _run(args: list[str], env_override: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Helper: run with a redirected trash dir
# ---------------------------------------------------------------------------

def _trash_env(tmp_path: Path) -> dict:
    """Return an env dict that redirects the trash to tmp_path/trash.

    Also sets HOME to tmp_path so the script's home-boundary check accepts
    files under tmp_path (tests create sources there).
    """
    return {
        "JANITOR_TRASH": str(tmp_path / "trash"),
        "HOME": str(tmp_path),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_quarantine_moves_file(tmp_path):
    """Quarantining a file moves it to trash, removes source, writes manifest."""
    src = tmp_path / "testfile.txt"
    src.write_text("hello")
    trash_env = _trash_env(tmp_path)

    result = _run(
        ["quarantine", str(src), "--reason", "unit test"],
        env_override=trash_env,
    )
    assert result.returncode == 0, result.stderr

    trash_root = tmp_path / "trash"
    today = datetime.now().strftime("%Y-%m-%d")
    dest = trash_root / today / "testfile.txt"

    assert dest.exists(), f"Expected {dest} to exist after quarantine"
    assert not src.exists(), "Source should be gone after quarantine"

    manifest = trash_root / today / "manifest.json"
    assert manifest.exists(), "manifest.json should be created"
    entries = json.loads(manifest.read_text())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["item"] == "testfile.txt"
    assert entry["from"] == str(src.resolve())
    assert entry["why"] == "unit test"
    assert "size" in entry
    assert "date" in entry


def test_quarantine_moves_directory(tmp_path):
    """Quarantining a directory tree moves the whole tree to trash."""
    src_dir = tmp_path / "mydir"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("aaa")
    (src_dir / "b.txt").write_text("bbb")
    trash_env = _trash_env(tmp_path)

    result = _run(
        ["quarantine", str(src_dir), "--reason", "dir test"],
        env_override=trash_env,
    )
    assert result.returncode == 0, result.stderr

    trash_root = tmp_path / "trash"
    today = datetime.now().strftime("%Y-%m-%d")
    dest = trash_root / today / "mydir"

    assert dest.is_dir(), "Quarantined directory should exist in trash"
    assert (dest / "a.txt").exists()
    assert not src_dir.exists(), "Source directory should be gone"

    manifest = trash_root / today / "manifest.json"
    entries = json.loads(manifest.read_text())
    assert any(e["item"] == "mydir" for e in entries)


def test_quarantine_missing_path_exits_nonzero(tmp_path):
    """Trying to quarantine a non-existent path should exit non-zero."""
    trash_env = _trash_env(tmp_path)
    result = _run(
        ["quarantine", "/nonexistent/path/that/does/not/exist", "--reason", "test"],
        env_override=trash_env,
    )
    assert result.returncode != 0


def test_quarantine_refuses_path_outside_home(tmp_path):
    """Paths outside $HOME should be refused (safety check)."""
    # /tmp is outside $HOME so the safety check should reject it
    outside = tmp_path / "outside_file.txt"
    outside.write_text("outside")
    trash_env = _trash_env(tmp_path)

    # Override HOME to a specific path so /tmp is definitively outside
    env = os.environ.copy()
    env.update(trash_env)
    env["HOME"] = "/home/youruser"  # ensure the check is against a non-/tmp path

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "quarantine", "/tmp/outside_safety_test", "--reason", "test"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, (
        "Script should refuse paths outside $HOME; got returncode 0\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_purge_removes_old_entries(tmp_path):
    """Entries older than --older-than days should be removed by purge."""
    trash_root = tmp_path / "trash"
    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d")
    old_dir = trash_root / old_date
    old_dir.mkdir(parents=True)
    (old_dir / "old_file.txt").write_text("old")
    # Write a minimal manifest
    manifest = old_dir / "manifest.json"
    manifest.write_text(json.dumps([{"item": "old_file.txt", "from": "/tmp/test-project/old_file.txt", "why": "test", "size": "4B", "date": "2026-01-01T00:00:00+00:00"}]))

    trash_env = _trash_env(tmp_path)
    result = _run(["purge", "--older-than", "30"], env_override=trash_env)
    assert result.returncode == 0, result.stderr
    assert not old_dir.exists(), f"Old dir {old_dir} should have been purged"


def test_purge_keeps_recent_entries(tmp_path):
    """Entries newer than --older-than days should NOT be removed by purge."""
    trash_root = tmp_path / "trash"
    recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    recent_dir = trash_root / recent_date
    recent_dir.mkdir(parents=True)
    (recent_dir / "recent_file.txt").write_text("recent")

    trash_env = _trash_env(tmp_path)
    result = _run(["purge", "--older-than", "30"], env_override=trash_env)
    assert result.returncode == 0, result.stderr
    assert recent_dir.exists(), f"Recent dir {recent_dir} should NOT have been purged"


def test_list_output(tmp_path):
    """After quarantining a file, 'list' should show the original path."""
    src = tmp_path / "listed_file.txt"
    src.write_text("list me")
    trash_env = _trash_env(tmp_path)

    _run(
        ["quarantine", str(src), "--reason", "list test"],
        env_override=trash_env,
    )

    result = _run(["list"], env_override=trash_env)
    assert result.returncode == 0, result.stderr
    assert str(src.resolve()) in result.stdout, (
        f"Expected original path {src.resolve()} in list output:\n{result.stdout}"
    )


def test_restore_roundtrip(tmp_path):
    """Quarantine a file then restore it; it should be back at the original path."""
    src = tmp_path / "restore_me.txt"
    src.write_text("restore test content")
    original_path = str(src.resolve())
    trash_env = _trash_env(tmp_path)

    # Quarantine
    result = _run(
        ["quarantine", str(src), "--reason", "restore test"],
        env_override=trash_env,
    )
    assert result.returncode == 0, result.stderr
    assert not src.exists(), "File should be in trash after quarantine"

    # Restore
    today = datetime.now().strftime("%Y-%m-%d")
    spec = f"{today}/restore_me.txt"
    result = _run(["restore", spec], env_override=trash_env)
    assert result.returncode == 0, f"Restore failed:\n{result.stdout}\n{result.stderr}"
    assert src.exists(), f"File should be back at {src} after restore"
    assert src.read_text() == "restore test content"

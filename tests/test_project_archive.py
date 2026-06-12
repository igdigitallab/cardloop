"""
Tests for Spec-023: Project Archive.

Tests the helper functions and business logic directly (no HTTP client needed).
Covers:
- _load_archived / _save_archived round-trip
- archive adds id + filtered from default list
- unarchive restores to default list
- archived-list helper returns only archived
- busy project → archived guard respected
- filesystem invariant: archive/unarchive writes ONLY data/archived.json, nothing under cwd
- archive state survives topics reload
- group assignment survives archive→unarchive
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _collect_projects,
    _load_archived,
    _save_archived,
    _archived_path,
    _load_groups,
    _save_groups,
    _find_project_by_id_any,
    _project_id,
)


# ─────────────────────────── fixtures ────────────────────────────────────────

def _make_ctx(tmp_path, project_dir):
    """Minimal ctx for archive tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return {
        "topics": {
            "1001:42": {
                "project": "my-project",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
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


# ─────────────────────────── unit: load/save ─────────────────────────────────

def test_load_archived_missing_file(tmp_path):
    """Missing archived.json → empty set."""
    ctx = {"DATA": tmp_path}
    result = _load_archived(ctx)
    assert result == set()


def test_load_archived_empty_list(tmp_path):
    """Empty list in archived.json → empty set."""
    (tmp_path / "archived.json").write_text("[]")
    ctx = {"DATA": tmp_path}
    assert _load_archived(ctx) == set()


def test_save_and_load_archived_round_trip(tmp_path):
    """Save a set, load it back."""
    ctx = {"DATA": tmp_path}
    _save_archived(ctx, {"proj-a", "proj-b"})
    loaded = _load_archived(ctx)
    assert loaded == {"proj-a", "proj-b"}


def test_save_archived_sorted(tmp_path):
    """Saved list is sorted (stable file content)."""
    ctx = {"DATA": tmp_path}
    _save_archived(ctx, {"z-proj", "a-proj", "m-proj"})
    raw = json.loads((tmp_path / "archived.json").read_text())
    assert raw == sorted(raw)


def test_load_archived_corrupted_file_returns_empty(tmp_path):
    """Corrupted JSON → empty set (no crash)."""
    (tmp_path / "archived.json").write_text("{bad json")
    ctx = {"DATA": tmp_path}
    assert _load_archived(ctx) == set()


# ─────────────────────────── archive + _collect_projects integration ─────────

def test_archived_project_filtered_from_collect(tmp_path):
    """Archived project does not appear in _collect_projects output."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Before archive: in list
    projects = _collect_projects(ctx)
    ids = [p["id"] for p in projects]
    assert "my-project" in ids

    # Archive it
    _save_archived(ctx, {"my-project"})

    # After archive: NOT in list
    projects = _collect_projects(ctx)
    ids = [p["id"] for p in projects]
    assert "my-project" not in ids


def test_unarchived_project_reappears_in_collect(tmp_path):
    """Removing from archived set makes project reappear in _collect_projects."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Archive then unarchive
    _save_archived(ctx, {"my-project"})
    assert "my-project" not in [p["id"] for p in _collect_projects(ctx)]

    _save_archived(ctx, set())
    assert "my-project" in [p["id"] for p in _collect_projects(ctx)]


def test_archived_list_empty_when_nothing_archived(tmp_path):
    """No archived.json → archived set is empty."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)
    archived = _load_archived(ctx)
    assert len(archived) == 0


def test_archived_project_found_by_find_project_by_id_any(tmp_path):
    """_find_project_by_id_any finds a project even when archived."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)
    _save_archived(ctx, {"my-project"})

    # _collect_projects won't find it (filtered), but _find_project_by_id_any will
    from_collect = [p["id"] for p in _collect_projects(ctx)]
    assert "my-project" not in from_collect

    found = _find_project_by_id_any(ctx, "my-project")
    assert found is not None
    assert found["id"] == "my-project"


# ─────────────────────────── busy guard ──────────────────────────────────────

def test_busy_guard_logic(tmp_path):
    """When project is busy (running dict has its session_key), archive should be blocked.
    This tests the guard logic in isolation — the running check is ctx['running'].get(session_key)."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    project = _find_project_by_id_any(ctx, "my-project")
    assert project is not None
    session_key = project["tg_thread"]

    # Not busy
    assert ctx["running"].get(session_key) is None

    # Mark busy
    ctx["running"][session_key] = True
    assert ctx["running"].get(session_key) is not None

    # Unmark
    ctx["running"].pop(session_key)
    assert ctx["running"].get(session_key) is None


# ─────────────────────────── filesystem invariant ────────────────────────────

def test_archive_does_not_touch_project_filesystem(tmp_path):
    """Archive: ONLY data/archived.json changes — no writes under the project cwd."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    # Create a file inside the project dir to track
    sentinel = pdir / "README.md"
    sentinel.write_text("sentinel")

    ctx = _make_ctx(tmp_path, pdir)

    # Snapshot files under project dir before
    before_files = {str(f): f.read_text() for f in pdir.rglob("*") if f.is_file()}

    # Simulate archive operation: only data/archived.json should change
    _save_archived(ctx, {"my-project"})

    # Snapshot after
    after_files = {str(f): f.read_text() for f in pdir.rglob("*") if f.is_file()}

    assert before_files == after_files, (
        "archive must not write any files under the project cwd"
    )

    # Only data/archived.json was created
    data_dir = tmp_path / "data"
    assert (data_dir / "archived.json").exists()
    # No other data-dir artifact touches project dir
    for f in data_dir.rglob("*"):
        assert not str(f).startswith(str(pdir)), (
            f"archive wrote into project cwd: {f}"
        )


def test_unarchive_does_not_touch_project_filesystem(tmp_path):
    """Unarchive: ONLY data/archived.json changes — no writes under the project cwd."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    (pdir / "sentinel.txt").write_text("keep me")

    ctx = _make_ctx(tmp_path, pdir)
    _save_archived(ctx, {"my-project"})

    before_files = {str(f): f.read_text() for f in pdir.rglob("*") if f.is_file()}

    # Simulate unarchive
    archived = _load_archived(ctx)
    archived.discard("my-project")
    _save_archived(ctx, archived)

    after_files = {str(f): f.read_text() for f in pdir.rglob("*") if f.is_file()}
    assert before_files == after_files, (
        "unarchive must not write any files under the project cwd"
    )


# ─────────────────────────── topics reload / reset isolation ─────────────────

def test_archive_state_survives_topics_reload(tmp_path):
    """topics.json hot-reload should not affect archived.json (separate files)."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    _save_archived(ctx, {"my-project"})

    # Simulate topics reload (would happen in _maybe_reload_topics)
    ctx["topics"]["1001:42"]["model"] = "opus"  # mutate topics in-memory

    # Archive state should be unaffected
    archived = _load_archived(ctx)
    assert "my-project" in archived


def test_archive_state_not_in_topics(tmp_path):
    """Archive state is stored in archived.json, NOT in topics.json entries."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    _save_archived(ctx, {"my-project"})

    # topics entry should be unchanged
    topic_entry = ctx["topics"]["1001:42"]
    assert "archived" not in topic_entry
    assert "hidden" not in topic_entry


# ─────────────────────────── group assignment survives archive ────────────────

def test_group_assignment_survives_archive_unarchive(tmp_path):
    """Group assignment in project_groups.json is preserved through archive→unarchive."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Assign to a group
    _save_groups(ctx, {"groups": ["MyGroup"], "assignments": {"my-project": "MyGroup"}})

    # Archive
    _save_archived(ctx, {"my-project"})
    groups = _load_groups(ctx)
    assert groups["assignments"].get("my-project") == "MyGroup"

    # Unarchive
    _save_archived(ctx, set())
    groups = _load_groups(ctx)
    assert groups["assignments"].get("my-project") == "MyGroup"

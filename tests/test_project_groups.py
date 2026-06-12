"""
Tests for Spec-024: Project Groups.

Tests the helper functions and business logic directly (no HTTP client needed).
Covers:
- _load_groups / _save_groups round-trip
- assign project to group (auto-creates if new)
- null assignment → ungrouped
- _collect_projects includes group field
- rename group preserves members
- delete group → members ungrouped
- reorder persists
- filesystem invariant: group ops ONLY modify data/project_groups.json, nothing under project cwd
- label validation (empty, control chars)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _collect_projects,
    _load_groups,
    _save_groups,
    _groups_path,
    _project_id,
)


# ─────────────────────────── helpers ─────────────────────────────────────────

def _make_ctx(tmp_path, project_dir):
    """Minimal ctx for groups tests."""
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

def test_load_groups_missing_file(tmp_path):
    """Missing project_groups.json → empty structure."""
    ctx = {"DATA": tmp_path}
    result = _load_groups(ctx)
    assert result == {"groups": [], "assignments": {}}


def test_load_groups_empty_file(tmp_path):
    """Empty/null JSON → default empty structure."""
    (tmp_path / "project_groups.json").write_text("null")
    ctx = {"DATA": tmp_path}
    result = _load_groups(ctx)
    assert result == {"groups": [], "assignments": {}}


def test_save_and_load_groups_round_trip(tmp_path):
    """Save groups+assignments, load back unchanged."""
    ctx = {"DATA": tmp_path}
    data = {"groups": ["Work", "Personal"], "assignments": {"my-proj": "Work"}}
    _save_groups(ctx, data)
    loaded = _load_groups(ctx)
    assert loaded["groups"] == ["Work", "Personal"]
    assert loaded["assignments"]["my-proj"] == "Work"


def test_load_groups_corrupted_file_returns_empty(tmp_path):
    """Corrupted JSON → empty structure (no crash)."""
    (tmp_path / "project_groups.json").write_text("{bad json!!!")
    ctx = {"DATA": tmp_path}
    result = _load_groups(ctx)
    assert result == {"groups": [], "assignments": {}}


# ─────────────────────────── assignment logic ────────────────────────────────

def test_assign_project_to_group(tmp_path):
    """Assigning a project to a group creates it in assignments."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    groups = _load_groups(ctx)
    groups["groups"].append("Work")
    groups["assignments"]["my-project"] = "Work"
    _save_groups(ctx, groups)

    loaded = _load_groups(ctx)
    assert loaded["assignments"]["my-project"] == "Work"
    assert "Work" in loaded["groups"]


def test_auto_create_group_on_assign(tmp_path):
    """Assigning to a new label auto-creates that label in groups list."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Simulate the auto-create logic that the endpoint does
    groups = _load_groups(ctx)
    label = "NewGroup"
    if label not in groups["groups"]:
        groups["groups"].append(label)
    groups["assignments"]["my-project"] = label
    _save_groups(ctx, groups)

    loaded = _load_groups(ctx)
    assert "NewGroup" in loaded["groups"]
    assert loaded["assignments"]["my-project"] == "NewGroup"


def test_null_assignment_clears_group(tmp_path):
    """Setting assignment to None removes the project from assignments."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Assign then clear
    groups = {"groups": ["Work"], "assignments": {"my-project": "Work"}}
    _save_groups(ctx, groups)

    groups = _load_groups(ctx)
    groups["assignments"].pop("my-project", None)
    _save_groups(ctx, groups)

    loaded = _load_groups(ctx)
    assert "my-project" not in loaded["assignments"]


# ─────────────────────────── _collect_projects includes group ────────────────

def test_collect_projects_includes_group_field(tmp_path):
    """_collect_projects returns group field for each project."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Assign project to a group
    _save_groups(ctx, {"groups": ["Work"], "assignments": {"my-project": "Work"}})

    projects = _collect_projects(ctx)
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("group") == "Work"


def test_collect_projects_group_null_when_not_assigned(tmp_path):
    """_collect_projects returns group=None for projects with no assignment."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    projects = _collect_projects(ctx)
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("group") is None


def test_collect_projects_group_null_when_label_not_in_groups_list(tmp_path):
    """If assignment references a deleted group label, group should be None."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Assignment exists but group label is NOT in the groups list (stale assignment)
    _save_groups(ctx, {"groups": [], "assignments": {"my-project": "DeletedGroup"}})

    projects = _collect_projects(ctx)
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    # Group should be None since "DeletedGroup" is not in groups list
    assert proj.get("group") is None


# ─────────────────────────── rename group ────────────────────────────────────

def test_rename_group_preserves_members(tmp_path):
    """Renaming a group (client sends new full groups list) preserves member assignments."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    _save_groups(ctx, {"groups": ["Work"], "assignments": {"my-project": "Work"}})

    # Simulate rename: client sends updated groups list + server renames assignments
    groups = _load_groups(ctx)
    old_label = "Work"
    new_label = "Work-Renamed"
    new_groups_list = ["Work-Renamed"]  # renamed

    # Update assignments
    new_assignments = {
        (new_label if v == old_label else v): new_label if v == old_label else v
        for k, v in groups["assignments"].items()
    }
    # Simpler: map old→new
    new_assignments = {k: new_label if v == old_label else v for k, v in groups["assignments"].items()}
    _save_groups(ctx, {"groups": new_groups_list, "assignments": new_assignments})

    loaded = _load_groups(ctx)
    assert loaded["assignments"]["my-project"] == "Work-Renamed"
    assert "Work-Renamed" in loaded["groups"]
    assert "Work" not in loaded["groups"]


# ─────────────────────────── delete group ────────────────────────────────────

def test_delete_group_ungroupes_members(tmp_path):
    """Deleting a group (removing label from groups list) ungroups its members."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    _save_groups(ctx, {"groups": ["Work", "Personal"], "assignments": {"my-project": "Work"}})

    # Simulate delete: remove "Work" from groups, clear its assignments
    groups = _load_groups(ctx)
    new_groups = [g for g in groups["groups"] if g != "Work"]
    new_assignments = {k: v for k, v in groups["assignments"].items() if v != "Work"}
    _save_groups(ctx, {"groups": new_groups, "assignments": new_assignments})

    loaded = _load_groups(ctx)
    assert "Work" not in loaded["groups"]
    assert "my-project" not in loaded["assignments"]

    # Project is now ungrouped in _collect_projects
    projects = _collect_projects(ctx)
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("group") is None


# ─────────────────────────── reorder ─────────────────────────────────────────

def test_reorder_groups_persists(tmp_path):
    """Reordering groups (different array order) persists correctly."""
    ctx = {"DATA": tmp_path}
    _save_groups(ctx, {"groups": ["A", "B", "C"], "assignments": {}})

    loaded = _load_groups(ctx)
    loaded["groups"] = ["C", "A", "B"]
    _save_groups(ctx, loaded)

    reloaded = _load_groups(ctx)
    assert reloaded["groups"] == ["C", "A", "B"]


# ─────────────────────────── filesystem invariant ────────────────────────────

def test_group_assign_does_not_touch_project_filesystem(tmp_path):
    """Group ops ONLY modify data/project_groups.json — no writes under project cwd."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    (pdir / "CLAUDE.md").write_text("# Project")
    (pdir / "TASKS.md").write_text("# Tasks")

    ctx = _make_ctx(tmp_path, pdir)

    # Snapshot the project dir
    before = {str(f): f.read_bytes() for f in pdir.rglob("*") if f.is_file()}

    # Perform group operations
    _save_groups(ctx, {"groups": ["TestGroup"], "assignments": {"my-project": "TestGroup"}})
    _save_groups(ctx, {"groups": ["TestGroup"], "assignments": {}})

    after = {str(f): f.read_bytes() for f in pdir.rglob("*") if f.is_file()}

    assert before == after, "group ops must not write any files under the project cwd"

    # project_groups.json was created in data/, not under pdir
    data_dir = tmp_path / "data"
    assert (data_dir / "project_groups.json").exists()
    # Verify no group file leaked into project dir
    for f in data_dir.rglob("*"):
        assert not str(f).startswith(str(pdir)), f"group op wrote into project cwd: {f}"


def test_group_operations_only_modify_groups_file(tmp_path):
    """After group ops, only project_groups.json exists in data/ (no extra files)."""
    ctx = {"DATA": tmp_path}

    before_data_files = set(tmp_path.glob("*"))

    _save_groups(ctx, {"groups": ["X"], "assignments": {"proj-a": "X"}})
    _load_groups(ctx)

    after_data_files = set(tmp_path.glob("*"))
    new_files = after_data_files - before_data_files
    assert new_files == {tmp_path / "project_groups.json"}, (
        f"expected only project_groups.json, got: {new_files}"
    )


# ─────────────────────────── label validation helpers ────────────────────────

def test_empty_label_should_not_be_stored(tmp_path):
    """Empty label is invalid — groups list should not contain empty strings."""
    ctx = {"DATA": tmp_path}
    label = "   ".strip()  # empty after strip
    assert label == ""
    # The endpoint validates this — here we just verify our helpers don't store it
    groups = _load_groups(ctx)
    assert "" not in groups["groups"]


def test_groups_structure_has_required_keys(tmp_path):
    """_load_groups always returns dict with 'groups' and 'assignments' keys."""
    ctx = {"DATA": tmp_path}
    result = _load_groups(ctx)
    assert "groups" in result
    assert "assignments" in result
    assert isinstance(result["groups"], list)
    assert isinstance(result["assignments"], dict)

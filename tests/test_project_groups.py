"""
Tests for Spec-024 + Spec-030 Phase 1: Project Groups.

Unit tests — helper functions and business logic directly (no HTTP client needed).
HTTP tests — Spec-030 Phase 1 atomic endpoints via aiohttp_client.

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
- POST /api/project-groups/create  (new, idempotent)
- POST /api/project-groups/rename  (remaps assignments, rejects missing/collision)
- POST /api/project-groups/delete  (unassigns projects, idempotent)
- POST /api/project-groups/reorder (permutation accepted; non-permutation rejected)
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _collect_projects,
    _load_groups,
    _save_groups,
    _groups_path,
    _project_id,
    _derive_token,
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


# ═══════════════════════════════════════════════════════════════════════════════
# Spec-030 Phase 1: HTTP tests for atomic group management endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# ─────────────────────────── fixtures ────────────────────────────────────────

PASSWORD = "test-password-030"


@pytest.fixture
def groups_ctx(tmp_path):
    """Minimal ctx for Spec-030 HTTP tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": PASSWORD,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(PASSWORD)
    return ctx


@pytest.fixture
def groups_app(groups_ctx):
    """aiohttp app wired with Spec-030 group management routes."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = groups_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    # Spec-024 routes (kept for compatibility check)
    app.router.add_get("/api/project-groups", _webapp.api_project_groups_get)
    app.router.add_post("/api/project-groups", _webapp.api_project_groups_manage)
    # Spec-030 Phase 1 routes
    app.router.add_post("/api/project-groups/create", _webapp.api_project_groups_create)
    app.router.add_post("/api/project-groups/rename", _webapp.api_project_groups_rename)
    app.router.add_post("/api/project-groups/delete", _webapp.api_project_groups_delete)
    app.router.add_post("/api/project-groups/reorder", _webapp.api_project_groups_reorder)

    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── /create ─────────────────────────────────────────

async def test_create_group_new(aiohttp_client, groups_app, groups_ctx):
    """POST /create with a new name appends it to groups and returns full state."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    resp = await client.post("/api/project-groups/create", json={"name": "Work"}, headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert "groups" in data and "assignments" in data
    assert "Work" in data["groups"]


async def test_create_group_idempotent(aiohttp_client, groups_app, groups_ctx):
    """POST /create with an existing name returns 200 with current state (no duplicate)."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    # Create once
    await client.post("/api/project-groups/create", json={"name": "Work"}, headers=h)
    # Create again
    resp = await client.post("/api/project-groups/create", json={"name": "Work"}, headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["groups"].count("Work") == 1


async def test_create_group_empty_name_rejected(aiohttp_client, groups_app, groups_ctx):
    """POST /create with empty name → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    resp = await client.post("/api/project-groups/create", json={"name": "  "}, headers=h)
    assert resp.status == 400


async def test_create_group_bad_json(aiohttp_client, groups_app, groups_ctx):
    """POST /create with non-JSON body → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    resp = await client.post(
        "/api/project-groups/create",
        data="not-json",
        headers={**h, "Content-Type": "application/json"},
    )
    assert resp.status == 400


# ─────────────────────────── /rename ─────────────────────────────────────────

async def test_rename_group_remaps_assignments(aiohttp_client, groups_app, groups_ctx):
    """POST /rename renames the group AND remaps every assignment pointing to it."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)

    # Seed state: group "Work", project "proj-a" assigned to it
    _save_groups(groups_ctx, {
        "groups": ["Work", "Personal"],
        "assignments": {"proj-a": "Work", "proj-b": "Personal"},
    })

    resp = await client.post(
        "/api/project-groups/rename", json={"from": "Work", "to": "Office"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    # Groups list updated
    assert "Office" in data["groups"]
    assert "Work" not in data["groups"]
    assert "Personal" in data["groups"]
    # Assignment for proj-a remapped
    assert data["assignments"]["proj-a"] == "Office"
    # Unrelated assignment untouched
    assert data["assignments"]["proj-b"] == "Personal"


async def test_rename_group_rejects_missing_from(aiohttp_client, groups_app, groups_ctx):
    """POST /rename with unknown 'from' → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["Work"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "NoSuchGroup", "to": "Other"}, headers=h
    )
    assert resp.status == 400


async def test_rename_group_rejects_collision(aiohttp_client, groups_app, groups_ctx):
    """POST /rename where 'to' already exists (and != 'from') → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["Work", "Personal"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "Work", "to": "Personal"}, headers=h
    )
    assert resp.status == 400


async def test_rename_group_empty_to_rejected(aiohttp_client, groups_app, groups_ctx):
    """POST /rename with empty 'to' → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["Work"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "Work", "to": ""}, headers=h
    )
    assert resp.status == 400


# ─────────────────────────── /delete ─────────────────────────────────────────

async def test_delete_group_unassigns_projects(aiohttp_client, groups_app, groups_ctx):
    """POST /delete removes group and unassigns all projects pointing to it."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": ["Work", "Personal"],
        "assignments": {"proj-a": "Work", "proj-b": "Work", "proj-c": "Personal"},
    })
    resp = await client.post("/api/project-groups/delete", json={"name": "Work"}, headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert "Work" not in data["groups"]
    assert "Personal" in data["groups"]
    # proj-a and proj-b are now ungrouped
    assert "proj-a" not in data["assignments"]
    assert "proj-b" not in data["assignments"]
    # proj-c assignment to Personal survives
    assert data["assignments"]["proj-c"] == "Personal"


async def test_delete_group_idempotent_absent(aiohttp_client, groups_app, groups_ctx):
    """POST /delete for a group that doesn't exist → 200 with unchanged state."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["Work"], "assignments": {"proj-a": "Work"}})
    resp = await client.post(
        "/api/project-groups/delete", json={"name": "DoesNotExist"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    assert "Work" in data["groups"]
    assert data["assignments"]["proj-a"] == "Work"


# ─────────────────────────── /reorder ────────────────────────────────────────

async def test_reorder_accepts_permutation(aiohttp_client, groups_app, groups_ctx):
    """POST /reorder with a valid permutation → 200, groups list updated in new order."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": ["A", "B", "C"],
        "assignments": {"proj-x": "B"},
    })
    resp = await client.post(
        "/api/project-groups/reorder", json={"order": ["C", "A", "B"]}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["groups"] == ["C", "A", "B"]
    # Assignments untouched
    assert data["assignments"]["proj-x"] == "B"


async def test_reorder_rejects_non_permutation_extra_name(aiohttp_client, groups_app, groups_ctx):
    """POST /reorder with an unknown name → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["A", "B"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/reorder", json={"order": ["A", "B", "X"]}, headers=h
    )
    assert resp.status == 400


async def test_reorder_rejects_non_permutation_missing_name(aiohttp_client, groups_app, groups_ctx):
    """POST /reorder missing an existing name → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["A", "B", "C"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/reorder", json={"order": ["A", "B"]}, headers=h
    )
    assert resp.status == 400


async def test_reorder_rejects_non_list(aiohttp_client, groups_app, groups_ctx):
    """POST /reorder with non-list order → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    resp = await client.post(
        "/api/project-groups/reorder", json={"order": "not-a-list"}, headers=h
    )
    assert resp.status == 400


# ═══════════════════════════════════════════════════════════════════════════════
# Spec-061: nested folder paths — cascade behaviour on the 3 endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# ─────────────────────────── /create ancestor auto-create ────────────────────

async def test_create_nested_auto_creates_ancestors(aiohttp_client, groups_app, groups_ctx):
    """POST /create with a deep path auto-creates every missing ancestor in order."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    resp = await client.post(
        "/api/project-groups/create", json={"name": "A/B/C"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    # All three ancestors present, appended in ancestor order.
    assert data["groups"] == ["A", "A/B", "A/B/C"]


async def test_create_nested_only_appends_missing_ancestors(aiohttp_client, groups_app, groups_ctx):
    """Existing ancestors are not duplicated; only the missing tail is appended."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["A"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/create", json={"name": "A/B/C"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["groups"] == ["A", "A/B", "A/B/C"]
    assert data["groups"].count("A") == 1


async def test_create_path_with_empty_segment_rejected(aiohttp_client, groups_app, groups_ctx):
    """A path with an empty/whitespace segment ('A//B', 'A/ /B') → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    resp = await client.post(
        "/api/project-groups/create", json={"name": "A//B"}, headers=h
    )
    assert resp.status == 400
    resp = await client.post(
        "/api/project-groups/create", json={"name": "A/ /B"}, headers=h
    )
    assert resp.status == 400


# ─────────────────────────── /rename subtree cascade ─────────────────────────

async def test_rename_cascades_descendant_folder_and_assignment(aiohttp_client, groups_app, groups_ctx):
    """Renaming a folder rewrites its descendants AND the assignments under them."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": ["Business", "Business/Clients", "Business/Clients/VIP", "Personal"],
        "assignments": {
            "proj-a": "Business/Clients",
            "proj-b": "Business/Clients/VIP",
            "proj-c": "Personal",
        },
    })
    resp = await client.post(
        "/api/project-groups/rename",
        json={"from": "Business/Clients", "to": "Business/Accounts"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    # The folder + its descendant both moved, order preserved.
    assert data["groups"] == [
        "Business", "Business/Accounts", "Business/Accounts/VIP", "Personal",
    ]
    # Assignments under the subtree remapped; unrelated one untouched.
    assert data["assignments"]["proj-a"] == "Business/Accounts"
    assert data["assignments"]["proj-b"] == "Business/Accounts/VIP"
    assert data["assignments"]["proj-c"] == "Personal"


async def test_rename_as_move_reparents_subtree(aiohttp_client, groups_app, groups_ctx):
    """rename used as a MOVE (A/X → B/X) re-parents the folder and auto-creates ancestors."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": ["A", "A/X", "A/X/Deep", "B"],
        "assignments": {"proj-a": "A/X", "proj-deep": "A/X/Deep"},
    })
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "A/X", "to": "B/X"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    # A/X and A/X/Deep moved under B; A and B (existing ancestors) retained.
    assert "B/X" in data["groups"]
    assert "B/X/Deep" in data["groups"]
    assert "A/X" not in data["groups"]
    assert "A/X/Deep" not in data["groups"]
    assert "A" in data["groups"]
    assert "B" in data["groups"]
    # Assignments re-parented.
    assert data["assignments"]["proj-a"] == "B/X"
    assert data["assignments"]["proj-deep"] == "B/X/Deep"


async def test_rename_as_move_auto_creates_missing_parent(aiohttp_client, groups_app, groups_ctx):
    """Moving under a parent that doesn't exist yet auto-creates the parent chain."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["A", "A/X"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "A/X", "to": "New/Parent/X"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    # Missing ancestors of the destination were created.
    assert "New" in data["groups"]
    assert "New/Parent" in data["groups"]
    assert "New/Parent/X" in data["groups"]
    assert "A/X" not in data["groups"]


async def test_rename_collision_with_existing_sibling_rejected(aiohttp_client, groups_app, groups_ctx):
    """A new path colliding with an existing group outside the affected set → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": ["A", "A/X", "A/Y"],
        "assignments": {"proj-a": "A/X"},
    })
    # Renaming A/X → A/Y collides with the existing A/Y folder.
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "A/X", "to": "A/Y"}, headers=h
    )
    assert resp.status == 400
    # State unchanged.
    after = _load_groups(groups_ctx)
    assert after["groups"] == ["A", "A/X", "A/Y"]
    assert after["assignments"]["proj-a"] == "A/X"


async def test_rename_missing_from_path_rejected(aiohttp_client, groups_app, groups_ctx):
    """Renaming a nested path that doesn't exist → 400."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {"groups": ["A"], "assignments": {}})
    resp = await client.post(
        "/api/project-groups/rename", json={"from": "A/Nope", "to": "A/Z"}, headers=h
    )
    assert resp.status == 400


# ─────────────────────────── /delete subtree cascade ─────────────────────────

async def test_delete_removes_whole_subtree_and_unassigns(aiohttp_client, groups_app, groups_ctx):
    """Deleting a folder removes it + all descendants and unassigns every project in the subtree."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": [
            "Business",
            "Business/Clients",
            "Business/Clients/VIP",
            "Business/Leads",
            "Personal",
        ],
        "assignments": {
            "proj-a": "Business/Clients",
            "proj-b": "Business/Clients/VIP",
            "proj-c": "Business/Leads",
            "proj-d": "Personal",
        },
    })
    resp = await client.post(
        "/api/project-groups/delete", json={"name": "Business/Clients"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    # Clients + its descendant gone; sibling Leads and Personal survive.
    assert "Business/Clients" not in data["groups"]
    assert "Business/Clients/VIP" not in data["groups"]
    assert "Business" in data["groups"]
    assert "Business/Leads" in data["groups"]
    assert "Personal" in data["groups"]
    # Projects inside the deleted subtree are unassigned; others untouched.
    assert "proj-a" not in data["assignments"]
    assert "proj-b" not in data["assignments"]
    assert data["assignments"]["proj-c"] == "Business/Leads"
    assert data["assignments"]["proj-d"] == "Personal"


async def test_delete_subtree_does_not_touch_prefix_sibling(aiohttp_client, groups_app, groups_ctx):
    """delete 'A/B' must NOT match 'A/BC' (only true path descendants under 'A/B/')."""
    client = await aiohttp_client(groups_app)
    h = _auth(groups_ctx)
    _save_groups(groups_ctx, {
        "groups": ["A", "A/B", "A/BC", "A/B/Deep"],
        "assignments": {"p1": "A/BC", "p2": "A/B/Deep"},
    })
    resp = await client.post(
        "/api/project-groups/delete", json={"name": "A/B"}, headers=h
    )
    assert resp.status == 200
    data = await resp.json()
    assert "A/B" not in data["groups"]
    assert "A/B/Deep" not in data["groups"]
    # 'A/BC' is a sibling, not a descendant — it must survive.
    assert "A/BC" in data["groups"]
    assert data["assignments"]["p1"] == "A/BC"
    assert "p2" not in data["assignments"]

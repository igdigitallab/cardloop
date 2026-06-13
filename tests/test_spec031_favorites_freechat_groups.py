"""
Tests for Spec-031: Favorites + free-chat groups.

Unit tests only (no aiohttp_client) — mirrors the pattern from test_project_groups.py.

Covers:
- _load_favorites / _save_favorites round-trip and defaults
- Favorite toggle persists and surfaces as favorite=True in _collect_projects
- Unfavoriting removes from set
- Free chat assigned to a group surfaces with that group in _collect_projects
- A free chat without a group has group=None in _collect_projects
- All projects (real + free) include the favorite field
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _collect_projects,
    _load_favorites,
    _save_favorites,
    _load_groups,
    _save_groups,
    _load_free_chats,
    _save_free_chats,
    _project_id,
)


# ─────────────────────────── helpers ─────────────────────────────────────────

def _make_ctx(tmp_path, project_dir=None, topics=None):
    """Minimal ctx for spec-031 tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    ctx = {
        "topics": topics or {},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    if project_dir:
        pid = _project_id(str(project_dir))
        ctx["topics"] = {
            "1001:42": {
                "project": pid,
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        }
    return ctx


# ─────────────────────────── load/save favorites ─────────────────────────────

def test_load_favorites_missing_file(tmp_path):
    """Missing project_favorites.json → empty structure."""
    ctx = {"DATA": tmp_path}
    result = _load_favorites(ctx)
    assert result == {"favorites": []}


def test_load_favorites_empty_structure(tmp_path):
    """Null/empty JSON → empty structure."""
    (tmp_path / "project_favorites.json").write_text("null")
    ctx = {"DATA": tmp_path}
    assert _load_favorites(ctx) == {"favorites": []}


def test_load_favorites_corrupted_returns_empty(tmp_path):
    """Corrupted JSON → empty structure (no crash)."""
    (tmp_path / "project_favorites.json").write_text("{bad json!!!")
    ctx = {"DATA": tmp_path}
    assert _load_favorites(ctx) == {"favorites": []}


def test_save_and_load_favorites_round_trip(tmp_path):
    """Save favorites, load back unchanged."""
    ctx = {"DATA": tmp_path}
    data = {"favorites": ["proj-a", "free-abc123"]}
    _save_favorites(ctx, data)
    loaded = _load_favorites(ctx)
    assert loaded["favorites"] == ["proj-a", "free-abc123"]


def test_favorites_structure_has_required_keys(tmp_path):
    """_load_favorites always returns dict with 'favorites' key that is a list."""
    ctx = {"DATA": tmp_path}
    result = _load_favorites(ctx)
    assert "favorites" in result
    assert isinstance(result["favorites"], list)


# ─────────────────────────── collect_projects: favorite field ────────────────

def test_collect_projects_favorite_false_by_default(tmp_path):
    """Projects not in favorites have favorite=False in _collect_projects."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    projects = _collect_projects(ctx)
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("favorite") is False


def test_collect_projects_favorite_true_when_starred(tmp_path):
    """Project in favorites set surfaces as favorite=True in _collect_projects."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Save favorite
    _save_favorites(ctx, {"favorites": ["my-project"]})

    projects = _collect_projects(ctx)
    proj = next((p for p in projects if p["id"] == "my-project"), None)
    assert proj is not None
    assert proj.get("favorite") is True


def test_collect_projects_unfavorite_removes_flag(tmp_path):
    """After removing from favorites, project has favorite=False again."""
    pdir = tmp_path / "my-project"
    pdir.mkdir()
    ctx = _make_ctx(tmp_path, pdir)

    # Add then remove
    _save_favorites(ctx, {"favorites": ["my-project"]})
    projects = _collect_projects(ctx)
    assert next(p for p in projects if p["id"] == "my-project")["favorite"] is True

    _save_favorites(ctx, {"favorites": []})
    projects = _collect_projects(ctx)
    assert next(p for p in projects if p["id"] == "my-project")["favorite"] is False


def test_collect_projects_favorite_field_present_for_all(tmp_path):
    """Every project dict includes the 'favorite' key."""
    pdir1 = tmp_path / "proj-a"
    pdir1.mkdir()
    pdir2 = tmp_path / "proj-b"
    pdir2.mkdir()
    ctx = _make_ctx(tmp_path, topics={
        "1001:1": {"project": "proj-a", "cwd": str(pdir1), "model": "sonnet"},
        "1001:2": {"project": "proj-b", "cwd": str(pdir2), "model": "sonnet"},
    })

    projects = _collect_projects(ctx)
    for p in projects:
        assert "favorite" in p, f"Project {p['id']} is missing 'favorite' key"


# ─────────────────────────── free chat: group field ──────────────────────────

def _write_free_chat(ctx, fid, label="Test Chat"):
    """Helper to write a free chat entry."""
    free = _load_free_chats(ctx)
    free[fid] = {"label": label, "cwd": str(Path.home()), "model": "sonnet", "created_at": 0}
    _save_free_chats(ctx, free)


def test_free_chat_group_none_when_not_assigned(tmp_path):
    """Free chat without group assignment has group=None in _collect_projects."""
    ctx = _make_ctx(tmp_path)
    _write_free_chat(ctx, "free-abc12345")

    projects = _collect_projects(ctx)
    fc = next((p for p in projects if p["id"] == "free-abc12345"), None)
    assert fc is not None
    assert fc.get("group") is None


def test_free_chat_group_populated_when_assigned(tmp_path):
    """Free chat assigned to a valid group surfaces with that group."""
    ctx = _make_ctx(tmp_path)
    _write_free_chat(ctx, "free-abc12345")

    # Assign the free chat to a group
    _save_groups(ctx, {
        "groups": ["Work"],
        "assignments": {"free-abc12345": "Work"},
    })

    projects = _collect_projects(ctx)
    fc = next((p for p in projects if p["id"] == "free-abc12345"), None)
    assert fc is not None
    assert fc.get("group") == "Work"


def test_free_chat_group_null_when_group_not_in_list(tmp_path):
    """Free chat assigned to a deleted group (stale assignment) has group=None."""
    ctx = _make_ctx(tmp_path)
    _write_free_chat(ctx, "free-abc12345")

    # Assignment exists but group is NOT in valid groups list
    _save_groups(ctx, {
        "groups": [],  # empty: "DeletedGroup" removed
        "assignments": {"free-abc12345": "DeletedGroup"},
    })

    projects = _collect_projects(ctx)
    fc = next((p for p in projects if p["id"] == "free-abc12345"), None)
    assert fc is not None
    assert fc.get("group") is None


def test_free_chat_favorite_round_trip(tmp_path):
    """Free chat can be favorited and surfaces as favorite=True in _collect_projects."""
    ctx = _make_ctx(tmp_path)
    _write_free_chat(ctx, "free-abc12345")

    # Initially not favorited
    projects = _collect_projects(ctx)
    fc = next((p for p in projects if p["id"] == "free-abc12345"), None)
    assert fc is not None
    assert fc.get("favorite") is False

    # Favorite it
    _save_favorites(ctx, {"favorites": ["free-abc12345"]})
    projects = _collect_projects(ctx)
    fc = next((p for p in projects if p["id"] == "free-abc12345"), None)
    assert fc.get("favorite") is True


def test_free_chat_favorite_field_present(tmp_path):
    """Free chat dict always includes 'favorite' key."""
    ctx = _make_ctx(tmp_path)
    _write_free_chat(ctx, "free-abc12345")

    projects = _collect_projects(ctx)
    fc = next((p for p in projects if p["id"] == "free-abc12345"), None)
    assert fc is not None
    assert "favorite" in fc

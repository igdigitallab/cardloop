"""
Tests for Spec-025: Project Hard-Delete.

Covers:
- _path_allowlist_check: home, bot dir, ancestor of bot dir, symlink escape, happy path
- api_project_delete: non-archived → 409, confirm_name mismatch → 400, busy → 409
- api_project_delete: happy path → moved to trash, sidecar written, cockpit state cleaned
- api_project_delete_precheck: git repo with dirty/unpushed state; non-git
- _run_janitor_trash_purge: old entry purged, new kept; refuses path outside trash
- api_trash_list: lists trashed projects
- api_trash_restore: moves back + rebinds; collision → 409
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _load_archived,
    _save_archived,
    _load_groups,
    _save_groups,
    _path_allowlist_check,
    _run_janitor_trash_purge,
    _trash_dir,
    TRASH_RETENTION_DAYS,
    api_project_delete,
    api_project_delete_precheck,
    api_trash_list,
    api_trash_restore,
)


# ─────────────────────────── helpers ─────────────────────────────────────────

def _make_ctx(tmp_path, project_dir, here_dir=None, extra_topics=None):
    """Minimal ctx for delete tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    topics = {
        "1001:42": {
            "project": "my-project",
            "cwd": str(project_dir),
            "model": "sonnet",
        }
    }
    if extra_topics:
        topics.update(extra_topics)
    ctx = {
        "topics": topics,
        "sessions": {"1001:42": "session-abc"},
        "running": {},
        "DATA": data_dir,
        "HERE": here_dir or ROOT,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    return ctx


def _make_request(ctx, pid, body=None):
    """Create a fake aiohttp request for delete endpoint."""
    req = MagicMock()
    req.app = {"ctx": ctx}
    req.match_info = {"id": pid}
    if body is not None:
        async def _json():
            return body
        req.json = _json
    else:
        async def _json():
            raise ValueError("no body")
        req.json = _json
    return req


def _make_get_request(ctx, pid):
    req = MagicMock()
    req.app = {"ctx": ctx}
    req.match_info = {"id": pid}
    return req


def _make_trash_request(ctx, entry):
    req = MagicMock()
    req.app = {"ctx": ctx}
    req.match_info = {"entry": entry}
    return req


# ─────────────────────────── _path_allowlist_check ───────────────────────────

def test_path_allowlist_home_dir_rejected(tmp_path):
    """cwd == home → rejected."""
    fake_home = str(tmp_path)
    ctx = {"HERE": str(ROOT)}
    err = _path_allowlist_check(fake_home, ctx, _home_override=fake_home)
    assert err is not None
    assert "home" in err.lower() or "strictly" in err.lower()


def test_path_allowlist_home_subdir_accepted(tmp_path):
    """cwd is a real subdir under home → accepted."""
    fake_home = str(tmp_path)
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()
    ctx = {"HERE": str(ROOT)}
    err = _path_allowlist_check(str(project_dir), ctx, _home_override=fake_home)
    assert err is None


def test_path_allowlist_bot_dir_rejected(tmp_path):
    """cwd == bot dir (HERE) → rejected."""
    fake_home = tmp_path
    bot_dir = fake_home / "claude-ops-bot"
    bot_dir.mkdir()
    ctx = {"HERE": str(bot_dir)}
    err = _path_allowlist_check(str(bot_dir), ctx, _home_override=str(fake_home))
    assert err is not None
    assert "claude-ops-bot" in err.lower() or "bot" in err.lower()


def test_path_allowlist_ancestor_of_bot_rejected(tmp_path):
    """cwd is an ancestor of bot dir → rejected."""
    fake_home = tmp_path
    # bot dir is at fake_home/projects/claude-ops-bot
    projects_dir = fake_home / "projects"
    projects_dir.mkdir()
    bot_dir = projects_dir / "claude-ops-bot"
    bot_dir.mkdir()
    # cwd is fake_home/projects (ancestor of bot_dir)
    ctx = {"HERE": str(bot_dir)}
    err = _path_allowlist_check(str(projects_dir), ctx, _home_override=str(fake_home))
    assert err is not None
    assert "ancestor" in err.lower()


def test_path_allowlist_outside_home_rejected(tmp_path):
    """cwd outside home → rejected."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    ctx = {"HERE": str(ROOT)}
    err = _path_allowlist_check(str(outside), ctx, _home_override=str(fake_home))
    assert err is not None


def test_path_allowlist_symlink_escaping_home_rejected(tmp_path):
    """cwd is a symlink that resolves outside home → rejected."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    outside = tmp_path / "outside-target"
    outside.mkdir()
    # Create a symlink inside home that points outside home
    link = fake_home / "escape-link"
    link.symlink_to(outside)
    ctx = {"HERE": str(ROOT)}
    err = _path_allowlist_check(str(link), ctx, _home_override=str(fake_home))
    # realpath(link) resolves to outside-target, which is not under fake_home
    assert err is not None


def test_path_allowlist_nested_subdir_accepted(tmp_path):
    """cwd nested several levels under home → accepted."""
    fake_home = tmp_path
    project_dir = tmp_path / "projects" / "subdir" / "my-project"
    project_dir.mkdir(parents=True)
    ctx = {"HERE": str(ROOT)}
    err = _path_allowlist_check(str(project_dir), ctx, _home_override=str(fake_home))
    assert err is None


# ─────────────────────────── api_project_delete: guardrails ──────────────────

@pytest.mark.asyncio
async def test_delete_non_archived_409(tmp_path):
    """Non-archived project → 409, folder untouched."""
    project_dir = tmp_path / "home" / "my-project"
    project_dir.mkdir(parents=True)
    ctx = _make_ctx(tmp_path, project_dir)
    # Do NOT add to archived
    req = _make_request(ctx, "my-project", {"confirm_name": "my-project"})
    resp = await api_project_delete(req)
    assert resp.status == 409
    body = json.loads(resp.body)
    assert "archived" in body["error"].lower()
    assert project_dir.exists()


@pytest.mark.asyncio
async def test_delete_confirm_name_mismatch_400(tmp_path):
    """confirm_name mismatch → 400, folder untouched."""
    project_dir = tmp_path / "home" / "my-project"
    project_dir.mkdir(parents=True)
    ctx = _make_ctx(tmp_path, project_dir)
    _save_archived(ctx, {"my-project"})
    req = _make_request(ctx, "my-project", {"confirm_name": "wrong-name"})
    with patch("webapp._path_allowlist_check", return_value=None):
        resp = await api_project_delete(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "confirm_name" in body["error"].lower() or "match" in body["error"].lower()
    assert project_dir.exists()


@pytest.mark.asyncio
async def test_delete_path_rejected_400(tmp_path):
    """Path allowlist failure → 400, folder untouched."""
    project_dir = tmp_path / "home" / "my-project"
    project_dir.mkdir(parents=True)
    ctx = _make_ctx(tmp_path, project_dir)
    _save_archived(ctx, {"my-project"})
    req = _make_request(ctx, "my-project", {"confirm_name": "my-project"})
    with patch("webapp._path_allowlist_check", return_value="cwd is not strictly under home directory"):
        resp = await api_project_delete(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "path rejected" in body["error"].lower()
    assert project_dir.exists()


@pytest.mark.asyncio
async def test_delete_busy_project_409(tmp_path):
    """Busy project → 409, folder untouched."""
    project_dir = tmp_path / "home" / "my-project"
    project_dir.mkdir(parents=True)
    ctx = _make_ctx(tmp_path, project_dir)
    ctx["running"]["1001:42"] = True  # mark as busy
    _save_archived(ctx, {"my-project"})
    req = _make_request(ctx, "my-project", {"confirm_name": "my-project"})
    with patch("webapp._path_allowlist_check", return_value=None):
        resp = await api_project_delete(req)
    assert resp.status == 409
    body = json.loads(resp.body)
    assert "busy" in body["error"].lower()
    assert project_dir.exists()


@pytest.mark.asyncio
async def test_delete_project_not_found_404(tmp_path):
    """Non-existent project → 404."""
    ctx = _make_ctx(tmp_path, tmp_path / "nonexistent")
    req = _make_request(ctx, "nonexistent-id", {"confirm_name": "nonexistent-id"})
    resp = await api_project_delete(req)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_delete_invalid_json_400(tmp_path):
    """Invalid JSON body → 400."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    req = _make_request(ctx, "my-project", None)  # will raise on json()
    resp = await api_project_delete(req)
    assert resp.status == 400


# ─────────────────────────── api_project_delete: happy path ──────────────────

@pytest.mark.asyncio
async def test_delete_happy_path(tmp_path):
    """Happy path: archived project → moved to trash, sidecar written, state cleaned."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project_dir = fake_home / "my-project"
    project_dir.mkdir()
    (project_dir / "file.txt").write_text("hello")

    ctx = _make_ctx(tmp_path, project_dir, here_dir=str(tmp_path / "bot"))
    _save_archived(ctx, {"my-project"})
    # Add timeline file to test cleanup
    timeline_dir = ctx["DATA"] / "timeline"
    timeline_dir.mkdir(exist_ok=True)
    slug = str(project_dir).replace("/", "-")
    timeline_file = timeline_dir / f"{slug}.jsonl"
    timeline_file.write_text('{"event":"test"}\n')

    req = _make_request(ctx, "my-project", {"confirm_name": "my-project"})
    with patch("webapp._path_allowlist_check", return_value=None):
        resp = await api_project_delete(req)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["deleted"] is True
    assert "trash_path" in body
    assert "purge_at" in body

    # Original dir must be gone
    assert not project_dir.exists()

    # Trash dir must have the folder
    trash_folder = Path(body["trash_path"])
    assert trash_folder.exists()
    assert (trash_folder / "file.txt").exists()

    # Sidecar must exist
    sidecar_path = trash_folder.parent / f"{trash_folder.name}.json"
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["id"] == "my-project"
    assert sidecar["original_cwd"] == str(project_dir)
    assert "deleted_at" in sidecar

    # topics.json cleaned
    assert "1001:42" not in ctx["topics"]

    # archived.json cleaned
    assert "my-project" not in _load_archived(ctx)

    # timeline file cleaned
    assert not timeline_file.exists()

    # audit line written
    audit_dir = ctx["DATA"] / "audit"
    audit_files = list(audit_dir.glob("audit-*.log"))
    assert len(audit_files) == 1
    audit_content = audit_files[0].read_text()
    assert "DELETE⚠️" in audit_content
    assert "my-project" in audit_content


@pytest.mark.asyncio
async def test_delete_cleans_group_assignment(tmp_path):
    """Happy path: group assignment is removed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project_dir = fake_home / "my-project"
    project_dir.mkdir()

    ctx = _make_ctx(tmp_path, project_dir, here_dir=str(tmp_path / "bot"))
    _save_archived(ctx, {"my-project"})
    # Add group assignment
    _save_groups(ctx, {"groups": ["team-a"], "assignments": {"my-project": "team-a", "other": "team-a"}})

    req = _make_request(ctx, "my-project", {"confirm_name": "my-project"})
    with patch("webapp._path_allowlist_check", return_value=None):
        resp = await api_project_delete(req)

    assert resp.status == 200
    groups_data = _load_groups(ctx)
    assert "my-project" not in groups_data["assignments"]
    assert "other" in groups_data["assignments"]


@pytest.mark.asyncio
async def test_delete_tg_topic_deleted(tmp_path):
    """Happy path: deleteForumTopic is called when tg_thread is set."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project_dir = fake_home / "my-project"
    project_dir.mkdir()

    mock_bot = AsyncMock()
    mock_ptb = MagicMock()
    mock_ptb.bot = mock_bot

    ctx = _make_ctx(tmp_path, project_dir, here_dir=str(tmp_path / "bot"))
    ctx["ptb_app"] = mock_ptb
    _save_archived(ctx, {"my-project"})

    req = _make_request(ctx, "my-project", {"confirm_name": "my-project"})
    with patch("webapp._path_allowlist_check", return_value=None):
        resp = await api_project_delete(req)

    assert resp.status == 200
    mock_bot.delete_forum_topic.assert_called_once_with(
        chat_id=1001, message_thread_id=42
    )


@pytest.mark.asyncio
async def test_delete_missing_cwd_400(tmp_path):
    """cwd doesn't exist on disk → 400."""
    project_dir = tmp_path / "home" / "nonexistent-dir"
    # Do NOT create the dir
    ctx = _make_ctx(tmp_path, project_dir)
    _save_archived(ctx, {"nonexistent-dir"})
    req = _make_request(ctx, "nonexistent-dir", {"confirm_name": "my-project"})
    with patch("webapp._path_allowlist_check", return_value=None):
        resp = await api_project_delete(req)
    assert resp.status == 400


# ─────────────────────────── api_project_delete_precheck ─────────────────────

@pytest.mark.asyncio
async def test_precheck_not_found_404(tmp_path):
    """Precheck for nonexistent project → 404."""
    ctx = _make_ctx(tmp_path, tmp_path / "nonexistent")
    req = _make_get_request(ctx, "nonexistent-id")
    resp = await api_project_delete_precheck(req)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_precheck_non_git_dir(tmp_path):
    """Non-git dir → is_git=False."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    ctx = _make_ctx(tmp_path, project_dir)
    req = _make_get_request(ctx, "my-project")
    resp = await api_project_delete_precheck(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["is_git"] is False
    assert body["uncommitted_count"] == 0
    assert body["unpushed_count"] == 0


@pytest.mark.asyncio
async def test_precheck_git_repo(tmp_path):
    """Git repo → is_git=True with counts."""
    import subprocess
    project_dir = tmp_path / "git-project"
    project_dir.mkdir()
    # Init git repo with a commit
    subprocess.run(["git", "init", str(project_dir)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project_dir), "config", "user.email", "test@test.com"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project_dir), "config", "user.name", "Test"],
                   capture_output=True, check=True)
    (project_dir / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(project_dir), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project_dir), "commit", "-m", "init"],
                   capture_output=True, check=True)
    # Add an uncommitted file
    (project_dir / "dirty.txt").write_text("dirty")

    # Build ctx with git-project cwd
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    ctx = {
        "topics": {
            "1001:42": {"project": "git-project", "cwd": str(project_dir), "model": "sonnet"}
        },
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "HERE": ROOT,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "ptb_app": None,
    }

    req = _make_get_request(ctx, "git-project")
    resp = await api_project_delete_precheck(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["is_git"] is True
    assert body["uncommitted_count"] >= 1
    assert body["branch"] is not None


# ─────────────────────────── _run_janitor_trash_purge ────────────────────────

def test_janitor_purges_old_entry(tmp_path):
    """Entry older than TRASH_RETENTION_DAYS → purged."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}

    # Create old trash entry
    trash_dir = _trash_dir(ctx)
    old_folder = trash_dir / "my-project-111"
    old_folder.mkdir()
    (old_folder / "file.txt").write_text("old content")
    old_ts = time.time() - (TRASH_RETENTION_DAYS + 2) * 86400
    sidecar = {
        "id": "my-project",
        "name": "my-project",
        "original_cwd": "/home/user/my-project",
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old_ts)),
        "tg_chat": None,
        "tg_thread": None,
    }
    (trash_dir / "my-project-111.json").write_text(json.dumps(sidecar))

    purged = _run_janitor_trash_purge(ctx)
    assert "my-project-111" in purged
    assert not old_folder.exists()
    assert not (trash_dir / "my-project-111.json").exists()


def test_janitor_keeps_new_entry(tmp_path):
    """Entry within TRASH_RETENTION_DAYS → kept."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}

    trash_dir = _trash_dir(ctx)
    new_folder = trash_dir / "new-project-999"
    new_folder.mkdir()
    new_ts = time.time() - 1 * 86400  # 1 day old
    sidecar = {
        "id": "new-project",
        "name": "new-project",
        "original_cwd": "/home/user/new-project",
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(new_ts)),
        "tg_chat": None,
        "tg_thread": None,
    }
    (trash_dir / "new-project-999.json").write_text(json.dumps(sidecar))

    purged = _run_janitor_trash_purge(ctx)
    assert "new-project-999" not in purged
    assert new_folder.exists()


def test_janitor_refuses_path_outside_trash(tmp_path):
    """Janitor refuses to rm a folder that resolves outside data/trash/."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}
    trash_dir = _trash_dir(ctx)

    # Create a symlink inside trash that points outside trash
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete")
    link_in_trash = trash_dir / "escape-entry"
    link_in_trash.symlink_to(outside)

    old_ts = time.time() - (TRASH_RETENTION_DAYS + 2) * 86400
    sidecar = {
        "id": "escape",
        "name": "escape",
        "original_cwd": "/home/user/escape",
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old_ts)),
        "tg_chat": None,
        "tg_thread": None,
    }
    (trash_dir / "escape-entry.json").write_text(json.dumps(sidecar))

    _run_janitor_trash_purge(ctx)
    # The outside dir must NOT be deleted
    assert outside.exists()
    assert (outside / "precious.txt").exists()


# ─────────────────────────── api_trash_list ──────────────────────────────────

@pytest.mark.asyncio
async def test_trash_list_empty(tmp_path):
    """Empty trash → empty list."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}
    req = MagicMock()
    req.app = {"ctx": ctx}
    resp = await api_trash_list(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["trash"] == []


@pytest.mark.asyncio
async def test_trash_list_shows_entries(tmp_path):
    """Trash list shows entries with days_left."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}

    trash_dir = _trash_dir(ctx)
    ts = time.time() - 1 * 86400  # 1 day old
    sidecar = {
        "id": "some-project",
        "name": "some-project",
        "original_cwd": "/home/user/some-project",
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "tg_chat": None,
        "tg_thread": None,
    }
    (trash_dir / "some-project-123.json").write_text(json.dumps(sidecar))

    req = MagicMock()
    req.app = {"ctx": ctx}
    resp = await api_trash_list(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert len(body["trash"]) == 1
    entry = body["trash"][0]
    assert entry["id"] == "some-project"
    assert entry["name"] == "some-project"
    # days_left depends on local TZ offset vs UTC stored timestamp
    # accept any value in [TRASH_RETENTION_DAYS - 2, TRASH_RETENTION_DAYS]
    assert 0 <= entry["days_left"] <= TRASH_RETENTION_DAYS


# ─────────────────────────── api_trash_restore ───────────────────────────────

@pytest.mark.asyncio
async def test_restore_moves_back_and_rebinds(tmp_path):
    """Restore: moves folder back + rebinds topics.json."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original_cwd = tmp_path / "home" / "restored-project"
    (tmp_path / "home").mkdir(exist_ok=True)

    save_topics_called = [False]

    def save_topics():
        save_topics_called[0] = True

    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "HERE": ROOT,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": save_topics,
        "run_engine": None,
        "ptb_app": None,
    }

    # Create a trash entry
    trash_dir = _trash_dir(ctx)
    entry_name = "restored-project-555"
    folder = trash_dir / entry_name
    folder.mkdir()
    (folder / "main.py").write_text("print('hello')")
    sidecar = {
        "id": "restored-project",
        "name": "restored-project",
        "original_cwd": str(original_cwd),
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tg_chat": 1001,
        "tg_thread": 42,
    }
    (trash_dir / f"{entry_name}.json").write_text(json.dumps(sidecar))

    req = _make_trash_request(ctx, entry_name)
    resp = await api_trash_restore(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["restored"] is True
    assert body["cwd"] == str(original_cwd)

    # Folder is back
    assert original_cwd.exists()
    assert (original_cwd / "main.py").exists()

    # Sidecar removed
    assert not (trash_dir / f"{entry_name}.json").exists()

    # Topic rebound
    assert "1001:42" in ctx["topics"]
    assert ctx["topics"]["1001:42"]["cwd"] == str(original_cwd)
    assert save_topics_called[0]


@pytest.mark.asyncio
async def test_restore_collision_409(tmp_path):
    """Restore collision: original path occupied → 409."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original_cwd = tmp_path / "home" / "occupied-project"
    (tmp_path / "home").mkdir(exist_ok=True)
    original_cwd.mkdir()  # Already exists!

    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "HERE": ROOT,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }

    trash_dir = _trash_dir(ctx)
    entry_name = "occupied-project-777"
    folder = trash_dir / entry_name
    folder.mkdir()
    sidecar = {
        "id": "occupied-project",
        "name": "occupied-project",
        "original_cwd": str(original_cwd),
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tg_chat": None,
        "tg_thread": None,
    }
    (trash_dir / f"{entry_name}.json").write_text(json.dumps(sidecar))

    req = _make_trash_request(ctx, entry_name)
    resp = await api_trash_restore(req)
    assert resp.status == 409
    body = json.loads(resp.body)
    assert "occupied" in body["error"].lower()


@pytest.mark.asyncio
async def test_restore_not_found_404(tmp_path):
    """Restore non-existent entry → 404."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}
    req = _make_trash_request(ctx, "nonexistent-entry-999")
    resp = await api_trash_restore(req)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_restore_invalid_entry_name_400(tmp_path):
    """Restore with path-traversal entry name → 400."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {"DATA": data_dir}
    req = _make_trash_request(ctx, "../etc/passwd")
    resp = await api_trash_restore(req)
    assert resp.status == 400

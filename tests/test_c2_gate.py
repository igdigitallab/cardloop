"""
Tests for C2-gate: worktree mode for cards (apply/discard/mode-detector).

All worktree operations run against a temporary git repo.
Does NOT touch the operator's live projects.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _card_run_mode,
    _card_worktree_setup,
    _commit_in_worktree,
    _diff_from_worktree,
    _run_card,
    _write_run_meta,
    _read_run_meta,
    _write_sidecar,
    _tasks_path,
    _load_board,
    _save_board,
    _done_path,
)


# ─────────────────────────── fixtures ───────────────────────────

@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """Temporary git repo with a baseline commit. Returns Path to the repo root."""
    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(cwd), check=True, capture_output=True)
    (cwd / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(cwd), check=True, capture_output=True)
    return cwd


@pytest.fixture
def tmp_no_git(tmp_path: Path) -> Path:
    """Temporary directory WITHOUT a git repo."""
    cwd = tmp_path / "norepo"
    cwd.mkdir()
    return cwd


@pytest.fixture
def fake_ctx_with_data(tmp_path: Path) -> dict:
    """ctx with a real DATA directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return {
        "topics": {},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }


def _make_board(cwd: Path, card_id: str, col: str = "review") -> dict:
    """Creates TASKS.md with a card in the given column."""
    card = {"id": card_id, "text": "Test task"}
    lines = [
        "# Tasks",
        "## Backlog",
        "## In Progress",
        "## Review",
    ]
    if col == "review":
        lines.append(f"- [ ] Test task <!--ops:{card_id}-->")
    lines += ["## Failed"]
    _tasks_path(str(cwd)).write_text("\n".join(lines), encoding="utf-8")
    return card


# ─────────────────────────── mode detector ───────────────────────────

async def test_mode_detector_worktree_clean_git(tmp_git):
    """git + clean tree → worktree"""
    mode = await _card_run_mode(str(tmp_git))
    assert mode == "worktree"


async def test_mode_detector_legacy_no_git(tmp_no_git):
    """Not a git repo → legacy"""
    mode = await _card_run_mode(str(tmp_no_git))
    assert mode == "legacy"


async def test_mode_detector_legacy_dirty_tree(tmp_git):
    """git + dirty tree → legacy"""
    (tmp_git / "dirty.txt").write_text("uncommitted change\n")
    mode = await _card_run_mode(str(tmp_git))
    assert mode == "legacy"


# ─── invariant #1 (spec-067 v3): allow_legacy=False → 'blocked', never in-place ───
# The unattended autopilot path must HARD-ABORT rather than silently edit a tree it
# cannot cleanly isolate. Interactive/manual cards keep the legacy fallback (default).

async def test_mode_detector_blocked_dirty_tree_no_legacy(tmp_git):
    """allow_legacy=False + dirty tree → blocked (autopilot must not edit in-place)."""
    (tmp_git / "dirty.txt").write_text("uncommitted change\n")
    mode = await _card_run_mode(str(tmp_git), allow_legacy=False)
    assert mode == "blocked"


async def test_mode_detector_blocked_no_git_no_legacy(tmp_no_git):
    """allow_legacy=False + non-git dir → blocked (cannot isolate without git)."""
    mode = await _card_run_mode(str(tmp_no_git), allow_legacy=False)
    assert mode == "blocked"


async def test_mode_detector_blocked_git_disabled_no_legacy(tmp_git):
    """allow_legacy=False + git disabled by project setting → blocked (git never touched)."""
    mode = await _card_run_mode(str(tmp_git), git_enabled=False, allow_legacy=False)
    assert mode == "blocked"


async def test_mode_detector_clean_git_no_legacy_still_worktree(tmp_git):
    """allow_legacy=False + clean git tree → worktree (isolation IS possible)."""
    mode = await _card_run_mode(str(tmp_git), allow_legacy=False)
    assert mode == "worktree"


async def test_mode_detector_default_unchanged_dirty_still_legacy(tmp_git):
    """Regression: the default (allow_legacy=True) is UNCHANGED — dirty tree → legacy."""
    (tmp_git / "dirty.txt").write_text("uncommitted change\n")
    mode = await _card_run_mode(str(tmp_git))  # no allow_legacy → defaults True
    assert mode == "legacy"


# ─────────────────────────── worktree setup ───────────────────────────

async def test_worktree_setup_creates_worktree(tmp_git):
    """_card_worktree_setup creates a worktree and a card-<id> branch."""
    info = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info is not None, "setup must return a dict, not None"
    assert "wt_path" in info
    assert "base_branch" in info
    wt = Path(info["wt_path"])
    assert wt.exists(), "Worktree directory must exist"
    # Verify that the branch was created
    result = subprocess.run(
        ["git", "branch"], cwd=str(tmp_git), capture_output=True, text=True
    )
    assert "card-aabbcc" in result.stdout


async def test_worktree_setup_cleans_existing(tmp_git):
    """Repeated setup cleans the old worktree and recreates it."""
    info1 = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info1 is not None
    # Write a file in the worktree — after cleanup it must be gone
    old_wt = Path(info1["wt_path"])
    (old_wt / "canary.txt").write_text("old")
    info2 = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info2 is not None
    # Old worktree removed, new one created
    new_wt = Path(info2["wt_path"])
    assert new_wt.exists()
    assert not (new_wt / "canary.txt").exists(), "Canary from old worktree must not be in the new one"


async def test_worktree_setup_no_git(tmp_no_git):
    """Not a git repo → None (graceful degradation)."""
    info = await _card_worktree_setup(str(tmp_no_git), "aabbcc")
    assert info is None


# ─────────────────────────── _commit_in_worktree ───────────────────────────

async def test_commit_in_worktree_with_changes(tmp_git):
    """If the worktree has changes — commit is made, returns True."""
    info = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info is not None
    wt_path = info["wt_path"]
    # Write a file in the worktree
    (Path(wt_path) / "new_file.py").write_text("# created by agent\n")
    result = await _commit_in_worktree(wt_path, "aabbcc", "Add new file")
    assert result is True
    # Verify that the commit appeared
    log = subprocess.run(["git", "log", "--oneline"], cwd=wt_path, capture_output=True, text=True)
    assert "card aabbcc" in log.stdout


async def test_commit_in_worktree_no_changes(tmp_git):
    """No changes → no commit, returns False."""
    info = await _card_worktree_setup(str(tmp_git), "aabbcc")
    assert info is not None
    result = await _commit_in_worktree(info["wt_path"], "aabbcc", "No changes")
    assert result is False


# ─────────────────────────── JSON meta ───────────────────────────

def test_write_read_run_meta(tmp_path):
    """_write_run_meta / _read_run_meta round-trip."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": "aabbcc",
        "mode": "worktree",
        "branch": "card-aabbcc",
        "base_branch": "main",
        "wt_path": "/tmp/wt",
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, "aabbcc", meta)
    loaded = _read_run_meta(data_dir, "aabbcc")
    assert loaded == meta


def test_read_run_meta_missing(tmp_path):
    """_read_run_meta returns None when the file does not exist."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert _read_run_meta(data_dir, "nonexistent") is None


# ─────────────────────────── _write_sidecar with mode ───────────────────────────

def test_write_sidecar_worktree_mode_writes_json(tmp_path):
    """_write_sidecar in worktree mode writes both .md and .json."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="aabbcc",
        name="proj",
        prompt="Test prompt",
        answer_text="Test answer",
        ok=True,
        exc_info=None,
        diff_stat="1 file",
        diff_full="diff --git ...",
        run_mode="worktree",
        wt_branch="card-aabbcc",
        base_branch="main",
        wt_path="/tmp/.worktrees/card-aabbcc",
        has_changes=True,
    )
    md = tmp_path / "runs" / "aabbcc.md"
    js = tmp_path / "runs" / "aabbcc.json"
    assert md.exists()
    assert js.exists()
    meta = json.loads(js.read_text())
    assert meta["mode"] == "worktree"
    assert meta["has_changes"] is True
    assert meta["applied"] is False
    assert meta["discarded"] is False


def test_write_sidecar_legacy_mode_writes_json(tmp_path):
    """_write_sidecar in legacy mode also writes JSON with mode=legacy."""
    _write_sidecar(
        data_dir=tmp_path,
        card_id="cccccc",
        name="proj",
        prompt="x",
        answer_text="y",
        ok=True,
        exc_info=None,
        diff_stat="",
        diff_full="",
    )
    js = tmp_path / "runs" / "cccccc.json"
    assert js.exists()
    meta = json.loads(js.read_text())
    assert meta["mode"] == "legacy"


# ─────────────────────────── apply success ───────────────────────────

async def test_apply_success(tmp_git, tmp_path):
    """apply: successful merge --no-ff → card Done, worktree removed."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply, _tasks_path

    # Setup: create a worktree with a commit
    card_id = "aabbcc"
    info = await _card_worktree_setup(str(tmp_git), card_id)
    assert info is not None
    (Path(info["wt_path"]) / "feature.py").write_text("x = 1\n")
    await _commit_in_worktree(info["wt_path"], card_id, "Add feature")

    # DATA and meta
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": info["base_branch"],
        "wt_path": info["wt_path"],
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    # Board with card in Review
    _make_board(tmp_git, card_id, "review")

    # ctx and app
    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))

    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    data = json.loads(resp.body)
    assert resp.status == 200, f"Expected 200, got {resp.status}: {data}"
    assert data["applied"] is True

    # Worktree removed
    assert not Path(info["wt_path"]).exists(), "Worktree must be removed after apply"

    # Card in Done
    dp = _done_path(str(tmp_git))
    assert dp.exists()
    done_content = dp.read_text()
    assert card_id in done_content or "Test task" in done_content

    # meta updated
    updated_meta = _read_run_meta(data_dir, card_id)
    assert updated_meta["applied"] is True


async def test_apply_conflict(tmp_git, tmp_path):
    """apply with a conflict → 409, card stays in Review, worktree alive."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    card_id = "aabbcc"
    info = await _card_worktree_setup(str(tmp_git), card_id)
    assert info is not None

    # Write a change to README.md in the worktree
    (Path(info["wt_path"]) / "README.md").write_text("# Modified in branch\n")
    await _commit_in_worktree(info["wt_path"], card_id, "Modify README")

    # Also change README.md in main tree → conflict
    (tmp_git / "README.md").write_text("# Modified on main\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_git), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main change"],
        cwd=str(tmp_git), check=True, capture_output=True
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": info["base_branch"],
        "wt_path": info["wt_path"],
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)
    _make_board(tmp_git, card_id, "review")

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 409, f"Expected 409 (conflict), got {resp.status}"
    data = json.loads(resp.body)
    assert "error" in data

    # Worktree must be alive
    assert Path(info["wt_path"]).exists(), "Worktree must survive after conflict"

    # meta not marked applied
    updated = _read_run_meta(data_dir, card_id)
    assert updated["applied"] is False


async def test_discard(tmp_git, tmp_path):
    """discard → branch/worktree removed, card back in Backlog."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_discard

    card_id = "aabbcc"
    info = await _card_worktree_setup(str(tmp_git), card_id)
    assert info is not None

    # Make a commit so the branch is not empty
    (Path(info["wt_path"]) / "new.py").write_text("x = 1\n")
    await _commit_in_worktree(info["wt_path"], card_id, "Add file")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": info["base_branch"],
        "wt_path": info["wt_path"],
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)
    _make_board(tmp_git, card_id, "review")

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/discard",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_discard(req)
    data = json.loads(resp.body)
    assert resp.status == 200, f"Expected 200, got {resp.status}: {data}"
    assert data["discarded"] is True

    # Worktree removed
    assert not Path(info["wt_path"]).exists(), "Worktree must be removed after discard"

    # Branch removed
    branches = subprocess.run(
        ["git", "branch"], cwd=str(tmp_git), capture_output=True, text=True
    ).stdout
    assert f"card-{card_id}" not in branches

    # Card back in Backlog
    _, _, cols = _load_board(str(tmp_git))
    assert any(c["id"] == card_id for c in cols["backlog"]), "Card must return to Backlog"

    # meta updated
    updated = _read_run_meta(data_dir, card_id)
    assert updated["discarded"] is True


async def test_apply_legacy_returns_400(tmp_path):
    """apply for a legacy card → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    # Write legacy meta
    _write_run_meta(data_dir, card_id, {
        "card_id": card_id,
        "mode": "legacy",
        "branch": None,
        "base_branch": None,
        "wt_path": None,
        "has_changes": True,
        "applied": False,
        "discarded": False,
    })

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 400, f"Expected 400, got {resp.status}"


async def test_discard_legacy_returns_400(tmp_path):
    """discard for a legacy card → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_discard

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    _write_run_meta(data_dir, card_id, {
        "card_id": card_id,
        "mode": "legacy",
        "branch": None,
        "base_branch": None,
        "wt_path": None,
        "has_changes": False,
        "applied": False,
        "discarded": False,
    })

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/discard",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_discard(req)
    assert resp.status == 400


async def test_apply_bad_card_id(tmp_path):
    """apply with bad card_id → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/../../etc/passwd/apply",
        match_info={"id": pid, "card": "../../etc/passwd"},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 400


async def test_discard_bad_card_id(tmp_path):
    """discard with bad card_id → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_discard

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/bad!id/discard",
        match_info={"id": pid, "card": "bad!id"},
        app=app,
    )

    resp = await api_card_discard(req)
    assert resp.status == 400


async def test_apply_no_meta_returns_400(tmp_path):
    """apply with no JSON meta (file missing) → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from webapp import api_card_apply

    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/apply",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_apply(req)
    assert resp.status == 400


# ─────────────────────────── _diff_from_worktree ───────────────────────────

async def test_diff_from_worktree_with_changes(tmp_git):
    """worktree with a commit → diff_full contains the change, diff_stat is non-empty."""
    info = await _card_worktree_setup(str(tmp_git), "dddddd")
    assert info is not None
    wt_path = info["wt_path"]
    base_branch = info["base_branch"]

    # Write a file in the worktree and commit
    (Path(wt_path) / "agent_output.py").write_text("result = 42\n")
    committed = await _commit_in_worktree(wt_path, "dddddd", "Add agent output")
    assert committed is True

    diff_full, diff_stat = await _diff_from_worktree(wt_path, base_branch)

    assert "agent_output.py" in diff_full, "diff_full must contain the changed file name"
    assert diff_stat != "", "diff_stat must not be empty when changes exist"


async def test_diff_from_worktree_no_changes(tmp_git):
    """worktree without changes → diff is empty."""
    info = await _card_worktree_setup(str(tmp_git), "eeeeee")
    assert info is not None
    wt_path = info["wt_path"]
    base_branch = info["base_branch"]

    # No commit — worktree is identical to base
    diff_full, diff_stat = await _diff_from_worktree(wt_path, base_branch)

    assert diff_full == "", "diff_full must be empty with no changes"
    assert diff_stat == "", "diff_stat must be empty with no changes"


async def test_diff_from_worktree_invalid_path():
    """Invalid path → returns ('', '') without raising."""
    diff_full, diff_stat = await _diff_from_worktree("/nonexistent/path/xyz", "main")
    assert diff_full == "", "diff_full must be '' for invalid path"
    assert diff_stat == "", "diff_stat must be '' for invalid path"


# ─────────────────────────── e2e _run_card (worktree + legacy) ───────────────────────────

def _make_fake_card(card_id: str, text: str = "Test task") -> dict:
    return {"id": card_id, "text": text}


def _make_ctx_for_run_card(data_dir: Path, cwd: str, run_engine_factory) -> dict:
    """ctx sufficient for _run_card: includes run_engine, save_sessions, sessions."""
    pid = Path(cwd.rstrip("/")).name
    return {
        "topics": {
            f"0:{pid}": {"cwd": cwd, "project": pid, "name": pid, "tg_thread": f"0:{pid}"},
        },
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": run_engine_factory,
        "ptb_app": None,
    }


async def test_run_card_worktree_isolation(tmp_git, tmp_path):
    """Integration test: agent writes to worktree, NOT to the project working tree."""
    card_id = "ffffff"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cwd = str(tmp_git)

    # Create TASKS.md with card in In Progress (where the agent picks it up)
    _make_board(tmp_git, card_id, "review")

    # Prepare worktree
    wt_info = await _card_worktree_setup(cwd, card_id)
    assert wt_info is not None, "worktree setup must successfully create a worktree"
    wt_path = wt_info["wt_path"]

    # Mock run_engine: writes a file to the cwd it receives, then finishes
    async def fake_run_engine(**kwargs):
        engine_cwd = kwargs.get("cwd", "")
        # Write file to the cwd that _run_card passes — this must be wt_path
        (Path(engine_cwd) / "agent_created.py").write_text("x = 1\n")
        yield {"type": "text", "text": "Done"}
        yield {"type": "result", "session_id": "fake-sid-001"}

    project = {"name": "testrepo", "cwd": cwd, "model": "sonnet"}
    session_key = f"0:{Path(cwd).name}"

    ctx = _make_ctx_for_run_card(data_dir, cwd, fake_run_engine)
    ctx["running"][session_key] = True  # simulate lock reservation

    await _run_card(ctx, None, project, _make_fake_card(card_id), session_key,
                    run_mode="worktree", wt_info=wt_info)

    # INVARIANT 1: file created in worktree, NOT in project cwd
    assert (Path(wt_path) / "agent_created.py").exists(), \
        "Agent file must be in worktree"
    assert not (tmp_git / "agent_created.py").exists(), \
        "Agent file must NOT be in the project working tree"

    # INVARIANT 2: TRACKED files in the project working tree are unchanged
    # (untracked files such as TASKS.md or .worktrees/ are allowed)
    diff_tracked = subprocess.run(
        ["git", "diff", "HEAD"], cwd=cwd, capture_output=True, text=True
    )
    assert diff_tracked.stdout.strip() == "", \
        f"Tracked files in working tree must be clean: {diff_tracked.stdout.strip()!r}"
    # Agent file must NOT appear as untracked in tracked tree
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True
    )
    # agent_created.py must not be in the working tree status
    assert "agent_created.py" not in status.stdout, \
        f"agent_created.py must not be in working tree status: {status.stdout!r}"

    # INVARIANT 3: card moved to Review, JSON meta written
    meta = _read_run_meta(data_dir, card_id)
    assert meta is not None, "JSON meta must be written"
    assert meta["mode"] == "worktree", f"mode must be 'worktree', got {meta['mode']!r}"
    assert meta["has_changes"] is True, "has_changes must be True (agent created a file)"

    # INVARIANT 4: worktree NOT removed, branch exists
    assert Path(wt_path).exists(), "Worktree must NOT be removed after the run"
    branches = subprocess.run(
        ["git", "branch"], cwd=cwd, capture_output=True, text=True
    ).stdout
    assert f"card-{card_id}" in branches, f"Branch card-{card_id} must exist"

    # INVARIANT 5: lock released
    assert session_key not in ctx["running"], "running[session_key] must be released in finally"

    # INVARIANT 6: card in Review (moved from backlog)
    _, _, cols = _load_board(cwd)
    assert any(c["id"] == card_id for c in cols["review"]), \
        "Card must be in Review after successful run"


async def test_run_card_legacy_mode(tmp_no_git, tmp_path):
    """Legacy mode: file created in project cwd, meta mode=legacy."""
    card_id = "bbbbbb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cwd = str(tmp_no_git)

    # Create TASKS.md with a card
    _make_board(tmp_no_git, card_id, "review")

    async def fake_run_engine_legacy(**kwargs):
        engine_cwd = kwargs.get("cwd", "")
        (Path(engine_cwd) / "legacy_output.txt").write_text("legacy result\n")
        yield {"type": "text", "text": "Legacy done"}
        yield {"type": "result", "session_id": "fake-sid-002"}

    project = {"name": "norepo", "cwd": cwd, "model": "sonnet"}
    session_key = f"0:{Path(cwd).name}"

    ctx = _make_ctx_for_run_card(data_dir, cwd, fake_run_engine_legacy)
    ctx["running"][session_key] = True

    # In legacy mode wt_info=None
    await _run_card(ctx, None, project, _make_fake_card(card_id), session_key,
                    run_mode="legacy", wt_info=None)

    # File created in project cwd
    assert (tmp_no_git / "legacy_output.txt").exists(), \
        "Agent file must be in project cwd in legacy mode"

    # Meta written with mode=legacy
    meta = _read_run_meta(data_dir, card_id)
    assert meta is not None, "JSON meta must be written"
    assert meta["mode"] == "legacy", f"mode must be 'legacy', got {meta['mode']!r}"

    # Lock released
    assert session_key not in ctx["running"], "running[session_key] must be released in finally"

    # Card in Review
    _, _, cols = _load_board(cwd)
    assert any(c["id"] == card_id for c in cols["review"]), \
        "Card must be in Review after successful run (legacy)"


# ─────────────────────────── utilities ───────────────────────────

def _project_id(cwd: str) -> str:
    return Path(cwd.rstrip("/")).name


def _make_ctx_with_project(data_dir: Path, cwd: str) -> dict:
    """Creates a minimal ctx with the given project cwd, sufficient for api_card_apply/discard."""
    pid = _project_id(cwd)
    return {
        "topics": {
            f"0:{pid}": {"cwd": cwd, "project": pid, "name": pid, "tg_thread": f"0:{pid}"},
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

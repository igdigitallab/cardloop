"""
Tests for Spec 009 — quality gate: _run_quality_gate + API /check.

Does NOT run real project tests (only tmp_git fixtures).
Fixtures: tmp_git (from conftest / local), _make_ctx_with_project, _project_id.
"""
import asyncio
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import (
    _run_quality_gate,
    api_card_check,
    _write_run_meta,
    _read_run_meta,
    _valid_card_id,
)


# ─────────────────────────── fixtures ───────────────────────────

@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """Temporary git repo with a baseline commit."""
    cwd = tmp_path / "testrepo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(cwd), check=True, capture_output=True)
    (cwd / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(cwd), check=True, capture_output=True)
    return cwd


def _project_id(cwd: str) -> str:
    return Path(cwd.rstrip("/")).name


def _make_ctx_with_project(data_dir: Path, cwd: str) -> dict:
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


def _link_venv(proj: Path) -> None:
    """Point the fake project's venv at the REAL test venv so the gate's
    'venv/bin/pytest' / 'venv/bin/python -m pytest' can actually import pytest.

    Symlinking only venv/bin/python (no pyvenv.cfg, no site-packages) works
    locally but breaks on CI: there sys.executable is itself a symlink into a
    PEP-405 venv, and a bare bin/python with no pyvenv.cfg resolves back to the
    base interpreter — which has no pytest → 'No module named pytest' → every
    "expect safe" gate test falsely came back risky. Symlinking the whole venv
    dir keeps pyvenv.cfg + site-packages, so pytest imports in any environment."""
    venv_root = Path(sys.executable).parent.parent
    (proj / "venv").symlink_to(venv_root, target_is_directory=True)


def _make_passing_project(tmp_path: Path) -> Path:
    """Project with pytest + a test that passes."""
    p = tmp_path / "passing_proj"
    p.mkdir()
    (p / "tests").mkdir()
    (p / "tests" / "__init__.py").write_text("")
    (p / "tests" / "test_ok.py").write_text("def test_always_pass(): assert 1 == 1\n")
    _link_venv(p)
    return p


def _make_failing_project(tmp_path: Path) -> Path:
    """Project with pytest + a test that fails."""
    p = tmp_path / "failing_proj"
    p.mkdir()
    (p / "tests").mkdir()
    (p / "tests" / "__init__.py").write_text("")
    (p / "tests" / "test_fail.py").write_text("def test_always_fail(): assert False, 'intentional'\n")
    _link_venv(p)
    return p


def _make_no_test_project(tmp_path: Path) -> Path:
    """Project without a test configuration."""
    p = tmp_path / "no_test_proj"
    p.mkdir()
    (p / "main.py").write_text("x = 1\n")
    return p


# ─────────────────────────── _run_quality_gate unit ───────────────────────────

async def test_gate_passing_tests_returns_safe(tmp_path):
    """Project with passing tests → safe."""
    proj = _make_passing_project(tmp_path)
    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "safe", f"Expected safe, got: {result}"
    assert result["tests"]["detected"] is True
    assert result["tests"]["ok"] is True
    assert result["tests"]["exit_code"] == 0
    assert result["tests"]["timed_out"] is False
    assert result["lint"] is None


async def test_gate_failing_tests_returns_risky(tmp_path):
    """Project with failing tests → risky."""
    proj = _make_failing_project(tmp_path)
    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "risky", f"Expected risky, got: {result}"
    assert result["tests"]["detected"] is True
    assert result["tests"]["ok"] is False
    assert result["tests"]["exit_code"] != 0
    assert result["tests"]["timed_out"] is False


async def test_gate_no_tests_returns_unknown(tmp_path):
    """Project without a test configuration → unknown."""
    proj = _make_no_test_project(tmp_path)
    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "unknown", f"Expected unknown, got: {result}"
    assert result["tests"]["detected"] is False
    assert result["tests"]["cmd"] is None


async def test_gate_runs_in_wt_path(tmp_path):
    """_run_quality_gate runs tests in the given wt_path, not in the process cwd."""
    # Create a project with tests in a separate folder
    proj = _make_passing_project(tmp_path)
    # Confirm that tests would not be found from a different cwd (tmp_path)
    result_wrong = await _run_quality_gate(str(tmp_path))
    # From proj — they are found
    result_ok = await _run_quality_gate(str(proj))
    assert result_ok["verdict"] == "safe"
    # From tmp_path — no tests/ or pytest config
    assert result_wrong["verdict"] == "unknown"


async def test_gate_secrets_in_env(tmp_path):
    """Secrets are injected into env: a test can read them via os.environ."""
    proj = tmp_path / "secret_proj"
    proj.mkdir()
    (proj / "tests").mkdir()
    (proj / "tests" / "__init__.py").write_text("")
    # Test checks the MY_SECRET_42 environment variable
    test_code = textwrap.dedent("""\
        import os
        def test_has_secret():
            val = os.environ.get('MY_SECRET_42', '')
            assert val == 'hello_world', f'Got: {val!r}'
    """)
    (proj / "tests" / "test_secret.py").write_text(test_code)
    _link_venv(proj)

    # Without secret — test fails
    result_no_secret = await _run_quality_gate(str(proj))
    assert result_no_secret["verdict"] == "risky"

    # With secret — test passes
    result_with_secret = await _run_quality_gate(str(proj), env={"MY_SECRET_42": "hello_world"})
    assert result_with_secret["verdict"] == "safe", (
        f"Test should pass with secret. Output: {result_with_secret['tests']['output']}"
    )


async def test_gate_output_truncated(tmp_path):
    """Test output is truncated to ~20k characters."""
    proj = tmp_path / "loud_proj"
    proj.mkdir()
    (proj / "tests").mkdir()
    (proj / "tests" / "__init__.py").write_text("")
    # Test that prints a lot of output
    test_code = textwrap.dedent("""\
        def test_loud():
            for i in range(5000):
                print('x' * 10)
            assert False
    """)
    (proj / "tests" / "test_loud.py").write_text(test_code)
    _link_venv(proj)

    result = await _run_quality_gate(str(proj))
    assert result["verdict"] == "risky"
    assert len(result["tests"]["output"]) <= 21000  # small headroom


# ─────────────────────────── API /check ───────────────────────────

async def test_api_check_worktree_returns_verdict(tmp_git, tmp_path):
    """check API for a worktree card → returns a verdict."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Write worktree meta (wt_path = tmp_git — no tests there → unknown, but no error)
    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(tmp_git),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    data = json.loads(resp.body)
    assert "verdict" in data
    assert data["verdict"] in ("safe", "risky", "unknown")


async def test_api_check_legacy_returns_unknown(tmp_path):
    """check API for a legacy card → {verdict:'unknown', reason:'legacy'}."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"

    # Legacy meta
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
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "unknown"
    assert data.get("reason") == "legacy"


async def test_api_check_no_meta_returns_unknown(tmp_path):
    """check API without a meta sidecar → {verdict:'unknown', reason:'legacy'}."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"
    # No meta file

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "unknown"


async def test_api_check_bad_card_id_returns_400(tmp_path):
    """check API with invalid card_id → 400."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    bad_card_id = "../evil"

    pid = _project_id(str(cwd))
    ctx = _make_ctx_with_project(data_dir, str(cwd))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{bad_card_id}/check",
        match_info={"id": pid, "card": bad_card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 400, f"Expected 400 for bad card_id, got {resp.status}"


async def test_api_check_missing_worktree_returns_404(tmp_path):
    """check API: wt_path does not exist → 404."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    cwd = tmp_path / "myproj"
    cwd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    card_id = "aabbcc"

    # Worktree meta with a non-existent path
    _write_run_meta(data_dir, card_id, {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(tmp_path / "nonexistent-wt"),
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
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 404, f"Expected 404 for non-existent worktree, got {resp.status}"


async def test_api_check_updates_meta_gate_field(tmp_git, tmp_path):
    """check API updates meta['gate'] with verdict and ts."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(tmp_git),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200

    # Meta must be updated with the gate field
    updated_meta = _read_run_meta(data_dir, card_id)
    assert updated_meta is not None
    assert "gate" in updated_meta, "meta must contain the gate field"
    gate = updated_meta["gate"]
    assert "verdict" in gate
    assert "ts" in gate
    assert gate["verdict"] in ("safe", "risky", "unknown")


async def test_api_check_project_not_found_returns_404(tmp_path):
    """check API for a non-existent project → 404."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = {
        "topics": {},  # empty — no projects
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        "/api/projects/nonexistent/tasks/aabbcc/check",
        match_info={"id": "nonexistent", "card": "aabbcc"},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 404


async def test_api_check_passing_project_in_wt(tmp_git, tmp_path):
    """check API: worktree with passing tests → safe."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Create a worktree directory with passing tests
    wt = tmp_path / "wt_passing"
    wt.mkdir()
    (wt / "tests").mkdir()
    (wt / "tests" / "__init__.py").write_text("")
    (wt / "tests" / "test_ok.py").write_text("def test_pass(): assert True\n")
    _link_venv(wt)

    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(wt),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "safe", f"Expected safe, got: {data}"
    assert data["tests"]["ok"] is True


async def test_api_check_failing_project_in_wt(tmp_git, tmp_path):
    """check API: worktree with failing tests → risky."""
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web

    card_id = "aabbcc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    wt = tmp_path / "wt_failing"
    wt.mkdir()
    (wt / "tests").mkdir()
    (wt / "tests" / "__init__.py").write_text("")
    (wt / "tests" / "test_fail.py").write_text("def test_fail(): assert False\n")

    meta = {
        "card_id": card_id,
        "mode": "worktree",
        "branch": f"card-{card_id}",
        "base_branch": "main",
        "wt_path": str(wt),
        "has_changes": True,
        "applied": False,
        "discarded": False,
    }
    _write_run_meta(data_dir, card_id, meta)

    pid = _project_id(str(tmp_git))
    ctx = _make_ctx_with_project(data_dir, str(tmp_git))
    app = web.Application()
    app["ctx"] = ctx

    req = make_mocked_request(
        "POST",
        f"/api/projects/{pid}/tasks/{card_id}/check",
        match_info={"id": pid, "card": card_id},
        app=app,
    )

    resp = await api_card_check(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["verdict"] == "risky", f"Expected risky, got: {data}"
    assert data["tests"]["ok"] is False

"""
Board/card API tests via aiohttp.test_utils.

Covers:
- api_move_task: moving between columns and to=done (archived in DONE.md)
- api_delete_task: deleting a card
- api_update_task: updating card text
- api_create_task: creating a card in the desired column
- card_id validation: bad id → 400

Does NOT start a real run_engine (mocked or set to None).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _tasks_path, _done_path, _write_sidecar, _derive_token


# ─────────────────────────── helpers ───────────────────────────


def _make_tasks_md(cwd: Path, backlog=None, in_progress=None, review=None, failed=None) -> None:
    """Write TASKS.md with the given cards. If id is not set — generate a hex id."""
    import secrets

    def _line(card):
        if isinstance(card, str):
            return f"- [ ] {card} <!--ops:{secrets.token_hex(3)}-->"
        # card = dict with id and text
        return f"- [ ] {card['text']} <!--ops:{card['id']}-->"

    lines = ["# Tasks\n", "Test project\n"]
    lines += ["## Backlog\n"]
    for c in (backlog or []):
        lines.append(_line(c))
    lines += ["\n## In Progress\n"]
    for c in (in_progress or []):
        lines.append(_line(c))
    lines += ["\n## Review\n"]
    for c in (review or []):
        lines.append(_line(c))
    lines += ["\n## Failed\n"]
    for c in (failed or []):
        lines.append(_line(c))
    _tasks_path(str(cwd)).write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def project_dir(tmp_path):
    """Temporary project directory with TASKS.md."""
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_with_project(tmp_path, project_dir):
    """ctx with a single project in topics."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,  # degraded mode — auto-run is not started
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def board_app(fake_ctx_with_project):
    """aiohttp application with the full set of board routes."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects", _webapp.api_projects)
    app.router.add_get("/api/me", _webapp.api_me)
    app.router.add_get("/api/projects/{id}/tasks", _webapp.api_project_tasks)
    app.router.add_post("/api/projects/{id}/tasks", _webapp.api_create_task)
    app.router.add_get("/api/projects/{id}/tasks/done", _webapp.api_tasks_done)
    app.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    app.router.add_delete("/api/projects/{id}/tasks/{card}", _webapp.api_delete_task)
    app.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", _webapp.api_update_task)
    app.router.add_get("/api/projects/{id}/tasks/{card}/run", _webapp.api_card_run)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── GET /api/projects/{id}/tasks ───────────────────────────


async def test_get_tasks_empty_board(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks for a project with no TASKS.md → 200, exists=False, all columns empty."""
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/tasks", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False
    for col in data["columns"]:
        assert col["cards"] == []


async def test_get_tasks_with_board(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks with an existing TASKS.md → cards are returned."""
    card = {"id": "aabbcc", "text": "Do something"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/tasks", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is True
    backlog_col = next(c for c in data["columns"] if c["key"] == "backlog")
    assert len(backlog_col["cards"]) == 1
    assert backlog_col["cards"][0]["text"] == "Do something"


async def test_get_tasks_unknown_project(aiohttp_client, board_app, fake_ctx_with_project):
    """GET /tasks for a nonexistent project → 404."""
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/nonexistent/tasks", headers=h)
    assert resp.status == 404


# ─────────────────────────── POST /api/projects/{id}/tasks ───────────────────────────


async def test_create_task_in_backlog(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """POST /tasks with column=backlog → card is created in Backlog."""
    _make_tasks_md(project_dir)  # empty board
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks",
        json={"text": "New card", "column": "backlog"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    backlog_col = next(c for c in data["columns"] if c["key"] == "backlog")
    assert any(c["text"] == "New card" for c in backlog_col["cards"])


async def test_create_task_empty_text(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """POST /tasks with empty text → 400."""
    _make_tasks_md(project_dir)
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks",
        json={"text": "  ", "column": "backlog"},
        headers=h,
    )
    assert resp.status == 400


async def test_create_task_invalid_project(aiohttp_client, board_app, fake_ctx_with_project):
    """POST /tasks for a nonexistent project → 404."""
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/ghost/tasks",
        json={"text": "whatever"},
        headers=h,
    )
    assert resp.status == 404


# ─────────────────────────── POST .../tasks/{card}/move ───────────────────────────


async def test_move_card_backlog_to_review(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """Move card Backlog → Review — card appears in Review."""
    card = {"id": "aabbcc", "text": "Task A"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks/aabbcc/move",
        json={"to": "review"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    review_col = next(c for c in data["columns"] if c["key"] == "review")
    backlog_col = next(c for c in data["columns"] if c["key"] == "backlog")
    assert any(c["id"] == "aabbcc" for c in review_col["cards"])
    assert not any(c["id"] == "aabbcc" for c in backlog_col["cards"])


async def test_move_card_to_done_creates_done_md(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """to=done → card goes into DONE.md and disappears from all columns."""
    card = {"id": "aabbcc", "text": "Finished task"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks/aabbcc/move",
        json={"to": "done"},
        headers=h,
    )
    assert resp.status == 200
    # Card should not appear in any board column
    data = await resp.json()
    all_cards = [c for col in data["columns"] for c in col["cards"]]
    assert not any(c["id"] == "aabbcc" for c in all_cards)

    # DONE.md should exist and contain the card text
    done_path = _done_path(str(project_dir))
    assert done_path.exists()
    content = done_path.read_text(encoding="utf-8")
    assert "Finished task" in content


async def test_move_card_to_done_appends_to_existing_done_md(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """to=done when DONE.md already exists → appends, does not overwrite."""
    done_path = _done_path(str(project_dir))
    done_path.write_text("# Done — myproject\n- [x] Old task · 2026-01-01\n", encoding="utf-8")

    card = {"id": "ccddee", "text": "New finished task"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks/ccddee/move",
        json={"to": "done"},
        headers=h,
    )
    assert resp.status == 200
    content = done_path.read_text(encoding="utf-8")
    assert "Old task" in content  # old entry preserved
    assert "New finished task" in content  # new entry added


async def test_move_card_nonexistent_card(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """Moving a nonexistent card → 404."""
    _make_tasks_md(project_dir)  # empty board
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks/ffffff/move",
        json={"to": "review"},
        headers=h,
    )
    assert resp.status == 404


async def test_move_card_bad_card_id(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """Invalid card_id → 400 (before searching the board)."""
    _make_tasks_md(project_dir)
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks/../etc/move",
        json={"to": "review"},
        headers=h,
    )
    # aiohttp may return 404 from routing or 400 from validation
    assert resp.status in (400, 404)


async def test_move_card_to_in_progress_no_engine(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """to=in_progress without run_engine (degraded) → 200, card in In Progress."""
    card = {"id": "aabbcc", "text": "Start me"}
    _make_tasks_md(project_dir, backlog=[card])

    # run_engine = None (already set in fixture)
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.post(
        "/api/projects/myproject/tasks/aabbcc/move",
        json={"to": "in_progress"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    ip_col = next(c for c in data["columns"] if c["key"] == "in_progress")
    assert any(c["id"] == "aabbcc" for c in ip_col["cards"])


# ─────────────────────────── DELETE .../tasks/{card} ───────────────────────────


async def test_delete_card(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """DELETE card → 200, card disappears from the board."""
    card = {"id": "aabbcc", "text": "Delete me"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete(
        "/api/projects/myproject/tasks/aabbcc",
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    all_cards = [c for col in data["columns"] for c in col["cards"]]
    assert not any(c["id"] == "aabbcc" for c in all_cards)

    # Also verify that TASKS.md on disk is updated
    content = _tasks_path(str(project_dir)).read_text(encoding="utf-8")
    assert "aabbcc" not in content


async def test_delete_card_not_found(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """DELETE nonexistent card → 404."""
    _make_tasks_md(project_dir)
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.delete("/api/projects/myproject/tasks/ffffff", headers=h)
    assert resp.status == 404


async def test_delete_card_bad_id(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """DELETE with invalid card_id → 400."""
    _make_tasks_md(project_dir)
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    # Long card_id > 20 chars → 400
    resp = await client.delete(
        "/api/projects/myproject/tasks/aabbccddeeff0011223344",
        headers=h,
    )
    assert resp.status == 400


# ─────────────────────────── PATCH .../tasks/{card} (update) ───────────────────────────


async def test_update_card_text(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """PATCH card → 200, text updated."""
    card = {"id": "aabbcc", "text": "Old text"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.patch(
        "/api/projects/myproject/tasks/aabbcc",
        json={"text": "New text"},
        headers=h,
    )
    assert resp.status == 200
    data = await resp.json()
    all_cards = [c for col in data["columns"] for c in col["cards"]]
    updated = next((c for c in all_cards if c["id"] == "aabbcc"), None)
    assert updated is not None
    assert updated["text"] == "New text"


async def test_update_card_empty_text(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """PATCH with empty text → 400."""
    card = {"id": "aabbcc", "text": "Original"}
    _make_tasks_md(project_dir, backlog=[card])

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.patch(
        "/api/projects/myproject/tasks/aabbcc",
        json={"text": ""},
        headers=h,
    )
    assert resp.status == 400


async def test_update_card_bad_id(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """PATCH with invalid card_id → 400."""
    _make_tasks_md(project_dir)
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.patch(
        "/api/projects/myproject/tasks/!!!invalid!!!",
        json={"text": "x"},
        headers=h,
    )
    assert resp.status == 400


# ─────────────────────────── GET .../tasks/{card}/run (sidecar) ───────────────────────────


async def test_card_run_sidecar_missing(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks/{card}/run for a missing sidecar → exists=False."""
    _make_tasks_md(project_dir, backlog=[{"id": "aabbcc", "text": "card"}])
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/tasks/aabbcc/run", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False
    assert data["content"] == ""


async def test_card_run_sidecar_exists(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks/{card}/run when sidecar exists → exists=True, content non-empty."""
    data_dir = fake_ctx_with_project["DATA"]
    _write_sidecar(
        data_dir,
        card_id="aabbcc",
        name="myproject",
        prompt="Do the thing",
        answer_text="Done!",
        ok=True,
        exc_info=None,
        diff_stat="",
        diff_full="",
    )

    _make_tasks_md(project_dir, review=[{"id": "aabbcc", "text": "card"}])
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/tasks/aabbcc/run", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is True
    assert "Done!" in data["content"]


async def test_card_run_bad_card_id(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks/{card}/run with invalid card_id → 400."""
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get(
        "/api/projects/myproject/tasks/../../etc/run",
        headers=h,
    )
    # aiohttp routing may not match ../ or will return 400
    assert resp.status in (400, 404)


# ─────────────────────────── GET .../tasks/done ───────────────────────────


async def test_tasks_done_no_done_md(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks/done with no DONE.md → exists=False, content=""."""
    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/tasks/done", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False
    assert data["content"] == ""


async def test_tasks_done_with_done_md(aiohttp_client, board_app, fake_ctx_with_project, project_dir):
    """GET /tasks/done with DONE.md present → exists=True, content contains text."""
    done_path = _done_path(str(project_dir))
    done_path.write_text("# Done\n- [x] Completed · 2026-01-01\n", encoding="utf-8")

    client = await aiohttp_client(board_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/tasks/done", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is True
    assert "Completed" in data["content"]

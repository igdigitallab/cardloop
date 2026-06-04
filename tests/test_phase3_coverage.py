"""
Spec-011 Ф3 — TEST-COVERAGE: новые тесты для ранее непокрытых endpoint'ов.

Покрывает:
1. api_new_project  (POST /api/projects/new) — scaffolding + guard + run_engine=None
2. Sessions API     (GET sessions / POST session new+resume / GET session-history / GET session-context)
3. Free chats       (POST /api/free / DELETE /api/free/{id}) — create/list/delete
4. api_project_health ROUTE (GET /api/projects/{id}/health) — контракт Ф1/Ф2
5. api_project_audit + api_project_upgrade — create card + 404/409 + run_engine=None
6. _run_log_cmd timeout — unit-тест, не зависает
7. api_global_file_write  — path-traversal / .env блок / legit write

Стиль: aiohttp_client + Cookie auth — как test_board_api.py / test_webapp_smoke.py.
run_engine всегда None (деградация) — никакого реального SDK.
"""

import sys
import json
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _tasks_path


# ──────────────────────────── общие helpers / fixtures ──────────────────────


def _auth(ctx):
    """Cookie-заголовок из предварительно вычисленного токена."""
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


@pytest.fixture
def base_ctx(tmp_path):
    """Минимальный ctx для большинства route-тестов."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "secr3t"
    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
        "GROUP_CHAT_ID": 0,
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def project_ctx(tmp_path):
    """ctx с одним проектом 'myproj' в topics."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pdir = tmp_path / "myproj"
    pdir.mkdir()
    password = "secr3t"
    ctx = {
        "topics": {
            "0:1": {
                "project": "myproj",
                "cwd": str(pdir),
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
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
        "GROUP_CHAT_ID": 0,
    }
    ctx["_auth_token"] = _derive_token(password)
    ctx["_pdir"] = pdir
    return ctx


def _make_app(ctx, routes: list[tuple]):
    """Создаёт aiohttp.web.Application с auth-middleware и заданным набором роутов."""
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    return app


# ══════════════════════════════════════════════════════════════════════════════
# 1. api_new_project  ─  POST /api/projects/new
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def new_project_app(base_ctx):
    return _make_app(base_ctx, [
        ("POST", "/api/projects/new", _webapp.api_new_project),
        ("GET",  "/api/health",       _webapp.api_health),
    ])


async def test_new_project_creates_scaffolding_files(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """POST /api/projects/new (run_engine=None) → папка создана с CLAUDE.md / README.md / TASKS.md / .gitignore."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={"name": "test-proj"}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()

    # Базовые поля ответа
    assert "id" in data
    assert "cwd" in data
    assert data.get("started") is False  # run_engine=None → без агента

    cwd = Path(data["cwd"])
    assert cwd.is_dir(), "папка проекта должна существовать"
    assert (cwd / "CLAUDE.md").is_file(), "CLAUDE.md должен быть создан"
    assert (cwd / "README.md").is_file(), "README.md должен быть создан"
    assert (cwd / "TASKS.md").is_file(), "TASKS.md должен быть создан"
    assert (cwd / ".gitignore").is_file(), ".gitignore должен быть создан"


async def test_new_project_tasks_has_init_card(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """TASKS.md нового проекта содержит стартовую карточку в In Progress."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={}, headers=_auth(base_ctx))
    assert resp.status == 200
    cwd = Path((await resp.json())["cwd"])
    tasks_text = (cwd / "TASKS.md").read_text(encoding="utf-8")
    assert "<!--ops:" in tasks_text, "TASKS.md должен содержать ops-маркер стартовой карточки"


async def test_new_project_registered_in_topics(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """После создания проект регистрируется в ctx['topics'] с правильными project и cwd."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={"name": "my-new"}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()
    cwd = data["cwd"]

    # Ровно одна запись появилась в topics (base_ctx стартовал с пустым topics)
    assert len(base_ctx["topics"]) >= 1
    # Находим запись по cwd
    entry = next((v for v in base_ctx["topics"].values() if v.get("cwd") == cwd), None)
    assert entry is not None, f"topics не содержит записи с cwd={cwd!r}: {base_ctx['topics']!r}"
    assert entry["project"] == "my-new", f"project должен быть 'my-new', получили {entry['project']!r}"
    assert entry["cwd"] == cwd, f"cwd должен быть {cwd!r}, получили {entry['cwd']!r}"


async def test_new_project_no_auth_returns_401(aiohttp_client, new_project_app):
    """POST /api/projects/new без cookie → 401."""
    client = await aiohttp_client(new_project_app)
    resp = await client.post("/api/projects/new", json={"name": "x"})
    assert resp.status == 401


async def test_new_project_409_on_existing_dir(aiohttp_client, new_project_app, base_ctx, tmp_path, monkeypatch):
    """Если директория уже существует (FileExistsError) → 409."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Подменяем time.time чтобы два вызова подряд дали одинаковый slug
    import webapp

    _fixed_ts = 9999999999

    def fixed_time():
        return _fixed_ts

    monkeypatch.setattr(webapp.time, "time", fixed_time)

    # Первый вызов — создаёт директорию
    client = await aiohttp_client(new_project_app)
    resp1 = await client.post("/api/projects/new", json={}, headers=_auth(base_ctx))
    assert resp1.status == 200

    # Второй вызов с тем же timestamp → та же папка уже существует → 409
    resp2 = await client.post("/api/projects/new", json={}, headers=_auth(base_ctx))
    assert resp2.status == 409


# ══════════════════════════════════════════════════════════════════════════════
# 2. Sessions API
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sessions_app(project_ctx):
    return _make_app(project_ctx, [
        ("GET",  "/api/projects/{id}/sessions",        _webapp.api_project_sessions),
        ("POST", "/api/projects/{id}/session",          _webapp.api_project_set_session),
        ("GET",  "/api/projects/{id}/session-history",  _webapp.api_project_session_history),
        ("GET",  "/api/projects/{id}/session-context",  _webapp.api_project_session_context),
    ])


async def test_sessions_list_empty(aiohttp_client, sessions_app, project_ctx):
    """GET /api/projects/{id}/sessions без файлов → {"sessions": []}."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/sessions", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data == {"sessions": []}, f"Ожидали {{\"sessions\": []}}, получили {data!r}"


async def test_sessions_list_unknown_project_404(aiohttp_client, sessions_app, project_ctx):
    """GET /sessions для неизвестного проекта → 404."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/ghost/sessions", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_session_new_clears_active(aiohttp_client, sessions_app, project_ctx):
    """POST /session {action:new} → active=None (старая сессия сброшена)."""
    # Эмулируем наличие активной сессии
    project_ctx["sessions"]["0:1"] = "old-session-id"

    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "new"}, headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("active") is None
    assert "0:1" not in project_ctx["sessions"]


async def test_session_resume_missing_file_returns_400(aiohttp_client, sessions_app, project_ctx):
    """POST /session {action:resume, session_id:nonexistent} → 400 (файл не найден)."""
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "resume", "session_id": "nosuchsession"},
                             headers=_auth(project_ctx))
    assert resp.status == 400


async def test_session_resume_traversal_rejected(aiohttp_client, sessions_app, project_ctx):
    """POST /session {action:resume, session_id:'../evil'} → 400 (traversal sanitization)."""
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "resume", "session_id": "../evil"},
                             headers=_auth(project_ctx))
    assert resp.status == 400


async def test_session_resume_valid(aiohttp_client, sessions_app, project_ctx, tmp_path, monkeypatch):
    """POST /session {action:resume, session_id:valid} → active=session_id."""
    pdir = project_ctx["_pdir"]

    # Создаём фейковый .jsonl по пути, который вернёт _sdk_sessions_dir
    fake_sdk_dir = tmp_path / "sdk-dir"
    fake_sdk_dir.mkdir()
    (fake_sdk_dir / "abcdef123456.jsonl").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: fake_sdk_dir)

    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "resume", "session_id": "abcdef123456"},
                             headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("active") == "abcdef123456"


async def test_session_bad_action_400(aiohttp_client, sessions_app, project_ctx):
    """POST /session с неизвестным action → 400."""
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "teleport"},
                             headers=_auth(project_ctx))
    assert resp.status == 400


async def test_session_set_while_busy_409(aiohttp_client, sessions_app, project_ctx):
    """POST /session пока проект занят (running) → 409."""
    project_ctx["running"]["0:1"] = True
    client = await aiohttp_client(sessions_app)
    resp = await client.post("/api/projects/myproj/session",
                             json={"action": "new"}, headers=_auth(project_ctx))
    assert resp.status == 409


async def test_session_history_no_session(aiohttp_client, sessions_app, project_ctx):
    """GET /session-history без активной сессии → messages=[], session_id=None."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/session-history", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("messages") == []
    assert data.get("session_id") is None


async def test_session_history_with_jsonl(aiohttp_client, sessions_app, project_ctx, tmp_path, monkeypatch):
    """GET /session-history?session_id=... с реальным .jsonl → messages непустой."""
    fake_sdk_dir = tmp_path / "sdk-hist"
    fake_sdk_dir.mkdir()
    jsonl_path = fake_sdk_dir / "sess001.jsonl"
    # Минимальный SDK-транскрипт: одно user-сообщение
    entry = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "Hello Claude"},
    })
    jsonl_path.write_text(entry + "\n", encoding="utf-8")

    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: fake_sdk_dir)

    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/session-history?session_id=sess001",
                            headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("session_id") == "sess001"
    assert isinstance(data.get("messages"), list)
    assert len(data["messages"]) >= 1
    assert data["messages"][0]["role"] == "user"
    assert "Hello Claude" in data["messages"][0]["text"]


async def test_session_context_no_session(aiohttp_client, sessions_app, project_ctx):
    """GET /session-context без активной сессии → все поля пустые, session_id=None."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/session-context", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data == {"read": [], "edited": [], "commands": [], "session_id": None}, (
        f"Ожидали пустой контекст, получили {data!r}"
    )


async def test_sessions_require_auth(aiohttp_client, sessions_app):
    """GET /sessions без cookie → 401."""
    client = await aiohttp_client(sessions_app)
    resp = await client.get("/api/projects/myproj/sessions")
    assert resp.status == 401


# ══════════════════════════════════════════════════════════════════════════════
# 3. Free chats  —  POST /api/free / DELETE /api/free/{id}
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def free_app(base_ctx):
    return _make_app(base_ctx, [
        ("POST",   "/api/free",          _webapp.api_free_create),
        ("POST",   "/api/free/{id}/rename", _webapp.api_free_rename),
        ("DELETE", "/api/free/{id}",     _webapp.api_free_delete),
        ("GET",    "/api/projects",      _webapp.api_projects),
    ])


async def test_free_create_returns_free_id(aiohttp_client, free_app, base_ctx):
    """POST /api/free → id начинается с 'free-'."""
    client = await aiohttp_client(free_app)
    resp = await client.post("/api/free", json={}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data["id"].startswith("free-"), f"id должен начинаться с 'free-', получили {data['id']!r}"


async def test_free_create_persists_in_projects_list(aiohttp_client, free_app, base_ctx):
    """После создания свободного чата GET /api/projects включает его."""
    client = await aiohttp_client(free_app)
    cr = await client.post("/api/free", json={"label": "My Free Chat"}, headers=_auth(base_ctx))
    assert cr.status == 200
    fid = (await cr.json())["id"]

    resp = await client.get("/api/projects", headers=_auth(base_ctx))
    assert resp.status == 200
    projects = (await resp.json())["projects"]
    free_ids = [p["id"] for p in projects if p.get("is_free")]
    assert fid in free_ids, f"free chat {fid} должен быть в списке проектов"


async def test_free_create_with_label(aiohttp_client, free_app, base_ctx):
    """POST /api/free с label → label сохраняется."""
    client = await aiohttp_client(free_app)
    resp = await client.post("/api/free", json={"label": "Research session"}, headers=_auth(base_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("label") == "Research session"


async def test_free_delete_removes_from_list(aiohttp_client, free_app, base_ctx):
    """DELETE /api/free/{id} → ok=True, из списка пропал."""
    client = await aiohttp_client(free_app)
    cr = await client.post("/api/free", json={}, headers=_auth(base_ctx))
    fid = (await cr.json())["id"]

    del_resp = await client.delete(f"/api/free/{fid}", headers=_auth(base_ctx))
    assert del_resp.status == 200
    assert (await del_resp.json()).get("ok") is True

    list_resp = await client.get("/api/projects", headers=_auth(base_ctx))
    free_ids = [p["id"] for p in (await list_resp.json())["projects"] if p.get("is_free")]
    assert fid not in free_ids, "удалённый free chat не должен быть в списке"


async def test_free_delete_nonexistent_404(aiohttp_client, free_app, base_ctx):
    """DELETE несуществующего free chat → 404."""
    client = await aiohttp_client(free_app)
    resp = await client.delete("/api/free/free-000000ff", headers=_auth(base_ctx))
    assert resp.status == 404


async def test_free_delete_busy_409(aiohttp_client, free_app, base_ctx):
    """DELETE /api/free/{id} пока чат занят → 409."""
    client = await aiohttp_client(free_app)
    cr = await client.post("/api/free", json={}, headers=_auth(base_ctx))
    fid = (await cr.json())["id"]

    # Эмулируем занятость
    base_ctx["running"][fid] = True

    resp = await client.delete(f"/api/free/{fid}", headers=_auth(base_ctx))
    assert resp.status == 409


async def test_free_require_auth(aiohttp_client, free_app):
    """POST /api/free без cookie → 401."""
    client = await aiohttp_client(free_app)
    resp = await client.post("/api/free", json={})
    assert resp.status == 401


# ══════════════════════════════════════════════════════════════════════════════
# 4. api_project_health ROUTE  —  GET /api/projects/{id}/health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def health_app(project_ctx):
    return _make_app(project_ctx, [
        ("GET", "/api/projects/{id}/health", _webapp.api_project_health),
    ])


async def test_health_route_returns_expected_shape(aiohttp_client, health_app, project_ctx):
    """GET /api/projects/{id}/health → {items, score, total, color}."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data, "ответ должен содержать 'items'"
    assert "score" in data, "ответ должен содержать 'score'"
    assert "total" in data, "ответ должен содержать 'total'"
    assert "color" in data, "ответ должен содержать 'color'"
    assert data["color"] in ("green", "yellow", "red")
    assert isinstance(data["items"], list)
    assert isinstance(data["score"], int)
    assert isinstance(data["total"], int)


async def test_health_route_404_on_unknown_project(aiohttp_client, health_app, project_ctx):
    """GET /health для несуществующего проекта → 404."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/ghost/health", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_health_route_capability_items_present(aiohttp_client, health_app, project_ctx):
    """Capability items cap_log_cmd, cap_error_handler, cap_test_cmd присутствуют в items."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    keys = {item["key"] for item in data["items"]}
    assert "cap_log_cmd" in keys, "cap_log_cmd должен быть в items"
    assert "cap_error_handler" in keys, "cap_error_handler должен быть в items"
    assert "cap_test_cmd" in keys, "cap_test_cmd должен быть в items"


async def test_health_route_cap_test_cmd_is_optional(aiohttp_client, health_app, project_ctx):
    """cap_test_cmd должен иметь optional=True (не влияет на score)."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    cap_test = next((i for i in data["items"] if i["key"] == "cap_test_cmd"), None)
    assert cap_test is not None, "cap_test_cmd не найден"
    assert cap_test.get("optional") is True, "cap_test_cmd должен быть optional=True"


async def test_health_route_cap_log_cmd_not_optional(aiohttp_client, health_app, project_ctx):
    """cap_log_cmd НЕ должен иметь optional=True (влияет на score). Контракт Ф1."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    cap_log = next((i for i in data["items"] if i["key"] == "cap_log_cmd"), None)
    assert cap_log is not None
    assert not cap_log.get("optional"), "cap_log_cmd НЕ должен быть optional"


async def test_health_route_cap_error_handler_not_optional(aiohttp_client, health_app, project_ctx):
    """cap_error_handler НЕ должен иметь optional=True. Контракт Ф2."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    cap_eh = next((i for i in data["items"] if i["key"] == "cap_error_handler"), None)
    assert cap_eh is not None
    assert not cap_eh.get("optional"), "cap_error_handler НЕ должен быть optional"


async def test_health_route_requires_auth(aiohttp_client, health_app):
    """GET /health без cookie → 401."""
    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health")
    assert resp.status == 401


async def test_health_full_project_is_green(aiohttp_client, health_app, project_ctx):
    """Проект с CLAUDE.md / TASKS.md / README.md / .gitignore(.env) / .git
    + cockpit-rules + log_cmd + error-handler → score==total, color=='green'."""
    pdir = project_ctx["_pdir"]

    # CLAUDE.md — с разделом правил кокпита И декларацией error handler
    (pdir / "CLAUDE.md").write_text(
        "# My project\n## Правила работы в кокпите\nRules here\n"
        "\n## ClaudeOps conformance\nerror handler: app.exception_handler registered\n",
        encoding="utf-8",
    )
    tasks_text = "# Tasks\nФормат карточки: ok\n## Backlog\n## In Progress\n## Review\n## Failed\n"
    (pdir / "TASKS.md").write_text(tasks_text, encoding="utf-8")
    (pdir / "README.md").write_text("# Readme\n", encoding="utf-8")
    (pdir / ".gitignore").write_text(".env\n", encoding="utf-8")
    (pdir / ".git").mkdir()

    # Задаём log_cmd в topics (cap_log_cmd)
    project_ctx["topics"]["0:1"]["log_cmd"] = "echo hello"

    client = await aiohttp_client(health_app)
    resp = await client.get("/api/projects/myproj/health", headers=_auth(project_ctx))
    data = await resp.json()
    assert data["score"] == data["total"], (
        f"score должен равняться total, score={data['score']}, total={data['total']}, "
        f"items={[(i['key'], i['ok']) for i in data['items']]}"
    )
    assert data["color"] == "green", f"color должен быть green, получили {data['color']!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 5. api_project_audit + api_project_upgrade
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def audit_upgrade_app(project_ctx):
    return _make_app(project_ctx, [
        ("POST", "/api/projects/{id}/audit",   _webapp.api_project_audit),
        ("POST", "/api/projects/{id}/upgrade", _webapp.api_project_upgrade),
        ("GET",  "/api/projects/{id}/tasks",   _webapp.api_project_tasks),
    ])


async def test_audit_creates_card_run_engine_none(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /audit (run_engine=None) → ok=True, card_id присутствует, started=False."""
    pdir = project_ctx["_pdir"]
    # Пустая доска
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/audit", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert "card_id" in data
    assert data.get("started") is False


async def test_audit_card_appears_in_in_progress(aiohttp_client, audit_upgrade_app, project_ctx):
    """После /audit карточка с emoji '🩺' лежит в In Progress на доске."""
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    await client.post("/api/projects/myproj/audit", headers=_auth(project_ctx))

    tasks_resp = await client.get("/api/projects/myproj/tasks", headers=_auth(project_ctx))
    data = await tasks_resp.json()
    ip_col = next(c for c in data["columns"] if c["key"] == "in_progress")
    assert len(ip_col["cards"]) >= 1
    card_texts = [c.get("text", "") for c in ip_col["cards"]]
    assert any("🩺" in t for t in card_texts), (
        f"Карточка аудита должна содержать '🩺', тексты карточек: {card_texts!r}"
    )


async def test_audit_404_unknown_project(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /audit для неизвестного проекта → 404."""
    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/ghost/audit", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_audit_409_when_busy(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /audit пока проект занят → 409."""
    project_ctx["running"]["0:1"] = True
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/audit", headers=_auth(project_ctx))
    assert resp.status == 409


async def test_upgrade_creates_card_run_engine_none(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /upgrade (run_engine=None) → ok=True, card_id присутствует, started=False."""
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/upgrade", headers=_auth(project_ctx))
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert "card_id" in data
    assert data.get("started") is False


async def test_upgrade_card_appears_in_in_progress(aiohttp_client, audit_upgrade_app, project_ctx):
    """После /upgrade карточка '🔧' лежит в In Progress."""
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    await client.post("/api/projects/myproj/upgrade", headers=_auth(project_ctx))

    tasks_resp = await client.get("/api/projects/myproj/tasks", headers=_auth(project_ctx))
    ip_col = next(c for c in (await tasks_resp.json())["columns"] if c["key"] == "in_progress")
    assert len(ip_col["cards"]) >= 1
    card_texts = [c.get("text", "") for c in ip_col["cards"]]
    assert any("🔧" in t for t in card_texts), (
        f"Карточка upgrade должна содержать '🔧', тексты карточек: {card_texts!r}"
    )


async def test_upgrade_404_unknown_project(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /upgrade для неизвестного проекта → 404."""
    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/ghost/upgrade", headers=_auth(project_ctx))
    assert resp.status == 404


async def test_upgrade_409_when_busy(aiohttp_client, audit_upgrade_app, project_ctx):
    """POST /upgrade пока проект занят → 409."""
    project_ctx["running"]["0:1"] = True
    pdir = project_ctx["_pdir"]
    _tasks_path(str(pdir)).write_text("# Tasks\n## Backlog\n## In Progress\n## Review\n## Failed\n", encoding="utf-8")

    client = await aiohttp_client(audit_upgrade_app)
    resp = await client.post("/api/projects/myproj/upgrade", headers=_auth(project_ctx))
    assert resp.status == 409


# ══════════════════════════════════════════════════════════════════════════════
# 6. _run_log_cmd — timeout unit test
# ══════════════════════════════════════════════════════════════════════════════


async def test_run_log_cmd_timeout_returns_empty_string():
    """_run_log_cmd с командой дольше timeout → возвращает '' (не зависает)."""
    from webapp import _run_log_cmd

    result = await _run_log_cmd("sleep 5", timeout=0.3)
    assert result == "", f"При timeout должна возвращаться пустая строка, получили {result!r}"


async def test_run_log_cmd_fast_echo_returns_output():
    """_run_log_cmd быстрой команды → stdout возвращается."""
    from webapp import _run_log_cmd

    result = await _run_log_cmd("echo hello_log")
    assert "hello_log" in result, f"Ожидали 'hello_log' в выводе, получили {result!r}"


async def test_run_log_cmd_nonexistent_command_returns_empty():
    """_run_log_cmd несуществующей команды → '' (без исключения)."""
    from webapp import _run_log_cmd

    result = await _run_log_cmd("__no_such_cmd_xyz123__")
    assert result == "", f"Несуществующая команда должна вернуть '', получили {result!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. api_global_file_write  (POST /api/global/file)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def global_file_app(base_ctx):
    return _make_app(base_ctx, [
        ("POST", "/api/global/file", _webapp.api_global_file_write),
        ("GET",  "/api/global/file", _webapp.api_global_file),
    ])


async def test_global_file_write_legit(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Запись разрешённого файла внутри home-dir → ok=True, файл обновлён."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Создаём файл внутри home
    target = tmp_path / "notes.txt"
    target.write_text("old content", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=notes.txt",
        json={"content": "new content"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert target.read_text(encoding="utf-8") == "new content"


async def test_global_file_write_traversal_rejected(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """path traversal '../etc/passwd' в POST /api/global/file → 400."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=../etc/passwd",
        json={"content": "evil"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 400, f"Traversal должен давать 400, получили {resp.status}"


async def test_global_file_write_env_blocked(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Запись .env → 403 (блокировка секретов)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    # Создаём файл чтобы не получить 404 раньше проверки имени
    (tmp_path / ".env").write_text("SECRET=old", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=.env",
        json={"content": "SECRET=evil"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 403, f"Запись .env должна давать 403, получили {resp.status}"


async def test_global_file_write_env_production_blocked(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """Запись .env.production → 403 (любой .env* кроме .env.example)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    (tmp_path / ".env.production").write_text("KEY=old", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=.env.production",
        json={"content": "KEY=evil"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 403


async def test_global_file_write_env_example_allowed(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """.env.example — НЕ блокируется (_is_secret_name возвращает False для него)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    target = tmp_path / ".env.example"
    target.write_text("KEY=placeholder", encoding="utf-8")

    client = await aiohttp_client(global_file_app)
    resp = await client.post(
        "/api/global/file?path=.env.example",
        json={"content": "KEY=newplaceholder"},
        headers=_auth(base_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert target.read_text(encoding="utf-8") == "KEY=newplaceholder", (
        f"Файл должен содержать новый контент, получили {target.read_text(encoding='utf-8')!r}"
    )


async def test_global_file_write_requires_auth(aiohttp_client, global_file_app, tmp_path, monkeypatch):
    """POST /api/global/file без cookie → 401."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))
    client = await aiohttp_client(global_file_app)
    resp = await client.post("/api/global/file?path=notes.txt", json={"content": "x"})
    assert resp.status == 401


async def test_global_file_write_no_path_param_400(aiohttp_client, global_file_app, base_ctx, tmp_path, monkeypatch):
    """POST /api/global/file без ?path= → 400."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))
    client = await aiohttp_client(global_file_app)
    resp = await client.post("/api/global/file", json={"content": "x"}, headers=_auth(base_ctx))
    assert resp.status == 400

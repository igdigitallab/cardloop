"""
Тесты эндпоинта api_project_rename (POST /api/projects/{id}/rename).

Проверяем:
- валидный slug → 200 + перемещение папки + обновление topics.json
- невалидный slug → 400
- папка назначения уже существует → 409
- занятый проект → 409
- несуществующий проект → 404

Slug-юниты — в test_slug.py. Здесь тестируется именно эндпоинт.
"""
import sys
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "old-name"
    pdir.mkdir()
    return pdir


@pytest.fixture
def rename_ctx(tmp_path, project_dir):
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "old-name",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


@pytest.fixture
def rename_app(rename_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = rename_ctx

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_post("/api/projects/{id}/rename", _webapp.api_project_rename)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


# ─────────────────────────── тесты ───────────────────────────────


async def test_rename_valid_slug_renames_folder(aiohttp_client, rename_app, rename_ctx, project_dir):
    """Валидный slug → 200 + папка переименована."""
    new_name = "new-name"
    client = await aiohttp_client(rename_app)

    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": new_name},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("ok") is True
    assert data.get("new_name") == new_name

    # Старая папка не существует, новая существует
    old_path = project_dir
    new_path = project_dir.parent / new_name
    assert not old_path.exists(), "Старая папка должна быть удалена"
    assert new_path.exists(), "Новая папка должна существовать"


async def test_rename_updates_topics_cwd(aiohttp_client, rename_app, rename_ctx, project_dir):
    """После переименования ctx['topics'] обновлён — cwd и project обновлены."""
    client = await aiohttp_client(rename_app)

    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": "shiny-new"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 200

    # Проверяем что topics обновились
    topic = rename_ctx["topics"]["1001:42"]
    assert "shiny-new" in topic["cwd"]
    assert topic["project"] == "shiny-new"


async def test_rename_invalid_slug_returns_400(aiohttp_client, rename_app, rename_ctx):
    """Невалидный slug → 400."""
    client = await aiohttp_client(rename_app)

    for bad_slug in ["-bad", "bad-", "UPPER", "with space", "abc!def"]:
        resp = await client.post(
            "/api/projects/old-name/rename",
            json={"slug": bad_slug},
            headers=_auth_headers(rename_ctx),
        )
        assert resp.status == 400, f"Slug {bad_slug!r} должен дать 400, получили: {resp.status}"


async def test_rename_empty_slug_returns_400(aiohttp_client, rename_app, rename_ctx):
    """Пустой slug → 400."""
    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": ""},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 400


async def test_rename_target_exists_returns_409(aiohttp_client, rename_app, rename_ctx, project_dir):
    """Целевая папка уже существует → 409."""
    existing = project_dir.parent / "already-exists"
    existing.mkdir()

    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": "already-exists"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 409


async def test_rename_busy_project_returns_409(aiohttp_client, rename_app, rename_ctx):
    """Занятый проект (running[session_key] установлен) → 409."""
    rename_ctx["running"]["1001:42"] = True

    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": "new-name"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 409
    data = await resp.json()
    assert "busy" in data.get("error", "").lower()


async def test_rename_nonexistent_project_returns_404(aiohttp_client, rename_app, rename_ctx):
    """Несуществующий id → 404."""
    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/ghost-project/rename",
        json={"slug": "new-name"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 404


async def test_rename_migrates_sdk_sessions(aiohttp_client, rename_app, rename_ctx, project_dir, tmp_path, monkeypatch):
    """SDK-история диалогов (~/.claude/projects/<slug>) переносится на новый slug.

    Регрессия: без миграции после rename кокпит читает пустой новый slug —
    «пропали все сессии общения».
    """
    sdk_root = tmp_path / "claude-projects"
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir",
                        lambda cwd: sdk_root / cwd.replace("/", "-"))

    old_sdk = _webapp._sdk_sessions_dir(str(project_dir))
    old_sdk.mkdir(parents=True)
    (old_sdk / "sess-1.jsonl").write_text('{"x":1}\n', encoding="utf-8")

    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": "moved-proj"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 200

    new_cwd = project_dir.parent / "moved-proj"
    new_sdk = _webapp._sdk_sessions_dir(str(new_cwd))
    assert not old_sdk.exists(), "старый SDK-каталог должен быть перенесён"
    assert (new_sdk / "sess-1.jsonl").is_file(), "сессия должна оказаться под новым slug"
    assert (new_sdk / "sess-1.jsonl").read_text(encoding="utf-8") == '{"x":1}\n'


async def test_rename_migrates_timeline(aiohttp_client, rename_app, rename_ctx, project_dir, tmp_path, monkeypatch):
    """Timeline-лента (DATA/timeline/<slug>.jsonl + .1 backup) переносится на новый slug."""
    # Перенаправляем SDK-каталог в tmp, чтобы не трогать реальный ~/.claude
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir",
                        lambda cwd: tmp_path / "claude-projects" / cwd.replace("/", "-"))

    tdir = rename_ctx["DATA"] / "timeline"
    tdir.mkdir(parents=True, exist_ok=True)
    old_slug = str(project_dir).replace("/", "-")
    (tdir / f"{old_slug}.jsonl").write_text('{"e":1}\n', encoding="utf-8")
    (tdir / f"{old_slug}.jsonl.1").write_text('{"e":0}\n', encoding="utf-8")

    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": "tl-proj"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 200

    new_cwd = project_dir.parent / "tl-proj"
    new_slug = str(new_cwd).replace("/", "-")
    assert not (tdir / f"{old_slug}.jsonl").exists(), "старый timeline должен быть перенесён"
    assert (tdir / f"{new_slug}.jsonl").read_text(encoding="utf-8") == '{"e":1}\n'
    assert (tdir / f"{new_slug}.jsonl.1").read_text(encoding="utf-8") == '{"e":0}\n'


async def test_rename_returns_new_id_and_cwd(aiohttp_client, rename_app, rename_ctx, project_dir):
    """Ответ содержит new_id, new_cwd, new_name."""
    client = await aiohttp_client(rename_app)
    resp = await client.post(
        "/api/projects/old-name/rename",
        json={"slug": "renamed-proj"},
        headers=_auth_headers(rename_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data.get("new_id") == "renamed-proj"
    assert data.get("new_name") == "renamed-proj"
    assert "renamed-proj" in data.get("new_cwd", "")

"""
Тесты синхронизации forum-топика Telegram с проектом кокпита.

Покрывают флоу:
- создание нового проекта → автоматически создаётся forum-топик (create_forum_topic),
  thread_id становится ключом сессии;
- переименование проекта в вебе → имя топика синкается (edit_forum_topic);
- граничные случаи _sync_forum_topic_name: синтетический ключ / нет ptb → no-op.

Unit-тесты на сам slug — в test_slug.py; на миграцию сессий при rename — в test_project_rename.py.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── фейковый PTB-бот ───────────────────────────


class _FakeBot:
    def __init__(self, new_thread_id: int = 999777):
        self.edit_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._new_thread_id = new_thread_id

    async def edit_forum_topic(self, chat_id, message_thread_id, name):
        self.edit_calls.append({"chat_id": chat_id, "thread": message_thread_id, "name": name})

    async def create_forum_topic(self, chat_id, name):
        self.create_calls.append({"chat_id": chat_id, "name": name})

        class _Topic:
            message_thread_id = self._new_thread_id

        return _Topic()


class _FakePTB:
    def __init__(self, new_thread_id: int = 999777):
        self.bot = _FakeBot(new_thread_id)


# ───────────────── unit: _sync_forum_topic_name ─────────────────


async def test_sync_real_key_calls_edit_forum_topic():
    """Реальный ключ chat:thread → edit_forum_topic с (chat, thread, name)."""
    ptb = _FakePTB()
    ctx = {"ptb_app": ptb}
    await _webapp._sync_forum_topic_name(ctx, "-100123:42", "family-emergency")
    assert ptb.bot.edit_calls == [
        {"chat_id": -100123, "thread": 42, "name": "family-emergency"}
    ]


@pytest.mark.parametrize("bad_key", ["-100123:0", "-100123:abc", "synthetic-no-colon", "", "1756000000"])
async def test_sync_synthetic_key_no_edit(bad_key):
    """Синтетический ключ (нет топика / thread не число / 0) → edit НЕ вызывается."""
    ptb = _FakePTB()
    ctx = {"ptb_app": ptb}
    await _webapp._sync_forum_topic_name(ctx, bad_key, "whatever")
    assert ptb.bot.edit_calls == []


async def test_sync_no_ptb_app_noop():
    """Нет ptb_app в ctx → тихий no-op, без исключения."""
    await _webapp._sync_forum_topic_name({"ptb_app": None}, "-100123:42", "name")


async def test_sync_swallows_edit_errors():
    """Ошибка edit_forum_topic не пробрасывается (некритичная операция)."""
    class _BoomBot:
        async def edit_forum_topic(self, **kw):
            raise RuntimeError("topic deleted")

    class _BoomPTB:
        bot = _BoomBot()

    # не должно бросить
    await _webapp._sync_forum_topic_name({"ptb_app": _BoomPTB()}, "-100123:42", "name")


# ───────────────── HTTP: rename синкает имя топика ─────────────────


@pytest.fixture
def ft_ctx(tmp_path):
    password = "testpass"
    pdir = tmp_path / "old-name"
    pdir.mkdir()
    ctx = {
        "topics": {"-100500:7": {"project": "old-name", "cwd": str(pdir), "model": "sonnet"}},
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "DEFAULT_MODEL": "sonnet",
        "GROUP_CHAT_ID": -100500,
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": _FakePTB(),
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    ctx["_project_dir"] = pdir
    return ctx


@pytest.fixture
def ft_app(ft_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ft_ctx
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_post("/api/projects/new", _webapp.api_new_project)
    app.router.add_post("/api/projects/{id}/rename", _webapp.api_project_rename)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_rename_syncs_forum_topic_name(aiohttp_client, ft_app, ft_ctx, monkeypatch):
    """Переименование проекта → edit_forum_topic вызван с новым именем и верным thread."""
    # SDK-каталог в tmp, чтобы миграция не трогала реальный ~/.claude
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir",
                        lambda cwd: ft_ctx["DATA"] / "sdk" / cwd.replace("/", "-"))

    client = await aiohttp_client(ft_app)
    resp = await client.post("/api/projects/old-name/rename",
                             json={"slug": "family-emergency"}, headers=_auth(ft_ctx))
    assert resp.status == 200

    edits = ft_ctx["ptb_app"].bot.edit_calls
    assert edits == [{"chat_id": -100500, "thread": 7, "name": "family-emergency"}]


# ───────────────── HTTP: новый проект создаёт forum-топик ─────────────────
# spec-046 Phase D: TG forum topic creation removed from api_new_project.
# These tests verify old Phase B/C behaviour and are now skipped.


@pytest.mark.skip(reason="spec-046 Phase D: TG forum topic creation removed from api_new_project")
async def test_new_project_creates_forum_topic(aiohttp_client, ft_app, ft_ctx, tmp_path, monkeypatch):
    """POST /api/projects/new → create_forum_topic вызван, thread_id → ключ сессии в topics."""
    # ~/projects → tmp, чтобы не плодить реальные папки
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(ft_app)
    resp = await client.post("/api/projects/new", json={}, headers=_auth(ft_ctx))
    assert resp.status == 200
    data = await resp.json()

    # топик создан
    creates = ft_ctx["ptb_app"].bot.create_calls
    assert len(creates) == 1
    assert creates[0]["chat_id"] == -100500

    # папка untitled-<ts> создана под tmp/projects
    assert data["name"].startswith("untitled-")
    assert (tmp_path / "projects" / data["name"]).is_dir()

    # spec-040 Phase 0: session key is now the slug (project name), not chat:thread.
    # The original TG chat:thread is stored in the topic entry's "tg_key" field.
    slug = data["name"]
    assert slug in ft_ctx["topics"], f"slug {slug!r} not found in topics {list(ft_ctx['topics'].keys())}"
    topic_entry = ft_ctx["topics"][slug]
    assert topic_entry["cwd"].endswith(data["name"])
    # tg_key stores the original chat:thread for TG reverse lookup
    expected_tg_key = f"-100500:{ft_ctx['ptb_app'].bot._new_thread_id}"
    assert topic_entry.get("tg_key") == expected_tg_key


@pytest.mark.skip(reason="spec-046 Phase D: TG forum topic creation removed from api_new_project")
async def test_new_project_named_uses_name_for_topic(aiohttp_client, ft_app, ft_ctx, tmp_path, monkeypatch):
    """Если имя передано — топик создаётся сразу с этим именем (не плейсхолдер)."""
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))

    client = await aiohttp_client(ft_app)
    resp = await client.post("/api/projects/new", json={"name": "example-project"}, headers=_auth(ft_ctx))
    assert resp.status == 200

    creates = ft_ctx["ptb_app"].bot.create_calls
    assert len(creates) == 1
    assert creates[0]["name"] == "example-project"

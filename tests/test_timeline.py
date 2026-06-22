"""
Тесты Timeline (Spec 008) — персистентность шины событий.

Покрывает:
- _timeline_path: стабильный slug из cwd; fallback на _unknown для неизвестного session_key
- _timeline_append: пишет JSONL, добавляет ts, обрезает text >2000, не пишет env
- ротация при >5MB: rename → .jsonl.1, продолжает запись в новый файл
- _bus_publish → запись в timeline (интеграция)
- эндпоинт GET: 200 + события, пагинация before/limit, 404 для несуществующего проекта
- битая строка в JSONL не валит чтение (graceful)
- env НЕ попадает в timeline-запись (даже если передать в событие)
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _timeline_path,
    _timeline_append,
    _timeline_init,
    _timeline_slug_from_cwd,
    _timeline_read_events,
    _bus_publish,
    _derive_token,
    api_project_timeline,
    _TIMELINE_TEXT_LIMIT,
)

# ─────────────────────────── helpers ──────────────────────────────────────────

def _reset_timeline_state(data_dir: Path, topics: dict) -> None:
    """Инициализирует модульное состояние timeline для каждого теста."""
    ctx = {
        "DATA": data_dir,
        "topics": topics,
    }
    _timeline_init(ctx)


# ─────────────────────────── unit: slug ───────────────────────────────────────

class TestTimelineSlug:
    def test_slug_replaces_slashes(self):
        """/home/youruser/myproject → -home-youruser-myproject."""
        slug = _timeline_slug_from_cwd("/home/youruser/myproject")
        assert slug == "-home-youruser-myproject"

    def test_slug_stable(self):
        """Тот же cwd → тот же slug при повторном вызове."""
        cwd = "/home/youruser/some-project"
        assert _timeline_slug_from_cwd(cwd) == _timeline_slug_from_cwd(cwd)

    def test_slug_no_path_components(self):
        """Slug не содержит '/' (нет path-traversal через имя файла)."""
        slug = _timeline_slug_from_cwd("/home/youruser/my/nested/project")
        assert "/" not in slug

    def test_slug_basename_project(self):
        """Basename часть присутствует в slug."""
        slug = _timeline_slug_from_cwd("/home/youruser/claude-ops-bot")
        assert "claude-ops-bot" in slug


# ─────────────────────────── unit: _timeline_path ─────────────────────────────

class TestTimelinePath:
    def test_path_known_session(self, tmp_path):
        """Известный session_key → path по cwd проекта."""
        cwd = str(tmp_path / "myproject")
        topics = {"42:100": {"project": "myproject", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("42:100")
        assert p is not None
        slug = _timeline_slug_from_cwd(cwd)
        assert p.name == f"{slug}.jsonl"

    def test_path_unknown_session_fallback(self, tmp_path):
        """Неизвестный session_key → _unknown или session-slug (не None, не падает)."""
        _reset_timeline_state(tmp_path / "data", {})
        p = _timeline_path("unknown:999")
        assert p is not None
        # Допустимые имена: либо _unknown.jsonl, либо slug из session_key
        assert p.suffix == ".jsonl"

    def test_path_returns_none_before_init(self):
        """До _timeline_init → возвращает None (не падает)."""
        original = _webapp._TIMELINE_DATA_DIR
        try:
            _webapp._TIMELINE_DATA_DIR = None
            result = _timeline_path("any:key")
            assert result is None
        finally:
            _webapp._TIMELINE_DATA_DIR = original

    def test_path_in_data_timeline_dir(self, tmp_path):
        """Path всегда лежит в DATA/timeline/."""
        data = tmp_path / "data"
        cwd = str(tmp_path / "proj")
        topics = {"1:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(data, topics)

        p = _timeline_path("1:1")
        assert p is not None
        assert p.parent == data / "timeline"


# ─────────────────────────── unit: _timeline_append ──────────────────────────

class TestTimelineAppend:
    def setup_method(self):
        """Настройка вызывается перед каждым тестом через pytest."""

    def test_append_creates_file(self, tmp_path):
        """_timeline_append создаёт JSONL-файл при первой записи."""
        cwd = str(tmp_path / "proj")
        topics = {"10:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:1", {"kind": "run_start", "run_id": "abc"})

        p = _timeline_path("10:1")
        assert p is not None and p.exists()

    def test_append_writes_valid_jsonl(self, tmp_path):
        """Каждая строка — валидный JSON."""
        cwd = str(tmp_path / "proj")
        topics = {"10:2": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:2", {"kind": "run_start", "run_id": "x1"})
        _timeline_append("10:2", {"kind": "text", "text": "hello"})

        p = _timeline_path("10:2")
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "kind" in obj

    def test_append_adds_ts(self, tmp_path):
        """Запись содержит поле ts (timestamp)."""
        cwd = str(tmp_path / "proj")
        topics = {"10:3": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        before = time.time()
        _timeline_append("10:3", {"kind": "run_end"})
        after = time.time()

        p = _timeline_path("10:3")
        obj = json.loads(p.read_text().strip())
        assert "ts" in obj
        assert before <= obj["ts"] <= after

    def test_append_truncates_text_field(self, tmp_path):
        """Длинный text обрезается до _TIMELINE_TEXT_LIMIT симв."""
        cwd = str(tmp_path / "proj")
        topics = {"10:4": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        long_text = "A" * (_TIMELINE_TEXT_LIMIT + 500)
        _timeline_append("10:4", {"kind": "text", "text": long_text})

        p = _timeline_path("10:4")
        obj = json.loads(p.read_text().strip())
        assert len(obj["text"]) <= _TIMELINE_TEXT_LIMIT + 1  # +1 for ellipsis char
        assert obj["text"].endswith("…")

    def test_append_short_text_not_truncated(self, tmp_path):
        """Короткий text не обрезается."""
        cwd = str(tmp_path / "proj")
        topics = {"10:5": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:5", {"kind": "text", "text": "short"})
        p = _timeline_path("10:5")
        obj = json.loads(p.read_text().strip())
        assert obj["text"] == "short"

    def test_append_excludes_env_field(self, tmp_path):
        """Поле env никогда не попадает в запись."""
        cwd = str(tmp_path / "proj")
        topics = {"10:6": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _timeline_append("10:6", {"kind": "run_start", "env": {"SECRET": "s3cr3t!"}})

        p = _timeline_path("10:6")
        obj = json.loads(p.read_text().strip())
        assert "env" not in obj
        assert "s3cr3t!" not in p.read_text()

    def test_append_no_crash_if_not_init(self):
        """_timeline_append молча возвращает None если DATA не инициализирован."""
        original = _webapp._TIMELINE_DATA_DIR
        try:
            _webapp._TIMELINE_DATA_DIR = None
            # Не должно бросать
            _timeline_append("any:key", {"kind": "text"})
        finally:
            _webapp._TIMELINE_DATA_DIR = original

    def test_rotation_at_5mb(self, tmp_path):
        """Ротация: файл >5MB → переименовывается в .jsonl.1, продолжает писать в новый."""
        cwd = str(tmp_path / "proj")
        topics = {"20:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("20:1")
        assert p is not None

        # Создаём файл > 5MB
        p.parent.mkdir(parents=True, exist_ok=True)
        big_content = ("x" * 1023 + "\n") * (5 * 1024 + 10)  # чуть > 5MB
        p.write_text(big_content, encoding="utf-8")
        assert p.stat().st_size > 5 * 1024 * 1024

        # Добавляем событие — должна произойти ротация
        _timeline_append("20:1", {"kind": "run_end"})

        backup = p.with_suffix(".jsonl.1")
        assert backup.exists(), "backup .jsonl.1 должен существовать после ротации"
        # Основной файл должен содержать только новое событие
        assert p.exists()
        new_content = p.read_text()
        assert "run_end" in new_content
        # Размер нового файла много меньше 5MB
        assert p.stat().st_size < 5 * 1024 * 1024

    def test_rotation_overwrites_old_backup(self, tmp_path):
        """Старый .jsonl.1 перезаписывается при новой ротации."""
        cwd = str(tmp_path / "proj")
        topics = {"20:2": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("20:2")
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = p.with_suffix(".jsonl.1")

        # Создаём старый backup
        backup.write_text("old backup content\n", encoding="utf-8")

        # Создаём файл > 5MB
        big_content = ("y" * 1023 + "\n") * (5 * 1024 + 10)
        p.write_text(big_content, encoding="utf-8")

        _timeline_append("20:2", {"kind": "text"})

        # Старый backup перезаписан
        assert "old backup content" not in backup.read_text()


# ─────────────────────────── integration: _bus_publish → timeline ─────────────

class TestBusPublishIntegration:
    def test_bus_publish_writes_to_timeline(self, tmp_path):
        """_bus_publish автоматически персистирует событие в timeline."""
        cwd = str(tmp_path / "proj")
        topics = {"5:10": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _bus_publish("5:10", {"kind": "run_start", "run_id": "card-x"})

        p = _timeline_path("5:10")
        assert p is not None and p.exists()
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        obj = json.loads(lines[0])
        assert obj["kind"] == "run_start"
        assert obj["run_id"] == "card-x"
        assert "ts" in obj

    def test_bus_publish_multiple_events(self, tmp_path):
        """Несколько _bus_publish → несколько строк в JSONL."""
        cwd = str(tmp_path / "proj")
        topics = {"5:11": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        _bus_publish("5:11", {"kind": "run_start"})
        _bus_publish("5:11", {"kind": "text", "text": "Hello"})
        _bus_publish("5:11", {"kind": "run_end", "outcome": "ok"})

        p = _timeline_path("5:11")
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == 3
        kinds = [json.loads(l)["kind"] for l in lines]
        assert kinds == ["run_start", "text", "run_end"]


# ─────────────────────────── unit: _timeline_read_events ─────────────────────

class TestTimelineReadEvents:
    def test_read_empty_returns_empty(self, tmp_path):
        """Нет файла → пустой список."""
        _reset_timeline_state(tmp_path / "data", {})
        events = _timeline_read_events("nonexistent:key", 200, None)
        assert events == []

    def test_read_events_chronological(self, tmp_path):
        """События возвращаются в хронологическом порядке (новые внизу)."""
        cwd = str(tmp_path / "proj")
        topics = {"30:1": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        # Пишем с возрастающими ts
        for i in range(5):
            _timeline_append("30:1", {"kind": "text", "ts_order": i})

        events = _timeline_read_events("30:1", 200, None)
        assert len(events) == 5
        ts_values = [e["ts"] for e in events]
        assert ts_values == sorted(ts_values), "Должны быть в хронологическом порядке"

    def test_read_limit(self, tmp_path):
        """limit=3 → не более 3 последних событий."""
        cwd = str(tmp_path / "proj")
        topics = {"30:2": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        for _ in range(10):
            _timeline_append("30:2", {"kind": "text", "text": "x"})

        events = _timeline_read_events("30:2", 3, None)
        assert len(events) == 3

    def test_read_before_pagination(self, tmp_path):
        """before=<ts> → только события со ts < before."""
        cwd = str(tmp_path / "proj")
        topics = {"30:3": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        for i in range(5):
            _timeline_append("30:3", {"kind": "text"})

        events_all = _timeline_read_events("30:3", 200, None)
        # Берём ts 3-го события (с начала)
        cutoff_ts = events_all[2]["ts"]
        events_before = _timeline_read_events("30:3", 200, cutoff_ts)
        # Должны получить только события со ts < cutoff_ts
        assert all(e["ts"] < cutoff_ts for e in events_before)
        assert len(events_before) <= 2  # может совпадать по ts — допускаем ≤ 2

    def test_read_graceful_broken_line(self, tmp_path):
        """Битая строка JSONL не валит чтение — просто пропускается."""
        cwd = str(tmp_path / "proj")
        topics = {"30:4": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("30:4")
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        # Одна нормальная + одна битая + одна нормальная строка
        p.write_text(
            '{"ts": 1.0, "kind": "run_start"}\n'
            'THIS IS NOT JSON %%!!@@\n'
            '{"ts": 2.0, "kind": "run_end"}\n',
            encoding="utf-8",
        )

        events = _timeline_read_events("30:4", 200, None)
        # Битая строка пропущена — получаем 2 события
        assert len(events) == 2
        kinds = [e["kind"] for e in events]
        assert "run_start" in kinds
        assert "run_end" in kinds

    def test_read_includes_backup_file(self, tmp_path):
        """События читаются из .jsonl.1 (backup) + текущего файла."""
        cwd = str(tmp_path / "proj")
        topics = {"30:5": {"project": "proj", "cwd": cwd}}
        _reset_timeline_state(tmp_path / "data", topics)

        p = _timeline_path("30:5")
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = p.with_suffix(".jsonl.1")

        # Старые события в backup
        backup.write_text('{"ts": 1.0, "kind": "run_start"}\n', encoding="utf-8")
        # Новые в текущем
        p.write_text('{"ts": 2.0, "kind": "run_end"}\n', encoding="utf-8")

        events = _timeline_read_events("30:5", 200, None)
        assert len(events) == 2
        assert events[0]["kind"] == "run_start"  # backup первый
        assert events[1]["kind"] == "run_end"


# ─────────────────────────── API endpoint tests ───────────────────────────────

@pytest.fixture
def fake_ctx_with_project(tmp_path):
    """ctx с одним проектом и настроенным timeline."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

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
        "VAULT_PROJECTS": tmp_path / "vault",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    # Инициализируем timeline
    _timeline_init(ctx)
    return ctx


@pytest.fixture
def timeline_app(fake_ctx_with_project):
    """aiohttp-приложение с маршрутами timeline."""
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project

    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_post("/api/login", _webapp.api_login)
    app.router.add_get("/api/projects/{id}/timeline", _webapp.api_project_timeline)

    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_api_timeline_empty(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline с пустым логом → 200, events:[]."""
    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert "events" in data
    assert data["events"] == []


async def test_api_timeline_not_found(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline несуществующего проекта → 404."""
    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/nonexistent/timeline", headers=h)
    assert resp.status == 404


async def test_api_timeline_unauthorized(aiohttp_client, timeline_app):
    """GET /timeline без авторизации → 401."""
    client = await aiohttp_client(timeline_app)
    resp = await client.get("/api/projects/myproject/timeline")
    assert resp.status == 401


async def test_api_timeline_returns_events(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline возвращает события из JSONL-лога."""
    # Публикуем события в шину (они автоматически пишутся в timeline)
    _bus_publish("1001:42", {"kind": "run_start", "run_id": "card-abc"})
    _bus_publish("1001:42", {"kind": "run_end", "outcome": "ok", "run_id": "card-abc"})

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline", headers=h)
    assert resp.status == 200
    data = await resp.json()
    events = data["events"]
    assert len(events) >= 2
    kinds = [e["kind"] for e in events]
    assert "run_start" in kinds
    assert "run_end" in kinds


async def test_api_timeline_limit_param(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline?limit=1 → не более 1 события."""
    for _ in range(5):
        _bus_publish("1001:42", {"kind": "text", "text": "x"})

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline?limit=1", headers=h)
    assert resp.status == 200
    data = await resp.json()
    assert len(data["events"]) <= 1


async def test_api_timeline_before_param(aiohttp_client, timeline_app, fake_ctx_with_project):
    """GET /timeline?before=<ts> → только события раньше ts."""
    _bus_publish("1001:42", {"kind": "run_start", "run_id": "r1"})
    # Небольшая пауза для разных ts
    import asyncio
    await asyncio.sleep(0.01)
    cutoff = time.time()
    await asyncio.sleep(0.01)
    _bus_publish("1001:42", {"kind": "run_end", "run_id": "r1"})

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get(f"/api/projects/myproject/timeline?before={cutoff}", headers=h)
    assert resp.status == 200
    data = await resp.json()
    # Все события должны быть до cutoff
    for e in data["events"]:
        assert e["ts"] < cutoff


async def test_api_timeline_env_not_in_response(aiohttp_client, timeline_app, fake_ctx_with_project):
    """env-поле не должно попадать в ответ эндпоинта (даже если было в событии)."""
    # Публикуем событие с env — _timeline_append должен отфильтровать
    _bus_publish("1001:42", {
        "kind": "run_start",
        "run_id": "env-test",
        "env": {"SECRET_KEY": "SUPER_SECRET_DO_NOT_LEAK"},
    })

    client = await aiohttp_client(timeline_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/timeline", headers=h)
    assert resp.status == 200

    resp_text = await resp.text()
    assert "SUPER_SECRET_DO_NOT_LEAK" not in resp_text, \
        "Secret leaked into timeline API response!"
    data = await resp.json()
    for e in data["events"]:
        assert "env" not in e, "env field must never appear in timeline events"

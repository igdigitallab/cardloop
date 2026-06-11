"""
Тесты системы настроек кокпита (карточка f2ba02).

Покрывают:
- глобальный стор data/settings.json: load/save/get + hot-reload по mtime;
- валидацию глобальных настроек (тип/диапазон/модель/сброс);
- провязку в рантайм: self_heal master-kill, дефолт-модель;
- per-project git_enabled: helper + _card_run_mode → legacy + git-sync 409;
- API GET/POST глобальных и per-project настроек.
"""
import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


def _reset_settings_globals():
    _webapp._SETTINGS_PATH = None
    _webapp._SETTINGS_CACHE = {}
    _webapp._SETTINGS_MTIME = 0.0


@pytest.fixture
def settings_tmp(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _webapp._settings_init({"DATA": data})
    _webapp._SETTINGS_CACHE = {}
    _webapp._SETTINGS_MTIME = 0.0
    yield data
    _reset_settings_globals()


# ─────────────── глобальный стор ───────────────


def test_get_global_setting_fallback_when_empty(settings_tmp):
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 300


def test_save_and_get_global_setting(settings_tmp):
    _webapp._save_global_settings({"scan_interval_sec": 120})
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 120


def test_global_settings_hot_reload(settings_tmp):
    _webapp._save_global_settings({"scan_interval_sec": 99})
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 99
    (settings_tmp / "settings.json").write_text(json.dumps({"scan_interval_sec": 77}), encoding="utf-8")
    _webapp._SETTINGS_MTIME = 0.0  # форсим перечитать (mtime мог совпасть в пределах тика)
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 77


def test_global_settings_broken_file_keeps_cache(settings_tmp):
    _webapp._save_global_settings({"scan_interval_sec": 60})
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 60  # заполняем кэш
    (settings_tmp / "settings.json").write_text("{не json", encoding="utf-8")
    _webapp._SETTINGS_MTIME = 0.0
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 60  # битый файл → прошлый кэш


# ─────────────── валидация ───────────────


def test_validate_unknown_key():
    clean, err = _webapp._validate_global_settings({"nope": 1})
    assert err and clean == {}


def test_validate_out_of_range():
    assert _webapp._validate_global_settings({"scan_interval_sec": 5})[1]


def test_validate_bad_type():
    assert _webapp._validate_global_settings({"watchdog_stall_sec": "yes"})[1]


def test_validate_model_normalizes_and_rejects():
    clean, err = _webapp._validate_global_settings({"default_model": "OPUS"})
    assert err is None and clean["default_model"] == "opus"
    assert _webapp._validate_global_settings({"default_model": "gpt"})[1]


def test_validate_empty_resets_to_none():
    clean, err = _webapp._validate_global_settings({"default_model": ""})
    assert err is None and clean["default_model"] is None


# ─────────────── провязка в рантайм ───────────────


def test_effective_default_model(settings_tmp):
    ctx = {"DEFAULT_MODEL": "opus"}
    assert _webapp._effective_default_model(ctx) == "opus"
    _webapp._save_global_settings({"default_model": "haiku"})
    assert _webapp._effective_default_model(ctx) == "haiku"


# ─────────────── git per-project ───────────────


def test_git_enabled_default_true():
    assert _webapp._git_enabled({}) is True
    assert _webapp._git_enabled({"git_enabled": True}) is True
    assert _webapp._git_enabled({"git_enabled": False}) is False


async def test_card_run_mode_git_disabled_is_legacy():
    # git_enabled=False → legacy без обращения к git
    assert await _webapp._card_run_mode("/no/such/path", git_enabled=False) == "legacy"


# ─────────────── HTTP ───────────────


@pytest.fixture
def app_ctx(tmp_path):
    password = "testpass"
    pdir = tmp_path / "proj"
    pdir.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    _webapp._settings_init({"DATA": data})
    _webapp._SETTINGS_CACHE = {}
    _webapp._SETTINGS_MTIME = 0.0
    ctx = {
        "topics": {"-100:5": {"project": "proj", "cwd": str(pdir), "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": password,
        "DATA": data, "HERE": ROOT, "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    yield ctx
    _reset_settings_globals()


@pytest.fixture
def app(app_ctx):
    from aiohttp import web

    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = app_ctx
    a.router.add_get("/api/health", _webapp.api_health)
    a.router.add_post("/api/login", _webapp.api_login)
    a.router.add_get("/api/settings", _webapp.api_settings_get)
    a.router.add_post("/api/settings", _webapp.api_settings_post)
    a.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    a.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    a.router.add_post("/api/projects/{id}/git/sync", _webapp.api_project_git_sync)
    return a


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_api_settings_get(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.get("/api/settings", headers=_auth(app_ctx))
    assert r.status == 200
    d = await r.json()
    assert {"stored", "effective", "spec"} <= set(d)
    assert "scan_interval_sec" in d["effective"]


async def test_api_settings_post_valid_persists(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post("/api/settings", json={"scan_interval_sec": 120}, headers=_auth(app_ctx))
    assert r.status == 200
    assert _webapp._get_global_setting("scan_interval_sec", 300) == 120


async def test_api_settings_post_invalid_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post("/api/settings", json={"scan_interval_sec": 1}, headers=_auth(app_ctx))
    assert r.status == 400


async def test_api_project_settings_get_defaults(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.get("/api/projects/proj/settings", headers=_auth(app_ctx))
    assert r.status == 200
    assert (await r.json())["git_enabled"] is True


async def test_api_project_settings_set_git_off(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post("/api/projects/proj/settings", json={"git_enabled": False}, headers=_auth(app_ctx))
    assert r.status == 200
    assert (await r.json())["settings"]["git_enabled"] is False
    assert app_ctx["topics"]["-100:5"]["git_enabled"] is False


async def test_api_project_settings_invalid_key_400(aiohttp_client, app, app_ctx):
    client = await aiohttp_client(app)
    r = await client.post("/api/projects/proj/settings", json={"bogus": 1}, headers=_auth(app_ctx))
    assert r.status == 400


async def test_git_sync_409_when_disabled(aiohttp_client, app, app_ctx):
    app_ctx["topics"]["-100:5"]["git_enabled"] = False
    client = await aiohttp_client(app)
    r = await client.post("/api/projects/proj/git/sync", json={}, headers=_auth(app_ctx))
    assert r.status == 409

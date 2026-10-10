"""Project health — HTTP surface, acknowledge flow, fleet sweep, once-a-day push, module wiring."""
import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp  # noqa: E402
from webapp import _derive_token  # noqa: E402
from features.project_health import logic as L  # noqa: E402
from features.project_health import loop as LP  # noqa: E402


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture(autouse=True)
def _fresh_state():
    LP._cache.clear()
    LP._last_sweep.update({"at": None, "results": {}})
    yield
    LP._cache.clear()
    LP._last_sweep.update({"at": None, "results": {}})


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("GLOBAL_CLAUDE_MD", raising=False)
    return h


@pytest.fixture
def project_dir(home):
    p = home / "myproject"
    p.mkdir()
    return p


@pytest.fixture
def ctx(tmp_path, home, project_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    c = {
        "topics": {"1001:42": {"project": "myproject", "cwd": str(project_dir), "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": password, "DATA": data_dir, "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects", "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    c["_auth_token"] = _derive_token(password)
    return c


@pytest.fixture
def app(ctx):
    from aiohttp import web
    from features.project_health.routes import add_routes
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = ctx
    add_routes(a)
    return a


def _h(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def native_memory(project_dir: Path) -> Path:
    d = _webapp._sdk_sessions_dir(str(project_dir)) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hooks_file(project_dir: Path, payload=None) -> bytes:
    f = project_dir / ".claude" / "settings.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload or {"hooks": {"PreToolUse": [{"command": "echo hi"}]}}).encode()
    f.write_bytes(raw)
    return raw


def ids(res):
    return [f["id"] for f in res["findings"]]


# ─────────────────────────── GET /api/projects/{id}/health-check ───────────────────────────


async def test_requires_auth(aiohttp_client, app):
    client = await aiohttp_client(app)
    assert (await client.get("/api/projects/myproject/health-check")).status == 401
    assert (await client.post("/api/projects/myproject/health-check/ack", json={})).status == 401
    assert (await client.get("/api/health-check")).status == 401


async def test_unknown_project_is_404(aiohttp_client, app, ctx):
    client = await aiohttp_client(app)
    assert (await client.get("/api/projects/nope/health-check", headers=_h(ctx))).status == 404
    assert (await client.post("/api/projects/nope/health-check/ack", json={}, headers=_h(ctx))).status == 404


async def test_healthy_project_returns_an_empty_list(aiohttp_client, app, ctx):
    client = await aiohttp_client(app)
    resp = await client.get("/api/projects/myproject/health-check", headers=_h(ctx))
    body = await resp.json()
    assert resp.status == 200
    assert body["findings"] == [] and body["errors"] == [] and body["project_id"] == "myproject"


async def test_findings_flow_through_the_api(aiohttp_client, app, ctx, project_dir):
    (native_memory(project_dir) / "MEMORY.md").write_text("\n".join(["x"] * 198) + "\n")
    hooks_file(project_dir)
    client = await aiohttp_client(app)
    body = await (await client.get("/api/projects/myproject/health-check", headers=_h(ctx))).json()
    assert sorted(ids(body)) == ["memory_index_near_cap", "project_settings_untrusted"]
    assert body["findings"][0]["severity"] == "crit"


async def test_result_is_cached_and_fresh_bypasses_the_cache(aiohttp_client, app, ctx, project_dir):
    idx = native_memory(project_dir) / "MEMORY.md"
    idx.write_text("\n".join(["x"] * 198) + "\n")
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check"
    first = await (await client.get(url, headers=_h(ctx))).json()
    assert ids(first) == ["memory_index_near_cap"]
    idx.write_text("short\n")                                  # healed behind the cache's back
    cached = await (await client.get(url, headers=_h(ctx))).json()
    assert ids(cached) == ["memory_index_near_cap"] and cached["checked_at"] == first["checked_at"]
    fresh = await (await client.get(url + "?fresh=1", headers=_h(ctx))).json()
    assert fresh["findings"] == []
    after = await (await client.get(url, headers=_h(ctx))).json()      # fresh refilled the cache
    assert after["findings"] == []


async def test_cache_expires_after_its_ttl(aiohttp_client, app, ctx, project_dir, monkeypatch):
    idx = native_memory(project_dir) / "MEMORY.md"
    idx.write_text("\n".join(["x"] * 198) + "\n")
    monkeypatch.setattr(LP, "CACHE_TTL_SEC", -1)             # every entry is born expired
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check"
    assert ids(await (await client.get(url, headers=_h(ctx))).json()) == ["memory_index_near_cap"]
    idx.write_text("short\n")
    assert (await (await client.get(url, headers=_h(ctx))).json())["findings"] == []   # re-ran, not served stale


async def test_cached_only_never_runs_the_checks(aiohttp_client, app, ctx, project_dir, monkeypatch):
    (native_memory(project_dir) / "MEMORY.md").write_text("\n".join(["x"] * 198) + "\n")
    calls = []
    real = L.run_checks
    monkeypatch.setattr(L, "run_checks", lambda *a, **k: calls.append(1) or real(*a, **k))
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check?cached=1"
    empty = await (await client.get(url, headers=_h(ctx))).json()
    assert empty["findings"] == [] and empty["checked_at"] is None and calls == []
    await client.get("/api/projects/myproject/health-check", headers=_h(ctx))
    assert calls == [1]
    warm = await (await client.get(url, headers=_h(ctx))).json()
    assert ids(warm) == ["memory_index_near_cap"] and calls == [1]


async def test_checks_run_off_the_event_loop_thread(aiohttp_client, app, ctx, monkeypatch):
    seen = []
    real = L.run_checks
    monkeypatch.setattr(L, "run_checks", lambda *a, **k: seen.append(threading.current_thread()) or real(*a, **k))
    client = await aiohttp_client(app)
    await client.get("/api/projects/myproject/health-check?fresh=1", headers=_h(ctx))
    assert seen and seen[0] is not threading.main_thread()


async def test_free_chat_is_never_checked(aiohttp_client, app, ctx, tmp_path):
    (ctx["DATA"] / "free_chats.json").write_text(json.dumps({"free-abc": {"label": "scratch", "created_at": 1}}))
    client = await aiohttp_client(app)
    resp = await client.get("/api/projects/free-abc/health-check", headers=_h(ctx))
    assert resp.status == 200 and (await resp.json())["findings"] == []


# ─────────────────────────── acknowledge flow ───────────────────────────


async def test_ack_silences_until_the_file_changes(aiohttp_client, app, ctx, project_dir):
    raw = hooks_file(project_dir)
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check"
    body = await (await client.get(url + "?fresh=1", headers=_h(ctx))).json()
    f = body["findings"][0]
    assert f["id"] == "project_settings_untrusted" and f["ackable"] is True
    digest = hashlib.sha256(raw).hexdigest()
    assert f["ack_sha256"] == digest

    resp = await client.post(url + "/ack", json={"check_id": f["id"], "sha256": digest}, headers=_h(ctx))
    assert resp.status == 200
    assert (await resp.json())["findings"] == []
    stored = json.loads((ctx["DATA"] / L.ACK_FILE_NAME).read_text())
    assert stored == {"myproject": {"project_settings_untrusted": [digest]}}
    assert (await (await client.get(url + "?fresh=1", headers=_h(ctx))).json())["findings"] == []

    hooks_file(project_dir, {"hooks": {"PreToolUse": [{"command": "echo CHANGED"}]}})
    back = await (await client.get(url + "?fresh=1", headers=_h(ctx))).json()
    assert ids(back) == ["project_settings_untrusted"]
    assert back["findings"][0]["ack_sha256"] != digest


async def test_ack_survives_a_restart(aiohttp_client, app, ctx, project_dir):
    """The ack lives in data/, not in memory: a new process reads it back."""
    raw = hooks_file(project_dir)
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check"
    await client.post(url + "/ack", json={"check_id": "project_settings_untrusted",
                                          "sha256": hashlib.sha256(raw).hexdigest()}, headers=_h(ctx))
    LP._cache.clear()
    LP._last_sweep.update({"at": None, "results": {}})
    assert (await (await client.get(url, headers=_h(ctx))).json())["findings"] == []


async def test_ack_with_a_stale_hash_is_refused_and_returns_the_fresh_state(aiohttp_client, app, ctx, project_dir):
    hooks_file(project_dir)
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check/ack"
    resp = await client.post(url, json={"check_id": "project_settings_untrusted", "sha256": "a" * 64},
                             headers=_h(ctx))
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] and ids(body) == ["project_settings_untrusted"]
    assert not (ctx["DATA"] / L.ACK_FILE_NAME).exists()


async def test_ack_validates_its_input(aiohttp_client, app, ctx, project_dir):
    raw = hooks_file(project_dir)
    good = hashlib.sha256(raw).hexdigest()
    client = await aiohttp_client(app)
    url = "/api/projects/myproject/health-check/ack"
    h = _h(ctx)
    assert (await client.post(url, data="not json", headers=h)).status == 400
    assert (await client.post(url, json=[1], headers=h)).status == 400
    assert (await client.post(url, json={"check_id": "memory_index_near_cap", "sha256": good}, headers=h)).status == 400
    assert (await client.post(url, json={"check_id": "project_settings_untrusted", "sha256": "xyz"}, headers=h)).status == 400
    assert (await client.post(url, json={"check_id": "project_settings_untrusted"}, headers=h)).status == 400
    assert (await client.post(url, json={"check_id": "project_settings_untrusted",
                                         "sha256": good.upper()}, headers=h)).status == 400
    assert not (ctx["DATA"] / L.ACK_FILE_NAME).exists()


# ─────────────────────────── fleet sweep + digest + push ───────────────────────────


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def fake(payload):
        sent.append(json.loads(payload))
    monkeypatch.setattr(_webapp, "_push_broadcast", fake)
    return sent


def sick(project_dir):
    (native_memory(project_dir) / "MEMORY.md").write_text("\n".join(["x"] * 198) + "\n")


async def test_fleet_endpoint_lists_only_sick_projects_after_a_sweep(aiohttp_client, app, ctx, project_dir, pushes):
    client = await aiohttp_client(app)
    empty = await (await client.get("/api/health-check", headers=_h(ctx))).json()
    assert empty["last_sweep_at"] is None and empty["projects"] == []
    sick(project_dir)
    await LP.sweep_once(ctx)
    snap = await (await client.get("/api/health-check", headers=_h(ctx))).json()
    assert snap["counts"] == {"projects": 1, "crit": 1, "warn": 0}
    assert snap["projects"][0]["project_id"] == "myproject" and snap["last_sweep_at"]
    assert snap["mode"] in ("on", "off") and snap["interval_sec"] > 0


async def test_sweep_skips_free_chats_and_survives_a_broken_project(ctx, project_dir, pushes, monkeypatch, tmp_path):
    (ctx["DATA"] / "free_chats.json").write_text(json.dumps({"free-abc": {"label": "scratch", "created_at": 1}}))
    broken = tmp_path / "home" / "broken"
    broken.mkdir()
    ctx["topics"]["1001:43"] = {"project": "broken", "cwd": str(broken), "model": "sonnet"}
    sick(project_dir)
    real = L.run_checks

    def picky(project, env, *a, **k):
        if project.id == "broken":
            raise RuntimeError("boom")
        return real(project, env, *a, **k)
    monkeypatch.setattr(L, "run_checks", picky)
    await LP.sweep_once(ctx)
    assert set(LP._last_sweep["results"]) == {"myproject"}


async def test_sweep_writes_one_digest_only_when_there_are_findings(ctx, project_dir, pushes):
    await LP.sweep_once(ctx)
    assert not list((ctx["DATA"] / "inbox").glob("project-health-*.md")) if (ctx["DATA"] / "inbox").exists() else True
    sick(project_dir)
    await LP.sweep_once(ctx)
    digests = list((ctx["DATA"] / "inbox").glob("project-health-*.md"))
    assert len(digests) == 1
    text = digests[0].read_text()
    assert "myproject" in text and "Fix:" in text
    await LP.sweep_once(ctx)
    assert len(list((ctx["DATA"] / "inbox").glob("project-health-*.md"))) == 1   # rewritten, not duplicated


async def test_push_goes_out_once_per_day(ctx, project_dir, pushes, monkeypatch):
    monkeypatch.setattr(LP, "_today", lambda: "2026-10-09")
    sick(project_dir)
    await LP.sweep_once(ctx)
    await LP.sweep_once(ctx)
    await LP.sweep_once(ctx)
    assert len(pushes) == 1
    assert "1 new finding" in pushes[0]["body"] and "critical" in pushes[0]["body"]


async def test_second_new_finding_the_same_day_waits_for_tomorrow(ctx, project_dir, pushes, monkeypatch):
    day = {"d": "2026-10-09"}
    monkeypatch.setattr(LP, "_today", lambda: day["d"])
    sick(project_dir)
    await LP.sweep_once(ctx)
    assert len(pushes) == 1
    hooks_file(project_dir)                 # a NEW finding appears later the same day
    await LP.sweep_once(ctx)
    assert len(pushes) == 1                 # at most once per calendar day
    await LP.sweep_once(ctx)
    assert len(pushes) == 1
    day["d"] = "2026-10-10"                 # tomorrow: the unannounced one goes out
    await LP.sweep_once(ctx)
    assert len(pushes) == 2 and "1 new finding" in pushes[1]["body"]


async def test_a_standing_finding_does_not_nag_every_morning(ctx, project_dir, pushes, monkeypatch):
    day = {"d": "2026-10-09"}
    monkeypatch.setattr(LP, "_today", lambda: day["d"])
    sick(project_dir)
    await LP.sweep_once(ctx)
    for d in ("2026-10-10", "2026-10-11", "2026-10-12"):
        day["d"] = d
        await LP.sweep_once(ctx)
    assert len(pushes) == 1
    # the digest file is still refreshed daily
    assert (ctx["DATA"] / "inbox" / "project-health-2026-10-12.md").exists()


async def test_push_state_survives_a_restart(ctx, project_dir, pushes, monkeypatch):
    monkeypatch.setattr(LP, "_today", lambda: "2026-10-09")
    sick(project_dir)
    await LP.sweep_once(ctx)
    assert len(pushes) == 1
    state = json.loads((ctx["DATA"] / LP.STATE_FILE).read_text())
    assert state["last_push_day"] == "2026-10-09" and state["notified"] == ["myproject:memory_index_near_cap:native"]
    LP._cache.clear()
    LP._last_sweep.update({"at": None, "results": {}})            # "restart": memory gone, file stays
    await LP.sweep_once(ctx)
    assert len(pushes) == 1


async def test_a_healed_and_returning_finding_is_announced_again(ctx, project_dir, pushes, monkeypatch):
    day = {"d": "2026-10-09"}
    monkeypatch.setattr(LP, "_today", lambda: day["d"])
    idx = native_memory(project_dir) / "MEMORY.md"
    sick(project_dir)
    await LP.sweep_once(ctx)
    assert len(pushes) == 1
    day["d"] = "2026-10-10"
    idx.write_text("tiny\n")
    await LP.sweep_once(ctx)                                     # healed: forgotten
    assert len(pushes) == 1
    day["d"] = "2026-10-11"
    sick(project_dir)
    await LP.sweep_once(ctx)                                     # back again: news
    assert len(pushes) == 2


async def test_failed_push_does_not_break_the_sweep_or_lose_state(ctx, project_dir, monkeypatch):
    async def boom(payload):
        raise RuntimeError("push down")
    monkeypatch.setattr(_webapp, "_push_broadcast", boom)
    sick(project_dir)
    snap = await LP.sweep_once(ctx)
    assert snap["counts"]["projects"] == 1
    assert (ctx["DATA"] / LP.STATE_FILE).exists()


async def test_healthy_fleet_pushes_nothing_and_writes_no_digest(ctx, pushes):
    await LP.sweep_once(ctx)
    assert pushes == []
    assert not (ctx["DATA"] / "inbox").exists() or not list((ctx["DATA"] / "inbox").glob("project-health-*"))


# ─────────────────────────── api_project_health + module wiring ───────────────────────────


async def test_header_pill_endpoint_and_the_check_agree_on_env_exposed(aiohttp_client, ctx, project_dir):
    """api_project_health (the .env pill) and the health check share one rule: they cannot disagree."""
    import subprocess
    from aiohttp import web
    from features.project_health.routes import add_routes
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True, capture_output=True)
    (project_dir / ".env").write_text("S=1")
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = ctx
    a.router.add_get("/api/projects/{id}/health", _webapp.api_project_health)
    add_routes(a)
    client = await aiohttp_client(a)
    pill = await (await client.get("/api/projects/myproject/health", headers=_h(ctx))).json()
    check = await (await client.get("/api/projects/myproject/health-check", headers=_h(ctx))).json()
    assert pill["security_warn"] is True and pill["security_hint"] == L.ENV_EXPOSED_HINT
    assert "env_exposed" in ids(check)
    (project_dir / ".gitignore").write_text(".env\n")
    pill = await (await client.get("/api/projects/myproject/health", headers=_h(ctx))).json()
    check = await (await client.get("/api/projects/myproject/health-check?fresh=1", headers=_h(ctx))).json()
    assert pill["security_warn"] is False and pill["security_hint"] is None
    assert "env_exposed" not in ids(check)


def test_module_is_registered_and_default_on():
    import modules
    mods = {m["id"]: m for m in modules.list_modules()}
    assert mods["project_health"]["enabled"] is True


def test_register_is_a_no_op_when_the_module_is_disabled(monkeypatch):
    import modules
    from aiohttp import web
    from features import project_health
    monkeypatch.setattr(modules, "is_enabled", lambda mid: False)
    app = web.Application()
    project_health.register(app, {})
    assert not [r for r in app.router.routes() if "health-check" in str(r.resource)]


def test_register_adds_routes_and_starts_the_loop_only_when_mode_on(monkeypatch):
    import modules
    from aiohttp import web
    from features import project_health
    monkeypatch.setattr(modules, "is_enabled", lambda mid: True)
    spawned = []
    monkeypatch.setattr(_webapp, "_spawn_bg", lambda coro: (coro.close(), spawned.append(1))[1])
    monkeypatch.setattr(_webapp, "_STARTUP_BG_TASKS", [])
    monkeypatch.setattr(L, "MODE", "on")
    app = web.Application()
    project_health.register(app, {})
    paths = {r.resource.canonical for r in app.router.routes() if hasattr(r.resource, "canonical")}
    assert {"/api/health-check", "/api/projects/{id}/health-check",
            "/api/projects/{id}/health-check/ack"} <= paths
    assert spawned == [1]

    spawned.clear()
    monkeypatch.setattr(L, "MODE", "off")
    project_health.register(web.Application(), {})
    assert spawned == []                                   # routes stay, the daily loop does not start

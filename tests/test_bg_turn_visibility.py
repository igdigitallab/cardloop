"""
Tests for the "drain-surfaced autonomous turn" blind spot.

An autonomous CLI turn (a task-notification wake, or a mid-turn steered message the CLI
deferred past the turn boundary) is surfaced by engine._drain_between_turns through
webapp._bg_run_event.  It is REAL work on the CLI, but it is not registered in
ctx["running"] — the engine popped that slot the moment the operator's own turn ended.

Three operator-visible failures came out of that single gap:

  * POST /chat/stop returned {"stopped": false} for the whole bg turn — the Stop button
    silently did nothing.
  * GET /live reported running:false, so the run bar vanished and the composer stayed on
    "Send" while the agent was visibly working.
  * POST /chat and the chat-queue drain both considered the session idle and started a new
    engine turn straight over the busy CLI.  receive_response() then terminates on the BG
    turn's ResultMessage, so the operator's message is answered by the previous turn's tail
    and its real answer is orphaned into the next bg run.

_bg_turn_active() is the shared predicate, with a staleness cap so a leaked marker can
never wedge a project as permanently busy.
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token

SESSION_KEY = "1001:42"


@pytest.fixture(autouse=True)
def _clean_bg_registries():
    """_bg_run_ids/_bg_run_started are module-level — isolate every test."""
    _webapp._bg_run_ids.pop(SESSION_KEY, None)
    _webapp._bg_run_started.pop(SESSION_KEY, None)
    yield
    _webapp._bg_run_ids.pop(SESSION_KEY, None)
    _webapp._bg_run_started.pop(SESSION_KEY, None)


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (tmp_path / "myproject").mkdir(exist_ok=True)
    ctx = {
        "topics": {SESSION_KEY: {"project": "myproject",
                                 "cwd": str(tmp_path / "myproject"),
                                 "model": "sonnet"}},
        "sessions": {},
        "running": {},
        "live_clients": {},
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    return ctx


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_post("/api/projects/{id}/chat/stop", _webapp.api_project_chat_stop)
    a.router.add_get("/api/projects/{id}/live", _webapp.api_project_live)
    a.router.add_get("/api/projects/{id}/running", _webapp.api_project_running)
    return a


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


class _FakeClient:
    def __init__(self):
        self.interrupts = 0

    async def interrupt(self):
        self.interrupts += 1


class _FakeEntry:
    def __init__(self, client):
        self.client = client


def _open_bg_turn(age_sec: float = 0.0):
    _webapp._bg_run_ids[SESSION_KEY] = "abc123"
    _webapp._bg_run_started[SESSION_KEY] = time.monotonic() - age_sec


# ─────────────────────────── _bg_turn_active ──────────────────────────────────


def test_bg_turn_active_is_false_when_no_bg_run():
    assert _webapp._bg_turn_active(SESSION_KEY) is False


def test_bg_turn_active_is_true_for_a_fresh_bg_run():
    _open_bg_turn()
    assert _webapp._bg_turn_active(SESSION_KEY) is True


def test_bg_turn_active_ignores_a_stale_marker():
    """A leaked marker must not wedge the project as busy forever — past the staleness cap
    the predicate reports idle even though the id is still present."""
    _open_bg_turn(age_sec=_webapp._BG_RUN_STALE_SEC + 60)
    assert _webapp._bg_turn_active(SESSION_KEY) is False


def test_bg_run_event_stamps_and_clears_the_start_time(monkeypatch):
    """The lifecycle callback must keep id and timestamp in lockstep, or staleness is
    measured against a timestamp from an earlier run."""
    monkeypatch.setattr(_webapp, "_WEBAPP_CTX", {"running": {}}, raising=False)
    _webapp._bg_run_event(SESSION_KEY, "start")
    assert SESSION_KEY in _webapp._bg_run_ids
    assert SESSION_KEY in _webapp._bg_run_started
    _webapp._bg_run_event(SESSION_KEY, "end")
    assert SESSION_KEY not in _webapp._bg_run_ids
    assert SESSION_KEY not in _webapp._bg_run_started


# ─────────────────────────── /chat/stop ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_reaches_a_bg_turn_through_the_live_client(aiohttp_client, fake_ctx, app):
    """running[] is empty during a bg turn — the endpoint must fall back to the live client
    instead of reporting stopped=False at the operator."""
    client_obj = _FakeClient()
    fake_ctx["live_clients"][SESSION_KEY] = _FakeEntry(client_obj)
    _open_bg_turn()

    http = await aiohttp_client(app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth(fake_ctx))

    assert resp.status == 200
    assert await resp.json() == {"ok": True, "stopped": True}
    assert client_obj.interrupts == 1


@pytest.mark.asyncio
async def test_stop_prefers_the_registered_run_over_the_bg_fallback(aiohttp_client, fake_ctx, app):
    """A registered run owns the turn; the fallback must not steal it."""
    registered, bg = _FakeClient(), _FakeClient()
    fake_ctx["running"][SESSION_KEY] = registered
    fake_ctx["live_clients"][SESSION_KEY] = _FakeEntry(bg)
    _open_bg_turn()

    http = await aiohttp_client(app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth(fake_ctx))

    assert (await resp.json())["stopped"] is True
    assert registered.interrupts == 1
    assert bg.interrupts == 0


@pytest.mark.asyncio
async def test_stop_bg_fallback_needs_a_live_client(aiohttp_client, fake_ctx, app):
    """Marker set but the client already evicted → honest stopped=False, no crash."""
    _open_bg_turn()
    http = await aiohttp_client(app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth(fake_ctx))
    assert await resp.json() == {"ok": True, "stopped": False}


@pytest.mark.asyncio
async def test_stop_ignores_a_stale_bg_marker(aiohttp_client, fake_ctx, app):
    client_obj = _FakeClient()
    fake_ctx["live_clients"][SESSION_KEY] = _FakeEntry(client_obj)
    _open_bg_turn(age_sec=_webapp._BG_RUN_STALE_SEC + 60)

    http = await aiohttp_client(app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth(fake_ctx))

    assert (await resp.json())["stopped"] is False
    assert client_obj.interrupts == 0


# ─────────────────────────── /live and /running ───────────────────────────────


@pytest.mark.asyncio
async def test_live_reports_running_during_a_bg_turn(aiohttp_client, fake_ctx, app):
    """The run bar and the Stop button both track this flag."""
    _open_bg_turn()
    http = await aiohttp_client(app)
    resp = await http.get("/api/projects/myproject/live", headers=_auth(fake_ctx))
    assert (await resp.json())["running"] is True


@pytest.mark.asyncio
async def test_live_reports_idle_without_a_bg_turn(aiohttp_client, fake_ctx, app):
    http = await aiohttp_client(app)
    resp = await http.get("/api/projects/myproject/live", headers=_auth(fake_ctx))
    assert (await resp.json())["running"] is False


@pytest.mark.asyncio
async def test_running_endpoint_reports_a_bg_turn(aiohttp_client, fake_ctx, app):
    _open_bg_turn()
    http = await aiohttp_client(app)
    resp = await http.get("/api/projects/myproject/running", headers=_auth(fake_ctx))
    assert (await resp.json())["running"] is True


# ─────────────────────────── chat-queue drain ─────────────────────────────────


@pytest.mark.asyncio
async def test_queue_drain_waits_for_a_bg_turn(fake_ctx):
    """Draining into a busy CLI is what desyncs the stream — the item must stay queued."""
    _webapp._CHAT_QUEUE.pop(SESSION_KEY, None)
    _webapp._chat_queue_enqueue(SESSION_KEY, "follow-up", None, "myproject")
    _open_bg_turn()
    try:
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is False
        # Still queued — the backstop loop retries once the bg turn ends.
        assert [i["text"] for i in _webapp._chat_queue_get(SESSION_KEY)] == ["follow-up"]
    finally:
        _webapp._CHAT_QUEUE.pop(SESSION_KEY, None)

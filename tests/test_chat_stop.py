"""
Tests for POST /api/projects/{id}/chat/stop (api_project_chat_stop).

The endpoint had no coverage at all, which let a real failure mode hide: what is
parked in ctx["running"] is not always an SDK client.  api_project_chat reserves
the slot SYNCHRONOUSLY with the sentinel `True` before the first await (engine.py
documents this as a race), and api_project_rotate parks the same sentinel for the
duration of a rotate.  `True` has no .interrupt, so a stop landing in that window
is a silent no-op — the caller must be able to tell that apart from "nothing was
running", which is why the response carries `stopped`.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── Fixtures ─────────────────────────────────────────


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (tmp_path / "myproject").mkdir(exist_ok=True)
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(tmp_path / "myproject"),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
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
def stop_app(fake_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/chat/stop", _webapp.api_project_chat_stop)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


class _FakeClient:
    """Stand-in for ClaudeSDKClient — only .interrupt() matters to the endpoint."""

    def __init__(self, raises: bool = False):
        self.interrupts = 0
        self._raises = raises

    async def interrupt(self):
        self.interrupts += 1
        if self._raises:
            raise RuntimeError("subprocess already gone")


# ─────────────────────────── Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_interrupts_live_client(aiohttp_client, fake_ctx, stop_app):
    """A real client in running[] → interrupt() is called, stopped=True."""
    client_obj = _FakeClient()
    fake_ctx["running"]["1001:42"] = client_obj

    http = await aiohttp_client(stop_app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth_headers(fake_ctx))

    assert resp.status == 200
    assert await resp.json() == {"ok": True, "stopped": True}
    assert client_obj.interrupts == 1


@pytest.mark.asyncio
async def test_stop_reports_false_when_nothing_running(aiohttp_client, fake_ctx, stop_app):
    """Idle project → stopped=False, and the request still succeeds."""
    http = await aiohttp_client(stop_app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth_headers(fake_ctx))

    assert resp.status == 200
    assert await resp.json() == {"ok": True, "stopped": False}


@pytest.mark.asyncio
async def test_stop_is_a_noop_against_the_true_sentinel(aiohttp_client, fake_ctx, stop_app):
    """The `True` placeholder (turn reserved, SDK client not yet created; also held for
    the duration of a rotate) has no .interrupt — the endpoint must report stopped=False
    rather than crash on it.  This is the window in which a stop silently does nothing."""
    fake_ctx["running"]["1001:42"] = True

    http = await aiohttp_client(stop_app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth_headers(fake_ctx))

    assert resp.status == 200
    assert await resp.json() == {"ok": True, "stopped": False}
    # The sentinel must survive — the turn owns that slot and clears it in its finally.
    assert fake_ctx["running"]["1001:42"] is True


@pytest.mark.asyncio
async def test_stop_swallows_a_failing_interrupt(aiohttp_client, fake_ctx, stop_app):
    """A dead subprocess raising from interrupt() must not surface as a 500 — the turn is
    already over from the operator's point of view."""
    client_obj = _FakeClient(raises=True)
    fake_ctx["running"]["1001:42"] = client_obj

    http = await aiohttp_client(stop_app)
    resp = await http.post("/api/projects/myproject/chat/stop", headers=_auth_headers(fake_ctx))

    assert resp.status == 200
    assert await resp.json() == {"ok": True, "stopped": True}
    assert client_obj.interrupts == 1


@pytest.mark.asyncio
async def test_stop_unknown_project_is_404(aiohttp_client, fake_ctx, stop_app):
    """The frontend swallows stop errors, so a 404 here is invisible in the UI — pin the
    contract so a resolver change cannot quietly turn Stop into a no-op."""
    http = await aiohttp_client(stop_app)
    resp = await http.post("/api/projects/nosuchproject/chat/stop", headers=_auth_headers(fake_ctx))

    assert resp.status == 404


@pytest.mark.asyncio
async def test_stop_resolves_a_free_chat(aiohttp_client, fake_ctx, stop_app, tmp_path):
    """Free chats are virtual projects (id == session_key == free-<uuid>) appended by
    _collect_projects.  Stop must reach them too — they are where most ad-hoc runs happen."""
    import json as _json

    (fake_ctx["DATA"] / "free_chats.json").write_text(
        _json.dumps({"free-abcd1234": {"label": "scratch", "cwd": str(tmp_path), "model": "sonnet"}}),
        encoding="utf-8",
    )
    client_obj = _FakeClient()
    fake_ctx["running"]["free-abcd1234"] = client_obj

    http = await aiohttp_client(stop_app)
    resp = await http.post("/api/projects/free-abcd1234/chat/stop", headers=_auth_headers(fake_ctx))

    assert resp.status == 200
    assert await resp.json() == {"ok": True, "stopped": True}
    assert client_obj.interrupts == 1

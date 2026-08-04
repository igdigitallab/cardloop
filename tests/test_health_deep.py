"""
Root-fix A3: /api/health?deep=1 must report background children ("agents"), not just
in-flight turns ("running") — restart-self.sh used to restart at running==0 and SIGTERM
live sub-agents whose parent turn had already ended.
"""
import sys
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


@pytest.fixture
def monitors(monkeypatch):
    monkeypatch.setattr(_webapp, "_monitors", {})
    return _webapp._monitors


def _make_app(ctx):
    app = web.Application()
    app["ctx"] = ctx
    app.router.add_get("/api/health", _webapp.api_health)
    return app


async def test_deep_reports_running_and_agents(aiohttp_client, monitors):
    monitors["k1"] = {
        "a1": {"id": "a1", "kind": "agent", "status": "running"},
        "a2": {"id": "a2", "kind": "workflow", "status": "running"},
        "a3": {"id": "a3", "kind": "monitor", "status": "running"},
        "a4": {"id": "a4", "kind": "agent", "status": "done"},      # terminal — not counted
        "a5": {"id": "a5", "kind": "bash", "status": "running"},    # bash — not counted
    }
    monitors["k2"] = {
        "b1": {"id": "b1", "kind": "agent", "status": "running"},   # second session counts too
    }
    ctx = {"running": {"k9": object()}}
    client = await aiohttp_client(_make_app(ctx))

    resp = await client.get("/api/health?deep=1")
    data = await resp.json()
    assert data["running"] == 1
    assert data["agents"] == 4


async def test_deep_zero_when_no_monitors(aiohttp_client, monitors):
    client = await aiohttp_client(_make_app({"running": {}}))
    data = await (await client.get("/api/health?deep=1")).json()
    assert data == {"ok": True, "running": 0, "agents": 0}


async def test_shallow_unchanged(aiohttp_client, monitors):
    monitors["k1"] = {"a1": {"id": "a1", "kind": "agent", "status": "running"}}
    client = await aiohttp_client(_make_app({"running": {}}))
    data = await (await client.get("/api/health")).json()
    assert data == {"ok": True}


def test_live_agent_monitor_count_never_raises(monkeypatch):
    monkeypatch.setattr(_webapp, "_monitors", None)  # pathological state
    assert _webapp._live_agent_monitor_count() == 0

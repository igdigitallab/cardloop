"""
Scrolling a chat back past the last 100 messages (session-history paging).

The feed used to be the last 100 messages of the session and nothing above them was reachable.
Every message now carries `idx`, its position in the whole feed; a transcript is append-only, so
the position never changes and is a cursor: `before=N` pages back, `since=N` returns N to the end
(the window of a chat that already has older pages above it, so the window cannot slide away
from them and leave a gap).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import webapp as _webapp

from test_grok_wiring import (  # noqa: F401 - fixtures used by name
    PROJECT_ID, _auth, _seed_chat, engines, fake_ctx, isolate,
)


def _transcript(path: Path, n: int, start: int = 0, mode: str = "w") -> None:
    """n operator messages, each followed by an assistant reply → 2n feed messages."""
    with open(path, mode, encoding="utf-8") as f:
        for i in range(start, start + n):
            f.write(json.dumps({"type": "user", "uuid": f"u{i}",
                                "message": {"role": "user", "content": f"q{i}"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}}) + "\n")


def _texts(msgs):
    return [m["text"] for m in msgs]


# ─────────────────────────── _session_history ─────────────────────────────────


def test_every_message_carries_its_position_and_it_survives_appends(tmp_path):
    p = tmp_path / "s.jsonl"
    _transcript(p, 3)
    first = _webapp._session_history(p)
    assert [m["idx"] for m in first] == [0, 1, 2, 3, 4, 5]
    _transcript(p, 2, start=3, mode="a")
    again = _webapp._session_history(p)
    assert [(m["idx"], m["text"]) for m in again[:6]] == [(m["idx"], m["text"]) for m in first]


def test_the_default_window_is_still_the_last_100(tmp_path):
    p = tmp_path / "s.jsonl"
    _transcript(p, 80)  # 160 messages
    msgs = _webapp._session_history(p)
    assert len(msgs) == 100 and msgs[0]["idx"] == 60 and msgs[-1]["idx"] == 159


def test_before_pages_back_until_the_first_message(tmp_path):
    p = tmp_path / "s.jsonl"
    _transcript(p, 80)
    page = _webapp._session_history(p, before=60)
    assert [m["idx"] for m in page] == list(range(0, 60))
    page = _webapp._session_history(p, limit=10, before=60)
    assert [m["idx"] for m in page] == list(range(50, 60))
    assert _texts(page)[:2] == ["q25", "a25"]
    assert _webapp._session_history(p, before=0) == []


def test_before_past_the_end_is_the_tail(tmp_path):
    p = tmp_path / "s.jsonl"
    _transcript(p, 5)
    assert [m["idx"] for m in _webapp._session_history(p, limit=3, before=999)] == [7, 8, 9]


def test_since_returns_the_whole_range_to_the_end(tmp_path):
    p = tmp_path / "s.jsonl"
    _transcript(p, 80)
    msgs = _webapp._session_history(p, since=20)
    assert msgs[0]["idx"] == 20 and msgs[-1]["idx"] == 159 and len(msgs) == 140


def test_since_too_far_back_or_past_the_end_degrades_to_the_tail(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    _transcript(p, 80)
    monkeypatch.setattr(_webapp, "_HISTORY_SINCE_MAX", 50)
    assert _webapp._session_history(p, since=10)[0]["idx"] == 60
    assert _webapp._session_history(p, since=500)[0]["idx"] == 60


# ─────────────────────────── endpoint ─────────────────────────────────────────


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_get("/api/projects/{id}/session-history", _webapp.api_project_session_history)
    return a


@pytest.fixture
def transcript(tmp_path, monkeypatch, fake_ctx):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    _transcript(sdk / "sess-1.jsonl", 150)  # 300 messages
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk)
    _seed_chat(fake_ctx, provider="claude", session_id="sess-1")
    return sdk


async def _get(client, fake_ctx, qs):
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history?session_id=sess-1{qs}",
                            headers=_auth(fake_ctx))
    assert resp.status == 200
    return await resp.json()


async def test_endpoint_pages_back_with_before_and_limit(aiohttp_client, fake_ctx, app, transcript):
    client = await aiohttp_client(app)
    tail = await _get(client, fake_ctx, "")
    assert [m["idx"] for m in tail["messages"]] == list(range(200, 300))
    page = await _get(client, fake_ctx, "&before=200")
    assert [m["idx"] for m in page["messages"]] == list(range(100, 200))
    page = await _get(client, fake_ctx, "&before=100&limit=40")
    assert [m["idx"] for m in page["messages"]] == list(range(60, 100))
    assert page["session_id"] == "sess-1"


async def test_endpoint_since_and_limit_cap(aiohttp_client, fake_ctx, app, transcript, monkeypatch):
    monkeypatch.setattr(_webapp, "_HISTORY_PAGE_MAX", 50)
    client = await aiohttp_client(app)
    since = await _get(client, fake_ctx, "&since=120")
    assert [m["idx"] for m in since["messages"]] == list(range(120, 300))
    capped = await _get(client, fake_ctx, "&before=300&limit=100000")
    assert [m["idx"] for m in capped["messages"]] == list(range(250, 300))


async def test_endpoint_ignores_a_malformed_cursor(aiohttp_client, fake_ctx, app, transcript):
    client = await aiohttp_client(app)
    for qs in ("&before=abc", "&before=-5", "&since=x", "&limit=-1"):
        body = await _get(client, fake_ctx, qs)
        assert [m["idx"] for m in body["messages"]] == list(range(200, 300)), qs

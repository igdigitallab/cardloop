"""
Session-list stats for Claude transcripts (message count + last assistant turn), cached per file.

A cold cockpit load asks every open project tab for its session list. Before this module the list
parsed EVERY line of EVERY transcript of the project on the event loop (two full passes per file,
and for all files, not just the 30 it returns): on ops that was 392 files / 2 GB across 12 open
tabs, and every other request, the active chat's history included, queued behind it for 10-30 s
warm and 1-2 min with a cold page cache. These tests pin the three fixes: stats are read once and
then only for appended lines, only the 30 newest transcripts are read, and the reads happen off the
event loop.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import transcript_stats as ts
import webapp as _webapp

from test_grok_wiring import (  # noqa: F401 - fixtures used by name
    PROJECT_ID, SESSION_KEY, _auth, _seed_chat, engines, fake_ctx, isolate,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    ts._CACHE.clear()
    yield
    ts._CACHE.clear()


@pytest.fixture
def parsed(monkeypatch):
    """Counts the lines the scanner actually JSON-parses."""
    calls = []
    real = ts._loads

    def counting(raw):
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(ts, "_loads", counting)
    return calls


def _user(text="hi"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(inp=100, read=0, create=0, at="2026-06-13T03:00:00.000Z"):
    return {"type": "assistant", "timestamp": at, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                  "cache_creation_input_tokens": create}}}


def _write(path: Path, rows, mode="w", newline_last=True):
    text = "\n".join(json.dumps(r) for r in rows)
    with open(path, mode, encoding="utf-8") as f:
        f.write(text + ("\n" if newline_last else ""))


# ─────────────────────────── stats semantics ──────────────────────────────────


def test_counts_messages_and_reads_the_last_assistant_turn(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_user("a"), _assistant(100, 400, 0, "2026-06-13T03:00:00.000Z"),
               {"type": "summary", "summary": "x"},
               _user("b"), _assistant(200, 200, 50, "2026-06-13T04:00:00.000Z")])
    got = ts.stats(p)
    assert got == {"message_count": 4, "context_tokens": 450,
                   "last_turn_at_ms": 1781323200000, "last_cache_hit_pct": 50}


def test_a_missing_file_is_empty_stats(tmp_path):
    assert ts.stats(tmp_path / "nope.jsonl") == {
        "message_count": 0, "context_tokens": 0, "last_turn_at_ms": None, "last_cache_hit_pct": None}


def test_odd_lines_are_skipped_and_do_not_end_the_scan(tmp_path):
    # The old scanners aborted at the first JSON array or string `message` and returned what they
    # had counted so far; a stray line must not hide the rest of the transcript.
    p = tmp_path / "s.jsonl"
    p.write_text("[1, 2]\nnot json\n" + json.dumps({"type": "assistant", "message": "str"}) + "\n"
                 + json.dumps(_user()) + "\n" + json.dumps(_assistant(7)) + "\n", encoding="utf-8")
    got = ts.stats(p)
    assert got["message_count"] == 2 and got["context_tokens"] == 7


def test_an_assistant_turn_without_a_timestamp_falls_back_to_the_file_mtime(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_assistant(10, 10, 0, at=None)])
    os.utime(p, (1_700_000_000, 1_700_000_000))
    assert ts.stats(p)["last_turn_at_ms"] == 1_700_000_000_000


# ─────────────────────────── caching ──────────────────────────────────────────


def test_an_unchanged_file_is_not_parsed_again(tmp_path, parsed):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(), _assistant()])
    first = ts.stats(p)
    n = len(parsed)
    assert n == 2
    assert ts.stats(p) == first
    assert len(parsed) == n


def test_appended_lines_are_the_only_ones_parsed(tmp_path, parsed):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(), _assistant(100)])
    ts.stats(p)
    parsed.clear()
    _write(p, [_user("more"), _assistant(900, at="2026-06-13T05:00:00.000Z")], mode="a")
    got = ts.stats(p)
    assert len(parsed) == 2
    assert got["message_count"] == 4 and got["context_tokens"] == 900


def test_a_line_still_being_written_is_counted_once_it_completes(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(), _assistant(100)], newline_last=False)
    assert ts.stats(p)["message_count"] == 2  # a final line without "\n" still counts
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n" + json.dumps(_user("next")) + "\n")
    assert ts.stats(p)["message_count"] == 3  # ...but is not counted twice once completed
    line = json.dumps(_assistant(4242))
    with open(p, "a", encoding="utf-8") as f:
        f.write(line[:25])  # the CLI is mid-write: not JSON yet
    assert ts.stats(p)["message_count"] == 3
    with open(p, "a", encoding="utf-8") as f:
        f.write(line[25:] + "\n")
    got = ts.stats(p)
    assert (got["message_count"], got["context_tokens"]) == (4, 4242)


def test_a_shrunk_file_is_scanned_from_the_start(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(), _user(), _user(), _assistant(5)])
    assert ts.stats(p)["message_count"] == 4
    _write(p, [_assistant(3)])
    got = ts.stats(p)
    assert (got["message_count"], got["context_tokens"]) == (1, 3)


def test_a_same_size_rewrite_is_scanned_from_the_start(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_assistant(111)])
    assert ts.stats(p)["context_tokens"] == 111
    _write(p, [_assistant(222)])
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert ts.stats(p)["context_tokens"] == 222


# ─────────────────────────── endpoints ────────────────────────────────────────


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    async def ping(req):
        return web.json_response({"ok": True})

    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_get("/api/projects/{id}/sessions", _webapp.api_project_sessions)
    a.router.add_get("/api/projects/{id}/session-history", _webapp.api_project_session_history)
    a.router.add_get("/api/ping", ping)
    return a


def _sdk_dir(tmp_path, monkeypatch, n):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    for i in range(n):
        f = sdk / f"sess-{i:02d}.jsonl"
        _write(f, [_user(f"message {i}"), _assistant(i + 1)])
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk)
    return sdk


async def test_the_session_list_reads_only_the_30_newest_transcripts(
    aiohttp_client, fake_ctx, app, tmp_path, monkeypatch
):
    _sdk_dir(tmp_path, monkeypatch, 35)
    _seed_chat(fake_ctx, provider="claude")
    read = []
    real = ts.stats
    monkeypatch.setattr(ts, "stats", lambda p: (read.append(Path(p).stem), real(p))[1])
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/sessions", headers=_auth(fake_ctx))
    rows = (await resp.json())["sessions"]
    newest = [f"sess-{i:02d}" for i in range(34, 4, -1)]
    assert [r["session_id"] for r in rows] == newest
    assert sorted(read) == sorted(newest)
    top = rows[0]
    assert top["message_count"] == 2 and top["context_tokens"] == 35
    assert top["preview"] == "message 34"


async def test_the_session_list_does_not_block_other_requests(
    aiohttp_client, fake_ctx, app, tmp_path, monkeypatch
):
    _sdk_dir(tmp_path, monkeypatch, 1)
    _seed_chat(fake_ctx, provider="claude")

    def slow(p):
        time.sleep(1.0)
        return ts._empty_result()

    monkeypatch.setattr(ts, "stats", slow)
    client = await aiohttp_client(app)
    t0 = time.monotonic()
    listing = asyncio.ensure_future(
        client.get(f"/api/projects/{PROJECT_ID}/sessions", headers=_auth(fake_ctx)))
    await asyncio.sleep(0.1)
    pong = await client.get("/api/ping", headers=_auth(fake_ctx))
    assert pong.status == 200
    assert time.monotonic() - t0 < 0.5
    assert not listing.done()
    assert (await listing).status == 200


async def test_session_history_does_not_block_other_requests(
    aiohttp_client, fake_ctx, app, tmp_path, monkeypatch
):
    sdk = _sdk_dir(tmp_path, monkeypatch, 1)
    _seed_chat(fake_ctx, provider="claude", session_id="sess-00")
    real = _webapp._session_history

    def slow(*a, **kw):
        time.sleep(1.0)
        return real(*a, **kw)

    monkeypatch.setattr(_webapp, "_session_history", slow)
    client = await aiohttp_client(app)
    t0 = time.monotonic()
    hist = asyncio.ensure_future(client.get(
        f"/api/projects/{PROJECT_ID}/session-history?session_id=sess-00", headers=_auth(fake_ctx)))
    await asyncio.sleep(0.1)
    pong = await client.get("/api/ping", headers=_auth(fake_ctx))
    assert pong.status == 200
    assert time.monotonic() - t0 < 0.5
    body = await (await hist).json()
    assert [m["text"] for m in body["messages"] if m["role"] == "user"] == ["message 0"]
    assert body["context_tokens"] == 1
    assert sdk.is_dir()

"""
Tests for spec-089 §7: transcript peek per agent row.

Covers:
- _transcript_tail_entries: formatting (tool_use / text blocks), the ignored tool_result on a
  'user' line, the missing-timestamp fallback, the <=120/<=200 truncation, last-n selection,
  and the 64 KB tail window (older entries + a partial first line are dropped).
- _workflow_journal_tail_entries: header + per-agent "started -> result|failed" lines.
- GET /api/projects/{id}/monitors/{mid}/tail: unknown project/monitor 404, a direct agent
  transcript, a workflow-internal agent transcript (second glob), a stream-only row with no
  file -> 404 "no transcript", n clamping, the path-resolution cache, and a workflow row's
  journal summary (+ 404 without a journal).
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── helpers ───────────────────────────────────────────


def _expected_ts(iso: str) -> str:
    """Mirrors webapp._transcript_tail_ts so assertions aren't hardcoded to a timezone."""
    s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%H:%M:%S")


def _jsonl(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def _assistant(ts: "str | None", content: list) -> dict:
    obj = {"type": "assistant", "message": {"role": "assistant", "content": content}}
    if ts is not None:
        obj["timestamp"] = ts
    return obj


def _tool_use(name: str, **input_kwargs) -> dict:
    return {"type": "tool_use", "id": "t1", "name": name, "input": input_kwargs}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _user_tool_result(tool_use_id: str = "t1") -> dict:
    return {"type": "user", "timestamp": "2026-09-01T10:00:00.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}]}}


# ─────────────────────────── _transcript_tail_entries ──────────────────────────


def test_transcript_tail_mixed_entries(tmp_path):
    ts1 = "2026-09-01T17:03:11.123Z"
    ts2 = "2026-09-01T17:03:12.000Z"
    ts3 = "2026-09-01T17:03:13.500Z"
    long_cmd = "echo " + ("x" * 300)
    long_text = "first line of the message " + ("y" * 250) + "\nsecond line never shown"

    lines = [
        _assistant(ts1, [_tool_use("Bash", command=long_cmd)]),
        _user_tool_result(),  # must be ignored entirely — no entry emitted
        _assistant(ts2, [_tool_use("Read", file_path="/home/igor/cardloop/webapp.py")]),
        _assistant(ts3, [_text(long_text)]),
        _assistant(None, [_text("no timestamp on this one")]),  # missing timestamp
    ]
    p = tmp_path / "agent-a1.jsonl"
    p.write_text(_jsonl(*lines), encoding="utf-8")

    entries = _webapp._transcript_tail_entries(p, n=20)
    assert len(entries) == 4  # the tool_result line contributed nothing

    # tool_use Bash: command truncated to <=120 chars total (119 + ellipsis)
    e0 = entries[0]
    assert e0.startswith(f"[{_expected_ts(ts1)}] ⚙ Bash: ")
    target0 = e0.split(": ", 1)[1]
    assert len(target0) == 120
    assert target0.endswith("…")

    # tool_use Read: file_path shown verbatim (well under the cap)
    e1 = entries[1]
    assert e1 == f"[{_expected_ts(ts2)}] ⚙ Read: /home/igor/cardloop/webapp.py"

    # text block: first line only, whitespace-collapsed, truncated to <=200 chars total
    e2 = entries[2]
    assert e2.startswith(f"[{_expected_ts(ts3)}] ✎ ")
    body2 = e2.split("] ✎ ", 1)[1]
    assert len(body2) == 200
    assert body2.endswith("…")
    assert "second line" not in e2

    # missing timestamp -> [--:--:--]
    e3 = entries[3]
    assert e3 == "[--:--:--] ✎ no timestamp on this one"


def test_transcript_tail_last_n_selection(tmp_path):
    objs = [_assistant(f"2026-09-01T10:00:{i:02d}.000Z", [_text(f"step {i}")]) for i in range(25)]
    p = tmp_path / "agent-a2.jsonl"
    p.write_text(_jsonl(*objs), encoding="utf-8")

    entries = _webapp._transcript_tail_entries(p, n=5)
    assert len(entries) == 5
    assert entries[-1].endswith("step 24")
    assert entries[0].endswith("step 20")


def test_transcript_tail_respects_max_bytes_window(tmp_path):
    """A file larger than the tail window: entries before the window are NOT returned, and a
    partial first line (split mid-record by the seek) is dropped rather than mis-parsed."""
    import re
    from datetime import timedelta

    base = datetime(2026, 9, 1, 9, 0, 0)
    # Each padding line is index-tagged and long enough that a chunk of the 200 exceed the
    # 64 KB default tail window (the text itself gets truncated to 200 chars — that's a
    # separate, already-covered behaviour; this test only cares about which lines survive).
    padding = [_assistant((base + timedelta(seconds=i)).isoformat() + "Z", [_text(f"pad-{i:03d} " + "p" * 500)])
               for i in range(200)]
    marker = _assistant((base + timedelta(hours=2)).isoformat() + "Z", [_text("MARKER after the window")])
    p = tmp_path / "agent-a3.jsonl"
    p.write_text(_jsonl(*padding) + _jsonl(marker), encoding="utf-8")
    assert p.stat().st_size > 65536

    entries = _webapp._transcript_tail_entries(p, n=1000, max_bytes=65536)
    joined = "\n".join(entries)
    # The earliest padding lines are well outside the last-64KB window — must not appear.
    assert "pad-000 " not in joined
    assert "pad-001 " not in joined
    # The marker (written last) and some late-index padding (inside the window) DO survive.
    assert "MARKER after the window" in joined
    assert "pad-199 " in joined
    # Every returned entry parsed a full, well-formed JSON line — a partial first line (cut
    # mid-record by the seek) would either fail to parse (dropped, fine) or, if it happened to
    # parse as valid JSON some other way, would not carry a real timestamp. Assert every
    # surviving entry has one — proof no mis-parsed artifact slipped through.
    assert entries  # sanity: the window did retain something
    for e in entries:
        assert re.match(r"^\[\d\d:\d\d:\d\d\] ✎ ", e), f"malformed or unparsed entry: {e!r}"


def test_transcript_tail_missing_file_returns_empty(tmp_path):
    assert _webapp._transcript_tail_entries(tmp_path / "nope.jsonl", n=20) == []


def test_transcript_tail_malformed_line_skipped(tmp_path):
    p = tmp_path / "agent-a4.jsonl"
    p.write_text("not json at all\n" + _jsonl(_assistant("2026-09-01T10:00:00Z", [_text("ok")])),
                 encoding="utf-8")
    entries = _webapp._transcript_tail_entries(p, n=20)
    assert entries == [f"[{_expected_ts('2026-09-01T10:00:00Z')}] ✎ ok"]


# ─────────────────────────── _workflow_journal_tail_entries ────────────────────


def test_workflow_journal_tail_entries(tmp_path):
    journal = tmp_path / "journal.jsonl"
    lines = [
        {"type": "started", "key": "v2:x1", "agentId": "agentA"},
        {"type": "started", "key": "v2:x2", "agentId": "agentB"},
        {"type": "result", "key": "v2:x1", "agentId": "agentA", "result": {"ok": True}},
        {"type": "failed", "key": "v2:x2", "agentId": "agentB", "error": "boom"},
    ]
    journal.write_text(_jsonl(*lines), encoding="utf-8")

    out = _webapp._workflow_journal_tail_entries(journal, n=10)
    assert out[0] == "2 agent(s): started=2 result=1 failed=1"
    assert "agentA: started → result" in out
    assert "agentB: started → failed" in out


def test_workflow_journal_tail_entries_missing_file(tmp_path):
    assert _webapp._workflow_journal_tail_entries(tmp_path / "nope.jsonl", n=10) == []


def test_workflow_journal_tail_entries_last_n_excludes_header_from_count(tmp_path):
    journal = tmp_path / "journal.jsonl"
    lines = []
    for i in range(10):
        lines.append({"type": "started", "key": f"v2:{i}", "agentId": f"agent{i}"})
    journal.write_text(_jsonl(*lines), encoding="utf-8")
    out = _webapp._workflow_journal_tail_entries(journal, n=3)
    assert out[0] == "10 agent(s): started=10 result=0 failed=0"
    assert len(out) == 4  # header + 3 agent lines
    assert out[1:] == ["agent7: started", "agent8: started", "agent9: started"]


# ─────────────────────────── GET .../monitors/{mid}/tail ───────────────────────


@pytest.fixture(autouse=True)
def reset_state():
    old_monitors = dict(_webapp._monitors)
    old_cache = dict(_webapp._TRANSCRIPT_PATH_CACHE)
    _webapp._monitors.clear()
    _webapp._TRANSCRIPT_PATH_CACHE.clear()
    yield
    _webapp._monitors.clear()
    _webapp._monitors.update(old_monitors)
    _webapp._TRANSCRIPT_PATH_CACHE.clear()
    _webapp._TRANSCRIPT_PATH_CACHE.update(old_cache)


@pytest.fixture
def sdk_layout(tmp_path):
    """<sdk>/sess1/subagents/agent-a1.jsonl (direct) and
    <sdk>/sess1/subagents/workflows/wf_run1/{agent-a2.jsonl,journal.jsonl} (workflow-internal),
    matching the exact two globs the sweeper (_agent_activity_sweep_loop) uses."""
    sdk_dir = tmp_path / "sdk"
    subagents = sdk_dir / "sess1" / "subagents"
    subagents.mkdir(parents=True)
    direct = subagents / "agent-a1.jsonl"
    direct.write_text(_jsonl(_assistant("2026-09-01T10:00:00Z", [_tool_use("Bash", command="ls")])),
                       encoding="utf-8")

    wf_dir = subagents / "workflows" / "wf_run1"
    wf_dir.mkdir(parents=True)
    nested = wf_dir / "agent-a2.jsonl"
    nested.write_text(_jsonl(_assistant("2026-09-01T10:05:00Z", [_text("nested agent step")])),
                       encoding="utf-8")
    journal = wf_dir / "journal.jsonl"
    journal.write_text(_jsonl(
        {"type": "started", "key": "v2:x1", "agentId": "agentA"},
        {"type": "result", "key": "v2:x1", "agentId": "agentA", "result": {}},
    ), encoding="utf-8")
    return sdk_dir


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
def tail_app(fake_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/projects/{id}/monitors/{mid}/tail", _webapp.api_project_monitor_tail)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _row(mid: str, kind: str = "agent", status: str = "running", **extra) -> dict:
    return {"id": mid, "kind": kind, "label": mid, "status": status,
            "started": time.time(), "ts": time.time(), "tail": "", "agent": None, **extra}


@pytest.mark.asyncio
async def test_unknown_project_404(aiohttp_client, fake_ctx, tail_app):
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/nope/monitors/a1/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_unknown_monitor_404(aiohttp_client, fake_ctx, tail_app, monkeypatch, sdk_layout):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {}
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/myproject/monitors/ghost/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 404
    body = await resp.json()
    assert body["error"] == "monitor not found"


@pytest.mark.asyncio
async def test_agent_row_direct_transcript(aiohttp_client, fake_ctx, tail_app, monkeypatch, sdk_layout):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {"a1": _row("a1")}
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/myproject/monitors/a1/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "agent"
    assert body["status"] == "running"
    assert body["path"].endswith("agent-a1.jsonl")
    assert any("Bash: ls" in ln for ln in body["lines"])


@pytest.mark.asyncio
async def test_agent_row_workflow_internal_transcript(aiohttp_client, fake_ctx, tail_app,
                                                        monkeypatch, sdk_layout):
    """The direct glob misses a workflow-internal agent — the endpoint must fall back to the
    second glob (subagents/workflows/*/agent-<id>.jsonl), same as the sweep loop does."""
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {"a2": _row("a2")}
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/myproject/monitors/a2/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert "workflows/wf_run1/agent-a2.jsonl" in body["path"]
    assert any("nested agent step" in ln for ln in body["lines"])


@pytest.mark.asyncio
async def test_stream_only_row_no_file_404(aiohttp_client, fake_ctx, tail_app, monkeypatch, sdk_layout):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {"s1": _row("s1", stream=True)}
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/myproject/monitors/s1/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 404
    body = await resp.json()
    assert body["error"] == "no transcript"


@pytest.mark.asyncio
async def test_n_clamping(aiohttp_client, fake_ctx, tail_app, monkeypatch, sdk_layout):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {"a1": _row("a1")}
    client = await aiohttp_client(tail_app)

    resp = await client.get("/api/projects/myproject/monitors/a1/tail?n=0",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert len(body["lines"]) <= 1

    # n=999 clamps to 200 — assert indirectly via a request that doesn't error and a monkeypatch
    # spy on _transcript_tail_entries to observe the clamped n.
    seen = {}
    orig = _webapp._transcript_tail_entries

    def _spy(path, n, max_bytes=65536):
        seen["n"] = n
        return orig(path, n, max_bytes)

    monkeypatch.setattr(_webapp, "_transcript_tail_entries", _spy)
    resp = await client.get("/api/projects/myproject/monitors/a1/tail?n=999",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    assert seen["n"] == 200


@pytest.mark.asyncio
async def test_path_cache_hit_skips_second_glob(aiohttp_client, fake_ctx, tail_app, monkeypatch, sdk_layout):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {"a1": _row("a1")}
    client = await aiohttp_client(tail_app)

    resp1 = await client.get("/api/projects/myproject/monitors/a1/tail",
                              headers=_auth_headers(fake_ctx))
    assert resp1.status == 200
    assert (sk, "a1") in _webapp._TRANSCRIPT_PATH_CACHE

    # Break resolution on a cache miss — if the second call still globs, it 404s.
    def _boom(cwd):
        raise AssertionError("_sdk_sessions_dir must not be called again on a cache hit")

    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", _boom)
    resp2 = await client.get("/api/projects/myproject/monitors/a1/tail",
                              headers=_auth_headers(fake_ctx))
    assert resp2.status == 200
    body2 = await resp2.json()
    assert body2["path"].endswith("agent-a1.jsonl")


@pytest.mark.asyncio
async def test_workflow_row_journal_summary(aiohttp_client, fake_ctx, tail_app, monkeypatch, sdk_layout):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk_layout)
    sk = "1001:42"
    _webapp._monitors[sk] = {"wf_run1": _row("wf_run1", kind="workflow")}
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/myproject/monitors/wf_run1/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "workflow"
    assert body["lines"][0] == "1 agent(s): started=1 result=1 failed=0"
    assert "agentA: started → result" in body["lines"]


@pytest.mark.asyncio
async def test_workflow_row_no_journal_404(aiohttp_client, fake_ctx, tail_app, monkeypatch, tmp_path):
    empty_sdk = tmp_path / "empty-sdk"
    empty_sdk.mkdir()
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: empty_sdk)
    sk = "1001:42"
    _webapp._monitors[sk] = {"wf_none": _row("wf_none", kind="workflow")}
    client = await aiohttp_client(tail_app)
    resp = await client.get("/api/projects/myproject/monitors/wf_none/tail",
                             headers=_auth_headers(fake_ctx))
    assert resp.status == 404
    body = await resp.json()
    assert body["error"] == "no transcript"

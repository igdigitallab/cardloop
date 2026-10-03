"""
spec-095 P3/P4 wiring: characterization of the endpoints Grok is about to join.

Written BEFORE the wiring and green on the commit it was written against: every test here pins what a
Claude or a Codex request gets today (history, session list, session switch, global search, usage
dashboard, manual rotate, the handoff endpoint, the queue drain's rotation-summary check). The Grok
branches must not move a byte of any of it; the Grok behaviour itself is in
`test_grok_p3p4_wiring.py`.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import webapp as _webapp

from test_grok_wiring import (  # noqa: F401 - fixtures used by name
    CHAT_ID, PROJECT_ID, SESSION_KEY, _auth, _chat_record, _seed_chat, codex_on, engines, fake_ctx,
    isolate,
)

@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_get("/api/projects/{id}/session-history", _webapp.api_project_session_history)
    a.router.add_get("/api/projects/{id}/sessions", _webapp.api_project_sessions)
    a.router.add_post("/api/projects/{id}/session", _webapp.api_project_set_session)
    a.router.add_get("/api/search", _webapp.api_search)
    a.router.add_get("/api/usage/dashboard", _webapp.api_usage_dashboard)
    a.router.add_post("/api/projects/{id}/rotate", _webapp.api_project_rotate)
    a.router.add_post("/api/projects/{id}/chats/{chat_id}/handoff", _webapp.api_project_chat_handoff)
    return a


# ───────────────────────── session history ────────────────────────────────────


async def test_history_codex_active_chat_shape(aiohttp_client, fake_ctx, app, codex_on, monkeypatch):
    _seed_chat(fake_ctx, provider="codex", codex_thread_id="thread-abc12345")
    seen = {}

    async def read_thread(tid):
        seen["tid"] = tid
        return {"thread": {"tokenUsage": {"total": {"totalTokens": 321}, "modelContextWindow": 9000}}}

    monkeypatch.setattr(_webapp._codex, "read_thread", read_thread)
    monkeypatch.setattr(_webapp._codex, "history_messages", lambda payload: [
        {"role": "user", "text": "hi <system-reminder>x</system-reminder>", "tools": []},
        {"role": "assistant", "text": "<system-reminder>kept</system-reminder>", "tools": []},
    ])
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history", headers=_auth(fake_ctx))
    assert resp.status == 200
    assert await resp.json() == {
        "messages": [{"role": "user", "text": "hi", "tools": []},
                     {"role": "assistant", "text": "<system-reminder>kept</system-reminder>", "tools": []}],
        "session_id": None, "codex_thread_id": "thread-abc12345", "provider": "codex",
        "context_tokens": 321, "context_window": 9000, "last_cache_hit_pct": None,
    }
    assert seen["tid"] == "thread-abc12345"


async def test_history_codex_explicit_thread_wins_even_on_a_claude_chat(
    aiohttp_client, fake_ctx, app, codex_on, monkeypatch
):
    _seed_chat(fake_ctx, provider="claude")
    monkeypatch.setattr(_webapp._codex, "read_thread", lambda tid: _async({"thread": {}}))
    monkeypatch.setattr(_webapp._codex, "history_messages", lambda payload: [])
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history?codex_thread_id=thread-zzzz9999",
                            headers=_auth(fake_ctx))
    body = await resp.json()
    assert body["provider"] == "codex" and body["codex_thread_id"] == "thread-zzzz9999"


async def _async(value):
    return value


async def test_history_codex_without_a_thread_is_the_empty_codex_shape(
    aiohttp_client, fake_ctx, app, codex_on
):
    _seed_chat(fake_ctx, provider="codex")
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history", headers=_auth(fake_ctx))
    assert await resp.json() == {"messages": [], "session_id": None, "codex_thread_id": None,
                                 "provider": "codex", "context_tokens": 0}


async def test_history_codex_invalid_thread_is_400_and_read_failure_is_502(
    aiohttp_client, fake_ctx, app, codex_on, monkeypatch
):
    _seed_chat(fake_ctx, provider="codex")
    client = await aiohttp_client(app)
    bad = await client.get(f"/api/projects/{PROJECT_ID}/session-history?codex_thread_id=../x",
                           headers=_auth(fake_ctx))
    assert bad.status == 400 and await bad.json() == {"error": "invalid codex_thread_id"}

    async def boom(tid):
        raise RuntimeError("app server down")

    monkeypatch.setattr(_webapp._codex, "read_thread", boom)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history?codex_thread_id=thread-ok123456",
                            headers=_auth(fake_ctx))
    assert resp.status == 502
    assert await resp.json() == {"error": "Codex history unavailable: app server down"}


async def test_history_claude_branches(aiohttp_client, fake_ctx, app, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: tmp_path / "sdk")
    _seed_chat(fake_ctx, provider="claude")
    fake_ctx["sessions"].clear()
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history", headers=_auth(fake_ctx))
    assert await resp.json() == {"messages": [], "session_id": None, "context_tokens": 0,
                                 "last_cache_hit_pct": None}
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history?session_id=abc",
                            headers=_auth(fake_ctx))
    assert await resp.json() == {"messages": [], "session_id": "abc"}
    resp = await client.get(f"/api/projects/{PROJECT_ID}/session-history?session_id=..",
                            headers=_auth(fake_ctx))
    assert resp.status == 400 and await resp.json() == {"error": "invalid session_id"}


# ───────────────────────── session list ───────────────────────────────────────


async def test_sessions_codex_rows(aiohttp_client, fake_ctx, app, codex_on, monkeypatch):
    _seed_chat(fake_ctx, provider="codex", codex_thread_id="T-ACTIVE")
    seen = {}

    async def list_threads(**kw):
        seen.update(kw)
        return [
            {"id": "T-ACTIVE", "recencyAt": 1_800_000_000, "preview": "p1", "name": "named",
             "turns": [1, 2, 3]},
            {"id": "T-OLD", "updatedAt": 1_700_000_000, "preview": None, "name": None},
        ]

    monkeypatch.setattr(_webapp._codex, "list_threads", list_threads)
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/sessions", headers=_auth(fake_ctx))
    body = await resp.json()
    assert seen == {"cwd": fake_ctx["topics"][SESSION_KEY]["cwd"], "limit": 30}
    assert body["provider"] == "codex"
    assert body["sessions"] == [
        {"session_id": "T-ACTIVE", "codex_thread_id": "T-ACTIVE", "provider": "codex",
         "last_used": "2027-01-15T08:00:00+00:00", "preview": "p1", "is_active": True,
         "label": "named", "message_count": 3, "context_tokens": None},
        {"session_id": "T-OLD", "codex_thread_id": "T-OLD", "provider": "codex",
         "last_used": "2023-11-14T22:13:20+00:00", "preview": "", "is_active": False,
         "label": None, "message_count": 0, "context_tokens": None},
    ]


async def test_sessions_codex_failure_is_an_empty_list_with_the_error(
    aiohttp_client, fake_ctx, app, codex_on, monkeypatch
):
    _seed_chat(fake_ctx, provider="codex")

    async def boom(**kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(_webapp._codex, "list_threads", boom)
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/sessions", headers=_auth(fake_ctx))
    assert await resp.json() == {"sessions": [], "provider": "codex", "error": "nope"}


async def test_sessions_claude_with_no_sdk_dir_is_empty(aiohttp_client, fake_ctx, app, tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: tmp_path / "missing")
    _seed_chat(fake_ctx, provider="claude")
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/sessions", headers=_auth(fake_ctx))
    assert await resp.json() == {"sessions": []}


async def test_sessions_claude_lists_its_transcripts(aiohttp_client, fake_ctx, app, tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "sess-1.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hello there"}}\n', encoding="utf-8")
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk)
    _seed_chat(fake_ctx, provider="claude", session_id="sess-1")
    fake_ctx["sessions"][SESSION_KEY] = "sess-1"
    client = await aiohttp_client(app)
    resp = await client.get(f"/api/projects/{PROJECT_ID}/sessions", headers=_auth(fake_ctx))
    body = await resp.json()
    assert [s["session_id"] for s in body["sessions"]] == ["sess-1"]
    assert body["sessions"][0]["is_active"] is True
    assert set(body["sessions"][0]) == {"session_id", "last_used", "preview", "is_active", "label",
                                        "message_count", "context_tokens"}


# ───────────────────────── session switch ─────────────────────────────────────


async def test_set_session_new_clears_each_providers_own_field(
    aiohttp_client, fake_ctx, app, codex_on
):
    client = await aiohttp_client(app)
    _seed_chat(fake_ctx, provider="claude", session_id="C1", codex_thread_id="X1")
    fake_ctx["sessions"][SESSION_KEY] = "C1"
    resp = await client.post(f"/api/projects/{PROJECT_ID}/session", json={"action": "new"},
                             headers=_auth(fake_ctx))
    assert await resp.json() == {"active": None}
    rec = _chat_record(fake_ctx)
    assert rec["session_id"] is None and rec["codex_thread_id"] == "X1"
    assert SESSION_KEY not in fake_ctx["sessions"]

    _seed_chat(fake_ctx, provider="codex", session_id="C2", codex_thread_id="X2")
    fake_ctx["sessions"][SESSION_KEY] = "FLAT"
    resp = await client.post(f"/api/projects/{PROJECT_ID}/session", json={"action": "new"},
                             headers=_auth(fake_ctx))
    assert await resp.json() == {"active": None}
    rec = _chat_record(fake_ctx)
    assert rec["codex_thread_id"] is None and rec["session_id"] == "C2"
    assert fake_ctx["sessions"][SESSION_KEY] == "FLAT"


async def test_set_session_resume_codex(aiohttp_client, fake_ctx, app, codex_on):
    _seed_chat(fake_ctx, provider="codex")
    client = await aiohttp_client(app)
    resp = await client.post(f"/api/projects/{PROJECT_ID}/session",
                             json={"action": "resume", "session_id": "thread-resume1"},
                             headers=_auth(fake_ctx))
    assert await resp.json() == {"active": "thread-resume1", "provider": "codex"}
    assert _chat_record(fake_ctx)["codex_thread_id"] == "thread-resume1"
    bad = await client.post(f"/api/projects/{PROJECT_ID}/session",
                            json={"action": "resume", "session_id": "../etc"}, headers=_auth(fake_ctx))
    assert bad.status == 400 and await bad.json() == {"error": "invalid Codex thread id"}


async def test_set_session_resume_claude_requires_an_existing_transcript(
    aiohttp_client, fake_ctx, app, tmp_path, monkeypatch
):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "real.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk)
    _seed_chat(fake_ctx, provider="claude")
    client = await aiohttp_client(app)
    missing = await client.post(f"/api/projects/{PROJECT_ID}/session",
                                json={"action": "resume", "session_id": "ghost"}, headers=_auth(fake_ctx))
    assert missing.status == 400 and await missing.json() == {"error": "session not found"}
    ok = await client.post(f"/api/projects/{PROJECT_ID}/session",
                           json={"action": "resume", "session_id": "real"}, headers=_auth(fake_ctx))
    assert await ok.json() == {"active": "real"}
    assert _chat_record(fake_ctx)["session_id"] == "real"
    assert fake_ctx["sessions"][SESSION_KEY] == "real"
    nope = await client.post(f"/api/projects/{PROJECT_ID}/session", json={"action": "x"},
                             headers=_auth(fake_ctx))
    assert nope.status == 400 and await nope.json() == {"error": "action must be 'new' or 'resume'"}


# ───────────────────────── global search ──────────────────────────────────────


@pytest.fixture
def search_stubs(monkeypatch):
    monkeypatch.setattr(_webapp, "_search_maybe_scan", lambda ctx: _async(None))
    monkeypatch.setattr(_webapp._search, "search_at", lambda *a, **k: [{"source": "board", "snippet": "b"}])


async def test_search_codex_threads_join_the_hits(
    aiohttp_client, fake_ctx, app, codex_on, search_stubs, monkeypatch
):
    cwd = fake_ctx["topics"][SESSION_KEY]["cwd"]

    async def list_threads(**kw):
        assert kw == {"limit": 30, "search_term": "needle"}
        return [{"id": "T1", "cwd": cwd, "recencyAt": 5, "preview": "x" * 900},
                {"id": "T2", "cwd": "/elsewhere", "recencyAt": 6, "preview": "other"}]

    monkeypatch.setattr(_webapp._codex, "list_threads", list_threads)
    client = await aiohttp_client(app)
    resp = await client.get("/api/search?q=needle", headers=_auth(fake_ctx))
    hits = (await resp.json())["hits"]
    assert hits[0] == {"source": "board", "snippet": "b"}
    assert len(hits) == 2
    hit = hits[1]
    assert hit["project_id"] == PROJECT_ID and hit["source"] == "chat" and hit["provider"] == "codex"
    assert hit["ts"] == 5 and hit["snippet"] == "x" * 800
    assert hit["ref"] == {"codex_thread_id": "T1", "provider": "codex"}


async def test_search_without_codex_returns_the_index_hits_only(
    aiohttp_client, fake_ctx, app, search_stubs
):
    client = await aiohttp_client(app)
    resp = await client.get("/api/search?q=needle", headers=_auth(fake_ctx))
    assert await resp.json() == {"hits": [{"source": "board", "snippet": "b"}]}
    assert await (await client.get("/api/search?q=", headers=_auth(fake_ctx))).json() == {"hits": []}


# ───────────────────────── usage dashboard ────────────────────────────────────


async def test_usage_dashboard_providers_block_without_grok(
    aiohttp_client, fake_ctx, app, codex_on, monkeypatch
):
    import usage_scanner
    monkeypatch.setattr(_webapp, "_maybe_scan_usage", lambda ctx: _async(None))
    monkeypatch.setattr(usage_scanner, "dashboard_data",
                        lambda **kw: {"overview": {"turns": 4, "cost": 2.5}})
    monkeypatch.setattr(_webapp._codex, "usage_rows", lambda data, days=None: [
        {"model": "gpt-x", "input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 2,
         "reasoning_output_tokens": 1}])
    client = await aiohttp_client(app)
    resp = await client.get("/api/usage/dashboard?days=7", headers=_auth(fake_ctx))
    data = await resp.json()
    assert list(data["providers"]) == ["claude", "codex"]
    assert data["providers"]["claude"] == {"turns": 4, "cost": 2.5, "subscription_cost_available": True}
    assert data["providers"]["codex"] == {
        "turns": 1, "input": 10, "output": 5, "cached_input": 2, "reasoning_output": 1,
        "by_model": [{"model": "gpt-x", "turns": 1, "input": 10, "output": 5, "cached_input": 2,
                      "reasoning_output": 1}],
        "subscription_cost_available": False, "cost": None,
    }


# ───────────────────────── manual rotate (Claude path) ────────────────────────


async def test_rotate_claude_clears_both_layers_and_stores_the_summary(
    aiohttp_client, fake_ctx, app, monkeypatch
):
    _seed_chat(fake_ctx, provider="claude", session_id="C-SID")
    fake_ctx["sessions"][SESSION_KEY] = "C-SID"
    fake_ctx["pending_handoff"] = {}
    fake_ctx["live_clients"] = {}
    calls = []

    async def build(ctx, key, cwd, sid):
        calls.append((key, cwd, sid))
        return "SUMMARY"

    async def title(summary):
        return None

    monkeypatch.setattr(_webapp, "_build_handoff", build)
    monkeypatch.setattr(_webapp, "_build_session_title", title)
    client = await aiohttp_client(app)
    resp = await client.post(f"/api/projects/{PROJECT_ID}/rotate", json={"handoff": True},
                             headers=_auth(fake_ctx))
    assert await resp.json() == {"ok": True, "reset": True, "handoff": True}
    assert calls == [(SESSION_KEY, fake_ctx["topics"][SESSION_KEY]["cwd"], "C-SID")]
    assert fake_ctx["pending_handoff"] == {SESSION_KEY: "SUMMARY"}
    assert _chat_record(fake_ctx)["session_id"] is None
    assert SESSION_KEY not in fake_ctx["sessions"]
    assert fake_ctx["running"].get(SESSION_KEY) is None


async def test_rotate_claude_without_a_session_is_a_no_op(aiohttp_client, fake_ctx, app):
    _seed_chat(fake_ctx, provider="claude")
    fake_ctx["sessions"].clear()
    fake_ctx["live_clients"] = {}
    client = await aiohttp_client(app)
    resp = await client.post(f"/api/projects/{PROJECT_ID}/rotate", json={}, headers=_auth(fake_ctx))
    assert await resp.json() == {"ok": True, "reset": False, "reason": "no active session"}


# ───────────────────────── handoff endpoint ───────────────────────────────────


async def test_handoff_preview_builds_from_the_posted_messages(aiohttp_client, fake_ctx, app, codex_on):
    _seed_chat(fake_ctx, provider="codex")
    client = await aiohttp_client(app)
    msgs = [{"role": "user", "text": "never touch webapp.py", "tools": []},
            {"role": "assistant", "text": "ok", "tools": [{"kind": "edit", "file": "a.py"}]}]
    resp = await client.post(f"/api/projects/{PROJECT_ID}/chats/{CHAT_ID}/handoff",
                             json={"messages": msgs, "from_label": "Codex", "to_label": "Claude"},
                             headers=_auth(fake_ctx))
    built = (await resp.json())["handoff"]
    assert built["constraints"] == ["never touch webapp.py"]
    assert built["files"] == ["a.py"] and built["unreplayed"] == 0
    assert built["text"].startswith("# Handoff: Codex → Claude\n")
    assert "## Standing constraints (verbatim, from the operator)\n- never touch webapp.py" in built["text"]
    assert "[operator] never touch webapp.py" in built["text"]
    assert "[previous engine] ok" in built["text"]
    assert set(built) == {"text", "constraints", "files", "recent", "unreplayed"}
    assert _chat_record(fake_ctx).get("runtime_handoff") is None


async def test_handoff_commit_arms_the_block_for_the_chats_current_provider(
    aiohttp_client, fake_ctx, app, codex_on
):
    _seed_chat(fake_ctx, provider="codex")
    client = await aiohttp_client(app)
    resp = await client.post(f"/api/projects/{PROJECT_ID}/chats/{CHAT_ID}/handoff",
                             json={"messages": [], "from_label": "Claude", "to_label": "Codex",
                                   "commit": True, "text": "EDITED"}, headers=_auth(fake_ctx))
    assert (await resp.json())["armed"] is True
    armed = _chat_record(fake_ctx)["runtime_handoff"]
    assert {k: armed[k] for k in ("text", "from_label", "to_label", "for_provider", "for_backend")} == {
        "text": "EDITED", "from_label": "Claude", "to_label": "Codex", "for_provider": "codex",
        "for_backend": ""}


def test_build_handoff_without_trust_tags_is_the_pre_grok_text():
    """Pinned to the exact text the builder produced before the verified-row work: rows that carry
    no `verified` key (every Claude and Codex row) are trusted and render exactly as they did."""
    import handoff as hf

    out = hf.build_handoff(
        [{"role": "user", "text": "do not push\nthen continue", "tools": []},
         {"role": "assistant", "text": "done", "tools": [{"file": "x.py"}]}],
        from_label="A", to_label="B")
    assert out["text"] == "\n".join([
        "# Handoff: A → B", "",
        "This conversation was running on A and continues here. You do NOT have its transcript: "
        "0 earlier message(s) were not replayed. Everything you can rely on is below. Do not assume "
        "work you cannot see is absent — ask before redoing or reverting anything.",
        "", "## Standing constraints (verbatim, from the operator)", "- do not push",
        "", "## Files this session touched", "- x.py",
        "", "## Last messages (raw)", "[operator] do not push then continue", "[previous engine] done",
        "", "---"])
    assert set(out) == {"text", "constraints", "files", "recent", "unreplayed"}

"""
spec-095 P2: Grok wired through the provider seam and the D5 privacy gate.

Three families, all written against the behaviour a caller can observe:

  * characterization — the exact kwargs the Grok engine factory receives at each of the three run
    sites (queue drain, direct chat POST, board card) and exactly what is written back, in the
    style of `test_provider_seam.py`; every fake engine result carries the OTHER providers' id
    keys poisoned, so an id read through the wrong key is visible;
  * (historical) the per-project gate — removed 2026-10-03: choosing Grok IS the consent; the generic
    gate seam stays in providers.py with no provider using it. It used to answer HTTP 409 at every
    selection site, the same refusal at every RUN site (the project can lose the flag after the
    message was accepted), never a fall back to another provider;
  * the registry — the Grok row, the disabled state, the capability errors.

Each guard here was proved to bite with a line-anchored single-branch mutation pass (the harness is
a throwaway; the evidence lives in the P2 report).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import board as _board
import grok_engine
import providers
import runtime
import webapp as _webapp
from webapp import _derive_token

SESSION_KEY = "1001:42"
PROJECT_ID = "myproject"
CHAT_ID = "aaaaaa"
CLAUDE_FLAT = "CLAUDE-FLAT-SESSION"
REFUSAL = "grok is not enabled for this project"

GROK_CHAT_KEYS = {
    "project_name", "cwd", "prompt", "session_key", "model", "resume_session_id", "ctx",
    "ephemeral", "effort", "multi_agent", "plan_mode", "chat_id", "entrypoint",
}
GROK_CARD_KEYS = {
    "project_name", "cwd", "prompt", "session_key", "model", "resume_session_id", "ctx",
    "ephemeral", "effort", "plan_mode", "multi_agent", "entrypoint",
}

# provider -> (continuity field, result key, model)
SHAPES = {
    "claude": ("session_id", "session_id", "opus"),
    "codex": ("codex_thread_id", "thread_id", "gpt-5.6-sol"),
    "grok": ("grok_session_id", "provider_session_id", "grok-4.7-build-fast"),
}


# ─────────────────────────── fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _every_grok_session_exists(monkeypatch):
    """These tests pin the kwargs/write-back of the run sites with made-up ids ("OLD-ID"); the run
    sites now drop a resume id Grok no longer has (P3), so the existence check is stubbed true
    here. The drop itself is tested in test_grok_p3p4_wiring.py with real session files (which
    imports `isolate` below but not this)."""
    monkeypatch.setattr(_webapp._grok_history, "session_exists", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    old_file = _webapp._CHAT_QUEUE_FILE
    old_queue = dict(_webapp._CHAT_QUEUE)
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = tmp_path / "chat-queue.json"
    _webapp._live_turns.pop(SESSION_KEY, None)
    yield
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old_queue)
    _webapp._CHAT_QUEUE_FILE = old_file
    _webapp._monitors.clear()
    _webapp._bg_run_ids.clear()
    _webapp._bg_run_started.clear()
    _webapp._live_turns.pop(SESSION_KEY, None)


@pytest.fixture
def engines():
    """Per-provider call logs. Every engine answers with ITS OWN id under its own result key and
    a poison value under the other providers' keys."""
    return {"claude": [], "codex": [], "grok": []}


def _recording_engine(name, log):
    own_key = SHAPES[name][1]

    async def engine(**kwargs):
        log[name].append(kwargs)
        yield {"type": "text", "text": "answer"}
        event = {"type": "result", "context_tokens": 7, "session_id": "WRONG-ID",
                 "thread_id": "WRONG-ID", "provider_session_id": "WRONG-ID"}
        event[own_key] = "NEW-ID"
        yield event
    return engine


@pytest.fixture
def fake_ctx(tmp_path, engines):
    data = tmp_path / "data"
    data.mkdir()
    proj = tmp_path / PROJECT_ID
    proj.mkdir()
    ctx = {
        "topics": {SESSION_KEY: {"project": PROJECT_ID, "cwd": str(proj), "model": "sonnet"}},
        "sessions": {SESSION_KEY: CLAUDE_FLAT},
        "running": {},
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "MODELS": {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"},
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": _recording_engine("claude", engines),
        "run_codex_engine": _recording_engine("codex", engines),
        "run_grok_engine": _recording_engine("grok", engines),
        "ptb_app": None,
        "rate_limits": {},
        "cwd_locks": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    return ctx


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    a.router.add_post("/api/projects/{id}/chat/queue", _webapp.api_chat_queue_add)
    a.router.add_get("/api/projects/{id}/chats", _webapp.api_project_chats_list)
    a.router.add_post("/api/projects/{id}/chats", _webapp.api_project_chats_create)
    a.router.add_route("PATCH", "/api/projects/{id}/chats/{chat_id}",
                       _webapp.api_project_chats_patch)
    a.router.add_post("/api/free", _webapp.api_free_create)
    a.router.add_post("/api/projects/{id}/tasks", _webapp.api_create_task)
    a.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", _webapp.api_update_task)
    a.router.add_post("/api/projects/{id}/tasks/{card}/move", _webapp.api_move_task)
    a.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    a.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    a.router.add_post("/api/projects/{id}/chat/stop", _webapp.api_project_chat_stop)
    a.router.add_get("/api/projects", _webapp.api_projects)
    a.router.add_get("/api/agent-providers", _webapp.api_agent_providers)
    return a


def _grok_info(*, available=True, models=("grok-4.7", "grok-4.7-build-fast", "grok-4.6"),
               **extra):
    info = {
        "provider": "grok", "enabled": True, "available": available,
        "authenticated": available, "auth_type": "oidc" if available else None,
        "plan_type": "SuperGrok", "reasoning_levels": ["low", "medium", "high", "xhigh"],
        "models": [{"value": m, "label": m, "default": m == models[0]} for m in models],
        "capabilities": grok_engine.capabilities(), "error": None if available else "down",
        "version": "1.0.46", "warnings": [],
        "sandbox": {"profile": "cardloop", "deny_count": 12, "bwrap": "/usr/bin/bwrap",
                    "probe": "ok"},
    }
    info.update(extra)
    return info


@pytest.fixture
def grok_on(monkeypatch, fake_ctx):
    """Grok switched on and available (flag + registry), the project NOT yet opted in."""
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: True)

    async def info():
        return _grok_info()
    fake_ctx["grok_provider_info"] = info


@pytest.fixture
def codex_on(monkeypatch):
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)


async def _sse_events(resp) -> list:
    import json
    body = await resp.read()
    out = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except Exception:
                pass
    return out


def _seed_chat(ctx, *, provider, model=None, **extra):
    chat = {"id": CHAT_ID, "name": "Main", "provider": provider,
            "model": SHAPES[provider][2] if model is None else model, **extra}
    _webapp._save_chats(ctx, {PROJECT_ID: {"active": CHAT_ID, "chats": [chat]}})


def _chat_record(ctx) -> dict:
    return _webapp._load_chats(ctx)[PROJECT_ID]["chats"][0]


def _no_engine_calls(engines):
    return {k: len(v) for k, v in engines.items()} == {"claude": 0, "codex": 0, "grok": 0}


_PATCHES = (
    patch.object(_webapp, "_secrets_read", return_value={}),
    patch.object(_webapp, "_build_agents_kwargs", return_value={}),
)


async def _drain(fake_ctx, item_kwargs):
    item = _webapp._chat_queue_enqueue(SESSION_KEY, "queued text", **item_kwargs)
    assert item is not None
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is True
        await asyncio.sleep(0.05)


def _live_events():
    return list((_webapp._live_turns.get(SESSION_KEY) or {}).get("events") or [])


# ─────────────────── characterization: queue drain (run site 1) ───────────────


@pytest.mark.asyncio
async def test_queue_drain_grok_kwargs_and_writeback(fake_ctx, engines, grok_on):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID",
               runtime_handoff={"text": "handoff-text", "for_provider": "grok",
                                "for_backend": "", "from_label": "A", "to_label": "B"})
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert pinned == {"provider": "grok", "model": "grok-4.7-build-fast"}
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned,
                                effort="high", ultracode=True))

    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [0, 0, 1]
    kw = engines["grok"][0]
    assert set(kw) == GROK_CHAT_KEYS, set(kw) ^ GROK_CHAT_KEYS
    assert kw["ctx"] is fake_ctx and kw["model"] == "grok-4.7-build-fast"
    assert kw["resume_session_id"] == "OLD-ID"
    assert kw["ephemeral"] is False and kw["chat_id"] == CHAT_ID and kw["entrypoint"] == "chat"
    assert kw["effort"] == "high" and kw["multi_agent"] is True and kw["plan_mode"] is False
    assert kw["project_name"] == "myproject" and kw["session_key"] == SESSION_KEY
    assert kw["prompt"].startswith("handoff-text\n\n") and kw["prompt"].endswith("queued text")

    rec = _chat_record(fake_ctx)
    assert rec["grok_session_id"] == "NEW-ID", "the id lands in Grok's own continuity field"
    assert rec.get("session_id") is None and rec.get("codex_thread_id") is None
    assert "runtime_handoff" not in rec
    assert fake_ctx["sessions"][SESSION_KEY] == CLAUDE_FLAT, "grok never touches the claude mirror"


@pytest.mark.asyncio
async def test_queue_drain_free_grok_chat_persists_its_id_on_the_free_record(
    fake_ctx, engines, grok_on, tmp_path
):
    fid = "free-grok0001"
    _webapp._save_free_chats(fake_ctx, {fid: {
        "label": "free", "cwd": str(tmp_path), "model": "grok-4.7", "provider": "grok",
        "session_id": None, "codex_thread_id": None,
        "grok_session_id": None, "created_at": 1}})
    fake_ctx["topics"].clear()
    _webapp._ensure_chat_entry(fake_ctx, fid, fid)
    pinned = {"provider": "grok", "model": "grok-4.7"}
    item = _webapp._chat_queue_enqueue(fid, "hello", project_id=fid, pinned_runtime=pinned)
    assert item is not None
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, fid) is True
        await asyncio.sleep(0.05)
    assert len(engines["grok"]) == 1 and not engines["claude"] and not engines["codex"]
    assert _webapp._load_free_chats(fake_ctx)[fid]["grok_session_id"] == "NEW-ID"
    assert _webapp._load_free_chats(fake_ctx)[fid]["session_id"] is None


@pytest.mark.asyncio
async def test_queue_pinning_a_message_accepted_on_grok_still_runs_on_grok_after_a_switch(
    fake_ctx, engines, grok_on
):
    """The queue pins the PROVIDER at accept time: the chat flips to Claude and the project's
    defaults change while the message waits — it still runs on Grok, with Grok's own id."""
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-THREAD")
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert pinned["provider"] == "grok"
    # ... the operator switches the chat to Claude and changes the project defaults
    _seed_chat(fake_ctx, provider="claude", model="opus", session_id="CLAUDE-OWN",
               grok_session_id="GROK-THREAD")
    fake_ctx["topics"][SESSION_KEY]["board_provider"] = "claude"
    fake_ctx["topics"][SESSION_KEY]["grok_model"] = "grok-4.6"
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID, pinned_runtime=pinned))
    assert len(engines["grok"]) == 1 and not engines["claude"]
    assert engines["grok"][0]["resume_session_id"] == "GROK-THREAD"
    assert engines["grok"][0]["model"] == "grok-4.7-build-fast", "the pinned model, not the new default"
    rec = _chat_record(fake_ctx)
    assert rec["grok_session_id"] == "NEW-ID" and rec["session_id"] == "CLAUDE-OWN"
    assert rec["provider"] == "claude", "the pin does not rewrite the chat's own selection"


@pytest.mark.asyncio
async def test_queue_drain_grok_never_inherits_claudes_flat_session(fake_ctx, engines, grok_on):
    """If resolving the chat blows up after the provider was assigned, the legacy flat-map
    fallback holds CLAUDE's id — it must never reach a Grok run as a resume id."""
    _seed_chat(fake_ctx, provider="grok", grok_session_id="GROK-THREAD")

    def boom(*_a, **_k):
        raise RuntimeError("account inspection failed")

    with patch.object(_webapp, "_resolve_run_account", side_effect=boom):
        await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                    pinned_runtime={"provider": "grok", "model": "grok-4.7"}))
    assert engines["grok"][0]["resume_session_id"] is None
    assert fake_ctx["sessions"][SESSION_KEY] == CLAUDE_FLAT


@pytest.mark.asyncio
async def test_queue_drain_grok_writeback_failure_never_falls_back_to_the_flat_map(
    fake_ctx, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID")

    def boom(*_a, **_k):
        raise OSError("disk full")

    with patch.object(_webapp, "_save_chats", side_effect=boom):
        await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                    pinned_runtime={"provider": "grok", "model": "grok-4.7"}))
    assert fake_ctx["sessions"][SESSION_KEY] == CLAUDE_FLAT


@pytest.mark.asyncio
async def test_queue_drain_clears_the_flags_grok_cannot_honour_loudly(
    fake_ctx, engines, grok_on, capsys
):
    """Parity with Codex: a message already accepted is not dropped — the flag is cleared and
    the log names it. (The direct POST refuses instead; see the capability tests below.)"""
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID")
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID, plan_mode=True,
                                ask_mode=True,
                                pinned_runtime={"provider": "grok", "model": "grok-4.7"}))
    assert "clearing the incompatible flag(s)" in capsys.readouterr().out
    assert engines["grok"][0]["plan_mode"] is False


# ─────────────────── characterization: direct chat POST (run site 2) ──────────


@pytest.mark.asyncio
async def test_chat_post_grok_kwargs_writeback_and_result_frame(
    aiohttp_client, fake_ctx, app, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID",
               runtime_handoff={"text": "handoff-text", "for_provider": "grok",
                                "for_backend": "", "from_label": "A", "to_label": "B"})
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello", "chat_id": CHAT_ID,
                                       "think_mode": "xhigh", "ultracode": True},
                                 headers=_auth(fake_ctx))
        assert resp.status == 200, await resp.text()
        events = await _sse_events(resp)

    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [0, 0, 1]
    kw = engines["grok"][0]
    assert set(kw) == GROK_CHAT_KEYS, set(kw) ^ GROK_CHAT_KEYS
    assert kw["ctx"] is fake_ctx and kw["model"] == "grok-4.7-build-fast"
    assert kw["resume_session_id"] == "OLD-ID"
    assert kw["ephemeral"] is False and kw["chat_id"] == CHAT_ID and kw["entrypoint"] == "chat"
    assert kw["effort"] == "xhigh" and kw["multi_agent"] is True and kw["plan_mode"] is False
    assert kw["prompt"].endswith("hello") and "handoff-text" in kw["prompt"]

    rec = _chat_record(fake_ctx)
    assert rec["grok_session_id"] == "NEW-ID"
    assert rec.get("session_id") is None and rec.get("codex_thread_id") is None
    assert "runtime_handoff" not in rec
    assert fake_ctx["sessions"][SESSION_KEY] == CLAUDE_FLAT

    result = next(e for e in events if e.get("type") == "result")
    assert result["provider"] == "grok"
    assert result["grok_session_id"] == "NEW-ID"
    assert result["session_id"] is None and result["codex_thread_id"] is None


@pytest.mark.asyncio
async def test_chat_post_grok_without_a_chat_model_uses_the_projects_grok_model(
    aiohttp_client, fake_ctx, app, engines, grok_on
):
    fake_ctx["topics"][SESSION_KEY]["grok_model"] = "grok-4.6"
    _seed_chat(fake_ctx, provider="grok", model="")
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat", json={"prompt": "x"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert engines["grok"][0]["model"] == "grok-4.6"


@pytest.mark.asyncio
async def test_chat_post_grok_model_falls_back_to_the_builtin_default(
    aiohttp_client, fake_ctx, app, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", model="")
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat", json={"prompt": "x"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert engines["grok"][0]["model"] == grok_engine.DEFAULT_GROK_MODEL


# ─────────────────── characterization: board card (run site 3) ────────────────


def _card_project(tmp_path, **extra):
    cwd = tmp_path / "cardproj"
    cwd.mkdir(exist_ok=True)
    return {"name": "myproject", "cwd": str(cwd), "session_key": SESSION_KEY, "model": "sonnet",
            **extra}


async def _run_card_with(fake_ctx, tmp_path, *, project_extra=None, card_extra=None):
    project = _card_project(tmp_path, **(project_extra or {}))
    card = {"id": "aabbcc", "text": "Build feature", "description": None, **(card_extra or {})}
    fake_ctx["sessions"][SESSION_KEY] = "shared-chat-session"
    fake_ctx["running"][SESSION_KEY] = True
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        await _webapp._run_card(fake_ctx, None, project, card, SESSION_KEY, run_mode="legacy")
    return project


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["card_override", "project_board_provider"])
async def test_card_grok_runs_on_grok_with_exact_kwargs(fake_ctx, tmp_path, engines, how):
    if how == "card_override":
        extra = dict(card_extra={"provider": "grok", "model": "grok-card"})
        expect_model = "grok-card"
    else:
        extra = dict(project_extra={"board_provider": "grok", "grok_model": "grok-proj"})
        expect_model = "grok-proj"
    await _run_card_with(fake_ctx, tmp_path, **extra)
    assert [len(engines[k]) for k in ("claude", "codex", "grok")] == [0, 0, 1]
    kw = engines["grok"][0]
    assert set(kw) == GROK_CARD_KEYS, set(kw) ^ GROK_CARD_KEYS
    assert kw["model"] == expect_model
    assert kw["resume_session_id"] is None and kw["ephemeral"] is True
    assert kw["entrypoint"] == "card"
    assert kw["effort"] is None and kw["plan_mode"] is False and kw["multi_agent"] is False
    assert fake_ctx["sessions"][SESSION_KEY] == "shared-chat-session", "cards never write sessions"


@pytest.mark.asyncio
async def test_card_grok_model_falls_back_to_the_grok_default(fake_ctx, tmp_path, engines):
    await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "grok"})
    assert engines["grok"][0]["model"] == grok_engine.DEFAULT_GROK_MODEL


@pytest.mark.asyncio
async def test_card_on_a_project_pinned_to_the_local_backend_runs_on_claude_not_grok(
    fake_ctx, tmp_path, engines
):
    """The spec-092 local pin overrides the card's own provider — and so does it for Grok, so
    the gate is judged on Claude (no refusal) and the card never reaches xAI."""
    async def fine(ctx, chat, provider, project):
        return "ollama", ""

    async def coerce(ctx, backend, model):
        return model, ""

    with patch.object(_webapp, "_resolve_run_backend", side_effect=fine), \
         patch.object(_webapp, "_coerce_model_for_backend", side_effect=coerce):
        await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "grok"},
                             project_extra={"backend": "ollama"})
    assert len(engines["claude"]) == 1 and not engines["grok"]


@pytest.mark.parametrize("card_provider,board_provider,expected", [
    ("grok", None, "grok"), (None, "grok", "grok"), ("claude", "grok", "claude"),
    ("grok", "codex", "grok"), ("vertex", "grok", "grok"), (None, "vertex", "claude"),
])
def test_effective_card_provider_with_grok(card_provider, board_provider, expected):
    card = {"provider": card_provider} if card_provider else {}
    project = {"board_provider": board_provider} if board_provider else {}
    assert _webapp._effective_card_provider(card, project) == expected


def test_card_provider_for_run_local_pin_beats_every_adapter():
    project = {"board_provider": "grok", "backend": "ollama"}
    assert _webapp._card_provider_for_run({"provider": "grok"}, project) == "claude"
    assert _webapp._card_provider_for_run({"provider": "grok"}, {"backend": ""}) == "grok"


@pytest.mark.parametrize("meta,expected", [
    ("provider=grok", {"provider": "grok"}),
    ("provider=grok model=grok-4.6", {"provider": "grok", "model": "grok-4.6"}),
    ("provider=claude model=grok-4.6", {"provider": "claude"}),
    ("provider=vertex", {}),
])
def test_board_marker_accepts_grok(meta, expected):
    assert _board._parse_marker_meta(meta) == expected


def test_board_marker_grok_round_trip():
    cols = {key: [] for key, _label, _status in _board.BOARD_COLUMNS}
    first = _board.BOARD_COLUMNS[0][0]
    cols[first] = [{"id": "aaaaaa", "text": "one", "provider": "grok", "model": "grok-4.6"}]
    text = _board._serialize_tasks("# T", cols, "p")
    assert "<!--ops:aaaaaa provider=grok model=grok-4.6-->" in text
    _pre, parsed = _board._parse_tasks(text)
    assert parsed[first][0]["provider"] == "grok" and parsed[first][0]["model"] == "grok-4.6"


# ─────────────────── the gate: selection sites => 409 ─────────────────────────


def test_gate_refusal_is_strict_about_unknown_providers():
    with pytest.raises(KeyError):
        _webapp._provider_gate_refusal({}, "vertex")


@pytest.mark.asyncio
async def test_chat_create_on_grok_needs_no_project_flag(
    aiohttp_client, fake_ctx, app, grok_on, monkeypatch
):
    client = await aiohttp_client(app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/chats", json={"provider": "grok"},
                          headers=_auth(fake_ctx))
    assert r.status == 201


@pytest.mark.asyncio
async def test_claude_and_codex_chats_are_never_gated(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    for provider in ("claude", "codex"):
        r = await client.post(f"/api/projects/{PROJECT_ID}/chats", json={"provider": provider},
                              headers=_auth(fake_ctx))
        assert r.status == 201, provider


@pytest.mark.asyncio
async def test_free_chat_payload_for_claude_and_codex_carries_no_gate_key(
    aiohttp_client, fake_ctx, app
):
    client = await aiohttp_client(app)
    for provider in ("claude", "codex"):
        r = await client.post("/api/free", json={"provider": provider}, headers=_auth(fake_ctx))
        body = await r.json()
        assert "grok_allowed" not in body, provider


@pytest.mark.asyncio
async def test_runtime_patch_validates_the_grok_model_against_the_registry(
    aiohttp_client, fake_ctx, app, grok_on
):
    _seed_chat(fake_ctx, provider="claude", model="opus")
    client = await aiohttp_client(app)
    url = f"/api/projects/{PROJECT_ID}/chats/{CHAT_ID}"
    r = await client.patch(url, json={"provider": "grok", "model": "grok-9000",
                                      "expected_revision": 0}, headers=_auth(fake_ctx))
    assert r.status == 400 and "does not belong" in (await r.json())["error"]
    r = await client.patch(url, json={"provider": "grok", "model": "gpt-5.6-sol",
                                      "expected_revision": 0}, headers=_auth(fake_ctx))
    assert r.status == 400


@pytest.mark.asyncio
async def test_runtime_patch_to_an_unavailable_grok_is_a_400_not_a_switch(
    aiohttp_client, fake_ctx, app, monkeypatch
):
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: True)

    async def down():
        return _grok_info(available=False)
    fake_ctx["grok_provider_info"] = down
    _seed_chat(fake_ctx, provider="claude", model="opus")
    client = await aiohttp_client(app)
    r = await client.patch(f"/api/projects/{PROJECT_ID}/chats/{CHAT_ID}",
                           json={"provider": "grok", "model": "grok-4.7", "expected_revision": 0},
                           headers=_auth(fake_ctx))
    assert r.status == 400 and "not currently available" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_card_run_via_move_on_a_locally_pinned_project_is_judged_on_claude(
    aiohttp_client, fake_ctx, app, engines
):
    """The local-backend pin overrides the card's provider at run time, so the pre-check must
    judge what will actually run — a pinned project is not refused for a Grok card it will run
    on Claude."""
    cwd = fake_ctx["topics"][SESSION_KEY]["cwd"]
    fake_ctx["topics"][SESSION_KEY]["backend"] = "ollama"
    _webapp._save_board(cwd, "myproject", "# T", {
        "backlog": [{"id": "aabbcc", "text": "do it", "provider": "grok"}],
        "in_progress": [], "review": [], "failed": []})
    started = []

    async def fake_start(ctx, app_, project, card_id):
        started.append(card_id)
        return {"started": True, "card_id": card_id}

    client = await aiohttp_client(app)
    with patch.object(_webapp, "_start_card_run", side_effect=fake_start):
        r = await client.post(f"/api/projects/{PROJECT_ID}/tasks/aabbcc/move",
                              json={"to": "in_progress"}, headers=_auth(fake_ctx))
    assert r.status == 200, await r.text()
    assert started == ["aabbcc"]


@pytest.mark.asyncio
async def test_runtime_patch_with_an_unregistered_provider_is_a_400_not_a_crash(
    aiohttp_client, fake_ctx, app, grok_on
):
    _seed_chat(fake_ctx, provider="claude", model="opus")
    client = await aiohttp_client(app)
    r = await client.patch(f"/api/projects/{PROJECT_ID}/chats/{CHAT_ID}",
                           json={"provider": "vertex", "model": "m", "expected_revision": 0},
                           headers=_auth(fake_ctx))
    assert r.status == 400 and "unknown provider" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_chat_post_on_grok_needs_no_project_flag(
    aiohttp_client, fake_ctx, app, engines, grok_on, monkeypatch
):
    _seed_chat(fake_ctx, provider="grok")
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat", json={"prompt": "x"},
                                 headers=_auth(fake_ctx))
        assert resp.status == 200
        await _sse_events(resp)
    assert len(engines["grok"]) == 1


# ─────────────────── the gate: RUN sites (no fall back, ever) ─────────────────


@pytest.mark.asyncio
async def test_queue_drain_runs_a_grok_item_without_any_project_flag(
    fake_ctx, engines, grok_on, monkeypatch
):
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID")
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                pinned_runtime={"provider": "grok", "model": "grok-4.7"}))
    assert len(engines["grok"]) == 1 and not engines["claude"]


@pytest.mark.asyncio
async def test_card_run_on_grok_needs_no_project_flag(fake_ctx, tmp_path, engines):
    await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "grok"})
    assert len(engines["grok"]) == 1 and not engines["claude"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_ungated_providers_never_pay_for_the_gate_lookup(
    fake_ctx, engines, codex_on, provider
):
    """Claude and Codex runs must do exactly the work they did before the gate existed: the live
    project lookup happens only for a provider that HAS a gate."""
    _field, _key, model = SHAPES[provider]
    _seed_chat(fake_ctx, provider=provider, model=model)

    def boom(*_a, **_k):
        raise AssertionError("the project registry must not be read for an ungated provider")

    with patch.object(_webapp, "_find_project_by_id", side_effect=boom), \
         patch.object(_webapp, "_collect_projects", side_effect=boom):
        await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                    pinned_runtime={"provider": provider, "model": model}))
    assert len(engines[provider]) == 1


# ─────────────────── unknown / disabled: error, never Claude ──────────────────


@pytest.mark.asyncio
async def test_queue_drain_unknown_pinned_provider_errors_and_never_runs_claude(
    fake_ctx, engines, capsys
):
    _seed_chat(fake_ctx, provider="claude", model="opus", session_id="OLD")
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                pinned_runtime={"provider": "vertex", "model": "m"}))
    assert _no_engine_calls(engines)
    assert "unknown provider 'vertex'" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_chat_post_unknown_chat_provider_is_a_400_never_claude(
    aiohttp_client, fake_ctx, app, engines
):
    _seed_chat(fake_ctx, provider="vertex", model="m")
    client = await aiohttp_client(app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                          json={"prompt": "x", "chat_id": CHAT_ID}, headers=_auth(fake_ctx))
    assert r.status == 400 and "not a registered provider" in (await r.json())["error"]
    assert _no_engine_calls(engines)


@pytest.mark.asyncio
async def test_card_with_an_unregistered_provider_value_never_selects_grok_or_claude_by_accident(
    fake_ctx, tmp_path, engines
):
    await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "vertex"})
    assert len(engines["claude"]) == 1 and not engines["grok"], \
        "an unknown card value is ignored (documented: project default, else Claude)"


def test_grok_disabled_registry_hides_the_row_and_the_known_map_says_off(monkeypatch, fake_ctx):
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: False)
    assert _webapp._known_agent_providers()["grok"] is False


@pytest.mark.asyncio
async def test_disabled_grok_is_hidden_from_the_registry_payload(
    aiohttp_client, fake_ctx, app, monkeypatch
):
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: False)
    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [])
    client = await aiohttp_client(app)
    body = await (await client.get("/api/agent-providers", headers=_auth(fake_ctx))).json()
    assert [p["provider"] for p in body["providers"]] == ["claude", "codex"]


@pytest.mark.asyncio
async def test_a_chat_pinned_to_disabled_grok_shows_unavailable_and_never_runs_on_claude(
    aiohttp_client, fake_ctx, app, engines, monkeypatch
):
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: False)
    _seed_chat(fake_ctx, provider="grok", grok_session_id="G1")
    client = await aiohttp_client(app)
    # shown as Grok-unavailable (the record is preserved, not rewritten to Claude)
    listing = await (await client.get(f"/api/projects/{PROJECT_ID}/chats",
                                      headers=_auth(fake_ctx))).json()
    chat = next(c for c in listing["chats"] if c["id"] == CHAT_ID)
    assert chat["provider"] == "grok" and chat["provider_status"] == "unavailable"
    assert chat["grok_session_id"] == "G1"
    # a direct send errors out
    r = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                          json={"prompt": "hi", "chat_id": CHAT_ID}, headers=_auth(fake_ctx))
    assert r.status == 409 and "temporarily unavailable" in (await r.json())["error"]
    assert _no_engine_calls(engines)


@pytest.mark.asyncio
async def test_queue_drain_on_disabled_grok_errors_in_the_chat_and_never_runs_on_claude(
    fake_ctx, engines, monkeypatch
):
    """The item was accepted while Grok was on and pinned to it; Grok is switched off before the
    drain. The registered engine itself refuses a run while GROK_ENABLED is off."""
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: False)
    fake_ctx["run_grok_engine"] = grok_engine.run_grok_engine     # the REAL engine, no fake
    _seed_chat(fake_ctx, provider="grok", grok_session_id="OLD-ID")
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                pinned_runtime={"provider": "grok", "model": "grok-4.7"}))
    assert not engines["claude"] and not engines["codex"]
    errors = [e for e in _live_events() if e.get("type") == "error"]
    assert errors and "GROK_ENABLED=false" in errors[0]["error"]
    assert _chat_record(fake_ctx)["grok_session_id"] == "OLD-ID"
    assert fake_ctx["sessions"][SESSION_KEY] == CLAUDE_FLAT


@pytest.mark.asyncio
async def test_card_on_disabled_grok_fails_with_the_engines_own_reason(
    fake_ctx, tmp_path, engines, monkeypatch
):
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: False)
    fake_ctx["run_grok_engine"] = grok_engine.run_grok_engine
    project = _card_project(tmp_path)
    _webapp._save_board(project["cwd"], "myproject", "# T", {
        "backlog": [], "in_progress": [{"id": "aabbcc", "text": "Build", "provider": "grok"}],
        "review": [], "failed": []})
    card = {"id": "aabbcc", "text": "Build", "provider": "grok", "description": None}
    fake_ctx["running"][SESSION_KEY] = True
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        await _webapp._run_card(fake_ctx, None, project, card, SESSION_KEY, run_mode="legacy")
    assert not engines["claude"] and not engines["codex"]
    _, _pre, cols = _webapp._load_board(project["cwd"])
    assert [c["id"] for c in cols["failed"]] == ["aabbcc"]
    assert "GROK_ENABLED=false" in (fake_ctx["DATA"] / "runs" / "aabbcc.md").read_text()


# ─────────────────── registry row, capability errors ──────────────────────────


@pytest.mark.asyncio
async def test_registry_grok_row_shape_when_enabled(aiohttp_client, fake_ctx, app, grok_on,
                                                    monkeypatch):
    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [])
    client = await aiohttp_client(app)
    body = await (await client.get("/api/agent-providers", headers=_auth(fake_ctx))).json()
    assert [p["provider"] for p in body["providers"]] == ["claude", "codex", "grok"]
    row = body["providers"][2]
    assert row["enabled"] is True and row["available"] is True and row["error"] is None
    assert [m["value"] for m in row["models"]] == ["grok-4.7", "grok-4.7-build-fast", "grok-4.6"]
    assert row["capabilities"]["plan_mode"] is False and row["capabilities"]["ask_mode"] is False
    assert row["capabilities"]["multi_agent"] is True and row["capabilities"]["interrupt"] is True
    # the extras provider_info returns ride along
    assert row["version"] == "1.0.46" and row["warnings"] == []
    assert row["sandbox"]["profile"] == "cardloop" and row["plan_type"] == "SuperGrok"
    assert row["accounts"] == [] and row["backends"] == []
    assert row["reasoning_levels"] == ["low", "medium", "high", "xhigh"]


@pytest.mark.asyncio
async def test_registry_grok_row_unavailable_carries_the_reason(
    aiohttp_client, fake_ctx, app, monkeypatch
):
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: True)
    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [])

    async def down():
        return _grok_info(available=False, error="bubblewrap not found")
    fake_ctx["grok_provider_info"] = down
    client = await aiohttp_client(app)
    row = (await (await client.get("/api/agent-providers", headers=_auth(fake_ctx))).json()
           )["providers"][2]
    assert row["available"] is False and row["error"] == "bubblewrap not found"


@pytest.mark.asyncio
async def test_registry_never_waits_on_a_slow_grok_probe_and_never_breaks_on_a_failing_one(
    aiohttp_client, fake_ctx, app, monkeypatch
):
    """Grok's own probe can include a real model turn. The registry that also serves Claude must
    answer within its cap and with HTTP 200 whatever Grok's probe does."""
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: True)
    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [])
    monkeypatch.setattr(_webapp, "_GROK_REGISTRY_WAIT_SEC", 0.05)

    async def slow():
        await asyncio.sleep(30)
    fake_ctx["grok_provider_info"] = slow
    client = await aiohttp_client(app)
    started = asyncio.get_event_loop().time()
    r = await client.get("/api/agent-providers", headers=_auth(fake_ctx))
    assert asyncio.get_event_loop().time() - started < 3
    assert r.status == 200
    row = (await r.json())["providers"][2]
    assert row["available"] is False and "still running" in row["error"]

    async def broken():
        raise RuntimeError("probe exploded")
    fake_ctx["grok_provider_info"] = broken
    r = await client.get("/api/agent-providers", headers=_auth(fake_ctx))
    assert r.status == 200
    body = await r.json()
    assert [p["provider"] for p in body["providers"]][:2] == ["claude", "codex"]
    assert "probe exploded" in body["providers"][2]["error"]


@pytest.mark.asyncio
async def test_registry_claude_and_codex_rows_are_unchanged_by_grok(
    aiohttp_client, fake_ctx, app, grok_on, monkeypatch
):
    monkeypatch.setattr(_webapp._accounts, "list_accounts", lambda: [])
    client = await aiohttp_client(app)
    on = (await (await client.get("/api/agent-providers", headers=_auth(fake_ctx))).json())
    monkeypatch.setattr(_webapp._grok, "grok_enabled", lambda: False)
    off = (await (await client.get("/api/agent-providers", headers=_auth(fake_ctx))).json())
    assert on["providers"][:2] == off["providers"] and on["default"] == off["default"] == "claude"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,conflict", [
    ("plan_mode", "grok does not support plan_mode"),
    ("ask_mode", "grok cannot honour ask_mode"),
])
async def test_chat_post_plan_and_ask_on_grok_are_visible_capability_errors(
    aiohttp_client, fake_ctx, app, engines, grok_on, flag, conflict
):
    _seed_chat(fake_ctx, provider="grok")
    client = await aiohttp_client(app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                          json={"prompt": "x", flag: True}, headers=_auth(fake_ctx))
    assert r.status == 409
    body = await r.json()
    assert any(conflict in c for c in body["conflicts"]) and body["provider"] == "grok"
    assert _no_engine_calls(engines), "refused, not silently downgraded and run"


def test_capability_conflicts_for_grok_match_the_spec():
    caps = providers.get("grok").capabilities()
    rc = runtime.RunContext(origin_kind="chat", origin_id="p", provider="grok", backend="",
                            model="grok-4.7", account="main", revision=0,
                            plan_mode=True, ask_mode=True, ultracode=True)
    conflicts = runtime.capability_conflicts(rc, caps)
    assert any("ask_mode" in c for c in conflicts) and any("plan_mode" in c for c in conflicts)
    assert not any("multi-agent" in c for c in conflicts), "multi_agent is supported"


def test_run_context_carries_grok_session_id_next_to_the_other_ids():
    info = {"claude": runtime.ProviderInfo(provider="claude", available=True, models=("opus",)),
            "grok": runtime.ProviderInfo(provider="grok", available=True, models=("grok-4.7",))}
    rc = runtime.resolve_runtime(
        origin_kind="chat", origin_id="p", providers=info, accounts_mod=_FakeAccounts(),
        chat={"provider": "grok", "model": "grok-4.7", "session_id": "S", "codex_thread_id": "T",
              "grok_session_id": "G"})
    assert (rc.provider, rc.session_id, rc.codex_thread_id, rc.grok_session_id) == (
        "grok", "S", "T", "G")


class _FakeAccounts:
    @staticmethod
    def resolve(_account):
        return "main"

    @staticmethod
    def validate(_account):
        return True, ""


@pytest.mark.asyncio
async def test_chat_response_always_carries_every_adapters_continuity_field(
    aiohttp_client, fake_ctx, app
):
    _seed_chat(fake_ctx, provider="claude", model="opus")
    client = await aiohttp_client(app)
    listing = await (await client.get(f"/api/projects/{PROJECT_ID}/chats",
                                      headers=_auth(fake_ctx))).json()
    chat = listing["chats"][0]
    assert chat["codex_thread_id"] is None and chat["grok_session_id"] is None


# ─────────────────── cross-provider continuity poisoning ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_each_providers_result_id_lands_only_in_its_own_field_at_every_run_site(
    aiohttp_client, fake_ctx, app, engines, grok_on, codex_on, provider
):
    """Every fake result carries the other providers' keys poisoned; a reader that is not
    provider-aware writes WRONG-ID somewhere. Drain and direct POST, in both orders."""
    field, _key, model = SHAPES[provider]
    others = [f for f in ("session_id", "codex_thread_id", "grok_session_id") if f != field]
    _seed_chat(fake_ctx, provider=provider, model=model, **{f: f"KEEP-{f}" for f in others},
               **{field: "OLD-ID"})
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello", "chat_id": CHAT_ID},
                                 headers=_auth(fake_ctx))
        assert resp.status == 200, await resp.text()
        await _sse_events(resp)
    rec = _chat_record(fake_ctx)
    assert rec[field] == "NEW-ID"
    assert all(rec[f] == f"KEEP-{f}" for f in others), rec

    _seed_chat(fake_ctx, provider=provider, model=model, **{f: f"KEEP-{f}" for f in others},
               **{field: "OLD-ID"})
    await _drain(fake_ctx, dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                                pinned_runtime={"provider": provider, "model": model}))
    rec = _chat_record(fake_ctx)
    assert rec[field] == "NEW-ID"
    assert all(rec[f] == f"KEEP-{f}" for f in others), rec
    for name, calls in engines.items():
        assert (len(calls) == 2) is (name == provider)
    kw = engines[provider][0]
    assert kw["resume_session_id" if provider != "codex" else "resume_thread_id"] == "OLD-ID"


@pytest.mark.asyncio
async def test_a_chat_flipped_back_to_grok_resumes_grok_not_the_id_another_engine_minted(
    aiohttp_client, fake_ctx, app, engines, grok_on
):
    _seed_chat(fake_ctx, provider="grok", session_id="CLAUDE-ID", codex_thread_id="CODEX-ID",
               grok_session_id="GROK-ID")
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat", json={"prompt": "x"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert engines["grok"][0]["resume_session_id"] == "GROK-ID"


# ─────────────────── the first POST to a never-listed free chat ───────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "grok"])
async def test_first_post_to_a_never_listed_free_adapter_chat_runs_on_that_adapter(
    aiohttp_client, fake_ctx, app, engines, grok_on, codex_on, tmp_path, provider
):
    """Pre-existing (P0 outcome): with no chats.json entry yet the turn resolved no chat record
    and ran on CLAUDE carrying the adapter's model id. The entry is now seeded from the free
    record first, exactly as listing the chat does."""
    fid = f"free-first-{provider}"
    field, _key, model = SHAPES[provider]
    _webapp._save_free_chats(fake_ctx, {fid: {
        "label": "f", "cwd": str(tmp_path), "model": model, "provider": provider,
        "session_id": None, "codex_thread_id": None,
        "grok_session_id": None, "created_at": 1}})
    assert fid not in _webapp._load_chats(fake_ctx)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{fid}/chat", json={"prompt": "hi"},
                                 headers=_auth(fake_ctx))
        assert resp.status == 200, await resp.text()
        await _sse_events(resp)
    assert not engines["claude"], "the adapter's model id must never reach Claude"
    assert len(engines[provider]) == 1 and engines[provider][0]["model"] == model
    rec = _webapp._load_free_chats(fake_ctx)[fid]
    assert rec[field] == "NEW-ID" and rec["session_id"] is None


@pytest.mark.asyncio
async def test_first_post_to_a_never_listed_real_project_seeds_nothing_before_it_decides(
    aiohttp_client, fake_ctx, app, engines
):
    """The seeding is for free chats only: a Claude project that was never listed keeps its
    exact old behaviour — here a busy POST queues and returns without touching chats.json."""
    fake_ctx["running"][SESSION_KEY] = True
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat", json={"prompt": "queue me"},
                                 headers=_auth(fake_ctx))
        events = await _sse_events(resp)
    assert any(e.get("type") == "queued" for e in events)
    assert _webapp._load_chats(fake_ctx) == {}


@pytest.mark.asyncio
async def test_first_post_to_a_never_listed_free_claude_chat_is_unchanged(
    aiohttp_client, fake_ctx, app, engines, tmp_path
):
    fid = "free-first-claude"
    _webapp._save_free_chats(fake_ctx, {fid: {
        "label": "f", "cwd": str(tmp_path), "model": "opus", "provider": "claude",
        "session_id": None, "codex_thread_id": None, "created_at": 1}})
    fake_ctx["sessions"][fid] = "FREE-FLAT"
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{fid}/chat", json={"prompt": "hi"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert len(engines["claude"]) == 1
    assert engines["claude"][0]["resume_session_id"] == "FREE-FLAT"
    assert fake_ctx["sessions"][fid] == "NEW-ID"


# ─────────────────── the Stop button ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_stop_button_reaches_a_running_grok_turn(aiohttp_client, fake_ctx, app):
    """The engine parks a `GrokTurn` in ctx["running"]; the generic Stop route calls its
    `interrupt()`, which asks the agent to cancel (session/cancel) and waits for the stop."""
    sent: list = []

    class FakeAcp:
        exited = False

        def __init__(self):
            self.prompt_done = asyncio.Event()

        async def notify(self, method, params):
            sent.append((method, params))
            self.prompt_done.set()

        def kill_group(self, _sig):
            raise AssertionError("a clean cancel must not need the kill")

    turn = grok_engine.GrokTurn(SESSION_KEY)
    turn._acp, turn.prompt_started, turn.session_id = FakeAcp(), True, "SESS-1"
    fake_ctx["running"][SESSION_KEY] = turn
    client = await aiohttp_client(app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/chat/stop", headers=_auth(fake_ctx))
    assert r.status == 200 and (await r.json()) == {"ok": True, "stopped": True}
    assert sent == [("session/cancel", {"sessionId": "SESS-1"})]
    assert turn.cancel_requested is True


# ─────────────────── bot.py ───────────────────────────────────────────────────


def test_bot_registers_the_grok_engine_and_info_in_the_ctx():
    import bot
    ctx = bot._build_ctx()
    assert ctx["run_grok_engine"] is grok_engine.run_grok_engine
    assert ctx["grok_provider_info"] is grok_engine.provider_info
    assert ctx["run_codex_engine"] is not None, "Codex wiring untouched"
    for spec in providers.specs():
        assert callable(spec.engine(ctx)), spec.name


@pytest.mark.asyncio
async def test_bot_schedules_no_grok_probe_when_grok_is_off(monkeypatch):
    import bot
    called = []

    async def info(**kw):
        called.append(kw)
        return {}
    monkeypatch.setattr(grok_engine, "grok_enabled", lambda: False)
    monkeypatch.setattr(grok_engine, "provider_info", info)
    assert bot._schedule_grok_startup_probe({}) is None
    await asyncio.sleep(0.01)
    assert called == []


@pytest.mark.asyncio
async def test_bot_startup_probe_journals_ready_and_unavailable_under_grok(monkeypatch, capsys):
    import bot
    monkeypatch.setattr(grok_engine, "grok_enabled", lambda: True)
    ctx = {}

    async def ready(*, force=False):
        assert force is True
        return _grok_info()
    monkeypatch.setattr(grok_engine, "provider_info", ready)
    await bot._grok_startup_probe(ctx)
    out = capsys.readouterr().out
    assert "[grok] ready via oidc auth (3 models, CLI 1.0.46)" in out
    assert ctx["grok_startup_info"]["available"] is True

    async def down(*, force=False):
        return _grok_info(available=False, error="no login")
    monkeypatch.setattr(grok_engine, "provider_info", down)
    await bot._grok_startup_probe(ctx)
    assert "[grok] unavailable; Claude remains active: no login" in capsys.readouterr().out

    async def boom(*, force=False):
        raise RuntimeError("probe broke")
    monkeypatch.setattr(grok_engine, "provider_info", boom)
    await bot._grok_startup_probe(ctx)             # must not raise
    assert "[grok] startup probe failed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_bot_amain_starts_the_grok_probe_only_after_the_cockpit_is_listening(monkeypatch):
    """The probe may spend a real model turn: it must not run before webapp.start() returns,
    must not delay boot, and must be cancelled (not leaked) at shutdown."""
    import bot
    import signal as _signal

    order: list = []
    release = asyncio.Event()
    probe_state = {"cancelled": False}
    handlers: dict = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler",
                        lambda sig, cb, *a: handlers.__setitem__(sig, cb), raising=False)
    monkeypatch.setattr(grok_engine, "grok_enabled", lambda: True)

    async def probe(*, force=False):
        order.append("probe-start")
        try:
            await release.wait()
        except asyncio.CancelledError:
            probe_state["cancelled"] = True
            raise
        return _grok_info()

    async def fake_start(ctx):
        order.append("start-begin")
        await asyncio.sleep(0.05)
        order.append("start-done")

    async def fake_tunnel(_enabled):
        order.append("after-start")
        await asyncio.sleep(0.02)
        order.append("probe-still-pending")
        handlers[_signal.SIGTERM]()

    async def noop(*_a, **_k):
        return None

    async def graceful(*_a, **_k):
        # the explicit cancel must already have happened: a model turn must not keep running
        # through the unbounded session flush
        await asyncio.sleep(0)
        probe_state["cancelled_before_flush"] = probe_state["cancelled"]

    monkeypatch.setattr(grok_engine, "provider_info", probe)
    monkeypatch.setattr(bot, "_run_startup_migration", lambda: None)
    monkeypatch.setattr(bot, "_build_ctx", lambda: {"topics": {}})
    monkeypatch.setattr(bot.webapp, "start", fake_start)
    monkeypatch.setattr(bot.webapp, "stop", noop)
    monkeypatch.setattr(bot, "_maybe_start_tunnel", fake_tunnel)
    monkeypatch.setattr(bot.tunnel, "stop_tunnel", noop)
    monkeypatch.setattr(bot, "_graceful_shutdown", graceful)
    monkeypatch.setattr(bot.codex_engine, "codex_enabled", lambda: False)

    await asyncio.wait_for(bot._amain(), timeout=10)
    assert order.index("start-done") < order.index("probe-start"), order
    assert order.index("after-start") < order.index("probe-still-pending"), order
    assert probe_state["cancelled"] is True, "shutdown cancels the in-flight probe"
    assert probe_state["cancelled_before_flush"] is True, "...before the session flush, not after"


# ─────────────────── no privacy gate: choosing Grok is the consent ─────────────────


@pytest.mark.asyncio
async def test_every_selection_site_accepts_grok_without_any_project_flag(aiohttp_client, fake_ctx, app, grok_on):
    """The former per-project gate is gone (2026-10-03): a project record with no Grok field at all takes a
    Grok chat, a Grok free chat rooted at $HOME, a Grok card and a Grok board default."""
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)
    r = await client.post(f"/api/projects/{PROJECT_ID}/chats", json={"provider": "grok"}, headers=h)
    assert r.status == 201
    r = await client.post("/api/free", json={"provider": "grok"}, headers=h)          # cwd defaults to $HOME
    assert r.status in (200, 201), await r.text()
    r = await client.post(f"/api/projects/{PROJECT_ID}/tasks", json={"text": "x", "provider": "grok"}, headers=h)
    assert r.status in (200, 201), await r.text()
    r = await client.post(f"/api/projects/{PROJECT_ID}/settings", json={"board_provider": "grok"}, headers=h)
    assert r.status == 200, await r.text()


@pytest.mark.asyncio
async def test_the_settings_view_no_longer_carries_grok_allowed_and_a_stale_post_of_it_is_an_unknown_key(
    aiohttp_client, fake_ctx, app, grok_on
):
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)
    body = await (await client.get(f"/api/projects/{PROJECT_ID}/settings", headers=h)).json()
    assert "grok_allowed" not in body and "grok_allowed" not in body.get("settings", body)
    r = await client.post(f"/api/projects/{PROJECT_ID}/settings", json={"grok_allowed": True}, headers=h)
    assert r.status == 400 and "grok_allowed" in (await r.json())["error"]

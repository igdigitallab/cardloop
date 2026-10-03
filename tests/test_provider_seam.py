"""
spec-095 P0: characterization of every place the cockpit branches on the provider.

These tests were written against the code BEFORE the provider seam (`providers.py`) existed and
must pass on it unchanged; they then keep passing across the refactor. They pin what a caller
can observe, not how it is computed:

  * the exact keyword arguments each of the three run sites hands the engine factory
    (queue drain, direct chat POST, board card) — for Claude AND Codex;
  * which engine factory in ctx answers, and that the other one is never touched;
  * what is written back to the chat record / flat session mirror after the engine answers;
  * the provider normalisation, model defaults and validation messages at the non-run sites.

A Grok-shaped third provider is deliberately NOT here: P0 is "zero behaviour change".
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import board as _board
import webapp as _webapp
from webapp import _derive_token

SESSION_KEY = "1001:42"
PROJECT_ID = "myproject"
CHAT_ID = "aaaaaa"

# Exact kwarg sets. `agents_kwargs` is patched to {} in every test so these are complete.
CLAUDE_CHAT_KEYS = {
    "project_name", "cwd", "prompt", "session_key", "model", "resume_session_id", "env",
    "project_account", "backend", "ctx", "ephemeral", "effort", "ultracode", "plan_mode",
    "ask_mode", "chat_id",
}
CODEX_CHAT_KEYS = {
    "project_name", "cwd", "prompt", "session_key", "model", "resume_thread_id", "ctx",
    "ephemeral", "effort", "multi_agent", "plan_mode", "chat_id", "entrypoint",
}
CLAUDE_CARD_KEYS = {
    "project_name", "cwd", "prompt", "session_key", "model", "resume_session_id", "env",
    "project_account", "backend", "ctx", "ephemeral", "output_format", "entrypoint",
}
CODEX_CARD_KEYS = {
    "project_name", "cwd", "prompt", "session_key", "model", "resume_thread_id", "ctx",
    "ephemeral", "effort", "plan_mode", "multi_agent", "entrypoint",
}


# ─────────────────────────── fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_shared_state(tmp_path):
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


async def _unused_engine(**kwargs):
    raise AssertionError(f"this engine must not have run: {kwargs}")
    yield  # pragma: no cover


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    proj = tmp_path / PROJECT_ID
    proj.mkdir()
    ctx = {
        "topics": {SESSION_KEY: {"project": PROJECT_ID, "cwd": str(proj), "model": "sonnet"}},
        "sessions": {},
        "running": {},
        "password": "testpass",
        "DATA": data,
        "HERE": ROOT,
        "VAULT_PROJECTS": None,
        "DEFAULT_MODEL": "sonnet",
        "MODELS": {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"},
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": _unused_engine,
        "run_codex_engine": _unused_engine,
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
    a.router.add_post("/api/projects/{id}/chats", _webapp.api_project_chats_create)
    a.router.add_post("/api/free", _webapp.api_free_create)
    a.router.add_post("/api/projects/{id}/tasks", _webapp.api_create_task)
    a.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", _webapp.api_update_task)
    a.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    a.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    return a


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


def _seed_chat(ctx, *, provider, model, field, value, **extra):
    chat = {"id": CHAT_ID, "name": "Main", "provider": provider, "model": model, **extra}
    if field and value is not None:
        chat[field] = value
    _webapp._save_chats(ctx, {PROJECT_ID: {"active": CHAT_ID, "chats": [chat]}})


def _chat_record(ctx) -> dict:
    return _webapp._load_chats(ctx)[PROJECT_ID]["chats"][0]


# Per-provider shape of one chat turn: how the chat stores its resume id, which ctx key holds
# the engine, which kwarg takes the id, and which `result` event key brings the new id back.
CASES = {
    "claude": dict(model="opus", field="session_id", engine_key="run_engine",
                   other_key="run_codex_engine", resume_kwarg="resume_session_id",
                   result_key="session_id", keys=CLAUDE_CHAT_KEYS, other_field="codex_thread_id"),
    "codex": dict(model="gpt-5.6-sol", field="codex_thread_id", engine_key="run_codex_engine",
                  other_key="run_engine", resume_kwarg="resume_thread_id",
                  result_key="thread_id", keys=CODEX_CHAT_KEYS, other_field="session_id"),
}


def _install_engines(ctx, case, calls):
    async def fake(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "answer"}
        yield {"type": "result", case["result_key"]: "NEW-ID", "context_tokens": 7}

    ctx[case["engine_key"]] = fake
    ctx[case["other_key"]] = _unused_engine


# ─────────────────────────── seam 1: queue drain ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_queue_drain_kwargs_and_writeback(fake_ctx, codex_on, provider):
    case = CASES[provider]
    _seed_chat(fake_ctx, provider=provider, model=case["model"], field=case["field"],
               value="OLD-ID",
               runtime_handoff={"text": "handoff-text", "for_provider": provider,
                                "for_backend": "", "from_label": "A", "to_label": "B"})
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert pinned == {"provider": provider, "model": case["model"]}
    item = _webapp._chat_queue_enqueue(SESSION_KEY, "do the thing", chat_id=CHAT_ID,
                                       project_id=PROJECT_ID, pinned_runtime=pinned)
    assert item is not None
    calls: list = []
    _install_engines(fake_ctx, case, calls)

    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is True
        await asyncio.sleep(0.05)

    assert len(calls) == 1, "exactly one engine call, on the pinned provider"
    kw = calls[0]
    assert set(kw) == case["keys"], (set(kw) ^ case["keys"])
    assert kw["ctx"] is fake_ctx
    assert kw["model"] == case["model"]
    assert kw[case["resume_kwarg"]] == "OLD-ID"
    assert kw["ephemeral"] is False
    assert kw["chat_id"] == CHAT_ID
    assert kw["project_name"] == "myproject" and kw["session_key"] == SESSION_KEY
    assert kw["plan_mode"] is False
    assert kw["prompt"].startswith("handoff-text\n\n") and kw["prompt"].endswith("do the thing")
    if provider == "codex":
        assert kw["multi_agent"] is False and kw["entrypoint"] == "chat"
    else:
        assert kw["ultracode"] is False and kw["ask_mode"] is False
        assert kw["backend"] == "" and isinstance(kw["env"], dict)

    rec = _chat_record(fake_ctx)
    assert rec[case["field"]] == "NEW-ID", "new id lands in this provider's continuity field"
    assert rec.get(case["other_field"]) is None, "the other provider's id is untouched"
    assert "runtime_handoff" not in rec, "answered turn clears the armed handoff"
    if provider == "claude":
        assert fake_ctx["sessions"][SESSION_KEY] == "NEW-ID", "claude mirrors to the flat map"
    else:
        assert SESSION_KEY not in fake_ctx["sessions"], "codex never touches the claude mirror"


@pytest.mark.asyncio
async def test_queue_drain_legacy_item_reads_provider_from_chat_record(fake_ctx, codex_on):
    """No pinned runtime → the documented fallback re-reads provider + continuity from the chat."""
    case = CASES["codex"]
    _seed_chat(fake_ctx, provider="codex", model=case["model"], field="codex_thread_id",
               value="OLD-ID")
    item = _webapp._chat_queue_enqueue(SESSION_KEY, "legacy", chat_id=CHAT_ID,
                                       project_id=PROJECT_ID)
    assert item is not None and "runtime" not in item
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is True
        await asyncio.sleep(0.05)
    assert calls and calls[0]["resume_thread_id"] == "OLD-ID"
    assert _chat_record(fake_ctx)["codex_thread_id"] == "NEW-ID"


# ─────────────────────────── seam 2: direct chat POST ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_chat_post_kwargs_writeback_and_result_frame(
    aiohttp_client, fake_ctx, app, codex_on, provider
):
    case = CASES[provider]
    _seed_chat(fake_ctx, provider=provider, model=case["model"], field=case["field"],
               value="OLD-ID",
               runtime_handoff={"text": "handoff-text", "for_provider": provider,
                                "for_backend": "", "from_label": "A", "to_label": "B"})
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello", "chat_id": CHAT_ID},
                                 headers=_auth(fake_ctx))
        assert resp.status == 200, await resp.text()
        events = await _sse_events(resp)

    assert len(calls) == 1
    kw = calls[0]
    assert set(kw) == case["keys"], (set(kw) ^ case["keys"])
    assert kw["ctx"] is fake_ctx
    assert kw["model"] == case["model"]
    assert kw[case["resume_kwarg"]] == "OLD-ID"
    assert kw["ephemeral"] is False and kw["chat_id"] == CHAT_ID
    assert kw["plan_mode"] is False
    assert kw["prompt"].endswith("hello") and "handoff-text" in kw["prompt"]
    if provider == "codex":
        assert kw["multi_agent"] is False and kw["entrypoint"] == "chat"
    else:
        assert kw["ultracode"] is False and kw["ask_mode"] is False and kw["backend"] == ""

    rec = _chat_record(fake_ctx)
    assert rec[case["field"]] == "NEW-ID"
    assert rec.get(case["other_field"]) is None
    assert "runtime_handoff" not in rec
    if provider == "claude":
        assert fake_ctx["sessions"][SESSION_KEY] == "NEW-ID"
    else:
        assert SESSION_KEY not in fake_ctx["sessions"]

    result = next(e for e in events if e.get("type") == "result")
    assert result["provider"] == provider
    # Public SSE contract: both fields are ALWAYS present; the one that does not apply is null.
    assert result["session_id"] == ("NEW-ID" if provider == "claude" else None)
    assert result["codex_thread_id"] == ("NEW-ID" if provider == "codex" else None)


@pytest.mark.asyncio
async def test_chat_post_codex_effort_and_ultracode_reach_engine(
    aiohttp_client, fake_ctx, app, codex_on
):
    """Per-turn flags travel to the light engine under their own names (ultracode→multi_agent)."""
    case = CASES["codex"]
    _seed_chat(fake_ctx, provider="codex", model=case["model"], field=None, value=None)
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "x", "think_mode": "high", "plan_mode": True,
                                       "ultracode": True},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls, await resp.text()
    kw = calls[0]
    assert kw["effort"] == "high" and kw["plan_mode"] is True and kw["multi_agent"] is True
    assert kw["resume_thread_id"] is None


# ─────────────────────────── seam 3: board card run ───────────────────────────


async def _run_card_with(fake_ctx, tmp_path, *, project_extra=None, card_extra=None,
                         provider_key="run_engine"):
    cwd = tmp_path / "cardproj"
    cwd.mkdir(exist_ok=True)
    project = {"name": "myproject", "cwd": str(cwd), "session_key": SESSION_KEY,
               "model": "sonnet", **(project_extra or {})}
    card = {"id": "aabbcc", "text": "Build feature", "description": None, **(card_extra or {})}
    calls = {"claude": [], "codex": []}

    async def claude_engine(**kwargs):
        calls["claude"].append(kwargs)
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "session_id": "card-sid"}

    async def codex_engine(**kwargs):
        calls["codex"].append(kwargs)
        yield {"type": "text", "text": "done"}
        yield {"type": "result", "thread_id": "card-thread"}

    fake_ctx["run_engine"] = claude_engine
    fake_ctx["run_codex_engine"] = codex_engine
    fake_ctx["sessions"][SESSION_KEY] = "shared-chat-session"
    fake_ctx["running"][SESSION_KEY] = True
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        await _webapp._run_card(fake_ctx, None, project, card, SESSION_KEY, run_mode="legacy")
    return calls


@pytest.mark.asyncio
async def test_card_default_runs_on_claude_with_exact_kwargs(fake_ctx, tmp_path):
    calls = await _run_card_with(fake_ctx, tmp_path)
    assert not calls["codex"] and len(calls["claude"]) == 1
    kw = calls["claude"][0]
    assert set(kw) == CLAUDE_CARD_KEYS, (set(kw) ^ CLAUDE_CARD_KEYS)
    assert kw["ephemeral"] is True and kw["entrypoint"] == "card"
    assert kw["resume_session_id"] is None
    assert kw["model"] == _webapp._effective_card_model({"id": "aabbcc"})
    assert kw["env"]["CARDLOOP_RUN_MODE"] == "card"
    assert kw["ctx"] is fake_ctx and kw["backend"] == ""
    assert fake_ctx["sessions"][SESSION_KEY] == "shared-chat-session", "cards never write sessions"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["card_override", "project_board_provider"])
async def test_card_codex_runs_on_codex_with_exact_kwargs(fake_ctx, tmp_path, how):
    if how == "card_override":
        extra = dict(card_extra={"provider": "codex", "model": "gpt-card"})
        expect_model = "gpt-card"
    else:
        extra = dict(project_extra={"board_provider": "codex", "codex_model": "gpt-proj"})
        expect_model = "gpt-proj"
    calls = await _run_card_with(fake_ctx, tmp_path, **extra)
    assert not calls["claude"] and len(calls["codex"]) == 1
    kw = calls["codex"][0]
    assert set(kw) == CODEX_CARD_KEYS, (set(kw) ^ CODEX_CARD_KEYS)
    assert kw["model"] == expect_model
    assert kw["resume_thread_id"] is None and kw["ephemeral"] is True
    assert kw["entrypoint"] == "card"
    assert kw["effort"] is None and kw["plan_mode"] is False and kw["multi_agent"] is False
    assert fake_ctx["sessions"][SESSION_KEY] == "shared-chat-session"


@pytest.mark.asyncio
async def test_card_codex_model_falls_back_to_the_codex_default(fake_ctx, tmp_path):
    calls = await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "codex"})
    assert calls["codex"][0]["model"] == _webapp._codex.DEFAULT_CODEX_MODEL


@pytest.mark.asyncio
async def test_card_override_claude_beats_project_board_provider_codex(fake_ctx, tmp_path):
    calls = await _run_card_with(fake_ctx, tmp_path, card_extra={"provider": "claude"},
                                 project_extra={"board_provider": "codex"})
    assert calls["claude"] and not calls["codex"]


# ─────────────────────────── pure helpers ─────────────────────────────────────


@pytest.mark.parametrize("card_provider,board_provider,expected", [
    (None, None, "claude"),
    (None, "claude", "claude"),
    (None, "codex", "codex"),
    ("codex", None, "codex"),
    ("codex", "claude", "codex"),
    ("claude", "codex", "claude"),
    ("vertex", "codex", "codex"),     # unknown card value is ignored, project default wins
    ("vertex", None, "claude"),
    (None, "vertex", "claude"),       # unknown project value never selects an engine
])
def test_effective_card_provider_matrix(card_provider, board_provider, expected):
    card = {"provider": card_provider} if card_provider else {}
    project = {"board_provider": board_provider} if board_provider else {}
    assert _webapp._effective_card_provider(card, project) == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_known_agent_providers_map(monkeypatch, enabled):
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: enabled)
    assert _webapp._known_agent_providers() == {"claude": True, "codex": enabled}


def test_free_chat_continuity_writes_the_providers_own_field(fake_ctx):
    _webapp._save_free_chats(fake_ctx, {"free-1": {"provider": "claude", "session_id": None,
                                                   "codex_thread_id": None}})
    _webapp._save_free_chat_continuity(fake_ctx, "free-1", provider="claude", continuity_id="S1")
    rec = _webapp._load_free_chats(fake_ctx)["free-1"]
    assert rec["session_id"] == "S1" and rec["codex_thread_id"] is None
    _webapp._save_free_chat_continuity(fake_ctx, "free-1", provider="codex", continuity_id="T1")
    rec = _webapp._load_free_chats(fake_ctx)["free-1"]
    assert rec["session_id"] == "S1" and rec["codex_thread_id"] == "T1"


def test_free_chat_continuity_ignores_real_projects_and_missing_records(fake_ctx):
    _webapp._save_free_chats(fake_ctx, {})
    _webapp._save_free_chat_continuity(fake_ctx, "myproject", provider="claude", continuity_id="S")
    _webapp._save_free_chat_continuity(fake_ctx, None, provider="claude", continuity_id="S")
    _webapp._save_free_chat_continuity(fake_ctx, "free-gone", provider="claude", continuity_id="S")
    assert _webapp._load_free_chats(fake_ctx) == {}


def test_pin_chat_runtime_model_defaults_per_provider(fake_ctx, codex_on):
    _webapp._save_chats(fake_ctx, {PROJECT_ID: {"active": CHAT_ID, "chats": [
        {"id": CHAT_ID, "name": "C", "provider": "claude"},
        {"id": "bbbbbb", "name": "X", "provider": "codex"},
    ]}})
    proj = {"id": PROJECT_ID, "model": "haiku", "codex_model": "gpt-proj"}
    assert _webapp._pin_chat_runtime(fake_ctx, proj, CHAT_ID) == {"provider": "claude",
                                                                  "model": "haiku"}
    assert _webapp._pin_chat_runtime(fake_ctx, proj, "bbbbbb") == {"provider": "codex",
                                                                   "model": "gpt-proj"}
    bare = {"id": PROJECT_ID}
    assert _webapp._pin_chat_runtime(fake_ctx, bare, CHAT_ID)["model"] == "sonnet"
    assert (_webapp._pin_chat_runtime(fake_ctx, bare, "bbbbbb")["model"]
            == _webapp._codex.DEFAULT_CODEX_MODEL)


def test_project_settings_view_normalises_board_provider_and_codex_model():
    v = _webapp._project_settings_view({"board_provider": "codex", "codex_model": "gpt-x"})
    assert v["board_provider"] == "codex" and v["codex_model"] == "gpt-x"
    v = _webapp._project_settings_view({"board_provider": "vertex"})
    assert v["board_provider"] == "claude"
    assert v["codex_model"] == _webapp._codex.DEFAULT_CODEX_MODEL


def test_collect_projects_exposes_board_provider_and_codex_model(fake_ctx):
    fake_ctx["topics"][SESSION_KEY]["board_provider"] = "codex"
    fake_ctx["topics"][SESSION_KEY]["codex_model"] = "gpt-y"
    proj = next(p for p in _webapp._collect_projects(fake_ctx) if p["id"] == PROJECT_ID)
    assert proj["board_provider"] == "codex" and proj["codex_model"] == "gpt-y"
    fake_ctx["topics"][SESSION_KEY]["board_provider"] = "vertex"
    fake_ctx["topics"][SESSION_KEY].pop("codex_model")
    proj = next(p for p in _webapp._collect_projects(fake_ctx) if p["id"] == PROJECT_ID)
    assert proj["board_provider"] == "claude"
    assert proj["codex_model"] == _webapp._codex.DEFAULT_CODEX_MODEL


@pytest.mark.parametrize("meta,expected", [
    ("provider=codex", {"provider": "codex"}),
    ("provider=claude", {"provider": "claude"}),
    ("provider=vertex", {}),
    ("provider=codex model=gpt-9.9", {"provider": "codex", "model": "gpt-9.9"}),
    ("provider=claude model=gpt-9.9", {"provider": "claude"}),   # alias list only for claude
    ("model=gpt-9.9", {}),
    ("provider=claude model=opus", {"provider": "claude", "model": "opus"}),
])
def test_board_marker_provider_parsing(meta, expected):
    assert _board._parse_marker_meta(meta) == expected


def test_board_marker_provider_round_trip_and_unknown_dropped():
    cols = {key: [] for key, _label, _status in _board.BOARD_COLUMNS}
    first = _board.BOARD_COLUMNS[0][0]
    cols[first] = [
        {"id": "aaaaaa", "text": "one", "provider": "codex", "model": "gpt-9.9"},
        {"id": "bbbbbb", "text": "two", "provider": "claude"},
        {"id": "cccccc", "text": "three", "provider": "vertex"},
    ]
    text = _board._serialize_tasks("# T", cols, "p")
    assert "<!--ops:aaaaaa provider=codex model=gpt-9.9-->" in text
    assert "<!--ops:bbbbbb provider=claude-->" in text
    assert "provider=vertex" not in text
    _pre, parsed = _board._parse_tasks(text)
    got = {c["id"]: c.get("provider") for c in parsed[first]}
    assert got == {"aaaaaa": "codex", "bbbbbb": "claude", "cccccc": None}


# ─────────────────────────── non-run HTTP surfaces ────────────────────────────


@pytest.mark.asyncio
async def test_chat_create_provider_normalisation_and_model_rules(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    url = f"/api/projects/{PROJECT_ID}/chats"
    h = _auth(fake_ctx)

    r = await client.post(url, json={"provider": "codex"}, headers=h)
    assert r.status == 201
    body = await r.json()
    assert body["provider"] == "codex" and body["model"] == _webapp._codex.DEFAULT_CODEX_MODEL
    assert body["codex_thread_id"] is None and body["session_id"] is None

    r = await client.post(url, json={"provider": "codex", "model": "bad model!"}, headers=h)
    assert r.status == 400 and (await r.json())["error"] == "invalid Codex model"

    r = await client.post(url, json={"provider": "claude", "model": "gpt-5"}, headers=h)
    assert r.status == 400 and (await r.json())["error"] == "invalid Claude model"

    r = await client.post(url, json={"provider": "vertex"}, headers=h)
    assert r.status == 201
    body = await r.json()
    assert body["provider"] == "claude", "an unknown provider on create falls back to claude"
    assert body["model"] is None


@pytest.mark.asyncio
async def test_free_chat_create_provider_normalisation_and_model_rules(
    aiohttp_client, fake_ctx, app
):
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)

    r = await client.post("/api/free", json={"provider": "codex"}, headers=h)
    body = await r.json()
    assert body["provider"] == "codex" and body["model"] == _webapp._codex.DEFAULT_CODEX_MODEL
    assert body["session_id"] is None and body["codex_thread_id"] is None

    r = await client.post("/api/free", json={"provider": "codex", "model": "bad model!"},
                          headers=h)
    assert r.status == 400 and (await r.json())["error"] == "invalid Codex model"

    r = await client.post("/api/free", json={"provider": "vertex", "model": "not-a-model"},
                          headers=h)
    body = await r.json()
    assert body["provider"] == "claude"
    assert body["model"] == _webapp._effective_default_model(fake_ctx)


@pytest.mark.asyncio
async def test_card_create_and_update_provider_validation(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)
    r = await client.post(f"/api/projects/{PROJECT_ID}/tasks",
                          json={"text": "t", "provider": "vertex"}, headers=h)
    assert r.status == 400
    assert (await r.json())["error"] == "provider: must be claude or codex"

    r = await client.post(f"/api/projects/{PROJECT_ID}/tasks",
                          json={"text": "a codex card", "provider": "codex"}, headers=h)
    assert r.status in (200, 201), await r.text()

    r = await client.patch(f"/api/projects/{PROJECT_ID}/tasks/aaaaaa",
                           json={"text": "t", "provider": "vertex"}, headers=h)
    assert r.status == 400
    assert (await r.json())["error"] == "provider: must be claude, codex, or empty"


@pytest.mark.asyncio
async def test_project_settings_codex_model_validation(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)
    r = await client.post(f"/api/projects/{PROJECT_ID}/settings",
                          json={"codex_model": "bad model!"}, headers=h)
    assert r.status == 400 and (await r.json())["error"] == "codex_model: invalid model id"
    r = await client.post(f"/api/projects/{PROJECT_ID}/settings",
                          json={"codex_model": "gpt-z", "board_provider": "codex"}, headers=h)
    assert r.status == 200, await r.text()
    r = await client.get(f"/api/projects/{PROJECT_ID}/settings", headers=h)
    view = await r.json()
    assert view["codex_model"] == "gpt-z" and view["board_provider"] == "codex"


# ─────────────────────────── gaps found by the mutation pass ──────────────────


def test_collect_projects_labels_free_chats_with_their_provider(fake_ctx):
    _webapp._save_free_chats(fake_ctx, {
        "free-c0dex": {"label": "cx", "cwd": "/tmp", "model": "m", "provider": "codex",
                       "created_at": 1},
        "free-claud": {"label": "cl", "cwd": "/tmp", "model": "m", "provider": "claude",
                       "created_at": 2},
        "free-vtx": {"label": "vx", "cwd": "/tmp", "model": "m", "provider": "vertex",
                     "created_at": 3},
    })
    by_id = {p["id"]: p for p in _webapp._collect_projects(fake_ctx)}
    assert by_id["free-c0dex"]["provider"] == "codex"
    assert by_id["free-claud"]["provider"] == "claude"
    assert by_id["free-vtx"]["provider"] == "claude"
    assert by_id["free-c0dex"]["codex_model"] == _webapp._codex.DEFAULT_CODEX_MODEL


@pytest.mark.asyncio
async def test_card_provider_can_be_set_changed_and_cleared(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)
    r = await client.post(f"/api/projects/{PROJECT_ID}/tasks",
                          json={"text": "card for provider edits"}, headers=h)
    assert r.status in (200, 201), await r.text()
    _, _pre, cols = _webapp._load_board(fake_ctx["topics"][SESSION_KEY]["cwd"])
    card_id = next(c["id"] for col in cols.values() for c in col
                   if c["text"] == "card for provider edits")
    cwd = fake_ctx["topics"][SESSION_KEY]["cwd"]

    def provider_on_board():
        _, _pre, cols = _webapp._load_board(cwd)
        return next(c.get("provider") for col in cols.values() for c in col if c["id"] == card_id)

    for sent, expected in (("codex", "codex"), ("claude", "claude"), ("", None)):
        r = await client.patch(f"/api/projects/{PROJECT_ID}/tasks/{card_id}",
                               json={"text": "card for provider edits", "provider": sent},
                               headers=h)
        assert r.status == 200, await r.text()
        assert provider_on_board() == expected, sent


@pytest.mark.asyncio
async def test_chat_post_codex_without_chat_model_uses_the_project_codex_model(
    aiohttp_client, fake_ctx, app, codex_on
):
    case = CASES["codex"]
    fake_ctx["topics"][SESSION_KEY]["codex_model"] = "gpt-from-project"
    _seed_chat(fake_ctx, provider="codex", model=None, field=None, value=None)
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat", json={"prompt": "x"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls and calls[0]["model"] == "gpt-from-project"


@pytest.mark.asyncio
async def test_chat_post_claude_unknown_chat_id_resumes_the_flat_session_map(
    aiohttp_client, fake_ctx, app
):
    """Legacy path: a request naming a chat the entry does not contain still resumes whatever
    the flat per-project session map holds — for Claude only."""
    case = CASES["claude"]
    _seed_chat(fake_ctx, provider="claude", model="opus", field=None, value=None)
    fake_ctx["sessions"][SESSION_KEY] = "FLAT-SID"
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "x", "chat_id": "bbbbbb"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls, await resp.text()
    assert calls[0]["resume_session_id"] == "FLAT-SID"


@pytest.mark.parametrize("provider,expect_provider,sid,thread", [
    ("codex", "codex", None, "T-FREE"),
    ("claude", "claude", "S-FLAT", None),
    ("vertex", "claude", "S-FLAT", None),   # an unknown free-chat provider seeds as claude
])
def test_ensure_chat_entry_seeds_main_chat_from_a_free_record(
    fake_ctx, provider, expect_provider, sid, thread
):
    _webapp._save_free_chats(fake_ctx, {"free-seed": {
        "label": "f", "cwd": "/tmp", "model": "m-free", "provider": provider,
        "session_id": None, "codex_thread_id": "T-FREE", "created_at": 1,
    }})
    fake_ctx["sessions"]["free-seed"] = "S-FLAT"
    data = _webapp._ensure_chat_entry(fake_ctx, "free-seed", "free-seed")
    chat = data["free-seed"]["chats"][0]
    assert chat["provider"] == expect_provider
    assert chat["model"] == "m-free"
    assert chat["session_id"] == sid
    assert chat["codex_thread_id"] == thread


@pytest.mark.asyncio
async def test_project_settings_board_provider_validation_and_storage(
    aiohttp_client, fake_ctx, app
):
    client = await aiohttp_client(app)
    h = _auth(fake_ctx)
    url = f"/api/projects/{PROJECT_ID}/settings"
    topic = fake_ctx["topics"][SESSION_KEY]

    r = await client.post(url, json={"board_provider": "vertex"}, headers=h)
    assert r.status == 400
    assert (await r.json())["error"] == "board_provider: must be claude or codex"

    r = await client.post(url, json={"board_provider": " CODEX "}, headers=h)
    assert r.status == 200, await r.text()
    assert topic["board_provider"] == "codex"

    r = await client.post(url, json={"board_provider": "claude"}, headers=h)
    assert r.status == 200, await r.text()
    assert topic.get("board_provider") is None, "claude is stored as 'unset', not as a value"
    view = await (await client.get(url, headers=h)).json()
    assert view["board_provider"] == "claude"


def _entry(active, *chats):
    return {"active": active, "chats": [{"id": cid, "provider": prov} for cid, prov in chats]}


@pytest.mark.parametrize("codex_enabled,active,chats,expected", [
    (True, "x1", [("c1", "claude"), ("x1", "codex")], "x1"),
    (True, "c1", [("c1", "claude"), ("x1", "codex")], "c1"),
    (False, "c1", [("c1", "claude"), ("x1", "codex")], "c1"),
    (False, "c1", [("c1", "claude"), ("c2", "claude")], "c1"),   # a visible active chat stays
    (False, "x1", [("c1", "claude"), ("x1", "codex"), ("c2", "claude")], "c2"),
    (False, "x1", [("x1", "codex"), ("x2", "codex")], "x1"),
    (False, "x1", [("c1", "claude"), ("x1", "codex")], "c1"),
])
def test_effective_active_chat_hides_a_disabled_provider(
    monkeypatch, codex_enabled, active, chats, expected
):
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: codex_enabled)
    assert _webapp._effective_active_chat(_entry(active, *chats)) == expected


# ─────────────────────────── gaps found by the post-seam mutation pass ────────


async def _drain_once(fake_ctx, case, *, item_kwargs, pending=None):
    if pending is not None:
        fake_ctx["pending_handoff"] = {SESSION_KEY: pending}
    item = _webapp._chat_queue_enqueue(SESSION_KEY, "queued text", **item_kwargs)
    assert item is not None
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is True
        await asyncio.sleep(0.05)
    assert len(calls) == 1
    return calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,has_id,injected", [
    ("claude", True, False),    # resumed session: the rotation summary must not be re-injected
    ("claude", False, True),    # fresh session after a rotation: it must be
    # Known pre-seam quirk, pinned on purpose: the drain path only ever counted Claude's id, so a
    # RESUMED adapter thread still receives a pending rotation summary. The direct POST path was
    # fixed for this; changing the drain is a behaviour change and belongs to its own commit.
    ("codex", True, True),
])
async def test_queue_drain_pending_rotation_handoff_injection(
    fake_ctx, codex_on, provider, has_id, injected
):
    case = CASES[provider]
    _seed_chat(fake_ctx, provider=provider, model=case["model"], field=case["field"],
               value="OLD-ID" if has_id else None)
    kw = await _drain_once(
        fake_ctx, case, pending="ROTATION-SUMMARY",
        item_kwargs=dict(chat_id=CHAT_ID, project_id=PROJECT_ID,
                         pinned_runtime={"provider": provider, "model": case["model"]}),
    )
    assert ("ROTATION-SUMMARY" in kw["prompt"]) is injected
    assert ("ROTATION-SUMMARY" in str(fake_ctx["pending_handoff"].get(SESSION_KEY))) is (not injected)


@pytest.mark.asyncio
async def test_queue_drain_legacy_item_without_project_uses_and_updates_the_flat_map(fake_ctx):
    case = CASES["claude"]
    fake_ctx["sessions"][SESSION_KEY] = "FLAT-OLD"
    kw = await _drain_once(fake_ctx, case, item_kwargs={})
    assert kw["resume_session_id"] == "FLAT-OLD"
    assert fake_ctx["sessions"][SESSION_KEY] == "NEW-ID"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,has_id,pack_expected", [
    ("claude", False, True), ("claude", True, False),
    ("codex", False, True), ("codex", True, False),
])
async def test_chat_post_context_pack_only_on_a_fresh_conversation(
    aiohttp_client, fake_ctx, app, codex_on, monkeypatch, provider, has_id, pack_expected
):
    """The pack is for a conversation with no continuity id — judged with the id of the provider
    that will actually answer (a Codex thread counts; the old check only looked at Claude's)."""
    case = CASES[provider]
    packs: list = []

    def fake_assemble(*_a, **_k):
        packs.append(1)
        return "THE-PACK"

    monkeypatch.setattr(_webapp._context_pack, "assemble", fake_assemble)
    _seed_chat(fake_ctx, provider=provider, model=case["model"], field=case["field"],
               value="OLD-ID" if has_id else None)
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello"}, headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls, await resp.text()
    assert bool(packs) is pack_expected
    assert ("THE-PACK" in calls[0]["prompt"]) is pack_expected


@pytest.mark.asyncio
async def test_chat_post_unknown_chat_id_writes_the_new_session_to_the_flat_map(
    aiohttp_client, fake_ctx, app
):
    case = CASES["claude"]
    _seed_chat(fake_ctx, provider="claude", model="opus", field=None, value=None)
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "x", "chat_id": "bbbbbb"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls, await resp.text()
    assert fake_ctx["sessions"][SESSION_KEY] == "NEW-ID"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_free_chat_post_label_is_inherited_by_claude_sessions_only(
    aiohttp_client, fake_ctx, app, codex_on, provider
):
    case = CASES[provider]
    fid = "free-lbl01"
    _webapp._save_free_chats(fake_ctx, {fid: {
        "label": "My free tab", "cwd": str(Path(fake_ctx["DATA"]).parent), "model": case["model"],
        "provider": provider, "session_id": None, "codex_thread_id": None, "created_at": 1,
    }})
    # The UI lists a chat's tabs before it sends, and that listing seeds the chat entry from the
    # free record. (POSTing to a never-listed free Codex chat resolves no chat → runs on Claude;
    # a latent bug older than the seam, reported separately and deliberately not pinned here.)
    _webapp._ensure_chat_entry(fake_ctx, fid, fid)
    calls: list = []
    _install_engines(fake_ctx, case, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{fid}/chat", json={"prompt": "hi"},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls, await resp.text()
    labels = _webapp._load_session_labels(fake_ctx)
    assert (labels.get("NEW-ID") == "My free tab") is (provider == "claude")
    rec = _webapp._load_free_chats(fake_ctx)[fid]
    assert rec[case["field"]] == "NEW-ID", "free-chat record keeps the provider's own id"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,cleared", [("claude", False), ("codex", True)])
async def test_queue_drain_clears_flags_the_answering_runtime_cannot_honour(
    fake_ctx, codex_on, capsys, provider, cleared
):
    """A queued turn was already accepted, so an unhonourable flag is cleared (loudly) rather
    than failing the turn — judged against the capabilities of the provider that will answer."""
    case = CASES[provider]
    _seed_chat(fake_ctx, provider=provider, model=case["model"], field=case["field"],
               value="OLD-ID")
    kw = await _drain_once(
        fake_ctx, case,
        item_kwargs=dict(chat_id=CHAT_ID, project_id=PROJECT_ID, ask_mode=True,
                         pinned_runtime={"provider": provider, "model": case["model"]}),
    )
    out = capsys.readouterr().out
    assert ("clearing the incompatible flag(s)" in out) is cleared
    if provider == "claude":
        assert kw["ask_mode"] is True, "Claude keeps its real approval gate"


@pytest.mark.asyncio
async def test_chat_create_persists_every_providers_continuity_field(
    aiohttp_client, fake_ctx, app
):
    client = await aiohttp_client(app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/chats", json={"provider": "claude"},
                          headers=_auth(fake_ctx))
    assert r.status == 201
    new_id = (await r.json())["id"]
    rec = next(c for c in _webapp._load_chats(fake_ctx)[PROJECT_ID]["chats"] if c["id"] == new_id)
    assert rec["session_id"] is None and "codex_thread_id" in rec and rec["codex_thread_id"] is None

"""
spec-092: wiring runtime.py into the chat path (PATCH /chats/{id}, the chat queue, the
direct POST /chat provider/ask-mode decisions).

Covers:
  1. _chat_provider_lookup / _chat_provider — the fail-closed vs. permissive split.
  2. _chat_response never emits a null provider and carries provider_status/runtime_revision.
  3. PATCH /chats/{id}: real runtime changes, CAS (stale revision), and the busy refusal
     (ctx["running"], a bg turn, live sub-agent monitors) — each checked to actually gate
     the NEW code path, not just the old immutability guard it replaced.
  4. Chat-queue pinning: an item accepted against one provider must drain on that provider
     even if the chat record changes underneath it while queued.
  5. api_project_chat: fail-closed provider resolution (unavailable/unknown) and the
     ask-mode capability_conflicts check replacing the old silent downgrade.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import runtime as rt
import webapp as _webapp
from webapp import _derive_token


# ─────────────────────────── fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_chat_queue(tmp_path):
    old_file = _webapp._CHAT_QUEUE_FILE
    old_queue = dict(_webapp._CHAT_QUEUE)
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE_FILE = tmp_path / "chat-queue.json"
    yield
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old_queue)
    _webapp._CHAT_QUEUE_FILE = old_file


@pytest.fixture(autouse=True)
def reset_monitors_and_bg():
    """These module-level dicts are shared global state — never leak a fake busy/live-
    subagent marker into another test."""
    yield
    _webapp._monitors.clear()
    _webapp._bg_run_ids.clear()
    _webapp._bg_run_started.clear()


async def _unused_claude_engine(**kwargs):
    raise AssertionError(f"the default claude engine should not have run: {kwargs}")
    yield  # pragma: no cover — makes this an async generator function


@pytest.fixture
def fake_ctx(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
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
        "MODELS": {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"},
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        # api_project_chat / _chat_queue_execute both do an upfront `ctx.get("run_engine")
        # is None` guard BEFORE the provider is even resolved — a dummy default here lets
        # codex-only tests hit that provider resolution instead of the generic degraded-
        # launch guard. It is never actually invoked when the resolved provider is codex.
        "run_engine": _unused_claude_engine,
        "run_codex_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    (tmp_path / "myproject").mkdir(exist_ok=True)
    return ctx


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


@pytest.fixture
def chats_app(fake_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_get("/api/projects/{id}/chats", _webapp.api_project_chats_list)
    app.router.add_post("/api/projects/{id}/chats", _webapp.api_project_chats_create)
    app.router.add_route("PATCH", "/api/projects/{id}/chats/{chat_id}", _webapp.api_project_chats_patch)
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    app.router.add_post("/api/projects/{id}/chat/queue", _webapp.api_chat_queue_add)
    return app


async def _read_sse_events(resp) -> list:
    import json
    body = await resp.read()
    events = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass
    return events


def _enable_codex(monkeypatch, *, available=True, models=("gpt-5.6-sol",)):
    """Force codex 'enabled' (sync flag _chat_provider_lookup reads) AND wire an async
    provider_info fake (used by PATCH's fuller registry + GET /api/agent-providers)."""
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)

    async def _fake_info():
        return {
            "provider": "codex", "enabled": True, "available": available,
            "authenticated": available, "auth_type": "chatgpt" if available else None,
            "models": [{"value": m, "label": m} for m in models],
            "reasoning_levels": ["high"],
            "capabilities": {
                "chat": True, "board": True, "history": True, "search": True,
                "usage": True, "plan_mode": True, "multi_agent": True,
                "skills": True, "plugins": True, "interrupt": True,
                # deliberately NO ask_mode — codex has no can_use_tool hook.
            },
            "error": None,
        }
    return _fake_info


# ─────────────────────────── 1 & 2: provider lookup / response shape ──────────


def test_chat_provider_lookup_legacy_no_key_is_ok_claude():
    lookup = _webapp._chat_provider_lookup({"name": "Main"})
    assert lookup.status is rt.ProviderStatus.OK
    assert lookup.value == "claude"


def test_chat_provider_lookup_unknown_value_is_not_silently_claude():
    """The measured bug: the OLD ternary mapped ANY non-'codex' string to 'claude'. A
    garbage/never-valid value must come back UNKNOWN, not OK/'claude'."""
    lookup = _webapp._chat_provider_lookup({"provider": "vertex"})
    assert lookup.status is rt.ProviderStatus.UNKNOWN
    assert lookup.value == "vertex"


def test_chat_provider_lookup_disabled_codex_is_unavailable_not_claude(monkeypatch):
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: False)
    lookup = _webapp._chat_provider_lookup({"provider": "codex"})
    assert lookup.status is rt.ProviderStatus.UNAVAILABLE
    assert lookup.value == "codex"


def test_chat_provider_display_label_never_raises_on_unknown():
    """_chat_provider (the permissive display helper) must return SOME string for the many
    read-only call sites that have no error path — never raise, never return None."""
    assert _webapp._chat_provider({"provider": "vertex"}) == "claude"
    assert _webapp._chat_provider({"provider": 42}) == "claude"
    assert _webapp._chat_provider(None) == "claude"


def test_chat_response_never_emits_null_provider_and_exposes_new_fields():
    chat = {"id": "abc123", "name": "Weird", "provider": "vertex", "runtime_revision": 3}
    resp = _webapp._chat_response(chat)
    assert resp["provider"] is not None
    assert resp["provider"] == "claude"  # permissive display fallback
    assert resp["provider_status"] == "unknown"
    assert resp["runtime_revision"] == 3


def test_chat_response_defaults_runtime_revision_to_zero():
    resp = _webapp._chat_response({"id": "abc123", "name": "Main"})
    assert resp["runtime_revision"] == 0


def test_runtime_patch_keys_match_runtime_module():
    """Pins webapp's local filter tuple against runtime.py's own — same drift guard as the
    existing engine._MEMORY_MODES precedent in this codebase."""
    assert set(_webapp._RUNTIME_PATCH_KEYS) == set(rt._RUNTIME_PATCH_KEYS)


def test_claude_capabilities_include_ask_mode():
    """Wiring precondition for item 5: capability_conflicts treats an ABSENT key as
    unsupported, so Claude's real ask_mode capability must actually be present or every
    ask-mode request would wrongly conflict."""
    assert _webapp._CLAUDE_CAPABILITIES.get("ask_mode") is True


# ─────────────────────────── 3. PATCH /chats/{id} ──────────────────────────────


@pytest.mark.asyncio
async def test_patch_switches_provider_with_compatible_model_and_bumps_revision(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    fake_ctx["codex_provider_info"] = _enable_codex(monkeypatch)
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    resp = await client.patch(
        f"/api/projects/myproject/chats/{chat_id}",
        json={"provider": "codex", "model": "gpt-5.6-sol", "expected_revision": 0},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["chat"]["provider"] == "codex"
    assert data["chat"]["model"] == "gpt-5.6-sol"
    assert data["chat"]["runtime_revision"] == 1


@pytest.mark.asyncio
async def test_patch_rejects_stale_revision_with_409(aiohttp_client, fake_ctx, chats_app):
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    resp = await client.patch(
        f"/api/projects/myproject/chats/{chat_id}",
        json={"model": "opus", "expected_revision": 5},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 409
    body = await resp.json()
    assert "stale" in body["error"].lower()


@pytest.mark.asyncio
async def test_patch_refuses_runtime_change_while_turn_running(
    aiohttp_client, fake_ctx, chats_app
):
    session_key = "1001:42"
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    fake_ctx["running"][session_key] = True
    try:
        resp = await client.patch(
            f"/api/projects/myproject/chats/{chat_id}",
            json={"model": "opus", "expected_revision": 0},
            headers=_auth(fake_ctx),
        )
        assert resp.status == 409
        body = await resp.json()
        assert body.get("busy") is True
        # Nothing changed on disk.
        chats = _webapp._load_chats(fake_ctx)["myproject"]["chats"]
        chat = next(c for c in chats if c["id"] == chat_id)
        assert chat.get("model") is None
        assert chat.get("runtime_revision", 0) == 0
    finally:
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_patch_refuses_runtime_change_while_bg_turn_active(
    aiohttp_client, fake_ctx, chats_app
):
    session_key = "1001:42"
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    _webapp._bg_run_ids[session_key] = "deadbeef"
    _webapp._bg_run_started[session_key] = __import__("time").monotonic()
    resp = await client.patch(
        f"/api/projects/myproject/chats/{chat_id}",
        json={"model": "opus", "expected_revision": 0},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 409
    body = await resp.json()
    assert body.get("busy") is True


@pytest.mark.asyncio
async def test_patch_refuses_runtime_change_while_live_subagents(
    aiohttp_client, fake_ctx, chats_app
):
    """The third, non-optional busy check: a fingerprint/runtime change is deliberately
    DEFERRED by engine.py while background sub-agents are still live (disconnect() would
    SIGTERM them) — the PATCH must refuse instead of reporting a switch that is not
    actually in effect."""
    session_key = "1001:42"
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    _webapp._monitors[session_key] = {
        "mon1": {"kind": "agent", "status": "running"},
    }
    resp = await client.patch(
        f"/api/projects/myproject/chats/{chat_id}",
        json={"model": "opus", "expected_revision": 0},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 409
    body = await resp.json()
    assert body.get("busy") is True


@pytest.mark.asyncio
async def test_patch_via_injected_session_has_live_subagents_ctx_key(
    aiohttp_client, fake_ctx, chats_app
):
    """A ctx that DOES provide the engine.session_has_live_subagents wrapper (the way
    _build_ctx wires it in production) must be honoured too, not just the local monitors
    fallback."""
    session_key = "1001:42"
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    fake_ctx["session_has_live_subagents"] = lambda sk: sk == session_key
    resp = await client.patch(
        f"/api/projects/myproject/chats/{chat_id}",
        json={"model": "opus", "expected_revision": 0},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 409


@pytest.mark.asyncio
async def test_patch_rename_still_works_while_running_and_untouched_by_revision(
    aiohttp_client, fake_ctx, chats_app
):
    """A pure {name} patch is NOT a runtime change — it must NOT be blocked by a busy
    session, and must not touch runtime_revision at all (regression: the busy/CAS
    machinery must be scoped to the runtime keys only)."""
    session_key = "1001:42"
    client = await aiohttp_client(chats_app)
    created = await client.post("/api/projects/myproject/chats", json={}, headers=_auth(fake_ctx))
    chat_id = (await created.json())["id"]

    fake_ctx["running"][session_key] = True
    try:
        resp = await client.patch(
            f"/api/projects/myproject/chats/{chat_id}",
            json={"name": "Renamed"},
            headers=_auth(fake_ctx),
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["chat"]["name"] == "Renamed"
        assert data["chat"]["runtime_revision"] == 0
    finally:
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_patch_provider_switch_without_compatible_model_rejected_400(
    aiohttp_client, fake_ctx, chats_app
):
    """Confirms the NEW validation path (not the old hardcoded immutability) is what
    rejects this — an incompatible-model switch must fail even with codex available."""
    client = await aiohttp_client(chats_app)
    created = await client.post(
        "/api/projects/myproject/chats", json={"model": "sonnet"}, headers=_auth(fake_ctx)
    )
    chat_id = (await created.json())["id"]
    resp = await client.patch(
        f"/api/projects/myproject/chats/{chat_id}",
        json={"provider": "codex", "expected_revision": 0},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 400
    body = await resp.json()
    assert "does not belong to provider" in body["error"] or "leaves no model" in body["error"]


# ─────────────────────────── 4. chat-queue pinning ─────────────────────────────


def test_enqueue_stores_pinned_runtime_on_item():
    item = _webapp._chat_queue_enqueue(
        "1001:42", "hi", pinned_runtime={"provider": "claude", "model": "sonnet"}
    )
    assert item is not None
    assert item["runtime"] == {"provider": "claude", "model": "sonnet"}


def test_enqueue_without_pinned_runtime_omits_field():
    item = _webapp._chat_queue_enqueue("1001:42", "hi")
    assert item is not None
    assert "runtime" not in item


def test_pin_chat_runtime_returns_none_for_missing_chat(fake_ctx):
    assert _webapp._pin_chat_runtime(fake_ctx, {"id": "myproject"}, "nonexistent") is None


def test_pin_chat_runtime_returns_none_for_unavailable_provider(fake_ctx, monkeypatch):
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: False)
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "codex"}]},
    })
    assert _webapp._pin_chat_runtime(fake_ctx, {"id": "myproject"}, "aaaaaa") is None


def test_pin_chat_runtime_resolves_provider_and_model(fake_ctx):
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "model": "opus"}]},
    })
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": "myproject"}, "aaaaaa")
    assert pinned == {"provider": "claude", "model": "opus"}


@pytest.mark.asyncio
async def test_drain_uses_pinned_runtime_not_the_current_chat_record(fake_ctx, monkeypatch):
    """THE money test for item 3: a message accepted while the chat was on Claude/opus must
    still execute on Claude/opus even if the chat record is switched to Codex BEFORE it
    drains — reproducing the exact hazard named in the brief ('a message typed against
    Claude can execute on Codex'). Without pinning, this test fails because the drain
    re-reads the (now-codex) chat record and calls run_codex_engine instead."""
    session_key = "1001:42"
    project_id = "myproject"
    _webapp._live_turns.pop(session_key, None)
    _webapp._save_chats(fake_ctx, {
        project_id: {
            "active": "aaaaaa",
            "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                       "model": "opus", "session_id": "sess-1"}],
        }
    })

    # Accept the message while the chat is on claude/opus.
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": project_id}, "aaaaaa")
    assert pinned == {"provider": "claude", "model": "opus"}
    item = _webapp._chat_queue_enqueue(
        session_key, "do the thing", chat_id="aaaaaa", project_id=project_id,
        pinned_runtime=pinned,
    )
    assert item is not None

    # The operator flips the SAME chat to codex before this item drains.
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)
    chats = _webapp._load_chats(fake_ctx)
    chats[project_id]["chats"][0]["provider"] = "codex"
    chats[project_id]["chats"][0]["model"] = "gpt-5.6-sol"
    _webapp._save_chats(fake_ctx, chats)

    claude_calls: list = []
    codex_calls: list = []

    async def fake_claude_engine(**kwargs):
        claude_calls.append(kwargs)
        yield {"type": "result", "session_id": "sess-1"}

    async def fake_codex_engine(**kwargs):
        codex_calls.append(kwargs)
        yield {"type": "result", "thread_id": "thread-1"}

    fake_ctx["run_engine"] = fake_claude_engine
    fake_ctx["run_codex_engine"] = fake_codex_engine

    def fake_spawn_bg(coro):
        return asyncio.ensure_future(coro)

    try:
        with patch.object(_webapp, "_spawn_bg", side_effect=fake_spawn_bg), \
             patch.object(_webapp, "_secrets_read", return_value={}), \
             patch.object(_webapp, "_build_agents_kwargs", return_value={}):
            assert await _webapp._chat_queue_drain_one(fake_ctx, session_key) is True
            await asyncio.sleep(0.05)

        assert claude_calls, "the pinned Claude engine was never called"
        assert not codex_calls, (
            f"the drain ran on codex despite a Claude pin: {codex_calls}"
        )
        assert claude_calls[0]["model"] == "opus"
    finally:
        _webapp._live_turns.pop(session_key, None)
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_drain_without_pin_falls_back_to_current_chat_state(fake_ctx, monkeypatch):
    """Legacy item (no pinned runtime, e.g. an internal enqueue that predates spec-092
    pinning): the drain must still resolve provider/model from the chat record, exactly as
    it always has — this is the deliberate, documented fallback, not a regression."""
    session_key = "1001:42"
    project_id = "myproject"
    _webapp._live_turns.pop(session_key, None)
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)
    _webapp._save_chats(fake_ctx, {
        project_id: {
            "active": "aaaaaa",
            "chats": [{"id": "aaaaaa", "name": "Main", "provider": "codex",
                       "model": "gpt-5.6-sol", "codex_thread_id": "thread-9"}],
        }
    })
    item = _webapp._chat_queue_enqueue(
        session_key, "no pin here", chat_id="aaaaaa", project_id=project_id,
    )
    assert item is not None and "runtime" not in item

    codex_calls: list = []

    async def fake_codex_engine(**kwargs):
        codex_calls.append(kwargs)
        yield {"type": "result", "thread_id": "thread-9"}

    fake_ctx["run_codex_engine"] = fake_codex_engine

    def fake_spawn_bg(coro):
        return asyncio.ensure_future(coro)

    try:
        with patch.object(_webapp, "_spawn_bg", side_effect=fake_spawn_bg), \
             patch.object(_webapp, "_secrets_read", return_value={}), \
             patch.object(_webapp, "_build_agents_kwargs", return_value={}):
            assert await _webapp._chat_queue_drain_one(fake_ctx, session_key) is True
            await asyncio.sleep(0.05)
        assert codex_calls, "legacy (unpinned) item should still resolve codex from the chat record"
    finally:
        _webapp._live_turns.pop(session_key, None)
        fake_ctx["running"].pop(session_key, None)


@pytest.mark.asyncio
async def test_api_chat_queue_add_pins_runtime(aiohttp_client, fake_ctx, chats_app):
    """POST /chat/queue (the explicit 'queue this message' endpoint) must pin the runtime
    at accept time too, exactly like the busy-branch of POST /chat."""
    client = await aiohttp_client(chats_app)
    created = await client.post(
        "/api/projects/myproject/chats", json={"model": "haiku"}, headers=_auth(fake_ctx)
    )
    chat_id = (await created.json())["id"]

    resp = await client.post(
        "/api/projects/myproject/chat/queue",
        json={"text": "queue me", "chat_id": chat_id},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 201, await resp.text()
    data = await resp.json()
    assert data["item"]["runtime"] == {"provider": "claude", "model": "haiku"}


# ─────────────────────── 5. api_project_chat: fail-closed + capability_conflicts ─────


@pytest.mark.asyncio
async def test_chat_post_rejects_disabled_codex_provider_with_409(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    """A chat pinned to codex while CODEX_ENABLED is off must error immediately, not run
    (and definitely not silently fall back to Claude). Forced False explicitly — this repo's
    own dev .env sets CODEX_ENABLED=true, so relying on the ambient default would be flaky."""
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: False)
    codex_calls: list = []

    async def fake_codex_engine(**kwargs):
        codex_calls.append(kwargs)
        yield {"type": "text", "text": "should never run"}

    fake_ctx["run_codex_engine"] = fake_codex_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "codex",
                                 "model": "gpt-5.6-sol"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post(
        "/api/projects/myproject/chat", json={"prompt": "hi"}, headers=_auth(fake_ctx)
    )
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert "unavailable" in body["error"].lower()
    assert not codex_calls


@pytest.mark.asyncio
async def test_chat_post_rejects_unknown_provider_with_400(aiohttp_client, fake_ctx, chats_app):
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "vertex"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post(
        "/api/projects/myproject/chat", json={"prompt": "hi"}, headers=_auth(fake_ctx)
    )
    assert resp.status == 400
    body = await resp.json()
    assert "not a registered provider" in body["error"]


@pytest.mark.asyncio
async def test_chat_post_ask_mode_on_codex_errors_instead_of_silently_downgrading(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    """THE named bug (webapp.py:13272 old line number): ask_mode on a Codex chat used to be
    silently cleared to False and the turn ran anyway, ungated. It must now ERROR the
    request and never call the engine at all."""
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)
    codex_calls: list = []

    async def fake_codex_engine(**kwargs):
        codex_calls.append(kwargs)
        yield {"type": "text", "text": "should never run ungated"}

    fake_ctx["run_codex_engine"] = fake_codex_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "codex",
                                 "model": "gpt-5.6-sol"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "hi", "ask_mode": True},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert "ask_mode" in body["error"]
    assert not codex_calls, "the engine must never run once a requested capability conflicts"


@pytest.mark.asyncio
async def test_chat_post_ask_mode_on_claude_still_works(aiohttp_client, fake_ctx, chats_app):
    """Regression guard: Claude's REAL, working ask_mode capability must not be collateral
    damage from wiring capability_conflicts in — it must still be allowed to run gated."""
    claude_calls: list = []

    async def fake_claude_engine(**kwargs):
        claude_calls.append(kwargs)
        yield {"type": "text", "text": "ok, gated"}
        yield {"type": "result", "session_id": "sess-ask"}

    fake_ctx["run_engine"] = fake_claude_engine
    client = await aiohttp_client(chats_app)
    resp = await client.post(
        "/api/projects/myproject/chat",
        json={"prompt": "hi", "ask_mode": True},
        headers=_auth(fake_ctx),
    )
    assert resp.status == 200, await resp.text()
    events = await _read_sse_events(resp)
    assert any(e.get("type") == "error" for e in events) is False
    assert claude_calls, "the direct run never reached the engine"
    assert claude_calls[0]["ask_mode"] is True

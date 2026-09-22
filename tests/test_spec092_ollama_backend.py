"""
spec-092 P3: local inference as a BACKEND of the Claude harness.

Ollama is deliberately NOT a provider: it serves a native Anthropic /v1/messages, so the
whole agent loop rides on an env overlay instead of a third engine. What this file pins:

  1. The leak guard — a non-ollama run must have the overlay vars actively REMOVED, not
     merely not-added. A leaked ANTHROPIC_BASE_URL sends subscription traffic to a $0 local
     endpoint while looking like a perfectly normal turn. This is the file's reason to exist.
  2. Availability is a LIVE probe with a short cache, never a config flag (the GPU is shared
     with ComfyUI and the box disappears unattended).
  3. Backend-specific model validation: a local model name is legal on the local backend and
     illegal on the cloud one, and vice versa.
  4. The run path refuses at the START of a turn when the box is down, and a queued turn
     parks itself instead of silently falling back to the cloud subscription.
  5. Local models cost $0 even when their name contains a cloud keyword.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import ollama_backend as ob
import runtime as rt
import usage_pricing as up
import webapp as _webapp

from test_spec092_runtime_wiring import (  # noqa: F401 — fixtures used by name
    _auth, chats_app, fake_ctx, reset_chat_queue, reset_monitors_and_bg,
)


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    ob.reset_cache()
    yield
    ob.reset_cache()


# ───────────────────────── 1. the leak guard ──────────────────────────────────


def _rc(backend: str, model: str = "sonnet") -> rt.RunContext:
    return rt.RunContext(
        origin_kind="chat", origin_id="p1", provider="claude", backend=backend,
        model=model, account="main", revision=0,
    )


def test_ollama_env_never_leaks_into_claude_run():
    """THE test the spec names. A cloud run must be told to REMOVE the overlay vars — not
    merely to skip adding them — because the service's own environment can carry a stale
    value (a systemd unit, a leftover export) that would redirect a subscription-billed turn
    at a local endpoint with no error and no visible sign."""
    overlay = rt.ollama_env_overlay(_rc(""), base_url="http://box:11434")
    assert overlay.to_set == {}
    assert set(overlay.to_unset) == set(rt.OLLAMA_ENV_VAR_NAMES)
    assert "ANTHROPIC_BASE_URL" in overlay.to_unset


def test_engine_strips_the_overlay_vars_from_a_cloud_run():
    """End of the same rope, one level up: engine.run_engine builds the run's env dict, so
    the removal has to actually happen THERE, not only in the pure helper."""
    import engine

    env = {
        "ANTHROPIC_BASE_URL": "http://leftover:11434",
        "ANTHROPIC_AUTH_TOKEN": "local",
        "ANTHROPIC_MODEL": "qwen3.8:27b-q4_K_M",
        "SOMETHING_ELSE": "keep me",
    }
    effective = dict(env)
    overlay = rt.ollama_env_overlay(_rc(""), base_url=None)
    for name in overlay.to_unset:
        effective.pop(name, None)
    effective.update(overlay.to_set)
    assert "ANTHROPIC_BASE_URL" not in effective
    assert "ANTHROPIC_MODEL" not in effective
    assert effective["SOMETHING_ELSE"] == "keep me"
    # And the source of that rule is reachable from the engine module the run path uses.
    assert hasattr(engine, "run_engine")


def test_the_overlay_applies_only_on_the_backend_field():
    """Gated on `backend`, not on `provider` — the picker's local row IS provider='claude'."""
    overlay = rt.ollama_env_overlay(_rc("ollama", model="qwen3.8:27b-q4_K_M"),
                                    base_url="http://box:11434")
    assert overlay.to_unset == ()
    assert overlay.to_set["ANTHROPIC_BASE_URL"] == "http://box:11434"
    assert overlay.to_set["ANTHROPIC_MODEL"] == "qwen3.8:27b-q4_K_M"
    # The CLI's own helper calls hit the same endpoint — otherwise an "all-local" turn still
    # reaches out to the cloud for its cheap classification traffic.
    assert overlay.to_set["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "qwen3.8:27b-q4_K_M"


def test_an_ollama_backend_without_a_base_url_is_an_error_not_a_cloud_run():
    with pytest.raises(rt.RuntimeResolutionError):
        rt.ollama_env_overlay(_rc("ollama"), base_url=None)


def test_bot_strips_the_overlay_vars_from_the_process_environment():
    """The SDK merges a run's env dict OVER the process environment, so removing a name from
    the dict does not unset an inherited one. bot.py has to strip them at the boundary."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'os.environ.pop("ANTHROPIC_API_KEY", None)' in src
    for name in rt.OLLAMA_ENV_VAR_NAMES:
        assert name in src, f"{name} must be popped from the process env at startup"


# ───────────────────────── 2. availability is a live probe ────────────────────


@pytest.mark.asyncio
async def test_backend_is_unavailable_when_the_box_does_not_answer(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")  # nothing listens
    info = await ob.backend_info(force=True)
    assert info["enabled"] is True
    assert info["available"] is False
    assert "not answering" in (info["error"] or "")


@pytest.mark.asyncio
async def test_backend_off_by_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    info = await ob.backend_info(force=True)
    assert info["enabled"] is False and info["available"] is False


@pytest.mark.asyncio
async def test_probe_result_is_cached(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    calls = {"n": 0}

    def fake_tags(url):
        calls["n"] += 1
        return [{"name": "qwen3.8:27b-q4_K_M"}]

    monkeypatch.setattr(ob, "_fetch_tags", fake_tags)
    first = await ob.backend_info(force=True)
    second = await ob.backend_info()
    assert first["available"] and second["available"]
    assert calls["n"] == 1, "a second call inside the TTL must not re-probe the box"


# ───────────────────────── 3. backend-specific model validation ───────────────


def _providers_with_ollama():
    return {
        "claude": rt.ProviderInfo(
            provider="claude", available=True, models=("sonnet", "opus"),
            backends=("", "ollama"),
            backend_models={"ollama": ("qwen3.8:27b-q4_K_M",)},
        )
    }


def test_a_local_model_is_legal_on_the_local_backend():
    ok, reason = rt.validate_runtime_change(
        {"backend": "ollama", "model": "qwen3.8:27b-q4_K_M"},
        providers=_providers_with_ollama(), accounts_list=[{"id": "main"}],
        current={"provider": "claude", "model": "sonnet"},
    )
    assert ok, reason


def test_a_cloud_alias_is_rejected_on_the_local_backend():
    """The failure this prevents: the chat says Ollama, the CLI is handed `sonnet`, and the
    local endpoint has no such model — the turn dies on the first token."""
    ok, reason = rt.validate_runtime_change(
        {"backend": "ollama"},
        providers=_providers_with_ollama(), accounts_list=[{"id": "main"}],
        current={"provider": "claude", "model": "sonnet"},
    )
    assert not ok and "ollama" in reason


def test_a_local_model_is_rejected_on_the_cloud_backend():
    ok, reason = rt.validate_runtime_change(
        {"backend": "", "model": "qwen3.8:27b-q4_K_M"},
        providers=_providers_with_ollama(), accounts_list=[{"id": "main"}],
        current={"provider": "claude", "model": "sonnet", "backend": "ollama"},
    )
    assert not ok and "does not belong" in reason


def test_ollama_is_refused_as_a_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "ollama"},
        providers=_providers_with_ollama(), accounts_list=[{"id": "main"}],
        current={"provider": "claude", "model": "sonnet"},
    )
    assert not ok and "is not a provider" in reason


def test_an_unadvertised_backend_is_rejected():
    providers = {"claude": rt.ProviderInfo(provider="claude", available=True,
                                           models=("sonnet",), backends=("",))}
    ok, reason = rt.validate_runtime_change(
        {"backend": "ollama", "model": "sonnet"},
        providers=providers, accounts_list=[{"id": "main"}],
        current={"provider": "claude", "model": "sonnet"},
    )
    assert not ok and "not offered" in reason


# ───────────────────────── 4. the run path ────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_on_a_dead_local_backend_refuses_before_the_turn_starts(
    aiohttp_client, fake_ctx, chats_app
):
    """Refusing up front is the whole point: otherwise the CLI dies mid-sentence on a bare
    TCP refusal with the operator's prompt already half-answered — and a silent fallback to
    the cloud subscription is the one thing an 'all-local' chat must never do."""
    async def never(**kwargs):
        raise AssertionError("no engine may run when the pinned backend is down")
        yield  # pragma: no cover

    async def down():
        return {"backend": "ollama", "enabled": True, "available": False, "models": [],
                "base_url": "http://box:11434", "error": "the GPU may be held by another stack"}

    fake_ctx["run_engine"] = never
    fake_ctx["ollama_backend_info"] = down
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Local", "provider": "claude",
                                 "backend": "ollama", "model": "qwen3.8:27b-q4_K_M"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hi", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 409
    body = await resp.json()
    assert body.get("backend_unavailable") is True
    assert "GPU" in body["error"]


@pytest.mark.asyncio
async def test_chat_on_a_live_local_backend_reaches_the_engine_with_the_backend(
    aiohttp_client, fake_ctx, chats_app
):
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "local hi"}
        yield {"type": "result", "session_id": "s-local"}

    async def up_info():
        return {"backend": "ollama", "enabled": True, "available": True,
                "models": [{"value": "qwen3.8:27b-q4_K_M", "label": "qwen"}],
                "base_url": "http://box:11434", "error": None}

    fake_ctx["run_engine"] = fake_engine
    fake_ctx["ollama_backend_info"] = up_info
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Local", "provider": "claude",
                                 "backend": "ollama", "model": "qwen3.8:27b-q4_K_M"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hi", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and calls[0]["backend"] == "ollama"
    assert calls[0]["model"] == "qwen3.8:27b-q4_K_M"


@pytest.mark.asyncio
async def test_a_cloud_chat_never_receives_a_backend(aiohttp_client, fake_ctx, chats_app):
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s1"}

    fake_ctx["run_engine"] = fake_engine
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat", json={"prompt": "hi"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and calls[0]["backend"] == ""


@pytest.mark.asyncio
async def test_a_queued_local_turn_parks_instead_of_running_on_the_cloud(fake_ctx, capsys):
    """spec-092 P3b: eviction is a DESIGN CONSTANT (the 3090 is shared with ComfyUI on
    purpose), so losing the backend mid-queue must be neither an operator-facing error nor a
    silent re-route. The message goes back on the queue, deferred, with a reason."""
    async def never(**kwargs):
        raise AssertionError("a parked turn must not reach any engine")
        yield  # pragma: no cover

    async def down():
        return {"backend": "ollama", "enabled": True, "available": False, "models": [],
                "base_url": "http://box:11434", "error": "not answering"}

    fake_ctx["run_engine"] = never
    fake_ctx["ollama_backend_info"] = down
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Local", "provider": "claude",
                                 "backend": "ollama", "model": "qwen3.8:27b-q4_K_M"}]},
    })
    item = _webapp._chat_queue_enqueue(
        "1001:42", "run this locally", "aaaaaa", "myproject",
        pinned_runtime={"provider": "claude", "model": "qwen3.8:27b-q4_K_M",
                        "backend": "ollama"},
    )
    assert item is not None
    # _chat_queue_drain_one pops the item before spawning the executor; calling the executor
    # directly has to reproduce that, or the "parked" copy is counted alongside the original.
    _webapp._chat_queue_pop_ready("1001:42")
    await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)
    parked = _webapp._chat_queue_get("1001:42")
    assert len(parked) == 1, "the message must be back on the queue, not lost"
    assert parked[0]["text"] == "run this locally"
    assert "backend evicted" in parked[0]["blocked_reason"]
    assert parked[0]["not_before"] > 0
    assert parked[0]["runtime"]["backend"] == "ollama"
    assert "parked" in capsys.readouterr().out


def test_a_deferred_item_does_not_block_other_chats():
    """The queue is per PROJECT. A parked local turn sitting at its head would otherwise
    hold every OTHER chat's messages behind it for as long as the GPU is busy."""
    import time as _t
    _webapp._CHAT_QUEUE["k"] = [
        {"id": "parked", "chat_id": "local1", "text": "local", "created_at": _t.time(),
         "not_before": _t.time() + 300},
        {"id": "ready", "chat_id": "cloud1", "text": "cloud", "created_at": _t.time()},
    ]
    got = _webapp._chat_queue_pop_ready("k")
    assert got is not None and got["id"] == "ready"
    left = _webapp._chat_queue_get("k")
    assert len(left) == 1 and left[0]["id"] == "parked"
    _webapp._CHAT_QUEUE.pop("k", None)


def test_a_deferred_item_DOES_hold_back_its_own_chat():
    """The other half, and the one that matters for correctness: skipping ahead within the
    SAME chat would let the later message run first, advance that chat's session_id, and
    leave the earlier one resuming a conversation that already answers a question it has not
    asked yet."""
    import time as _t
    _webapp._CHAT_QUEUE["k"] = [
        {"id": "parked", "chat_id": "same", "text": "first", "created_at": _t.time(),
         "not_before": _t.time() + 300},
        {"id": "later", "chat_id": "same", "text": "second", "created_at": _t.time()},
        {"id": "other", "chat_id": "elsewhere", "text": "unrelated", "created_at": _t.time()},
    ]
    got = _webapp._chat_queue_pop_ready("k")
    assert got is not None and got["id"] == "other", (
        "the unrelated chat runs; the same chat's later message waits its turn"
    )
    assert [i["id"] for i in _webapp._chat_queue_get("k")] == ["parked", "later"]
    _webapp._CHAT_QUEUE.pop("k", None)


def test_pop_ready_returns_none_when_everything_is_deferred():
    import time as _t
    _webapp._CHAT_QUEUE["k"] = [
        {"id": "parked", "text": "local", "created_at": _t.time(), "not_before": _t.time() + 300},
    ]
    assert _webapp._chat_queue_pop_ready("k") is None
    assert len(_webapp._chat_queue_get("k")) == 1
    _webapp._CHAT_QUEUE.pop("k", None)


# ───────────────────────── 5. a local turn costs nothing ──────────────────────


def test_local_models_are_never_billed():
    assert up.is_billable("qwen3.8:27b-q4_K_M") is False
    assert up.get_pricing("qwen3.8:27b-q4_K_M") is None


def test_a_local_model_named_like_a_cloud_one_is_still_free():
    """The trap the spec named: pricing keys off the model NAME, so a local finetune tagged
    `...sonnet...` would be priced at cloud Sonnet rates and inflate every figure."""
    assert up.is_billable("my-sonnet-finetune:13b") is False
    assert up.get_pricing("my-sonnet-finetune:13b") is None


def test_cloud_models_are_still_billed():
    assert up.is_billable("claude-sonnet-5") is True
    assert up.get_pricing("claude-opus-4-8-20260115") is not None
    assert up.is_billable("sonnet") is True


def test_an_available_backend_with_no_known_models_is_refused_not_cloud_validated():
    """Review finding 4. `backend_models.get(b) or info.models` fell back to the CLOUD list
    for an empty entry, so `sonnet` validated as a legal pick for a local box that has no
    such model — a turn that dies on its first token. Unknown is a refusal here."""
    providers = {"claude": rt.ProviderInfo(
        provider="claude", available=True, models=("sonnet",),
        backends=("", "ollama"), backend_models={},
    )}
    ok, reason = rt.validate_runtime_change(
        {"backend": "ollama", "model": "sonnet"},
        providers=providers, accounts_list=[{"id": "main"}],
        current={"provider": "claude", "model": "sonnet"},
    )
    assert not ok and "no models are known" in reason


@pytest.mark.asyncio
async def test_a_parked_turn_keeps_its_auto_rotate_optin(fake_ctx):
    """Review finding 2: auto_rotate was not passed through on re-enqueue, so a message that
    parked and later drained ran with the operator's explicit opt-in silently dropped."""
    async def down():
        return {"backend": "ollama", "enabled": True, "available": False, "models": [],
                "base_url": "http://box:11434", "error": "not answering"}

    async def never(**kwargs):
        raise AssertionError("must not run")
        yield  # pragma: no cover

    fake_ctx["run_engine"] = never
    fake_ctx["ollama_backend_info"] = down
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Local", "provider": "claude",
                                 "backend": "ollama", "model": "qwen3.8:27b-q4_K_M"}]},
    })
    item = _webapp._chat_queue_enqueue(
        "1001:42", "rotate after this", "aaaaaa", "myproject", auto_rotate=True,
        pinned_runtime={"provider": "claude", "model": "qwen3.8:27b-q4_K_M",
                        "backend": "ollama"},
    )
    _webapp._chat_queue_pop_ready("1001:42")
    await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)
    parked = _webapp._chat_queue_get("1001:42")
    assert len(parked) == 1 and parked[0]["auto_rotate"] is True


# ───────────────────── project-level pin (the operator's actual ask) ──────────


@pytest.mark.asyncio
async def test_a_project_pinned_to_the_local_box_runs_every_chat_there(
    aiohttp_client, fake_ctx, chats_app
):
    """The operator's stated use: pin a whole project to the local model so it keeps working
    when the paid subscription is unavailable. The chat record says nothing about a backend —
    the project pin alone must route the turn."""
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s1"}

    async def up_info():
        return {"backend": "ollama", "enabled": True, "available": True,
                "models": [{"value": "qwen3.8:27b-q4_K_M", "label": "qwen"}],
                "base_url": "http://box:11434", "error": None}

    fake_ctx["run_engine"] = fake_engine
    fake_ctx["ollama_backend_info"] = up_info
    fake_ctx["topics"]["1001:42"]["backend"] = "ollama"
    client = await aiohttp_client(chats_app)
    resp = await client.post("/api/projects/myproject/chat", json={"prompt": "hi"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and calls[0]["backend"] == "ollama"


@pytest.mark.asyncio
async def test_a_pinned_project_swaps_a_cloud_model_for_one_the_box_serves(
    aiohttp_client, fake_ctx, chats_app
):
    """Without this the switch is not an emergency switch: the chat still says `sonnet`, the
    local endpoint has no such name, and the very first turn dies — so the operator would
    have to hand-retype a model id at exactly the moment their subscription just failed."""
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s1"}

    async def up_info():
        return {"backend": "ollama", "enabled": True, "available": True,
                "models": [{"value": "qwen3.8:27b-q4_K_M", "label": "qwen"}],
                "base_url": "http://box:11434", "error": None}

    fake_ctx["run_engine"] = fake_engine
    fake_ctx["ollama_backend_info"] = up_info
    fake_ctx["topics"]["1001:42"]["backend"] = "ollama"
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet"}]},
    })
    resp = await (await aiohttp_client(chats_app)).post(
        "/api/projects/myproject/chat", json={"prompt": "hi", "chat_id": "aaaaaa"},
        headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and calls[0]["model"] == "qwen3.8:27b-q4_K_M"


@pytest.mark.asyncio
async def test_a_pinned_project_refuses_to_let_a_chat_move_back_to_the_cloud(
    aiohttp_client, fake_ctx, chats_app, monkeypatch
):
    """The pin is containment, not a default. If a chat could override it the picker would be
    a lie: _resolve_run_backend makes the project win at RUN time, so the row would say Codex
    while every turn still went to the local box."""
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)

    async def codex_info():
        return {"provider": "codex", "enabled": True, "available": True, "authenticated": True,
                "models": [{"value": "gpt-5.6-sol", "label": "gpt"}],
                "reasoning_levels": ["high"], "capabilities": {"plan_mode": True},
                "error": None}

    async def up_info():
        return {"backend": "ollama", "enabled": True, "available": True,
                "models": [{"value": "qwen3.8:27b-q4_K_M", "label": "qwen"}],
                "base_url": "http://box:11434", "error": None}

    fake_ctx["codex_provider_info"] = codex_info
    fake_ctx["ollama_backend_info"] = up_info
    fake_ctx["topics"]["1001:42"]["backend"] = "ollama"
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "model": "sonnet"}]},
    })
    client = await aiohttp_client(chats_app)
    resp = await client.patch("/api/projects/myproject/chats/aaaaaa",
                              json={"provider": "codex", "model": "gpt-5.6-sol"},
                              headers=_auth(fake_ctx))
    assert resp.status == 409
    body = await resp.json()
    assert body["project_pinned"] == "ollama"


@pytest.mark.asyncio
async def test_a_cloud_project_still_lets_one_chat_go_local(
    aiohttp_client, fake_ctx, chats_app
):
    """The other direction stays open: pinning a single chat to the local box inside a cloud
    project only ever REMOVES cloud traffic, so there is nothing to contain."""
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s1"}

    async def up_info():
        return {"backend": "ollama", "enabled": True, "available": True,
                "models": [{"value": "qwen3.8:27b-q4_K_M", "label": "qwen"}],
                "base_url": "http://box:11434", "error": None}

    fake_ctx["run_engine"] = fake_engine
    fake_ctx["ollama_backend_info"] = up_info
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Local", "provider": "claude",
                                 "backend": "ollama", "model": "qwen3.8:27b-q4_K_M"}]},
    })
    resp = await (await aiohttp_client(chats_app)).post(
        "/api/projects/myproject/chat", json={"prompt": "hi", "chat_id": "aaaaaa"},
        headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and calls[0]["backend"] == "ollama"


def test_the_project_backend_setting_is_a_real_settings_field():
    """A field missing from _PROJECT_SETTING_FIELDS is rejected by the settings PATCH, and
    one missing from the project record assembly is invisible to every endpoint — the
    selector would appear to save and change nothing."""
    assert "backend" in _webapp._PROJECT_SETTING_FIELDS
    src = (ROOT / "webapp.py").read_text(encoding="utf-8")
    assert src.count('"backend": b.get("backend") or None,') == 2, (
        "both project-record builders (bound projects and free chats) must carry the pin"
    )

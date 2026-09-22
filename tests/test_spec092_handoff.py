"""
spec-092 P2: the handoff block a chat hands to the other engine when it crosses runtimes.

The design constraint that shapes every test here: this is a DETERMINISTIC extractor, not a
model call. The spec's review found three defects blocking reuse of the /rotate summariser —
it parses Claude JSONL only, its injector keys pending summaries by the PROJECT session_key
(a sibling chat can consume one) and deletes them BEFORE delivery is confirmed, and its
helper hardwires claude-sonnet-5 with no account/backend, which would ship an all-local
chat's transcript to the cloud. Each of those is pinned below as a property of the
replacement, not as a comment.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import handoff as hf
import webapp as _webapp

from test_spec092_runtime_wiring import (  # noqa: F401 — fixtures used by name
    _auth, chats_app, fake_ctx, reset_chat_queue, reset_monitors_and_bg,
)


def _app(fake_ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx
    app.router.add_post("/api/projects/{id}/chats/{chat_id}/handoff",
                        _webapp.api_project_chat_handoff)
    app.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    return app


# ───────────────────────── the extractor ──────────────────────────────────────


def test_negative_constraints_are_carried_verbatim():
    """A summary is lossy exactly where agentic work lives. A paraphrased "avoid changing
    that module" is not the same instruction as the operator's own words, and the next
    engine acts on what it reads."""
    msgs = [
        {"role": "user", "text": "start the picker\ndo not touch webapp.py", "tools": []},
        {"role": "assistant", "text": "sure, I will avoid webapp.py", "tools": []},
    ]
    out = hf.extract_constraints(msgs)
    assert "do not touch webapp.py" in out


def test_only_the_operators_constraints_count():
    """The assistant restating a rule is an echo — carrying it doubles the noise and gives a
    model's own paraphrase the authority of an instruction."""
    msgs = [{"role": "assistant", "text": "I will never touch engine.py", "tools": []}]
    assert hf.extract_constraints(msgs) == []


def test_russian_constraints_are_recognised():
    """The operator writes in Russian; a rule stated there binds exactly as much."""
    msgs = [{"role": "user", "text": "никогда не пушь без тестов", "tools": []}]
    assert hf.extract_constraints(msgs) == ["никогда не пушь без тестов"]


def test_repeated_constraints_are_deduplicated():
    msgs = [
        {"role": "user", "text": "do not push without tests", "tools": []},
        {"role": "user", "text": "Do Not Push Without Tests", "tools": []},
    ]
    assert len(hf.extract_constraints(msgs)) == 1


def test_touched_files_come_from_the_tool_calls():
    msgs = [{"role": "assistant", "text": "", "tools": [
        {"kind": "edit", "file": "engine.py"},
        {"kind": "read", "file": "runtime.py"},
        {"kind": "bash", "cmd": "ls"},
    ]}]
    assert hf.extract_files(msgs) == ["engine.py", "runtime.py"]


def test_cockpit_chrome_is_not_conversation():
    """Board strips, runtime markers and model-fallback rows mean nothing to the other
    engine and would waste its (possibly 32k) context."""
    msgs = [
        {"role": "board", "text": "card moved", "tools": []},
        {"role": "runtime", "text": "", "tools": []},
        {"role": "user", "text": "real question", "tools": []},
    ]
    recent = hf.recent_messages(msgs)
    assert [m["role"] for m in recent] == ["user"]


def test_long_messages_are_truncated_not_dropped():
    msgs = [{"role": "user", "text": "x" * 5000, "tools": []}]
    out = hf.recent_messages(msgs)
    assert len(out) == 1 and "truncated" in out[0]["text"]


def test_the_block_states_what_was_not_replayed():
    msgs = [{"role": "user", "text": f"msg {i}", "tools": []} for i in range(20)]
    built = hf.build_handoff(msgs, from_label="Claude · Main", to_label="Codex (ChatGPT)")
    assert built["unreplayed"] == 20 - len(built["recent"])
    assert "not replayed" in built["text"]
    assert "Claude · Main" in built["text"] and "Codex (ChatGPT)" in built["text"]


def test_the_block_warns_against_reverting_unseen_work():
    """The measured hazard: turns 1-5 on Claude, 6-8 on Codex, back to Claude. The returning
    engine never saw 6-8 and will happily "fix back" work that is already correct."""
    built = hf.build_handoff([{"role": "user", "text": "hi", "tools": []}],
                             from_label="A", to_label="B")
    assert "redoing or reverting" in built["text"]


# ───────────────────────── the endpoint ───────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_does_not_store_anything(aiohttp_client, fake_ctx):
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude"}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post(
        "/api/projects/myproject/chats/aaaaaa/handoff",
        json={"messages": [{"role": "user", "text": "do not touch webapp.py", "tools": []}],
              "from_label": "Claude", "to_label": "Codex"},
        headers=_auth(fake_ctx))
    assert resp.status == 200
    body = await resp.json()
    assert "do not touch webapp.py" in body["handoff"]["constraints"]
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert "runtime_handoff" not in chat, "a preview must not arm anything"


@pytest.mark.asyncio
async def test_commit_arms_the_operators_edited_text(aiohttp_client, fake_ctx):
    """The operator must be able to EDIT it — a wrong 'standing constraint' quoted into the
    next engine is believed as fact, so the extractor's guess is a draft, not a verdict."""
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude"}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post(
        "/api/projects/myproject/chats/aaaaaa/handoff",
        json={"messages": [], "from_label": "Claude", "to_label": "Codex",
              "commit": True, "text": "ONLY what I actually meant"},
        headers=_auth(fake_ctx))
    assert resp.status == 200 and (await resp.json())["armed"] is True
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert chat["runtime_handoff"]["text"] == "ONLY what I actually meant"


@pytest.mark.asyncio
async def test_an_emptied_block_means_send_nothing(aiohttp_client, fake_ctx):
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "runtime_handoff": {"text": "stale"}}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post(
        "/api/projects/myproject/chats/aaaaaa/handoff",
        json={"messages": [], "from_label": "A", "to_label": "B", "commit": True, "text": "  "},
        headers=_auth(fake_ctx))
    assert (await resp.json())["armed"] is False
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert "runtime_handoff" not in chat


@pytest.mark.asyncio
async def test_the_handoff_is_stored_on_the_chat_not_the_session_key(aiohttp_client, fake_ctx):
    """The /rotate defect this refuses to inherit: its injector keys pending summaries by the
    PROJECT session_key, so a sibling chat of the same project can consume one."""
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa", "chats": [
            {"id": "aaaaaa", "name": "One", "provider": "claude"},
            {"id": "bbbbbb", "name": "Two", "provider": "claude"},
        ]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    await client.post("/api/projects/myproject/chats/aaaaaa/handoff",
                      json={"messages": [], "from_label": "A", "to_label": "B",
                            "commit": True, "text": "for chat one only"},
                      headers=_auth(fake_ctx))
    chats = _webapp._load_chats(fake_ctx)["myproject"]["chats"]
    assert chats[0].get("runtime_handoff", {}).get("text") == "for chat one only"
    assert "runtime_handoff" not in chats[1]
    assert not (fake_ctx.get("pending_handoff") or {}), "must not touch /rotate's own store"


# ───────────────────────── delivery ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_armed_handoff_prefixes_the_next_prompt_and_clears_on_confirmation(
    aiohttp_client, fake_ctx
):
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "got it"}
        yield {"type": "result", "session_id": "s-new"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "runtime_handoff": {"text": "# Handoff\nconstraint: X",
                                                     "from_label": "Codex",
                                                     "to_label": "Claude"}}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "continue", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls, "the turn must run"
    assert calls[0]["prompt"].startswith("# Handoff")
    assert "continue" in calls[0]["prompt"]
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert "runtime_handoff" not in chat, "cleared only AFTER the engine answered"


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_the_handoff_armed(aiohttp_client, fake_ctx):
    """The /rotate defect this refuses to inherit #2: it deletes the summary BEFORE delivery
    is confirmed. A turn that dies before the engine ever read the block must not lose it —
    otherwise the next engine answers a conversation it knows nothing about, silently."""
    async def dying_engine(**kwargs):
        yield {"type": "error", "error": "boom"}

    fake_ctx["run_engine"] = dying_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "runtime_handoff": {"text": "# Handoff\nkeep me"}}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "continue", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    await resp.text()
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert chat.get("runtime_handoff", {}).get("text") == "# Handoff\nkeep me"


def test_the_builder_cannot_reach_the_network_or_a_model():
    """The /rotate defect this refuses to inherit #3: its summariser helper hardwires
    claude-sonnet-5 with no account or backend, so building a handoff for an ALL-LOCAL chat
    would ship that chat's transcript to the cloud.

    Asserted on the module's IMPORT GRAPH, not on its source text — a substring check would
    trip over this very explanation in a docstring, and (worse) would keep passing if the
    call were spelled differently. What actually makes the leak impossible is that nothing
    capable of I/O is reachable from here.
    """
    import ast as _ast

    tree = _ast.parse((ROOT / "handoff.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # Everything this module is allowed to need. Anything else is a new capability and must
    # be justified deliberately, not slip in.
    assert imported <= {"re", "__future__"}, f"unexpected imports: {sorted(imported)}"
    # And it is pure: same input, same output, no clock, no id generation.
    msgs = [{"role": "user", "text": "do not touch X", "tools": []}]
    a = hf.build_handoff(msgs, from_label="A", to_label="B")
    b = hf.build_handoff(msgs, from_label="A", to_label="B")
    assert a == b


# ───────────── review findings: the lifecycle holes ───────────────────────────


@pytest.mark.asyncio
async def test_a_queued_turn_also_receives_the_handoff(fake_ctx):
    """Review finding 1 (blocker). The injection lived ONLY in the direct-run branch, which
    is reached after the busy/queue branches have already returned. A message typed just
    before a crossing — or one queued behind live sub-agents — therefore ran with the bare
    prompt, and the block stayed armed to glue itself onto some later, unrelated turn."""
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "s-q"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "runtime_handoff": {"text": "# Handoff\nconstraint: X",
                                                     "for_provider": "claude",
                                                     "for_backend": ""}}]},
    })
    item = _webapp._chat_queue_enqueue("1001:42", "continue", "aaaaaa", "myproject",
                                       pinned_runtime={"provider": "claude", "model": "sonnet"})
    _webapp._chat_queue_pop_ready("1001:42")
    await _webapp._chat_queue_execute(fake_ctx, "1001:42", item)
    assert calls, "the queued turn must run"
    assert calls[0]["prompt"].startswith("# Handoff")
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert "runtime_handoff" not in chat, "and it must be cleared once that turn answered"


@pytest.mark.asyncio
async def test_a_handoff_is_never_delivered_to_the_engine_it_was_written_for(
    aiohttp_client, fake_ctx
):
    """Switch Claude → Codex (block armed for Codex), change your mind, switch back before
    sending. The block is now a summary of Claude's own work addressed to Claude: delivering
    it would tell the engine it is missing context it has in full."""
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s1"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "runtime_handoff": {"text": "written for codex",
                                                     "for_provider": "codex",
                                                     "for_backend": ""}}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hello", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    assert resp.status == 200
    await resp.text()
    assert calls and not calls[0]["prompt"].startswith("written for codex")
    chat = _webapp._load_chats(fake_ctx)["myproject"]["chats"][0]
    assert "runtime_handoff" not in chat, "a stale block is dropped, not left to misfire later"


@pytest.mark.asyncio
async def test_a_legacy_block_without_a_target_is_still_delivered(aiohttp_client, fake_ctx):
    """Records armed before `for_provider` existed can only exist inside one deploy window.
    Treating them as stale would silently discard an operator's edited block."""
    calls: list = []

    async def fake_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "session_id": "s1"}

    fake_ctx["run_engine"] = fake_engine
    _webapp._save_chats(fake_ctx, {
        "myproject": {"active": "aaaaaa",
                      "chats": [{"id": "aaaaaa", "name": "Main", "provider": "claude",
                                 "runtime_handoff": {"text": "legacy block"}}]},
    })
    client = await aiohttp_client(_app(fake_ctx))
    resp = await client.post("/api/projects/myproject/chat",
                             json={"prompt": "hello", "chat_id": "aaaaaa"},
                             headers=_auth(fake_ctx))
    await resp.text()
    assert calls and calls[0]["prompt"].startswith("legacy block")

"""spec-098 endpoint A: POST /api/projects/{id}/sessions/{provider}/{session_id}/delete.

Contract: JSON-only (415), FORGET_ENABLED=0 -> 404 disabled, dry run -> plan + plan_token, execute
recomputes the token after quiescing (409 changed), every refusal leaves every store byte-identical,
bus events session_deleted + forget_residue.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))

import forget
import forget_claude  # noqa: F401
import webapp
from _forget_world import K_SID, PID, SENT, T_SID, World, j
from test_forget_core import check_world, seed_runtime_state

URL = "/api/projects/{pid}/sessions/{prov}/{sid}/delete"


@pytest.fixture
def world(tmp_path, monkeypatch):
    forget._CRASH_AFTER = None
    w = World(tmp_path, monkeypatch)
    yield w
    forget._CONFIG.clear()
    webapp._recent_board_events.pop("1001:42", None)
    webapp._live_turns.pop("1001:42", None)


@pytest.fixture
def events(monkeypatch):
    out = []
    orig = webapp._bus_publish

    def rec(session_key, event, persist=True):
        out.append((session_key, event, persist))
        return orig(session_key, event, persist=persist)
    monkeypatch.setattr(webapp, "_bus_publish", rec)
    return out


@pytest.fixture
async def client(aiohttp_client, world):
    ctx = world.ctx()
    app = web.Application(middlewares=[webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/sessions/{provider}/{session_id}/delete", webapp.api_session_delete)
    c = await aiohttp_client(app)
    c.ctx = ctx
    c.hdr = {"Cookie": f"cops_auth={ctx['_auth_token']}"}
    return c


async def post(client, body, *, pid=PID, prov="claude", sid=T_SID, **kw):
    return await client.post(URL.format(pid=pid, prov=prov, sid=sid), json=body, headers=client.hdr, **kw)


async def dry(client, **kw):
    r = await post(client, {"dry_run": True}, **kw)
    assert r.status == 200, await r.text()
    return (await r.json())["plan_token"]


# ───────────────────────────── plan ─────────────────────────────────────────────────────────
async def test_dry_run_returns_the_contract_plan_and_touches_nothing(client, world):
    before = world.digest_all()
    r = await post(client, {"dry_run": True})
    body = await r.json()
    assert r.status == 200 and body["ok"] is True and len(body["plan_token"]) == 64
    p = body["plan"]
    assert set(p) == {"provider", "session_id", "title", "turns", "first_ts", "last_ts", "files", "bytes",
                      "active_in_chats", "side_effects", "lineage", "diary_days", "limits", "warnings"}
    assert p["provider"] == "claude" and p["active_in_chats"] == ["c1"] and p["turns"] == 1
    assert all(set(s) == {"kind", "detail"} for s in p["side_effects"])
    assert world.digest_all() == before


async def test_missing_dry_run_flag_is_a_dry_run(client, world):
    before = world.digest_all()
    r = await post(client, {})
    assert r.status == 200 and "plan" in await r.json()
    assert world.digest_all() == before


async def test_lineage_lists_successors_from_links_and_the_lineage_log(client, world):
    (world.data / "session_links.json").write_text(json.dumps({K_SID: {"prev": T_SID, "handoff": True}}))
    (world.data / "session_lineage.jsonl").write_bytes(j(
        {"kind": "runtime_handoff", "from_provider": "claude", "from_session": T_SID,
         "to_provider": "codex", "to_session": "thread-abcdef12", "ts": 1}))
    r = await post(client, {"dry_run": True})
    lin = (await r.json())["plan"]["lineage"]
    assert {(x["provider"], x["session_id"], x["kind"]) for x in lin} == {
        ("claude", K_SID, "rotate"), ("codex", "thread-abcdef12", "runtime_handoff")}


# ───────────────────────────── execute ──────────────────────────────────────────────────────
async def test_execute_purges_and_publishes_events(client, world, events):
    before = world.digest_all()
    seed_runtime_state()
    token = await dry(client)
    r = await post(client, {"dry_run": False, "plan_token": token})
    body = await r.json()
    assert r.status == 200 and body["ok"] and body["residue"] == "scanning" and body["also_deleted"] == []
    assert set(body["deleted"]) == {"files", "timeline_lines", "audit_lines", "search_rows", "media", "unattributed"}
    assert body["deleted"]["timeline_lines"] == 4 and body["deleted"]["audit_lines"] == 2
    deleted_ev = [e for _k, e, persist in events if e.get("kind") == "session_deleted"]
    assert deleted_ev and deleted_ev[0] == {"kind": "session_deleted", "provider": "claude", "session_id": T_SID,
                                            "project_id": PID, "chat_ids": ["c1"]}
    assert all(persist is False for _k, e, persist in events if e.get("kind") in ("session_deleted", "forget_residue"))
    for _ in range(100):                                  # the residue scan runs after the response
        if any(e.get("kind") == "forget_residue" for _k, e, _p in events):
            break
        await asyncio.sleep(0.1)
    residue = [e for _k, e, _p in events if e.get("kind") == "forget_residue"][0]
    assert residue["clean"] is True and residue["hits"] == [] and residue["truncated"] is False
    assert residue["provider"] == "claude" and residue["session_id"] == T_SID
    assert check_world(world, before, client.ctx) == []
    assert not client.ctx["running"], "the running slots must be released"
    assert (world.data / "inbox" / f"forget-{T_SID[:8]}.txt").exists()


async def test_also_delete_follows_the_lineage_the_preview_showed(client, world):
    (world.data / "session_links.json").write_text(json.dumps({K_SID: {"prev": T_SID, "handoff": True}}))
    token = await dry(client)
    r = await post(client, {"dry_run": False, "plan_token": token,
                            "also_delete": [{"provider": "claude", "session_id": K_SID}]})
    body = await r.json()
    assert r.status == 200 and body["also_deleted"] == [{"provider": "claude", "session_id": K_SID}]
    assert not (world.proj_dir / f"{K_SID}.jsonl").exists() and not (world.proj_dir / f"{T_SID}.jsonl").exists()
    assert not world.sentinel_paths()


async def test_also_delete_of_an_unlisted_session_is_refused_up_front(client, world):
    token = await dry(client)
    before = world.digest_all()
    r = await post(client, {"dry_run": False, "plan_token": token,
                            "also_delete": [{"provider": "claude", "session_id": K_SID}]})
    assert r.status == 400 and (await r.json())["code"] == "bad_request"
    assert world.digest_all() == before


# ───────────────────────────── acceptance 3: refusals leave every store byte-identical ──────
async def test_disabled_is_404(client, world, monkeypatch):
    monkeypatch.setenv("FORGET_ENABLED", "0")
    before = world.digest_all()
    r = await post(client, {"dry_run": True})
    assert r.status == 404 and (await r.json())["code"] == "disabled"
    assert world.digest_all() == before


@pytest.mark.parametrize("kw", [dict(data=b"{}", headers={"Content-Type": "text/plain"}),
                                dict(data="dry_run=true", headers={"Content-Type": "application/x-www-form-urlencoded"})])
async def test_non_json_content_type_is_415(client, world, kw):
    before = world.digest_all()
    kw["headers"] = {**client.hdr, **kw["headers"]}
    r = await client.post(URL.format(pid=PID, prov="claude", sid=T_SID), **kw)
    assert r.status == 415
    assert world.digest_all() == before


async def test_unknown_project_session_provider_and_bad_id(client, world):
    before = world.digest_all()
    r = await post(client, {"dry_run": True}, pid="nope")
    assert r.status == 404 and (await r.json())["code"] == "not_found"
    r = await post(client, {"dry_run": True}, sid="33333333-cccc-4ccc-8ccc-333333333333")
    assert r.status == 404 and (await r.json())["code"] == "not_found"
    r = await post(client, {"dry_run": True}, prov="nope")
    assert r.status == 400 and (await r.json())["code"] == "no_capability"
    r = await post(client, {"dry_run": True}, sid="..bad")
    assert r.status == 400 and (await r.json())["code"] == "bad_request"
    r = await client.post(URL.format(pid=PID, prov="claude", sid=T_SID), data=b"[1]",
                          headers={**client.hdr, "Content-Type": "application/json"})
    assert r.status == 400
    assert world.digest_all() == before


async def test_execute_without_a_token_is_400(client, world):
    before = world.digest_all()
    r = await post(client, {"dry_run": False})
    assert r.status == 400 and (await r.json())["code"] == "bad_request"
    assert world.digest_all() == before


async def test_busy_turn_is_409_and_foreign_slot_is_kept(client, world):
    token = await dry(client)
    before = world.digest_all()
    client.ctx["running"]["1001:42"] = True               # a turn is in flight
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 409 and (await r.json())["code"] == "busy"
    assert client.ctx["running"] == {"1001:42": True}      # not ours: must not be released
    assert world.digest_all() == before


async def test_busy_in_another_bound_chat_releases_the_slots_it_took(client, world):
    """A session bound to a free chat whose own slot is busy: the project's slot taken first is freed."""
    import webapp as w
    (world.data / "free_chats.json").write_text(json.dumps({"free-1": {"label": "f", "cwd": str(world.cwd),
                                                                       "session_id": T_SID}}))
    token = await dry(client)
    before = world.digest_all()
    client.ctx["running"]["free-1"] = True
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 409 and (await r.json())["code"] == "busy"
    assert client.ctx["running"] == {"free-1": True} and w
    assert world.digest_all() == before


async def test_live_subagents_is_409_and_slots_are_released(client, world, monkeypatch):
    token = await dry(client)
    before = world.digest_all()
    monkeypatch.setattr(webapp, "_has_live_agent_monitors", lambda k: True)
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 409 and (await r.json())["code"] == "subagents"
    assert client.ctx["running"] == {}
    assert world.digest_all() == before


async def test_a_foreign_writer_fd_is_409_writer(client, world, monkeypatch):
    monkeypatch.setattr(webapp.asyncio, "sleep", _fast_sleep)
    token = await dry(client)
    before = world.digest_all()
    with open(world.proj_dir / f"{T_SID}.jsonl", "ab") as fh:
        p = subprocess.Popen(["sleep", "30"], stdout=fh)
    try:
        r = await post(client, {"dry_run": False, "plan_token": token})
        assert r.status == 409 and (await r.json())["code"] == "writer"
        assert client.ctx["running"] == {}
        assert world.digest_all() == before
    finally:
        p.kill()
        p.wait()


async def test_a_foreign_claude_process_resuming_the_session_is_409(client, world, monkeypatch):
    monkeypatch.setattr(webapp.asyncio, "sleep", _fast_sleep)
    token = await dry(client)
    before = world.digest_all()
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "claude", "--resume", T_SID])
    try:
        r = await post(client, {"dry_run": False, "plan_token": token})
        assert r.status == 409 and (await r.json())["code"] == "writer"
        assert world.digest_all() == before
    finally:
        p.kill()
        p.wait()


async def _fast_sleep(_s):
    return None


async def test_a_new_prompt_since_the_preview_is_409_changed(client, world):
    token = await dry(client)
    with open(world.proj_dir / f"{T_SID}.jsonl", "ab") as f:      # the operator talked on
        f.write(j({"type": "user", "uuid": "u-3", "timestamp": "2026-11-18T10:00:00.000Z",
                   "message": {"role": "user", "content": "one more question"}}))
    before = world.digest_all()
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 409 and (await r.json())["code"] == "changed"
    assert world.digest_all() == before and client.ctx["running"] == {}


async def test_live_client_appending_ai_title_does_not_stale_the_token(client, world):
    token = await dry(client)
    with open(world.proj_dir / f"{T_SID}.jsonl", "ab") as f:      # the live CLI writes housekeeping lines
        f.write(j({"type": "ai-title", "aiTitle": "x", "sessionId": T_SID}))
        f.write(j({"type": "last-prompt", "lastPrompt": "y", "sessionId": T_SID}))
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 200, await r.text()


async def test_a_wrong_token_is_409_changed(client, world):
    before = world.digest_all()
    r = await post(client, {"dry_run": False, "plan_token": "0" * 64})
    assert r.status == 409 and (await r.json())["code"] == "changed"
    assert world.digest_all() == before


async def test_the_live_client_is_evicted_before_the_delete(client, world):
    evicted = []

    async def evict(key, ctx):
        evicted.append(key)
    client.ctx["evict_live_client"] = evict
    token = await dry(client)
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 200 and "1001:42" in evicted


async def test_an_engine_failure_is_500_and_the_journal_stays_for_resume(client, world, monkeypatch):
    token = await dry(client)

    def boom(self, ctx, art):
        raise OSError("disk on fire")
    monkeypatch.setattr(forget_claude.ClaudeAdapter, "delete", boom)
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 500 and (await r.json())["code"] == "failed"
    assert list((world.data / "forget_pending").glob("*.json"))
    assert client.ctx["running"] == {}
    assert SENT.encode() in (world.proj_dir / f"{T_SID}.jsonl").read_bytes()


async def test_unbound_old_session_of_the_project_deletes_under_the_project_key(client, world):
    """A session no chat points at is still deleted; the project's own slot guards the run."""
    chats = json.loads((world.data / "chats.json").read_text())
    chats[PID]["chats"][0]["session_id"] = "44444444-dddd-4ddd-8ddd-444444444444"
    (world.data / "chats.json").write_text(json.dumps(chats))
    client.ctx["sessions"].clear()
    token = await dry(client)
    r = await post(client, {"dry_run": False, "plan_token": token})
    assert r.status == 200
    assert not (world.proj_dir / f"{T_SID}.jsonl").exists()
    assert json.loads((world.data / "chats.json").read_text())[PID]["chats"][0]["session_id"] \
        == "44444444-dddd-4ddd-8ddd-444444444444"

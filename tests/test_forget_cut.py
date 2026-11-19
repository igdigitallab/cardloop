"""spec-098 B (P4): "forget from here" for Claude — cut the chat's current session at a turn mark.

The world is a 3-turn session bound to chat c3. Forgetting from turn 2 must leave the transcript
byte-for-byte equal to turn 1, remove only the artifacts the dropped turns own (their sub-agents,
tool-results and file-history backups the kept turn never referenced), drop only the dropped runs'
timeline/audit/trace lines, keep the session id and chat binding, and give turn 2's prompt back.
Every refusal leaves every store byte-identical.
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))

import forget
import forget_claude  # noqa: F401 - registers the adapter
import search as _search
import usage_scanner
import webapp
from _forget_world import K_SID, PID, T_SID, World, iso, j, local_iso

C_SID = "33333333-cccc-4ccc-8ccc-333333333333"
SK = "1001:42"
S1 = "keepturnonemarker5b2e"            # kept turn 1
S2 = "zqcutturntwosentinel88"           # dropped turn 2
S3 = "zqcutturnthreesentinel99"         # dropped turn 3
SHARED = "the quarterly harbour inspection schedule moved to thursday afternoon"   # in turn 1 AND 2
P1 = f"first prompt about {S1} and the lighthouse maintenance plan for spring"
P2_BODY = f"second prompt asks about {S2} and the ferry timetable for the islands"
R1 = f"Reply one: {S1} noted. Also, {SHARED}."
R2 = f"Reply two mentions {S2} while repeating that {SHARED}."
P3 = f"third prompt wants {S3} written into the harbour master's notes today"
R3 = f"Reply three confirms {S3} is recorded in the notes for the harbour master"


@pytest.fixture
def world(tmp_path, monkeypatch):
    forget._CRASH_AFTER = None
    w = World(tmp_path, monkeypatch)
    w.cut = _seed(w)
    webapp._recent_board_events.pop(SK, None)
    webapp._live_turns.pop(SK, None)
    yield w
    forget._CRASH_AFTER = None
    forget._CONFIG.clear()
    webapp._recent_board_events.pop(SK, None)
    webapp._live_turns.pop(SK, None)


def _turn(n: int, t: float, prompt: str, reply: str, *, tools=(), results=(), backups=(), steer=None) -> bytes:
    sid = C_SID
    lines = [
        j({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(t), "sessionId": sid, "content": prompt}),
        j({"type": "queue-operation", "operation": "dequeue", "timestamp": iso(t + 0.05), "sessionId": sid}),
        j({"type": "user", "uuid": f"u{n}", "timestamp": iso(t + 0.1), "sessionId": sid,
           "message": {"role": "user", "content": prompt}}),
        j({"type": "assistant", "uuid": f"a{n}", "timestamp": iso(t + 1), "sessionId": sid,
           "message": {"role": "assistant", "content": [{"type": "text", "text": reply}, *tools]}}),
    ]
    if steer:
        lines.append(j({"type": "attachment", "uuid": f"st{n}", "timestamp": iso(t + 1.2), "sessionId": sid,
                        "attachment": {"type": "queued_command", "prompt": steer}}))
    for tid, text in results:
        lines.append(j({"type": "user", "uuid": f"r{n}-{tid}", "timestamp": iso(t + 1.5), "sessionId": sid,
                        "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": tid, "content": text}]}}))
    lines.append(j({"type": "file-history-snapshot", "messageId": f"u{n}", "snapshot": {
        "messageId": f"u{n}", "trackedFileBackups": {f"f{b}.txt": {"backupFileName": b, "version": 1}
                                                     for b in backups}}, "isSnapshotUpdate": False}))
    lines.append(j({"type": "last-prompt", "lastPrompt": prompt, "sessionId": sid}))
    return b"".join(lines)


def _seed(w: World) -> dict:
    t0, d, h = w.t0 + 100, w.data, w.home
    media = d / "chat-media" / PID
    for name in ("turn2.png", "turn3.png"):
        (media / name).write_bytes(b"png-" + name.encode())
    p2 = P2_BODY + f"\nattached file: {d}/chat-media/{PID}/turn2.png"
    p3 = P3 + f"\nattached file: {d}/chat-media/{PID}/turn3.png"
    t1 = _turn(1, t0, P1, R1, tools=[{"type": "tool_use", "id": "toolu_k1", "name": "Task", "input": {"prompt": "dig"}}],
               results=[("toolu_k1", "output saved to tool-results/keepout01.txt")], backups=["kb@v1"])
    t2 = _turn(2, t0 + 10, p2, R2,
               tools=[{"type": "tool_use", "id": "toolu_d2", "name": "Task", "input": {"prompt": "dig more"}},
                      {"type": "tool_use", "id": "toolu_g2", "name": "Bash", "input": {"command": "git push origin master"}}],
               results=[("toolu_d2", f"output saved to tool-results/dropout02.txt and keepout01.txt {S2}"),
                        ("toolu_s2", SHARED)],          # a dropped item made only of kept text
               backups=["kb@v1", "db@v2"], steer=f"also mention {S2} in the steered note please, thanks")
    t3 = _turn(3, t0 + 20, p3, R3)
    tr = w.proj_dir / f"{C_SID}.jsonl"
    tr.write_bytes(t1 + t2 + t3)
    sd = w.proj_dir / C_SID
    (sd / "subagents").mkdir(parents=True)
    (sd / "tool-results").mkdir()
    (sd / "subagents" / "agent-k.jsonl").write_bytes(j(
        {"type": "assistant", "uuid": "sk-1", "timestamp": iso(t0 + 0.5),
         "message": {"role": "assistant", "content": [{"type": "text", "text": f"kept subagent on {S1} work items"}]}}))
    (sd / "subagents" / "agent-k.meta.json").write_text(json.dumps({"toolUseId": "toolu_k1"}))
    (sd / "subagents" / "agent-d.jsonl").write_bytes(j(
        {"type": "assistant", "uuid": "sd-1", "timestamp": iso(t0 + 10.5),
         "message": {"role": "assistant", "content": [{"type": "text", "text": f"dropped subagent says {S2} loudly"}]}}))
    (sd / "subagents" / "agent-d.meta.json").write_text(json.dumps({"toolUseId": "toolu_d2"}))
    (sd / "tool-results" / "keepout01.txt").write_text(f"kept tool output about {S1}\n")
    (sd / "tool-results" / "dropout02.txt").write_text(f"dropped tool output about {S2}\n")
    for cfg in (h / ".claude", h / ".claude-accounts" / "work"):
        fh = cfg / "file-history" / C_SID
        fh.mkdir(parents=True)
        (fh / "kb@v1").write_text("kept backup\n")
        (fh / "db@v2").write_text(f"dropped backup {S2}\n")
    off2, off3 = len(t1), len(t1) + len(t2)
    marks = [
        {"run_id": "r1", "chat_id": "c3", "session_key": SK, "source": "chat", "session_id": C_SID,
         "offset": 0, "ts_start": t0 - 0.5},
        {"run_id": "r1", "ts_end": t0 + 2},
        {"run_id": "r2", "chat_id": "c3", "session_key": SK, "source": "chat", "session_id": C_SID,
         "offset": off2, "ts_start": t0 + 9.5},
        {"run_id": "r2", "ts_end": t0 + 12},
        {"run_id": "r3", "chat_id": "c3", "session_key": SK, "source": "chat", "session_id": C_SID,
         "offset": off3, "ts_start": t0 + 19.5},
        {"run_id": "r3", "ts_end": t0 + 22},
    ]
    (d / "turn_marks" / f"{C_SID}.jsonl").write_bytes(b"".join(j(m) for m in marks))
    chats = json.loads((d / "chats.json").read_text())
    chats[PID]["chats"].append({"id": "c3", "name": "Third", "provider": "claude", "model": "opus",
                                "session_id": C_SID})
    (d / "chats.json").write_text(json.dumps(chats, indent=2))

    def ev(off, **kw):
        return j({"ts": t0 + off, "session_key": SK, "chat_id": "c3", **kw})
    tl_keep = [ev(-0.4, kind="run_start", prompt=P1, run_id="r1"), ev(1, kind="text", text=R1, run_id="r1"),
               ev(2, kind="run_end", run_id="r1", session_id=C_SID, provider="claude")]
    tl_drop = [ev(9.6, kind="run_start", prompt=P2_BODY, run_id="r2"), ev(11, kind="text", text=R2, run_id="r2"),
               ev(12, kind="run_end", run_id="r2", session_id=C_SID, provider="claude"),
               ev(19.6, kind="run_start", prompt=P3, run_id="r3"), ev(21, kind="text", text=R3, run_id="r3"),
               ev(22, kind="run_end", run_id="r3", session_id=C_SID, provider="claude")]
    with open(w.timeline_path, "ab") as f:
        f.write(b"".join(tl_keep + tl_drop))
    au_keep = [f"{local_iso(t0)} [{PID}] TASK run=r1: {P1}\n", f"{local_iso(t0 + 1)} [{PID}] RESULT run=r1: ok {S1}\n",
               f"{local_iso(t0 + 11)} [other] RESULT run=r2: another project's line\n"]
    au_drop = [f"{local_iso(t0 + 10)} [{PID}] TASK run=r2: {P2_BODY}\n",
               f"{local_iso(t0 + 11)} [{PID}] RESULT run=r2: done {S2}\n",
               f"{local_iso(t0 + 21)} [{PID}] RESULT run=r3: done {S3}\n"]
    with open(w.audit_path, "a", encoding="utf-8") as f:
        f.write("".join(au_keep + au_drop))
    trace = d / "chat-trace.jsonl"
    trace.write_bytes(b"".join([
        j({"ts": t0 - 0.6, "session_key": SK, "stage": "recv", "chat_id": "c3", "preview": P1[:120]}),
        j({"ts": t0 + 9.4, "session_key": SK, "stage": "recv", "chat_id": "c3", "preview": P2_BODY[:120]}),
        j({"ts": t0 + 19.4, "session_key": SK, "stage": "recv", "chat_id": "c3", "preview": P3[:120]}),
        j({"ts": t0 + 15, "session_key": SK, "stage": "recv", "chat_id": "c1", "preview": "another chat's message"}),
    ]))
    conn = _search.get_db(d / "search.db")
    _search.index_transcripts(conn, PID, PID, w.proj_dir)
    _search.record_hits(conn, "ui", [{"ref": C_SID, "project_id": PID, "source": "chat"}])
    conn.close()
    usage_scanner.scan(projects_dir=h / ".claude" / "projects", db_path=d / "usage.db")
    return {"t0": t0, "t1": t1, "t2": t2, "t3": t3, "off2": off2, "off3": off3, "transcript": tr,
            "tl_keep": tl_keep, "tl_drop": tl_drop, "au_keep": au_keep, "au_drop": au_drop, "p2": p2}


async def cut_plan(world, anchor="u2", ctx=None):
    ctx = ctx or world.ctx()
    env = webapp._forget_env(ctx)
    adapter = forget.adapter("claude")
    bound = env.state.bound("claude", C_SID)
    keys = {SK, *[b["session_key"] for b in bound]}
    build = webapp._forget_cut_builder(ctx, world.project(), adapter, "c3", C_SID, anchor)
    plan = await asyncio.get_running_loop().run_in_executor(None, lambda: build(env, keys, bound))
    return ctx, env, adapter, plan


async def cut_it(world, anchor="u2"):
    ctx, env, adapter, plan = await cut_plan(world, anchor)
    counts = await forget.execute_plan(env, adapter, ctx, plan)
    return ctx, env, adapter, plan, counts


def _raw_has(world, token: str) -> "list[str]":
    return world.sentinel_paths(token)


# ───────────────────────────── the cut itself ───────────────────────────────────────────────
async def test_forget_from_turn_two_leaves_turn_one_byte_for_byte(world):
    c = world.cut
    other = {p: b for p, b in world.files().items() if T_SID in p or K_SID in p}
    ctx, env, adapter, plan, counts = await cut_it(world)
    assert plan.mode == "cut" and plan.run_ids == ["r2", "r3"]
    assert c["transcript"].read_bytes() == c["t1"]
    assert counts["turns"] == 2
    for tok in (S2, S3):
        assert _raw_has(world, tok) == [], tok
    # the session, its binding and every other session stay
    chats = json.loads((world.data / "chats.json").read_text())
    assert next(ch for ch in chats[PID]["chats"] if ch["id"] == "c3")["session_id"] == C_SID
    assert {p: b for p, b in world.files().items() if T_SID in p or K_SID in p} == other
    marks = [json.loads(x) for x in (world.data / "turn_marks" / f"{C_SID}.jsonl").read_text().splitlines()]
    assert {m["run_id"] for m in marks} == {"r1"}


async def test_only_the_dropped_turns_artifacts_go(world):
    sd = world.proj_dir / C_SID
    await cut_it(world)
    assert (sd / "subagents" / "agent-k.jsonl").exists() and (sd / "subagents" / "agent-k.meta.json").exists()
    assert not (sd / "subagents" / "agent-d.jsonl").exists() and not (sd / "subagents" / "agent-d.meta.json").exists()
    assert (sd / "tool-results" / "keepout01.txt").exists()          # also named by turn 2: turn 1 needs it
    assert not (sd / "tool-results" / "dropout02.txt").exists()
    for cfg in (world.home / ".claude", world.home / ".claude-accounts" / "work"):
        assert (cfg / "file-history" / C_SID / "kb@v1").exists()
        assert not (cfg / "file-history" / C_SID / "db@v2").exists()


async def test_logs_lose_only_the_dropped_runs(world):
    c = world.cut
    tl_before = world.timeline_path.read_bytes()
    au_before = world.audit_path.read_text(encoding="utf-8")
    await cut_it(world)
    tl = world.timeline_path.read_bytes()
    assert tl == b"".join(x for x in tl_before.splitlines(keepends=True) if x not in c["tl_drop"])
    au = world.audit_path.read_text(encoding="utf-8")
    assert au == "".join(x for x in au_before.splitlines(keepends=True) if x not in c["au_drop"])
    assert "[other] RESULT run=r2" in au                    # another project's line with the same id
    trace = [json.loads(x) for x in (world.data / "chat-trace.jsonl").read_text().splitlines()]
    assert [t["preview"] for t in trace] == [P1[:120], "another chat's message"]


async def test_restored_prompt_keeps_its_attachment_and_later_uploads_go(world):
    ctx, env, adapter, plan, counts = await cut_it(world)
    assert plan.restored_prompt.startswith(P2_BODY)
    assert "turn2.png" in plan.restored_prompt
    media = world.data / "chat-media" / PID
    assert (media / "turn2.png").exists()
    assert not (media / "turn3.png").exists()
    assert (media / "shared.png").exists()


async def test_search_and_usage_follow_the_cut(world):
    c = world.cut
    await cut_it(world)
    raw = b"".join((world.data / n).read_bytes() for n in ("search.db",) if (world.data / n).exists())
    assert S2.encode() not in raw and S3.encode() not in raw
    conn = _search.get_db(world.data / "search.db")
    try:
        _search.index_transcripts(conn, PID, PID, world.proj_dir)        # the next indexer sweep
        hits = conn.execute("SELECT count(*) FROM docs WHERE docs MATCH ?", (S1,)).fetchone()[0]
        gone = conn.execute("SELECT count(*) FROM docs WHERE docs MATCH ?", (S2,)).fetchone()[0]
        kept_hits = conn.execute("SELECT count(*) FROM doc_hits WHERE ref = ?", (C_SID,)).fetchone()[0]
    finally:
        conn.close()
    assert hits >= 1 and gone == 0 and kept_hits == 1
    u = sqlite3.connect(world.data / "usage.db")
    try:
        lines = u.execute("SELECT lines FROM processed_files WHERE path = ?", (str(c["transcript"]),)).fetchone()[0]
    finally:
        u.close()
    assert lines == c["t1"].count(b"\n")


async def test_residue_windows_skip_text_the_kept_turn_also_holds(world):
    ctx, env, adapter, plan = await cut_plan(world)
    assert plan.held.windows and not any(w in SHARED or w in R1 or w in P1 for w in plan.held.windows)
    await forget.execute_plan(env, adapter, ctx, plan)
    res = await forget.residue_scan(world.data, plan.held.windows, [world.proj_dir, world.data], home=world.home)
    assert res["hits"] == [], res


async def test_steered_row_anchors_the_same_cut(world):
    _ctx, _env, _ad, plan = await cut_plan(world, anchor="st2")
    assert plan.mode == "cut" and plan.run_ids == ["r2", "r3"] and plan.art.extra["cut"] == world.cut["off2"]


async def test_anchor_in_the_first_turn_is_the_whole_session(world):
    _ctx, _env, _ad, plan = await cut_plan(world, anchor="u1")
    assert plan.mode == "session" and plan.restored_prompt.startswith(P1)


async def test_token_changes_when_a_turn_ran_since_the_preview(world):
    _c, _e, _a, plan = await cut_plan(world)
    with open(world.data / "turn_marks" / f"{C_SID}.jsonl", "ab") as f:
        f.write(j({"run_id": "r4", "session_id": C_SID, "offset": world.cut["transcript"].stat().st_size,
                   "ts_start": time.time()}))
    _c, _e, _a, plan2 = await cut_plan(world)
    assert plan2.token != plan.token and plan2.run_ids == ["r2", "r3", "r4"]


# ───────────────────────────── refusals: nothing touched ────────────────────────────────────
async def _refused(world, anchor, code):
    before = world.digest_all()
    with pytest.raises(forget.ForgetError) as ei:
        await cut_plan(world, anchor)
    assert ei.value.code == code
    assert world.digest_all() == before


async def test_anchor_from_another_session_is_a_predecessor(world):
    await _refused(world, "u-1", "predecessor")         # u-1 lives in T_SID, not in c3's session


async def test_session_without_marks_cannot_be_cut(world):
    (world.data / "turn_marks" / f"{C_SID}.jsonl").unlink()
    await _refused(world, "u2", "no_mark")


async def test_a_later_run_without_a_mark_is_not_dragged_in(world):
    """r3 left no mark: its anchor would map to r2, a turn the operator did not pick."""
    mf = world.data / "turn_marks" / f"{C_SID}.jsonl"
    rows = [json.loads(x) for x in mf.read_text().splitlines()]
    mf.write_bytes(b"".join(j(r) for r in rows if r["run_id"] != "r3"))
    await _refused(world, "u3", "no_mark")


async def test_unfinished_run_falls_back_to_counting_turn_starts(world):
    mf = world.data / "turn_marks" / f"{C_SID}.jsonl"
    rows = [json.loads(x) for x in mf.read_text().splitlines()]
    mf.write_bytes(b"".join(j(r) for r in rows if r["run_id"] != "r3" and not ("ts_end" in r and r["run_id"] == "r2")))
    await _refused(world, "u3", "no_mark")
    _c, _e, _a, plan = await cut_plan(world, anchor="st2")    # a steered row inside r2 still maps to r2
    assert plan.run_ids == ["r2"]


async def test_a_mark_that_is_not_a_line_start_is_refused(world):
    mf = world.data / "turn_marks" / f"{C_SID}.jsonl"
    rows = [json.loads(x) for x in mf.read_text().splitlines()]
    for r in rows:
        if r.get("run_id") == "r2" and "offset" in r:
            r["offset"] += 5
    mf.write_bytes(b"".join(j(r) for r in rows))
    await _refused(world, "u2", "no_mark")


async def test_transcript_changed_after_the_plan_is_not_cut(world):
    ctx, env, adapter, plan = await cut_plan(world)
    with open(world.cut["transcript"], "ab") as f:
        f.write(j({"type": "ai-title", "aiTitle": "late", "sessionId": C_SID}))
    before = world.cut["transcript"].read_bytes()
    with pytest.raises(forget.ForgetError) as ei:
        await forget.execute_plan(env, adapter, ctx, plan)
    assert ei.value.code == "changed"
    assert world.cut["transcript"].read_bytes() == before


async def test_a_cut_that_does_not_verify_is_undone(world, monkeypatch):
    ctx, env, adapter, plan = await cut_plan(world)
    before = world.cut["transcript"].read_bytes()
    monkeypatch.setattr(forget_claude, "_last_line_parses", lambda path, size: False)
    with pytest.raises(forget.ForgetError) as ei:
        await forget.execute_plan(env, adapter, ctx, plan)
    assert ei.value.code == "failed"
    assert world.cut["transcript"].read_bytes() == before


# ───────────────────────────── crash safety ─────────────────────────────────────────────────
@pytest.mark.parametrize("step", [4, 7, 8])
async def test_crash_then_resume_reaches_the_same_state(world, step):
    ctx, env, adapter, plan = await cut_plan(world)
    forget._CRASH_AFTER = step
    with pytest.raises(forget.SimulatedCrash):
        await forget.execute_plan(env, adapter, ctx, plan)
    forget._CRASH_AFTER = None
    forget.configure(env_factory=webapp._forget_env)
    done = await forget.resume_pending(ctx)
    assert done == [plan.token]
    assert world.cut["transcript"].read_bytes() == world.cut["t1"]
    for tok in (S2, S3):
        assert _raw_has(world, tok) == [], (step, tok)
    assert not list((world.data / "forget_pending").glob("*.json"))


async def test_resume_never_cuts_a_transcript_that_grew_after_a_crash(world):
    ctx, env, adapter, plan = await cut_plan(world)
    forget._CRASH_AFTER = 3
    with pytest.raises(forget.SimulatedCrash):
        await forget.execute_plan(env, adapter, ctx, plan)
    forget._CRASH_AFTER = None
    with open(world.cut["transcript"], "ab") as f:                 # a turn ran before the resume
        f.write(j({"type": "user", "uuid": "u9", "message": {"role": "user", "content": "new turn"}}))
    before = world.cut["transcript"].read_bytes()
    forget.configure(env_factory=webapp._forget_env)
    assert await forget.resume_pending(ctx) == []
    assert world.cut["transcript"].read_bytes() == before
    assert not list((world.data / "forget_pending").glob("*.json"))
    assert "could not be finished" in (world.data / "inbox" / f"forget-{C_SID[:8]}.txt").read_text()


async def test_pending_record_holds_no_text(world):
    _c, _e, _a, plan = await cut_plan(world)
    rec = json.dumps(plan.record())
    for s in (S2, S3, P2_BODY[:40], R2[:40]):
        assert s not in rec


# ───────────────────────────── endpoint B ───────────────────────────────────────────────────
URL = "/api/projects/{pid}/chats/{chat}/forget"


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
    app.router.add_post("/api/projects/{id}/chats/{chat_id}/forget", webapp.api_chat_forget)
    app.router.add_get("/api/projects/{id}/session-history", webapp.api_project_session_history)
    c = await aiohttp_client(app)
    c.ctx = ctx
    c.hdr = {"Cookie": f"cops_auth={ctx['_auth_token']}"}
    return c


async def bpost(client, body, chat="c3", **kw):
    return await client.post(URL.format(pid=PID, chat=chat), json=body, headers=client.hdr, **kw)


async def test_endpoint_dry_run_then_cut(client, world, events):
    before = world.digest_all()
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": True})
    body = await r.json()
    assert r.status == 200, body
    p = body["plan"]
    assert p["whole_session"] is False and p["turns"] == 2 and p["chat_id"] == "c3"
    assert p["restored_prompt"].startswith(P2_BODY)
    assert any(se["kind"] == "git" for se in p["side_effects"])
    assert world.digest_all() == before
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": False, "plan_token": body["plan_token"]})
    out = await r.json()
    assert r.status == 200, out
    assert out["whole_session"] is False and out["forgotten"]["turns"] == 2
    assert out["restored_prompt"].startswith(P2_BODY)
    assert world.cut["transcript"].read_bytes() == world.cut["t1"]
    kinds = [e["kind"] for _k, e, persist in events if not persist]
    assert "chat_forgotten" in kinds and "session_deleted" not in kinds
    ev = next(e for _k, e, _p in events if e["kind"] == "chat_forgotten")
    assert ev["chat_id"] == "c3" and ev["session_id"] == C_SID and ev["cut_ts"] == pytest.approx(world.cut["t0"] + 9.5)


async def test_endpoint_first_turn_deletes_the_whole_session(client, world, events):
    r = await bpost(client, {"anchor_uuid": "u1", "dry_run": True})
    body = await r.json()
    assert r.status == 200 and body["plan"]["whole_session"] is True
    r = await bpost(client, {"anchor_uuid": "u1", "dry_run": False, "plan_token": body["plan_token"]})
    out = await r.json()
    assert r.status == 200 and out["whole_session"] is True and out["restored_prompt"].startswith(P1)
    assert not world.cut["transcript"].exists()
    assert any(e["kind"] == "session_deleted" for _k, e, _p in events)
    chats = json.loads((world.data / "chats.json").read_text())
    assert next(ch for ch in chats[PID]["chats"] if ch["id"] == "c3")["session_id"] is None


async def test_endpoint_stale_token_is_409_and_touches_nothing(client, world):
    before = world.digest_all()
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": False, "plan_token": "0" * 64})
    assert r.status == 409 and (await r.json())["code"] == "changed"
    assert world.digest_all() == before


async def test_endpoint_refusals(client, world):
    r = await client.post(URL.format(pid=PID, chat="c3"), data="anchor_uuid=u2", headers=client.hdr)
    assert r.status == 415
    assert (await bpost(client, {"anchor_uuid": "../../x", "dry_run": True})).status == 400
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": True}, chat="nochat")
    assert r.status == 404
    r = await bpost(client, {"anchor_uuid": "u-1", "dry_run": True})
    assert r.status == 400 and (await r.json())["code"] == "predecessor"
    client.ctx["running"][SK] = True
    tok = (await (await bpost(client, {"anchor_uuid": "u2", "dry_run": True})).json())["plan_token"]
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": False, "plan_token": tok})
    assert r.status == 409 and (await r.json())["code"] == "busy"
    client.ctx["running"].pop(SK, None)


async def test_endpoint_codex_chat_has_no_capability(client, world):
    chats = json.loads((world.data / "chats.json").read_text())
    chats[PID]["chats"].append({"id": "cx", "name": "Codex", "provider": "codex", "codex_thread_id": "t" * 12})
    (world.data / "chats.json").write_text(json.dumps(chats))
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": True}, chat="cx")
    assert r.status == 400 and (await r.json())["code"] == "no_capability"


async def test_endpoint_disabled(client, world, monkeypatch):
    monkeypatch.setenv("FORGET_ENABLED", "0")
    r = await bpost(client, {"anchor_uuid": "u2", "dry_run": True})
    assert r.status == 404 and (await r.json())["code"] == "disabled"


async def test_history_reports_where_forgetting_starts(client, world):
    r = await client.get(f"/api/projects/{PID}/session-history?session_id={C_SID}", headers=client.hdr)
    body = await r.json()
    assert body["forget_from_ms"] == int((world.cut["t0"] - 0.5) * 1000)
    r = await client.get(f"/api/projects/{PID}/session-history?session_id={K_SID}", headers=client.hdr)
    assert (await r.json())["forget_from_ms"] is None


async def test_side_stores_lose_only_what_the_dropped_turns_made(world):
    """Armed runtime handoff, fired deferred prompts, plan sidecars, the runtime rings: by the cut's
    time span and the dropped prompts, never the kept turn's."""
    from datetime import datetime, timezone
    t0 = world.cut["t0"]
    chats = json.loads((world.data / "chats.json").read_text())
    c3 = next(ch for ch in chats[PID]["chats"] if ch["id"] == "c3")
    c3["runtime_handoff"] = {"text": "summary with no distinctive strings", "created_at": t0 + 15,
                             "from_provider": "claude", "from_session": C_SID}
    c2 = next(ch for ch in chats[PID]["chats"] if ch["id"] == "c2")
    c2["runtime_handoff"] = {"text": "older summary of turn one only", "created_at": t0 + 3,
                             "from_provider": "claude", "from_session": C_SID}
    (world.data / "chats.json").write_text(json.dumps(chats))

    def at(x):
        return datetime.fromtimestamp(x, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    (world.data / "deferred.json").write_text(json.dumps([
        {"id": "d-keep", "session_key": SK, "prompt": P1, "status": "fired", "fired_at": at(t0 - 0.4)},
        {"id": "d-drop", "session_key": SK, "status": "fired", "fired_at": at(t0 + 19.6),
         "prompt": P3 + f"\nattached file: {world.data}/chat-media/{PID}/turn3.png"},   # as the CLI got it
        {"id": "d-pending", "session_key": SK, "prompt": P3, "status": "pending", "created": at(t0 + 19.6)}]))
    pdir = world.data / "plans"
    pdir.mkdir(exist_ok=True)
    (pdir / "plankeep1.json").write_text(json.dumps({"id": "plankeep1", "session_key": SK, "chat_id": "c3",
                                                     "created_at": t0 + 1}))
    (pdir / "plandrop2.json").write_text(json.dumps({"id": "plandrop2", "session_key": SK, "chat_id": "c3",
                                                     "created_at": t0 + 11}))
    webapp._live_turns[SK] = {"status": "done", "events": []}
    await cut_it(world)
    chats = json.loads((world.data / "chats.json").read_text())
    assert "runtime_handoff" not in next(ch for ch in chats[PID]["chats"] if ch["id"] == "c3")
    assert "runtime_handoff" in next(ch for ch in chats[PID]["chats"] if ch["id"] == "c2")
    assert [r["id"] for r in json.loads((world.data / "deferred.json").read_text())] == ["d-keep", "d-pending"]
    assert (pdir / "plankeep1.json").exists() and not (pdir / "plandrop2.json").exists()
    assert SK not in webapp._live_turns

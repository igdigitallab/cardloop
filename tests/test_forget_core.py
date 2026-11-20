"""spec-098 Forget: the purge engine on a synthetic world (acceptance 1, 2, 4, 6, 7).

Everything runs against `tests/_forget_world.py` (tmp HOME + tmp DATA). A sentinel that exists only
in the deleted session must be gone from the RAW BYTES of every store after execute.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import forget
import forget_claude  # noqa: F401 - registers the adapter
import webapp
from _forget_world import (K_PROMPT, K_REPLY, K_SID, PID, SENT, T_SID, TEXT_PROMPT, TEXT_REPLY, TEXT_TOOL, World, j,
                           local_iso)

LOOP_KEY = "1001:42"


@pytest.fixture
def world(tmp_path, monkeypatch):
    forget._CRASH_AFTER = None
    w = World(tmp_path, monkeypatch)
    webapp._recent_board_events.pop(LOOP_KEY, None)
    webapp._live_turns.pop(LOOP_KEY, None)
    yield w
    forget._CRASH_AFTER = None
    webapp._recent_board_events.pop(LOOP_KEY, None)
    webapp._live_turns.pop(LOOP_KEY, None)
    forget._CONFIG.clear()


async def plan_for(world, ctx=None, sid=T_SID):
    ctx = ctx or world.ctx()
    env = webapp._forget_env(ctx)
    adapter = forget.adapter("claude")
    keys = {LOOP_KEY}
    files = webapp._forget_timeline_files(ctx, world.project(), keys)
    bound = env.state.bound("claude", sid)
    loop = asyncio.get_running_loop()
    plan = await loop.run_in_executor(
        None, lambda: forget.build_plan(env, adapter, ctx, world.project(), sid, timeline_files=files,
                                        session_key=LOOP_KEY, bound=bound))
    return ctx, env, adapter, plan


async def forget_it(world):
    ctx, env, adapter, plan = await plan_for(world)
    counts = await forget.execute_plan(env, adapter, ctx, plan)
    return ctx, env, adapter, plan, counts


def seed_runtime_state():
    webapp._recent_board_events[LOOP_KEY] = [{"ts": 9e12, "summary": "x"}]
    webapp._live_turns[LOOP_KEY] = {"status": "done", "events": []}


def kept_lines(path_lines):
    return b"".join(path_lines)


def check_world(world, before, ctx) -> "list[str]":
    """Every way the world can still hold the deleted session. [] = fully forgotten and nothing
    else touched. The mutation test requires this to flag each disabled step."""
    h, d = world.home, world.data
    bad: list = []
    now = world.files()
    leaked = world.sentinel_paths()
    if leaked:
        bad.append(f"sentinel still in {[Path(p).name for p in leaked]}")
    gone = [
        world.proj_dir / f"{T_SID}.jsonl", world.proj_dir / T_SID,
        h / ".claude" / "file-history" / T_SID, h / ".claude-accounts" / "work" / "file-history" / T_SID,
        h / ".claude" / "session-env" / T_SID, h / ".claude-accounts" / "work" / "session-env" / T_SID,
        h / ".claude" / "debug" / f"{T_SID}.txt", d / "turn_marks" / f"{T_SID}.jsonl",
        d / "chat-media" / PID / "shot-1.png",
    ]
    bad += [f"still exists: {p.name}" for p in gone if p.exists()]
    # everything not expected to change must be byte-identical
    rewritten = {world.timeline_path, world.audit_path, d / "chats.json", d / "session_labels.json",
                 d / "session_links.json", d / "session_lineage.jsonl", d / "search.db", d / "search.db-wal",
                 d / "search.db-shm", d / "usage.db", d / "usage.db-wal", d / "usage.db-shm",
                 h / ".claude" / "history.jsonl", h / ".claude-accounts" / "work" / "history.jsonl"}
    gone_prefixes = tuple(str(p) for p in gone)
    import hashlib
    for p, dig in before.items():
        pp = Path(p)
        if pp in rewritten or p.startswith(gone_prefixes) or "forget_pending" in p or "/inbox/" in p:
            continue
        if p in now and hashlib.sha256(now[p]).hexdigest() != dig:
            bad.append(f"kept file changed: {pp.name}")
    # rewritten files: exactly the kept lines, in order
    stop = j({"ts": world.t0 + 40.0, "session_key": "1001:42", "kind": "operator_stop"})
    if world.timeline_path.read_bytes() != b"".join(world.tl_kept) + stop:
        bad.append("timeline not exactly the kept lines")
    if world.audit_path.read_text() != "".join(world.au_kept):
        bad.append("audit not exactly the kept lines")
    for cfg in (h / ".claude", h / ".claude-accounts" / "work"):
        hist = (cfg / "history.jsonl").read_bytes()
        if T_SID.encode() in hist or K_SID.encode() not in hist:
            bad.append(f"history.jsonl wrong in {cfg.name}")
    labels = json.loads((d / "session_labels.json").read_text())
    if T_SID in labels or K_SID not in labels:
        bad.append("session_labels")
    links = json.loads((d / "session_links.json").read_text())
    if T_SID in links or K_SID not in links:
        bad.append("session_links")
    lin = (d / "session_lineage.jsonl").read_text()
    if T_SID in lin or K_SID not in lin:
        bad.append("session_lineage")
    chats = json.loads((d / "chats.json").read_text())[PID]["chats"]
    c1, c2 = chats
    if c1.get("session_id") is not None or "runtime_handoff" in c1:
        bad.append("chat c1 still bound / armed handoff kept")
    if c2.get("session_id") != K_SID:
        bad.append("chat c2 binding changed")
    if ctx["sessions"].get(LOOP_KEY) == T_SID:
        bad.append("flat session mirror still names the session")
    if webapp._recent_board_events.get(LOOP_KEY) or webapp._live_turns.get(LOOP_KEY):
        bad.append("in-memory rings not cleared")
    # search.db: target rows and doc_hits gone, kept rows still there
    conn = sqlite3.connect(d / "search.db")
    try:
        n_t = conn.execute("SELECT count(*) FROM doc_rows WHERE path LIKE ?", (f"%{T_SID}%",)).fetchone()[0]
        n_k = conn.execute("SELECT count(*) FROM doc_rows WHERE path LIKE ?", (f"%{K_SID}%",)).fetchone()[0]
        hits = {r[0] for r in conn.execute("SELECT ref FROM doc_hits WHERE source='chat'")}
        fs = {r[0] for r in conn.execute("SELECT path FROM file_state")}
        kept_text = conn.execute("SELECT count(*) FROM docs WHERE docs MATCH 'quarterly'").fetchone()[0]
    finally:
        conn.close()
    if n_t:
        bad.append("search rows of the session remain")
    if not n_k or not kept_text:
        bad.append("kept session's search rows damaged")
    if T_SID in hits or K_SID not in hits:
        bad.append("doc_hits")
    if any(T_SID in p for p in fs):
        bad.append("search file_state of the transcript remains")
    u = sqlite3.connect(d / "usage.db")
    try:
        pf = [r[0] for r in u.execute("SELECT path FROM processed_files")]
    finally:
        u.close()
    if any(T_SID in p for p in pf):
        bad.append("usage processed_files rows remain")
    if (d / "forget_pending").exists() and list((d / "forget_pending").glob("*.json")):
        bad.append("pending record left behind")
    return bad


# ───────────────────────────── acceptance 1 ─────────────────────────────────────────────────
async def test_sentinel_gone_everywhere_and_kept_content_identical(world):
    before = world.digest_all()
    numbers = world.usage_numbers()
    paths = world.sentinel_paths()
    names = {Path(p).name for p in paths}
    assert "search.db" in names, "fixture must put the sentinel into the search index"
    assert f"{T_SID}.jsonl" in names and "chats.json" in names and "history.jsonl" in names
    seed_runtime_state()
    ctx, env, adapter, plan, counts = await forget_it(world)
    assert check_world(world, before, ctx) == []
    assert world.usage_numbers() == numbers          # spend stays: numbers only
    assert counts["files"] > 0 and counts["timeline_lines"] == 4 and counts["audit_lines"] == 2
    assert counts["search_rows"] > 0 and counts["media"] == 1
    # the shared upload (named in a kept transcript) survives
    assert (world.data / "chat-media" / PID / "shared.png").exists()
    assert (world.data / "chat-media" / PID / "other.png").exists()


async def test_raw_bytes_of_wal_are_clean_too(world):
    """The -wal file counts as a store: nothing may be left in it after the checkpoint."""
    await forget_it(world)
    for suffix in ("search.db-wal", "search.db-shm", "search.db"):
        p = world.data / suffix
        if p.exists():
            assert SENT.encode() not in p.read_bytes(), suffix


async def test_plan_view_matches_contract_shape(world):
    ctx, env, adapter, plan = await plan_for(world)
    v = forget.plan_view(plan)
    assert v["provider"] == "claude" and v["session_id"] == T_SID
    assert v["turns"] == 1 and v["title"] == "target label"
    assert v["active_in_chats"] == ["c1"] and v["files"] >= 6 and v["bytes"] > 0
    kinds = {s["kind"] for s in v["side_effects"]}
    assert {"file_write", "git"} <= kinds
    assert any("Anthropic" in s for s in v["limits"]) and len(v["limits"]) == 4
    assert len(plan.token) == 64
    json.dumps(v)


async def test_pending_journal_holds_ids_only(world):
    forget._CRASH_AFTER = 3
    with pytest.raises(forget.SimulatedCrash):
        await forget_it(world)
    recs = list((world.data / "forget_pending").glob("*.json"))
    assert len(recs) == 1
    raw = recs[0].read_bytes()
    assert SENT.encode() not in raw and TEXT_PROMPT.encode() not in raw
    assert b"delete me session" not in raw.lower()      # not even the title


# ───────────────────────────── acceptance 2: mutation check ─────────────────────────────────
def _noop(*a, **k):
    return 0


async def _anoop(*a, **k):
    return 0


MUTATIONS = {
    "adapter_delete": (forget_claude.ClaudeAdapter, "delete", lambda s, c, a: {"files": 0}),
    "adapter_verify": None,  # covered together with delete below
    "history_rewrite": (forget_claude.ClaudeAdapter, "_rewrite_history", staticmethod(lambda p, s: 0)),
    "unbind": (webapp._ForgetState, "unbind", _anoop),
    "labels_links": (webapp._ForgetState, "drop_labels_links", lambda s, sid: None),
    "armed_handoff": (webapp._ForgetState, "drop_handoffs", _anoop),
    "runtime_rings": (webapp._ForgetState, "clear_runtime", lambda s, a, b: None),
    "lineage": (forget, "remove_lineage", lambda d, s: 0),
    "media": (forget, "plan_media", lambda *a, **k: []),
    "timeline": (forget, "select_timeline_lines", lambda *a, **k: {"digests": [], "unattributed": 0, "runs": []}),
    "audit": (forget, "select_audit_lines", lambda *a, **k: {"digests": [], "unattributed": 0}),
    "search": (forget, "purge_search", lambda *a, **k: {"rows": 0, "hits": 0, "checkpoint": "skipped"}),
    "usage": (forget, "purge_usage", _noop),
    "turn_marks": (forget, "turn_marks_path", lambda d, s: Path(d) / "nope" / "x.jsonl"),
}


@pytest.mark.parametrize("step", [k for k, v in MUTATIONS.items() if v])
async def test_mutation_disabling_one_step_is_caught(world, monkeypatch, step):
    """Acceptance 2: with ONE purge step switched off, the acceptance check must go red."""
    before = world.digest_all()
    seed_runtime_state()
    target, name, repl = MUTATIONS[step]
    monkeypatch.setattr(target, name, repl)
    if step == "adapter_delete":
        monkeypatch.setattr(forget_claude.ClaudeAdapter, "verify_gone", lambda s, c, a: [])
    try:
        ctx, env, adapter, plan, counts = await forget_it(world)
    except forget.ForgetError:
        return                      # the engine's own verify_gone refused: caught at the source
    assert check_world(world, before, ctx) != [], f"disabling {step} went unnoticed"


async def test_secure_delete_is_what_removes_the_index_tokens(world, monkeypatch):
    """Without FTS5 secure-delete the sentinel tokens would linger in the index; the engine's
    purge must leave none even when the table is big enough to have segments."""
    conn = sqlite3.connect(world.data / "search.db")
    for i in range(400):
        conn.execute("INSERT INTO docs (project_id, project_name, source, ts, ref, ref2, path, line, tier, title, body)"
                     " VALUES ('p','p','chat',0,'r','','/x/other.jsonl',0,'','',?)", (f"filler words number {i} alpha beta",))
    conn.commit()
    conn.close()
    await forget_it(world)
    for suffix in ("search.db", "search.db-wal"):
        p = world.data / suffix
        if p.exists():
            assert SENT.encode() not in p.read_bytes()


# ───────────────────────────── acceptance 4: crash safety ───────────────────────────────────
@pytest.mark.parametrize("n", [3, 4, 5, 6, 7, 8, 9])
async def test_crash_then_resume_reaches_the_same_final_state(world, n):
    before = world.digest_all()
    seed_runtime_state()
    forget._CRASH_AFTER = n
    with pytest.raises(forget.SimulatedCrash):
        await forget_it(world)
    forget._CRASH_AFTER = None
    assert list((world.data / "forget_pending").glob("*.json")), "journal must survive a crash"
    ctx = world.ctx()
    forget.configure(env_factory=webapp._forget_env)
    done = await forget.resume_pending(ctx)
    assert len(done) == 1
    # the crash happened in-process, so the flat mirror / in-memory rings follow the chats.json truth
    ctx["sessions"].pop(LOOP_KEY, None)
    assert check_world(world, before, ctx) == []


async def test_resume_is_idempotent_and_ignores_unknown_provider(world):
    forget.configure(env_factory=webapp._forget_env)
    d = world.data / "forget_pending"
    d.mkdir()
    (d / "x.json").write_text(json.dumps({"token": "x", "provider": "nope", "session_id": "s"}))
    (d / "bad.json").write_text("{not json")
    assert await forget.resume_pending(world.ctx()) == []


# ───────────────────────────── acceptance 6: old runs, interleaved chats ────────────────────
async def test_old_runs_without_ids_only_the_target_chats_lines_go(world):
    t0 = world.t0
    sk, other = "1001:42", "free-zzz"

    def ev(off, key=sk, **kw):
        return j({"ts": t0 + off, "session_key": key, **kw})

    keep, drop = [], []
    seq = [
        (drop, ev(0.3, kind="run_start", source="chat", prompt=TEXT_PROMPT)),            # target chat, no id
        (drop, ev(1.0, kind="tool_result", tool="Bash", output="t1")),
        (keep, ev(1.5, key=other, kind="text", text="another session_key interleaved")),  # unattributable
        (drop, ev(2.0, kind="text", text="t reply")),
        (drop, ev(2.5, kind="run_end", source="chat", outcome="ok")),
        (keep, ev(30.3, kind="run_start", source="chat", prompt=K_PROMPT)),               # other chat, no id
        (keep, ev(31.0, kind="tool_result", tool="Bash", output="k1")),
        (keep, ev(32.0, kind="text", text=K_REPLY)),
        (keep, ev(32.5, kind="run_end", source="chat", outcome="ok")),
        (drop, ev(33.0, kind="steer", text="x")) if False else (keep, ev(33.0, kind="operator_stop")),
    ]
    world.timeline_path.write_bytes(b"".join(b for _l, b in seq))
    ctx, env, adapter, plan = await plan_for(world)
    assert plan.unattributed == 1
    n = sum(len(v) for v in plan.timeline.values())
    assert n == len([1 for lst, _b in seq if lst is drop])
    await forget.execute_plan(env, adapter, ctx, plan)
    assert world.timeline_path.read_bytes() == b"".join(b for lst, b in seq if lst is keep)


async def test_run_start_outside_the_120s_window_is_not_attributed(world):
    t0 = world.t0
    line = j({"ts": t0 + 500, "session_key": LOOP_KEY, "kind": "run_start", "source": "chat", "prompt": TEXT_PROMPT})
    world.timeline_path.write_bytes(line)
    _ctx, _env, _a, plan = await plan_for(world)
    assert plan.timeline == {}


async def test_known_run_ids_from_run_end_session_id_and_marks(world):
    t0 = world.t0
    mk = lambda off, **kw: j({"ts": t0 + off, "session_key": LOOP_KEY, **kw})  # noqa: E731
    lines = [
        mk(900, kind="run_start", source="bg", prompt="", run_id="rBG"),     # no prompt, no mark: only run_end names it
        mk(901, kind="text", text="bg text", run_id="rBG"),
        mk(902, kind="run_end", source="bg", outcome="ok", run_id="rBG", session_id=T_SID, provider="claude"),
        mk(903, kind="text", text="other run text", run_id="rOTHER"),
    ]
    world.timeline_path.write_bytes(b"".join(lines))
    _ctx, _env, _a, plan = await plan_for(world)
    assert sum(len(v) for v in plan.timeline.values()) == 3 and "rBG" in plan.run_ids


async def test_audit_lines_carry_a_run_token(world):
    ad = world.audit_path
    ts = local_iso(world.t0 + 1)
    ad.write_text(f"{ts} [{PID}] TASK run=rA: whatever stamped\n{ts} [{PID}] RESULT run=rA: out\n"
                  f"{ts} [{PID}] RESULT run=rB: other\n")
    (world.data / "turn_marks" / f"{T_SID}.jsonl").write_bytes(j({"run_id": "rA", "offset": 0}))
    ctx, env, adapter, plan = await plan_for(world)
    assert sum(len(v) for v in plan.audit.values()) == 2
    await forget.execute_plan(env, adapter, ctx, plan)
    assert ad.read_text() == f"{ts} [{PID}] RESULT run=rB: other\n"


# ───────────────────────────── acceptance 7: residue scan ───────────────────────────────────
async def test_planted_copy_in_an_unlisted_file_is_reported_with_its_path(world):
    ctx, env, adapter, plan, _c = await forget_it(world)
    planted = world.home / ".gemini" / "antigravity-cli" / "brain" / "x.txt"
    planted.parent.mkdir(parents=True)
    planted.write_text(f"opinion run quoted: {TEXT_PROMPT}\n")
    vault = world.home / "vault" / "Journal" / "2026-11-17.md"
    vault.write_text(f"diary: {TEXT_PROMPT}\n")
    published = []
    env.publish = lambda sk, ev: published.append((sk, ev))
    res = await forget.finish_with_residue(env, adapter, ctx, plan)
    paths = {h["path"] for h in res["hits"]}
    assert str(planted) in paths and str(vault) in paths
    assert not res["clean"]
    assert planted.exists() and vault.exists()           # report only, never deletes
    ev = published[-1][1]
    assert ev["kind"] == "forget_residue" and ev["clean"] is False and ev["session_id"] == T_SID
    note = (world.data / "inbox" / f"forget-{T_SID[:8]}.txt").read_text()
    assert str(planted) in note and SENT not in note and TEXT_PROMPT not in note


async def test_clean_world_reports_clean(world):
    ctx, env, adapter, plan, _c = await forget_it(world)
    res = await forget.finish_with_residue(env, adapter, ctx, plan)
    assert res == {"clean": True, "hits": [], "truncated": False, "checked": True}


async def test_python_fallback_when_rg_is_missing(world, monkeypatch):
    ctx, env, adapter, plan, _c = await forget_it(world)
    planted = world.home / ".claude-accounts" / "work" / "session-env" / "leak.txt"
    planted.write_text(f"leftover {TEXT_PROMPT}")
    monkeypatch.setattr(forget, "_rg", lambda: None)
    res = await forget.finish_with_residue(env, adapter, ctx, plan)
    assert str(planted) in {h["path"] for h in res["hits"]}


async def test_residue_finds_a_leftover_search_row(world):
    """The index is queried through SQL: a row that survived (here: planted) is reported."""
    ctx, env, adapter, plan, _c = await forget_it(world)
    conn = sqlite3.connect(world.data / "search.db")
    conn.execute("INSERT INTO docs (project_id, project_name, source, ts, ref, ref2, path, line, tier, title, body)"
                 " VALUES ('p','p','file',0,'','','/some/file.md',0,'','',?)",
                 (" ".join([TEXT_PROMPT, TEXT_REPLY, TEXT_TOOL, f"hook said {SENT} in its stdout",
                            f"subagent found {SENT} too", f"debug {SENT}", f"backup {SENT}"]),))
    conn.commit()
    conn.close()
    res = await forget.finish_with_residue(env, adapter, ctx, plan)
    assert any(h["store"] == "search index" for h in res["hits"])


async def test_residue_budget_is_a_hard_cap(world, monkeypatch):
    ctx, env, adapter, plan, _c = await forget_it(world)
    monkeypatch.setattr(forget, "_rg", lambda: None)
    big = world.data / "big"
    big.mkdir()
    for i in range(50):
        (big / f"f{i}").write_bytes(b"x" * 1000)
    res = await forget.residue_scan(world.data, plan.held.windows, [big], budget=0.0)
    assert res["truncated"] is True and res["clean"] is False


# ───────────────────────────── unit: held windows, side effects ─────────────────────────────
def test_windows_are_ascii_safe_so_json_escaping_cannot_hide_a_hit():
    text = 'He said "quote" and a newline\nthen ' + "plain words that make a long enough ascii run here " * 2
    items = [forget.ContentItem("assistant", text)]
    held = forget.collect_held(iter(items), seed="s")
    assert held.windows
    for w in held.windows:
        assert len(w) == forget.WINDOW_LEN and all(c.isalnum() or c in " .,:_-" for c in w)
        assert json.dumps(w)[1:-1] == w


def test_non_ascii_text_gets_a_raw_utf8_window():
    held = forget.collect_held(iter([forget.ContentItem("assistant", "Это длинный русский текст без латиницы вообще, совсем")]),
                               seed="s")
    assert held.windows and not held.windows[0].isascii()


def test_context_pack_and_reminders_never_become_residue_windows():
    """Live 19.11: windows taken from the context pack hit every timeline of the cockpit."""
    pack = "<context-pack>\n" + "Board card Ship the onboarding wizard for every new operator account. " * 3 + "\n</context-pack>"
    note = "<system-reminder>Reminder text that the SDK adds to many prompts in every chat.</system-reminder>"
    own = "Remember the code word ZQOWNSENTINEL and reply with the stored value please."
    held = forget.collect_held(iter([forget.ContentItem("prompt", pack + "\n\n" + note + "\n" + own, 1.0, uuid="u")]),
                               seed="s")
    assert held.windows and all(w in own for w in held.windows)
    assert held.prompts                       # the prompt's own keys still come from the full text


def test_grok_home_next_to_the_data_dir_is_not_cockpit_data(tmp_path):
    data = tmp_path / "data"
    assert forget._classify_hit(str(tmp_path / "data-grok-home" / "sessions" / "x.jsonl"), data, tmp_path) == "grok home"
    assert forget._classify_hit(str(data / "timeline" / "p.jsonl"), data, tmp_path) == "cockpit data"


def test_session_title_skips_the_injected_pack():
    pack = "<context-pack>\n" + "x" * 400 + "\n</context-pack>"
    assert forget.session_title(pack + "\n\nfix the login bug") == "fix the login bug"
    assert forget.session_title(pack[:120], pack + "\nsecond") == "second"     # a pack cut at 120 says nothing
    assert forget.session_title(None, "") == ""
    assert forget.session_title("y" * 300) == "y" * 120


def test_window_cap_and_determinism():
    items = [forget.ContentItem("assistant", "lorem ipsum dolor sit " + f"{i:05d}" + " amet consectetur adipiscing elit")
             for i in range(2000)]
    a = forget.collect_held(iter(items), seed="s").windows
    b = forget.collect_held(iter(items), seed="s").windows
    assert len(a) == forget.MAX_WINDOWS and a == b


@pytest.mark.parametrize("tool,inp,kind", [
    ("Write", {"file_path": "/a/b.txt"}, "file_write"),
    ("Edit", {"file_path": "/home/x/.claude/projects/p/memory/note.md"}, "memory"),
    ("Bash", {"command": "git push origin master"}, "git"),
    ("Bash", {"command": "bash restart-self.sh"}, "deploy"),
    ("Bash", {"command": "opinion \"is this ok\""}, "sent_out"),
    ("mcp__x__second_opinion", {}, "sent_out"),
    ("mcp__mail__send_mail", {}, "message"),
])
def test_side_effect_classification(tool, inp, kind):
    assert kind in {s["kind"] for s in forget.classify_tool_use(tool, inp)}


def test_side_effect_scan_ignores_reads():
    assert forget.classify_tool_use("Read", {"file_path": "/a"}) == []
    assert forget.classify_tool_use("Bash", {"command": "ls -la"}) == []


def test_capabilities_follow_the_flag(monkeypatch):
    assert forget.capabilities("claude") == {"delete_session": True, "forget_turn": True}
    assert forget.capabilities("nope") == {"delete_session": False, "forget_turn": False}
    monkeypatch.setenv("FORGET_ENABLED", "0")
    assert forget.capabilities("claude") == {"delete_session": False, "forget_turn": False}


def test_providers_rows_carry_the_forget_flags():
    import providers
    caps = providers.get("claude").capabilities()
    assert caps["delete_session"] is True and caps["forget_turn"] is True
    assert providers.get("codex").capabilities()["delete_session"] in (True, False)

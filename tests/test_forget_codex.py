"""spec-098: the Codex forget adapter on a synthetic CODEX_HOME shaped like the measured one
(p0/codex.md): rollouts, state_5 / logs_2 / thread_history_1 / memories_1 / goals_1 / queue_1,
history.jsonl, session_index.jsonl, shell_snapshots, a spawned child and a fork. The native
`thread/delete` is replaced by a function that does what P0 measured (unlink the rollout, delete
the threads + logs rows WITHOUT secure_delete) so the hand sweep is what the test judges."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

import forget
import forget_codex
from forget_codex import CodexAdapter

SENT = "ZEBRA-SENTINEL-4471"       # only in the session that gets deleted
KEEP = "OTTER-KEEPSAKE-9902"       # only in the session that stays
FORK_KEEP = "FORKED-BY-OPERATOR-4417"  # the operator's own fork of T: an independent thread, it stays

T = "01a20000-0000-7000-8000-000000000001"       # target thread
CHILD = "01a20000-0000-7000-8000-000000000002"   # sub-agent of T
FORK = "01a20000-0000-7000-8000-000000000003"    # fork of T
OTHER = "01a20000-0000-7000-8000-0000000000aa"   # unrelated thread

SCHEMAS = {
    "state_5": """
        CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL,
            first_user_message TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL,
            child_thread_id TEXT NOT NULL PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE thread_dynamic_tools (thread_id TEXT NOT NULL, position INTEGER NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY(thread_id, position));
        CREATE TABLE thread_artifacts (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, payload TEXT NOT NULL);
    """,
    "logs_2": """
        CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, feedback_log_body TEXT,
            thread_id TEXT);
    """,
    "thread_history_1": """
        CREATE TABLE thread_turns (thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY (thread_id, turn_id));
        CREATE TABLE thread_items (thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
            item_json TEXT NOT NULL, PRIMARY KEY (thread_id, turn_id, item_id));
        CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY, next INTEGER NOT NULL);
        CREATE TABLE thread_realtime_items (thread_id TEXT NOT NULL, item_id TEXT NOT NULL, item_json TEXT NOT NULL,
            PRIMARY KEY (thread_id, item_id));
        CREATE TRIGGER thread_realtime_items_projection_cleanup AFTER DELETE ON thread_history_projection_state
            BEGIN DELETE FROM thread_realtime_items WHERE thread_id = OLD.thread_id; END;
    """,
    "memories_1": """
        CREATE TABLE stage1_outputs (thread_id TEXT PRIMARY KEY, raw_memory TEXT NOT NULL, rollout_summary TEXT NOT NULL);
    """,
    "goals_1": """
        CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY NOT NULL, objective TEXT NOT NULL);
    """,
    "queue_1": """
        CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE queued_thread_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE);
        CREATE TRIGGER queued_items_revision_after_delete AFTER DELETE ON queued_items BEGIN
            INSERT INTO queued_thread_revisions (thread_id) VALUES (OLD.thread_id)
            ON CONFLICT(thread_id) DO UPDATE SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
    """,
}


def _db(home: Path, name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(home / f"{name}.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMAS[name])
    return conn


def _rollout(home: Path, tid: str, *, text: str, meta: dict | None = None, archived=False) -> Path:
    root = home / ("archived_sessions" if archived else "sessions/2026/11/18")
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"rollout-2026-11-18T10-00-00-{tid}.jsonl"
    rows = [
        {"timestamp": "2026-11-18T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": tid, "cwd": "/work/p", **(meta or {})}},
        {"timestamp": "2026-11-18T10:00:01.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": f"prompt {text}"}},
        {"timestamp": "2026-11-18T10:00:02.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "c1",
                     "arguments": json.dumps({"cmd": f"echo {text}"})}},
        {"timestamp": "2026-11-18T10:00:03.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": [{"type": "input_text", "text": f"out {text}"}]}},
        {"timestamp": "2026-11-18T10:00:04.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "id": "m1",
                     "content": [{"type": "output_text", "text": f"answer {text}"}]}},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "codex"
    h.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(h))
    monkeypatch.setattr(forget_codex, "_BUSY_SEC", 0.2)
    monkeypatch.setattr(forget_codex, "_PROC_ROOT", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    state, logs, hist = _db(h, "state_5"), _db(h, "logs_2"), _db(h, "thread_history_1")
    mem, goals, queue = _db(h, "memories_1"), _db(h, "goals_1"), _db(h, "queue_1")
    rolls = {
        T: _rollout(h, T, text=SENT),
        CHILD: _rollout(h, CHILD, text=SENT, meta={"parent_thread_id": T}, archived=True),
        FORK: _rollout(h, FORK, text=FORK_KEEP, meta={"forked_from_id": T}),
        OTHER: _rollout(h, OTHER, text=KEEP),
    }
    for tid, p in rolls.items():
        word = KEEP if tid == OTHER else (FORK_KEEP if tid == FORK else SENT)
        state.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,0)",
                      (tid, str(p), 1795000000, 1795003600, "/work/p", f"title {word}", f"first {word}"))
        logs.execute("INSERT INTO logs (ts, feedback_log_body, thread_id) VALUES (1, ?, ?)", (("pad " * 3000) + f"log {word}", tid))
        hist.execute("INSERT INTO thread_turns VALUES (?, 't1', 'done')", (tid,))
        hist.execute("INSERT INTO thread_items VALUES (?, 't1', 'i1', ?)", (tid, json.dumps({"text": word})))
        hist.execute("INSERT INTO thread_history_projection_state VALUES (?, 1)", (tid,))
        hist.execute("INSERT INTO thread_realtime_items VALUES (?, 'r1', ?)", (tid, json.dumps({"text": word})))
        mem.execute("INSERT INTO stage1_outputs VALUES (?, ?, ?)", (tid, f"mem {word}", f"sum {word}"))
        goals.execute("INSERT INTO thread_goals VALUES (?, ?)", (tid, f"goal {word}"))
        queue.execute("INSERT INTO queued_items VALUES (?, ?, ?)", (f"q-{tid}", tid, json.dumps({"t": word})))
        queue.execute("INSERT INTO queued_thread_revisions (thread_id) VALUES (?)", (tid,))
        state.execute("INSERT INTO thread_dynamic_tools VALUES (?, 0, 'x')", (tid,))
        (h / "shell_snapshots").mkdir(exist_ok=True)
        (h / "shell_snapshots" / f"{tid}.1795000000.sh").write_text("export A=1\n")
    state.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')", (T, CHILD))
    for c in (state, logs, hist, mem, goals, queue):
        c.commit()
        c.close()
    (h / "history.jsonl").write_text(
        "".join(json.dumps({"session_id": t, "ts": 1, "text": f"hist {SENT if t != OTHER else KEEP}"}) + "\n"
                for t in (T, OTHER, CHILD)))
    (h / "session_index.jsonl").write_text(
        "".join(json.dumps({"id": t, "thread_name": f"n {SENT if t not in (OTHER, FORK) else KEEP}"}) + "\n"
                for t in (OTHER, T, FORK, T)))
    return h


def _fake_native(home: Path):
    """What P0 measured: rollout unlinked, threads + logs rows deleted, no secure_delete, WAL left."""
    def native(tid: str) -> None:
        state = sqlite3.connect(home / "state_5.sqlite")
        state.execute("PRAGMA secure_delete=OFF")      # the Rust sqlx build leaves freed pages (P0)
        row = state.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
        if row:
            try:
                os.unlink(row[0])
            except FileNotFoundError:
                pass
        state.execute("DELETE FROM threads WHERE id=?", (tid,))
        state.commit()
        state.close()
        logs = sqlite3.connect(home / "logs_2.sqlite")
        logs.execute("PRAGMA secure_delete=OFF")
        logs.execute("DELETE FROM logs WHERE thread_id=?", (tid,))
        logs.commit()
        logs.close()
    return native


def _raw_hits(root: Path, needle: str) -> list[str]:
    n = needle.encode()
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and n in p.read_bytes())


def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _count(home: Path, db: str, sql: str, *args) -> int:
    c = sqlite3.connect(home / f"{db}.sqlite")
    try:
        return c.execute(sql, args).fetchone()[0]
    finally:
        c.close()


def test_registered_and_protocol():
    a = forget.adapter("codex")
    assert a is not None and isinstance(a, forget.Adapter)
    assert a.name == "codex" and a.can_forget_turn is False


def test_capability_follows_codex_enabled(monkeypatch):
    monkeypatch.delenv("CODEX_ENABLED", raising=False)
    assert CodexAdapter().can_delete_session is False
    monkeypatch.setenv("CODEX_ENABLED", "true")
    assert CodexAdapter().can_delete_session is True


def test_locate_finds_the_whole_family(home):
    art = CodexAdapter().locate({}, {"cwd": "/work/p"}, T)
    assert art and art.provider == "codex" and art.cwd == "/work/p"
    assert set(art.extra["threads"]) == {T, CHILD}                 # a fork is NOT deleted with its source
    assert art.extra["children"] == [CHILD] and art.extra["forks"] == [FORK]
    names = {p.name for p in art.paths}
    assert any(n.endswith(f"{CHILD}.jsonl") for n in names)       # the archived child's rollout
    assert not any(FORK in n for n in names)                       # nothing of the fork is planned
    assert not any(OTHER in n for n in names)
    assert art.first_ts == 1795000000.0 and SENT in art.title
    assert json.dumps(art.extra).count(SENT) == 0                  # extra carries no text
    assert CodexAdapter().locate({}, {}, "01a20000-0000-7000-8000-0000000000ff") is None
    assert CodexAdapter().locate({}, {}, "../../etc/passwd") is None


def test_delete_leaves_no_sentinel_byte_and_keeps_the_other_thread(home, monkeypatch):
    monkeypatch.setattr(forget_codex, "_native_delete", _fake_native(home))
    ad = CodexAdapter()
    art = ad.locate({}, {"cwd": "/work/p"}, T)
    # the P0 residue pattern is reproducible here: after the native delete alone the text is still on disk
    _fake_native(home)(T)
    assert "logs_2.sqlite" in _raw_hits(home, SENT), "fixture must leave residue after the native delete alone"
    res = ad.delete({}, art)
    assert _raw_hits(home, SENT) == [], _raw_hits(home, SENT)
    assert ad.verify_gone({}, art) == []
    assert res["leftovers"] == [] and res["lines"] == 4 and res["files"] >= 2
    assert {"state_5.sqlite", "logs_2.sqlite", "thread_history_1.sqlite"} <= set(res["compacted"])
    # the unrelated thread is intact in every store
    assert OTHER in (home / "history.jsonl").read_text()
    assert KEEP in (home / "history.jsonl").read_text()
    assert [json.loads(x)["id"] for x in (home / "session_index.jsonl").read_text().splitlines()] == [OTHER, FORK]
    for db, sql in (("state_5", "SELECT count(*) FROM threads WHERE id=?"),
                    ("logs_2", "SELECT count(*) FROM logs WHERE thread_id=?"),
                    ("thread_history_1", "SELECT count(*) FROM thread_items WHERE thread_id=?"),
                    ("thread_history_1", "SELECT count(*) FROM thread_realtime_items WHERE thread_id=?"),
                    ("memories_1", "SELECT count(*) FROM stage1_outputs WHERE thread_id=?"),
                    ("goals_1", "SELECT count(*) FROM thread_goals WHERE thread_id=?"),
                    ("queue_1", "SELECT count(*) FROM queued_items WHERE thread_id=?"),
                    ("queue_1", "SELECT count(*) FROM queued_thread_revisions WHERE thread_id=?")):
        assert _count(home, db, sql, OTHER) == 1, (db, sql)
        assert _count(home, db, sql, T) == 0, (db, sql)
    assert _count(home, "state_5", "SELECT count(*) FROM thread_spawn_edges") == 0
    assert _raw_hits(home, KEEP)                                   # still readable
    assert list((home / "sessions").rglob(f"*{OTHER}.jsonl"))
    assert (home / "shell_snapshots" / f"{OTHER}.1795000000.sh").exists()
    assert not list((home / "shell_snapshots").glob(f"{T}.*"))
    # idempotent
    again = ad.delete({}, art)
    assert again["lines"] == 0 and again["files"] == 0 and again["rows"] == 0


def test_native_failure_is_reported_and_the_hand_sweep_still_runs(home, monkeypatch):
    def boom(tid):
        raise RuntimeError("thread not found")
    monkeypatch.setattr(forget_codex, "_native_delete", boom)
    ad = CodexAdapter()
    art = ad.locate({}, {"cwd": "/work/p"}, T)
    res = ad.delete({}, art)
    assert len(res["native_errors"]) == 2
    assert _raw_hits(home, SENT) == []                             # rollouts + rows swept by hand


def test_busy_refusal_names_the_process_and_touches_nothing(home, tmp_path):
    proc = tmp_path / "proc"
    other_proc = proc / "4242"
    other_proc.mkdir()
    (other_proc / "cmdline").write_bytes(b"/usr/lib/node_modules/@openai/codex/bin/codex\0exec\0--json\0")
    unrelated = proc / "77"
    unrelated.mkdir()
    (unrelated / "cmdline").write_bytes(b"/usr/bin/python3\0-m\0pytest\0tests/test_forget_codex.py\0")
    own = proc / "88"
    own.mkdir()
    (own / "cmdline").write_bytes(b"codex\0app-server\0")
    forget_codex._OWN_PIDS.add(88)
    try:
        before = _tree_hash(home)
        why = CodexAdapter().busy_reason({}, {}, T)
        assert why and why.startswith("Codex is in use") and "4242" in why and "88" not in why
        assert _tree_hash(home) == before
        (other_proc / "cmdline").unlink()
        assert CodexAdapter().busy_reason({}, {}, T) is None
    finally:
        forget_codex._OWN_PIDS.discard(88)


def test_node_wrapper_counts_as_codex(home, tmp_path):
    p = tmp_path / "proc" / "9"
    p.mkdir()
    (p / "cmdline").write_bytes(b"node\0/usr/lib/node_modules/@openai/codex/bin/codex.js\0exec\0")
    assert "in use" in CodexAdapter().busy_reason({}, {}, T)


def test_an_argument_named_codex_is_not_a_codex_process(home, tmp_path):
    """Live 19.11: `python3 check.py codex` refused a delete as "Codex is in use"."""
    p = tmp_path / "proc" / "9"
    p.mkdir()
    (p / "cmdline").write_bytes(b"python3\0forget_live_a.py\0codex\0")
    assert CodexAdapter().busy_reason({}, {}, T) is None
    (p / "cmdline").write_bytes(b"node\0--max-old-space-size=4096\0/opt/codex/bin/codex.js\0exec\0")
    assert "in use" in CodexAdapter().busy_reason({}, {}, T)


def test_locked_database_is_a_leftover_not_a_failure(home, monkeypatch):
    monkeypatch.setattr(forget_codex, "_native_delete", _fake_native(home))
    ad = CodexAdapter()
    art = ad.locate({}, {"cwd": "/work/p"}, T)
    blocker = sqlite3.connect(home / "memories_1.sqlite", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        res = ad.delete({}, art)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert any(x.startswith("memories_1.sqlite") for x in res["leftovers"])
    assert _count(home, "memories_1", "SELECT count(*) FROM stage1_outputs WHERE thread_id=?", T) == 1
    # everything else still got cleaned
    assert _count(home, "state_5", "SELECT count(*) FROM threads WHERE id=?", T) == 0
    assert (home / "history.jsonl").read_text().count(T) == 0
    assert "memories_1.sqlite" in " ".join(ad.verify_gone({}, art))


def test_symlinked_rollout_is_refused_and_the_target_survives(home, tmp_path, monkeypatch):
    monkeypatch.setattr(forget_codex, "_native_delete", lambda tid: None)
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    ad = CodexAdapter()
    art = ad.locate({}, {"cwd": "/work/p"}, T)
    link = home / "sessions" / "2026" / "11" / "18" / "rollout-2026-11-18T10-00-00-evil.jsonl"
    link.symlink_to(outside)
    art.paths.append(link)
    res = ad.delete({}, art)
    assert outside.read_text() == "precious"
    assert any("refused" in x for x in res["leftovers"])
    # a path outside the home is refused too
    art.paths.append(outside)
    res = ad.delete({}, art)
    assert outside.read_text() == "precious"


def test_symlinked_history_file_is_not_replaced(home, tmp_path, monkeypatch):
    monkeypatch.setattr(forget_codex, "_native_delete", lambda tid: None)
    real = tmp_path / "real-history.jsonl"
    real.write_text((home / "history.jsonl").read_text())
    (home / "history.jsonl").unlink()
    (home / "history.jsonl").symlink_to(real)
    before = real.read_bytes()
    ad = CodexAdapter()
    res = ad.delete({}, ad.locate({}, {"cwd": "/work/p"}, T))
    assert real.read_bytes() == before and (home / "history.jsonl").is_symlink()
    assert any(x.startswith("history.jsonl") for x in res["leftovers"])


def test_read_content_streams_every_kind(home):
    ad = CodexAdapter()
    items = list(ad.read_content(ad.locate({}, {"cwd": "/work/p"}, T)))
    kinds = {i.kind for i in items}
    assert {"prompt", "assistant", "tool_use", "tool_result"} <= kinds
    prompts = [i for i in items if i.kind == "prompt"]
    assert len(prompts) == 2 and all(SENT in i.text for i in prompts)   # T + its sub-agent; the fork is not read
    tool = next(i for i in items if i.kind == "tool_use")
    assert tool.tool == "shell" and tool.tool_input["cmd"] == f"echo {SENT}" and tool.ts
    assert next(i for i in items if i.kind == "tool_result").text == f"out {SENT}"


def test_residue_root_is_the_codex_home(home):
    assert CodexAdapter().residue_roots({}) == [home]


def test_a_thread_of_another_directory_is_not_this_projects(home):
    """~/.codex is shared with the operator's own Codex: a thread whose cwd is not the project's is
    invisible to this project's delete (404), even with a valid id."""
    assert CodexAdapter().locate({}, {"cwd": "/somewhere/else"}, T) is None
    assert CodexAdapter().locate({}, {}, T) is None


def test_the_operators_fork_survives_byte_identical(home, monkeypatch):
    monkeypatch.setattr(forget_codex, "_native_delete", _fake_native(home))
    fork_files = {p: p.read_bytes() for p in home.rglob("*") if FORK in p.name}
    ad = CodexAdapter()
    art = ad.locate({}, {"cwd": "/work/p"}, T)
    ad.delete({}, art)
    assert ad.verify_gone({}, art) == []
    for p, b in fork_files.items():
        assert p.read_bytes() == b
    assert _count(home, "state_5", "SELECT count(*) FROM threads WHERE id=?", FORK) == 1


def test_another_thread_quoting_the_id_does_not_block_verify(home, monkeypatch):
    """F8: a row of ANOTHER thread that merely mentions the id is not the thread's row."""
    monkeypatch.setattr(forget_codex, "_native_delete", _fake_native(home))
    c = sqlite3.connect(home / "logs_2.sqlite")
    c.execute("INSERT INTO logs (ts, feedback_log_body, thread_id) VALUES (2, ?, ?)", (f"look at thread {T}", OTHER))
    c.commit()
    c.close()
    ad = CodexAdapter()
    art = ad.locate({}, {"cwd": "/work/p"}, T)
    ad.delete({}, art)
    assert ad.verify_gone({}, art) == []
    assert _count(home, "logs_2", "SELECT count(*) FROM logs WHERE thread_id=?", OTHER) == 2

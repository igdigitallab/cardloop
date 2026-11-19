"""spec-098: the Grok forget adapter on a synthetic GROK_HOME shaped like the measured one
(p0/grok.md): sessions/<url-encoded cwd>/<sid>/ files, a shared per-cwd prompt_history.jsonl,
the GLOBAL sessions/session_search.sqlite (FTS5, external content) and the cockpit's
grok_sent / grok_sessions files."""
import hashlib
import json
import os
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

import forget
import forget_grok
from forget_grok import GrokAdapter

SENT = "QUOKKA-SENTINEL-3318"      # only in the session that gets deleted
KEEP = "WOMBAT-KEEPSAKE-7741"      # only in the session that stays
S1 = "01a30000-0000-7000-8000-000000000001"     # target
S2 = "01a30000-0000-7000-8000-000000000002"     # same cwd, stays
S3 = "01a30000-0000-7000-8000-000000000003"     # other cwd, stays
CWD = "/work/proj one"
CWD2 = "/work/other"

SEARCH_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE session_docs (session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, updated_at INTEGER NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE VIRTUAL TABLE session_docs_fts USING fts5(title, content, content='session_docs', content_rowid='rowid');
CREATE TRIGGER session_docs_ai AFTER INSERT ON session_docs BEGIN
    INSERT INTO session_docs_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content); END;
CREATE TRIGGER session_docs_ad AFTER DELETE ON session_docs BEGIN
    INSERT INTO session_docs_fts(session_docs_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content); END;
"""


def _session(group: Path, sid: str, word: str) -> Path:
    d = group / sid
    (d / "terminal").mkdir(parents=True)
    rows = [
        {"type": "system", "content": "You are Grok"},
        {"type": "user", "content": [{"type": "text", "text": f"<user_query>\nprompt {word}\n</user_query>"}]},
        {"type": "reasoning", "id": "r", "summary": [{"type": "summary_text", "text": f"think {word}"}]},
        {"type": "assistant", "content": f"answer {word}",
         "tool_calls": [{"id": "call-1", "name": "run_terminal_command",
                         "arguments": json.dumps({"command": f"echo {word}", "description": "d"})}]},
        {"type": "tool_result", "tool_call_id": "call-1", "content": f"exit: 0\n{word}\n"},
    ]
    (d / "chat_history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "updates.jsonl").write_text(json.dumps({"update": {"text": word}}) + "\n")
    (d / "summary.json").write_text(json.dumps({
        "info": {"id": sid, "cwd": CWD}, "session_summary": f"summary {word}",
        "created_at": "2026-11-18T10:00:00Z", "updated_at": "2026-11-18T11:00:00Z"}))
    (d / "terminal" / "call-1.log").write_text(f"{word}\n")
    (d / "chat_history.jsonl.lock").write_text("")
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    home = tmp_path / "data-grok-home"
    (data / "grok_sent").mkdir(parents=True)
    (data / "grok_sessions").mkdir()
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setattr(forget_grok, "_PROC_ROOT", tmp_path / "proc")
    monkeypatch.setattr(forget_grok, "_BUSY_SEC", 0.2)
    (tmp_path / "proc").mkdir()
    sessions = home / "sessions"
    g1 = sessions / urllib.parse.quote(CWD, safe="")
    g2 = sessions / urllib.parse.quote(CWD2, safe="")
    _session(g1, S1, SENT)
    _session(g1, S2, KEEP)
    _session(g2, S3, KEEP)
    (g1 / "prompt_history.jsonl").write_text("".join(
        json.dumps({"timestamp": i, "session_id": s, "prompt": f"p {w}", "is_bash": False}) + "\n"
        for i, (s, w) in enumerate([(S1, SENT), (S2, KEEP), (S1, SENT)])))
    (g2 / "prompt_history.jsonl").write_text(
        json.dumps({"timestamp": 1, "session_id": S3, "prompt": f"p {KEEP}"}) + "\n")
    db = sqlite3.connect(sessions / "session_search.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SEARCH_SCHEMA)
    for sid, cwd, word in ((S1, CWD, SENT), (S2, CWD, KEEP), (S3, CWD2, KEEP)):
        db.execute("INSERT INTO session_docs VALUES (?,?,?,?,?,?)",
                   (sid, cwd, 1795000000, f"title {word}", f"content {word} " * 2000, "h"))
    db.commit()
    db.close()
    for sid, word in ((S1, SENT), (S2, KEEP)):
        (data / "grok_sent" / sid).write_text(hashlib.sha256(word.encode()).hexdigest() + "\n")
        (data / "grok_sessions" / sid).write_text(json.dumps({"cwd": CWD}) + "\n")
    return {"home": home, "data": data, "ctx": {"DATA": str(data)}, "project": {"cwd": CWD}, "tmp": tmp_path}


def _raw_hits(root: Path, needle: str) -> list[str]:
    n = needle.encode()
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and n in p.read_bytes())


def _tree_hash(*roots: Path) -> dict:
    out = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_registered_and_protocol():
    a = forget.adapter("grok")
    assert a is not None and isinstance(a, forget.Adapter)
    assert a.name == "grok" and a.can_forget_turn is False


def test_capability_follows_grok_enabled(monkeypatch):
    monkeypatch.delenv("GROK_ENABLED", raising=False)
    assert GrokAdapter().can_delete_session is False
    monkeypatch.setenv("GROK_ENABLED", "true")
    assert GrokAdapter().can_delete_session is True


def test_locate(env):
    art = GrokAdapter().locate(env["ctx"], env["project"], S1)
    assert art and art.cwd == CWD and art.provider == "grok"
    names = [str(p) for p in art.paths]
    assert any(n.endswith(f"/{S1}") and "sessions" in n for n in names)
    assert any("grok_sent" in n for n in names) and any("grok_sessions" in n for n in names)
    assert not any(S2 in n for n in names)
    assert art.first_ts and art.last_ts > art.first_ts
    assert SENT not in json.dumps(art.extra)
    assert GrokAdapter().locate(env["ctx"], env["project"], "01a30000-0000-7000-8000-0000000000ff") is None
    assert GrokAdapter().locate(env["ctx"], env["project"], "../../x") is None
    # found even when the project's cwd is not the one the session lives under
    # sessions/ is shared by every project: another project's list cannot reach this session (review G2)
    assert GrokAdapter().locate(env["ctx"], {"cwd": "/somewhere/else"}, S1) is None
    assert GrokAdapter().locate(env["ctx"], {"cwd": CWD2}, S1) is None
    assert GrokAdapter().locate(env["ctx"], {}, S1) is None


def test_delete_leaves_no_sentinel_byte_and_keeps_the_others(env):
    ad = GrokAdapter()
    art = ad.locate(env["ctx"], env["project"], S1)
    home, data = env["home"], env["data"]
    assert _raw_hits(home, SENT) and _raw_hits(data, SENT) == []
    res = ad.delete(env["ctx"], art)
    assert res["leftovers"] == [] and res["dirs"] == 1 and res["lines"] == 2 and res["rows"] == 1
    assert res["files"] == 2
    assert _raw_hits(home, SENT) == [], _raw_hits(home, SENT)
    assert _raw_hits(home, S1) == []                               # not even the id (sqlite + wal included)
    assert ad.verify_gone(env["ctx"], art) == []
    assert not (data / "grok_sent" / S1).exists() and not (data / "grok_sessions" / S1).exists()
    # the others are intact
    g1 = home / "sessions" / urllib.parse.quote(CWD, safe="")
    assert (g1 / S2 / "chat_history.jsonl").exists() and (home / "sessions" / urllib.parse.quote(CWD2, safe="") / S3).is_dir()
    lines = (g1 / "prompt_history.jsonl").read_text().splitlines()
    assert [json.loads(x)["session_id"] for x in lines] == [S2]
    db = sqlite3.connect(home / "sessions" / "session_search.sqlite")
    assert sorted(r[0] for r in db.execute("SELECT session_id FROM session_docs")) == [S2, S3]
    assert db.execute("SELECT count(*) FROM session_docs_fts WHERE session_docs_fts MATCH 'WOMBAT'").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM session_docs_fts WHERE session_docs_fts MATCH 'QUOKKA'").fetchone()[0] == 0
    db.close()
    assert (data / "grok_sent" / S2).exists() and (data / "grok_sessions" / S2).exists()
    assert _raw_hits(home, KEEP)
    again = ad.delete(env["ctx"], art)                              # idempotent
    assert again["dirs"] == 0 and again["lines"] == 0 and again["rows"] == 0 and again["files"] == 0


def test_busy_by_cwd_or_session_id_leaves_everything_untouched(env):
    proc = env["tmp"] / "proc"
    p = proc / "5150"
    p.mkdir()
    (p / "cmdline").write_bytes(b"/home/x/.grok/bin/grok\0agent\0--no-leader\0stdio\0")
    os.symlink(CWD, p / "cwd")
    before = _tree_hash(env["home"], env["data"])
    why = GrokAdapter().busy_reason(env["ctx"], env["project"], S1)
    assert why and "5150" in why
    assert _tree_hash(env["home"], env["data"]) == before
    # a grok elsewhere that does not name the session is not our business
    os.unlink(p / "cwd")
    os.symlink("/work/unrelated", p / "cwd")
    assert GrokAdapter().busy_reason(env["ctx"], env["project"], S1) is None
    # but one whose argv names the session is
    (p / "cmdline").write_bytes(f"grok\0agent\0--resume\0{S1}\0".encode())
    assert "5150" in GrokAdapter().busy_reason(env["ctx"], env["project"], S1)
    # a non-grok process in the cwd never counts
    (p / "cmdline").write_bytes(b"/usr/bin/vim\0notes.md\0")
    os.unlink(p / "cwd")
    os.symlink(CWD, p / "cwd")
    assert GrokAdapter().busy_reason(env["ctx"], env["project"], S1) is None


def test_symlinked_session_dir_is_refused_and_its_target_survives(env):
    home = env["home"]
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("precious")
    g1 = home / "sessions" / urllib.parse.quote(CWD, safe="")
    # the model replaced a session dir with a link to a directory outside the home
    import shutil
    shutil.rmtree(g1 / S2)
    (g1 / S2).symlink_to(outside, target_is_directory=True)
    ad = GrokAdapter()
    art = ad.locate(env["ctx"], env["project"], S1)
    art.paths.append(g1 / S2)
    ad.delete(env["ctx"], art)
    assert (outside / "precious.txt").read_text() == "precious"
    assert (g1 / S2).is_symlink()
    # and asking to forget the linked session itself does nothing to the target either
    art2 = forget.SessionArtifacts(provider="grok", session_id=S2, cwd=CWD, paths=[g1 / S2],
                                   extra={"home": str(home), "data": str(env["data"])})
    res = ad.delete(env["ctx"], art2)
    assert (outside / "precious.txt").read_text() == "precious"
    assert res["dirs"] == 0 and any("refused" in x for x in res["leftovers"])


def test_symlink_inside_the_session_is_unlinked_not_followed(env):
    outside = env["tmp"] / "outside2"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    g1 = env["home"] / "sessions" / urllib.parse.quote(CWD, safe="")
    (g1 / S1 / "terminal" / "evil").symlink_to(outside, target_is_directory=True)
    ad = GrokAdapter()
    ad.delete(env["ctx"], ad.locate(env["ctx"], env["project"], S1))
    assert (outside / "keep.txt").read_text() == "keep"
    assert not (g1 / S1).exists()


def test_symlinked_prompt_history_is_not_replaced(env):
    g1 = env["home"] / "sessions" / urllib.parse.quote(CWD, safe="")
    real = env["tmp"] / "real-ph.jsonl"
    real.write_text((g1 / "prompt_history.jsonl").read_text())
    (g1 / "prompt_history.jsonl").unlink()
    (g1 / "prompt_history.jsonl").symlink_to(real)
    before = real.read_bytes()
    ad = GrokAdapter()
    res = ad.delete(env["ctx"], ad.locate(env["ctx"], env["project"], S1))
    assert real.read_bytes() == before and (g1 / "prompt_history.jsonl").is_symlink()
    assert any(x.startswith("prompt_history.jsonl") for x in res["leftovers"])


def test_locked_search_db_is_a_leftover_not_a_failure(env):
    ad = GrokAdapter()
    art = ad.locate(env["ctx"], env["project"], S1)
    blocker = sqlite3.connect(env["home"] / "sessions" / "session_search.sqlite", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        res = ad.delete(env["ctx"], art)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert any(x.startswith("session_search.sqlite") for x in res["leftovers"])
    assert res["dirs"] == 1 and res["lines"] == 2
    assert "session_search.sqlite" in " ".join(ad.verify_gone(env["ctx"], art))


def test_read_content(env):
    ad = GrokAdapter()
    items = list(ad.read_content(ad.locate(env["ctx"], env["project"], S1)))
    kinds = [i.kind for i in items]
    assert {"summary", "prompt", "assistant", "tool_use", "tool_result"} <= set(kinds)
    assert next(i for i in items if i.kind == "prompt").text == f"prompt {SENT}"
    tool = next(i for i in items if i.kind == "tool_use")
    assert tool.tool_input.get("command") == f"echo {SENT}" and tool.uuid == "call-1"
    assert next(i for i in items if i.kind == "tool_result").text.endswith(f"{SENT}\n")


def test_residue_root_is_the_grok_home(env):
    assert GrokAdapter().residue_roots(env["ctx"]) == [env["home"]]


def test_text_left_by_groks_own_delete_is_vacuumed_out(env):
    """P0: `_x.ai/session/delete` drops the row but the text stays in free pages / FTS segments."""
    db_path = env["home"] / "sessions" / "session_search.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA secure_delete=OFF")
    db.execute("DELETE FROM session_docs WHERE session_id = ?", (S1,))
    db.commit()
    db.close()
    assert "sessions/session_search.sqlite" in _raw_hits(env["home"], SENT), "fixture must leave the residue"
    ad = GrokAdapter()
    art = ad.locate(env["ctx"], env["project"], S1)
    ad.delete(env["ctx"], art)
    assert _raw_hits(env["home"], SENT) == []


def test_another_session_quoting_the_id_does_not_block_verify(env):
    """Review G1: S2's prompt and S3's search row mention S1's id; neither is S1's, and verify must
    not report S1 as still present (that left the purge stuck after the dir was gone)."""
    g1 = env["home"] / "sessions" / urllib.parse.quote(CWD, safe="")
    with open(g1 / "prompt_history.jsonl", "a") as f:
        f.write(json.dumps({"timestamp": 9, "session_id": S2, "prompt": f"what happened in {S1}?"}) + "\n")
    db = sqlite3.connect(env["home"] / "sessions" / "session_search.sqlite")
    db.execute("UPDATE session_docs SET content = content || ? WHERE session_id = ?", (f" see {S1}", S3))
    db.commit()
    db.close()
    ad = GrokAdapter()
    art = ad.locate(env["ctx"], env["project"], S1)
    ad.delete(env["ctx"], art)
    assert ad.verify_gone(env["ctx"], art) == []
    lines = [json.loads(x) for x in (g1 / "prompt_history.jsonl").read_text().splitlines()]
    assert [r["session_id"] for r in lines] == [S2, S2]


def test_prompt_history_is_swept_even_when_the_session_dir_is_already_gone(env):
    """Review G7: Grok's own delete (or an earlier partial forget) removed the dir; the verbatim
    prompts in the shared prompt_history still go."""
    import shutil
    g1 = env["home"] / "sessions" / urllib.parse.quote(CWD, safe="")
    shutil.rmtree(g1 / S1)
    ad = GrokAdapter()
    art = ad.locate(env["ctx"], env["project"], S1)       # found by its search row
    assert art is not None
    ad.delete(env["ctx"], art)
    assert SENT not in (g1 / "prompt_history.jsonl").read_text()
    assert ad.verify_gone(env["ctx"], art) == []

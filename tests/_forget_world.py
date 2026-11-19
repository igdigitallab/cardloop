"""spec-098 test world: a tmp HOME + tmp DATA with synthetic stores in the measured shapes.

Not a test module (leading underscore). Everything lives under `tmp_path`; `Path.home()` and `$HOME`
point at it, so no test can touch the real ~/.claude, ~/.codex or cardloop/data.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import search as _search  # noqa: E402
import usage_scanner  # noqa: E402

SENT = "zqsentinel7f3a91c4"                      # appears ONLY in the target session's content
T_SID = "11111111-aaaa-4aaa-8aaa-111111111111"   # the session that gets deleted
K_SID = "22222222-bbbb-4bbb-8bbb-222222222222"   # another session of the same project: must survive
TEXT_PROMPT = f"please remember the code word {SENT} for this deleted session only"
TEXT_REPLY = f"Understood, the code word {SENT} is stored in my notes for this session"
TEXT_TOOL = f"tool output mentions {SENT} and some more filler words to make a window"
K_PROMPT = "kept session prompt about unrelated quarterly planning numbers please"
K_REPLY = "kept session reply about unrelated quarterly planning numbers listed here"
PID = "proj"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


def local_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def j(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class World:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.home = tmp / "home"
        self.data = tmp / "data"
        self.cwd = tmp / PID
        for d in (self.home, self.data, self.cwd):
            d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.home))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("FORGET_JOURNAL_DIR", str(self.home / "vault" / "Journal"))
        import re
        self.slug = re.sub(r"[^a-zA-Z0-9]", "-", str(self.cwd))
        self.proj_dir = self.home / ".claude" / "projects" / self.slug
        self.proj_dir.mkdir(parents=True)
        self.t0 = time.time() - 600
        self.build()

    # ── builders ────────────────────────────────────────────────────────────────────────────
    def build(self) -> None:
        h, d = self.home, self.data
        t0 = self.t0
        # target transcript (measured line types)
        t = self.proj_dir / f"{T_SID}.jsonl"
        lines = [
            j({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(t0), "sessionId": T_SID,
               "content": TEXT_PROMPT}),
            j({"type": "queue-operation", "operation": "dequeue", "timestamp": iso(t0 + 0.1), "sessionId": T_SID,
               "content": TEXT_PROMPT}),
            j({"type": "attachment", "uuid": "att-1", "timestamp": iso(t0 + 0.2), "sessionId": T_SID,
               "attachment": {"type": "hook_success", "content": f"hook said {SENT} in its stdout", "stdout": ""}}),
            j({"type": "file-history-snapshot", "messageId": "m1", "snapshot": {"trackedFileBackups": {}}}),
            j({"type": "user", "uuid": "u-1", "timestamp": iso(t0 + 0.5), "sessionId": T_SID,
               "message": {"role": "user",
                           "content": TEXT_PROMPT + f"\nattached file: {d}/chat-media/{PID}/shot-1.png"
                                                    f" and {d}/chat-media/{PID}/shared.png"}}),
            j({"type": "assistant", "uuid": "a-1", "timestamp": iso(t0 + 2), "sessionId": T_SID,
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": TEXT_REPLY},
                   {"type": "tool_use", "id": "toolu_1", "name": "Write",
                    "input": {"file_path": "/work/notes/out.txt", "content": "x"}},
                   {"type": "tool_use", "id": "toolu_2", "name": "Bash",
                    "input": {"command": "git commit -m wip"}},
                   {"type": "tool_use", "id": "toolu_task", "name": "Task",
                    "input": {"description": "dig", "prompt": "dig into it"}}]}}),
            j({"type": "user", "uuid": "u-2", "timestamp": iso(t0 + 3), "sessionId": T_SID,
               "message": {"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "toolu_1", "content": TEXT_TOOL}]}}),
            j({"type": "last-prompt", "lastPrompt": TEXT_PROMPT, "leafUuid": "a-1", "sessionId": T_SID}),
            j({"type": "ai-title", "aiTitle": "Delete me session", "sessionId": T_SID}),
        ]
        t.write_bytes(b"".join(lines))
        sd = self.proj_dir / T_SID
        (sd / "subagents").mkdir(parents=True)
        (sd / "subagents" / "agent-x.jsonl").write_bytes(j(
            {"type": "assistant", "isSidechain": True, "uuid": "sa-1", "timestamp": iso(t0 + 2.5),
             "message": {"role": "assistant", "content": [{"type": "text", "text": f"subagent found {SENT} too"}]}}))
        (sd / "subagents" / "agent-x.meta.json").write_text(json.dumps({"toolUseId": "toolu_task"}))
        (sd / "tool-results").mkdir()
        (sd / "tool-results" / "toolu_9.txt").write_text(f"big output with {SENT} inside\n" * 3)
        # kept session
        k = self.proj_dir / f"{K_SID}.jsonl"
        k.write_bytes(b"".join([
            j({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(t0 + 30), "sessionId": K_SID,
               "content": K_PROMPT}),
            j({"type": "user", "uuid": "ku-1", "timestamp": iso(t0 + 30.5), "sessionId": K_SID,
               "message": {"role": "user", "content": K_PROMPT + f" see {d}/chat-media/{PID}/shared.png"}}),
            j({"type": "assistant", "uuid": "ka-1", "timestamp": iso(t0 + 32), "sessionId": K_SID,
               "message": {"role": "assistant", "content": [{"type": "text", "text": K_REPLY}]}}),
        ]))
        # file-history / session-env, main + the `work` account (own copies)
        for cfg in (h / ".claude", h / ".claude-accounts" / "work"):
            for sid in (T_SID, K_SID):
                (cfg / "file-history" / sid).mkdir(parents=True)
                (cfg / "file-history" / sid / "abc@v1").write_text(f"backup {SENT if sid == T_SID else 'kept'}\n")
                (cfg / "session-env" / sid).mkdir(parents=True)
        (h / ".claude" / "debug").mkdir(exist_ok=True)
        (h / ".claude" / "debug" / f"{T_SID}.txt").write_text(f"debug {SENT}\n")
        # history.jsonl: main + account
        for cfg in (h / ".claude", h / ".claude-accounts" / "work"):
            (cfg / "history.jsonl").write_bytes(
                j({"display": TEXT_PROMPT, "pastedContents": {}, "timestamp": 1, "project": str(self.cwd),
                   "sessionId": T_SID})
                + j({"display": K_PROMPT, "pastedContents": {}, "timestamp": 2, "project": str(self.cwd),
                     "sessionId": K_SID}))
        # uploads
        media = d / "chat-media" / PID
        media.mkdir(parents=True)
        (media / "shot-1.png").write_bytes(b"png-only-target")
        (media / "shared.png").write_bytes(b"png-shared")
        (media / "other.png").write_bytes(b"png-other")
        # timeline: two interleaved chats of one project, run ids on run_start/run_end only
        tl = d / "timeline"
        tl.mkdir()
        self.timeline_path = tl / f"{str(self.cwd).replace('/', '-')}.jsonl"
        sk = "1001:42"
        def ev(off, **kw):
            return j({"ts": t0 + off, "session_key": sk, **kw})
        self.tl_target = [
            ev(0.2, kind="run_start", source="chat", prompt=TEXT_PROMPT, run_id="rT1", chat_id="c1"),
            ev(1.0, kind="tool_result", tool="Bash", output=TEXT_TOOL),
            ev(2.0, kind="text", text=TEXT_REPLY),
            ev(2.5, kind="run_end", source="chat", outcome="ok", run_id="rT1", chat_id="c1"),
        ]
        self.tl_kept = [
            ev(30.2, kind="run_start", source="chat", prompt=K_PROMPT, run_id="rK1", chat_id="c2"),
            ev(31.0, kind="tool_result", tool="Bash", output="kept output unrelated"),
            ev(32.0, kind="text", text=K_REPLY),
            ev(32.5, kind="run_end", source="chat", outcome="ok", run_id="rK1", chat_id="c2"),
        ]
        # interleave by time: target run, kept run, then a second kept-run tool line after both
        self.tl_all = self.tl_target + self.tl_kept + [ev(40.0, kind="operator_stop")]
        self.timeline_path.write_bytes(b"".join(self.tl_all))
        # audit (current month file); TASK text == short(prompt, 300)
        ad = d / "audit"
        ad.mkdir()
        self.audit_path = ad / f"audit-{time.strftime('%Y-%m', time.localtime(t0))}.log"
        self.au_target = [
            f"{local_iso(t0 + 1)} [{PID}] TASK: {TEXT_PROMPT}\n",
            f"{local_iso(t0 + 2)} [{PID}] RESULT: reply with {SENT} in it\n",
        ]
        self.au_kept = [
            f"{local_iso(t0 + 31)} [{PID}] TASK: {K_PROMPT}\n",
            f"{local_iso(t0 + 32)} [{PID}] RESULT: kept result\n",
        ]
        self.audit_path.write_text("".join(self.au_target + self.au_kept), encoding="utf-8")
        # cockpit stores
        (d / "session_labels.json").write_text(json.dumps({T_SID: "target label", K_SID: "kept label"}))
        (d / "session_links.json").write_text(json.dumps(
            {T_SID: {"prev": "99999999-0000-4000-8000-999999999999", "handoff": True},
             K_SID: {"prev": "88888888-0000-4000-8000-888888888888", "handoff": True}}))
        (d / "session_lineage.jsonl").write_bytes(
            j({"kind": "rotate", "from_provider": "claude", "from_session": "77777777-0000-4000-8000-777777777777",
               "to_provider": "claude", "to_session": T_SID, "ts": 1})
            + j({"kind": "rotate", "from_provider": "claude", "from_session": "66666666-0000-4000-8000-666666666666",
                 "to_provider": "claude", "to_session": K_SID, "ts": 2}))
        (d / "turn_marks").mkdir()
        (d / "turn_marks" / f"{T_SID}.jsonl").write_bytes(j({"run_id": "rT1", "chat_id": "c1", "offset": 0}))
        chats = {PID: {"active": "c1", "chats": [
            {"id": "c1", "name": "Main", "provider": "claude", "model": "opus", "session_id": T_SID,
             "runtime_handoff": {"text": f"handoff carrying {TEXT_REPLY}", "created_at": 1.0,
                                 "for_provider": "codex"}},
            {"id": "c2", "name": "Other", "provider": "claude", "model": "opus", "session_id": K_SID}]}}
        (d / "chats.json").write_text(json.dumps(chats, indent=2))
        # search.db (real indexer) + a doc_hit, usage.db (real scanner)
        db = d / "search.db"
        conn = _search.get_db(db)
        _search.init_db(conn)
        _search.index_transcripts(conn, PID, PID, self.proj_dir)
        _search.index_timeline_file(conn, PID, PID, self.timeline_path)
        _search.record_hits(conn, "ui", [{"ref": T_SID, "project_id": PID, "source": "chat"},
                                         {"ref": K_SID, "project_id": PID, "source": "chat"}])
        conn.close()
        usage_scanner.scan(projects_dir=h / ".claude" / "projects", db_path=d / "usage.db")
        (h / "vault" / "Journal").mkdir(parents=True)

    # ── ctx / project ───────────────────────────────────────────────────────────────────────
    def ctx(self) -> dict:
        import webapp
        ctx = {
            "topics": {"1001:42": {"project": PID, "cwd": str(self.cwd), "model": "sonnet"}},
            "sessions": {"1001:42": T_SID}, "running": {}, "password": "pw", "DATA": self.data,
            "HERE": ROOT, "VAULT_PROJECTS": None, "DEFAULT_MODEL": "sonnet",
            "MODELS": {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"},
            "save_sessions": lambda: None, "save_topics": lambda: None, "ptb_app": None,
            "rate_limits": {}, "cwd_locks": {},
        }
        ctx["_auth_token"] = webapp._derive_token("pw")
        return ctx

    def project(self) -> dict:
        return {"id": PID, "name": PID, "cwd": str(self.cwd), "session_key": "1001:42"}

    # ── snapshots ───────────────────────────────────────────────────────────────────────────
    def files(self) -> "dict[str, bytes]":
        out = {}
        for root in (self.home, self.data):
            for dp, _dns, fns in os.walk(root):
                for fn in fns:
                    p = os.path.join(dp, fn)
                    try:
                        out[p] = Path(p).read_bytes()
                    except OSError:
                        pass
        return out

    def digest_all(self) -> "dict[str, str]":
        return {p: hashlib.sha256(b).hexdigest() for p, b in self.files().items()}

    def sentinel_paths(self, token: str = SENT) -> "list[str]":
        needle = token.encode()
        return sorted(p for p, b in self.files().items() if needle in b)

    def usage_numbers(self) -> list:
        import sqlite3
        c = sqlite3.connect(self.data / "usage.db")
        try:
            return [list(c.execute(q)) for q in (
                "SELECT session_id,total_input_tokens,total_output_tokens,turn_count FROM sessions ORDER BY 1",
                "SELECT session_id,timestamp,input_tokens,output_tokens FROM turns ORDER BY id")]
        finally:
            c.close()


def make_env(world: World, **kw):
    import forget
    return forget.Env(data_dir=world.data, **kw)

"""spec-098 Forget: the Codex adapter (delete a whole thread everywhere Codex keeps it).

CODEX_HOME (env, else ``~/.codex``) is SHARED with the operator's own interactive Codex and with
``opinion --codex``. P0 (codex-cli 0.153.2, docs/internal/specs/p0/codex.md) measured:

* native ``thread/delete`` removes the rollout, the ``threads`` row and the ``logs`` rows by thread
  id, even for archived/forked threads, and is NOT refused while another process holds the thread
  (so the "Codex is in use" refusal here is ours and mandatory);
* it leaves ``shell_snapshots/<tid>.*``, stale WAL frames and freed pages (VACUUM is the only thing
  that clears them) and never looks at ``thread_history_*``, ``memories_*``, ``goals_*``,
  ``queue_*``, ``history.jsonl`` or ``session_index.jsonl``;
* a fork is a full copy of the earlier turns in its own rollout, a sub-agent is a child thread
  (``thread_spawn_edges``): the whole family goes.

Everything is local files; no model turn is needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

import forget
from forget import ContentItem, SessionArtifacts

_TID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,63}$")
_FORK_RE = re.compile(rb'"forked_from_id"\s*:\s*"([^"]+)"')
_PARENT_RE = re.compile(rb'"parent_thread_id"\s*:\s*"([^"]+)"')
_FIRST_LINE_CAP = 2 * 1024 * 1024        # a session_meta line carries the base instructions
_LINE_CAP = 16 * 1024 * 1024
_FAMILY_CAP = 500
_ID_COLUMNS = ("thread_id", "child_thread_id", "parent_thread_id")
_ALWAYS_COMPACT = ("state_", "logs_")     # P0: free pages + WAL keep the text in these two
_PROC_ROOT = Path("/proc")
_BUSY_SEC = 5.0                           # how long a locked db is waited for before it is a leftover
_OWN_PIDS: "set[int]" = set()             # codex processes this adapter itself spawned
_INTERPRETERS = ("node", "nodejs", "bun", "python", "python3")


# ------------------------------------------------------------------------------------------
# paths
# ------------------------------------------------------------------------------------------

def _home() -> Path:
    import codex_engine
    return codex_engine.codex_home()


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)
    except (OSError, ValueError):
        return False


def _dbs(home: Path) -> "list[Path]":
    try:
        return sorted(p for p in home.glob("*.sqlite") if p.is_file() and not p.is_symlink())
    except OSError:
        return []


def _state_db(home: Path) -> "Path | None":
    found = [p for p in _dbs(home) if p.name.startswith("state_")]
    return found[-1] if found else None


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_SEC)
    return conn


def _rollout_tid(path: Path) -> str:
    """``rollout-<ts>-<tid>.jsonl`` -> tid (a UUID is the last 36 chars of the stem)."""
    return path.stem[-36:]


def _rollouts(home: Path) -> "dict[str, list[Path]]":
    """tid -> rollout files under sessions/ and archived_sessions/ (symlinks never followed)."""
    out: "dict[str, list[Path]]" = {}
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if not root.is_dir() or root.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    p = Path(dirpath) / name
                    if not p.is_symlink():
                        out.setdefault(_rollout_tid(p), []).append(p)
    return out


def _first_line(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.readline(_FIRST_LINE_CAP)
    except OSError:
        return b""


def _links(rollouts: "dict[str, list[Path]]") -> "dict[str, set[str]]":
    """parent tid -> child tids that the rollouts' own first line names (fork or sub-agent)."""
    kids: "dict[str, set[str]]" = {}
    for tid, paths in rollouts.items():
        for p in paths:
            line = _first_line(p)
            for rx in (_FORK_RE, _PARENT_RE):
                m = rx.search(line)
                if m:
                    parent = m.group(1).decode("utf-8", "replace")
                    if parent != tid:
                        kids.setdefault(parent, set()).add(tid)
    return kids


def _edges(home: Path) -> "dict[str, set[str]]":
    db = _state_db(home)
    kids: "dict[str, set[str]]" = {}
    if db is None:
        return kids
    try:
        with _ro(db) as conn:
            for parent, child in conn.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                kids.setdefault(parent, set()).add(child)
    except sqlite3.Error:
        pass
    return kids


def _family(home: Path, tid: str, rollouts: "dict[str, list[Path]]") -> "tuple[list[str], list[str], list[str]]":
    """(thread ids that go: tid + its spawn children, transitive; spawn children; forks that STAY).

    A spawn child is the thread's own sub-agent and dies with it. A fork is an independent thread —
    typically the operator's own (`codex fork` in a terminal on a thread seen in the cockpit) that
    he keeps working in — so it is never deleted with its source; the plan names it instead."""
    edges = _edges(home)
    links = _links(rollouts)
    seen = [tid]
    children: "list[str]" = []
    queue = [tid]
    while queue and len(seen) < _FAMILY_CAP:
        cur = queue.pop(0)
        for kid in sorted(edges.get(cur, set())):
            if kid in seen or not _TID_RE.match(kid):
                continue
            seen.append(kid)
            queue.append(kid)
            children.append(kid)
    forks = sorted({k for t in seen for k in links.get(t, set())
                    if k not in seen and _TID_RE.match(k)})
    return seen, children, forks


def _same_dir(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a).rstrip("/") or "/") == os.path.realpath(str(b).rstrip("/") or "/")
    except (OSError, ValueError):
        return False


def _snapshots(home: Path, tids: "list[str]") -> "list[Path]":
    root = home / "shell_snapshots"
    if not root.is_dir() or root.is_symlink():
        return []
    out: "list[Path]" = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in sorted(names):
        if any(name == t or name.startswith(t + ".") for t in tids):
            p = root / name
            if not p.is_symlink() and p.is_file():
                out.append(p)
    return out


# ------------------------------------------------------------------------------------------
# helpers: running codex processes, async bridge, time
# ------------------------------------------------------------------------------------------

def _codex_processes() -> "list[tuple[int, str]]":
    me = os.getpid()
    found: "list[tuple[int, str]]" = []
    try:
        entries = os.listdir(_PROC_ROOT)
    except OSError:
        return found
    for ent in entries:
        if not ent.isdigit():
            continue
        pid = int(ent)
        if pid == me or pid in _OWN_PIDS:
            continue
        try:
            raw = (_PROC_ROOT / ent / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        exe = os.path.basename(argv[0])
        hit = exe == "codex" or exe.startswith("codex-")
        if not hit and exe.startswith(_INTERPRETERS):
            # Only the SCRIPT the interpreter runs (its first non-option argument) counts: a plain
            # argument named `codex` (`python3 check.py codex`) refused a live delete.
            script = next((a for a in argv[1:4] if not a.startswith("-")), "")
            b = os.path.basename(script)
            hit = b in ("codex", "codex.js", "codex.mjs") or b.startswith("codex-")
        if hit:
            found.append((pid, " ".join(argv)[:120]))
    return found


def _run_sync(coro_factory):
    """Run an async call from sync code: in this thread when no loop runs here, else in a helper."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    box: "dict[str, Any]" = {}

    def work() -> None:
        try:
            box["r"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["e"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("r")


def _native_delete(thread_id: str) -> None:
    """The app-server's own delete (tests replace this)."""
    import codex_engine
    _run_sync(lambda: codex_engine.delete_thread(thread_id))


def _iso(value) -> "float | None":
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------------------------------
# sweeps
# ------------------------------------------------------------------------------------------

def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sweep_plan(conn: sqlite3.Connection) -> "list[tuple[str, str, bool]]":
    """(table, column, is_like) pairs whose value names a thread: the sweep and verify share it."""
    plan: "list[tuple[str, str, bool]]" = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
        for c in _ID_COLUMNS:
            if c in cols:
                plan.append((table, c, False))
        if table == "threads" and "id" in cols:
            plan.append((table, "id", False))
        if "rollout_path" in cols and table != "threads":
            plan.append((table, "rollout_path", True))
    return plan


def _count_rows(path: Path, tids: "list[str]") -> int:
    if not tids:
        return 0
    try:
        with _ro(path) as conn:
            n = 0
            marks = ",".join("?" for _ in tids)
            for table, col, like in _sweep_plan(conn):
                if like:
                    for t in tids:
                        n += conn.execute(f"SELECT count(*) FROM {_quote(table)} WHERE {_quote(col)} LIKE ?",
                                          (f"%{t}%",)).fetchone()[0]
                else:
                    n += conn.execute(f"SELECT count(*) FROM {_quote(table)} WHERE {_quote(col)} IN ({marks})",
                                      tuple(tids)).fetchone()[0]
            return n
    except sqlite3.Error:
        return 0


def _jsonl_names(path: Path, key: str, ids: "set[str]") -> bool:
    needles = [i.encode() for i in ids]
    try:
        with open(path, "rb") as f:
            for line in f:
                if len(line) <= _LINE_CAP and any(n in line for n in needles):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get(key) in ids:
                        return True
    except OSError:
        return False
    return False


def _sweep_db(path: Path, tids: "list[str]") -> "tuple[int, str | None]":
    """Delete every row naming one of `tids` in one db: ONE transaction, secure_delete on.
    Returns (rows removed, problem). A busy db changes nothing and is reported, never raised."""
    try:
        conn = sqlite3.connect(path, timeout=_BUSY_SEC, isolation_level=None)
    except sqlite3.Error as exc:
        return 0, f"{path.name}: cannot open ({exc})"
    try:
        conn.execute("PRAGMA secure_delete=ON")
        plan = _sweep_plan(conn)
        if not plan:
            return 0, None
        removed = 0
        marks = ",".join("?" for _ in tids)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for _pass in range(2):      # a delete trigger may re-insert a bookkeeping row (queue_1)
                for table, col, like in plan:
                    if like:
                        for t in tids:
                            removed += conn.execute(
                                f"DELETE FROM {_quote(table)} WHERE {_quote(col)} LIKE ?", (f"%{t}%",)).rowcount
                    else:
                        removed += conn.execute(
                            f"DELETE FROM {_quote(table)} WHERE {_quote(col)} IN ({marks})", tuple(tids)).rowcount
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return 0, f"{path.name}: rows not removed ({exc})"
        return removed, None
    finally:
        conn.close()


def _compact(path: Path) -> "str | None":
    """wal_checkpoint(TRUNCATE) + VACUUM + checkpoint again. Returns a leftover note or None."""
    try:
        conn = sqlite3.connect(path, timeout=_BUSY_SEC, isolation_level=None)
    except sqlite3.Error as exc:
        return f"{path.name}: not compacted ({exc})"
    try:
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("VACUUM")
        busy2 = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if (busy and busy[0]) or (busy2 and busy2[0]):
            return f"{path.name}: WAL not fully checkpointed (database busy)"
        return None
    except sqlite3.Error as exc:
        return f"{path.name}: not compacted ({exc})"
    finally:
        conn.close()


def _rewrite_jsonl(path: Path, key: str, ids: "set[str]", home: Path) -> "tuple[int, str | None]":
    """Drop the lines whose `key` is in `ids`; every other line stays byte for byte. Atomic."""
    if not path.exists():
        return 0, None
    if path.is_symlink() or not _inside(path, home):
        return 0, f"{path.name}: refused (symlink)"
    needles = [i.encode() for i in ids]
    tmp = path.with_name(f".{path.name}.forget.tmp")
    dropped = 0
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            for line in src:
                if len(line) <= _LINE_CAP and any(n in line for n in needles):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        row = None
                    if isinstance(row, dict) and row.get(key) in ids:
                        dropped += 1
                        continue
                dst.write(line)
            dst.flush()
            os.fsync(dst.fileno())
        if dropped:
            try:
                os.chmod(tmp, path.stat().st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, path)
        else:
            tmp.unlink()
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return 0, f"{path.name}: not rewritten ({exc})"
    return dropped, None


def _file_has(path: Path, needles: "list[bytes]") -> bool:
    keep = max((len(n) for n in needles), default=1) - 1
    tail = b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(8 * 1024 * 1024)
                if not chunk:
                    return False
                buf = tail + chunk
                if any(n in buf for n in needles):
                    return True
                tail = buf[-keep:] if keep else b""
    except OSError:
        return False


# ------------------------------------------------------------------------------------------
# the adapter
# ------------------------------------------------------------------------------------------

class CodexAdapter:
    name = "codex"
    can_forget_turn = False

    @property
    def can_delete_session(self) -> bool:
        import codex_engine
        return bool(codex_engine.codex_enabled())

    # -- locate -------------------------------------------------------------------------------
    def locate(self, ctx: Mapping[str, Any], project: Mapping[str, Any], session_id: str) -> "SessionArtifacts | None":
        if not isinstance(session_id, str) or not _TID_RE.match(session_id):
            return None
        home = _home()
        rollouts = _rollouts(home)
        row = None
        db = _state_db(home)
        if db is not None:
            try:
                with _ro(db) as conn:
                    row = conn.execute(
                        "SELECT cwd, title, first_user_message, created_at, updated_at, rollout_path "
                        "FROM threads WHERE id = ?", (session_id,)).fetchone()
            except sqlite3.Error:
                row = None
        # ~/.codex is shared with the operator's own Codex and `opinion`: only a thread of THIS
        # project (its recorded cwd is the project's) can be deleted from this project's list
        if row is None or not _same_dir(row[0], (project or {}).get("cwd")):
            return None
        tids, children, forks = _family(home, session_id, rollouts)
        paths: "list[Path]" = []
        for t in tids:
            for p in rollouts.get(t, []):
                if p not in paths:
                    paths.append(p)
        if row and row[5]:
            rp = Path(row[5])
            if rp.is_file() and not rp.is_symlink() and _inside(rp, home) and rp not in paths:
                paths.append(rp)
        paths.extend(_snapshots(home, tids))
        first = last = None
        title = ""
        cwd = str((project or {}).get("cwd") or "")
        if row:
            cwd = row[0] or cwd
            title = forget.session_title(row[1], row[2])
            first, last = _iso(row[3]), _iso(row[4])
        return SessionArtifacts(
            provider="codex", session_id=session_id, cwd=cwd, title=title, paths=paths,
            first_ts=first, last_ts=last,
            extra={"home": str(home), "threads": tids, "children": children, "forks": forks,
                   "kept_copies": [{"kind": "fork", "id": f} for f in forks]})

    # -- busy ---------------------------------------------------------------------------------
    def busy_reason(self, ctx: Mapping[str, Any], project: Mapping[str, Any], session_id: str) -> "str | None":
        procs = _codex_processes()
        if procs:
            pid, cmd = procs[0]
            more = f" and {len(procs) - 1} more" if len(procs) > 1 else ""
            return (f"Codex is in use (pid {pid}: {cmd}{more}). ~/.codex is shared with your own Codex "
                    "and `opinion --codex`; close them and retry.")
        return None

    # -- content ------------------------------------------------------------------------------
    def read_content(self, art: SessionArtifacts) -> Iterator[ContentItem]:
        for path in art.paths:
            if path.name.startswith("rollout-") and path.suffix == ".jsonl":
                yield from self._read_rollout(path)

    @staticmethod
    def _text_parts(content) -> str:
        if isinstance(content, str):
            return content
        out = []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    out.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append(part["text"])
        return "\n".join(out)

    def _read_rollout(self, path: Path) -> Iterator[ContentItem]:
        try:
            fh = open(path, "rb")
        except OSError:
            return
        with fh:
            while True:
                line = fh.readline(_LINE_CAP)
                if not line:
                    return
                if len(line) >= _LINE_CAP and not line.endswith(b"\n"):
                    while True:                         # discard the rest of a giant line
                        rest = fh.readline(_LINE_CAP)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts = _iso(row.get("timestamp"))
                p = row.get("payload")
                if not isinstance(p, dict):
                    continue
                typ, sub = row.get("type"), p.get("type")
                if typ == "event_msg" and sub == "user_message" and isinstance(p.get("message"), str):
                    yield ContentItem("prompt", p["message"], ts)
                elif typ == "response_item" and sub == "message" and p.get("role") == "assistant":
                    text = self._text_parts(p.get("content"))
                    if text:
                        yield ContentItem("assistant", text, ts, uuid=p.get("id"))
                elif typ == "response_item" and sub in ("function_call", "custom_tool_call", "local_shell_call"):
                    yield self._tool_use(p, sub, ts)
                elif typ == "response_item" and sub in ("function_call_output", "custom_tool_call_output"):
                    out = p.get("output")
                    text = out if isinstance(out, str) else self._text_parts(out)
                    yield ContentItem("tool_result", text or "", ts, uuid=p.get("call_id"))
                elif typ == "compacted" and isinstance(p.get("message"), str):
                    yield ContentItem("summary", p["message"], ts)

    @staticmethod
    def _tool_use(p: dict, sub: str, ts) -> ContentItem:
        uid = p.get("call_id") or p.get("id")
        if sub == "function_call":
            args = p.get("arguments")
            parsed = None
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except ValueError:
                    parsed = None
            inp = parsed if isinstance(parsed, dict) else {"arguments": args}
            return ContentItem("tool_use", args if isinstance(args, str) else "", ts, tool=p.get("name"),
                               tool_input=inp, uuid=uid)
        if sub == "custom_tool_call":
            text = p.get("input") if isinstance(p.get("input"), str) else ""
            return ContentItem("tool_use", text, ts, tool=p.get("name"), tool_input={"input": text}, uuid=uid)
        action = p.get("action") if isinstance(p.get("action"), dict) else {}
        cmd = action.get("command")
        text = " ".join(cmd) if isinstance(cmd, list) else (cmd if isinstance(cmd, str) else "")
        return ContentItem("tool_use", text, ts, tool="shell", tool_input=action or {"command": text}, uuid=uid)

    # -- delete -------------------------------------------------------------------------------
    def delete(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict:
        home = Path(art.extra.get("home") or _home())
        tids = [t for t in art.extra.get("threads") or [art.session_id] if _TID_RE.match(str(t))]
        res = {"threads": 0, "files": 0, "rows": 0, "lines": 0, "compacted": [], "leftovers": [],
               "native_errors": []}
        # 1. the app-server's own delete, child threads first, then the thread
        for tid in reversed(tids):
            try:
                _native_delete(tid)
                res["threads"] += 1
            except Exception as exc:  # noqa: BLE001 - not found = already gone; the hand sweep follows
                res["native_errors"].append(f"{tid}: {exc}"[:200])
        # 2. files the native delete leaves (or all of them when it failed): never outside the home
        files = list(art.paths) + _snapshots(home, tids)
        for tids_rollouts in _rollouts(home).items():
            if tids_rollouts[0] in tids:
                files.extend(tids_rollouts[1])
        seen: "set[Path]" = set()
        for p in files:
            if p in seen:
                continue
            seen.add(p)
            if not os.path.lexists(p):
                continue
            if p.is_symlink() or not _inside(p, home):
                res["leftovers"].append(f"{p.name}: refused (symlink or outside the Codex home)")
                continue
            try:
                p.unlink()
                res["files"] += 1
            except OSError as exc:
                res["leftovers"].append(f"{p.name}: {exc}")
        # 3. rows, one transaction per db
        touched: "list[Path]" = []
        for db in _dbs(home):
            n, problem = _sweep_db(db, tids)
            res["rows"] += n
            if problem:
                res["leftovers"].append(problem)
            if n or db.name.startswith(_ALWAYS_COMPACT):
                touched.append(db)
        # 4. prompt/index lines
        idset = set(tids)
        for name, key in (("history.jsonl", "session_id"), ("session_index.jsonl", "id")):
            n, problem = _rewrite_jsonl(home / name, key, idset, home)
            res["lines"] += n
            if problem:
                res["leftovers"].append(problem)
        # 5. free pages + WAL keep the text; only VACUUM clears them
        for db in touched:
            note = _compact(db)
            if note:
                res["leftovers"].append(note)
            else:
                res["compacted"].append(db.name)
        return res

    # -- verify -------------------------------------------------------------------------------
    def verify_gone(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> "list[str]":
        home = Path(art.extra.get("home") or _home())
        tids = [t for t in art.extra.get("threads") or [art.session_id] if _TID_RE.match(str(t))]
        left: "list[str]" = []
        rollouts = _rollouts(home)
        for t in tids:
            left.extend(str(p) for p in rollouts.get(t, []))
        left.extend(str(p) for p in art.paths if os.path.lexists(p) and str(p) not in left)
        left.extend(str(p) for p in _snapshots(home, tids))
        # The same predicates the sweep used: rows whose id COLUMNS name a thread, jsonl lines whose
        # id FIELD does. A raw-bytes test also hit other threads' rows that merely mention the id (a
        # prompt "look at thread …", the app-server's own log line of the delete) and left the
        # purge stuck after the rollout was already gone. Bytes left in free pages / WAL are the
        # residue scan's business (reported, not a failure).
        idset = set(tids)
        for db in _dbs(home):
            n = _count_rows(db, tids)
            if n:
                left.append(f"{db} ({n} row(s) still name the thread)")
        for name, key in (("history.jsonl", "session_id"), ("session_index.jsonl", "id")):
            p = home / name
            if p.is_file() and _jsonl_names(p, key, idset):
                left.append(f"{p} (a line of the thread is still there)")
        return left

    def residue_roots(self, ctx: Mapping[str, Any]) -> "list[Path]":
        return [_home()]


ADAPTER = forget.register_adapter(CodexAdapter())

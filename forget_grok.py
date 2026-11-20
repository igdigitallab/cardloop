"""spec-098 Forget: the Grok adapter (delete a whole session from Cardloop's own GROK_HOME).

P0 (grok 1.0.46, docs/internal/specs/p0/grok.md) measured that the ACP call `_x.ai/session/delete`
is the same as removing `sessions/<url-encoded cwd>/<sid>/`, and that it leaves the verbatim prompts
in the per-cwd `prompt_history.jsonl`, the text in the global `sessions/session_search.sqlite`
(FTS segments / free pages until a rebuild + VACUUM) and the cockpit's `grok_sent` / `grok_sessions`
files. No model turn and no ACP process is needed, so this adapter does all of it directly.

⚠️ A Grok session file is MODEL-WRITABLE (GOTCHAS.md §Grok): nothing here follows a symlink, and every
path is resolved and checked to stay inside GROK_HOME/sessions (or the cockpit data dir) before it is
removed. `logs/unified.jsonl` keeps the session id and cwd (no text) and is left alone, as P0 found it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import urllib.parse
from pathlib import Path
from typing import Any, Iterator, Mapping

import forget
import grok_engine
import grok_history
import grok_jsonl
from forget import ContentItem, SessionArtifacts

_LINE_CAP = 16 * 1024 * 1024
_BUSY_SEC = 5.0                           # how long a locked db is waited for before it is a leftover
_PROC_ROOT = Path("/proc")
_SEARCH_DB = "session_search.sqlite"
_SESSION_FILES = ("chat_history.jsonl", "updates.jsonl")


# ------------------------------------------------------------------------------------------
# paths
# ------------------------------------------------------------------------------------------

def _home(ctx) -> Path:
    return grok_history._home(grok_engine.grok_home(dict(ctx) if ctx else None))


def _data(ctx) -> Path:
    return grok_engine.data_dir(dict(ctx) if ctx else None)


def _is_dir(p: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except OSError:
        return False


def _is_reg(p: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def _session_dirs(home: Path, sid: str, cwd: str) -> "list[Path]":
    """Every real `<group>/<sid>` directory under sessions/ (the project's cwd group first; a scan of
    the group names catches a session started from another cwd spelling)."""
    root = home / "sessions"
    if not _is_dir(root):
        return []
    root_real = root.resolve()
    found: "list[Path]" = []
    if cwd:
        try:
            cand = grok_history._session_dir(sid, cwd, home)
        except ValueError:
            cand = None
        if cand is not None:
            found.append(cand)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        cand = root / name / sid
        if cand in found:
            continue
        if _is_dir(root / name) and _is_dir(cand) and grok_history._inside(cand, root_real):
            found.append(cand)
    return found


def _group_cwd(sdir: Path) -> str:
    group = sdir.parent
    if group.name.lower().startswith("%2f"):
        return urllib.parse.unquote(group.name)
    return grok_history._cwd_file(group) or ""


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_SEC)


def _same_dir(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a).rstrip("/") or "/") == os.path.realpath(str(b).rstrip("/") or "/")
    except (OSError, ValueError):
        return False


def _jsonl_has_session(path: Path, sid: str) -> bool:
    """The rewrite's own predicate: a line of THIS session (`session_id == sid`), not any line that
    merely quotes the id (another session's prompt about it)."""
    needle = sid.encode()
    try:
        with open(path, "rb") as f:
            for line in f:
                if needle in line and len(line) <= _LINE_CAP:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("session_id") == sid:
                        return True
    except OSError:
        return False
    return False


def _search_rows_left(db: Path, sid: str) -> "int | None":
    """Rows the sweep would delete (non-FTS tables with a session_id column); None = cannot tell."""
    try:
        with _ro(db) as conn:
            n = 0
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if table.startswith("sqlite_") or "_fts" in table:
                    continue
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                if "session_id" in cols:
                    n += conn.execute(f'SELECT count(*) FROM "{table}" WHERE session_id = ?', (sid,)).fetchone()[0]
            return n
    except sqlite3.Error:
        return None


def _search_row(home: Path, sid: str) -> "tuple | None":
    db = home / "sessions" / _SEARCH_DB
    if not _is_reg(db):
        return None
    try:
        with _ro(db) as conn:
            return conn.execute("SELECT cwd, title, updated_at FROM session_docs WHERE session_id = ?",
                                (sid,)).fetchone()
    except sqlite3.Error:
        return None


# ------------------------------------------------------------------------------------------
# sweeps
# ------------------------------------------------------------------------------------------

def _rewrite_jsonl(path: Path, sid: str, sessions_real: Path) -> "tuple[int, str | None]":
    """Drop the lines whose `session_id` is `sid`; every other line stays byte for byte. Atomic."""
    if not os.path.lexists(path):
        return 0, None
    if not _is_reg(path) or not grok_history._inside(path, sessions_real):
        return 0, f"{path.name}: refused (symlink or outside the Grok home)"
    needle = sid.encode()
    tmp = path.with_name(f".{path.name}.forget.tmp")
    dropped = 0
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            for line in src:
                if needle in line and len(line) <= _LINE_CAP:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        row = None
                    if isinstance(row, dict) and row.get("session_id") == sid:
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


def _sweep_search_db(db: Path, sid: str) -> "tuple[int, str | None]":
    """Delete the session's row(s) (secure_delete on, one transaction), rebuild + optimize the FTS index
    when there is one, VACUUM, checkpoint. A busy db is a leftover note, never an exception."""
    try:
        conn = sqlite3.connect(db, timeout=_BUSY_SEC, isolation_level=None)
    except sqlite3.Error as exc:
        return 0, f"{db.name}: cannot open ({exc})"
    try:
        conn.execute("PRAGMA secure_delete=ON")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        removed = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in sorted(tables):
                if table.startswith("sqlite_") or "_fts" in table:
                    continue
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                if "session_id" in cols:
                    removed += conn.execute(f'DELETE FROM "{table}" WHERE session_id = ?', (sid,)).rowcount
            for fts in sorted(t for t in tables if t.endswith("_fts")):
                conn.execute(f'INSERT INTO "{fts}"("{fts}") VALUES (\'rebuild\')')
                conn.execute(f'INSERT INTO "{fts}"("{fts}") VALUES (\'optimize\')')
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return 0, f"{db.name}: rows not removed ({exc})"
        try:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.execute("VACUUM")
            busy2 = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            return removed, f"{db.name}: not compacted ({exc})"
        if (busy and busy[0]) or (busy2 and busy2[0]):
            return removed, f"{db.name}: WAL not fully checkpointed (database busy)"
        return removed, None
    finally:
        conn.close()


def _file_has(path: Path, needle: bytes) -> bool:
    tail = b""
    keep = len(needle) - 1
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(8 * 1024 * 1024)
                if not chunk:
                    return False
                buf = tail + chunk
                if needle in buf:
                    return True
                tail = buf[-keep:] if keep > 0 else b""
    except OSError:
        return False


def _grok_processes() -> "list[tuple[int, str, str, str]]":
    """(pid, cmdline, cwd, ...) of every running grok process."""
    found = []
    me = os.getpid()
    try:
        entries = os.listdir(_PROC_ROOT)
    except OSError:
        return found
    for ent in entries:
        if not ent.isdigit() or int(ent) == me:
            continue
        try:
            raw = (_PROC_ROOT / ent / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        exe = os.path.basename(argv[0])
        if not (exe == "grok" or exe.startswith("grok-")):
            continue
        try:
            cwd = os.readlink(_PROC_ROOT / ent / "cwd")
        except OSError:
            cwd = ""
        found.append((int(ent), " ".join(argv), cwd, ""))
    return found


# ------------------------------------------------------------------------------------------
# the adapter
# ------------------------------------------------------------------------------------------

class GrokAdapter:
    name = "grok"
    can_forget_turn = False

    @property
    def can_delete_session(self) -> bool:
        return bool(grok_engine.grok_enabled())

    # -- locate -------------------------------------------------------------------------------
    def locate(self, ctx: Mapping[str, Any], project: Mapping[str, Any], session_id: str) -> "SessionArtifacts | None":
        if not grok_history.valid_session_id(session_id):
            return None
        try:
            home = _home(ctx)
        except grok_history.GrokHistoryError:
            return None
        data = _data(ctx)
        proj_cwd = str((project or {}).get("cwd") or "")
        if not os.path.isabs(proj_cwd):
            return None
        # sessions/ and its search index are shared by every project: only a session whose recorded
        # cwd is THIS project's can be deleted from this project's list
        dirs = [d for d in _session_dirs(home, session_id, proj_cwd) if _same_dir(_group_cwd(d), proj_cwd)]
        row = _search_row(home, session_id)
        if row is not None and not _same_dir(row[0], proj_cwd):
            row = None
        if not dirs and row is None:
            return None
        sent = data / "grok_sent" / session_id
        bind = data / "grok_sessions" / session_id
        extras = [p for p in (sent, bind) if os.path.lexists(p)]
        paths: "list[Path]" = list(dirs) + extras
        cwd = (_group_cwd(dirs[0]) if dirs else "") or (row[0] if row else "") or proj_cwd
        title, first, last = "", None, None
        if dirs:
            summary = grok_history._read_json(dirs[0] / "summary.json") or {}
            title = forget.session_title(summary.get("title"), summary.get("session_summary"))
            first = grok_history._iso_to_epoch(summary.get("created_at"))
            last = grok_history._iso_to_epoch(summary.get("updated_at"))
        if not title and row:
            title = forget.session_title(row[1])
        if last is None and row and isinstance(row[2], (int, float)):
            last = float(row[2])
        groups = sorted({str(d.parent.name) for d in dirs})
        return SessionArtifacts(
            provider="grok", session_id=session_id, cwd=cwd, title=title, paths=paths,
            first_ts=first, last_ts=last,
            extra={"home": str(home), "data": str(data), "groups": groups,
                   "cwds": sorted({c for c in (_group_cwd(d) for d in dirs) if c} | ({proj_cwd} if proj_cwd else set()))})

    # -- busy ---------------------------------------------------------------------------------
    def busy_reason(self, ctx: Mapping[str, Any], project: Mapping[str, Any], session_id: str) -> "str | None":
        cwds = set()
        proj_cwd = str((project or {}).get("cwd") or "")
        if os.path.isabs(proj_cwd):
            cwds.update({proj_cwd, os.path.normpath(proj_cwd), os.path.realpath(proj_cwd)})
        try:
            home = _home(ctx)
            for d in _session_dirs(home, session_id, proj_cwd if os.path.isabs(proj_cwd) else ""):
                c = _group_cwd(d)
                if c:
                    cwds.update({c, os.path.normpath(c), os.path.realpath(c)})
        except (grok_history.GrokHistoryError, ValueError):
            pass
        for pid, cmd, cwd, _ in _grok_processes():
            if session_id in cmd or (cwd and cwd.rstrip("/") in {c.rstrip("/") for c in cwds}):
                return (f"Grok is running in this project (pid {pid}); wait for it to finish or stop it, "
                        "then retry.")
        return None

    # -- content ------------------------------------------------------------------------------
    def read_content(self, art: SessionArtifacts) -> Iterator[ContentItem]:
        for sdir in art.paths:
            if not (sdir.name == art.session_id and _is_dir(sdir)):
                continue
            summary = grok_history._read_json(sdir / "summary.json") or {}
            text = " ".join(str(summary.get(k)) for k in ("title", "session_summary") if summary.get(k))
            if text:
                yield ContentItem("summary", text)
            for _off, row in grok_jsonl.iter_jsonl(sdir / "chat_history.jsonl", max_bytes=1 << 62,
                                                   max_line=_LINE_CAP, from_head=True):
                kind = row.get("type")
                if kind == "user":
                    q = grok_history.user_query_text(row)
                    if q:
                        yield ContentItem("prompt", q)
                elif kind == "assistant":
                    text = grok_history.assistant_text(row)
                    if text.strip():
                        yield ContentItem("assistant", text)
                    for call in row.get("tool_calls") or []:
                        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                            continue
                        args = call.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = None
                        name, inp = grok_engine.map_tool(call["name"], args)
                        inp = inp if isinstance(inp, dict) else {}
                        yield ContentItem("tool_use", json.dumps(inp, ensure_ascii=False), tool=name,
                                          tool_input=inp, uuid=call.get("id"))
                elif kind == "tool_result":
                    content = row.get("content")
                    text = content if isinstance(content, str) else "\n".join(grok_history._parts_text(content))
                    yield ContentItem("tool_result", text, uuid=row.get("tool_call_id"))

    # -- delete -------------------------------------------------------------------------------
    def delete(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict:
        sid = art.session_id
        if not grok_history.valid_session_id(sid):
            raise ValueError("invalid grok session id")
        home = Path(art.extra.get("home") or _home(ctx))
        data = Path(art.extra.get("data") or _data(ctx))
        res = {"dirs": 0, "files": 0, "lines": 0, "rows": 0, "leftovers": []}
        sessions = home / "sessions"
        if os.path.lexists(sessions) and not _is_dir(sessions):
            res["leftovers"].append("sessions: refused (symlink)")
            return res
        if not _is_dir(sessions):
            sessions_real = sessions
        else:
            sessions_real = sessions.resolve()
        # 1. the session directories (re-derived, not trusted from the record)
        dirs = {p for p in art.paths if p.name == sid and p.parent.parent == sessions}
        dirs |= set(_session_dirs(home, sid, ""))
        for d in sorted(dirs):
            if not _is_dir(d) or not grok_history._inside(d, sessions_real):
                if os.path.lexists(d):
                    res["leftovers"].append(f"{d.name}: refused (symlink or outside the Grok home)")
                continue
            try:
                grok_engine._unlock_tree(str(d))
                shutil.rmtree(d)
                res["dirs"] += 1
            except OSError as exc:
                res["leftovers"].append(f"{d}: {exc}")
        # 2. prompt_history.jsonl of every group the session lived in
        groups = {d.parent for d in dirs}
        for c in art.extra.get("cwds") or []:
            try:
                groups.update(grok_history._group_dirs(sessions, sessions_real, c))
            except (ValueError, OSError, grok_history.GrokHistoryError):
                pass
        for g in sorted(groups):
            if _is_dir(g) and grok_history._inside(g, sessions_real):
                n, problem = _rewrite_jsonl(g / "prompt_history.jsonl", sid, sessions_real)
                res["lines"] += n
                if problem:
                    res["leftovers"].append(problem)
        # 3. the global search index
        db = sessions / _SEARCH_DB
        if _is_reg(db):
            n, problem = _sweep_search_db(db, sid)
            res["rows"] += n
            if problem:
                res["leftovers"].append(problem)
        # 4. cockpit side
        data_real = data.resolve() if _is_dir(data) else data
        for sub in ("grok_sent", "grok_sessions"):
            f = data / sub / sid
            if not os.path.lexists(f):
                continue
            if not _is_reg(f) or not grok_history._inside(f, data_real):
                res["leftovers"].append(f"{sub}/{sid}: refused (symlink or outside the data dir)")
                continue
            try:
                f.unlink()
                res["files"] += 1
            except OSError as exc:
                res["leftovers"].append(f"{sub}/{sid}: {exc}")
        return res

    # -- verify -------------------------------------------------------------------------------
    def verify_gone(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> "list[str]":
        sid = art.session_id
        home = Path(art.extra.get("home") or _home(ctx))
        data = Path(art.extra.get("data") or _data(ctx))
        left: "list[str]" = []
        for d in _session_dirs(home, sid, ""):
            left.append(str(d))
        for sub in ("grok_sent", "grok_sessions"):
            f = data / sub / sid
            if os.path.lexists(f):
                left.append(str(f))
        # The sweep's own predicates: a prompt_history line whose session_id is this one, a search
        # row keyed by it. A raw id substring also hit OTHER sessions that merely mention the id and
        # left the purge stuck after the session dir was gone; bytes in free pages are the residue
        # scan's business, and a busy index is not a failure.
        sessions = home / "sessions"
        if _is_dir(sessions):
            for g in sorted(os.listdir(sessions)):
                ph = sessions / g / "prompt_history.jsonl"
                if _is_reg(ph) and _jsonl_has_session(ph, sid):
                    left.append(f"{ph} (a line of the session is still there)")
            db = sessions / _SEARCH_DB
            if _is_reg(db):
                n = _search_rows_left(db, sid)
                if n:
                    left.append(f"{db} ({n} row(s) of the session are still there)")
        return left

    def residue_roots(self, ctx: Mapping[str, Any]) -> "list[Path]":
        return [_home(ctx)]


ADAPTER = forget.register_adapter(GrokAdapter())

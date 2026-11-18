"""spec-098: per-run byte offsets into a Claude transcript, so "forget from here" can truncate it.

One JSON line per Claude run in `<data>/turn_marks/<session_id>.jsonl`:
`{run_id, chat_id, session_key, source, session_id, offset, ts_start}`; a second line
`{run_id, ts_end}` may follow at run end. `offset` is the transcript's byte size measured BEFORE the
prompt reached the CLI (0 for a brand-new session). No conversation text is ever stored here.
Blocking file IO, tiny files. Nothing here may break a run: callers wrap every call.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def transcript_path(cwd: str, session_id: str) -> Path:
    """`~/.claude/projects/<slug>/<sid>.jsonl` (same slug rule as webapp._sdk_sessions_dir)."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def transcript_size(cwd: str, session_id: str) -> "int | None":
    """Current byte size of the transcript, or None when it does not exist / cannot be read."""
    try:
        return transcript_path(cwd, session_id).stat().st_size
    except OSError:
        return None


def _file(data_dir, session_id: str) -> "Path | None":
    sid = str(session_id or "")
    if not _SID_RE.match(sid):
        return None
    return Path(data_dir) / "turn_marks" / f"{sid}.jsonl"


def _append(data_dir, session_id: str, row: dict) -> None:
    path = _file(data_dir, session_id)
    if path is None:
        return
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def record(data_dir, *, run_id: str, session_id: str, offset: int, session_key: "str | None" = None,
           chat_id: "str | None" = None, source: "str | None" = None,
           ts_start: "float | None" = None) -> None:
    """Append the start mark of `run_id`. Idempotent per run_id: a second start mark is skipped."""
    if not run_id or not session_id:
        return
    if any(r.get("run_id") == run_id and "offset" in r for r in read(data_dir, session_id)):
        return
    _append(data_dir, session_id, {
        "run_id": run_id, "chat_id": chat_id, "session_key": session_key, "source": source,
        "session_id": session_id, "offset": int(offset),
        "ts_start": ts_start if ts_start is not None else time.time(),
    })


def record_end(data_dir, *, run_id: str, session_id: str, ts_end: "float | None" = None) -> None:
    if not run_id or not session_id:
        return
    _append(data_dir, session_id, {"run_id": run_id, "ts_end": ts_end if ts_end is not None else time.time()})


def read(data_dir, session_id: str) -> "list[dict]":
    """Every row of the session's mark file (start marks and end rows), in write order."""
    path = _file(data_dir, session_id)
    if path is None or not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def drop(data_dir, session_id: str) -> bool:
    """Delete the session's mark file. True when a file was removed."""
    path = _file(data_dir, session_id)
    if path is None:
        return False
    with _LOCK:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False

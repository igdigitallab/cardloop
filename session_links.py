"""Which Claude session a chat's session continued from, after its context was reset.

A rotation (↺, auto-rotation at the context limit, or "New session") clears a chat's session id;
the next turn starts a session the SDK names only once it answers. The chat feed reads one
transcript per session, so without a record of the predecessor it ends at the reset.

`data/session_links.json` maps a session id to {prev, trigger, at, context_tokens, handoff,
inferred}: written when the first turn of a fresh session names it (see webapp._bind_rotation),
or when the predecessor of an older session is recognised from its handoff (inferred=true).
Session ids are SDK UUIDs, unique across projects, so the map is flat.

Thread-safe; reads are cached until the file changes.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import fsutil

FILE = "session_links.json"

_LOCK = threading.Lock()
_MEM: dict[str, tuple[int, dict]] = {}  # path -> (mtime_ns, data)


def _path(data_dir: "str | os.PathLike[str]") -> str:
    return str(Path(data_dir) / FILE)


def _load(path: str) -> dict:
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    hit = _MEM.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _MEM[path] = (mtime, data)
    return data


def get(data_dir: "str | os.PathLike[str]", session_id: str) -> "dict | None":
    """The link record of `session_id`, or None."""
    with _LOCK:
        rec = _load(_path(data_dir)).get(session_id)
    return dict(rec) if isinstance(rec, dict) and rec.get("prev") else None


def put(data_dir: "str | os.PathLike[str]", session_id: str, rec: dict) -> None:
    """Record that `session_id` continued from `rec["prev"]`. An existing record is kept unless
    the new one is first-hand (not inferred) — what the cockpit saw beats what it recognised."""
    if not session_id or not rec.get("prev") or rec["prev"] == session_id:
        return
    path = _path(data_dir)
    with _LOCK:
        data = dict(_load(path))
        old = data.get(session_id)
        if isinstance(old, dict) and (rec.get("inferred") or not old.get("inferred")):
            return
        data[session_id] = rec
        fsutil.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1))
        _MEM.pop(path, None)

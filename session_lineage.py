"""spec-098: `data/session_lineage.jsonl` — one line per DELIVERED handoff between sessions.

`{kind: "runtime_handoff"|"rotate", from_provider, from_session, to_provider, to_session, ts}`.
Lets the forget dialog warn that deleting a session also leaves a copy of its text in a successor.
No text is stored. `record` never raises into a run; `remove_session` rewrites atomically.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
FILE_NAME = "session_lineage.jsonl"


def _path(data_dir) -> Path:
    return Path(data_dir) / FILE_NAME


def record(data_dir, **row) -> None:
    """Append one lineage row (`ts` defaults to now). Never raises."""
    try:
        row.setdefault("ts", time.time())
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _LOCK:
            p = _path(data_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _read_unlocked(data_dir) -> "list[dict]":
    out: list[dict] = []
    try:
        with open(_path(data_dir), "r", encoding="utf-8", errors="replace") as f:
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


def read(data_dir) -> "list[dict]":
    return _read_unlocked(data_dir)


def remove_session(data_dir, provider: str, session_id: str) -> int:
    """Drop every row naming (provider, session_id) as source or target; returns rows removed."""
    with _LOCK:
        rows = _read_unlocked(data_dir)
        keep = [r for r in rows
                if not ((r.get("from_provider") == provider and r.get("from_session") == session_id)
                        or (r.get("to_provider") == provider and r.get("to_session") == session_id))]
        removed = len(rows) - len(keep)
        if removed:
            p = _path(data_dir)
            tmp = p.with_name(p.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for r in keep:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
        return removed

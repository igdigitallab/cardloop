"""Per-transcript stats for the Claude session list, cached across requests.

The session list shows, for each of a project's newest transcripts, a message count and the last
assistant turn (context size, time, cache-hit %). Both need a pass over the whole file, and
transcripts run to hundreds of MB: a cold cockpit load, where every open project tab asks for its
list at once, used to parse gigabytes of JSONL and hold every other request for minutes.

A transcript is append-only JSONL, so this module remembers how far it has read each file and,
once the file has grown, parses only the new complete lines. A file that shrank, was replaced, or
changed without growing is scanned again from the start.

Blocking IO: call from an executor, never on the event loop.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# path -> {"ino", "size", "mtime_ns", "offset", "state", "result"}. Entries are replaced, never
# mutated, so a reader on another thread always sees a consistent one.
_CACHE: dict[str, dict] = {}
_CACHE_MAX = 5000

_loads = json.loads


def _empty_state() -> dict:
    return {"count": 0, "context_tokens": 0, "ts_ms": None, "pct": None}


def _empty_result() -> dict:
    return {"message_count": 0, "context_tokens": 0, "last_turn_at_ms": None, "last_cache_hit_pct": None}


def _num(v) -> int:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _ts_ms(raw) -> "int | None":
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.rstrip("Z"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _apply(state: dict, raw: bytes) -> None:
    """Fold one transcript line into `state` (in place)."""
    line = raw.strip()
    if not line or (b'"message"' not in line and b'"assistant"' not in line):
        return
    try:
        o = _loads(line.decode("utf-8", "replace"))
    except ValueError:
        return
    if not isinstance(o, dict):
        return
    msg = o.get("message")
    if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
        state["count"] += 1
    if o.get("type") != "assistant":
        return
    u = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(u, dict):
        u = {}
    fresh = _num(u.get("input_tokens"))
    read = _num(u.get("cache_read_input_tokens"))
    prompt = fresh + read + _num(u.get("cache_creation_input_tokens"))
    if prompt:
        state["context_tokens"] = prompt
    state["ts_ms"] = _ts_ms(o.get("timestamp"))
    # Hit ratio counts the read side only; cache_creation is the write side.
    state["pct"] = round(read / (read + fresh) * 100) if read + fresh > 0 else None


def _result(state: dict, mtime: float) -> dict:
    ts_ms = state["ts_ms"]
    if ts_ms is None and state["pct"] is not None:
        ts_ms = int(mtime * 1000)  # an assistant turn without a parseable timestamp
    return {"message_count": state["count"], "context_tokens": state["context_tokens"],
            "last_turn_at_ms": ts_ms, "last_cache_hit_pct": state["pct"]}


def stats(path: "str | Path") -> dict:
    """{message_count, context_tokens, last_turn_at_ms, last_cache_hit_pct} of one transcript.

    message_count     — lines whose `message.role` is user or assistant.
    context_tokens    — prompt tokens of the last assistant turn that reported usage
                        (input + cache_read + cache_creation); 0 if none.
    last_turn_at_ms   — the last assistant line's `timestamp`, or the file mtime when that
                        turn has usage but no parseable timestamp; None without an assistant turn.
    last_cache_hit_pct — round(cache_read / (cache_read + input) * 100) of the last assistant
                        line; None when it has no usage.
    Never raises; a missing or unreadable file is empty stats.
    """
    key = str(path)
    try:
        st = os.stat(key)
    except OSError:
        return _empty_result()
    ent = _CACHE.get(key)
    if ent is not None and ent["ino"] == st.st_ino and ent["size"] == st.st_size \
            and ent["mtime_ns"] == st.st_mtime_ns:
        return dict(ent["result"])
    try:
        with open(key, "rb") as f:
            state, offset = _empty_state(), 0
            if ent is not None and ent["ino"] == st.st_ino and st.st_size > ent["size"] \
                    and 0 < ent["offset"] <= st.st_size:
                # Grown in place: resume after the last complete line, provided the byte before it
                # is still the newline it was (a cheap check that the file was not replaced).
                f.seek(ent["offset"] - 1)
                if f.read(1) == b"\n":
                    state, offset = dict(ent["state"]), ent["offset"]
            f.seek(offset)
            tail = b""
            for raw in f:
                if raw.endswith(b"\n"):
                    _apply(state, raw)
                    offset += len(raw)
                else:
                    tail = raw  # a line still being written: counted now, re-read next time
    except OSError:
        return _empty_result()
    final = state
    if tail:
        final = dict(state)
        _apply(final, tail)
    result = _result(final, st.st_mtime)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                   "offset": offset, "state": state, "result": result}
    return dict(result)

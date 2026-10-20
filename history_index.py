"""Where each chat-feed message sits in a Claude transcript, cached across requests.

The chat feed is built from a session transcript (append-only JSONL, often hundreds of MB): one
line yields at most one feed message, most lines (tool results, bookkeeping) yield none. Every
history request used to parse the whole file to return 100 messages — on every hydrate, and again
for every page while the operator scrolls back.

This module parses a file once and remembers, per feed message, the byte offset of its line, its
role, timestamp and uuid. A page then re-reads only its own lines; a grown file is read from the
last complete line on. A file that shrank, was replaced, or no longer parses where the index says
a message starts is indexed again from byte 0.

What counts as a message is decided by the caller's `parse(obj) -> dict | None`, which must depend
on the line alone — it runs once when a line is indexed and again when the line is read back.

Blocking IO: call from an executor, never on the event loop.
"""
from __future__ import annotations

import json
import os
import threading
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Callable

Parse = Callable[[dict], "dict | None"]

_NO_TS = -(2 ** 63)
_ROLE_CODE = {"user": 1, "assistant": 2}
_CACHE_MAX = 64

# path -> _Index. Entries are replaced, never mutated after they are published, so a reader on
# another thread always sees a consistent one.
_CACHE: "OrderedDict[str, _Index]" = OrderedDict()
_LOCK = threading.Lock()


class _Index:
    __slots__ = ("ino", "size", "mtime_ns", "offset", "offsets", "ts", "roles", "uuids",
                 "first_user", "tail")

    def __init__(self) -> None:
        self.ino = self.size = self.mtime_ns = self.offset = 0
        self.offsets = array("q")   # byte offset of each message's line
        self.ts = array("q")        # epoch ms, _NO_TS when absent
        self.roles = bytearray()    # _ROLE_CODE of each message (0 = other)
        self.uuids: dict[str, int] = {}  # first message carrying each uuid
        self.first_user: "int | None" = None  # offset of the first user line with string content
        self.tail: "dict | None" = None  # message parsed from an unterminated last line

    def copy(self) -> "_Index":
        n = _Index()
        n.ino, n.size, n.mtime_ns, n.offset = self.ino, self.size, self.mtime_ns, self.offset
        n.offsets, n.ts, n.roles = array("q", self.offsets), array("q", self.ts), bytearray(self.roles)
        n.uuids, n.first_user = dict(self.uuids), self.first_user
        return n

    def __len__(self) -> int:
        return len(self.offsets) + (1 if self.tail is not None else 0)


def _decode(raw: bytes) -> "dict | None":
    line = raw.strip()
    if not line:
        return None
    try:
        o = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return o if isinstance(o, dict) else None


def _is_prompt_line(o: dict) -> bool:
    """A user line with string content that the harness did not write (isMeta)."""
    m = o.get("message")
    return (o.get("type") == "user" and o.get("isMeta") is not True
            and isinstance(m, dict) and isinstance(m.get("content"), str))


def _record(ix: _Index, offset: int, msg: dict) -> None:
    i = len(ix.offsets)
    ix.offsets.append(offset)
    ts = msg.get("ts")
    ix.ts.append(ts if isinstance(ts, int) and not isinstance(ts, bool) else _NO_TS)
    ix.roles.append(_ROLE_CODE.get(msg.get("role"), 0))
    u = msg.get("uuid")
    if u and u not in ix.uuids:
        ix.uuids[u] = i


def _index(path: str, parse: Parse) -> "_Index | None":
    """The current index of `path`, built or extended as needed. None when unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _LOCK:
        old = _CACHE.get(path)
        if old is not None:
            _CACHE.move_to_end(path)
    if old is not None and old.ino == st.st_ino and old.size == st.st_size \
            and old.mtime_ns == st.st_mtime_ns:
        return old
    try:
        with open(path, "rb") as f:
            ix = None
            if old is not None and old.ino == st.st_ino and st.st_size > old.size \
                    and 0 < old.offset <= st.st_size:
                # Grown in place: resume after the last complete line, provided the byte before
                # it is still the newline it was (a cheap check that the file was not replaced).
                f.seek(old.offset - 1)
                if f.read(1) == b"\n":
                    ix = old.copy()
            if ix is None:
                ix = _Index()
            ix.ino, ix.size, ix.mtime_ns = st.st_ino, st.st_size, st.st_mtime_ns
            offset = ix.offset
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    # A line still being written: served now if it already parses, re-read next time.
                    o = _decode(raw)
                    ix.tail = parse(o) if o is not None else None
                    break
                o = _decode(raw)
                if o is not None:
                    if ix.first_user is None and _is_prompt_line(o):
                        ix.first_user = offset
                    msg = parse(o)
                    if msg is not None:
                        _record(ix, offset, msg)
                offset += len(raw)
            ix.offset = offset
    except OSError:
        return None
    with _LOCK:
        _CACHE[path] = ix
        _CACHE.move_to_end(path)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return ix


def _drop(path: str) -> None:
    with _LOCK:
        _CACHE.pop(path, None)


class _Stale(Exception):
    """An indexed offset no longer holds the message it held (the file was rewritten)."""


def _read(path: str, parse: Parse, ix: _Index, lo: int, hi: int) -> list[dict]:
    """Messages lo..hi-1 of `ix`, each with its `idx`."""
    out: list[dict] = []
    n = len(ix.offsets)
    if lo < min(hi, n):
        with open(path, "rb") as f:
            for i in range(lo, min(hi, n)):
                f.seek(ix.offsets[i])
                o = _decode(f.readline())
                msg = parse(o) if o is not None else None
                if msg is None:
                    raise _Stale(path)
                msg["idx"] = i
                out.append(msg)
    if ix.tail is not None and lo <= n < hi:
        msg = dict(ix.tail)
        msg["idx"] = n
        out.append(msg)
    return out


def _anchor(ix: _Index, around_uuid: "str | None", around_ts: "int | None") -> int:
    """Index of the anchored message: the first one carrying `around_uuid`, else the one whose
    timestamp is nearest `around_ts` (earliest on a tie); -1 when neither matches."""
    if around_uuid:
        i = ix.uuids.get(around_uuid, -1)
        if i < 0 and ix.tail is not None and ix.tail.get("uuid") == around_uuid:
            i = len(ix.offsets)
        if i >= 0:
            return i
    if around_ts is None:
        return -1
    best, idx = None, -1
    for i, t in enumerate(ix.ts):
        if t == _NO_TS:
            continue
        d = abs(t - around_ts)
        if best is None or d < best:
            best, idx = d, i
    if ix.tail is not None:
        t = ix.tail.get("ts")
        if isinstance(t, int) and not isinstance(t, bool):
            d = abs(t - around_ts)
            if best is None or d < best:
                idx = len(ix.offsets)
    return idx


def window(path: "str | Path", parse: Parse, *, limit: int = 100,
           before: "int | None" = None, since: "int | None" = None, since_max: int = 2000,
           around_uuid: "str | None" = None, around_ts: "int | None" = None) -> "list[dict] | None":
    """The feed messages a history request asks for, each with its `idx`; None when the file
    cannot be read. Selection, in order of precedence:

      before=N  — the `limit` messages ending just above message N;
      since=N   — message N to the end, when that is at most `since_max` messages;
      anchor    — `limit` messages centred on the anchored message (see `_anchor`);
      otherwise — the last `limit` messages.
    """
    key = str(path)
    for _attempt in range(2):
        ix = _index(key, parse)
        if ix is None:
            return None
        n = len(ix)
        if before is not None:
            end = max(0, min(before, n))
            lo, hi = max(0, end - limit), end
        elif since is not None and 0 <= since <= n and n - since <= since_max:
            lo, hi = since, n
        elif n <= limit:
            lo, hi = 0, n
        else:
            i = _anchor(ix, around_uuid, around_ts)
            if i < 0:
                lo, hi = n - limit, n
            else:
                hi = min(n, max(i + limit // 2 + 1, limit))
                lo = max(0, hi - limit)
        try:
            return _read(key, parse, ix, lo, hi)
        except _Stale:
            _drop(key)
        except OSError:
            return None
    return None


def count(path: "str | Path", parse: Parse) -> int:
    """Number of feed messages in the file (0 when unreadable)."""
    ix = _index(str(path), parse)
    return len(ix) if ix is not None else 0


def last_of_role(path: "str | Path", parse: Parse, role: str,
                 want: "Callable[[dict], bool] | None" = None) -> "dict | None":
    """The last feed message with this role (and passing `want`), or None."""
    key = str(path)
    code = _ROLE_CODE.get(role, 0)
    for _attempt in range(2):
        ix = _index(key, parse)
        if ix is None:
            return None
        try:
            if ix.tail is not None and ix.tail.get("role") == role and (want is None or want(ix.tail)):
                return dict(ix.tail)
            for i in range(len(ix.offsets) - 1, -1, -1):
                if ix.roles[i] != code:
                    continue
                msg = _read(key, parse, ix, i, i + 1)[0]
                if want is None or want(msg):
                    return msg
            return None
        except _Stale:
            _drop(key)
        except OSError:
            return None
    return None


def first_user_line(path: "str | Path", parse: Parse) -> "dict | None":
    """The raw JSON object of the first operator prompt line (user, string content, not isMeta) —
    the line that carries whatever the cockpit prepended to a fresh session's first prompt."""
    key = str(path)
    ix = _index(key, parse)
    if ix is None or ix.first_user is None:
        return None
    try:
        with open(key, "rb") as f:
            f.seek(ix.first_user)
            o = _decode(f.readline())
    except OSError:
        return None
    if o is None or not _is_prompt_line(o):
        _drop(key)
        return None
    return o


def scan_back(path: "str | Path", max_bytes: int = 16 << 20):
    """The decoded JSON objects of a file's lines, last line first, reading at most `max_bytes`
    from the end (a line that starts further back is never yielded). For questions about the end
    of a transcript — when did it stop, what was said last — without indexing all of it."""
    try:
        f = open(str(path), "rb")
    except OSError:
        return
    with f:
        try:
            pos = f.seek(0, os.SEEK_END)
        except OSError:
            return
        end, step, carry = pos, 1 << 16, b""
        while pos > 0 and end - pos < max_bytes:
            n = min(step, pos)
            pos -= n
            f.seek(pos)
            pieces = (f.read(n) + carry).split(b"\n")
            # The first piece may begin mid-line unless the read reached the start of the file.
            carry = pieces.pop(0) if pos > 0 else b""
            for raw in reversed(pieces):
                o = _decode(raw)
                if o is not None:
                    yield o
            step = min(step * 2, 1 << 22)

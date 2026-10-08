"""Bounded, symlink-proof JSONL/JSON reads shared by the Grok readers (spec-095 P3/P4).

Pure stdlib, no state. ``grok_history`` (session files) and ``grok_usage`` (the engine's
append-only ledgers) both need the same three guarantees, so they live in one place:

* a file is opened ``O_NOFOLLOW`` and only if it is a REGULAR file - a symlink is refused and a
  FIFO planted under a known name cannot block the reading thread forever;
* a big file is read from its TAIL (newest rows) or HEAD, never slurped;
* one giant line is skipped chunk by chunk, never buffered whole, and a malformed line (the
  half-written last line of a running turn included) is skipped.

The caps are explicit arguments: each caller owns its own limits.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Iterator


def open_regular(path: Path):
    """Open ``path`` for reading as a REGULAR file without following a symlink and without ever
    blocking on a FIFO. Returns a binary file object or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):  # ValueError: a NUL byte in the path
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def read_small(path: Path, cap: int) -> "bytes | None":
    """The whole file when it is at most ``cap`` bytes, else None (also None when unreadable)."""
    fh = open_regular(path)
    if fh is None:
        return None
    with fh:
        data = fh.read(cap + 1)
    return None if len(data) > cap else data


def iter_jsonl(path: Path, *, max_bytes: int, max_line: int,
               from_head: bool = False) -> Iterator[tuple[int, dict]]:
    """Yield ``(byte offset, row)`` for every parseable dict row, byte-bounded and line-bounded.

    A file larger than ``max_bytes`` is read from ``size - max_bytes`` (the partial first line is
    dropped): callers want the NEWEST rows. ``from_head`` reads the FIRST ``max_bytes`` instead
    (a session's opening message). A malformed line is skipped, as is any line longer than
    ``max_line`` - its remainder is discarded chunk by chunk, never held in memory.
    """
    fh = open_regular(path)
    if fh is None:
        return
    with fh:
        try:
            size = os.fstat(fh.fileno()).st_size
        except OSError:
            return
        # Every read below is bounded by `size`, the length MEASURED AT OPEN: the file is written by the
        # model's own shell, and one that appends in a loop must not keep this (executor) thread reading.
        if size > max_bytes and not from_head:
            # Land one byte early and consume through the next newline: a start that is exactly
            # on a line boundary keeps that line, a mid-line start drops only the partial.
            fh.seek(size - max_bytes - 1)
            _skip_line(fh, max_line, size)
        while True:
            offset = fh.tell()
            if offset >= size or (from_head and offset >= max_bytes):
                return
            line = fh.readline(min(max_line + 1, size - offset))
            if not line:
                return
            if len(line) > max_line and not line.endswith(b"\n"):
                _skip_line(fh, max_line, size)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(obj, dict):
                yield offset, obj


def _skip_line(fh, max_line: int, end: int) -> None:
    """Discard the rest of the current line, never reading at or past byte ``end``."""
    while True:
        pos = fh.tell()
        if pos >= end:
            return
        chunk = fh.readline(min(max_line + 1, end - pos))
        if not chunk or chunk.endswith(b"\n"):
            return

"""Ledger of the prompts THIS cockpit sent into Grok sessions (spec-095, handoff trust).

Why it exists. A Grok session's `chat_history.jsonl` lives under GROK_HOME, and the model's own
sandboxed shell can WRITE there (measured 2026-10-02: a real turn appended a forged
`<user_query>` row to its own session file). `handoff.build_handoff` carries user rows to the
next engine as "Standing constraints (verbatim, from the operator)" — so, without a second
witness, a prompt-injected Grok turn could hand orders to Claude (bypassPermissions, the
operator's credentials) in the operator's voice.

The witness: one file per session, `<data>/grok_sent/<session-id>`, holding a SHA-256
fingerprint of every prompt the cockpit actually sent into that session. A `user` row read from
the session file is operator-authored only if its text matches one of them. The directory sits
beside GROK_HOME, not inside it: with the project directory as the working directory the
sandbox lets the shell write the project and /tmp but not the cockpit's data dir (measured: a
`touch` next to GROK_HOME was refused). The one hole is a project whose own directory CONTAINS
the data dir (the cockpit's own repo opted in to Grok) — keep that project out of
`grok_allowed`, or deny the data dir in `GROK_SANDBOX_DENY`.

What is recorded is the RAW prompt handed to the engine (context pack and handoff block
included), whitespace-normalised: the CLI wraps it in `<user_query>`, and the reader returns
exactly the wrapped text, so equality survives that round trip. Everything here is best-effort
and never raises into a run: a failed write only means a later crossing shows that one message
as unverified.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import grok_history
import grok_jsonl

SENT_DIR = "grok_sent"
FP_HEX = 64
MAX_READ_BYTES = 1024 * 1024          # ~16k fingerprints per session; a longer file is read from its tail


def fingerprint(text: str) -> str:
    """SHA-256 of the whitespace-normalised text (runs of whitespace collapse to one space)."""
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8", "replace")).hexdigest()


def _path(data_dir, session_id) -> "Path | None":
    if not data_dir or not grok_history.valid_session_id(session_id):
        return None  # the strict UUID shape is also what keeps a hostile id out of the path
    return Path(data_dir) / SENT_DIR / session_id


def sent_fingerprints(data_dir, session_id) -> frozenset:
    """Every fingerprint recorded for the session (empty when none, unreadable or malformed)."""
    path = _path(data_dir, session_id)
    if path is None:
        return frozenset()
    fh = grok_jsonl.open_regular(path)
    if fh is None:
        return frozenset()
    with fh:
        try:
            size = os.fstat(fh.fileno()).st_size
            if size > MAX_READ_BYTES:
                fh.seek(size - MAX_READ_BYTES)
            data = fh.read(MAX_READ_BYTES)
        except OSError:
            return frozenset()
    out = set()
    for line in data.decode("ascii", "ignore").splitlines():
        line = line.strip()
        if len(line) == FP_HEX and all(c in "0123456789abcdef" for c in line):
            out.add(line)
    return frozenset(out)


def record(data_dir, session_id, prompt) -> bool:
    """Append the fingerprint of a prompt that was just sent into `session_id`. True when the
    fingerprint is on disk afterwards. Never raises."""
    try:
        path = _path(data_dir, session_id)
        if path is None or not isinstance(prompt, str) or not prompt.strip():
            return False
        fp = fingerprint(prompt)
        if fp in sent_fingerprints(data_dir, session_id):
            return True
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            os.write(fd, (fp + "\n").encode("ascii"))
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError):
        return False


def tag_rows(rows: list, fingerprints) -> list:
    """`rows` with `verified` set on every `user` row: True only when its text matches a prompt in
    `fingerprints`. Assistant rows are model output and carry no tag. Returns NEW dicts."""
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("role") == "user":
            row = {**row, "verified": fingerprint(row.get("text") or "") in fingerprints}
        out.append(row)
    return out

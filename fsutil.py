"""fsutil.py — the ONE atomic file writer (spec-096 P2).

Every file that holds a secret (vault key + store, per-project secrets.env, the Web Push
VAPID private key, account/Grok config) goes through `atomic_write`. The old pattern was
`path.write_text(...)` and `chmod(0o600)` afterwards: the file exists with the process umask
(0644 under umask 022) for the time between the two calls, and a failure in between leaves a
world-readable secret behind. Here the temp file comes from `tempfile.mkstemp`, which creates
it 0600 with O_EXCL (no window, no symlink follow), so the content only ever lands in a file
the owner alone can read; `os.replace` then swaps it in atomically.

Stdlib only: `secret.py` (the CLI) and `tools/doctor.py` import modules that import this one.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write(path: "str | os.PathLike[str]", data: "bytes | str", mode: int = 0o600, *,
                 prefix: "str | None" = None, suffix: str = ".tmp") -> None:
    """Write `data` to `path` atomically; the final file has permission bits `mode`.

    tmp file in the target directory (same filesystem, so `os.replace` is atomic), written,
    flushed and fsynced, then renamed over `path`. The temp file is 0600 from the moment it
    exists; `mode` is applied to the open descriptor BEFORE any byte is written, so a wider
    mode never widens a secret retroactively and a narrower one never leaves a gap.
    `str` data is written as UTF-8 without newline translation. The temp file is removed on
    any failure; the parent directory is created if missing (default permissions — a caller
    that needs a private parent creates it first).

    A symlink at `path` is REPLACED by the new file, not followed (`os.replace` renames over the
    link itself). That is deliberate for a secret file in a directory the model can write: the
    bytes can never be steered through a planted link into another file. The flip side is that a
    link the operator made on purpose (a role or secrets.env symlinked to a shared file) is cut
    loose on the next save.
    """
    path = os.fspath(path)
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    if prefix is None:
        prefix = f".{os.path.basename(path)}."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=prefix, suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def create_exclusive(path: "str | os.PathLike[str]", data: "bytes | str", mode: int = 0o600) -> bool:
    """Publish `data` at `path` ONLY if nothing is there yet. True: this call created the file;
    False: something already existed (a file, or a symlink — never followed) and is untouched.

    The content is written to a private temp file first and then published with `os.link`, which
    fails with EEXIST instead of replacing: a concurrent reader sees either no file or the
    complete one, and of two racing creators exactly one wins (the loser reads the winner's file
    — `atomic_write` would let the second silently replace the first). On a filesystem without
    hard links it falls back to `O_CREAT|O_EXCL|O_NOFOLLOW`, which is still exclusive but lets a
    reader catch the file half-written.
    """
    path = os.fspath(path)
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp_path, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass                                    # no hard links here: exclusive create instead
        try:
            fd2 = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                          | getattr(os, "O_CLOEXEC", 0), mode)
        except FileExistsError:
            return False
        with os.fdopen(fd2, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        return True
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)


def tighten(path: "str | os.PathLike[str]", mode: int = 0o600) -> bool:
    """chmod an EXISTING file to `mode` if it grants any permission outside `mode`.

    Returns True when it changed the file. A missing file or a failed chmod returns False
    (startup hygiene must never take the cockpit down).
    """
    try:
        current = Path(path).stat().st_mode & 0o777
        if current & ~mode:
            os.chmod(path, mode)
            return True
    except OSError:
        pass
    return False

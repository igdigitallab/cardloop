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

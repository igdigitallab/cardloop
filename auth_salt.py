"""auth_salt.py — where the cockpit's cookie-signing salt comes from (spec-096 P3.1).

The session cookie is `scrypt(password, salt)`. The salt is what stops a stolen cookie from being
brute-forced offline against a shared password list, so it must NOT be a value that is published
in the repository: `.env.example` used to ship `WEB_COOKIE_SALT=CHANGE_ME_RANDOM`, and any
non-empty value was used verbatim, so a plain `cp .env.example .env` install signed every cookie
with a public salt.

Rules, in order:
1. An explicitly configured salt (non-blank, not a `CHANGE_ME...` placeholder) is used byte for
   byte as before — existing cookies stay valid.
2. Otherwise the salt lives in a private file in the data dir (`cookie_salt`, 0600): read it when
   it is there, generate one with `secrets` and persist it atomically when it is not. The value is
   never printed or logged.
3. If the data dir cannot be used at all, fall back to a per-process random salt (safe, but every
   restart signs everyone out) and say so — still without the value.

Stdlib only (+ `fsutil`): `tools/doctor.py` imports `is_placeholder` from here.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import fsutil

log = logging.getLogger(__name__)

SALT_FILENAME = "cookie_salt"
_PLACEHOLDER_PREFIX = "change_me"
# A persisted salt shorter than this is not something we wrote (we write 64 hex chars); treat it
# as damaged and replace it rather than sign cookies with a near-empty salt.
_MIN_FILE_SALT_CHARS = 16


def is_placeholder(value: "str | None") -> bool:
    """True for a blank value or a `CHANGE_ME...` placeholder (case-insensitive)."""
    stripped = (value or "").strip()
    return not stripped or stripped.lower().startswith(_PLACEHOLDER_PREFIX)


def _read_persisted(path: Path) -> "bytes | None":
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if len(text) < _MIN_FILE_SALT_CHARS:
        return None
    return text.encode()


def resolve(env_value: "str | None", data_dir: "str | os.PathLike[str] | None") -> bytes:
    """Return the salt bytes to sign cookies with (see the module docstring for the rules)."""
    if not is_placeholder(env_value):
        return (env_value or "").encode()          # explicit: byte-identical to the old behaviour

    if data_dir is not None:
        path = Path(data_dir) / SALT_FILENAME
        existing = _read_persisted(path)
        if existing is not None:
            fsutil.tighten(path)                    # a file copied around with 0644 is made private
            return existing
        try:
            fresh = secrets.token_hex(32)
            fsutil.atomic_write(path, fresh, 0o600)
            # Read back what is on disk (not what we generated) so two racing starts agree.
            persisted = _read_persisted(path)
            if persisted is not None:
                log.warning("auth: WEB_COOKIE_SALT is blank or a placeholder; generated a salt "
                            "and stored it in %s", path)
                return persisted
        except OSError as exc:
            log.warning("auth: cannot persist the cookie salt in %s (%s)", path, exc.__class__.__name__)

    log.warning("auth: WEB_COOKIE_SALT is blank or a placeholder and no data dir is usable; "
                "using a per-process salt (everyone is signed out on every restart)")
    return secrets.token_hex(32).encode()

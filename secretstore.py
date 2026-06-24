"""
secretstore.py — built-in encrypted-at-rest secret store (Spec 026, Phase 3).

Single Fernet-encrypted file, readable and writable by both the CLI (secret.py)
and the cockpit (webapp.py).  The module is SYNC — file I/O is tiny enough that
blocking is not a problem; callers that are async wrappers can run these in a
thread pool if needed, but for disk files it is not required.

Usage from CLI:
    import secretstore
    secretstore.set("MY_KEY", "s3cr3t", category="api")
    value = secretstore.get("MY_KEY")

Public API:
    init_key(force=False) -> str        generate + write keyfile (0600), return path
    get(name) -> str | None             decrypt and return a secret value
    set(name, value, category, notes)   create or update a secret
    list_meta() -> list[dict]           names + metadata, NO values
    delete(name) -> bool                remove a secret; False if absent
    get_full(name) -> dict | None       value + metadata (for reveal endpoint)
    import_env(path) -> int             import KEY=value or JSON file; return count

Key resolution order (highest priority first):
    1. env  CLAUDE_OPS_SECRET_KEY      (a base64-url Fernet key string)
    2. keyfile  CLAUDE_OPS_SECRET_KEYFILE  (default ~/.config/claude-ops/secret.key)
    3. If neither exists and the operation needs a key → raises RuntimeError.

Store file path:
    env  CLAUDE_OPS_SECRET_STORE  (default <repo-root>/data/vault/secrets.enc)

NEVER log or print secret values anywhere in this module.
"""

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

# ─────────────────────────── name validation ──────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")

# Reserved internal names (Spec 026, Phase 2 — TOTP).
# These match ^__.*__$ and are HIDDEN from list_meta() and the CLI list command
# so TOTP internals never appear as user secrets.  Internal code may still
# call get()/set()/delete() on them directly.
_RESERVED_RE = re.compile(r"^__.*__$")


def _validate_name(name: str) -> None:
    """Raise ValueError if name contains path-injection or invalid characters."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid secret name {name!r}: must match ^[A-Za-z0-9._-]{{1,128}}$"
        )


# ─────────────────────────── paths ────────────────────────────────────────────

_HERE = Path(__file__).parent  # repo root


def _default_store_path() -> Path:
    return _HERE / "data" / "vault" / "secrets.enc"


def _store_path() -> Path:
    raw = os.environ.get("CLAUDE_OPS_SECRET_STORE", "")
    return Path(raw) if raw else _default_store_path()


def _default_keyfile_path() -> Path:
    return Path(os.path.expanduser("~/.config/claude-ops/secret.key"))


def _keyfile_path() -> Path:
    raw = os.environ.get("CLAUDE_OPS_SECRET_KEYFILE", "")
    return Path(raw) if raw else _default_keyfile_path()


# ─────────────────────────── key management ───────────────────────────────────

def _load_key() -> bytes:
    """Return the Fernet key bytes.  Raises RuntimeError with guidance if absent."""
    # Priority 1: environment variable
    env_key = os.environ.get("CLAUDE_OPS_SECRET_KEY", "")
    if env_key:
        return env_key.encode()

    # Priority 2: keyfile
    kf = _keyfile_path()
    if kf.exists():
        return kf.read_bytes().strip()

    raise RuntimeError(
        "Secret store: no master key found. "
        "Run  `python secret.py init`  (or `secret init` if on PATH) "
        "to generate a key at "
        f"{kf}  — or set env CLAUDE_OPS_SECRET_KEY / CLAUDE_OPS_SECRET_KEYFILE."
    )


def init_key(force: bool = False) -> str:
    """Generate a new Fernet key and write it to the keyfile (mode 0600).

    Returns the keyfile path as a string.
    Raises FileExistsError if the keyfile already exists and force=False.
    """
    kf = _keyfile_path()
    if kf.exists() and not force:
        raise FileExistsError(
            f"Key file already exists: {kf}. "
            "Use force=True (or --force on CLI) to overwrite."
        )

    new_key = Fernet.generate_key()
    # Create parent directories with restricted permissions
    kf.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    kf.write_bytes(new_key)
    kf.chmod(0o600)
    return str(kf)


# ─────────────────────────── store I/O ────────────────────────────────────────

def _read_store() -> dict:
    """Load and decrypt the store.  Returns {} if the file is missing or empty."""
    path = _store_path()
    if not path.exists() or path.stat().st_size == 0:
        return {}

    key = _load_key()
    f = Fernet(key)
    try:
        raw = f.decrypt(path.read_bytes())
    except InvalidToken as exc:
        raise RuntimeError(
            f"Secret store: could not decrypt {path}. "
            "Wrong key, or file is corrupt."
        ) from exc

    return json.loads(raw.decode("utf-8"))


def _write_store(data: dict) -> None:
    """Encrypt and persist data to the store file."""
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    key = _load_key()
    f = Fernet(key)
    encrypted = f.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    # Write atomically via a temp file in the same directory
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(encrypted)
        tmp.chmod(0o600)
        tmp.replace(path)
    except Exception:
        # Clean up the temp file on failure
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


# ─────────────────────────── public API ───────────────────────────────────────

def get(name: str) -> Optional[str]:
    """Return the decrypted value for name, or None if absent."""
    _validate_name(name)
    data = _read_store()
    entry = data.get(name)
    if entry is None:
        return None
    return entry["value"]


def set(name: str, value: str, category: str = "", notes: str = "") -> None:
    """Create or update a secret.  Raises ValueError on invalid name."""
    _validate_name(name)
    data = _read_store()
    data[name] = {
        "value": value,
        "category": category,
        "notes": notes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_store(data)


def list_meta(include_reserved: bool = False) -> list:
    """Return metadata for all secrets — names, categories, notes, updated_at.

    Values are NEVER included in the output.

    Reserved names (matching ^__.*__$) are hidden by default so TOTP internals
    do not appear in the vault list.  Pass include_reserved=True only for
    internal tooling.
    """
    data = _read_store()
    result = []
    for name, entry in sorted(data.items()):
        if not include_reserved and _RESERVED_RE.match(name):
            continue
        result.append({
            "name": name,
            "category": entry.get("category", ""),
            "notes": entry.get("notes", ""),
            "updated_at": entry.get("updated_at", ""),
        })
    return result


def delete(name: str) -> bool:
    """Remove a secret.  Returns True if it existed, False otherwise."""
    _validate_name(name)
    data = _read_store()
    if name not in data:
        return False
    del data[name]
    _write_store(data)
    return True


def get_full(name: str) -> Optional[dict]:
    """Return value + metadata for a single secret, or None if absent.

    Used by the cockpit reveal endpoint — the value is included here because
    this function is only called when the operator explicitly requests it.
    """
    _validate_name(name)
    data = _read_store()
    entry = data.get(name)
    if entry is None:
        return None
    return {
        "name": name,
        "value": entry["value"],
        "category": entry.get("category", ""),
        "notes": entry.get("notes", ""),
        "updated_at": entry.get("updated_at", ""),
    }


def import_env(path: str) -> int:
    """Import secrets from a KEY=value (.env-style) or JSON file.

    .env-style: lines that are not blank and not starting with # are parsed as
    KEY=value (the first '=' splits key from value; leading/trailing whitespace
    is stripped; existing secrets are overwritten).

    JSON: the root object is expected to be {name: value_string, ...}.

    Returns the count of secrets imported.
    """
    p = Path(path)
    content = p.read_text(encoding="utf-8")

    pairs: dict = {}

    # Detect JSON vs .env-style
    stripped = content.lstrip()
    if stripped.startswith("{"):
        raw = json.loads(content)
        for k, v in raw.items():
            if isinstance(v, str):
                pairs[k] = v
            else:
                # Coerce non-string values to string
                pairs[k] = json.dumps(v)
    else:
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            pairs[key] = value

    count = 0
    for name, value in pairs.items():
        try:
            _validate_name(name)
        except ValueError:
            # Skip names that don't pass validation rather than aborting the import
            continue
        # load + write individually so each gets its own updated_at
        data = _read_store()
        existing = data.get(name, {})
        data[name] = {
            "value": value,
            "category": existing.get("category", ""),
            "notes": existing.get("notes", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_store(data)
        count += 1

    return count

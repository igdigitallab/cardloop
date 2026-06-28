"""Cardloop module registry — spec-065 Phase A.

A lightweight registry of built-in optional modules (features that can be
toggled on/off by the operator).  Each module ships with a ``default_enabled``
flag; the operator's overrides are persisted to ``data/modules.json``.

Persistence is lazy: the file is only written on ``set_enabled()``.  A missing
file simply means "all defaults apply".  Reads never create the file.

Import-safe: nothing is written to disk at import time.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Built-in module descriptors
# ---------------------------------------------------------------------------

_BUILTIN_MODULES: list[dict[str, Any]] = [
    {
        "id": "github",
        "name": "GitHub",
        "description": "Show git commit/sync status in the project header.",
        "version": "1.0.0",
        "provides": ["badge"],
        "default_enabled": True,
    },
    {
        "id": "browser",
        "name": "Browser",
        "description": (
            "Agent-driven browser shown live next to chat (spec-065 Phase B+)."
        ),
        "version": "1.0.0",
        "provides": ["pane", "tools"],
        "default_enabled": False,
        # spec-066: pluggable backend config. backend ∈ builtin|cloakbrowser|external-cdp.
        # Secrets (Cloak Manager token) never live here — they go to the encrypted safe.
        "default_config": {
            "backend": "builtin",
            "cdp_url": "",
            "manager_url": "",
            "default_profile": "",
            "per_project_profile": {},
            "agent_actions": "read",
            # Tier B stealth knobs (passed through to cloakbrowser.launch_async).
            "proxy": "",
            "geoip": False,
            "humanize": False,
            "timezone": "",
            "locale": "",
        },
    },
]

# Lookup by id for O(1) access.
_BUILTIN_BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in _BUILTIN_MODULES}

# ---------------------------------------------------------------------------
# Path helper (DATA dir resolved the same way as the rest of the codebase)
# ---------------------------------------------------------------------------

# The canonical data directory: sibling ``data/`` next to this file.
# webapp.py / engine.py use HERE / "data" — mirror that so the path is always
# correct even when the module is imported from outside the package root.
_HERE = Path(__file__).resolve().parent


def _modules_path() -> Path:
    """Return the path to data/modules.json.

    Resolves via DATA env var when set (test isolation), otherwise uses the
    ``data/`` directory next to this file — the same convention as engine.py.
    """
    data_env = os.environ.get("_CARDLOOP_DATA_DIR")
    if data_env:
        return Path(data_env) / "modules.json"
    return _HERE / "data" / "modules.json"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_overrides() -> dict[str, dict[str, Any]]:
    """Read persisted overrides from data/modules.json.

    Returns a dict ``{module_id: {"enabled": bool}}``.
    A missing or corrupt file returns an empty dict (= all defaults apply).
    Never creates the file.
    """
    p = _modules_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        # Keep only known ids to avoid stale data polluting the merge.
        return {
            k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
    except Exception:
        return {}


def _save_overrides(overrides: dict[str, dict[str, Any]]) -> None:
    """Atomically persist overrides to data/modules.json (tmp + replace)."""
    p = _modules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _effective_config(module_id: str, persisted: dict[str, Any]) -> dict[str, Any]:
    """Merge a module's ``default_config`` with any persisted ``config`` override.

    Shallow merge (top-level keys): defaults provide the full shape, the override
    wins per key. Modules with no ``default_config`` return ``{}``.
    """
    builtin = _BUILTIN_BY_ID.get(module_id, {})
    base = dict(builtin.get("default_config") or {})
    stored = persisted.get("config")
    if isinstance(stored, dict):
        base.update(stored)
    return base


def list_modules() -> list[dict[str, Any]]:
    """Return all built-in modules with their effective ``enabled`` state + ``config``.

    Merges the persisted overrides (if any) with each module's ``default_enabled``
    and ``default_config``.  Returned shape (pinned API contract):
    ``{id, name, description, version, provides, enabled, config}``.
    """
    overrides = _load_overrides()
    result: list[dict[str, Any]] = []
    for m in _BUILTIN_MODULES:
        mid = m["id"]
        persisted = overrides.get(mid, {})
        enabled = persisted.get("enabled", m["default_enabled"])
        result.append(
            {
                "id": mid,
                "name": m["name"],
                "description": m["description"],
                "version": m["version"],
                "provides": list(m["provides"]),
                "enabled": bool(enabled),
                "config": _effective_config(mid, persisted),
            }
        )
    return result


def is_enabled(module_id: str) -> bool:
    """Return True if the named module is currently enabled.

    Falls back to the module's ``default_enabled`` when no override is stored.
    Raises ``KeyError`` for an unknown module id.
    """
    builtin = _BUILTIN_BY_ID.get(module_id)
    if builtin is None:
        raise KeyError(f"Unknown module id: {module_id!r}")
    overrides = _load_overrides()
    persisted = overrides.get(module_id, {})
    return bool(persisted.get("enabled", builtin["default_enabled"]))


def set_enabled(module_id: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a module and persist the change atomically.

    Returns the updated module dict (same shape as a list_modules() entry).
    Raises ``KeyError`` for an unknown module id.
    """
    builtin = _BUILTIN_BY_ID.get(module_id)
    if builtin is None:
        raise KeyError(f"Unknown module id: {module_id!r}")

    overrides = _load_overrides()
    overrides.setdefault(module_id, {})["enabled"] = bool(enabled)
    _save_overrides(overrides)

    return {
        "id": module_id,
        "name": builtin["name"],
        "description": builtin["description"],
        "version": builtin["version"],
        "provides": list(builtin["provides"]),
        "enabled": bool(enabled),
        "config": _effective_config(module_id, overrides.get(module_id, {})),
    }


def get_config(module_id: str) -> dict[str, Any]:
    """Return the effective config (defaults merged with the persisted override).

    Returns ``{}`` for a module with no ``default_config``.  Raises ``KeyError``
    for an unknown module id.
    """
    if module_id not in _BUILTIN_BY_ID:
        raise KeyError(f"Unknown module id: {module_id!r}")
    overrides = _load_overrides()
    return _effective_config(module_id, overrides.get(module_id, {}))


def set_config(module_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Persist a module's ``config`` block (atomic) and return the updated module dict.

    Only keys present in the module's ``default_config`` are accepted — unknown keys
    are dropped so callers cannot smuggle arbitrary data (or secrets) into the file.
    Secrets must go to the encrypted safe, never here.  Raises ``KeyError`` for an
    unknown module id; ``TypeError`` if ``config`` is not a dict.
    """
    builtin = _BUILTIN_BY_ID.get(module_id)
    if builtin is None:
        raise KeyError(f"Unknown module id: {module_id!r}")
    if not isinstance(config, dict):
        raise TypeError("config must be a dict")
    allowed = set((builtin.get("default_config") or {}).keys())
    clean = {k: v for k, v in config.items() if k in allowed}

    overrides = _load_overrides()
    entry = overrides.setdefault(module_id, {})
    # Merge (not replace) so a partial update from the UI (e.g. just {"backend"})
    # does not silently reset the other fields to their defaults.
    existing = entry.get("config")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(clean)
    entry["config"] = merged
    _save_overrides(overrides)

    return {
        "id": module_id,
        "name": builtin["name"],
        "description": builtin["description"],
        "version": builtin["version"],
        "provides": list(builtin["provides"]),
        "enabled": is_enabled(module_id),
        "config": _effective_config(module_id, overrides.get(module_id, {})),
    }

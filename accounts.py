"""Multiple Claude subscriptions, one cockpit.

The Claude CLI resolves its credentials from ``$CLAUDE_CONFIG_DIR/.credentials.json``
(default: ``~/.claude``). That env var is the ONLY reliable switch: a
``CLAUDE_CODE_OAUTH_TOKEN`` in the environment is silently ignored whenever a
credentials file exists next to it, so "just pass another token" would keep billing
the first account with no visible sign. Verified against the bundled CLI on 2026-08-20.

An extra account therefore gets its own config dir under ``~/.claude-accounts/<id>/``
holding only its own ``.credentials.json``; everything that must stay shared
(``projects/`` transcripts, ``skills/``, ``hooks/``, ``settings.json``, …) is symlinked
back to ``~/.claude``. That keeps session resume, chat history and the usage scanner
working across a switch — the transcript of a run on account #2 still lands in the one
shared ``~/.claude/projects/<slug>/``.

Safety rules baked in here:
  * ``main`` is virtual and always present. Selecting it injects NO env at all, so the
    default install behaves byte-for-byte as it did before this module existed.
  * A registered account whose config dir or credentials went missing degrades to
    ``main`` instead of taking every run down.
  * ``set_active`` refuses an account that does not validate, so a typo cannot silently
    redirect the whole cockpit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

# The always-present, never-registered account: whatever ~/.claude holds today.
MAIN_ID = "main"

# Directories/files an extra config dir must share with ~/.claude for history, skills,
# hooks and settings to behave identically on every account. `projects` is the load-bearing
# one: engine._transcript_exists() looks under ~/.claude/projects, so a non-shared projects
# dir would make session resume self-heal on wrong evidence.
SHARED_LINKS = (
    "projects", "sessions", "skills", "skills-lazy", "agents", "commands",
    "hooks", "plugins", "ide", "settings.json", "settings.local.json", "CLAUDE.md",
)

# Only `projects` is fatal enough to warn about loudly — the rest degrade gracefully.
CRITICAL_LINKS = ("projects",)


def accounts_root() -> Path:
    """Root for extra config dirs. Override with CLAUDE_ACCOUNTS_DIR (tests, odd setups)."""
    return Path(os.environ.get("CLAUDE_ACCOUNTS_DIR") or (Path.home() / ".claude-accounts"))


def main_config_dir() -> Path:
    """The config dir account `main` uses — CLAUDE_CONFIG_DIR if the operator set one, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _state_path() -> Path:
    """data/accounts.json — same DATA resolution as modules.py."""
    data_env = os.environ.get("_CARDLOOP_DATA_DIR")
    if data_env:
        return Path(data_env) / "accounts.json"
    return _HERE / "data" / "accounts.json"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    """Read data/accounts.json. Missing/corrupt → the default single-account state."""
    p = _state_path()
    if not p.exists():
        return {"active": MAIN_ID, "accounts": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"active": MAIN_ID, "accounts": {}}
        accs = raw.get("accounts")
        if not isinstance(accs, dict):
            accs = {}
        clean: dict[str, dict] = {}
        for aid, rec in accs.items():
            if isinstance(aid, str) and aid != MAIN_ID and isinstance(rec, dict) and rec.get("config_dir"):
                clean[aid] = {
                    "label": str(rec.get("label") or aid),
                    "config_dir": str(rec["config_dir"]),
                }
        active = raw.get("active")
        if not isinstance(active, str) or (active != MAIN_ID and active not in clean):
            active = MAIN_ID
        return {"active": active, "accounts": clean}
    except Exception:
        return {"active": MAIN_ID, "accounts": {}}


def _save_state(state: dict[str, Any]) -> None:
    """Atomically persist data/accounts.json (tmp + replace)."""
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def config_dir(account_id: str) -> "str | None":
    """Config dir for an account. None for `main` — meaning "inject no env override"."""
    if not account_id or account_id == MAIN_ID:
        return None
    rec = load_state()["accounts"].get(account_id)
    return rec["config_dir"] if rec else None


def creds_path(account_id: str) -> str:
    """Path to that account's .credentials.json (main honours CLAUDE_CREDENTIALS_PATH)."""
    if not account_id or account_id == MAIN_ID:
        override = os.environ.get("CLAUDE_CREDENTIALS_PATH", "")
        if override:
            return os.path.expanduser(override)
        return str(main_config_dir() / ".credentials.json")
    cdir = config_dir(account_id)
    return str(Path(cdir) / ".credentials.json") if cdir else ""


def active_id() -> str:
    """The account every new run should use. Degrades to `main` if the selection broke."""
    state = load_state()
    aid = state["active"]
    if aid == MAIN_ID:
        return MAIN_ID
    ok, reason = _validate(aid, state)
    if not ok:
        print(f"[accounts] active account {aid!r} unusable ({reason}) — falling back to {MAIN_ID}")
        return MAIN_ID
    return aid


def resolve(project_account: "str | None" = None) -> str:
    """Which account a run should use: project override → global active → main.

    A project may be pinned to one subscription while the rest of the cockpit uses another.
    An override naming an account that is gone or not logged in does NOT fail the run — it
    falls through to the global choice, and `inspect()` still shows the operator why.
    """
    if project_account:
        ok, reason = _validate(project_account)
        if ok:
            return project_account
        print(f"[accounts] project override {project_account!r} unusable ({reason}) — "
              f"falling back to the global account")
    return active_id()


def env_overrides(account_id: "str | None" = None) -> dict[str, str]:
    """Env additions that bind a run to an account.

    `main` (or anything unusable) → {} — no CLAUDE_CONFIG_DIR is set and the CLI behaves
    exactly as it does on a single-account install.
    """
    aid = account_id if account_id is not None else active_id()
    if aid == MAIN_ID:
        return {}
    state = load_state()
    ok, reason = _validate(aid, state)
    if not ok:
        print(f"[accounts] {aid!r} unusable ({reason}) — running on {MAIN_ID}")
        return {}
    return {"CLAUDE_CONFIG_DIR": state["accounts"][aid]["config_dir"]}


# ---------------------------------------------------------------------------
# Validation & inspection
# ---------------------------------------------------------------------------

def _validate(account_id: str, state: "dict | None" = None) -> "tuple[bool, str]":
    """(usable?, reason). Usable = registered, dir present, credentials readable."""
    if account_id == MAIN_ID:
        return True, ""
    st = state or load_state()
    rec = st["accounts"].get(account_id)
    if not rec:
        return False, "not registered"
    cdir = Path(rec["config_dir"])
    if not cdir.is_dir():
        return False, f"config dir missing ({cdir})"
    cfile = cdir / ".credentials.json"
    if not cfile.exists():
        return False, "not logged in yet (no .credentials.json)"
    try:
        oauth = json.loads(cfile.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except Exception as exc:
        return False, f"credentials unreadable ({exc.__class__.__name__})"
    if not oauth.get("accessToken"):
        return False, "credentials have no accessToken"
    return True, ""


def validate(account_id: str) -> "tuple[bool, str]":
    """Public form of the usability check: (usable?, human-readable reason)."""
    return _validate(account_id)


def _links_ok(cdir: Path) -> "tuple[bool, list[str]]":
    """Do the critical shared paths resolve back into the main config dir?"""
    main = main_config_dir().resolve()
    broken: list[str] = []
    for name in CRITICAL_LINKS:
        target = cdir / name
        try:
            if not target.exists() or target.resolve() != (main / name).resolve():
                broken.append(name)
        except Exception:
            broken.append(name)
    return (not broken), broken


def inspect(account_id: str) -> dict[str, Any]:
    """Everything the UI/doctor needs about one account. Never raises, never logs a token."""
    state = load_state()
    is_main = account_id == MAIN_ID
    rec = None if is_main else state["accounts"].get(account_id)
    cdir = main_config_dir() if is_main else (Path(rec["config_dir"]) if rec else None)
    ok, reason = _validate(account_id, state)
    info: dict[str, Any] = {
        "id": account_id,
        "label": ("Main" if is_main else (rec["label"] if rec else account_id)),
        "config_dir": str(cdir) if cdir else "",
        "is_main": is_main,
        "active": state["active"] == account_id,
        "ok": ok,
        "reason": reason,
        "email": "",
        "plan": "",
        "expires_at": None,
        "shared_ok": True,
        "shared_broken": [],
    }
    if cdir and not is_main:
        shared_ok, broken = _links_ok(cdir)
        info["shared_ok"], info["shared_broken"] = shared_ok, broken
    if not ok or not cdir:
        return info
    try:
        oauth = json.loads((cdir / ".credentials.json").read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        info["plan"] = str(oauth.get("subscriptionType") or "")
        exp = oauth.get("expiresAt")
        if isinstance(exp, (int, float)):
            info["expires_at"] = int(exp / 1000)
    except Exception:
        pass
    # The account's email lives in the CLI's own state file, not in the credentials. Its
    # location follows CLAUDE_CONFIG_DIR when one is set, but a default install keeps it at
    # ~/.claude.json (NOT inside ~/.claude) — try both rather than showing a blank main account.
    # Only `main` may fall back to ~/.claude.json — for an extra account that file belongs to a
    # DIFFERENT subscription, and showing its email here would label the account with the wrong
    # identity, which is exactly the mistake this whole feature exists to prevent.
    candidates = [cdir / ".claude.json"] + ([Path.home() / ".claude.json"] if is_main else [])
    for state_file in candidates:
        try:
            acc = json.loads(state_file.read_text(encoding="utf-8")).get("oauthAccount") or {}
            if acc.get("emailAddress"):
                info["email"] = str(acc["emailAddress"])
                break
        except Exception:
            continue
    return info


def list_accounts() -> list[dict[str, Any]]:
    """All selectable accounts, `main` first."""
    state = load_state()
    return [inspect(MAIN_ID)] + [inspect(aid) for aid in sorted(state["accounts"])]


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------

def set_active(account_id: str) -> "tuple[bool, str]":
    """Switch every subsequent run to this account. Refuses an account that won't work."""
    state = load_state()
    ok, reason = _validate(account_id, state)
    if not ok:
        return False, reason
    state["active"] = account_id
    _save_state(state)
    print(f"[accounts] active account → {account_id}")
    return True, ""


def register(account_id: str, label: str, cdir: str) -> "tuple[bool, str]":
    """Add/update an extra account. The dir must exist; credentials may come later (login)."""
    if not account_id or account_id == MAIN_ID:
        return False, f"id must be non-empty and not {MAIN_ID!r}"
    if not all(c.isalnum() or c in "-_" for c in account_id):
        return False, "id may only contain letters, digits, '-' and '_'"
    p = Path(os.path.expanduser(cdir))
    if not p.is_dir():
        return False, f"config dir does not exist: {p}"
    state = load_state()
    state["accounts"][account_id] = {"label": label or account_id, "config_dir": str(p)}
    _save_state(state)
    return True, ""


def unregister(account_id: str) -> "tuple[bool, str]":
    """Forget an account (its config dir and credentials are left on disk, untouched)."""
    if account_id == MAIN_ID:
        return False, "cannot remove the main account"
    state = load_state()
    if account_id not in state["accounts"]:
        return False, "not registered"
    state["accounts"].pop(account_id)
    if state["active"] == account_id:
        state["active"] = MAIN_ID
    _save_state(state)
    return True, ""


def scaffold(account_id: str) -> "tuple[Path, list[str]]":
    """Create ~/.claude-accounts/<id>/ with the shared paths symlinked to ~/.claude.

    Returns (dir, linked_names). Never touches ~/.claude itself, and never overwrites an
    existing entry inside the new dir — re-running is safe.
    """
    cdir = accounts_root() / account_id
    cdir.mkdir(parents=True, exist_ok=True)
    os.chmod(cdir, 0o700)
    main = main_config_dir()
    linked: list[str] = []
    for name in SHARED_LINKS:
        src, dst = main / name, cdir / name
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        try:
            dst.symlink_to(src)
            linked.append(name)
        except Exception as exc:
            print(f"[accounts] could not link {name}: {exc!r}")
    return cdir, linked


def summary_for_prompt() -> str:
    """One short line for logs/diagnostics — never includes tokens."""
    aid = active_id()
    if aid == MAIN_ID:
        return "account=main"
    info = inspect(aid)
    return f"account={aid}" + (f" ({info['email']})" if info["email"] else "")


def touched_at() -> float:
    """mtime of accounts.json — lets callers cheaply notice an external switch."""
    try:
        return _state_path().stat().st_mtime
    except Exception:
        return 0.0


__all__ = [
    "MAIN_ID", "SHARED_LINKS", "accounts_root", "main_config_dir", "load_state",
    "config_dir", "creds_path", "active_id", "env_overrides", "inspect", "list_accounts",
    "set_active", "register", "unregister", "scaffold", "summary_for_prompt", "touched_at",
    "resolve", "validate",
]

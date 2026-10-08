#!/usr/bin/env python3
"""doctor.py — one-command diagnosis for a broken (or healthy) Cardloop cockpit.

Prints, in under 5 seconds, every fact an operator or a GitHub issue reporter needs:
Versions, Auth, Config, Service, Runtime, Data, then a Verdict (list of ✗/⚠ findings
with a one-line remedy each). Exit code is non-zero when any ✗ finding is present, so
it composes with cron/CI.

Read-only. Never mutates state, never restarts anything, never prints a secret —
redaction is always on; there is no flag to turn it off.

Run:
    venv/bin/python tools/doctor.py            # human-readable report
    venv/bin/python tools/doctor.py --json      # machine-readable
    make doctor                                 # same, via the Makefile

Design notes:
  - Reuses tools/verify_model_aliases.py's `_bundled_cli()` helper to locate the SDK's
    bundled `claude` binary (same glob, one source of truth) instead of duplicating it.
    It does NOT run that tool's live /v1/models probe (network + billed tokens, and
    would blow the <5s budget) — the alias-resolution ground truth stays a separate,
    opt-in check; doctor only verifies the installed `claude-agent-sdk` meets the floor
    pinned in requirements.txt (fast, local, zero cost) and flags a stale install.
  - Reuses board.py's TASKS.md parser for the Data section's card counts (stdlib-only,
    already a repo module — no logic duplicated, and only counts are read, never text).
  - Every probe takes its collaborators (subprocess runner, repo root, ...) as
    parameters with real defaults, so tests can drive the findings logic with fake
    data — no test needs systemd, the network, or a live cockpit.
  - The "Grok" section (spec-095 §5.9) exists only when GROK_ENABLED is on and is hidden
    when empty, so a cockpit without Grok prints exactly what it always did. It asks
    grok_engine for every fact it can (home, env, deny list, probe fingerprint) so the
    two cannot drift, and it NEVER runs a model turn: the sandbox-denial verdict is read
    from the engine's on-disk cache, never produced here.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools/
REPO_ROOT = HERE.parent                          # repo root

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import verify_model_aliases as _vma  # tools/verify_model_aliases.py — same dir, stdlib-only
except Exception:
    _vma = None

try:
    import auth_salt as _auth_salt  # auth_salt.py — stdlib-only; the ONE placeholder predicate (spec-096 P3.1)
except Exception:
    _auth_salt = None

try:
    import board as _board  # board.py — stdlib-only (asyncio/re/secrets/pathlib), no side effects
except Exception:
    _board = None


# ─────────────────────────── Fact / Finding model ──────────────────────────────

@dataclass
class Fact:
    """One line of a doctor section.

    level: "ok" | "warn" | "fail" | "info"
    "info" facts are shown in the section but never appear in the Verdict (they are
    context, not a problem — e.g. "TOTP: off" on a fresh install with no 2FA yet).
    """
    label: str
    value: str
    level: str = "ok"
    remedy: "str | None" = None


# ─────────────────────────── redaction (always on) ──────────────────────────────

# Defense in depth: pattern-based redaction for common secret shapes (catches
# anything that ends up in free text we don't fully control, e.g. a journal line
# or a subprocess error message) PLUS exact-value scrubbing of every secret this
# run itself loaded (WEB_PASSWORD, ANTHROPIC_API_KEY, the OAuth access token, ...).
# Applied to every Fact value/remedy right before it is rendered — nothing skips it.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT-shaped
]


def _redact(value: str, keep_prefix: int = 7, keep_suffix: int = 4) -> str:
    """prefix + ellipsis + last N chars, e.g. 'sk-ant-...' -> 'sk-ant-…XXXX'. Never
    returns anything close to the full value."""
    if not value:
        return ""
    if len(value) <= keep_prefix + keep_suffix:
        return "…"
    return f"{value[:keep_prefix]}…{value[-keep_suffix:]}"


def _scrub(text: "str | None", secrets: "list[str]") -> "str | None":
    if not text:
        return text
    out = text
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in out:
            out = out.replace(secret, _redact(secret))
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: _redact(m.group(0)), out)
    return out


# ─────────────────────────── small collaborators (fakeable in tests) ───────────

def _run(cmd: "list[str]", timeout: float = 3.0) -> "tuple[int, str, str] | None":
    """Run a subprocess; (returncode, stdout, stderr) or None if it could not even
    start (binary missing, timeout, permission denied, ...)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception:
        return None


def _http_get_json(url: str, timeout: float = 3.0) -> dict:
    """GET url, return parsed JSON. Raises on any failure (caller decides the finding)."""
    req = urllib.request.Request(url, headers={"User-Agent": "cardloop-doctor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — localhost only
        return json.loads(r.read().decode("utf-8"))


def _port_listening(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _find_bot_processes(repo_root: Path) -> "list[int]":
    """PIDs of bot.py processes for THIS repo, by scanning /proc/*/cmdline. Linux-only
    (the deployment target — systemd); returns [] wherever /proc is absent."""
    target = str(repo_root / "bot.py")
    pids: "list[int]" = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    try:
        for entry in os.listdir(proc):
            if not entry.isdigit():
                continue
            try:
                cmdline = (proc / entry / "cmdline").read_bytes()
            except Exception:
                continue
            if target.encode() in cmdline:
                pids.append(int(entry))
    except Exception:
        pass
    return pids


def _get_totp_status(repo_root: Path = REPO_ROOT) -> "tuple[bool | None, str]":
    """(enabled, note). enabled=None when it cannot be determined (no vault yet,
    cryptography missing, ...) — that is NOT a failure, just unknown.

    secretstore.py is already a mandatory repo dependency (cryptography>=48 is pinned
    in requirements.txt for the whole app) — this is not a new dependency for doctor,
    just a read-only `.get()` on one reserved key. Never prints the secret itself."""
    try:
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import secretstore
        active = secretstore.get("__totp_secret__")
        return bool(active), ""
    except Exception as e:  # noqa: BLE001 — any failure here just means "unknown"
        return None, str(e)


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _dir_size(path: Path, budget_sec: float = 2.0) -> str:
    """Total size of path, walked in pure Python with a wall-clock budget so a huge
    data/ directory can never blow doctor's <5s target — times out gracefully."""
    if not path.exists():
        return "0B"
    start = time.monotonic()
    total = 0
    truncated = False
    for root, _dirs, files in os.walk(path):
        if time.monotonic() - start > budget_sec:
            truncated = True
            break
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    human = _human_bytes(total)
    return f"{human} (timed out scanning — run `du -sh data/` for the exact figure)" if truncated else human


def _count_json_entries(path: Path) -> "int | None":
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, (dict, list)) else None
    except Exception:
        return None


def _newest_mtime(path: Path) -> "float | None":
    newest = None
    if not path.exists():
        return None
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                m = os.path.getmtime(os.path.join(root, f))
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest


def _parse_mem_value(v: "str | None") -> "int | None":
    """systemd memory property -> bytes, or None for 'infinity'/'[not set]'/unparsable
    (i.e. "no ceiling" — never confuse that with 0)."""
    v = (v or "").strip()
    if not v or v in ("infinity", "[not set]"):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _parse_version(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def _installed_version(dist_name: str) -> "str | None":
    """importlib.metadata lookup, isolated into its own function so tests can fake
    an installed/missing package without touching the real interpreter's metadata."""
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


# ─────────────────────────── env / .env loading ─────────────────────────────────

def _load_dotenv_merged(repo_root: Path = REPO_ROOT) -> "tuple[dict, Path, bool]":
    """Mirror bot.py's own _load_env(): .env values fill GAPS in the real process
    env, never override it. Never mutates the real os.environ — returns a merged
    copy so doctor stays side-effect-free."""
    merged = dict(os.environ)
    env_path = repo_root / ".env"
    exists = env_path.exists()
    if exists and not os.environ.get("COPS_NO_DOTENV"):
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                merged.setdefault(k.strip(), v.strip())
    return merged, env_path, exists


# ─────────────────────────── Versions ────────────────────────────────────────────

def _sdk_latest_seen(repo_root: Path) -> "str | None":
    """Newest claude-agent-sdk release the cockpit's daily SDK watch has seen on PyPI.

    Doctor never hits the network itself (read-only, <5s), so it reads that cache.
    Absent until the cockpit has run at least one check — then this stays silent."""
    try:
        state = json.loads((repo_root / "data" / "sdk-version.json").read_text(encoding="utf-8"))
        return str(state.get("latest") or "").strip() or None
    except Exception:
        return None


def probe_versions(repo_root: Path = REPO_ROOT, run=_run, installed_version=_installed_version) -> "list[Fact]":
    facts: "list[Fact]" = []

    desc = run(["git", "-C", str(repo_root), "describe", "--tags", "--always", "--dirty"])
    branch = run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
    if desc and desc[0] == 0:
        dirty = desc[1].endswith("-dirty")
        br = branch[1] if branch and branch[0] == 0 else "?"
        facts.append(Fact(
            "Cardloop", f"{desc[1]} [{br}]",
            level="warn" if dirty else "ok",
            remedy=("working tree has uncommitted changes — a card worktree run needs a "
                     "clean tree; commit or stash first") if dirty else None,
        ))
    else:
        facts.append(Fact("Cardloop", "not a git checkout (git describe failed)", level="warn",
                           remedy="expected for a tarball install; self-update needs a git checkout"))

    facts.append(Fact("Python", sys.version.split()[0]))

    node = run(["node", "--version"])
    if node and node[0] == 0:
        facts.append(Fact("Node", node[1]))
    else:
        facts.append(Fact("Node", "not found", level="fail",
                           remedy="install Node 20+ (needed to build web/) — see README Quickstart"))

    bundled_cli = _vma._bundled_cli() if _vma else None
    if bundled_cli:
        ver = run([bundled_cli, "--version"])
        facts.append(Fact("claude (bundled)", f"{ver[1] if ver and ver[0] == 0 else '?'}  {bundled_cli}"))
    else:
        facts.append(Fact("claude (bundled)", "not found under venv/", level="warn",
                           remedy="venv missing or claude-agent-sdk not installed — "
                                  "pip install -r requirements.txt"))

    # CLAUDE_CLI_PATH overrides the bundle for the whole cockpit (engine.CLI_PATH), so which
    # binary is ACTIVE decides alias resolution and which model ids the API will accept.
    cli_override_raw = _vma._cli_path_raw() if _vma else ""
    cli_override = _vma._cli_path_override() if _vma else None
    if cli_override:
        ver = run([cli_override, "--version"])
        facts.append(Fact("claude (CLAUDE_CLI_PATH, ACTIVE)",
                          f"{ver[1] if ver and ver[0] == 0 else '?'}  {cli_override}"))
    elif cli_override_raw:
        facts.append(Fact("claude (CLAUDE_CLI_PATH)", f"{cli_override_raw} — not an executable file",
                          level="warn",
                          remedy="fix the path or unset CLAUDE_CLI_PATH; the bundled CLI is "
                                 "serving every run in the meantime"))

    path_cli = run(["claude", "--version"])
    if path_cli and path_cli[0] == 0:
        facts.append(Fact("claude (PATH, fallback only)", path_cli[1], level="info"))

    sdk_version = installed_version("claude-agent-sdk")
    req_txt = repo_root / "requirements.txt"
    floor = None
    if req_txt.exists():
        m = re.search(r"claude-agent-sdk\s*>=\s*([\d.]+)", req_txt.read_text(encoding="utf-8"))
        if m:
            floor = m.group(1)
    if sdk_version is None:
        facts.append(Fact("claude-agent-sdk", "not importable in this interpreter", level="fail",
                           remedy="run via venv/bin/python (make doctor), not the system python"))
    elif floor and _parse_version(sdk_version) < _parse_version(floor):
        facts.append(Fact(
            "claude-agent-sdk", f"{sdk_version} (requirements.txt floor: >={floor})", level="fail",
            remedy=f"pip install -U 'claude-agent-sdk>={floor}' — a stale bundled CLI silently "
                   "resolves a model alias to an OLDER model with no error "
                   "(see memory opus5-alias-staleness-2026-07-24). After upgrading, run "
                   "tools/verify_model_aliases.py for the live ground-truth cross-check.",
        ))
    else:
        note = f" (floor >={floor} OK)" if floor else ""
        facts.append(Fact("claude-agent-sdk", f"{sdk_version}{note}"))

    # Meeting the floor is NOT the same as being current — the floor is our own number,
    # and it only moves when someone bumps it by hand. Compare against what PyPI actually
    # has (cached by the cockpit's SDK watch loop).
    latest_seen = _sdk_latest_seen(repo_root)
    if sdk_version and latest_seen and _parse_version(latest_seen) > _parse_version(sdk_version):
        facts.append(Fact(
            "claude-agent-sdk (PyPI)", f"{latest_seen} available (running {sdk_version})",
            level="warn",
            remedy=f"venv/bin/pip install -U 'claude-agent-sdk=={latest_seen}', restart, then run "
                   "tools/verify_model_aliases.py — the bundled CLI decides alias resolution, so a "
                   "stale SDK runs an older model with no error.",
        ))

    codex_enabled = str(os.environ.get("CODEX_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
    if codex_enabled:
        codex_version = installed_version("openai-codex")
        if codex_version:
            facts.append(Fact("Codex SDK", f"{codex_version} (CODEX_ENABLED=true)"))
        else:
            facts.append(Fact("Codex SDK", "CODEX_ENABLED=true but openai-codex is not installed",
                               level="fail", remedy="pip install -r requirements.txt"))
    else:
        facts.append(Fact("Codex SDK", "disabled (CODEX_ENABLED=false)", level="info"))

    return facts


# ─────────────────────────── Auth ────────────────────────────────────────────────

def probe_auth(env: dict, cred_path: "Path | None" = None) -> "list[Fact]":
    facts: "list[Fact]" = []
    mode = env.get("CLAUDE_AUTH_MODE", "subscription")
    facts.append(Fact("CLAUDE_AUTH_MODE", mode))

    if cred_path is None:
        cred_path = Path(env.get("CLAUDE_CREDENTIALS_PATH") or "~/.claude/.credentials.json").expanduser()

    if cred_path.exists():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            oauth = data.get("claudeAiOauth") or {}
            expires_at = oauth.get("expiresAt")
            if expires_at:
                try:
                    exp_dt = datetime.fromtimestamp(int(expires_at) / 1000.0, tz=timezone.utc)
                    remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds()
                    sub = oauth.get("subscriptionType", "")
                    if remaining < 0:
                        facts.append(Fact("OAuth credentials", f"EXPIRED {exp_dt.isoformat()}", level="fail",
                                           remedy="run `claude login` to refresh the subscription token"))
                    else:
                        facts.append(Fact("OAuth credentials",
                                           f"present, expires {exp_dt.isoformat()}"
                                           + (f" ({sub})" if sub else "")))
                except Exception:
                    facts.append(Fact("OAuth credentials", "present (could not parse expiry)", level="warn"))
            else:
                facts.append(Fact("OAuth credentials", "present (no expiry field found)", level="warn"))
        except Exception as e:
            facts.append(Fact("OAuth credentials", f"present but unreadable ({e})", level="warn"))
    else:
        if mode == "subscription":
            facts.append(Fact("OAuth credentials", f"NOT FOUND at {cred_path}", level="fail",
                               remedy="run `claude login` to authenticate the subscription"))
        else:
            facts.append(Fact("OAuth credentials", f"not found at {cred_path} (ok — CLAUDE_AUTH_MODE=api_key)",
                               level="info"))

    api_key = env.get("ANTHROPIC_API_KEY", "")
    if api_key:
        redacted = _redact(api_key)
        if mode == "subscription":
            facts.append(Fact(
                "ANTHROPIC_API_KEY", f"SET ({redacted}) while CLAUDE_AUTH_MODE=subscription", level="fail",
                remedy="unset ANTHROPIC_API_KEY — bot.py pops it at process start for subscription mode, "
                       "but its mere presence risks OTHER tools/processes silently billing the API",
            ))
        else:
            facts.append(Fact("ANTHROPIC_API_KEY", f"set ({redacted}) — CLAUDE_AUTH_MODE=api_key, "
                                                     "billing goes to the Anthropic API (intentional opt-in)",
                               level="warn"))
    else:
        if mode == "api_key":
            facts.append(Fact("ANTHROPIC_API_KEY", "NOT SET but CLAUDE_AUTH_MODE=api_key", level="fail",
                               remedy="set ANTHROPIC_API_KEY in .env, or switch CLAUDE_AUTH_MODE=subscription"))
        else:
            facts.append(Fact("ANTHROPIC_API_KEY", "not set (correct for subscription auth)"))

    facts.extend(_probe_accounts())
    return facts


def _probe_accounts() -> "list[Fact]":
    """Extra subscriptions (data/accounts.json). Silent on a single-account install.

    Worth checking every time because the failure mode is invisible: an active account whose
    config dir vanished degrades to `main`, and one whose `projects/` is not shared with
    ~/.claude quietly builds a second, separate chat history.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT))
        import accounts as _accounts
    except Exception:
        return []
    try:
        rows = _accounts.list_accounts()
    except Exception as exc:
        return [Fact("Accounts", f"could not read accounts.json ({exc})", level="warn")]
    if len(rows) < 2:
        return []  # nothing registered — nothing to say

    facts = [Fact("Accounts", f"{len(rows)} subscriptions, active = {_accounts.active_id()}")]
    for row in rows:
        if row["is_main"]:
            continue
        name = f"Account '{row['id']}'"
        who = row["email"] or row["config_dir"]
        if not row["ok"]:
            facts.append(Fact(name, f"unusable — {row['reason']}", level="warn",
                              remedy=f"tools/claude-acct login {row['id']}"))
            continue
        if not row["shared_ok"]:
            facts.append(Fact(name, f"{who} — NOT sharing {', '.join(row['shared_broken'])} with ~/.claude",
                              level="warn",
                              remedy="session resume and chat history diverge on this account; "
                                     f"re-link with: tools/claude-acct new {row['id']}"))
        else:
            facts.append(Fact(name, f"{who} ({row['plan'] or 'plan unknown'})"))
    return facts


# ─────────────────────────── Config ──────────────────────────────────────────────

def probe_config(env: dict, env_path: Path, env_exists: bool,
                  totp_status=_get_totp_status, repo_root: Path = REPO_ROOT) -> "list[Fact]":
    facts: "list[Fact]" = []

    if os.environ.get("COPS_NO_DOTENV"):
        facts.append(Fact(".env", f"COPS_NO_DOTENV set — {env_path} is NOT auto-loaded", level="info"))
    elif env_exists:
        facts.append(Fact(".env", str(env_path)))
    else:
        facts.append(Fact(".env", f"NOT FOUND at {env_path}", level="fail",
                           remedy="cp .env.example .env and set WEB_PASSWORD (or run ./install.sh)"))

    facts.append(Fact("Bind", f"{env.get('WEB_HOST', '127.0.0.1')}:{env.get('WEB_PORT', '8787')}"))

    pw = env.get("WEB_PASSWORD", "")
    if not pw:
        facts.append(Fact("WEB_PASSWORD", "NOT SET", level="fail",
                           remedy="set WEB_PASSWORD in .env — bot.py refuses to start with a blank password"))
    elif pw.strip().upper() == "CHANGE_ME":
        facts.append(Fact("WEB_PASSWORD", "still the placeholder CHANGE_ME", level="fail",
                           remedy="set a real password in .env — bot.py refuses to start with the placeholder"))
    else:
        facts.append(Fact("WEB_PASSWORD", "set"))

    # spec-096 P3.1: the runtime ignores a blank/placeholder salt and uses a generated one stored in
    # data/cookie_salt, so this is a notice, not a failure. The value is never echoed.
    salt = env.get("WEB_COOKIE_SALT", "")
    salt_is_placeholder = (_auth_salt.is_placeholder(salt) if _auth_salt
                           else salt.strip().lower().startswith("change_me"))
    if salt.strip() and not salt_is_placeholder:
        facts.append(Fact("WEB_COOKIE_SALT", "set"))
    elif salt.strip():
        facts.append(Fact("WEB_COOKIE_SALT", "a placeholder — ignored, a generated salt is used instead",
                           level="warn",
                           remedy="blank the value in .env (the cockpit then generates a private salt in "
                                  "data/cookie_salt), or set your own long random string"))
    else:
        facts.append(Fact("WEB_COOKIE_SALT", "blank — a private salt is generated and stored in data/cookie_salt",
                           level="info"))

    enabled, note = totp_status(repo_root=repo_root)
    if enabled is None:
        facts.append(Fact("TOTP", f"unknown ({note})" if note else "unknown", level="info"))
    else:
        facts.append(Fact("TOTP", "on" if enabled else "off"))

    facts.append(Fact("CARDLOOP_SERVICE", env.get("CARDLOOP_SERVICE") or "cardloop (default)"))
    facts.append(Fact("RESPONSE_LANGUAGE", env.get("RESPONSE_LANGUAGE") or "(none — agent replies in English)"))
    facts.append(Fact("DEFAULT_EFFORT", env.get("DEFAULT_EFFORT") or "high (default)"))

    return facts


# ─────────────────────────── Service ─────────────────────────────────────────────

def probe_service(service_name: str, run=_run, cgroup_root: Path = Path("/sys/fs/cgroup")) -> "list[Fact]":
    facts: "list[Fact]" = []

    show = run(["systemctl", "show", service_name,
                "-p", "ActiveState", "-p", "SubState", "-p", "MemoryHigh",
                "-p", "MemoryMax", "-p", "MemoryCurrent", "-p", "MainPID",
                "-p", "ControlGroup", "-p", "NRestarts", "-p", "OOMPolicy"])
    if not show or show[0] != 0:
        facts.append(Fact("systemd", f"could not query unit '{service_name}' "
                                      "(systemctl unavailable, no permission, or not systemd)",
                           level="info", remedy="if this host doesn't use systemd, ignore this section"))
        return facts

    props = dict(line.split("=", 1) for line in show[1].splitlines() if "=" in line)
    active, sub = props.get("ActiveState", "?"), props.get("SubState", "?")
    if active == "active":
        level, remedy = "ok", None
    elif active in ("activating", "reloading"):
        level, remedy = "warn", f"unit is {active}/{sub} — re-check shortly"
    else:
        level, remedy = "fail", f"unit is {active}/{sub} — check `journalctl -u {service_name} -n 50`"
    facts.append(Fact("systemd unit", f"{service_name}: {active}/{sub}", level=level, remedy=remedy))

    mh_raw, mm_raw = props.get("MemoryHigh"), props.get("MemoryMax")
    mh, mm = _parse_mem_value(mh_raw), _parse_mem_value(mm_raw)
    if mh is not None and mm is not None and mh < mm:
        facts.append(Fact(
            "MemoryHigh/MemoryMax", f"{mh_raw} / {mm_raw}", level="fail",
            remedy=f"MemoryHigh < MemoryMax throttles the WHOLE cgroup instead of OOM-killing the "
                   "offending process — the cockpit can freeze solid while `systemctl is-active` still "
                   f"says active. Fix: systemctl set-property {service_name} MemoryHigh=infinity",
        ))
    else:
        facts.append(Fact("MemoryHigh/MemoryMax", f"{mh_raw or '(unset)'} / {mm_raw or '(unset)'}"))

    # systemd's default OOMPolicy=stop turns ONE OOM-killed child (an agent's ffmpeg or python
    # job) into a stop of the whole unit: every live chat and sub-agent dies, then Restart=
    # brings the cockpit back as if nothing happened. Measured 2026-09-23: two such restarts in
    # one evening, each killing ~10 live turns because one agent's job outgrew its share.
    oom_policy = props.get("OOMPolicy")
    if oom_policy == "stop":
        facts.append(Fact(
            "OOMPolicy", oom_policy, level="fail",
            remedy="one OOM-killed agent child stops the WHOLE cockpit (all chats + sub-agents). "
                   "Add `OOMPolicy=continue` to the [Service] section, then `systemctl daemon-reload` "
                   "(no restart needed)",
        ))
    elif oom_policy:
        facts.append(Fact("OOMPolicy", oom_policy))

    mc = _parse_mem_value(props.get("MemoryCurrent"))
    if mc is not None:
        # Headroom matters, not the absolute number: every live client is a ~0.5 GB CLI
        # subprocess, so a cockpit sitting at 80-90% of MemoryMax is one fan-out away from
        # having a child OOM-killed mid-turn (which the operator experiences as a frozen chat
        # or a dead browser pane, not as an error).
        # Judge the WORKING SET, not memory.current: the raw figure counts reclaimable page cache and
        # slab, so on a host that reads files (git, backups, a `find`) it sits near the limit while
        # nothing is wrong — the same false alarm that once made the memory guard evict real chats.
        ws = None
        cg_rel = (props.get("ControlGroup") or "").strip()
        if cg_rel:
            try:
                import load_monitor
                ws = load_monitor.cgroup_working_set_bytes(cgroup_root / cg_rel.lstrip("/"))
            except Exception:
                ws = None
        frac = ((ws if ws is not None else mc) / mm) if mm else None
        pct = f" ({frac * 100:.0f}% of MemoryMax)" if frac is not None else ""
        if frac is not None and frac >= 0.90:
            level, remedy = "fail", ("almost out of headroom — an OOM kill mid-turn is imminent. "
                                     "Lower LIVE_CLIENT_MAX / LIVE_CLIENT_TTL_SEC in .env, or raise "
                                     f"MemoryMax: systemctl set-property {service_name} MemoryMax=...")
        elif frac is not None and frac >= 0.75:
            level, remedy = "warn", ("little headroom left — LIVE_CLIENT_MEM_GUARD should be evicting "
                                     "idle clients; if it is not, lower LIVE_CLIENT_MAX in .env")
        else:
            level, remedy = "ok", None
        shown = (f"{ws // (1024 * 1024)} MiB working set{pct}; raw {mc // (1024 * 1024)} MiB incl. reclaimable cache"
                 if ws is not None else f"{mc // (1024 * 1024)} MiB{pct}")
        facts.append(Fact("MemoryCurrent", shown, level=level, remedy=remedy))

    # OOM history for THIS cgroup since its last start. A restart resets the counter, so a
    # non-zero value means the kernel killed a child of the currently running service — the
    # single most misleading failure mode there is, because `systemctl is-active` still says
    # active and the only trace in the cockpit is work that silently stopped.
    cgroup = (props.get("ControlGroup") or "").strip()
    if cgroup:
        try:
            events = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "memory.events"
            kills = 0
            for line in events.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    kills = int(value)
            if kills:
                facts.append(Fact(
                    "OOM kills", f"{kills} since this unit started", level="fail",
                    remedy="the kernel killed a child process inside the cgroup (a CLI subprocess or "
                           "sub-agent) — work stopped silently. See `journalctl -k | grep -i oom` and "
                           "lower LIVE_CLIENT_MAX / raise MemoryMax",
                ))
            else:
                facts.append(Fact("OOM kills", "none since this unit started"))
        except Exception:
            pass
    restarts = props.get("NRestarts")
    if restarts and restarts.isdigit() and int(restarts) > 0:
        facts.append(Fact(
            "Restarts", f"{restarts} since boot", level="warn",
            remedy=f"systemd restarted the unit — check why: `journalctl -u {service_name} "
                   "| grep -E \"Failed with result|Stopped\"`",
        ))

    journal = run(["journalctl", "-u", service_name, "-n", "15", "-p", "warning", "--no-pager"])
    if journal and journal[0] == 0:
        text = journal[1].strip()
        if text and "-- No entries --" not in text:
            n = len(text.splitlines())
            facts.append(Fact("Recent warnings", f"{n} line(s) at priority <= warning in the last 15 — "
                                                   f"see `journalctl -u {service_name} -p warning -n 15`",
                               level="warn"))
        else:
            facts.append(Fact("Recent warnings", "none"))
    else:
        facts.append(Fact("Recent warnings", "could not read the journal (no permission / not systemd)",
                           level="info"))

    return facts


# ─────────────────────────── Runtime ─────────────────────────────────────────────

def probe_runtime(port: str, repo_root: Path = REPO_ROOT, http_get=_http_get_json,
                   find_procs=_find_bot_processes, port_listening=_port_listening) -> "list[Fact]":
    facts: "list[Fact]" = []
    port_i = int(port) if str(port).isdigit() else 8787

    url = f"http://127.0.0.1:{port_i}/api/health?deep=1"
    try:
        data = http_get(url)
        facts.append(Fact("GET /api/health?deep=1",
                           f"ok — running={data.get('running')} agents={data.get('agents')} "
                           f"plan_pending={data.get('plan_pending')}"))
    except Exception as e:
        facts.append(Fact("GET /api/health?deep=1", f"unreachable ({e})", level="fail",
                           remedy=f"cockpit not answering on 127.0.0.1:{port_i} — "
                                  "check the service is running and WEB_PORT matches"))

    pids = find_procs(repo_root)
    if not pids:
        facts.append(Fact("bot.py processes", "none found", level="info"))
    elif len(pids) == 1:
        facts.append(Fact("bot.py processes", f"1 (pid {pids[0]})"))
    else:
        facts.append(Fact("bot.py processes", f"{len(pids)} running: pids {pids}", level="warn",
                           remedy="multiple bot.py instances can fight over the same port/data/ files "
                                  "— stop the extras"))

    listening = port_listening("127.0.0.1", port_i)
    facts.append(Fact(f"port {port_i}", "listening" if listening else "NOT listening",
                       level="ok" if listening else "fail",
                       remedy=None if listening else "nothing is bound to this port — the cockpit is not running"))

    dist_index = repo_root / "web" / "dist" / "index.html"
    src_dir = repo_root / "web" / "src"
    if not dist_index.exists():
        facts.append(Fact("web/dist", "MISSING", level="fail", remedy="cd web && npm run build"))
    else:
        newest_src = _newest_mtime(src_dir)
        if newest_src and newest_src > dist_index.stat().st_mtime:
            facts.append(Fact("web/dist", "STALE — web/src has files newer than the last build", level="fail",
                               remedy="cd web && npm run build"))
        else:
            facts.append(Fact("web/dist", "up to date"))

    return facts


# ─────────────────────────── Data ────────────────────────────────────────────────

def _data_permissions_fact(data_dir: Path) -> Fact:
    """spec-096 P2: data/ holds chat history, tokens' metadata and the Web Push private key.
    Secret files inside are 0600 on their own; a group/other-accessible directory still lets a
    second local user list names and read every file that is not. Warn only — making data/ 0700
    is the operator's call (a backup job or another user may legitimately read it)."""
    try:
        mode = data_dir.stat().st_mode & 0o777
    except OSError as e:
        return Fact("data/ permissions", f"unreadable ({e})", level="info")
    if mode & 0o077:
        return Fact("data/ permissions", f"{mode:04o} (group/other can enter it)", level="warn",
                    remedy=f"chmod 700 {data_dir} — after checking that no backup job or other "
                           "user reads it (secret files inside are 0600 regardless)")
    return Fact("data/ permissions", f"{mode:04o} (owner only)")


def probe_data(repo_root: Path = REPO_ROOT) -> "list[Fact]":
    facts: "list[Fact]" = []
    data_dir = repo_root / "data"

    if not data_dir.exists():
        facts.append(Fact("data/", "missing (created automatically on first run of bot.py)", level="info"))
        return facts

    facts.append(Fact("data/ size", _dir_size(data_dir)))
    facts.append(_data_permissions_fact(data_dir))

    topics = _count_json_entries(data_dir / "topics.json")
    sessions = _count_json_entries(data_dir / "sessions.json")
    registry = _count_json_entries(data_dir / "registry.json")
    facts.append(Fact("topics.json", f"{topics} entries" if topics is not None else "absent"))
    facts.append(Fact("sessions.json", f"{sessions} entries" if sessions is not None else "absent"))
    facts.append(Fact("registry.json", f"{registry} entries" if registry is not None else "absent (optional)"))

    wt_dir = repo_root / ".worktrees"
    wt_count = len(list(wt_dir.glob("card-*"))) if wt_dir.exists() else 0
    facts.append(Fact("card worktrees", f"{wt_count} present" if wt_count else "none", level="info"))

    if _board is not None:
        try:
            _raw, _preamble, cols = _board._load_board(str(repo_root))
            summary = ", ".join(f"{_board._COLUMN_LABEL.get(k, k)}={len(v)}" for k, v in cols.items())
            facts.append(Fact("board (TASKS.md)", summary or "empty"))
        except Exception as e:
            facts.append(Fact("board (TASKS.md)", f"could not parse ({e})", level="warn"))
    else:
        facts.append(Fact("board (TASKS.md)", "board.py not importable", level="info"))

    return facts


# ─────────────────────────── Grok (spec-095 §5.9) ────────────────────────────────

# Thresholds live here (not inline) so a test can name them and the report can quote them.
GROK_CMD_TIMEOUT_SEC = 3.0               # `grok --version` / `grok inspect --json`: bounded, never a hang
GROK_AGENT_MAX_AGE_SEC = 15 * 60         # H7: a per-turn `grok agent` must never outlive its turn
GROK_LITTER_WARN = 20                    # §10-C7: ~2 sandbox-blocked* entries per spawn, reaped per turn
GROK_PROJECT_INSPECT_TIMEOUT_SEC = 5.0   # one `inspect --json` per opted-in project, under the sandbox
GROK_PROJECTS_MAX = 12                   # inspect calls per doctor run; the rest is reported as not checked
GROK_USAGE_WARN_BYTES = 10 * 1024 * 1024         # ~300 B/turn: this is ~35k turns, or a runaway loop
GROK_LIMIT_ERRORS_WARN_BYTES = 2 * 1024 * 1024   # rows of up to 8000 chars, only quota-shaped text

_GROK_GUARDED_VENDORS = ("claude", "cursor")


def _env_truthy(value: "str | None") -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _run_group(cmd: "list[str]", timeout: float = GROK_CMD_TIMEOUT_SEC, env: "dict | None" = None,
               cwd: "str | None" = None) -> "tuple[int, str, str] | None":
    """`_run` for a CLI that may fork helpers: its own process group, and the WHOLE group is
    killed on timeout and afterwards, so a hung probe can neither stall doctor (an orphan
    holding the pipe open would block `communicate()`) nor outlive it. None = could not
    finish (missing, timeout, permission denied...)."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace", env=env,
                                cwd=cwd, start_new_session=True)
    except Exception:
        return None
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "").strip(), (err or "").strip()
    except Exception:
        return None
    finally:
        _kill_group(proc.pid)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=1.0)


@contextlib.contextmanager
def _env_overlay(env: dict):
    """Make `env` (os.environ + .env gaps, as collect() builds it) the process environment for
    the keys grok_engine reads from os.environ itself, then put everything back. The engine's
    helpers take no env parameter — this is what lets doctor resolve GROK_HOME / GROK_BIN /
    the deny list through the engine's own code instead of a copy that could drift."""
    keys = {k for k in set(os.environ) | set(env) if k.startswith("GROK_")}
    saved = {k: os.environ.get(k) for k in keys | {"PATH", "HOME"}}
    try:
        for k in keys:                       # GROK_*: the merged env is the whole truth
            if k in env:
                os.environ[k] = env[k]
            else:
                os.environ.pop(k, None)
        for k in ("PATH", "HOME"):           # what the engine's child env and `~` read; never unset
            if k in env:
                os.environ[k] = env[k]
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fmt_age(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"


def _list_grok_agents(home: Path, proc_root: Path = Path("/proc")) -> "list[dict]":
    """Live `grok agent --no-leader stdio` processes that belong to THIS cockpit's GROK_HOME:
    [{pid, pgid, age}] (age = seconds since start, None when /proc/uptime is unreadable).

    The engine always spawns that exact argv with GROK_HOME in the child env, so the home is
    what tells the cockpit's turns apart from an operator's own `grok agent` (an editor
    integration, say). A process whose environ cannot be read is counted: when whose it is
    cannot be told, "leftover" is the safe guess. Linux only (/proc), [] elsewhere."""
    if not proc_root.is_dir():
        return []
    try:
        uptime: "float | None" = float((proc_root / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    try:
        tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        tck = 100
    want = os.path.realpath(home)
    found: "list[dict]" = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        base = proc_root / entry
        try:
            argv = (base / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not (b"agent" in argv and b"--no-leader" in argv and b"stdio" in argv):
            continue
        try:
            envs: "list[bytes] | None" = (base / "environ").read_bytes().split(b"\0")
        except OSError:
            envs = None
        if envs is not None:
            owner = next((e[len(b"GROK_HOME="):] for e in envs if e.startswith(b"GROK_HOME=")), None)
            if owner is None or os.path.realpath(owner.decode("utf-8", "replace")) != want:
                continue
        try:
            raw = (base / "stat").read_text()
            rest = raw[raw.rindex(")") + 2:].split()
            state, pgid, start = rest[0], int(rest[2]), int(rest[19])
        except (OSError, ValueError, IndexError):
            continue
        if state == "Z":
            continue
        found.append({"pid": int(entry), "pgid": pgid,
                      "age": (uptime - start / tck) if uptime is not None else None})
    return found


@dataclass
class _Grok:
    """What every Grok probe needs, resolved ONCE through the engine's own helpers."""
    ge: object
    home: Path
    data: Path
    ctx: dict
    env: dict                      # the engine's child env without the sandbox var (as its probes use)
    run: object
    now: object
    proc_root: Path
    scratch: Path                  # throwaway dir: the CLI writes into GROK_HOME even for --version/inspect
    binary: "str | None" = None
    version: "str | None" = None

    def scratch_env(self) -> dict:
        """The engine's child env, but pointed at a throwaway GROK_HOME: `grok --version` creates a
        missing home and `grok inspect` rewrites <home>/docs on every run, and doctor is read-only."""
        return self.ge.child_env(self.scratch / "home", sandbox=False)


def probe_grok(env: dict, repo_root: Path = REPO_ROOT, run=_run_group, proc_root: Path = Path("/proc"),
               now=time.time, secrets_out: "list | None" = None) -> "list[Fact]":
    """Facts about the optional Grok provider. [] (silent) unless GROK_ENABLED is on.

    Never starts a model turn and never writes: the sandbox-denial probe is the engine's, it
    costs a real turn, and doctor only reads its cached verdict. `secrets_out` receives the
    values this probe learned that must never print (the account email), for render-time scrub."""
    if not _env_truthy(env.get("GROK_ENABLED")):
        return []
    with _env_overlay(env):
        try:
            import grok_engine as ge
        except Exception as exc:  # noqa: BLE001 — py < 3.11 (tomllib), a broken module, ...
            return [Fact("Grok", f"GROK_ENABLED=true but grok_engine cannot be imported ({exc})",
                         level="warn", remedy="run via venv/bin/python (make doctor), not the system python")]
        data = Path(env.get("_CARDLOOP_DATA_DIR") or (repo_root / "data"))
        ctx = {"DATA": data}
        home = ge.grok_home(ctx)
        facts: "list[Fact]" = []
        with tempfile.TemporaryDirectory(prefix="doctor-grok-") as scratch:
            (Path(scratch) / "cwd").mkdir()
            g = _Grok(ge=ge, home=home, data=data, ctx=ctx, env=ge.child_env(home, sandbox=False),
                      run=run, now=now, proc_root=proc_root, scratch=Path(scratch), binary=ge.grok_bin())
            for name, step in (("CLI", lambda: _grok_cli(g)),
                               ("auth", lambda: _grok_auth(g, secrets_out)),
                               ("sandbox", lambda: _grok_sandbox(g)),
                               ("compat", lambda: _grok_compat(g)),
                               ("folder trust", lambda: _grok_folder_trust(g)),
                               ("compat (projects)", lambda: _grok_compat_projects(g)),
                               ("processes", lambda: _grok_processes(g)),
                               ("usage files", lambda: _grok_usage_files(g))):
                try:
                    facts.extend(step())
                except Exception as exc:  # noqa: BLE001 — one crashing step must not hide the others
                    facts.append(Fact(f"Grok {name}", f"probe crashed: {exc}", level="warn",
                                      remedy="file an issue with this output (doctor is read-only; nothing "
                                             "was changed)"))
        return facts


def _read_toml(path: Path) -> "dict | None":
    import tomllib
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _grok_cli(g: _Grok) -> "list[Fact]":
    ge = g.ge
    if not g.binary:
        raw = os.environ.get("GROK_BIN", "").strip()
        value = (f"GROK_BIN={raw} is not an executable file" if raw else
                 "not found (GROK_BIN unset, no `grok` on PATH, no ~/.grok/bin/grok)")
        return [Fact("Grok CLI", value, level="fail",
                     remedy="install it (curl -fsSL https://x.ai/cli/install.sh | bash) or point GROK_BIN "
                            "in .env at the binary — until then every Grok turn is refused")]
    facts: "list[Fact]" = []
    res = g.run([g.binary, "--version"], timeout=GROK_CMD_TIMEOUT_SEC, env=g.scratch_env(),
                cwd=str(g.scratch / "cwd"))
    m = None
    if res is None:
        facts.append(Fact("Grok CLI", f"`{g.binary} --version` did not finish in {GROK_CMD_TIMEOUT_SEC:.0f}s "
                                      "(or could not start)", level="warn",
                          remedy="run it by hand; a hung or non-executable binary refuses every Grok turn"))
    else:
        code, out, err = res
        m = ge._VERSION_RE.match(out.strip())
        if code != 0 or not m:
            facts.append(Fact("Grok CLI", f"{g.binary}: unrecognised `--version` output (exit {code})",
                              level="fail",
                              remedy="the engine only accepts `grok X.Y.Z …`; reinstall the CLI or fix GROK_BIN"))
            m = None
    if m:
        g.version = m.group(1)
        known = ", ".join(ge.KNOWN_GOOD_VERSIONS)
        if g.version in ge.KNOWN_GOOD_VERSIONS:
            facts.append(Fact("Grok CLI", f"{g.version} (known-good) — {g.binary}"))
        else:
            facts.append(Fact(
                "Grok CLI", f"{g.version} is not on the known-good list ({known}) — {g.binary}", level="warn",
                remedy=f"the adapter was verified against {known} only and the stream format can drift. Run "
                       "`venv/bin/python -m pytest tests/test_grok_live.py -m 'grok_live or grok_canary'` "
                       f"against this build, then add {g.version} to KNOWN_GOOD_VERSIONS in grok_engine.py "
                       "(the binary changed under us: e.g. an interactive `grok` auto-updating ~/.grok)"))
    facts.append(_grok_autoupdate(g))
    return facts


def _grok_autoupdate(g: _Grok) -> Fact:
    env_off = g.env.get("GROK_DISABLE_AUTOUPDATER") == "1"
    cfg = _read_toml(g.home / "config.toml")
    cfg_val = (cfg.get("cli") or {}).get("auto_update") if isinstance(cfg, dict) else None
    if not env_off:
        return Fact("Grok auto-update", "NOT disabled for engine turns (GROK_DISABLE_AUTOUPDATER is not in "
                                         "the child env)", level="warn",
                    remedy="a turn could swap the binary under a running cockpit — D3 in grok_engine.py must "
                           "carry GROK_DISABLE_AUTOUPDATER=1")
    if cfg_val is True:
        return Fact("Grok auto-update", f"off for engine turns, but {g.home / 'config.toml'} sets "
                                         "auto_update = true", level="warn",
                    remedy="the engine rewrites config.toml on the next turn; until then a `grok` run "
                           "with this GROK_HOME (e.g. tools/grok-acct login) may replace the shared binary")
    if cfg_val is False:
        note = "config.toml: auto_update = false"
    elif (g.home / "config.toml").exists():
        note = "config.toml unreadable or without a [cli] auto_update setting — rewritten on the next turn"
    else:
        note = "config.toml not generated yet"
    return Fact("Grok auto-update", f"off (GROK_DISABLE_AUTOUPDATER=1 in the child env; {note})")


def _grok_auth(g: _Grok, secrets_out: "list | None") -> "list[Fact]":
    a = g.ge.read_auth_facts(g.home)
    if a["email"] and secrets_out is not None:
        secrets_out.append(a["email"])
    login = "tools/grok-acct login"
    if not a["present"]:
        return [Fact("Grok auth", f"no login under {g.home} (auth.json missing or unreadable)", level="fail",
                     remedy=login)]
    if not a["oidc"]:
        return [Fact("Grok auth", "login is not a grok.com (OIDC) subscription login — API-key auth is refused",
                     level="fail", remedy=login)]
    if not a["retention_opt_out"]:
        return [Fact("Grok auth", "oidc login, but coding_data_retention_opt_out is not true — the engine "
                                  "refuses to send code to xAI", level="fail",
                     remedy="opt out of coding-data retention in the Grok account settings (grok.com), "
                            f"then `{login}`")]
    who = f" as {_redact(a['email'])}" if a["email"] else ""
    return [Fact("Grok auth", f"signed in{who} (oidc), coding-data retention opt-out: yes")]


def _grok_sandbox(g: _Grok) -> "list[Fact]":
    ge = g.ge
    facts: "list[Fact]" = []

    bwrap = shutil.which("bwrap", path=g.env.get("PATH"))
    if bwrap:
        facts.append(Fact("Grok sandbox (bwrap)", bwrap))
    else:
        facts.append(Fact("Grok sandbox (bwrap)", "bubblewrap not found on PATH", level="fail",
                          remedy="apt install bubblewrap — Grok turns are refused without the sandbox "
                                 "(no unsandboxed fallback exists)"))

    home = g.home
    if home.is_symlink():
        facts.append(Fact("Grok sandbox (GROK_HOME)", f"{home} is a symlink", level="fail",
                          remedy="point GROK_HOME at a real directory (Grok and the engine both refuse a "
                                 "symlinked home), then `tools/grok-acct login`"))
    elif home.exists() and not home.is_dir():
        facts.append(Fact("Grok sandbox (GROK_HOME)", f"{home} exists and is not a directory", level="fail",
                          remedy="move it away or point GROK_HOME elsewhere"))
    elif not home.exists():
        facts.append(Fact("Grok sandbox (GROK_HOME)", f"{home} does not exist yet (created by the first login)",
                          level="info"))
    else:
        facts.append(Fact("Grok sandbox (GROK_HOME)", str(home)))

    deny = None
    skipped: "list[str]" = []
    try:
        deny, skipped = ge.build_deny(home, g.ctx, bin_path=g.binary)
    except ge.GrokUnavailableError as exc:
        facts.append(Fact("Grok sandbox (profile)", f"deny list invalid: {exc}", level="fail",
                          remedy="fix GROK_SANDBOX_DENY in .env — the engine refuses every turn with this list"))
    else:
        facts.append(_grok_profile_fact(g, deny, skipped))

    fp = None
    if g.version and deny is not None:
        fp = ge._probe_fingerprint(g.version, {"deny": deny, "home": home})
    facts.append(_grok_probe_fact(g, fp))
    return facts


def _grok_profile_fact(g: _Grok, deny: "list[str]", skipped: "list[str]") -> Fact:
    ge = g.ge
    path = g.home / "sandbox.toml"
    name = "Grok sandbox (profile)"
    try:
        on_disk = path.read_text(encoding="utf-8")
    except OSError:
        return Fact(name, f"{path} not generated yet", level="warn",
                    remedy="written by the first Grok turn or the cockpit's startup provider probe "
                           "(grok_engine.ensure_home); doctor never writes it")
    if on_disk == ge._sandbox_toml(deny):
        gap = f", {len(skipped)} listed path(s) missing on this host and skipped" if skipped else ""
        return Fact(name, f"profile '{ge.SANDBOX_PROFILE}' (extends workspace), {len(deny)} deny entries{gap}")
    cfg = _read_toml(path)
    try:
        n = len(cfg["profiles"][ge.SANDBOX_PROFILE]["deny"])
    except (TypeError, KeyError):
        return Fact(name, f"{path} is unreadable or has no [profiles.{ge.SANDBOX_PROFILE}]", level="warn",
                    remedy="rewritten by the engine on the next turn")
    return Fact(name, f"{n} deny entries on disk, the engine would write {len(deny)} now (GROK_SANDBOX_DENY, "
                      "$HOME or the CLI path changed since the last turn)", level="warn",
                remedy="rewritten on the next turn; the cached sandbox probe is judged against the NEW list "
                       "and will be re-run (one small model turn)")


def _grok_probe_fact(g: _Grok, fp: "str | None") -> Fact:
    """The engine's cached sandbox-denial verdict. Doctor must not produce one: it costs a model turn."""
    ge = g.ge
    name = "Grok sandbox (probe)"
    path = g.data / "grok_sandbox_probe.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return Fact(name, "no verdict recorded — the sandbox denial has never been proven on this host",
                    level="warn",
                    remedy="the cockpit's startup provider probe records one (it spends one small model turn); "
                           "until then every Grok turn is refused. Doctor never runs a model turn itself")
    try:
        cached = json.loads(raw)
        state = cached["state"]
        ts = float(cached.get("ts", 0))
        if not isinstance(state, str):
            raise TypeError("state")
    except (ValueError, KeyError, TypeError):
        return Fact(name, f"{path} is unreadable", level="warn",
                    remedy="the engine re-runs the probe when the cockpit next checks the provider")
    detail = str(cached.get("detail") or "")[:200]
    age = g.now() - ts
    fp_match = None if fp is None else (cached.get("fingerprint") == fp)
    when = f"{_fmt_age(age)} ago"
    if state == "failed":
        if fp_match is False:
            return Fact(name, f"stale FAILED verdict ({when}) for a different CLI/deny list/home — "
                              "will be re-probed", level="warn",
                        remedy="the engine re-runs the probe (one small model turn) at the next provider check")
        return Fact(name, f"FAILED {when}: {detail}", level="fail",
                    remedy="the kernel deny list did NOT hide the canary file from the model's shell, so "
                           "Grok turns are refused. Check bubblewrap/Landlock on this host, then delete "
                           f"{path} and restart the cockpit to re-probe")
    if state == "ok":
        if fp_match is True and age < ge.SANDBOX_PROBE_OK_TTL_SEC:
            return Fact(name, f"ok {when}: {detail}")
        why = ("recorded for a different CLI version / deny list / home" if fp_match is False else
               "older than the cache TTL" if age >= ge.SANDBOX_PROBE_OK_TTL_SEC else
               "CLI version unknown, freshness cannot be checked")
        return Fact(name, f"stale ok verdict ({when}): {why}", level="warn",
                    remedy="the engine re-runs the probe (one small model turn) at the next provider check")
    if state == "inconclusive":
        return Fact(name, f"inconclusive {when}: {detail}", level="warn",
                    remedy="the engine fails closed (turns refused) and retries after ~15 min; check the login "
                           "and the network, `journalctl -u cardloop | grep '\\[grok\\]'` has the reason")
    return Fact(name, f"unrecognised verdict {state!r}", level="warn",
                remedy=f"delete {path}; the engine writes a fresh one")


def _grok_compat(g: _Grok) -> "list[Fact]":
    """H1: under the engine's env, `grok inspect --json` must show none of OUR Claude/Cursor
    surface. Strict about the format — a CLI that renames a key must read as 'cannot tell',
    never as '0 found'. Runs in a neutral cwd against a throwaway home that carries only the
    cockpit home's config.toml, so the result does not depend on where GROK_HOME lives (the
    default is inside the repo, which has its own .mcp.json) and nothing real is written."""
    name = "Grok compat"
    if not g.binary or not g.version:
        return [Fact(name, "not checked (no usable Grok CLI)", level="info")]
    scratch_home = g.scratch / "home"
    scratch_home.mkdir(exist_ok=True)
    with contextlib.suppress(OSError):
        shutil.copyfile(g.home / "config.toml", scratch_home / "config.toml")
    res = g.run([g.binary, "inspect", "--json"], timeout=GROK_CMD_TIMEOUT_SEC, env=g.scratch_env(),
                cwd=str(g.scratch / "cwd"))
    if res is None:
        return [Fact(name, f"could not inspect (no answer within {GROK_CMD_TIMEOUT_SEC:.0f}s, or the "
                           "CLI could not start)", level="warn",
                     remedy="run `grok inspect --json` by hand under the engine's env; until it answers the "
                            "no-Claude-config guarantee is unverified")]
    code, out, err = res
    if code != 0:
        return [Fact(name, f"could not inspect (exit {code}): {(err or out)[:120]}", level="warn",
                     remedy="the no-Claude-config guarantee is unverified until `grok inspect --json` works")]
    try:
        doc = json.loads(out)
        lists = {k: doc[k] for k in ("mcpServers", "hooks", "skills", "agents")}
        cells = doc["externalCompat"]["cells"]
        ok = (all(isinstance(v, list) and all(isinstance(r, dict) for r in v) for v in lists.values())
              and isinstance(cells, list) and all(isinstance(c, dict) for c in cells))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        return [Fact(name, "unrecognised `grok inspect --json` format (a key is missing or has another shape)",
                     level="warn",
                     remedy="the CLI changed its output (H8): the compat check cannot judge it. Re-record the "
                            "fixtures and update this check before trusting the engine on this build")]

    home_dir = os.path.expanduser("~")
    foreign_roots = [f"{home_dir}/{d}/" for d in (".claude", ".claude-accounts", ".cursor")]

    def foreign(rec: dict) -> bool:
        """Claude/Cursor origin: tagged so, sourced from their config, or loaded from their tree (a
        Claude PLUGIN carries no vendor tag but lives under ~/.claude*, and the D3 switches skip it)."""
        texts = [str((rec.get("source") or {}).get("path") or ""), str(rec.get("target") or "")]
        return (rec.get("vendor") in _GROK_GUARDED_VENDORS
                or (rec.get("source") or {}).get("type") in ("claude", "claudeJson")
                or any(root in t for root in foreign_roots for t in texts))

    active_mcp = [m.get("name") or "?" for m in lists["mcpServers"] if not m.get("disabled")]
    leaked = [f"{kind[:-1]} {r.get('name') or r.get('event') or '?'}"
              for kind in ("hooks", "skills", "agents") for r in lists[kind]
              if not r.get("disabled") and foreign(r)]
    cells_on = sorted(f"{c.get('vendor')}/{c.get('surface')}" for c in cells
                      if c.get("vendor") in _GROK_GUARDED_VENDORS and c.get("enabled")
                      and c.get("surface") in ("skills", "rules", "hooks", "mcps", "mcp", "agents"))

    def names(items: "list[str]") -> str:
        return ", ".join(items[:5]) + (f" (+{len(items) - 5} more)" if len(items) > 5 else "")

    if active_mcp:
        return [Fact(name, f"{len(active_mcp)} active MCP server(s): {names(active_mcp)}", level="fail",
                     remedy="H1: MCP servers (mail, drive, devices...) reach Grok turns that run with full tool "
                            "access, and the D3 switches (grok_engine.D3_ENV) are not stopping them — a CLI "
                            "update that ignores GROK_CLAUDE_MCPS_ENABLED, or servers declared in the Grok "
                            "home's own config.toml. Set GROK_ENABLED=false until fixed")]
    if leaked or cells_on:
        parts = []
        if leaked:
            parts.append(f"Claude/Cursor config still active: {names(leaked)}")
        if cells_on:
            parts.append(f"compat switches on: {names(cells_on)}")
        return [Fact(name, "; ".join(parts), level="warn",
                     remedy="the D3 env block turns the Claude/Cursor scans off but not Claude PLUGINS (hooks and "
                            "skills loaded from ~/.claude*/plugins), and a newer CLI may rename a switch: check "
                            "`grok inspect --json` under the engine's env and the plugin list")]
    return [Fact(name, "isolated: 0 active MCP servers, 0 Claude/Cursor hooks, skills or agents (global config, "
                       "neutral cwd — a project's own .mcp.json is not covered)")]


def _grok_folder_trust(g: _Grok) -> "list[Fact]":
    """Folder trust is the switch that keeps a project's own `.mcp.json` / `.grok/config.toml` MCP
    servers, `.grok/hooks` and `.grok/skills` from starting in a Grok turn (measured live, spec-095
    P1b: untrusted starts none, `GROK_FOLDER_TRUST=0` starts all). Two things can lift it: the env
    switch (the engine pins it on) and an entry in the cockpit home's trust store."""
    name = "Grok folder trust"
    pin = g.env.get("GROK_FOLDER_TRUST")
    if pin != "1":
        return [Fact(name, f"GROK_FOLDER_TRUST is not pinned on in the engine's child env (it is {pin!r})",
                     level="fail",
                     remedy="grok_engine.D3_ENV must carry GROK_FOLDER_TRUST=1: without it a CLI default that "
                            "changes would start every repo's own MCP servers and hooks with full tool access")]
    problem = g.ge._trust_store_problem(g.home)
    if problem:
        return [Fact(name, problem, level="fail",
                     remedy="until the file is emptied the engine refuses every Grok turn (GrokIsolationError); "
                            "Cardloop's GROK_HOME must only ever be used by the engine and tools/grok-acct")]
    return [Fact(name, f"no folder is trusted in {g.home / 'trusted_folders.toml'}; GROK_FOLDER_TRUST=1 pinned in "
                       "the child env — a project's own MCP servers, hooks and skills stay off (a turn aborts "
                       "with GrokIsolationError if one ever starts)")]


def _grok_project_dirs(data: Path) -> "list[tuple[str, str]] | None":
    """[(project name, cwd)] of the projects that USE Grok: a record of data/topics.json whose board default is
    Grok or that names a `grok_model`, or a project with a Grok chat in data/chats.json. Existing absolute
    directories only, one entry per real path. None when the registry cannot be read — the caller stays
    silent then."""
    try:
        records = json.loads((data / "topics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(records, dict):
        return None
    chat_projects: "set[str]" = set()
    try:
        chats = json.loads((data / "chats.json").read_text(encoding="utf-8"))
        for pid, entry in (chats.items() if isinstance(chats, dict) else []):
            items = entry.get("chats") if isinstance(entry, dict) else None
            if any(isinstance(c, dict) and c.get("provider") == "grok" for c in (items or [])):
                chat_projects.add(str(pid))
    except (OSError, ValueError):
        pass
    found: "dict[str, str]" = {}
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        uses = (rec.get("board_provider") == "grok" or bool(rec.get("grok_model"))
                or str(rec.get("project")) in chat_projects)
        cwd = rec.get("cwd")
        if not uses or not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            continue
        found.setdefault(os.path.realpath(cwd), str(rec.get("project") or os.path.basename(cwd)))
    return sorted(((n, c) for c, n in found.items()), key=lambda t: (t[0].lower(), t[1]))


def _judge_project_inspect(res: "tuple[int, str, str] | None") -> "tuple[str, str]":
    """(level, text) for one project's `grok inspect --json` taken under the engine's sandbox profile.
    Strict about the format: a missing or mis-typed key is 'cannot tell', never '0 found'."""
    if res is None:
        return "warn", (f"could not inspect (no answer within {GROK_PROJECT_INSPECT_TIMEOUT_SEC:.0f}s, "
                        "or the CLI could not start under the sandbox)")
    code, out, err = res
    if code != 0:
        return "warn", f"could not inspect (exit {code}): {(err or out)[:100]}"
    try:
        doc = json.loads(out)
        trusted = doc["projectTrusted"]
        lists = {k: doc[k] for k in ("mcpServers", "hooks", "skills")}
        ok = (isinstance(trusted, bool)
              and all(isinstance(v, list) and all(isinstance(r, dict) for r in v) for v in lists.values()))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        return "warn", "unrecognised `grok inspect --json` format (projectTrusted / mcpServers / hooks / skills)"
    mcp = [str(m.get("name") or "?") for m in lists["mcpServers"] if not m.get("disabled")]
    hooks = [f"{h.get('event') or '?'} ({(h.get('source') or {}).get('type') or '?'})"
             for h in lists["hooks"] if not h.get("disabled")]
    skills = [f"{r.get('name') or '?'} ({(r.get('source') or {}).get('type') or '?'})"
              for r in lists["skills"]
              if not r.get("disabled") and (r.get("source") or {}).get("type") != "bundled"]

    def names(items: "list[str]") -> str:
        return ", ".join(items[:4]) + (f" (+{len(items) - 4} more)" if len(items) > 4 else "")

    if trusted:
        seen = [f"{label} {names(items)}" for label, items in
                (("MCP", mcp), ("hooks", hooks), ("skills", skills)) if items]
        if seen:
            return "fail", ("the folder is TRUSTED: its own MCP servers, hooks and skills start in a turn with "
                            f"full tool access — active now: {'; '.join(seen)}")
        # MEASURED on grok 1.0.46 (spec-095 P7b live run): a project that has NO config of its own reports
        # projectTrusted=true even with an empty trust store and GROK_FOLDER_TRUST=1 pinned — there is
        # nothing to trust, so nothing can start. It turns false the moment the project gains a
        # .mcp.json (the pin working), and a store entry is judged by its own fact ("Grok folder trust").
        return "ok", "isolated"
    problems = []
    if hooks:
        problems.append(f"hook(s) active under the sandbox view: {names(hooks)}")
    if skills:
        problems.append(f"skill(s) outside the bundled set active: {names(skills)}")
    if problems:
        return "warn", "; ".join(problems)
    if mcp:
        return "ok", (f"MCP config listed ({names(mcp)}) but gated by folder trust — not started in a turn")
    return "ok", "isolated"


def _grok_compat_projects(g: _Grok) -> "list[Fact]":
    """The global `Grok compat` fact runs in a neutral cwd, so it cannot see a project's own
    `.mcp.json`. For each project that uses Grok (board default, `grok_model` or a Grok chat) this runs the same bounded
    `inspect --json` IN the project, under the sandbox profile the engine would apply and with a
    copy of the cockpit home's folder-trust store — i.e. what a turn there would load. A project
    MCP server listed while the folder is untrusted is gated and does not count against it; a
    trusted folder, or an active hook or non-bundled skill, does. Silent when the registry cannot
    be read."""
    name = "Grok compat (projects)"
    ge = g.ge
    projects = _grok_project_dirs(g.data)
    if projects is None:
        return []
    if not projects:
        return [Fact(name, "no project uses Grok yet — nothing to check", level="info")]
    if not g.binary or not g.version:
        return [Fact(name, "not checked (no usable Grok CLI)", level="info")]
    try:
        deny, _skipped = ge.build_deny(g.home, g.ctx, bin_path=g.binary)
    except ge.GrokUnavailableError as exc:
        return [Fact(name, f"not checked: the sandbox profile cannot be built ({exc})", level="warn",
                     remedy="fix GROK_SANDBOX_DENY first (see the sandbox profile fact)")]
    home = g.scratch / "projects-home"
    home.mkdir(exist_ok=True)
    (home / "sandbox.toml").write_text(ge._sandbox_toml(deny), encoding="utf-8")
    real_cfg = g.home / "config.toml"
    (home / "config.toml").write_text(real_cfg.read_text(encoding="utf-8") if ge._config_ok(real_cfg)
                                      else ge._config_toml(), encoding="utf-8")
    with contextlib.suppress(OSError):
        shutil.copyfile(g.home / "trusted_folders.toml", home / "trusted_folders.toml")
    env = ge.child_env(home, sandbox=True)
    rows: "list[tuple[str, str, str]]" = []
    for pname, cwd in projects[:GROK_PROJECTS_MAX]:
        res = g.run([g.binary, "inspect", "--json"], timeout=GROK_PROJECT_INSPECT_TIMEOUT_SEC, env=env, cwd=cwd)
        rows.append((pname, *_judge_project_inspect(res)))
    skipped = len(projects) - len(rows)
    tail = f" (+{skipped} more project(s) not checked, limit {GROK_PROJECTS_MAX})" if skipped else ""
    bad = [r for r in rows if r[1] != "ok"]
    notes = [f"{n}: {t}" for n, lv, t in rows if lv == "ok" and t != "isolated"]
    if not bad:
        value = f"{len(rows)} project(s) that use Grok checked under the sandbox view: isolated"
        if notes:
            value += " — " + "; ".join(notes[:3]) + (f" (+{len(notes) - 3} more)" if len(notes) > 3 else "")
        return [Fact(name, value + tail)]
    level = "fail" if any(r[1] == "fail" for r in bad) else "warn"
    value = "; ".join(f"{n}: {t}" for n, _, t in bad[:4]) + (f" (+{len(bad) - 4} more)" if len(bad) > 4 else "")
    if len(rows) > len(bad):
        value += f" — {len(rows) - len(bad)} other project(s) isolated"
    remedy = ("a trusted folder lets a repo's own MCP servers and hooks start with full tool access: empty "
              f"{g.home / 'trusted_folders.toml'} (the engine refuses every turn until then)"
              if level == "fail" else
              "run `grok inspect --json` in the project under the engine's env to see where it comes from; "
              "Claude plugins and ~/.agents skills are hidden by the engine's profile and config.toml, so a "
              "survivor is a Grok-home hook, a project one the folder trust let through, or a changed CLI")
    return [Fact(name, value + tail, level=level, remedy=remedy)]


def _grok_processes(g: _Grok) -> "list[Fact]":
    facts: "list[Fact]" = []
    procs = _list_grok_agents(g.home, g.proc_root)
    unknown = [p for p in procs if p["age"] is None]
    old = [p for p in procs if p["age"] is not None and p["age"] >= GROK_AGENT_MAX_AGE_SEC]
    limit = _fmt_age(GROK_AGENT_MAX_AGE_SEC)
    if old:
        shown = ", ".join(f"pid {p['pid']} ({_fmt_age(p['age'])})" for p in old[:5])
        facts.append(Fact(
            "Grok processes", f"{len(old)} leftover `grok agent` process(es) older than {limit}: {shown}",
            level="fail",
            remedy="a per-turn process must never outlive its turn (H7: leaked CLIs once cost ~10 GB). Kill the "
                   f"group: kill -TERM -- -{old[0]['pgid']} (SIGKILL if it stays), then find the turn that "
                   "skipped its teardown: `journalctl -u cardloop | grep '\\[grok\\]'`"))
    elif unknown:
        facts.append(Fact("Grok processes", f"{len(unknown)} `grok agent` process(es), age unreadable "
                                            "(no /proc/uptime)", level="warn",
                          remedy=f"cannot tell whether any is older than {limit}; check `ps -o pid,etime,args -C grok`"))
    elif procs:
        oldest = max(p["age"] for p in procs)
        facts.append(Fact("Grok processes", f"{len(procs)} turn(s) in flight, oldest {_fmt_age(oldest)} "
                                            f"(limit {limit})"))
    else:
        facts.append(Fact("Grok processes", "no `grok agent` processes running"))

    try:
        litter = sum(1 for e in os.scandir(g.home) if e.name.startswith("sandbox-blocked"))
    except OSError:
        litter = 0
    if litter > GROK_LITTER_WARN:
        facts.append(Fact(
            "Grok litter", f"{litter} sandbox-blocked* placeholders in {g.home} (limit {GROK_LITTER_WARN})",
            level="warn",
            remedy="every sandboxed spawn leaves ~2 (mode 000) and the per-turn reaper removes the dead ones, "
                   "so a pile means turns die before cleanup or something spawns grok with this home outside "
                   "the engine. Clean now: venv/bin/python -c \"import grok_engine as g, pathlib; "
                   f"print(len(g.reap_litter(pathlib.Path({str(g.home)!r}))))\""))
    else:
        facts.append(Fact("Grok litter", f"{litter} sandbox-blocked* placeholders (limit {GROK_LITTER_WARN})"))
    return facts


def _grok_usage_files(g: _Grok) -> "list[Fact]":
    rows = []
    big = []
    for fname, limit in (("grok_usage.jsonl", GROK_USAGE_WARN_BYTES),
                         ("grok_limit_errors.jsonl", GROK_LIMIT_ERRORS_WARN_BYTES)):
        try:
            size = (g.data / fname).stat().st_size
        except OSError:
            continue
        rows.append(f"{fname} {_human_bytes(size)}")
        if size > limit:
            big.append(f"{fname} (> {_human_bytes(limit)})")
    if not rows:
        return []
    if big:
        return [Fact("Grok usage files", "; ".join(rows) + f" — too large: {', '.join(big)}", level="warn",
                     remedy="append-only ledgers with no rotation: archive the file (mv) while the cockpit is "
                            "idle, or look for a loop that appends on every event")]
    return [Fact("Grok usage files", "; ".join(rows))]


# ─────────────────────────── orchestration ───────────────────────────────────────

CORE_SECTIONS = ("Versions", "Auth", "Config", "Service", "Runtime", "Data", "Load")
# Optional sections are shown only when they hold facts, so a feature that is switched off
# leaves the report (text and JSON) exactly as it was before the feature existed.
SECTIONS = CORE_SECTIONS + ("Grok",)


def _visible(sections: dict) -> "list[str]":
    return [n for n in SECTIONS if n in CORE_SECTIONS or sections.get(n)]


def _safe_section(name: str, fn, *args, **kwargs) -> "list[Fact]":
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — one crashing probe must not kill the report
        return [Fact(name, f"probe crashed: {e}", level="warn",
                      remedy="run with a traceback to debug: python -c "
                             "\"import tools.doctor\" (or file an issue with this output)")]


def probe_load(repo_root: Path) -> "list[Fact]":
    """Host load as the cockpit's top-bar meter sees it (spec-094), measured once from here.

    This process is not the cockpit, so the signals that need its registry (agent headcount,
    guard evictions, loop lag) are absent; everything about the HOST — memory working set, PSI,
    swap activity, temp dir, disk, cpu — is the same as what the meter reports."""
    sys.path.insert(0, str(repo_root))
    import load_monitor
    mon = load_monitor.Monitor()
    mon.sample({"data_dir": repo_root / "data"})
    time.sleep(1.0)                      # rates (swap-in) need two samples
    snap = mon.sample({"data_dir": repo_root / "data"})
    facts: "list[Fact]" = []
    if not snap["signals"]:
        return [Fact("Load", "nothing measurable on this host", level="info")]
    level = {"ok": "ok", "warn": "warn", "crit": "fail"}.get(snap["level"], "info")
    facts.append(Fact("Load", {"ok": "normal", "warn": "elevated", "crit": "overloaded"}.get(snap["level"], snap["level"]),
                      level=level))
    for sig in snap["signals"]:
        if sig["level"] != "ok":
            facts.append(Fact(sig["id"], sig["text"], level="warn" if sig["level"] == "warn" else "fail",
                              remedy=sig["hint"] or None))
    return facts


def collect(repo_root: Path = REPO_ROOT) -> "tuple[dict, list[str]]":
    """Run every probe. Returns (sections, secrets_to_scrub)."""
    env, env_path, env_exists = _load_dotenv_merged(repo_root)
    service_name = env.get("CARDLOOP_SERVICE") or "cardloop"
    port = env.get("WEB_PORT") or "8787"
    grok_secrets: "list[str]" = []        # filled by probe_grok (the account email) for render-time scrub

    sections = {
        "Versions": _safe_section("Versions", probe_versions, repo_root),
        "Auth": _safe_section("Auth", probe_auth, env),
        "Config": _safe_section("Config", probe_config, env, env_path, env_exists,
                                 _get_totp_status, repo_root),
        "Service": _safe_section("Service", probe_service, service_name),
        "Runtime": _safe_section("Runtime", probe_runtime, port, repo_root),
        "Data": _safe_section("Data", probe_data, repo_root),
        "Load": _safe_section("Load", probe_load, repo_root),
        "Grok": _safe_section("Grok", probe_grok, env, repo_root, secrets_out=grok_secrets),
    }

    secrets = [env.get("ANTHROPIC_API_KEY", ""), env.get("WEB_PASSWORD", ""),
               env.get("WEB_COOKIE_SALT", "")] + grok_secrets
    return sections, [s for s in secrets if s]


def _verdict(sections: dict) -> "list[tuple[str, Fact]]":
    return [(sect, f) for sect, facts in sections.items() for f in facts if f.level in ("warn", "fail")]


def _icon(level: str) -> str:
    return {"ok": "✓", "warn": "⚠", "fail": "✗", "info": "·"}.get(level, "?")


def render_text(sections: dict, secrets: "list[str]", elapsed: float) -> str:
    lines: "list[str]" = []
    for name in _visible(sections):
        lines.append(f"== {name} ==")
        for f in sections.get(name, []):
            value = _scrub(f.value, secrets)
            lines.append(f"  {_icon(f.level)} {f.label}: {value}")
        lines.append("")

    verdict = _verdict(sections)
    lines.append("== Verdict ==")
    if not verdict:
        lines.append("  no problems found")
    else:
        for sect, f in verdict:
            value = _scrub(f.value, secrets)
            remedy = _scrub(f.remedy, secrets)
            lines.append(f"  {_icon(f.level)} [{sect}] {f.label}: {value}")
            if remedy:
                lines.append(f"      -> {remedy}")
    lines.append("")
    lines.append(f"({elapsed:.2f}s)")
    return "\n".join(lines)


def render_json(sections: dict, secrets: "list[str]", elapsed: float, exit_code: int) -> str:
    def fact_dict(f: Fact) -> dict:
        return {
            "label": f.label,
            "value": _scrub(f.value, secrets),
            "level": f.level,
            "remedy": _scrub(f.remedy, secrets),
        }

    verdict = _verdict(sections)
    payload = {
        "sections": {name: [fact_dict(f) for f in sections.get(name, [])] for name in _visible(sections)},
        "verdict": {
            "ok": not verdict,
            "exit_code": exit_code,
            "findings": [{"section": sect, **fact_dict(f)} for sect, f in verdict],
        },
        "elapsed_sec": round(elapsed, 3),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-command diagnosis for a Cardloop cockpit. Read-only, "
                     "redacted output, exits non-zero when any ✗ finding is present.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    sections, secrets = collect(REPO_ROOT)
    elapsed = time.monotonic() - t0

    exit_code = 1 if any(f.level == "fail" for _facts in sections.values() for f in _facts) else 0

    if args.json:
        print(render_json(sections, secrets, elapsed, exit_code))
    else:
        print(render_text(sections, secrets, elapsed))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

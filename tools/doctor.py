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
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import socket
import subprocess
import sys
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

def probe_service(service_name: str, run=_run) -> "list[Fact]":
    facts: "list[Fact]" = []

    show = run(["systemctl", "show", service_name,
                "-p", "ActiveState", "-p", "SubState", "-p", "MemoryHigh",
                "-p", "MemoryMax", "-p", "MemoryCurrent", "-p", "MainPID"])
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

    mc = _parse_mem_value(props.get("MemoryCurrent"))
    if mc is not None:
        pct = f" ({mc * 100.0 / mm:.0f}% of MemoryMax)" if mm else ""
        facts.append(Fact("MemoryCurrent", f"{mc // (1024 * 1024)} MB{pct}"))

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

def probe_data(repo_root: Path = REPO_ROOT) -> "list[Fact]":
    facts: "list[Fact]" = []
    data_dir = repo_root / "data"

    if not data_dir.exists():
        facts.append(Fact("data/", "missing (created automatically on first run of bot.py)", level="info"))
        return facts

    facts.append(Fact("data/ size", _dir_size(data_dir)))

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


# ─────────────────────────── orchestration ───────────────────────────────────────

SECTIONS = ("Versions", "Auth", "Config", "Service", "Runtime", "Data")


def _safe_section(name: str, fn, *args, **kwargs) -> "list[Fact]":
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — one crashing probe must not kill the report
        return [Fact(name, f"probe crashed: {e}", level="warn",
                      remedy="run with a traceback to debug: python -c "
                             "\"import tools.doctor\" (or file an issue with this output)")]


def collect(repo_root: Path = REPO_ROOT) -> "tuple[dict, list[str]]":
    """Run every probe. Returns (sections, secrets_to_scrub)."""
    env, env_path, env_exists = _load_dotenv_merged(repo_root)
    service_name = env.get("CARDLOOP_SERVICE") or "cardloop"
    port = env.get("WEB_PORT") or "8787"

    sections = {
        "Versions": _safe_section("Versions", probe_versions, repo_root),
        "Auth": _safe_section("Auth", probe_auth, env),
        "Config": _safe_section("Config", probe_config, env, env_path, env_exists,
                                 _get_totp_status, repo_root),
        "Service": _safe_section("Service", probe_service, service_name),
        "Runtime": _safe_section("Runtime", probe_runtime, port, repo_root),
        "Data": _safe_section("Data", probe_data, repo_root),
    }

    secrets = [env.get("ANTHROPIC_API_KEY", ""), env.get("WEB_PASSWORD", ""),
               env.get("WEB_COOKIE_SALT", "")]
    return sections, [s for s in secrets if s]


def _verdict(sections: dict) -> "list[tuple[str, Fact]]":
    return [(sect, f) for sect, facts in sections.items() for f in facts if f.level in ("warn", "fail")]


def _icon(level: str) -> str:
    return {"ok": "✓", "warn": "⚠", "fail": "✗", "info": "·"}.get(level, "?")


def render_text(sections: dict, secrets: "list[str]", elapsed: float) -> str:
    lines: "list[str]" = []
    for name in SECTIONS:
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
        "sections": {name: [fact_dict(f) for f in sections.get(name, [])] for name in SECTIONS},
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

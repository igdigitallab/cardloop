"""Isolated Grok Build runtime adapter for Cardloop (spec-095, the third provider).

Same contract as ``codex_engine``: ``engine.py`` never imports this module and the web layer
selects it only for provider-pinned chats/cards. The public surface is deliberately the same
shape as Codex's: ``grok_enabled``, ``provider_info``, ``capabilities``, ``run_grok_engine``,
``GrokTurn`` (the ``ctx["running"]`` handle with ``async interrupt()``).

How a turn runs (spec-095 §5.1 + the §10 corrections):

* ONE ``grok agent --no-leader stdio`` subprocess per turn, spoken to over ACP (JSON-RPC lines).
  It starts in its own session so ``killpg`` takes the whole tree; ``finally`` always kills the
  group, also when our own task is cancelled, so no process outlives a turn or a deploy.
* The child environment is an ALLOWLIST plus the D3 block that switches off Grok's Claude/Cursor
  compat scan (our MCP servers, hooks and skills must never reach a Grok turn) and the
  interactive tools that would hang a headless turn. Nothing else from the cockpit's own
  environment (WEB_PASSWORD, VAPID keys, ANTHROPIC_/XAI_ keys...) is ever handed down.
* Every turn runs under a CUSTOM sandbox profile (``GROK_SANDBOX=cardloop``, generated in the
  cockpit's own ``GROK_HOME``). Only a custom profile fails closed; a built-in one that cannot be
  applied silently runs unsandboxed. ``--sandbox off`` does not exist in this module.
* Subscription auth only: the account must be an OIDC login with the data-retention opt-out set,
  re-verified against what the agent itself reports on ``authenticate``. An API key is never
  passed and never accepted.

Everything the engine decides is journaled with a ``[grok]`` prefix: key NAMES, never values.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import time
import tomllib
import uuid
from pathlib import Path
from typing import AsyncGenerator, Callable, NamedTuple

import fsutil
import grok_jsonl
import runtime_secrets as _rs

PROVIDER = "grok"
DEFAULT_GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.7")
# The levels Grok's `reasoning_effort` option offers. The cockpit passes its own "ultra"/"max"
# raw to adapter engines — anything outside this set is dropped, never forwarded.
GROK_REASONING_LEVELS = ("low", "medium", "high", "xhigh")
# CLI builds this module has been exercised against. An unknown (newer) build only WARNS: the
# stream format can drift, but refusing to start on every release would be its own outage.
KNOWN_GOOD_VERSIONS = ("1.0.46",)

SANDBOX_PROFILE = "cardloop"
_REGISTRY_TTL_SEC = 300.0
# A NEGATIVE row is re-probed fast: the operator fixes "not signed in" (tools/grok-acct login runs
# in another process and cannot reset this cache) and must not stare at "off" for five minutes.
_REGISTRY_FAIL_TTL_SEC = 15.0
_registry_cache: dict = {"ts": 0.0, "data": None}
_inflight: "asyncio.Future | None" = None

# --- timing / size knobs (module constants so tests can shrink them) ---------------------------
HANDSHAKE_TIMEOUT_SEC = 20.0   # EACH handshake step; a missing login makes `authenticate` hang
CLOSE_WAIT_SEC = 1.0           # best-effort session/close in the teardown
TERM_WAIT_SEC = 2.0            # SIGTERM -> SIGKILL grace
INTERRUPT_WAIT_SEC = 5.0       # session/cancel -> stopReason:"cancelled" before killpg
PROBE_TIMEOUT_SEC = 20.0       # `grok --version` / `grok models`
SANDBOX_PROBE_TURN_SEC = 120.0
SANDBOX_PROBE_OK_TTL_SEC = 7 * 86400.0
SANDBOX_PROBE_FAIL_TTL_SEC = 15 * 60.0
STREAM_LIMIT = 16 * 1024 * 1024  # asyncio's 64 KiB default dies on one big tool-output line
STDERR_RING_BYTES = 4096
RULES_MAX_BYTES = 24 * 1024
AUTH_MAX_BYTES = 256 * 1024    # a real auth.json is < 4 KiB; the model's shell can write the file, so it is capped
LIMIT_ERROR_MAX_CHARS = 8000

_AUTH_HINT = "Grok sign-in expired — run `tools/grok-acct login`"

_TRUTHY = {"1", "true", "yes", "on"}

# D3: switches that must be on for EVERY child. The first two blocks keep Grok from importing our
# Claude config (MCP servers, hooks, skills, rules, agents) and the Cursor twin; the rest stop
# telemetry/memory and the interactive or background tools that can hang a headless turn.
D3_ENV: dict[str, str] = {
    "GROK_CLAUDE_AGENTS_ENABLED": "0", "GROK_CLAUDE_HOOKS_ENABLED": "0",
    "GROK_CLAUDE_MCPS_ENABLED": "0", "GROK_CLAUDE_RULES_ENABLED": "0",
    "GROK_CLAUDE_SKILLS_ENABLED": "0",
    "GROK_CURSOR_AGENTS_ENABLED": "0", "GROK_CURSOR_HOOKS_ENABLED": "0",
    "GROK_CURSOR_MCPS_ENABLED": "0", "GROK_CURSOR_RULES_ENABLED": "0",
    "GROK_CURSOR_SKILLS_ENABLED": "0",
    # Foreign-session import (the bundled resume-claude/-codex/-cursor skills read it). Measured:
    # the cells flip to `env off` in `grok inspect`; nothing consumes them today, so this is the
    # defence in depth against a build that starts to.
    "GROK_CLAUDE_SESSIONS_ENABLED": "0", "GROK_CURSOR_SESSIONS_ENABLED": "0",
    "GROK_CODEX_SESSIONS_ENABLED": "0",
    "GROK_TELEMETRY_ENABLED": "0", "GROK_TELEMETRY_TRACE_UPLOAD": "0", "GROK_MEMORY": "0",
    "GROK_ASK_USER_QUESTION": "0", "GROK_AUTO_WAKE": "0", "GROK_WORKFLOWS": "0",
    # Folder trust is what keeps a PROJECT's own `.mcp.json` / `.grok/config.toml` MCP servers,
    # `.grok/hooks`, `.grok/skills` from starting in a Grok turn. Measured live with marker files
    # (spec-095 P1b): untrusted (the headless default) starts none of them, `GROK_FOLDER_TRUST=0`
    # starts all of them, `=1` still starts none. Pinned on so a changed default cannot flip it.
    "GROK_FOLDER_TRUST": "1",
    # `agent stdio` rejects --no-auto-update (it exists on the top-level command only); the
    # documented env switch is the way to stop a turn from swapping the binary underneath us.
    "GROK_DISABLE_AUTOUPDATER": "1",
}

# The ONLY parent variables a child inherits (plus LC_*). Not the cockpit service env: that one
# carries WEB_PASSWORD, push keys and tokens that a model-run `printenv` would put into context.
_ENV_ALLOW = (
    "PATH", "HOME", "LANG", "LANGUAGE", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
_ENV_ALLOW_PREFIXES = ("LC_",)

# D4 defaults. `~` = $HOME. Literal entries that do not exist are DROPPED at spawn (C7: Grok
# materialises a missing `deny` path as an empty read-only file on the host). Never add the
# `~/.grok` directory itself: the binary lives under it and Grok re-execs through bwrap, so
# denying it makes every start fail with EACCES.
DEFAULT_DENY = (
    "~/.claude", "~/.claude-accounts", "~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh",
    # Measured on this adapter (r-grok-live, 2026-10-02): with only the entries above the model's
    # shell READ ~/.claude.json (MCP server definitions + tokens live in $HOME, not in ~/.claude),
    # ~/.git-credentials and ~/.bash_history. The rest is the usual credential/history set.
    "~/.claude.json", "~/.git-credentials", "~/.netrc", "~/.npmrc", "~/.pypirc", "~/.docker",
    "~/.kube", "~/.config/gcloud", "~/.bash_history", "~/.zsh_history",
    # Measured by r-grok-live, deferred in the spec, added in P1b: other vendors' credential homes.
    "~/.azure", "~/.oci", "~/.codex", "~/.cursor",
    "**/.env", "**/secrets.env", "**/*.pem", "**/*.key",
)
# The Claude/Cursor IMPORT surface. Always denied, whatever GROK_SANDBOX_DENY says: measured live
# (P1b), a custom list without `~/.claude` made a Claude plugin's SessionStart hook FIRE inside a
# Grok turn even in an untrusted folder (plugins enabled from ~/.claude/settings.json resolve through
# ~/.claude/plugins), while with `~/.claude*` denied the registry is unreadable and nothing loads.
# Folder trust does not gate user-scope plugins; this kernel deny is what does.
FLOOR_DENY = ("~/.claude", "~/.claude-accounts", "~/.claude.json", "~/.cursor")

_REPO = Path(__file__).resolve().parent

_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|VAPID|AUTH)", re.I)
_QUOTA_RE = re.compile(
    r"rate.?limit|quota|usage.?limit|too many requests|\b429\b|exceeded|exhausted|credits?|"
    r"limit (?:reached|hit)|try again (?:later|in)|capacity", re.I)


class GrokUnavailableError(RuntimeError):
    """Raised when a Grok run is requested but the provider cannot serve it."""


class GrokAuthError(GrokUnavailableError):
    """The Grok login is missing, expired or not the subscription kind we accept."""


class GrokIsolationError(GrokUnavailableError):
    """The agent reports a tool surface this engine never grants (an MCP server, a hook): the turn
    is refused/aborted. Not an outage of the provider, so it never flips the registry row."""


class GrokProtocolError(RuntimeError):
    """The agent spoke outside the protocol (oversized line, unexpected exit...)."""


class _Stopped(Exception):
    """The operator stopped the turn before it produced a prompt response (clean end)."""


# ------------------------------------------------------------------------------------------
# logging (journal) — every decision, key NAMES only
# ------------------------------------------------------------------------------------------

def _secret_values() -> list[str]:
    out = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and _SECRET_NAME_RE.search(k):
            out.append(v)
    # spec-096 P3b: the cockpit's own secrets are scrubbed out of os.environ at start; redact
    # their values from the snapshot or the journal would stop masking exactly the ones that matter.
    out.extend(_rs.secret_values(min_len=8))
    return sorted(set(out), key=len, reverse=True)


def _redact(text: str) -> str:
    """Mask the value of any secret-named variable of the cockpit's own environment."""
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "***")
    return text


def _log(msg: str) -> None:
    print(f"[grok] {_redact(str(msg))}", flush=True)


# ------------------------------------------------------------------------------------------
# configuration (env knobs, D11)
# ------------------------------------------------------------------------------------------

def _truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def grok_enabled() -> bool:
    return _truthy(os.getenv("GROK_ENABLED", "false"))


def data_dir(ctx: dict | None = None) -> Path:
    """The cockpit data dir: ctx["DATA"], else the convention modules.py/accounts.py use."""
    d = (ctx or {}).get("DATA")
    if d:
        return Path(d)
    env = os.environ.get("_CARDLOOP_DATA_DIR")
    return Path(env) if env else _REPO / "data"


def grok_home(ctx: dict | None = None) -> Path:
    """Cardloop's OWN Grok home (config, login, sessions) — not the operator's ~/.grok."""
    raw = os.environ.get("GROK_HOME", "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    data = data_dir(ctx)
    # NEXT TO the data dir, never inside it: the data dir is hidden from the model as one directory and
    # a deny entry that contains GROK_HOME makes the CLI exit (see _data_deny_entry)
    return data.parent / f"{data.name}-grok-home"


def grok_bin() -> str | None:
    """Resolve the Grok binary: GROK_BIN (explicit, no fallback) else PATH else ~/.grok/bin."""
    raw = os.environ.get("GROK_BIN", "").strip()
    candidates = [raw] if raw else ["grok", str(Path.home() / ".grok" / "bin" / "grok")]
    for cand in candidates:
        found = shutil.which(os.path.expanduser(cand))
        if found:
            return found
    return None


def _canary_dir(ctx: dict | None = None) -> Path:
    return data_dir(ctx) / "grok-canary"


# ------------------------------------------------------------------------------------------
# child environment (D3)
# ------------------------------------------------------------------------------------------

def child_env(home: Path, *, sandbox: bool = True, parent: "dict | None" = None) -> dict[str, str]:
    """The complete environment of a Grok child: allowlisted parent vars + the D3 block."""
    src = os.environ if parent is None else parent
    env: dict[str, str] = {}
    for key in _ENV_ALLOW:
        if key in src and src[key] != "":
            env[key] = src[key]
    for key, value in src.items():
        if key.startswith(_ENV_ALLOW_PREFIXES) and value != "":
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", os.path.expanduser("~"))
    env.setdefault("TERM", "dumb")
    env.update(D3_ENV)
    env["GROK_HOME"] = str(home)
    if sandbox:
        env["GROK_SANDBOX"] = SANDBOX_PROFILE
    return env


# ------------------------------------------------------------------------------------------
# GROK_HOME: config.toml, sandbox.toml, litter reaper
# ------------------------------------------------------------------------------------------

def _toml_str(value: str) -> str:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise GrokUnavailableError("control character in a sandbox path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_GLOB_CHARS = set("*?[")


def _is_glob(entry: str) -> bool:
    return any(c in entry for c in _GLOB_CHARS)


def _glob_problem(entry: str) -> str | None:
    """Grok refuses to START on an unsupported glob, taking every turn with it: catch it here."""
    if any(c in entry for c in "{}\\"):
        return "brace alternation / backslash escapes are not supported"
    if "[]" in entry or "[[:" in entry:
        return "unsupported character-class form"
    segments = entry.split("/")
    body = segments[1:] if entry.startswith("/") else segments
    for seg in body:
        if seg == "" :
            return "empty path segment (doubled or trailing slash)"
        if seg in (".", ".."):
            return "'.' and '..' segments are not supported"
    return None


def _expand_deny_entry(raw: str) -> str:
    entry = raw.strip()
    home = str(Path.home())
    if entry == "~" or entry.startswith("~/"):
        return home + entry[1:]
    if entry.startswith("$HOME/"):
        return home + entry[5:]
    if entry.startswith("/") or entry.startswith("*"):
        return entry
    if _is_glob(entry):
        return entry
    return os.path.join(home, entry)  # bare relative literal = $HOME-relative


def _is_under(path: str, parent: str) -> bool:
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _reached_through_workspace_symlink(path: str, cwd: str) -> bool:
    """`path` is spelled inside `cwd` and a symlink lies on the way to it. The model's shell can rewrite names
    in its workspace, so such a symlink can be re-pointed at something the cockpit would then follow. A real
    directory inside the workspace is not this case."""
    lex, root = os.path.abspath(path), os.path.abspath(cwd)
    if lex != root and not lex.startswith(root.rstrip("/") + "/"):
        return False
    current = root
    for part in Path(lex).relative_to(root).parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def _data_deny_entry(home: Path, ctx: dict | None, bin_path: str | None) -> str | None:
    """The cockpit's OWN data dir (sessions, topics, secrets, the send ledger, usage ledgers, push keys,
    the search index) as ONE deny entry — ALWAYS applied, whatever GROK_SANDBOX_DENY says.

    One directory, not its children. Grok hides a denied path with a mount over it, set up when the
    sandbox starts. A mount over a FILE does not survive the cockpit's atomic rewrite (write a temp
    file, rename it over the original): the kernel detaches the mount and the model's shell reads the
    new contents for the rest of the turn (reproduced with bwrap, 2026-10-03: chats.json and
    crash-recovery-state.json are rewritten that way while any chat runs). A mount over the data
    DIRECTORY is never renamed, so what the cockpit writes inside stays hidden, and nothing new can be
    created there either (the per-child layout let a model plant a symlink at a temp name the cockpit
    then followed).

    The price: Grok must keep reaching its own home and CLI, and a deny entry that CONTAINS GROK_HOME
    makes `grok agent` exit 1 before the handshake (measured 2026-10-02). So GROK_HOME (default
    `<data dir>-grok-home`, next to the data dir) and the binary must live OUTSIDE the data dir; a layout
    that puts them inside is refused with the reason, never half hidden.

    None when there is nothing to hide: no data dir yet, or it is `$HOME` or above it (the home
    backstop owns that layout).
    """
    real_data = os.path.realpath(data_dir(ctx))
    if not os.path.isdir(real_data):
        return None
    real_home = os.path.realpath(Path.home())
    if real_home == real_data or real_home.startswith(real_data.rstrip("/") + "/"):
        _log(f"data dir {real_data} is $HOME or lies above it: not hidden from the model "
             f"(the home backstop owns that layout)")
        return None
    for what, path in (("GROK_HOME", home), ("the Grok CLI", bin_path)):
        if path and _is_under(str(path), real_data):
            raise GrokUnavailableError(
                f"{what} ({path}) is inside the cockpit data dir {real_data}, which has to be hidden from "
                f"the model's shell as one directory (hiding it file by file does not survive the cockpit "
                f"rewriting its files). Move {what} outside it"
                + (" (the default GROK_HOME is next to the data dir)" if what == "GROK_HOME" else ""))
    return real_data


def _cockpit_secret_paths() -> list[str]:
    """Secret-bearing paths of the cockpit that the `**/.env`-style globs do not reach (a relative glob is
    anchored at the project, so from any OTHER project it matches nothing):
    * the checkout's `.env` file, every `.env.*` sibling (`.env.bak-<date>`, `.env.local`; not
      `.env.example`) and `data.bak*` snapshots of the data dir (private transcripts and tokens —
      .gitignore keeps them out of git for the same reason);
    * the secret safe's Fernet key and store: the default `~/.config/claude-ops/` directory and wherever
      CLAUDE_OPS_SECRET_KEYFILE / CLAUDE_OPS_SECRET_STORE point (a key beside a readable store opens every
      credential in the safe).
    Only what exists survives: build_deny drops a missing literal."""
    try:
        names = sorted(e.name for e in os.scandir(_REPO))
    except OSError:
        names = []
    out = [str(_REPO / ".env")]
    out += [str(_REPO / n) for n in names
            if (n.startswith(".env.") and n != ".env.example") or n.startswith("data.bak")]
    out.append("~/.config/claude-ops")
    for var in ("CLAUDE_OPS_SECRET_KEYFILE", "CLAUDE_OPS_SECRET_STORE"):
        raw = os.environ.get(var, "").strip()
        if raw:
            out.append(os.path.expanduser(raw))
    return out


def build_deny(home: Path, ctx: dict | None = None, *, bin_path: str | None = None
               ) -> tuple[list[str], list[str]]:
    """(deny entries to hand Grok, literal entries skipped because they do not exist).

    GROK_SANDBOX_DENY (comma list) REPLACES the defaults when set; the canary dir that the
    availability probe reads, FLOOR_DENY (the Claude/Cursor import surface), the cockpit's own
    `.env` (+ its `.env.*` backups and `data.bak*` snapshots), the secret safe's key/store and its data dir
    (`_data_deny_entry`) are always present, and so is the operator's own `~/.grok/auth.json`.
    Raises GrokUnavailableError for an entry that would break every start (invalid glob, or a
    literal that hides the binary / GROK_HOME).
    """
    raw_env = os.environ.get("GROK_SANDBOX_DENY", "").strip()
    raw_entries = [e for e in raw_env.split(",") if e.strip()] if raw_env else list(DEFAULT_DENY)
    # The operator's own interactive login (~/.grok/auth.json): a different credential store than ours,
    # so part of the floor — a custom GROK_SANDBOX_DENY used to drop it silently. Skipped when Cardloop's
    # home IS ~/.grok: the agent needs that file.
    own = Path.home() / ".grok"
    if os.path.realpath(home) != os.path.realpath(own):
        raw_entries.append(str(own / "auth.json"))
    raw_entries.extend(FLOOR_DENY)
    raw_entries.append(str(_canary_dir(ctx)))
    # The cockpit's own secrets file and its backups. The default `**/.env` glob matches only INSIDE the
    # workspace (docs: a relative glob is anchored at the project) and only that exact name, so from any
    # other project `.env`, `.env.bak-<date>` and a `data.bak-<date>/` snapshot are all readable.
    raw_entries.extend(_cockpit_secret_paths())
    protected = [str(home)]
    if bin_path:
        protected.append(os.path.realpath(bin_path))
    protected.extend([str(Path.home()), "/"])
    deny: list[str] = []
    skipped: list[str] = []
    for raw in raw_entries:
        entry = _expand_deny_entry(raw)
        if _is_glob(entry):
            problem = _glob_problem(entry)
            if problem:
                raise GrokUnavailableError(f"GROK_SANDBOX_DENY entry {raw.strip()!r}: {problem}")
        else:
            real = os.path.realpath(entry)
            for p in protected:
                if real == os.path.realpath(p) or os.path.realpath(p).startswith(real.rstrip("/") + "/"):
                    raise GrokUnavailableError(
                        f"GROK_SANDBOX_DENY entry {raw.strip()!r} would hide the Grok binary, "
                        f"its home or $HOME itself — Grok could not start")
            if os.path.islink(entry):
                # Measured (2026-10-02): a symlink as a deny entry — to a file, a directory, /dev/null
                # or nowhere — makes `grok agent` exit 1 before the handshake, i.e. every turn fails.
                # What the link points at is what has to be hidden; a dangling one hides nothing.
                if not os.path.lexists(real):
                    skipped.append(entry)
                    continue
                entry = real
            if not os.path.lexists(entry):
                skipped.append(entry)
                continue
        if entry not in deny:
            deny.append(entry)
    data_entry = _data_deny_entry(home, ctx, bin_path)
    if data_entry and data_entry not in deny:     # _prune_nested keeps two IDENTICAL strings: dedupe here
        deny.append(data_entry)          # entries inside it (the canary dir, a custom one) are pruned below
    return _prune_nested(deny), skipped


def _prune_nested(deny: list[str]) -> list[str]:
    """Drop a literal entry that lies INSIDE another literal entry.

    bwrap binds each deny path over the host path; once a parent is bound the child's mount point
    cannot be created inside it, and the sandbox refuses to start ("Can't create file ...:
    Read-only file system") — measured with `~/.config` + `~/.config/gcloud` on this host. The
    parent already hides the child, so dropping it loses nothing. Globs are left alone."""
    real = {e: os.path.realpath(e) for e in deny if not _is_glob(e)}
    kept = []
    for e in deny:
        if e in real:
            r = real[e]
            inside = any(o != e and (r == real[o] and deny.index(o) < deny.index(e)
                                     or r.startswith(real[o].rstrip("/") + "/"))
                         for o in real)
            if inside:
                _log(f"deny entry {e} is inside another deny entry (or a duplicate): dropped")
                continue
        kept.append(e)
    return kept


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> bool:
    """Write `text` to `path` unless it already holds exactly that. True if rewritten."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    fsutil.atomic_write(path, text, mode)
    return True


def _sandbox_toml(deny: list[str]) -> str:
    lines = [
        "# Generated by Cardloop (grok_engine.ensure_home) on every turn — edits are overwritten.",
        f"[profiles.{SANDBOX_PROFILE}]",
        'extends = "workspace"',
        "deny = [",
    ]
    lines.extend(f"  {_toml_str(e)}," for e in deny)
    lines.append("]")
    return "\n".join(lines) + "\n"


# Skills the model must not see. `~/.agents/skills/*` (15 on this host) loads under the hermetic env
# whatever the D3 switches say (measured: it is not a Claude/Cursor source), and the bundled
# `resume-*` skills are the foreign-session importers. `[skills] ignore` hides a path prefix and
# `disabled` keeps a name listed but inactive; both verified live (P1b): after them the session's
# advertised slash commands carry neither.
_SKILLS_DISABLED = ("resume-claude", "resume-codex", "resume-cursor")
# Top-level tables that would widen what a Grok turn loads (more MCP servers, hooks, plugins,
# compat sources, folder trust off). The file is ours: one carrying any of them is regenerated.
_CONFIG_FORBIDDEN = ("folder_trust", "mcp_servers", "disabled_mcp_servers", "compat", "hooks",
                     "plugins", "marketplace")


def _agents_dir() -> str:
    return str(Path.home() / ".agents")


def _config_toml() -> str:
    return (
        "# Generated by Cardloop (grok_engine.ensure_home).\n"
        "[cli]\n"
        "auto_update = false\n"
        "\n"
        "# Defence in depth for the model's own shell: tool subprocesses inherit a small core set\n"
        "# (PATH, HOME...) with *KEY*/*SECRET*/*TOKEN* names dropped, not the whole agent env.\n"
        "[shell_environment_policy]\n"
        'inherit = "core"\n'
        "\n"
        "[skills]\n"
        f"ignore = [{_toml_str(_agents_dir())}]\n"
        f"disabled = [{', '.join(_toml_str(n) for n in _SKILLS_DISABLED)}]\n"
    )


def _config_ok(path: Path) -> bool:
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    skills = cfg.get("skills", {})
    return (cfg.get("cli", {}).get("auto_update") is False
            and cfg.get("shell_environment_policy", {}).get("inherit") == "core"
            and skills.get("ignore") == [_agents_dir()]
            and skills.get("disabled") == list(_SKILLS_DISABLED)
            and not any(k in cfg for k in _CONFIG_FORBIDDEN))


def ensure_home(ctx: dict | None = None, *, bin_path: str | None = None) -> dict:
    """Create/refresh Cardloop's Grok home. Returns {home, profile, deny, skipped}.

    A symlinked GROK_HOME is refused (Grok itself refuses to apply a sandbox over one, and it
    would let a retarget redirect where login and sessions land).
    """
    home = grok_home(ctx)
    if home.is_symlink():
        raise GrokUnavailableError(f"GROK_HOME {home} is a symlink — refusing (sandbox retarget hazard)")
    if home.exists() and not home.is_dir():
        raise GrokUnavailableError(f"GROK_HOME {home} exists and is not a directory")
    home.mkdir(parents=True, exist_ok=True)
    if not home.is_dir() or home.is_symlink():
        raise GrokUnavailableError(f"GROK_HOME {home} could not be created as a plain directory")
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    # The canary must exist BEFORE the deny list is built: literal entries that do not exist are
    # dropped (C7), and a probe canary missing from the profile would prove nothing.
    canary = _canary_dir(ctx)
    canary.mkdir(parents=True, exist_ok=True)
    secret = canary / "secret.txt"
    if not secret.is_file():
        secret.write_text("CANARY-" + uuid.uuid4().hex, encoding="utf-8")
        os.chmod(secret, 0o600)
    deny, skipped = build_deny(home, ctx, bin_path=bin_path)
    rewrote = _atomic_write(home / "sandbox.toml", _sandbox_toml(deny))
    if not _config_ok(home / "config.toml"):
        _atomic_write(home / "config.toml", _config_toml(), 0o600)
        rewrote = True
    if rewrote:
        _log(f"home {home}: sandbox profile {SANDBOX_PROFILE!r} written, deny={len(deny)} "
             f"entries, skipped_missing={len(skipped)}")
    sweep_foreign_layers(home)
    return {"home": home, "profile": SANDBOX_PROFILE, "deny": deny, "skipped": skipped}


_LITTER_RE = re.compile(r"^sandbox-blocked(?:-dir)?\.(\d+)$")


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, pgrp) of a pid from /proc, or None when it does not exist."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = raw[raw.rindex(")") + 2:].split()
    return rest[0], int(rest[2])


def _pid_alive(pid: int) -> bool:
    st = _proc_stat(pid)
    if st is not None:
        return st[0] != "Z"
    if Path("/proc/self/stat").exists():
        return False  # procfs is there and has no such pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    """Any non-zombie process left in the process group?"""
    if Path("/proc/self/stat").exists():
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            st = _proc_stat(int(entry.name))
            if st is not None and st[1] == pgid and st[0] != "Z":
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Layers of GROK_HOME that the CLI loads as INSTRUCTIONS or extra tooling and that the model's own shell can
# write (MEASURED on grok 1.0.46, 2026-10-03: a turn in project A wrote every one of these, and a turn in
# project B then listed the planted rules as its own global user rules). The code-execution layers — `hooks/`,
# `config.toml`, `managed_config.toml`, `requirements.toml`, `trusted_folders.toml`, `sandbox.toml` — are
# kernel write-protected by the CLI itself (Read-only file system, same run) and are not listed. The engine
# never writes or needs any of these (it generates config.toml itself), so a turn must start without them:
# otherwise one hostile project's turn poisons every other opted-in project's next turn.
FOREIGN_LAYERS = ("rules", "AGENTS.md", "CLAUDE.md", "GROK.md", "skills", "agents", "personas", "workflows",
                  "commands", "plugins", "lsp.json", "settings.json")


def _unlock_tree(top: str) -> None:
    """`chmod u+rwx` on `top` and every REAL directory below it, so that `shutil.rmtree` can list them.

    Never follows a symlink, at any depth: GROK_HOME is writable by the model's shell and this runs in the
    cockpit, unsandboxed. `os.walk` lists a symlinked directory in `dirs` and `os.chmod` follows it, so a link
    planted inside `rules/` used to get its TARGET chmod'ed (spec-096 P8 / review-spec095-security #2). A
    directory is entered only if `scandir` says it is one WITHOUT following links, and the chmod itself is
    `follow_symlinks=False` (the kernel refuses a link with NotImplementedError — also when the entry was
    swapped for a link after the scan). A link is left alone here; `rmtree` unlinks it, never descends."""
    stack = [top]
    while stack:
        cur = stack.pop()
        try:
            os.chmod(cur, stat.S_IRWXU, follow_symlinks=False)
        except (NotImplementedError, OSError):
            continue          # a link (or gone, or no nofollow chmod on this libc): leave it, rmtree decides
        try:
            with os.scandir(cur) as it:
                stack.extend(e.path for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            continue


def sweep_foreign_layers(home: Path) -> list[str]:
    """Remove the model-writable instruction layers from GROK_HOME before a turn. Returns the names removed.
    A symlink is unlinked, never followed. A layer that cannot be removed is an error (fail closed): the next
    turn would read it as the operator's own rules."""
    removed: list[str] = []
    for name in FOREIGN_LAYERS:
        path = home / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GrokUnavailableError(f"cannot inspect {path} in the Grok home: {exc!r}") from exc
        try:
            if stat.S_ISDIR(st.st_mode):
                _unlock_tree(str(path))                           # a model can leave mode-000 directories behind
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            raise GrokUnavailableError(
                f"cannot remove {path} from the Grok home: {exc!r} — the next turn would load it as a rule") from exc
        removed.append(name)
    if removed:
        _log(f"home {home}: removed {', '.join(removed)} (written by a model's shell, loaded as global "
             f"instructions by every project's next turn)")
    return removed


def reap_litter(home: Path, own_pid: int | None = None) -> list[str]:
    """Delete the placeholders every sandboxed Grok spawn leaves in its home.

    `sandbox-blocked-dir.<pid>` is mode 000 (so a plain rm fails) and `sandbox-blocked.<pid>`
    is an empty file; ~2 per spawn, forever. Only entries of OUR process or of a pid that no
    longer exists are removed: a concurrent turn's placeholders may still be a mount source.
    """
    removed: list[str] = []
    try:
        entries = list(os.scandir(home))
    except OSError:
        return removed
    for entry in entries:
        m = _LITTER_RE.match(entry.name)
        if not m:
            continue
        pid = int(m.group(1))
        if pid != own_pid and _pid_alive(pid):
            continue
        path = Path(entry.path)
        try:
            if entry.is_dir(follow_symlinks=False):
                os.chmod(path, stat.S_IRWXU)
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(entry.name)
        except OSError as exc:
            _log(f"reaper: could not remove {entry.name}: {exc!r}")
    return removed


# ------------------------------------------------------------------------------------------
# auth.json (key NAMES and booleans only — token values are never read into a variable)
# ------------------------------------------------------------------------------------------

def read_auth_facts(home: Path) -> dict:
    """{present, oidc, retention_opt_out, email} from <home>/auth.json. Secrets never leave."""
    facts = {"present": False, "oidc": False, "retention_opt_out": False, "email": None}
    # auth.json sits in the model-writable home: a capped, O_NOFOLLOW, regular-file-only read (a multi-GB file
    # or a FIFO planted under the name would otherwise OOM / hang the cockpit). Unreadable = not signed in.
    raw = grok_jsonl.read_small(home / "auth.json", AUTH_MAX_BYTES)
    if raw is None:
        return facts
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return facts
    if not isinstance(data, dict):
        return facts
    facts["present"] = True
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("auth_mode", "")).lower() == "oidc":
            facts["oidc"] = True
            facts["retention_opt_out"] = entry.get("coding_data_retention_opt_out") is True
            email = entry.get("email")
            facts["email"] = email if isinstance(email, str) else None
            break
    return facts


def _legacy_login_hint(home: Path, ctx: dict | None = None) -> str:
    """The default GROK_HOME used to be `<data>/grok-home` (inside the data dir); it is now `<data>-grok-home`.
    A login made at the old place is invisible now — say where it is instead of a bare "not signed in"."""
    if os.environ.get("GROK_HOME", "").strip():
        return ""
    old = data_dir(ctx) / "grok-home"
    if (old / "auth.json").is_file():
        return (f" (a login exists at the OLD default location {old}: the home now lives next to the data "
                f"dir — `mv {old} {home}`)")
    return ""


ACCOUNT_PIN_FILE = "grok_account.json"


def _account_pin_path(ctx: dict | None = None) -> Path:
    return data_dir(ctx) / ACCOUNT_PIN_FILE


def read_account_pin(ctx: dict | None = None) -> str | None:
    """The account (lower-cased e-mail) this cockpit's Grok login was set up with, or None."""
    try:
        data = json.loads(_account_pin_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email.lower() if isinstance(email, str) and email else None


def pin_account(email: str, ctx: dict | None = None) -> None:
    path = _account_pin_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({"email": email.lower()}) + "\n", 0o600)


def clear_account_pin(ctx: dict | None = None) -> None:
    with _suppress():
        _account_pin_path(ctx).unlink()


def _account_pin_problem(facts: dict, ctx: dict | None = None) -> str | None:
    """`auth.json` is WRITABLE by the model's shell (measured on grok 1.0.46: an append from a turn landed on
    the host). A prompt-injected turn could therefore swap in another account's login, after which every later
    turn — in every project — would send its code to that account. The account is pinned OUT of the model's
    reach (the data dir is hidden from its shell): the first verified login is recorded, a different one is
    refused until the operator runs `tools/grok-acct login`, which re-pins on purpose."""
    email = facts.get("email")
    pin = read_account_pin(ctx)
    if not isinstance(email, str) or not email:
        return ("the Grok login names no account, so it cannot be checked against the one this cockpit was set "
                "up with — run `tools/grok-acct login`") if pin else None
    if pin is None:
        try:
            pin_account(email, ctx)
        except OSError as exc:
            return f"cannot record the Grok account this cockpit is set up with ({exc!r})"
        _log("account pinned (first verified login)")
        return None
    if pin != email.lower():
        return ("the Grok login in GROK_HOME now names a different account than the one this cockpit was set up "
                "with (a turn's shell can rewrite auth.json). If you changed the account on purpose run "
                "`tools/grok-acct login`; otherwise `tools/grok-acct logout` and sign in again")
    return None


def _auth_problem(facts: dict, home: Path | None = None, ctx: dict | None = None) -> str | None:
    if not facts["present"]:
        return ("Grok is not signed in — run `tools/grok-acct login`"
                + (_legacy_login_hint(home, ctx) if home is not None else ""))
    if not facts["oidc"]:
        return ("Grok must be signed in with a grok.com (OIDC) subscription login; "
                "API-key auth is not allowed")
    if not facts["retention_opt_out"]:
        return ("this Grok account has not opted out of coding-data retention "
                "(coding_data_retention_opt_out is not true) — refusing to send code to xAI")
    return None


# ------------------------------------------------------------------------------------------
# capabilities / provider_info
# ------------------------------------------------------------------------------------------

def capabilities() -> dict:
    """Static capability map. Unlike Codex it names the four it does NOT have explicitly (false);
    runtime.capability_conflicts and the UI treat absent and false identically."""
    return _capabilities()


def _capabilities() -> dict:
    return {
        "chat": True, "board": True, "history": True, "search": True, "usage": True,
        "interrupt": True, "multi_agent": True,
        "ask_mode": False, "plan_mode": False, "skills": False, "plugins": False,
    }


_last_auth_meta: dict = {}
# "unknown" until the availability probe ran; a run refuses on "failed" (fail closed).
_sandbox_verdict: dict = {"state": "unknown", "detail": ""}


def _info(enabled: bool, available: bool, error: str | None, **extra) -> dict:
    data = {
        "provider": PROVIDER, "enabled": enabled, "available": available,
        "authenticated": available, "auth_type": "oidc" if available else None,
        "plan_type": _last_auth_meta.get("subscription_tier"),
        "models": [], "reasoning_levels": list(GROK_REASONING_LEVELS),
        "capabilities": _capabilities(), "error": error,
    }
    data.update(extra)
    return data


def mark_unavailable(reason: str) -> None:
    """Flip the cached registry row (e.g. login expired mid-turn) until the next real probe."""
    prev = _registry_cache["data"] or {}
    _registry_cache.update(ts=time.time(), data=_info(
        True, False, reason, models=prev.get("models", []), version=prev.get("version"),
        warnings=prev.get("warnings", [])))
    _log(f"registry flipped unavailable: {reason}")


def reset_cache() -> None:
    _registry_cache.update(ts=0.0, data=None)
    _sandbox_verdict.update(state="unknown", detail="")


_VERSION_RE = re.compile(r"^grok\s+(\d+\.\d+\.\d+)\b")


async def _run_probe_cmd(binary: str, args: list[str], env: dict, timeout: float,
                         cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        binary, *args, env=env, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        _kill_group(proc.pid, signal.SIGKILL)  # the probe's whole tree, finished or not
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class _suppress:
    """`contextlib.suppress(Exception)` — cancellation and exit signals still propagate."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc is not None and isinstance(exc, Exception)


def _kill_group(pgid: int | None, sig: int) -> None:
    if not pgid:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def parse_models_output(text: str) -> tuple[bool, str | None, list[dict]]:
    """`grok models` has no machine-readable mode. -> (logged_in, default_model, rows)."""
    logged_in = "logged in with grok.com" in text.lower()
    default = None
    rows: list[dict] = []
    in_list = False
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^Default model:\s*(\S+)", stripped)
        if m:
            default = m.group(1)
        if stripped.lower().startswith("available models"):
            in_list = True
            continue
        if in_list and stripped[:1] in {"*", "-"}:
            parts = stripped[1:].split()
            if parts:
                rows.append({"value": parts[0], "label": parts[0],
                             "default": "(default)" in stripped.lower()})
    if default:
        for row in rows:
            row["default"] = row["value"] == default
    for row in rows:
        row.setdefault("default_reasoning", "high")
        row["reasoning_levels"] = list(GROK_REASONING_LEVELS)
    return logged_in, default, rows


async def provider_info(*, force: bool = False) -> dict:
    """Feature flag, login, models and capability metadata — cached 300 s like Codex."""
    if not grok_enabled():
        return _info(False, False, "Grok is disabled by GROK_ENABLED=false")
    now = time.time()
    cached = _registry_cache["data"]
    if not force and cached is not None:
        ttl = _REGISTRY_TTL_SEC if cached.get("available") else _REGISTRY_FAIL_TTL_SEC
        if now - _registry_cache["ts"] < ttl:
            return cached
    # Two callers asking while a probe is running (startup + a registry read) share ONE probe: it
    # can include a real model turn (the sandbox probe) and must not be paid for twice.
    global _inflight
    loop = asyncio.get_running_loop()
    if _inflight is not None and not _inflight.done() and _inflight.get_loop() is loop:
        return await asyncio.shield(_inflight)
    fut = asyncio.ensure_future(_probe_provider())
    _inflight = fut
    try:
        data = await asyncio.shield(fut)
    finally:
        if _inflight is fut and fut.done():
            _inflight = None
    _registry_cache.update(ts=now, data=data)
    return data


async def _probe_provider() -> dict:
    warnings: list[str] = []

    def fail(reason: str, **extra) -> dict:
        _log(f"provider_info: unavailable — {reason}")
        return _info(True, False, reason, warnings=warnings, **extra)

    binary = grok_bin()
    if not binary:
        return fail("Grok CLI not found (set GROK_BIN, put `grok` on PATH, or install it under ~/.grok/bin)")
    try:
        info = ensure_home(bin_path=binary)
    except GrokUnavailableError as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail(f"cannot prepare GROK_HOME: {exc}")
    home = info["home"]
    trust = _trust_store_problem(home)
    if trust:   # every turn would be refused: say so in the registry instead of failing turn by turn
        return fail(trust)
    env = child_env(home, sandbox=False)
    try:
        code, out, err = await _run_probe_cmd(binary, ["--version"], env, PROBE_TIMEOUT_SEC, cwd=str(home))
    except Exception as exc:
        return fail(f"`grok --version` failed: {exc!r}")
    m = _VERSION_RE.match(out.strip())
    if code != 0 or not m:
        return fail(f"unrecognised `grok --version` output (exit {code})")
    version = m.group(1)
    if version not in KNOWN_GOOD_VERSIONS:
        warnings.append(f"grok {version} is newer than the builds this adapter was verified "
                        f"against ({', '.join(KNOWN_GOOD_VERSIONS)})")
        _log(f"warn: {warnings[-1]}")
    facts = read_auth_facts(home)
    problem = _auth_problem(facts, home) or _account_pin_problem(facts)
    if problem:
        return fail(problem, version=version)
    if not shutil.which("bwrap", path=env.get("PATH")):
        return fail("bubblewrap (bwrap) is required for the Grok sandbox and was not found on PATH",
                    version=version)
    try:
        code, out, err = await _run_probe_cmd(binary, ["models"], env, PROBE_TIMEOUT_SEC, cwd=str(home))
    except Exception as exc:
        return fail(f"`grok models` failed: {exc!r}", version=version)
    logged_in, default, models = parse_models_output(out)
    if code != 0 or not logged_in:
        first = (out.strip().splitlines() or [err.strip()[:120] or "no output"])[0]
        return fail(f"`grok models` says Grok is not signed in with grok.com ({first})", version=version)
    if not models:
        return fail("`grok models` listed no models", version=version)
    state, detail = await _ensure_sandbox_probe(binary, version, info, home)
    _sandbox_verdict.update(state=state, detail=detail)
    if state != "ok":
        return fail(f"sandbox denial probe {state}: {detail}", version=version)
    _log(f"provider_info: available version={version} models={len(models)} "
         f"sandbox={SANDBOX_PROFILE} deny={len(info['deny'])} probe=ok")
    return _info(True, True, None, models=models, version=version, warnings=warnings,
                 sandbox={"profile": SANDBOX_PROFILE, "deny_count": len(info["deny"]),
                          "bwrap": True, "probe": "ok"})


# ------------------------------------------------------------------------------------------
# sandbox denial probe (a real, tiny turn; verdict cached on disk by fingerprint)
# ------------------------------------------------------------------------------------------

def judge_probe(control: str, canary: str, haystack: str, end: str) -> tuple[str, str]:
    """Verdict of the probe turn. The canary leaking is a hard FAIL. Anything short of the whole command
    having run is inconclusive (fail closed — an `ok` is cached for days): no control file means it did not
    run, no END token means it stopped early, and no NON-ZERO `rc=` means the canary read was never
    attempted or did not fail (a denied read exits non-zero; a read that exited 0 and showed no canary
    proves nothing about the deny list)."""
    if canary in haystack:
        return "failed", "the sandbox deny list did NOT hide the canary file from the model's terminal"
    if control not in haystack:
        return "inconclusive", "the probe turn never read its control file (command did not run?)"
    if end not in haystack:
        return "inconclusive", "the probe command stopped before its last step (the canary read was never reached?)"
    if not re.search(r"\brc=[1-9]\d*", haystack):
        return "inconclusive", "the probe never showed a failed canary read (no non-zero exit status printed for it)"
    return "ok", "canary unreadable, control readable"


def _haystack(msg: dict) -> str:
    """Every string in a wire message, plus byte arrays (Grok sends raw output as int lists)."""
    parts: list[str] = []

    def walk(x):
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            if x and all(isinstance(i, int) and 0 <= i < 256 for i in x):
                parts.append(bytes(x).decode("utf-8", "replace"))
            else:
                for v in x:
                    walk(v)
    walk(msg)
    return "\n".join(parts)


def _probe_fingerprint(version: str, info: dict) -> str:
    h = hashlib.sha256()
    h.update(version.encode())
    h.update(_sandbox_toml(info["deny"]).encode())
    h.update(str(info["home"]).encode())
    return h.hexdigest()[:24]


def reset_sandbox_probe(ctx: dict | None = None) -> None:
    """Forget the persisted verdict so the next provider_info(force=True) re-runs the probe."""
    with _suppress():
        (data_dir(ctx) / "grok_sandbox_probe.json").unlink()
    _sandbox_verdict.update(state="unknown", detail="")


async def _ensure_sandbox_probe(binary: str, version: str, info: dict, home: Path) -> tuple[str, str]:
    path = data_dir() / "grok_sandbox_probe.json"
    fp = _probe_fingerprint(version, info)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(cached.get("ts", 0))
        ttl = SANDBOX_PROBE_OK_TTL_SEC if cached.get("state") == "ok" else SANDBOX_PROBE_FAIL_TTL_SEC
        if cached.get("fingerprint") == fp and age < ttl:
            return cached.get("state", "failed"), cached.get("detail", "")
    except (OSError, ValueError, TypeError):
        pass
    state, detail = await _probe_sandbox_denial(info)
    _log(f"sandbox denial probe: {state} — {detail}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps({"fingerprint": fp, "state": state, "detail": detail,
                                         "ts": time.time()}))
    except OSError as exc:
        _log(f"could not persist the sandbox probe verdict: {exc!r}")
    return state, detail


# The probe asks the model to run a shell command that reads a file it is supposed to be unable to
# read. Measured (spec-095 P1b/P7a): now and then the model declines it as a "security self-test"
# ("I won't run that command") and the probe, which then measured nothing, took the provider offline
# for 15 minutes. The retry therefore uses different words — see _probe_prompt.
_PROBE_SAID_CHARS = 300


def _probe_prompt(canary_file: Path, *, retry: bool) -> str:
    """The probe turn's prompt. The first attempt is the original wording. A model that declined it
    gets ONE retry in a fresh session with a plainer request (no "security self-test", no claim of
    authority). MEASURED live: an "authorised by the operator / expected to be denied" preamble — the
    obvious fix — was refused 3 times out of 3 (the model calls the claim a social-engineering framing
    and says so); the original wording passed 10 of 10 and the plain one 5 of 5."""
    # end.txt is read LAST: its token reaches the wire only if the whole command ran; `rc=$?` prints the
    # canary read's exit status, which is non-zero only if that read really failed — a model that runs
    # just the first part, or drops the middle, cannot earn an `ok` for a read it never attempted
    command = f'cat control.txt; echo ---; cat {canary_file} 2>&1; echo "rc=$?"; cat end.txt'
    if not retry:
        return ("Security self-test, not a coding task. Run exactly this one shell command with your "
                f"terminal tool, then reply with the single word DONE: {command}")
    return ("Run exactly this one shell command with your terminal tool, then reply with the single word "
            f"DONE: {command}")


class _ProbeAttempt(NamedTuple):
    state: str          # ok | failed | inconclusive
    detail: str
    said: str           # what the model wrote (redacted, capped)
    ran_tool: bool      # it issued a tool call
    errored: bool       # the turn itself failed (spawn, auth, timeout): not a model decision


async def _probe_attempt(info: dict, canary_file: Path, canary: str, *, retry: bool) -> _ProbeAttempt:
    """One probe turn."""
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="grok-probe-"))
    control = "CONTROL-" + uuid.uuid4().hex
    end = "END-" + uuid.uuid4().hex
    (work / "control.txt").write_text(control, encoding="utf-8")
    (work / "end.txt").write_text(end, encoding="utf-8")
    seen: list[str] = []
    said: list[str] = []
    ran_tool = False
    prompt = _probe_prompt(canary_file, retry=retry)
    try:
        async def drive():
            nonlocal ran_tool
            async for ev in _run_turn(
                    project_name="__probe__", cwd=str(work), prompt=prompt, session_key="__probe__",
                    model=None, resume_session_id=None, ctx={"DATA": data_dir()}, effort="low",
                    entrypoint="probe", tap=lambda m: seen.append(_haystack(m)), _gate=False):
                if ev["type"] == "error":
                    raise ev["exc"]
                if ev["type"] == "tool":
                    ran_tool = True
                elif ev["type"] == "text":
                    said.append(str(ev.get("text") or ""))
        await asyncio.wait_for(drive(), SANDBOX_PROBE_TURN_SEC)
    except Exception as exc:
        return _ProbeAttempt("inconclusive", f"probe turn failed: {_redact(str(exc))[:300]}", "", ran_tool, True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    state, detail = judge_probe(control, canary, "\n".join(seen), end)
    return _ProbeAttempt(state, detail, _redact(" ".join(" ".join(said).split()))[:_PROBE_SAID_CHARS],
                         ran_tool, False)


async def _probe_sandbox_denial(info: dict) -> tuple[str, str]:
    """Run ONE tiny real turn under the production profile and see whether the canary file is
    readable. This is the only way to learn that the kernel deny works on THIS host.

    A turn in which the model ran NO command (it declined the "security self-test") measured nothing:
    its words go to the journal and the probe is retried ONCE with plainer wording. Still
    nothing -> inconclusive (the provider stays unavailable: fail closed). A verdict from a turn that
    did run a command — a leak above all — is never retried."""
    canary_file = _canary_dir() / "secret.txt"
    try:
        canary = canary_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "failed", f"canary file unreadable by the cockpit itself: {exc!r}"
    first = await _probe_attempt(info, canary_file, canary, retry=False)
    if first.state != "inconclusive" or first.ran_tool or first.errored:
        return first.state, first.detail
    _log(f"sandbox denial probe: the model ran no command (it said: {first.said!r}) — retrying once "
         f"with plainer wording")
    second = await _probe_attempt(info, canary_file, canary, retry=True)
    if second.state == "inconclusive" and not second.ran_tool and not second.errored:
        _log(f"sandbox denial probe: the model declined again (it said: {second.said!r})")
        return second.state, f"{second.detail}; the model declined twice, last words: {second.said!r}"
    return second.state, second.detail


# ------------------------------------------------------------------------------------------
# rules text (_meta.rules) — mirrors codex_engine._developer_instructions
# ------------------------------------------------------------------------------------------

def _project_rules(cwd: str) -> str | None:
    """<cwd>/CLAUDE.md, size-capped. Never ~/CLAUDE.md (operator-private, must not go to xAI):
    the symlink-resolved path must live inside the project and must not BE the home file."""
    path = Path(cwd) / "CLAUDE.md"
    if not path.is_file():
        return None
    real = os.path.realpath(path)
    if real == os.path.realpath(Path.home() / "CLAUDE.md"):
        _log("rules: refusing to send ~/CLAUDE.md to Grok (cwd resolves to the home rules file)")
        return None
    if not _is_under(real, cwd):
        _log("rules: CLAUDE.md resolves outside the project directory — not sent")
        return None
    try:
        with open(real, "rb") as fh:
            raw = fh.read(RULES_MAX_BYTES + 1)
    except OSError:
        return None
    text = raw[:RULES_MAX_BYTES].decode("utf-8", "replace")
    if len(raw) > RULES_MAX_BYTES:
        text += f"\n[... truncated at {RULES_MAX_BYTES // 1024} KiB ...]"
    return text


def _instructions(project_name: str, cwd: str, *, multi_agent: bool) -> str:
    lines = [
        "You are the Grok engine inside Cardloop.",
        f"The selected project is {project_name!r} and its working directory is {cwd!r}.",
        "Follow the project's own rules below when they exist, and do not copy them into another file.",
    ]
    if multi_agent:
        lines.append(
            "Use subagents when independent parallel work materially improves the result, "
            "and synthesize their findings.")
    rules = _project_rules(cwd)
    if rules:
        lines.append("<project_rules source=\"CLAUDE.md\">\n" + rules + "\n</project_rules>")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------
# tool-name map (D10) — Grok tool + raw input -> the Claude vocabulary _format_tool renders
# ------------------------------------------------------------------------------------------

def _pick(d: dict, *keys, default=""):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _t_bash(i):
    return "Bash", {"command": _pick(i, "command", "cmd"), "description": _pick(i, "description")}


def _t_edit(i):
    return "Edit", {"file_path": _pick(i, "file_path", "path", "filePath"),
                    "old_string": _pick(i, "old_string", "old_str", "oldString"),
                    "new_string": _pick(i, "new_string", "new_str", "newString")}


def _t_write(i):
    return "Write", {"file_path": _pick(i, "file_path", "path", "filePath"),
                     "content": _pick(i, "content", "contents", "text")}


def _t_read(i):
    return "Read", {"file_path": _pick(i, "target_file", "file_path", "path", "filePath")}


def _t_grep(i):
    return "Grep", {"pattern": _pick(i, "pattern", "query", "regex"), "path": _pick(i, "path", "directory")}


def _t_ls(i):
    return "LS", {"path": _pick(i, "target_directory", "path", "directory", "dir")}


def _t_websearch(i):
    return "WebSearch", {"query": _pick(i, "query", "q")}


def _t_webfetch(i):
    return "WebFetch", {"url": _pick(i, "url")}


def _t_todo(i):
    # Claude's TodoWrite item is {content, status, activeForm}; Grok's is {id, content, status}.
    todos = [{"content": t.get("content", ""), "status": t.get("status", "pending"),
              "activeForm": t.get("content", "")}
             for t in (i.get("todos") or []) if isinstance(t, dict)]
    return "TodoWrite", {"todos": todos}


GROK_TOOL_MAP: dict[str, Callable[[dict], tuple[str, dict]]] = {
    "run_terminal_command": _t_bash,
    "search_replace": _t_edit,
    "write": _t_write,
    "read_file": _t_read,
    "grep": _t_grep,
    "list_dir": _t_ls,
    "web_search": _t_websearch,
    "web_fetch": _t_webfetch,
    "todo_write": _t_todo,
}
SUBAGENT_TOOLS = ("spawn_subagent",)
_unknown_tools_seen: set[str] = set()
_unknown_updates_seen: set[str] = set()

# session/update kinds that are pure protocol noise for the cockpit.
_IGNORED_UPDATES = {
    "agent_thought_chunk", "available_commands_update", "user_message_chunk", "plan",
    "current_mode_update", "config_option_update", "session_info_update",
}


def map_tool(name: str, raw_input) -> tuple[str, dict]:
    """Grok (tool name, rawInput) -> (Cardloop name, input). Unknown names pass through and are
    journaled ONCE each so a new Grok tool shows up in the log instead of vanishing."""
    inp = raw_input if isinstance(raw_input, dict) else {}
    fn = GROK_TOOL_MAP.get(name)
    if fn is not None:
        return fn(inp)
    if name not in _unknown_tools_seen:
        _unknown_tools_seen.add(name)
        _log(f"unmapped tool {name!r} passes through (input keys: {sorted(inp)[:8]})")
    return name or "?", dict(inp)


# ------------------------------------------------------------------------------------------
# isolation tripwire: what the AGENT says it loaded, whatever the reason
# ------------------------------------------------------------------------------------------

def _isolation_signal(msg: dict) -> str | None:
    """A reason to refuse the turn when a wire notification reports an MCP server or a hook.

    v1 grants a Grok turn NO MCP server (session/new sends `mcpServers: []`) and has no hooks, so
    anything of the kind comes from config the engine did not choose: a project's `.mcp.json` /
    `.grok/config.toml` / `.grok/hooks` once folder trust is granted, a gateway connector, a
    plugin. The preventive layers are folder trust + the kernel deny of ~/.claude*; this is the
    check that does not care WHICH layer failed. Names only — args/env can carry secrets.
    Shapes measured live (P1b) with a project MCP server and hook that were allowed to start."""
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "_x.ai/mcp/servers_updated":
        servers = params.get("mcpServers")
        if isinstance(servers, list) and servers:
            names = [str(sv.get("name") or "?")[:60] for sv in servers if isinstance(sv, dict)] or ["?"]
            return f"the agent loaded MCP server(s) {', '.join(names[:5])} — a Grok turn is granted none"
    elif method == "_x.ai/mcp_initialized":
        count = params.get("mcpToolCount")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return f"the agent exposes {count} MCP tool(s) — a Grok turn is granted none"
    elif method == "_x.ai/session_notification":
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        if update.get("sessionUpdate") == "hook_execution":
            return f"a hook ran in the Grok session ({str(update.get('event_name') or '?')[:60]}) — none is granted"
    elif method == "session/update":
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        if update.get("sessionUpdate") == "available_commands_update":
            tools = (update.get("_meta") or {}).get("tools") if isinstance(update.get("_meta"), dict) else None
            mcp = [t for t in tools if isinstance(t, str) and "__" in t] if isinstance(tools, list) else []
            if mcp:
                return f"the agent exposes MCP tool(s) {', '.join(mcp[:3])} — a Grok turn is granted none"
    return None


# ------------------------------------------------------------------------------------------
# ACP client
# ------------------------------------------------------------------------------------------

class _AcpExit(Exception):
    """The agent process ended (or its stdout closed) while we were waiting for it."""


class _AcpTimeout(Exception):
    def __init__(self, method: str, timeout: float):
        super().__init__(f"{method} did not answer within {timeout:g}s")
        self.method, self.timeout = method, timeout


class _AcpError(Exception):
    def __init__(self, method: str, error: dict):
        super().__init__(f"{method}: {error.get('message', error)}"
                         + (f" ({error['data']})" if error.get("data") else ""))
        self.method, self.error = method, error


class _Acp:
    """Line-delimited JSON-RPC over a child's stdio.

    One reader task owns stdout. Handshake replies resolve futures; every notification AND the
    prompt's own response go into ONE queue so their wire order is preserved. Server-initiated
    requests are answered on the spot (never left hanging: an unanswered permission request ends
    the turn as `cancelled`)."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.pgid = proc.pid  # start_new_session=True: the leader's pid is the group id
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._prompt_id: int | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.fatal: Exception | None = None
        self.exited = False
        # Set by the READER (not by whoever consumes the queue) the moment the prompt's response
        # or the process's end arrives: interrupt() must not depend on the consumer being awake.
        self.prompt_done = asyncio.Event()
        self.permission_requests = 0
        self.non_json_lines = 0
        # First sign, on the wire, of a tool surface this engine never grants (see _isolation_signal).
        # Set by the READER as the notification arrives, read by whoever consumes the queue.
        self.isolation: str | None = None
        # sessionId -> cancellationCategory of its `_x.ai/session/prompt_complete` (diagnostics only)
        self.cancellation: dict[str, str] = {}
        self._stderr = bytearray()
        self._reader = asyncio.ensure_future(self._read_loop())
        self._err_task = asyncio.ensure_future(self._drain_stderr())

    # --- diagnostics -------------------------------------------------------------------
    def stderr_tail(self) -> str:
        return _redact(bytes(self._stderr).decode("utf-8", "replace").strip())

    async def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = await self.proc.stderr.read(65536)
                if not chunk:
                    return
                self._stderr += chunk
                if len(self._stderr) > STDERR_RING_BYTES:
                    del self._stderr[:-STDERR_RING_BYTES]
        except Exception:
            return

    # --- reading -----------------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self.fatal = GrokProtocolError(
                        f"event too large: Grok sent a single line over {STREAM_LIMIT // 1024} KiB — "
                        f"turn aborted")
                    _log(f"protocol: {self.fatal}")
                    self.kill_group(signal.SIGKILL)
                    return
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.non_json_lines += 1
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except Exception as exc:  # pragma: no cover - defensive: never leave waiters hanging
            self.fatal = self.fatal or exc
        finally:
            self.exited = True
            self.prompt_done.set()
            err = _AcpExit("Grok closed its output")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            self.queue.put_nowait(("eof", None))

    async def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if "method" in msg and mid is not None:
            await self._answer_server_request(msg)
        elif "method" in msg:
            self._watch(msg)
            self.queue.put_nowait(("note", msg))
        elif mid is not None and ("result" in msg or "error" in msg):
            if mid == self._prompt_id:
                self.queue.put_nowait(("response", msg))
                self.prompt_done.set()
                return
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    def _watch(self, msg: dict) -> None:
        """Wire tripwires. Never raises: the reader task must outlive a malformed notification."""
        try:
            if msg.get("method") == "_x.ai/session/prompt_complete":
                p = msg.get("params") or {}
                if isinstance(p.get("sessionId"), str) and isinstance(p.get("cancellationCategory"), str):
                    self.cancellation[p["sessionId"]] = p["cancellationCategory"]
            if self.isolation is None:
                self.isolation = _isolation_signal(msg)
                if self.isolation:
                    _log(f"isolation tripwire: {self.isolation}")
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"wire watch failed: {exc!r}")

    async def _answer_server_request(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg["id"]
        if method == "session/request_permission":
            # yoloMode should make this unreachable. If it is not, answering "allow once" keeps
            # a full-auto turn alive; a silent or errored reply would end it as `cancelled`.
            self.permission_requests += 1
            options = (msg.get("params") or {}).get("options") or []
            pick = next((o for o in options if o.get("kind") == "allow_once"), None) or next(
                (o for o in options if str(o.get("kind", "")).startswith("allow")), None)
            if pick and pick.get("optionId") is not None:
                result = {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}
            else:
                result = {"outcome": {"outcome": "cancelled"}}
            _log(f"unexpected session/request_permission answered "
                 f"{'allow_once' if pick else 'cancelled'} (yoloMode should suppress it)")
            await self._send({"jsonrpc": "2.0", "id": mid, "result": result})
        else:
            await self._send({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": -32601, "message": f"method not supported: {method}"}})

    # --- writing -----------------------------------------------------------------------
    async def _send(self, obj: dict) -> None:
        if self.exited or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise _AcpExit("Grok is no longer running")
        try:
            self.proc.stdin.write((json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ConnectionError) as exc:
            raise _AcpExit("Grok closed its input") from exc

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise _AcpTimeout(method, timeout) from None
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise _AcpError(method, msg["error"] if isinstance(msg["error"], dict) else {"message": msg["error"]})
        return msg.get("result") or {}

    async def start_prompt(self, params: dict) -> None:
        """Send session/prompt; its response arrives IN ORDER through `queue`."""
        self._next_id += 1
        self._prompt_id = self._next_id
        await self._send({"jsonrpc": "2.0", "id": self._prompt_id, "method": "session/prompt",
                          "params": params})

    def discard_notes(self) -> int:
        """Drop notifications queued during setup (resume replays some); keep eof."""
        kept, dropped = [], 0
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item[0] == "note":
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            self.queue.put_nowait(item)
        return dropped

    # --- process control ---------------------------------------------------------------
    def kill_group(self, sig: int = signal.SIGKILL) -> None:
        _kill_group(self.pgid, sig)

    async def _wait_group_gone(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and (self.proc.returncode is None or _group_alive(self.pgid)):
            await asyncio.sleep(0.02)

    async def teardown(self, session_id: str | None) -> None:
        """close (best effort) -> SIGTERM -> grace -> SIGKILL, ALWAYS, also under cancellation.

        A cancellation that arrives mid-teardown is remembered and re-raised at the end: the
        kills below must run whatever happens to the waits around them."""
        cancelled: BaseException | None = None

        async def step(awaitable) -> None:
            nonlocal cancelled
            try:
                await awaitable
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                pass

        if session_id and not self.exited:
            await step(asyncio.wait_for(
                self.request("session/close", {"sessionId": session_id}, CLOSE_WAIT_SEC),
                CLOSE_WAIT_SEC + 0.5))
        try:
            if self.proc.stdin is not None and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
        self.kill_group(signal.SIGTERM)
        await step(self._wait_group_gone(TERM_WAIT_SEC))
        self.kill_group(signal.SIGKILL)
        await step(asyncio.wait_for(self.proc.wait(), 2.0))
        for task in (self._reader, self._err_task):
            if not task.done():
                task.cancel()
        await step(asyncio.wait({self._reader, self._err_task}, timeout=0.5))
        if cancelled is not None:
            raise cancelled


# ------------------------------------------------------------------------------------------
# the turn handle stored in ctx["running"][session_key]
# ------------------------------------------------------------------------------------------

class GrokTurn:
    """What webapp's Stop button calls: `await turn.interrupt()` (same shape as Codex's turn)."""

    def __init__(self, session_key: str):
        self.session_key = session_key
        self.session_id: str | None = None
        self.cancel_requested = False
        self.prompt_started = False
        self._acp: _Acp | None = None

    async def interrupt(self) -> None:
        acp = self._acp
        if acp is None or acp.exited or acp.prompt_done.is_set():
            return
        self.cancel_requested = True
        if not (self.prompt_started and self.session_id):
            _log(f"interrupt before the prompt started ({self.session_key}): killing the group")
            acp.kill_group(signal.SIGKILL)
            return
        _log(f"interrupt {self.session_key}: session/cancel")
        with _suppress():
            await acp.notify("session/cancel", {"sessionId": self.session_id})
        try:
            await asyncio.wait_for(acp.prompt_done.wait(), INTERRUPT_WAIT_SEC)
        except asyncio.TimeoutError:
            _log(f"interrupt {self.session_key}: no stopReason within {INTERRUPT_WAIT_SEC:g}s — killpg")
            acp.kill_group(signal.SIGKILL)


# ------------------------------------------------------------------------------------------
# event mapping (§5.4)
# ------------------------------------------------------------------------------------------

def _num(x) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


class _Mapper:
    """Turns wire notifications into Cardloop events, keeping assembled-text + usage state.

    Only updates of OUR session are mapped: a sub-agent runs as a second session whose own
    `session/update`s (text, tool calls) arrive on the same stdio with the child's sessionId."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.text_parts: list[str] = []
        self.seen_calls: set[str] = set()
        self.deferred: dict[str, str] = {}      # toolCallId -> tool name, input arrives later
        self.subagents: dict[str, str] = {}     # subagent_id -> description
        self.completed_usage: list[dict] = []   # one per model call of OUR session
        self.foreign_updates = 0

    def flush_text(self) -> list[dict]:
        text = "".join(self.text_parts)
        self.text_parts = []
        return [{"type": "text", "text": text}] if text.strip() else []

    def finish(self) -> list[dict]:
        """End of turn: assembled text, then any tool whose input never arrived."""
        out = self.flush_text()
        for call_id, name in list(self.deferred.items()):
            out.append(self._tool_event(name, {}))
            self.deferred.pop(call_id)
        return out

    def feed(self, msg: dict) -> list[dict]:
        method = msg.get("method")
        params = msg.get("params") or {}
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            return []
        kind = update.get("sessionUpdate")
        own = params.get("sessionId") in (None, self.session_id)
        if method == "session/update":
            if not own:
                self.foreign_updates += 1
                return []
            return self._session_update(kind, update)
        if method == "_x.ai/session_notification":
            if kind == "response_completed":
                if own and isinstance(update.get("usage"), dict):
                    self.completed_usage.append(update["usage"])
                return []
            if kind in ("subagent_spawned", "subagent_progress", "subagent_finished"):
                return self._subagent(kind, update)
        return []

    def _session_update(self, kind: str | None, update: dict) -> list[dict]:
        if kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str) and text:
                self.text_parts.append(text)
                return [{"type": "text_delta", "text": text}]
            return []
        if kind == "tool_call":
            return self.flush_text() + self._tool_call(update)
        if kind == "tool_call_update":
            return self._tool_update(update)
        if kind in _IGNORED_UPDATES or kind is None:
            return []
        if kind not in _unknown_updates_seen:
            _unknown_updates_seen.add(kind)
            _log(f"unmapped session update kind {kind!r} dropped")
        return []

    @staticmethod
    def _tool_name(update: dict) -> str:
        raw = update.get("rawInput")
        if isinstance(raw, dict) and raw.get("variant") == "WebSearch":
            return "web_search"  # a BACKEND tool: no x.ai/tool meta, title is "Web search:"
        meta = (update.get("_meta") or {}).get("x.ai/tool")
        if isinstance(meta, dict) and meta.get("name"):
            return str(meta["name"])
        return str(update.get("title") or update.get("toolName") or "")

    @staticmethod
    def _tool_event(name: str, raw) -> dict:
        mapped, inp = map_tool(name, raw)
        return {"type": "tool", "name": mapped, "input": inp}

    def _tool_call(self, update: dict) -> list[dict]:
        call_id = str(update.get("toolCallId") or "")
        if call_id and call_id in self.seen_calls:
            return []
        if call_id:
            self.seen_calls.add(call_id)
        name = self._tool_name(update)
        if name in SUBAGENT_TOOLS:
            return []  # the lifecycle comes from the subagent_* notifications
        raw = update.get("rawInput")
        if name == "web_search":
            self.deferred[call_id] = name  # the query only shows up in the completed update
            return []
        return [self._tool_event(name, raw)]

    def _tool_update(self, update: dict) -> list[dict]:
        call_id = str(update.get("toolCallId") or "")
        if call_id not in self.deferred or update.get("status") not in ("completed", "failed"):
            return []
        name = self.deferred.pop(call_id)
        action = (update.get("rawOutput") or {}).get("action") if isinstance(update.get("rawOutput"), dict) else None
        query = action.get("query") if isinstance(action, dict) else None
        return [self._tool_event(name, {"query": query or ""})]

    def _subagent(self, kind: str, update: dict) -> list[dict]:
        task_id = str(update.get("subagent_id") or update.get("child_session_id") or "")
        if not task_id:
            return []
        if kind == "subagent_spawned":
            desc = str(update.get("description") or update.get("subagent_type") or "subagent")[:500]
            self.subagents[task_id] = desc
            return [{"type": "subagent", "subtype": "started", "task_id": task_id,
                     "description": desc, "status": "running", "summary": None,
                     "last_tool_name": None}]
        desc = self.subagents.get(task_id, "subagent")
        if kind == "subagent_progress":
            tools = update.get("tools_used")
            return [{"type": "subagent", "subtype": "progress", "task_id": task_id,
                     "description": desc, "status": "running",
                     "summary": f"{_num(update.get('turn_count'))} turns, "
                                f"{_num(update.get('tool_call_count'))} tool calls",
                     "last_tool_name": tools[-1] if isinstance(tools, list) and tools else None}]
        ok = update.get("status") == "completed"
        out = update.get("output")
        return [{"type": "subagent", "subtype": "notification", "task_id": task_id,
                 "description": desc, "status": "completed" if ok else "failed",
                 "summary": out[:2000] if isinstance(out, str) else None, "last_tool_name": None}]

    # --- usage ---------------------------------------------------------------------------
    def usage_from(self, response_meta: dict) -> dict:
        """Aggregate usage for the turn: the response's own `_meta.usage` when present, else the
        sum of the per-model-call `response_completed` notifications."""
        agg = response_meta.get("usage") if isinstance(response_meta.get("usage"), dict) else None
        if agg:
            usage = {
                "input": _num(agg.get("inputTokens")), "output": _num(agg.get("outputTokens")),
                "cached": _num(agg.get("cachedReadTokens")),
                "reasoning": _num(agg.get("reasoningTokens")), "total": _num(agg.get("totalTokens")),
                "notional_usd": round(_num(agg.get("costUsdTicks")) / 1e10, 6),
            }
        else:
            calls = self.completed_usage
            usage = {
                "input": sum(_num(c.get("input_tokens")) + _num(c.get("cache_read_input_tokens")) for c in calls),
                "output": sum(_num(c.get("output_tokens")) for c in calls),
                "cached": sum(_num(c.get("cache_read_input_tokens")) for c in calls),
                "reasoning": sum(_num(c.get("reasoning_tokens")) for c in calls),
                "total": 0, "notional_usd": 0.0,
            }
            usage["total"] = usage["input"] + usage["output"]
        return usage

    def context_tokens(self, response_meta: dict) -> int:
        """Size of the LAST request's prompt (fresh + cache read): what the context window holds."""
        if self.completed_usage:
            last = self.completed_usage[-1]
            return (_num(last.get("input_tokens")) + _num(last.get("cache_read_input_tokens"))
                    + _num(last.get("cache_creation_input_tokens")))
        return _num(response_meta.get("inputTokens"))


# ------------------------------------------------------------------------------------------
# ledgers (D9 — writers only; readers/UI/counters are phase P4)
# ------------------------------------------------------------------------------------------

def _append_usage(data: Path | None, *, session_id: str | None, model: str, project_name: str,
                  session_key: str, entrypoint: str, usage: dict, duration_ms: int | None) -> None:
    if data is None:
        return
    try:
        row = {
            "ts": time.time(), "provider": PROVIDER, "session_id": session_id,
            "project": project_name, "session_key": session_key, "entrypoint": entrypoint,
            "model": model, "input": usage["input"], "output": usage["output"],
            "cached": usage["cached"], "reasoning": usage["reasoning"], "total": usage["total"],
            "duration_ms": duration_ms, "notional_usd": usage["notional_usd"],
        }
        with (data / "grok_usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        _log(f"usage ledger write failed: {exc!r}")


def _capture_limit_error(data: Path | None, *, source: str, text: str, session_id: str | None,
                         project_name: str, model: str) -> None:
    """Save the RAW text of anything quota-shaped. No parser on purpose (D9): the real shape of a
    Grok limit error is unknown until one is seen."""
    if data is None or not text or not _QUOTA_RE.search(text):
        return
    try:
        row = {"ts": time.time(), "source": source, "session_id": session_id,
               "project": project_name, "model": model,
               "text": _redact(text)[:LIMIT_ERROR_MAX_CHARS]}
        with (data / "grok_limit_errors.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        _log(f"quota-shaped failure captured to grok_limit_errors.jsonl (source={source})")
    except Exception as exc:
        _log(f"limit-error capture failed: {exc!r}")


# ------------------------------------------------------------------------------------------
# the engine
# ------------------------------------------------------------------------------------------

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _trust_store_problem(home: Path) -> str | None:
    """Folder trust is what keeps a project's own MCP servers/hooks/skills from starting. The store
    lives in OUR GROK_HOME and must stay empty: an entry means something (an interactive `grok`
    run with this home, a hand edit) granted it, so a turn would start them with full tool access.
    The sandbox write-denies the file for the model itself."""
    path = home / "trusted_folders.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{path} cannot be read ({exc.__class__.__name__}) — the folder-trust state is unknown"
    if any(ln.strip() and not ln.strip().startswith("#") for ln in text.splitlines()):
        return (f"folder trust is granted in {path}: a project's own MCP servers, hooks and skills "
                f"would start with full tool access — empty or delete that file (it is only ever "
                f"written by an interactive `grok` run that uses Cardloop's GROK_HOME)")
    return None


# `session/resume` answers without a `models` block, so the window of a model seen on any earlier
# new session of this process is remembered per model id.
_window_cache: dict[str, int] = {}


def _model_window(models_block, model_id: str | None) -> int | None:
    """totalContextTokens of `model_id` from a session/new|resume `models` block."""
    if not isinstance(models_block, dict):
        return None
    for item in models_block.get("availableModels") or []:
        if isinstance(item, dict) and item.get("modelId") == model_id:
            win = (item.get("_meta") or {}).get("totalContextTokens")
            return win if isinstance(win, int) else None
    return None


async def run_grok_engine(
    *, project_name: str, cwd: str, prompt: str, session_key: str, model: str | None = None,
    resume_session_id: str | None = None, ctx: dict | None = None, ephemeral: bool = False,
    effort: str | None = None, plan_mode: bool = False, multi_agent: bool = False,
    chat_id: str | None = None, entrypoint: str = "chat", **_ignored,
) -> AsyncGenerator[dict, None]:
    """Run one Grok turn and yield Cardloop-normalised events (same schema as run_codex_engine).

    `ephemeral` is accepted for signature parity; Grok sessions are always persisted in
    Cardloop's own GROK_HOME (they are what `resume_session_id` resumes)."""
    inner = _run_turn(
        project_name=project_name, cwd=cwd, prompt=prompt, session_key=session_key, model=model,
        resume_session_id=resume_session_id, ctx=ctx, effort=effort, plan_mode=plan_mode,
        multi_agent=multi_agent, chat_id=chat_id, entrypoint=entrypoint)
    try:
        async for ev in inner:
            yield ev
    finally:
        # Closing THIS generator must close the inner one too, or its finally (the group kill)
        # would wait for the garbage collector and a stopped turn would keep its process.
        await inner.aclose()


async def _run_turn(
    *, project_name: str, cwd: str, prompt: str, session_key: str, model: str | None,
    resume_session_id: str | None, ctx: dict | None, effort: str | None = None,
    plan_mode: bool = False, multi_agent: bool = False, chat_id: str | None = None,
    entrypoint: str = "chat", tap: "Callable[[dict], None] | None" = None, _gate: bool = True,
) -> AsyncGenerator[dict, None]:
    if not grok_enabled():
        yield {"type": "error", "exc": GrokUnavailableError("Grok is disabled by GROK_ENABLED=false")}
        return
    if plan_mode:
        # Plan mode cannot be sandbox-enforced on this host (spec §10-C5) and Grok has no ACP
        # switch for it: never run a "plan" turn that could still write.
        yield {"type": "error", "exc": GrokUnavailableError(
            "Grok does not support plan mode (a read-only turn cannot be enforced)")}
        return
    if _gate and _sandbox_verdict["state"] == "failed":
        yield {"type": "error", "exc": GrokUnavailableError(
            f"Grok sandbox check failed — refusing to run: {_sandbox_verdict['detail']}")}
        return
    selected_model = model or DEFAULT_GROK_MODEL
    if not _MODEL_RE.match(selected_model):
        yield {"type": "error", "exc": GrokUnavailableError(f"invalid Grok model id {selected_model!r}")}
        return
    selected_effort = effort if effort in GROK_REASONING_LEVELS else None
    if not os.path.isdir(cwd):
        yield {"type": "error", "exc": GrokUnavailableError(f"project directory does not exist: {cwd}")}
        return

    data = data_dir(ctx)
    turn = GrokTurn(session_key)
    acp: _Acp | None = None
    home: Path | None = None
    t_start = time.monotonic()
    try:
        binary = grok_bin()
        if not binary:
            raise GrokUnavailableError("Grok CLI not found (GROK_BIN / PATH / ~/.grok/bin)")
        info = ensure_home(ctx, bin_path=binary)
        home = info["home"]
        facts = read_auth_facts(home)
        problem = _auth_problem(facts, home, ctx) or _account_pin_problem(facts, ctx)
        if problem:
            raise GrokAuthError(problem)
        trust = _trust_store_problem(home)
        if trust:
            raise GrokIsolationError(trust)
        env = child_env(home)
        if not shutil.which("bwrap", path=env.get("PATH")):
            raise GrokUnavailableError("bubblewrap (bwrap) is required for the Grok sandbox and was not found")
        for entry in info["deny"]:
            if not _is_glob(entry) and _is_under(cwd, entry):
                raise GrokUnavailableError(
                    f"project directory {cwd} is inside the sandbox deny list entry {entry}")
        if _is_under(cwd, str(home)):
            raise GrokUnavailableError(f"project directory {cwd} is inside GROK_HOME ({home})")
        # The model's shell WRITES its project dir, so the cockpit's data dir or Grok's home may sit inside it
        # (the cockpit's own checkout, a chat rooted at $HOME) only as REAL directories: MEASURED with the
        # real CLI, the data dir and `.env` are then unreadable, unwritable and cannot be renamed or removed
        # (a mount over a directory), and the CLI pins the home's parents. What the model CAN do in its
        # workspace is re-point a SYMLINK that the cockpit follows — refused.
        for label, where in (("the cockpit data dir", data_dir(ctx)), ("GROK_HOME", home)):
            if _reached_through_workspace_symlink(str(where), cwd):
                raise GrokUnavailableError(
                    f"project directory {cwd} reaches {label} ({where}) through a symlink the model's shell "
                    f"could re-point: replace the symlink with the real directory")
        rules = _instructions(project_name, cwd, multi_agent=multi_agent)
        argv = [binary, "agent", "--no-leader", "stdio"]
        _log(f"spawn {session_key} argv={argv} cwd={cwd} sandbox={SANDBOX_PROFILE} "
             f"deny={len(info['deny'])} resume={'yes' if resume_session_id else 'no'} "
             f"model={selected_model} effort={selected_effort} env_keys={sorted(env)}")
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=STREAM_LIMIT)
        acp = _Acp(proc)
        turn._acp = acp
        if ctx is not None:
            ctx.setdefault("running", {})[session_key] = turn

        # ---- handshake: every step has its own timeout (a missing login HANGS authenticate) ----
        t0 = time.monotonic()
        try:
            init = await acp.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}},
                                     HANDSHAKE_TIMEOUT_SEC)
            _log(f"handshake initialize {1000 * (time.monotonic() - t0):.0f} ms")
            if resume_session_id and "resume" not in ((init.get("agentCapabilities") or {})
                                                      .get("sessionCapabilities") or {}):
                raise GrokUnavailableError("this Grok build cannot resume sessions (no resume capability)")
            t0 = time.monotonic()
            try:
                auth = await acp.request("authenticate", {"methodId": "cached_token"}, HANDSHAKE_TIMEOUT_SEC)
            except (_AcpTimeout, _AcpError) as exc:
                raise GrokAuthError(_AUTH_HINT) from exc
            _log(f"handshake authenticate {1000 * (time.monotonic() - t0):.0f} ms")
            _check_auth_meta(auth.get("_meta"), read_auth_facts(home))
            session_params = {"cwd": cwd, "mcpServers": [], "_meta": {"yoloMode": True, "rules": rules}}
            t0 = time.monotonic()
            if resume_session_id:
                session = await acp.request("session/resume",
                                            {"sessionId": resume_session_id, **session_params},
                                            HANDSHAKE_TIMEOUT_SEC)
                session_id = resume_session_id
            else:
                session = await acp.request("session/new", session_params, HANDSHAKE_TIMEOUT_SEC)
                session_id = session.get("sessionId")
            if not session_id:
                raise GrokProtocolError("session/new returned no sessionId")
            turn.session_id = session_id
            _log(f"handshake session {1000 * (time.monotonic() - t0):.0f} ms id={session_id}")
            await _apply_config(acp, session, session_id, selected_model, selected_effort)
        except _AcpTimeout as exc:
            raise GrokProtocolError(f"Grok did not answer in time: {exc}. {acp.stderr_tail()[-400:]}".strip()) from exc
        except _AcpError as exc:
            _capture_limit_error(data, source="handshake_error", text=json.dumps(exc.error),
                                 session_id=turn.session_id, project_name=project_name,
                                 model=selected_model)
            raise GrokProtocolError(f"Grok rejected the request: {_redact(str(exc))}") from exc
        except _AcpExit as exc:
            if turn.cancel_requested:
                raise _Stopped() from exc
            raise await _exit_error(acp, "during startup") from exc
        if acp.fatal:
            raise acp.fatal
        window = _model_window(session.get("models"), selected_model)
        if window:
            _window_cache[selected_model] = window
        else:
            window = _window_cache.get(selected_model)
        acp.discard_notes()
        if acp.isolation:   # seen during setup: refuse BEFORE the model gets a prompt
            raise GrokIsolationError(acp.isolation)

        # ---- the prompt ----
        mapper = _Mapper(session_id)
        prompt_t0 = time.monotonic()
        try:
            await acp.start_prompt({"sessionId": session_id,
                                    "prompt": [{"type": "text", "text": prompt}]})
        except _AcpExit as exc:
            if turn.cancel_requested:
                raise _Stopped() from exc
            raise await _exit_error(acp, "before the prompt was accepted") from exc
        turn.prompt_started = True
        response: dict | None = None
        while True:
            kind, msg = await acp.queue.get()
            if acp.isolation:   # an MCP server / hook showed up mid-turn: stop, do not yield more
                raise GrokIsolationError(acp.isolation)
            if kind == "note":
                if tap is not None:
                    tap(msg)
                for ev in mapper.feed(msg):
                    yield ev
                continue
            if kind == "response":
                response = msg
                if tap is not None:
                    tap(msg)
            break
        if acp.fatal:
            raise acp.fatal
        if response is None:  # EOF before a prompt response
            if turn.cancel_requested:
                _log(f"{session_key}: process gone after an operator stop — clean end")
            else:
                text = acp.stderr_tail()
                _capture_limit_error(data, source="exit", text=text, session_id=session_id,
                                     project_name=project_name, model=selected_model)
                raise await _exit_error(acp, "before answering the prompt")
            for ev in mapper.finish():
                yield ev
            yield _result_event(session_id, selected_model, int(1000 * (time.monotonic() - prompt_t0)),
                                {"input": 0, "output": 0, "cached": 0, "reasoning": 0, "total": 0,
                                 "notional_usd": 0.0}, 0, window)
            return
        if "error" in response:
            err = response["error"] if isinstance(response["error"], dict) else {"message": str(response["error"])}
            text = f"{err.get('message', err)}" + (f" ({err['data']})" if err.get("data") else "")
            _capture_limit_error(data, source="rpc_error", text=json.dumps(err), session_id=session_id,
                                 project_name=project_name, model=selected_model)
            raise GrokProtocolError(f"Grok failed the turn: {_redact(text)}")
        result = response.get("result") or {}
        stop = result.get("stopReason")
        meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
        _log(f"{session_key}: stopReason={stop!r} cancel_requested={turn.cancel_requested} "
             f"permission_requests={acp.permission_requests}")
        for ev in mapper.finish():
            yield ev
        usage = mapper.usage_from(meta)
        duration_ms = int(1000 * (time.monotonic() - prompt_t0))
        category = acp.cancellation.get(session_id)
        if stop == "cancelled" and not turn.cancel_requested:
            raise GrokProtocolError(
                "Grok cancelled the turn unprompted — most likely an unanswered permission request"
                + (f" (cancellationCategory={category})" if category else ""))
        if stop == "cancelled" and category and category != "MidTurnAbort":
            # our Stop raced a rejected/cancelled permission request: the operator asked for the stop,
            # so the turn still ends cleanly, but the journal keeps the other cause
            _log(f"{session_key}: operator stop, but Grok also reports cancellationCategory={category}")
        if stop not in ("end_turn", "cancelled"):
            raise GrokProtocolError(f"Grok ended the turn with stopReason {stop!r}")
        _append_usage(data, session_id=session_id, model=selected_model, project_name=project_name,
                      session_key=session_key, entrypoint=entrypoint, usage=usage,
                      duration_ms=duration_ms)
        yield _result_event(session_id, selected_model, duration_ms, usage,
                            mapper.context_tokens(meta), window)
    except _Stopped:
        _log(f"{session_key}: stopped before the prompt produced a response — clean end")
        yield _result_event(turn.session_id, selected_model, int(1000 * (time.monotonic() - t_start)),
                            {"input": 0, "output": 0, "cached": 0, "reasoning": 0, "total": 0,
                             "notional_usd": 0.0}, 0, None)
    except GrokAuthError as exc:
        mark_unavailable(str(exc))
        yield {"type": "error", "exc": exc}
    except Exception as exc:
        yield {"type": "error", "exc": exc}
    finally:
        if ctx is not None and ctx.get("running", {}).get(session_key) is turn:
            # The web handler owns final lock removal. Restore its sentinel so a concurrently
            # arriving request still observes the slot as occupied (same as Codex).
            ctx["running"][session_key] = True
        if acp is not None:
            try:
                await acp.teardown(turn.session_id)
            finally:
                # also when teardown re-raises a cancellation: the placeholders must not pile up
                if home is not None:
                    removed = reap_litter(home, acp.proc.pid)
                    _log(f"teardown {session_key}: exit={acp.proc.returncode} litter_removed={len(removed)}")


def _check_auth_meta(meta, facts: dict) -> None:
    """Re-verify, from what the AGENT reports, the two facts we checked in auth.json."""
    if not isinstance(meta, dict):
        _log("warn: authenticate returned no _meta; relying on the auth.json check")
        return
    _last_auth_meta.update({k: meta.get(k) for k in ("subscription_tier", "auth_mode") if k in meta})
    mode = meta.get("auth_mode")
    if isinstance(mode, str) and mode.lower() != "oidc":
        raise GrokAuthError(f"Grok reports auth_mode={mode!r}; only the grok.com subscription login is allowed")
    if meta.get("backend_billed") is True:
        raise GrokAuthError("Grok reports this session is API-billed; Cardloop runs subscription-only")
    if "coding_data_retention_opt_out" in meta and meta["coding_data_retention_opt_out"] is not True:
        raise GrokAuthError("Grok reports coding_data_retention_opt_out is not true — refusing to send code")
    email, want = meta.get("email"), facts.get("email")
    if isinstance(email, str) and isinstance(want, str) and email.lower() != want.lower():
        raise GrokAuthError("the Grok agent is signed in as a different account than this home's login")


async def _apply_config(acp: _Acp, session: dict, session_id: str, model: str, effort: str | None) -> None:
    current = {o.get("id"): o.get("currentValue") for o in (session.get("configOptions") or [])
               if isinstance(o, dict)}
    wanted = [("model", model)]
    if effort:
        wanted.append(("reasoning_effort", effort))
    for config_id, value in wanted:
        if current.get(config_id) == value:
            continue
        try:
            await acp.request("session/set_config_option",
                              {"sessionId": session_id, "configId": config_id, "value": value},
                              HANDSHAKE_TIMEOUT_SEC)
        except _AcpError as exc:
            raise GrokUnavailableError(f"Grok rejected {config_id} {value!r}: {exc}") from exc


async def _exit_error(acp: _Acp, when: str) -> Exception:
    """The error for a Grok process that went away: exit code + the tail of its stderr.

    stdout closing and the process being reaped / stderr being drained are three separate events;
    wait (bounded) for the last two so the message carries the code and the final stderr line."""
    for waiter in (acp.proc.wait(), asyncio.wait({acp._err_task}, timeout=1.0)):
        try:
            await asyncio.wait_for(waiter, 1.0)
        except Exception:
            pass
    code = acp.proc.returncode
    tail = acp.stderr_tail()
    tail_txt = f": {tail[-1500:]}" if tail else ""
    if re.search(r"log ?in|sign ?in|unauthori[sz]ed|not authenticated|token expired", tail, re.I):
        return GrokAuthError(f"{_AUTH_HINT}{tail_txt}")
    return GrokProtocolError(f"Grok exited {when} (code {code}){tail_txt}")


def _result_event(session_id: "str | None", model: str, duration_ms: int, usage: dict, ctx_tokens: int,
                  window: int | None) -> dict:
    return {
        "type": "result", "provider_session_id": session_id, "thread_id": None, "session_id": None,
        "model": model, "duration_ms": duration_ms, "context_tokens": ctx_tokens,
        "context_window": window,
        "usage": {
            "input_tokens": usage["input"], "output_tokens": usage["output"],
            "cached_input_tokens": usage["cached"], "reasoning_output_tokens": usage["reasoning"],
            "total_tokens": usage["total"], "context_window": window,
            "notional_usd": usage["notional_usd"],
        },
    }

"""runtime_secrets.py — keep the cockpit's own secrets out of every child process (spec-096 P3b).

The cockpit is started with its `.env` loaded into the process environment (systemd
`EnvironmentFile=`, or `bot.py`'s own loader). Left there, every secret reaches
  * every child the cockpit spawns (the Claude CLI via the SDK, which merges `os.environ`; Codex;
    terminals; test runners) — a model-run `printenv` puts WEB_PASSWORD into the transcript, and
  * `/proc/<cockpit pid>/environ`, readable by any process of the same uid.

Two mechanisms, both applied once at start (`bot.py`, when it runs as the program):

1. `harden_process()` — `prctl(PR_SET_DUMPABLE, 0)`. `/proc/<pid>/{environ,mem,maps,fd,cwd,...}`
   become root-owned and a same-uid process can no longer ptrace the cockpit. A child becomes
   dumpable again on exec (the kernel resets the flag for the new image), so agents stay normal.
2. `scrub()` — move secret-looking variables out of `os.environ` into a private in-process
   snapshot. In-process readers call `get()`, which answers from the snapshot (or from
   `os.environ` when a caller or a test set the variable itself).

What this is NOT: a boundary against an agent that runs as the same user and can open `.env` on
disk. It closes the environment/procfs channels, which is where secrets leaked accidentally.

Stdlib only; no import of any other cockpit module (it is imported first, before `.env` is read).
"""

from __future__ import annotations

import fnmatch
import os
import sys

# Variables that are always secrets, whatever they are called (not every one ends in a suffix
# the patterns below catch: AZURE_FOUNDRY_KEY and CLAUDE_OPS_SECRET_KEY end in `_KEY`).
EXPLICIT_NAMES = (
    "WEB_PASSWORD", "WEB_COOKIE_SALT", "BOT_TOKEN", "COOLIFY_API_TOKEN", "AZURE_FOUNDRY_KEY",
    "N8N_API_KEY", "TWOCAPTCHA_API_KEY", "OLLAMA_AUTH_TOKEN", "CLAUDE_OPS_SECRET_KEY",
    "JOURNAL_TG_BOT_TOKEN",
)
# Anything shaped like a credential. Matched against the upper-cased name; the pattern is a
# SUFFIX, so `SECOND_OPINION_AZURE_MAX_TOKENS` and `AUTOPILOT_DAILY_TOKEN_CAP` are not secrets.
PATTERNS = ("*_PASSWORD", "*_SALT", "*_TOKEN", "*_SECRET", "*_API_KEY")

# Never scrubbed, whatever they match:
#  * ANTHROPIC_* — bot.py already handles these on its own terms (subscription mode pops them,
#    api_key mode passes ANTHROPIC_API_KEY to the SDK on purpose).
#  * CLAUDE_CODE_OAUTH_TOKEN — the Claude CLI child's OWN credential (a headless host without a
#    credentials file authenticates with it); a child that loses it cannot run at all.
KEEP_PREFIXES = ("ANTHROPIC_",)
KEEP_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN",)

# Operator opt-out: comma-separated names that children are SUPPOSED to see (a GITHUB_TOKEN for
# `gh`, an HF_TOKEN, ...). Read from the environment the scrub runs on.
PASSTHROUGH_VAR = "AGENT_ENV_PASSTHROUGH"

_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3

_snapshot: dict[str, str] = {}


def passthrough_names(environ: "dict[str, str] | None" = None) -> "frozenset[str]":
    """The names the operator listed in AGENT_ENV_PASSTHROUGH (exact, case-insensitive)."""
    env = os.environ if environ is None else environ
    raw = env.get(PASSTHROUGH_VAR) or ""
    return frozenset(p.strip().upper() for p in raw.split(",") if p.strip())


def is_secret_name(name: str, passthrough: "frozenset[str] | None" = None) -> bool:
    """True when `name` is a secret that children must not inherit."""
    up = name.upper()
    if up == PASSTHROUGH_VAR or up in KEEP_NAMES or up.startswith(KEEP_PREFIXES):
        return False
    if passthrough is not None and up in passthrough:
        return False
    return up in EXPLICIT_NAMES or any(fnmatch.fnmatchcase(up, p) for p in PATTERNS)


def scrub(environ: "dict[str, str] | None" = None) -> "list[str]":
    """Move every secret out of `environ` (default: `os.environ`) into the snapshot.

    Returns the NAMES that were removed, sorted — values never leave this module. Idempotent: a
    second call finds nothing new and keeps the snapshot. Removing a key from `os.environ`
    also unsets it in the C environment (os.unsetenv), which is what every spawned child copies.
    """
    env = os.environ if environ is None else environ
    keep = passthrough_names(env)
    moved = []
    for name in list(env):
        if is_secret_name(name, keep):
            _snapshot[name] = env[name]
            del env[name]
            moved.append(name)
    return sorted(moved)


def get(name: str, default: str = "") -> str:
    """The value of a secret-bearing variable for an in-process reader.

    A value present in `os.environ` wins (a test, or a caller that set it on purpose); otherwise
    the startup snapshot answers. With no scrub ever run this is plain `os.environ.get`.
    """
    live = os.environ.get(name)
    if live is not None:
        return live
    return _snapshot.get(name, default)


def snapshot_names() -> "list[str]":
    """Names currently held in the snapshot (for diagnostics; never the values)."""
    return sorted(_snapshot)


def secret_values(min_len: int = 1) -> "list[str]":
    """The snapshot's values, longest first — for redacting log lines, nothing else."""
    return sorted({v for v in _snapshot.values() if len(v) >= min_len}, key=len, reverse=True)


def reset_for_tests() -> None:
    """Forget the snapshot (the module is process-global; tests must not leak it)."""
    _snapshot.clear()


def harden_process(force: bool = False) -> bool:
    """`prctl(PR_SET_DUMPABLE, 0)` on this process (Linux). True when it is applied.

    Skipped inside a pytest process unless `force`: a non-dumpable test runner would break the
    tests that read their own /proc entries. Any failure prints ONE warning and returns False —
    the cockpit still starts, just without the procfs hardening.
    """
    if not sys.platform.startswith("linux"):
        return False
    if not force and "pytest" in sys.modules:
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        if libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError("PR_GET_DUMPABLE still reports dumpable after PR_SET_DUMPABLE 0")
    except Exception as exc:  # noqa: BLE001 — hardening must never stop the cockpit starting
        print(f"[security] WARNING: could not make the process non-dumpable ({exc!r}); "
              f"/proc/{os.getpid()}/environ stays readable to same-user processes", flush=True)
        return False
    return True

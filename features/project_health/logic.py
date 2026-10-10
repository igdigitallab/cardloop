"""features/project_health/logic.py — pure checks for "is this project quietly unwell?".

Each check reads a project directory (plus a few injected locations) and returns zero or
more findings::

    {id, severity: "warn"|"crit", title, detail, fix_hint, subject, ...}

Design rules, learned the hard way (June 2026: an "X/8 health score" badge went red on 92 %
of projects with self-referential, software-biased checks and was removed as noise):

* NO score.  A finding exists only for a real, actionable risk and carries a one-line fix.
  A healthy project produces an empty list — silence is the success state.
* Software-only checks are never applied to content/scratchpad projects.
* Every input is injectable (paths, "now", the board reader, the test-command detector), so a
  check is tested against a tmp dir without a running cockpit.  This module must not import
  webapp: the cockpit builds the `Env` (see loop.build_env) and hands it in.
* Read-only.  Nothing here writes to a project; the only writer is `ack_add`, which stores a
  hash in the cockpit's own data dir.

A "pack" can contribute its own check with `register_check("my_id")`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import board
import context_pack
from token_estimate import approx_tokens

# ── Env-configured knobs (read once at import) ───────────────────────────────


def _env_num(name: str, default: float, cast=float):
    try:
        return cast(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return cast(default)


# HEALTH_CHECK_MODE: off | on.  `off` stops the daily sweep; the on-demand check still works.
MODE: str = (os.environ.get("HEALTH_CHECK_MODE", "on") or "on").strip().lower()
MODES: tuple[str, ...] = ("off", "on")
# Seconds between fleet sweeps.
INTERVAL_SEC: int = _env_num("HEALTH_CHECK_INTERVAL_SEC", 86400, int)
# `context_floor` warns above this many approximate tokens loaded before any work.
CONTEXT_FLOOR_WARN_TOKENS: int = _env_num("HEALTH_CONTEXT_FLOOR_WARN_TOKENS", 30000, int)
# `stale_work` warns when uncommitted/unpushed work has been sitting this long untouched.
STALE_WORK_DAYS: float = _env_num("HEALTH_STALE_WORK_DAYS", 2.0, float)

# One project must finish within this many seconds; checks left over are skipped, not failed.
CHECK_BUDGET_SEC = 3.0
# Files larger than this are skipped (a multi-MB "CLAUDE.md" is not worth reading on a timer).
MAX_FILE_BYTES = 2 * 1024 * 1024

# ── Constants with a story ───────────────────────────────────────────────────

# The Claude Code CLI loads a native MEMORY.md index verbatim, but silently drops everything
# past 200 lines or 25,000 bytes (whichever comes first) — the newest entries, because notes
# are appended at the end.  tools/memory-lint.py applies the same budget to both memory
# locations, so the curated index is judged by it too.
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_BYTES = 25_000
MEMORY_WARN_FRACTION = 0.80
MEMORY_CRIT_FRACTION = 0.95

# The curated index reaches the prompt only through the context pack, which clips it to this
# many characters (context_pack._SECTION_CAP) — so that is all the floor counts of it.
CURATED_INDEX_PACK_CHARS: int = context_pack._SECTION_CAP.get("memory_index", 1800)

# A worktree this young is left alone: the card may be mid-move between columns.
ORPHAN_WORKTREE_GRACE_SEC = 3600

SOFTWARE_ARCHETYPES = ("software", "ops")
BOARD_ONLY_FILES = ("TASKS.md", "DONE.md")  # cockpit-written board state, not "work"
# Cockpit/agent runtime state that shows up as untracked in a repo: never "work sitting unsaved".
COCKPIT_STATE_PREFIXES = (".worktrees/", ".claude/worktrees/", ".claude-ops/scan/", ".claude-ops/secrets/")

ACK_FILE_NAME = "project_health_ack.json"
ACKABLE_CHECKS = ("project_settings_untrusted",)
_ACK_KEEP_PER_PROJECT = 30

SEVERITIES = ("crit", "warn")  # display order


# ── Inputs ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Project:
    """The slice of a project record the checks read."""
    id: str
    name: str
    cwd: str
    archetype: Optional[str] = None   # the raw `type` setting; None = never set
    test_cmd: str = ""
    git_enabled: bool = True
    memory_mode: str = "auto"         # agents_config.memory: "project" disables native memory

    @property
    def software_like(self) -> bool:
        # Unset type is treated as software — same default as api_project_health.  Checks that
        # would be wrong for an untyped content folder also require something only code has
        # (a detectable test command, a git repo, a .env).
        return (self.archetype or "software") in SOFTWARE_ARCHETYPES

    @classmethod
    def from_record(cls, p: dict) -> "Project":
        ac = p.get("agents_config") if isinstance(p.get("agents_config"), dict) else {}
        return cls(
            id=str(p.get("id") or ""),
            name=str(p.get("name") or p.get("id") or ""),
            cwd=str(p.get("cwd") or ""),
            archetype=p.get("type") or None,
            test_cmd=str(p.get("test_cmd") or ""),
            git_enabled=p.get("git_enabled", True) is not False,
            memory_mode=str(ac.get("memory") or "auto"),
        )


@dataclass
class Env:
    """Everything a check may look at besides the project dir.  All injectable."""
    native_memory_dir: Callable[[str], Path]
    curated_memory_dir: Callable[[str], Path]
    global_claude_md: Optional[Path] = None
    # cwd -> (argv, human) | None; the cockpit passes webapp._detect_test_cmd.
    detect_test_cmd: Optional[Callable[[str], object]] = None
    # {project_id: {check_id: [acknowledged sha256, ...]}}
    acks: dict = field(default_factory=dict)
    now: float = field(default_factory=time.time)
    context_floor_warn_tokens: int = CONTEXT_FLOOR_WARN_TOKENS
    stale_work_days: float = STALE_WORK_DAYS
    # time.monotonic() value after which remaining work is skipped (set by run_checks).
    deadline: Optional[float] = None

    def remaining(self, cap: float = CHECK_BUDGET_SEC) -> float:
        if self.deadline is None:
            return cap
        return max(0.0, min(cap, self.deadline - time.monotonic()))


def finding(check_id: str, severity: str, title: str, detail: str, fix_hint: str,
            subject: str = "", **extra) -> dict:
    """The one finding shape.  `subject` tells apart several findings of one check."""
    return {"id": check_id, "severity": severity, "title": title, "detail": detail,
            "fix_hint": fix_hint, "subject": subject, **extra}


def finding_key(project_id: str, f: dict) -> str:
    """Stable identity of a finding across sweeps (severity changes keep the key)."""
    return f"{project_id}:{f.get('id', '')}:{f.get('subject', '')}"


# ── Registry ─────────────────────────────────────────────────────────────────

CheckFn = Callable[[Project, Env], list]
_CHECKS: list = []   # [(check_id, fn)] in run order


def register_check(check_id: str) -> Callable[[CheckFn], CheckFn]:
    """Decorator: add a check to the registry.  A later registration of the same id replaces it."""
    def deco(fn: CheckFn) -> CheckFn:
        _CHECKS[:] = [(i, f) for i, f in _CHECKS if i != check_id]
        _CHECKS.append((check_id, fn))
        return fn
    return deco


def check_ids() -> list:
    return [i for i, _ in _CHECKS]


def run_checks(project: Project, env: Env, budget_sec: float = CHECK_BUDGET_SEC) -> dict:
    """Run every registered check on one project.  Never raises.

    A check that throws is reported in `errors` (so a bug is visible in the API) but never
    becomes a finding: a health check that cries wolf about itself is exactly the noise this
    feature must not make.  Checks left when the budget runs out land in `skipped`.
    """
    t0 = time.monotonic()
    env.deadline = t0 + budget_sec
    findings: list = []
    errors: list = []
    skipped: list = []
    if project.cwd and Path(project.cwd).is_dir():
        for cid, fn in list(_CHECKS):
            if time.monotonic() >= env.deadline:
                skipped.append(cid)
                continue
            try:
                findings.extend(fn(project, env) or [])
            except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
                errors.append(f"{cid}: {type(exc).__name__}: {exc}")
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else 9,
                                 f["id"], f.get("subject", "")))
    return {
        "project_id": project.id, "name": project.name, "findings": findings,
        "checked_at": int(env.now), "took_ms": int((time.monotonic() - t0) * 1000),
        "errors": errors, "skipped": skipped,
    }


# ── Small readers ────────────────────────────────────────────────────────────


def _stat_size(path: Path) -> Optional[int]:
    """Size of a regular file, or None when it is missing/not a file."""
    try:
        if not path.is_file():
            return None
        return path.stat().st_size
    except OSError:
        return None


def _read_text(path: Path) -> Optional[str]:
    """UTF-8 text of a regular file; None if missing, unreadable or over MAX_FILE_BYTES."""
    size = _stat_size(path)
    if size is None or size > MAX_FILE_BYTES:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_bytes(path: Path) -> Optional[bytes]:
    size = _stat_size(path)
    if size is None or size > MAX_FILE_BYTES:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _count_lines(text: str) -> int:
    return len(text.splitlines())


def _fmt_k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k"


# ── a. memory_index_near_cap ─────────────────────────────────────────────────


@register_check("memory_index_near_cap")
def check_memory_index_near_cap(project: Project, env: Env) -> list:
    targets = []
    if project.memory_mode != "project":   # native memory is switched off for that mode
        targets.append(("native", env.native_memory_dir(project.cwd)))
    targets.append(("curated", env.curated_memory_dir(project.cwd)))
    out = []
    for kind, mem_dir in targets:
        index = Path(mem_dir) / "MEMORY.md"
        size = _stat_size(index)
        if size is None:
            continue
        text = _read_text(index)
        lines = _count_lines(text) if text is not None else None
        frac = max(size / MEMORY_INDEX_MAX_BYTES,
                   (lines / MEMORY_INDEX_MAX_LINES) if lines is not None else 0.0)
        if frac < MEMORY_WARN_FRACTION:
            continue
        sev = "crit" if frac >= MEMORY_CRIT_FRACTION else "warn"
        used = f"{size:,} bytes" + (f" / {lines} lines" if lines is not None else "")
        if kind == "native":
            why = "Claude Code silently drops index entries past the cap - the newest notes stop loading."
        else:
            why = ("Only the head of the curated index reaches the agent (the context pack clips it), "
                   "and tools/memory-lint.py holds it to the same budget.")
        out.append(finding(
            "memory_index_near_cap", sev,
            "Memory index at its cap" if sev == "crit" else "Memory index near its cap",
            f"{kind} MEMORY.md is {used} = {int(frac * 100)}% of the "
            f"{MEMORY_INDEX_MAX_LINES}-line / {MEMORY_INDEX_MAX_BYTES:,}-byte cap. {why}",
            f"Merge related entries into one-line pointers (~100 chars) and move detail into articles; "
            f"`tools/memory-lint.py --dir {mem_dir}`.",
            subject=kind,
        ))
    return out


# ── b. context_floor ─────────────────────────────────────────────────────────


def _clip_native_index(text: str) -> str:
    """The part of a native MEMORY.md the CLI actually loads (first 200 lines / 25,000 bytes)."""
    lines = text.splitlines(keepends=True)[:MEMORY_INDEX_MAX_LINES]
    clipped = "".join(lines)
    raw = clipped.encode("utf-8")
    if len(raw) > MEMORY_INDEX_MAX_BYTES:
        clipped = raw[:MEMORY_INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
    return clipped


@register_check("context_floor")
def check_context_floor(project: Project, env: Env) -> list:
    parts: list = []   # (label, chars)
    seen: set = set()

    def add(label: str, path: Optional[Path], clip: Optional[Callable[[str], str]] = None) -> None:
        if path is None:
            return
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:        # the project IS the home dir: count its CLAUDE.md once
            return
        text = _read_text(path)
        if text is None:
            return
        seen.add(key)
        parts.append((label, len(clip(text) if clip else text)))

    add("global CLAUDE.md", env.global_claude_md)
    add("CLAUDE.md", Path(project.cwd) / "CLAUDE.md")
    if project.memory_mode != "project":
        add("native MEMORY.md", Path(env.native_memory_dir(project.cwd)) / "MEMORY.md", _clip_native_index)
    add("curated MEMORY.md", Path(env.curated_memory_dir(project.cwd)) / "MEMORY.md",
        lambda t: t[:CURATED_INDEX_PACK_CHARS])

    total = sum(approx_tokens(c) for _, c in parts)
    if total <= env.context_floor_warn_tokens:
        return []
    breakdown = ", ".join(f"{label} {_fmt_k(approx_tokens(c))}" for label, c in
                          sorted(parts, key=lambda p: -p[1]))
    biggest = max(parts, key=lambda p: p[1])[0]
    if biggest == "global CLAUDE.md":
        hint = ("The global CLAUDE.md is the biggest part and every project pays it - "
                "route detail out of it into files read on demand.")
    elif biggest.endswith("MEMORY.md"):
        hint = "Trim the memory index to one-line pointers; detail belongs in articles read on demand."
    else:
        hint = ("Move reference material out of CLAUDE.md into docs the agent opens on demand; "
                "keep CLAUDE.md to rules and routes.")
    return [finding(
        "context_floor", "warn", "Heavy context before any work",
        f"About {_fmt_k(total)} tokens are loaded before the first message "
        f"(warn above {_fmt_k(env.context_floor_warn_tokens)}): {breakdown}. "
        f"Estimate at ~4 chars/token.",
        hint,
    )]


# ── c. no_test_cmd ───────────────────────────────────────────────────────────


@register_check("no_test_cmd")
def check_no_test_cmd(project: Project, env: Env) -> list:
    if not project.software_like or project.test_cmd.strip() or env.detect_test_cmd is None:
        return []
    # Only when a test command exists to be set: "write a test suite" is not a one-line fix,
    # and ~half the fleet has no suite at all - flagging those would be pure noise.
    detected = env.detect_test_cmd(project.cwd)
    if not detected:
        return []
    human = detected[1] if isinstance(detected, (tuple, list)) and len(detected) > 1 else str(detected)
    return [finding(
        "no_test_cmd", "warn", "No test command configured",
        f"Tests exist (`{human}`) but the test_cmd setting is empty, so the board janitor can "
        f"never auto-accept this project's cards.",
        f"Set test_cmd to `{human}` in project settings.",
    )]


# ── d. stale_work ────────────────────────────────────────────────────────────

_GIT_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL")


def _git(cwd: str, args: list, env: Env) -> Optional[str]:
    """Run a read-only git command; None on any failure or timeout.

    GIT_OPTIONAL_LOCKS=0 keeps `git status` from taking index.lock (it would otherwise race the
    operator's own commit); core.fsmonitor=false stops a repo-local config from running a program.
    """
    budget = env.remaining(2.0)
    if budget <= 0.05:
        return None
    genv = {k: v for k, v in os.environ.items() if k in _GIT_ENV_KEEP}
    genv.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        cp = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", cwd, *args],
                            capture_output=True, timeout=budget, env=genv)
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.decode("utf-8", errors="replace")


def _dirty_entries(cwd: str, env: Env) -> Optional[list]:
    """[(status code, path)] from `git status`; None when git failed.

    Untracked files are listed one by one (`all`): the collapsed form names a whole directory,
    so "which file is newest" and "is this just cockpit state" would both be guesses.  Cockpit
    runtime state is excluded by pathspec before git ever lists it.
    """
    excludes = [f":(exclude){p.rstrip('/')}" for p in COCKPIT_STATE_PREFIXES]
    out = _git(cwd, ["status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all",
                     "--", ".", *excludes], env)
    if out is None:
        return None
    return [(e[:2], e[3:]) for e in out.split("\0") if len(e) > 3]


def _age_text(seconds: float) -> str:
    days = seconds / 86400
    return f"{days:.0f}d" if days >= 1 else f"{seconds / 3600:.0f}h"


@register_check("stale_work")
def check_stale_work(project: Project, env: Env) -> list:
    if not project.software_like or not project.git_enabled:
        return []
    cwd = project.cwd
    if not (Path(cwd) / ".git").exists():
        return []
    limit = env.stale_work_days * 86400
    parts: list = []

    entries = _dirty_entries(cwd, env)
    real = [(code, rel) for code, rel in (entries or []) if rel not in BOARD_ONLY_FILES]
    newest = 0.0
    for _, rel in real:
        try:
            newest = max(newest, os.lstat(os.path.join(cwd, rel.rstrip("/"))).st_mtime)
        except OSError:
            continue   # deleted: no mtime to judge by
    if real and newest and env.now - newest > limit:
        untracked = sum(1 for code, _ in real if code == "??")
        kinds = ", ".join(x for x in (f"{len(real) - untracked} modified" if len(real) > untracked else "",
                                      f"{untracked} untracked" if untracked else "") if x)
        names = ", ".join(rel for _, rel in real[:3]) + (", ..." if len(real) > 3 else "")
        parts.append(f"{len(real)} uncommitted file(s) ({kinds}: {names}), the newest change "
                     f"{_age_text(env.now - newest)} ago")

    upstream = _git(cwd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], env)
    if upstream and upstream.strip():
        stamps = _git(cwd, ["log", "@{u}..HEAD", "--format=%ct"], env)
        times = [int(s) for s in (stamps or "").split() if s.isdigit()]
        if times and env.now - min(times) > limit:
            parts.append(f"{len(times)} unpushed commit(s), the oldest {_age_text(env.now - min(times))} old")

    if not parts:
        return []
    return [finding(
        "stale_work", "warn", "Work sitting unsaved in git",
        "; ".join(parts) + f". Nothing has moved for more than {env.stale_work_days:g} days.",
        "Commit and push it (the Sync button), or discard what you no longer need.",
    )]


# ── e. orphan_worktrees ──────────────────────────────────────────────────────


def board_active_card_ids(cwd: str) -> Optional[set]:
    """Ids of cards in In Progress or Review.  None when the board cannot be read."""
    try:
        _, _, cols = board._load_board(cwd)
    except Exception:  # noqa: BLE001 - an unreadable board means "unknown", never "orphan"
        return None
    ids = set()
    for col in ("in_progress", "review"):
        for c in cols.get(col) or []:
            if c.get("id"):
                ids.add(str(c["id"]))
    return ids


@register_check("orphan_worktrees")
def check_orphan_worktrees(project: Project, env: Env) -> list:
    root = Path(project.cwd) / ".worktrees"
    try:
        found = sorted(p for p in root.glob("card-*") if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    if not found:
        return []
    active = board_active_card_ids(project.cwd)
    if active is None:
        return []
    orphans = []
    for p in found:
        card_id = p.name[len("card-"):]
        if card_id in active:
            continue
        try:
            if env.now - p.stat().st_mtime < ORPHAN_WORKTREE_GRACE_SEC:
                continue
        except OSError:
            continue
        orphans.append(p.name)
    if not orphans:
        return []
    shown = ", ".join(orphans[:5]) + (f" (+{len(orphans) - 5} more)" if len(orphans) > 5 else "")
    return [finding(
        "orphan_worktrees", "warn", "Leftover card worktrees",
        f"{len(orphans)} worktree(s) under .worktrees/ belong to cards that are neither In Progress "
        f"nor in Review: {shown}.",
        "If nothing there is needed: `git worktree remove --force .worktrees/<name>` and "
        "`git branch -D <name>`.",
    )]


# ── f. env_exposed ───────────────────────────────────────────────────────────

ENV_EXPOSED_HINT = ("A .env file exists but is not covered by .gitignore — "
                    "secrets may be committed to git.")


def env_exposed(cwd: "str | Path", git_enabled: bool = True) -> bool:
    """True when a git project has a .env* file its .gitignore does not mention.

    Moved verbatim from api_project_health so the header pill and the health check can never
    disagree.  Caller decides whether the project is software/ops.
    """
    cwd = Path(cwd)
    git_repo = bool(git_enabled) and (cwd / ".git").exists()
    env_present = False
    try:
        env_present = any(
            f.is_file() and (f.name == ".env" or f.name.startswith(".env."))
            for f in cwd.iterdir()
        )
    except OSError:
        pass
    gitignore_covers = False
    try:
        gi = cwd / ".gitignore"
        if gi.is_file():
            gitignore_covers = ".env" in gi.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    return git_repo and env_present and not gitignore_covers


@register_check("env_exposed")
def check_env_exposed(project: Project, env: Env) -> list:
    if not project.software_like or not env_exposed(project.cwd, project.git_enabled):
        return []
    return [finding("env_exposed", "crit", ".env exposed", ENV_EXPOSED_HINT,
                    "Add .env to .gitignore so secrets are never committed.")]


# ── g. project_settings_untrusted ────────────────────────────────────────────

# engine.py passes setting_sources ["user", "project", "local"], so these files are honoured in
# headless sessions with no trust prompt: a repo that ships one runs its hooks on this host.
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json")


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def settings_flags(data: object) -> tuple:
    """(flags, is_crit) for one parsed settings file.  Names only - never values."""
    if not isinstance(data, dict):
        return [], False
    flags: list = []
    crit = False
    hooks = data.get("hooks")
    if hooks:
        events = sorted(str(k) for k in hooks) if isinstance(hooks, dict) else []
        flags.append("hooks" + (f" ({', '.join(events[:6])})" if events else ""))
        crit = True
    envmap = data.get("env")
    if isinstance(envmap, dict):
        bad = sorted(str(k) for k in envmap if str(k).startswith("ANTHROPIC_"))
        if bad:
            flags.append("env " + ", ".join(bad))   # key names only
            crit = True
    if data.get("enableAllProjectMcpServers") is True:
        flags.append("enableAllProjectMcpServers")
    perms = data.get("permissions")
    allow = perms.get("allow") if isinstance(perms, dict) else None
    if isinstance(allow, list) and any(a in ("Bash", "Bash(*)") for a in allow if isinstance(a, str)):
        flags.append("permissions.allow Bash")
    return flags, crit


def settings_hashes(cwd: str) -> dict:
    """{relative path: sha256} of every existing settings file (acknowledge-flow validation)."""
    out = {}
    for rel in SETTINGS_FILES:
        raw = _read_bytes(Path(cwd) / rel)
        if raw is not None:
            out[rel] = sha256_hex(raw)
    return out


@register_check("project_settings_untrusted")
def check_project_settings_untrusted(project: Project, env: Env) -> list:
    acked = set(((env.acks or {}).get(project.id) or {}).get("project_settings_untrusted") or [])
    out = []
    for rel in SETTINGS_FILES:
        raw = _read_bytes(Path(project.cwd) / rel)
        if raw is None:
            continue
        digest = sha256_hex(raw)
        if digest in acked:
            continue
        try:
            flags, crit = settings_flags(json.loads(raw.decode("utf-8", errors="replace")))
        except ValueError:
            continue   # not JSON: the CLI ignores it too
        if not flags:
            continue
        out.append(finding(
            "project_settings_untrusted", "crit" if crit else "warn",
            "Project settings run code without a prompt",
            f"{rel}: {'; '.join(flags)}. Headless sessions honour project settings with no trust prompt.",
            "Open the file and remove what you did not write; if it is intended, acknowledge it "
            "(this stays quiet until the file changes).",
            subject=rel, ack_sha256=digest, ackable=True,
        ))
    return out


# ── h. invisible_unicode ─────────────────────────────────────────────────────

MAX_INVISIBLE_LOCATIONS = 10

# Zero-width / bidi / filler code points that hide text from a human reviewer but not from the
# model (list: ECC check-unicode-safety.js, plus the bidi isolates and Tag block it also flags).
# Variation selectors (U+FE0F is in every emoji) are deliberately NOT listed.
_INVISIBLE_RE = re.compile(
    "[\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2061-\u2064\u2066-\u2069"
    "\u115f\u1160\u3164\u180e\U000e0000-\U000e007f]"
)
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
            or 0x2B00 <= cp <= 0x2BFF or 0x2300 <= cp <= 0x23FF or cp in (0x00A9, 0x00AE))


def _is_benign_invisible(text: str, i: int) -> bool:
    """The measured legitimate uses of an invisible character at text[i]."""
    ch = text[i]
    prev = text[i - 1] if i > 0 else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    cp = ord(ch)
    if cp == 0x200D:   # ZWJ glues emoji into one glyph (family, profession, heart-on-fire)
        j = i - 1
        while j >= 0 and text[j] == "\ufe0f":
            j -= 1
        return j >= 0 and _is_emoji(text[j]) and bool(nxt) and _is_emoji(nxt)
    if cp == 0x200B:
        if text.startswith("```", i + 1):      # agents escape a fence inside a fence this way
            return True
        return (prev, nxt) in (("/", "*"), ("*", "/"))   # breaks a `*/` or `/*` inside inline code
    if cp == 0xFEFF:
        return i == 0                           # a leading BOM from a Windows editor
    if 0xE0000 <= cp <= 0xE007F:                # Tag block: only inside a subdivision-flag emoji
        j = i
        while j > 0 and 0xE0000 <= ord(text[j - 1]) <= 0xE007F:
            j -= 1
        return j > 0 and text[j - 1] == "\U0001F3F4"
    return False


def scan_invisible(text: str) -> list:
    """[(line, code point, is_bidi_or_tag)] for every non-benign hit, in order."""
    hits = []
    for m in _INVISIBLE_RE.finditer(text):
        if _is_benign_invisible(text, m.start()):
            continue
        cp = ord(m.group())
        hits.append((text.count("\n", 0, m.start()) + 1, cp,
                     cp in _BIDI or 0xE0000 <= cp <= 0xE007F))
        if len(hits) >= 200:
            break
    return hits


def _instruction_files(project: Project, env: Env) -> list:
    """[(label, path)] of every file the agent reads as instructions."""
    cwd = Path(project.cwd)
    files = [("CLAUDE.md", cwd / "CLAUDE.md")]
    dirs = [(".claude-ops/memory", env.curated_memory_dir(project.cwd)),
            (".claude-ops/roles", cwd / ".claude-ops" / "roles")]
    if project.memory_mode != "project":
        dirs.append(("native memory", env.native_memory_dir(project.cwd)))
    for label, d in dirs:
        try:
            for p in sorted(Path(d).glob("*.md")):
                files.append((f"{label}/{p.name}", p))
        except OSError:
            continue
    return files


@register_check("invisible_unicode")
def check_invisible_unicode(project: Project, env: Env) -> list:
    locations: list = []
    total = 0
    severe = False
    for label, path in _instruction_files(project, env):
        if env.remaining() <= 0:
            break
        text = _read_text(path)
        if not text:
            continue
        seen_lines: set = set()
        for line, cp, bad in scan_invisible(text):
            if bad:
                severe = True
            if line in seen_lines:
                continue
            seen_lines.add(line)
            total += 1
            if len(locations) < MAX_INVISIBLE_LOCATIONS:
                locations.append(f"{label}:{line} (U+{cp:04X})")
    if not total:
        return []
    more = f" (+{total - len(locations)} more)" if total > len(locations) else ""
    return [finding(
        "invisible_unicode", "crit" if severe else "warn",
        "Invisible characters in agent instructions",
        f"{total} line(s) with hidden characters: {', '.join(locations)}{more}.",
        "Retype or strip those lines: hidden characters can carry text the agent reads and you cannot see.",
    )]


# ── acknowledge store (the only writer) ──────────────────────────────────────


def load_acks(data_dir: "str | Path") -> dict:
    try:
        raw = json.loads((Path(data_dir) / ACK_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def ack_add(data_dir: "str | Path", project_id: str, check_id: str, digest: str) -> dict:
    """Record an acknowledged hash (atomic write).  Returns the updated store."""
    from fsutil import atomic_write
    acks = load_acks(data_dir)
    mine = acks.setdefault(project_id, {})
    seen = [h for h in (mine.get(check_id) or []) if h != digest]
    seen.append(digest)
    mine[check_id] = seen[-_ACK_KEEP_PER_PROJECT:]
    atomic_write(Path(data_dir) / ACK_FILE_NAME, json.dumps(acks, indent=2), mode=0o600)
    return acks


# ── fleet digest ─────────────────────────────────────────────────────────────


def build_digest(results: list, now: Optional[float] = None) -> str:
    """One document for the whole fleet: only projects with findings, worst first."""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now if now is not None else time.time()))
    lines = [f"# Project health - {stamp}", ""]
    sick = [r for r in results if r.get("findings")]
    if not sick:
        lines.append("Every project is healthy.")
        return "\n".join(lines) + "\n"
    sick.sort(key=lambda r: (-sum(1 for f in r["findings"] if f["severity"] == "crit"),
                             -len(r["findings"]), str(r.get("name", "")).lower()))
    total = sum(len(r["findings"]) for r in sick)
    lines.append(f"{total} finding(s) in {len(sick)} project(s).")
    lines.append("")
    for r in sick:
        lines.append(f"## {r.get('name') or r.get('project_id')}")
        for f in r["findings"]:
            mark = "CRIT" if f["severity"] == "crit" else "warn"
            lines.append(f"- **{mark}** {f['title']} - {f['detail']}")
            lines.append(f"  Fix: {f['fix_hint']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def new_finding_keys(results: list, notified: set) -> set:
    """Keys of current findings not yet announced."""
    current = {finding_key(r["project_id"], f) for r in results for f in r.get("findings", [])}
    return current - notified


def valid_mode(m: object) -> bool:
    return isinstance(m, str) and m in MODES

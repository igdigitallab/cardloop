"""
policy_rules.py — declarative policy rules in Markdown files (PreToolUse).

A rule is a `*.md` file: YAML-ish frontmatter (what to match, what to do) plus a body (the
message the agent sees). The operator writes the file; the cockpit evaluates it before every
tool call of a Claude-engine run and either denies the call (`action: block`) or lets it run
with the message injected as context (`action: warn`). No code change, no restart: files are
re-read when their mtime changes. Format, operators, limits, trust rule: docs/RULES.md.

Tiers (higher wins, whole-file override by rule NAME — same model as roles.py):
    project   <cwd>/.claude-ops/rules
    global    $CARDLOOP_RULES_DIR or ~/.claude-ops/rules
    pack      every directory handed in via `extra_dirs` (the extension point for profession
              packs — this module only loads them, it does not know what a pack is)

Import hygiene: neither engine.py nor webapp.py is imported here (both import THIS module). The
audit sink is injected by the engine (`make_hook(..., audit_fn=audit)`), the trust opt-in is
read from the `ctx["topics"]` records the webapp already keeps.

Three things a reader must not "simplify":

1. Trust. A project-tier rule file that git TRACKS is ignored unless the operator opted that
   project in (`rules_trust_tracked`). Opening a cloned repository must not silently install
   policy — or inject text into the model. An untrusted file does not take part in the
   override resolution either, otherwise a cloned `enabled: false` file named like a global
   rule would switch that rule off. If git cannot answer, the project tier is treated as
   tracked.

2. Python's `re` has no timeout and holds the GIL. Measured: unanchored `\\s+x`, `.*a.*b`,
   `(ab)*c` against 64 KB of non-matching text run for minutes — none of them is rejected by
   any static check, and the hook runs on the cockpit's event loop. So evaluation runs under a
   SIGALRM watchdog (`_watchdog`; CPython's sre loop checks for signals), regex input is scanned
   in bounded windows, and anything that cannot be decided (timeout, oversized input, spent
   budget) is UNKNOWN: a `block` rule fails CLOSED on it, a `warn` rule fails open. Without
   the watchdog (non-main thread) the windows shrink instead. The static check
   (`unsafe_regex_reason`) only removes the exponential shapes.

3. `warn` must NOT carry `permissionDecision: "allow"`. Per the hooks reference "allow" skips
   the permission prompt, so a warn rule would silently auto-approve the call in ask mode.
   A warn returns `additionalContext` only.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import signal
import stat as _stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import roles as _roles  # frontmatter fence splitter + quote stripping only

try:  # private but stable across 3.11-3.14; the fallback in unsafe_regex_reason covers its loss
    from re import _constants as _sre_const
    from re import _parser as _sre_parse
except ImportError:  # pragma: no cover
    _sre_parse = None
    _sre_const = None

# ─────────────────────────── constants & limits ───────────────────────────

PROJECT_SUBDIR = ".claude-ops/rules"
TIERS = ("project", "global", "pack")           # precedence, highest first
TIER_RANK = {"project": 2, "global": 1, "pack": 0}

MAX_FILE_BYTES = 64 * 1024
MAX_RULES = 100
MAX_PATTERN_CHARS = 512
MAX_CONDITIONS = 16
MAX_MESSAGE_CHARS = 8000
MAX_NAME_CHARS = 100
MAX_DIR_FILES = 1000            # *.md files looked at per directory (the 100-rule cap applies after)
MATCH_WINDOW_CHARS = 64 * 1024  # one regex call never sees more than this
WINDOW_OVERLAP_CHARS = 2048     # a match shorter than this survives a window boundary
MAX_SCAN_CHARS = 1024 * 1024    # beyond this a regex condition is UNKNOWN (block fails closed)
UNARMED_WINDOW_CHARS = 4 * 1024  # no watchdog available: smaller windows, smaller scan cap
UNARMED_SCAN_CHARS = 16 * 1024
RULE_TIMEOUT_S = 0.1            # wall clock for one rule's regex work (watchdog)
EVAL_BUDGET_S = 0.5             # wall clock for one tool call's whole evaluation
MAX_EDIT_CANDIDATES = 100       # MultiEdit entries inspected
TRACKED_TTL_S = 10.0            # how long a `git ls-files` answer is reused
GIT_TIMEOUT_S = 3.0

VALID_EVENTS = ("bash", "file", "mcp", "all")
VALID_ACTIONS = ("block", "warn")
VALID_OPERATORS = ("regex_match", "contains", "equals", "not_contains", "starts_with", "ends_with")
BASE_FIELDS = ("tool_name", "command", "file_path", "content")
BASH_TOOLS = frozenset({"Bash", "PowerShell"})
FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

_RULE_KEYS = frozenset({"name", "enabled", "event", "tool_matcher", "conditions", "pattern", "action"})
_CONDITION_KEYS = frozenset({"field", "operator", "pattern"})
_REGEX_KEYS = frozenset({"pattern", "tool_matcher"})   # verbatim to end of line (no ` #` comments)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,%d}$" % (MAX_NAME_CHARS - 1))
_FIELD_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Z]|[A-Z])")
_REGEX_FLAGS = re.IGNORECASE | re.MULTILINE

NO, YES, UNKNOWN = 0, 1, 2   # tri-state: UNKNOWN = could not be decided (see module docstring, 2)


def global_dir() -> str:
    """$CARDLOOP_RULES_DIR, else ~/.claude-ops/rules. Never auto-created on read."""
    env = os.environ.get("CARDLOOP_RULES_DIR")
    return env if env else os.path.expanduser("~/.claude-ops/rules")


def project_dir(cwd: str) -> str:
    return os.path.join(cwd, PROJECT_SUBDIR)


# ─────────────────────────── data model ───────────────────────────

@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    pattern: str
    regex: "re.Pattern | None" = None


@dataclass(frozen=True)
class RuleDef:
    """A parsed, validated rule. Frozen: instances live in a cross-thread cache."""
    name: str
    enabled: bool
    event: str
    action: str
    message: str
    tool_matcher: "re.Pattern | None" = None
    shortcut: "re.Pattern | None" = None          # the `pattern:` form
    conditions: "tuple[Condition, ...]" = ()
    warnings: "tuple[str, ...]" = ()

    @property
    def uses_regex(self) -> bool:
        return self.shortcut is not None or any(c.regex is not None for c in self.conditions)


@dataclass
class RuleEntry:
    """One rule FILE as the loader saw it — the unit the report lists."""
    name: str                      # frontmatter name, else the file stem
    tier: str
    path: str
    rule: "RuleDef | None" = None
    error: "str | None" = None
    trusted: bool = True
    untrusted_reason: "str | None" = None
    shadowed_by: "str | None" = None

    @property
    def active(self) -> bool:
        return (self.rule is not None and self.rule.enabled and self.trusted
                and self.shadowed_by is None and self.error is None)

    @property
    def status(self) -> str:
        if self.error is not None:
            return "invalid"
        if not self.trusted:
            return "untrusted"
        if self.shadowed_by is not None:
            return "shadowed"
        if self.rule is not None and not self.rule.enabled:
            return "disabled"
        return "active"


@dataclass
class RuleSet:
    entries: "list[RuleEntry]" = field(default_factory=list)
    diagnostics: "list[str]" = field(default_factory=list)   # directory-level problems
    active: "tuple[RuleEntry, ...]" = ()


# ─────────────────────────── regex safety ───────────────────────────

def unsafe_regex_reason(pattern: str) -> "str | None":
    """Why `pattern` must not be loaded, or None. Static, so it can only remove the EXPONENTIAL
    shapes — quadratic ones are the watchdog's job (module docstring, 2).

    Rejected: empty / over-long patterns, backreferences, a quantifier (`*`, `+`, `{n,}`, `{n,m}`)
    around a group that holds a variable-width quantifier (`(a+)+`, `(.*)*`, `(\\w+\\s*)*`) unless
    both counts are small and bounded, and an alternation under a quantifier whose branches can
    start with the same character (`(a|aa)+`, `(\\d|[a-z])+`)."""
    if not isinstance(pattern, str) or not pattern:
        return "pattern must be a non-empty string"
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"pattern is longer than {MAX_PATTERN_CHARS} characters"
    if _sre_parse is None:  # pragma: no cover - conservative textual fallback
        if re.search(r"\\[1-9]|\(\?P=|\)\s*[*+{]", pattern):
            return "backreferences and quantified groups are not allowed"
        return None
    try:
        tree = _sre_parse.parse(pattern, _REGEX_FLAGS)
    except re.error as exc:
        return f"invalid regex: {exc}"
    except (RecursionError, OverflowError, ValueError):
        return "invalid regex: too complex"
    try:
        return _walk_regex(tree.data, ())
    except RecursionError:
        return "pattern is nested too deeply"


def _walk_regex(items, repeats: "tuple[int, ...]") -> "str | None":
    c = _sre_const
    repeat_ops = {c.MAX_REPEAT, c.MIN_REPEAT, getattr(c, "POSSESSIVE_REPEAT", c.MAX_REPEAT)}
    groupref_ops = {c.GROUPREF, c.GROUPREF_EXISTS}
    for op, av in items:
        if op in groupref_ops:
            return "backreferences are not allowed"
        if op in repeat_ops:
            lo, hi, body = av
            if hi > 1 and repeats and lo != hi:
                bound = hi
                for r in repeats:
                    bound = c.MAXREPEAT if r >= c.MAXREPEAT or bound >= c.MAXREPEAT else bound * r
                if bound > 256:
                    return "nested quantifiers (catastrophic backtracking risk)"
            why = _walk_regex(body.data, repeats + (hi,) if hi > 1 else repeats)
            if why:
                return why
        elif op == c.SUBPATTERN:
            why = _walk_regex(av[-1].data, repeats)
            if why:
                return why
        elif op == c.BRANCH:
            alts = av[1]
            if repeats:
                firsts = []
                for alt in alts:
                    first = alt.data[0] if alt.data else None
                    if first is None or first[0] != c.LITERAL:
                        return "alternation inside a quantifier must start every branch with a literal"
                    firsts.append(chr(first[1]).lower())
                if len(set(firsts)) != len(firsts):
                    return "alternation inside a quantifier has overlapping branches"
            for alt in alts:
                why = _walk_regex(alt.data, repeats)
                if why:
                    return why
        elif op in (c.ASSERT, c.ASSERT_NOT):
            why = _walk_regex(av[1].data, repeats)
            if why:
                return why
        elif op == getattr(c, "ATOMIC_GROUP", None):
            why = _walk_regex(av.data, repeats)
            if why:
                return why
    return None


def compile_safe(pattern: str) -> "tuple[re.Pattern | None, str | None]":
    """(compiled, None) or (None, reason). The pattern text is never echoed in the reason."""
    why = unsafe_regex_reason(pattern)
    if why:
        return None, why
    try:
        return re.compile(pattern, _REGEX_FLAGS), None
    except (re.error, RecursionError, OverflowError) as exc:
        return None, f"invalid regex: {exc}"


class _RegexTimeout(Exception):
    """Raised by the SIGALRM handler inside a runaway regex."""


def _raise_timeout(signum, frame):
    raise _RegexTimeout()


@contextlib.contextmanager
def _watchdog(seconds: float):
    """Interrupt a runaway `re` call after `seconds` of wall clock. Yields whether it is armed.

    Needs the main thread (signals) and an idle ITIMER_REAL; the cockpit's event loop runs on
    the main thread and nothing else in the repo uses SIGALRM. When it cannot arm, callers fall
    back to smaller scan windows. The previous handler is always restored."""
    armed = False
    prev = None
    try:
        if (hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
                and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)):
            prev = signal.signal(signal.SIGALRM, _raise_timeout)
            armed = True
            signal.setitimer(signal.ITIMER_REAL, seconds)
    except (ValueError, OSError, AttributeError):
        armed = False
    try:
        yield armed
    finally:
        if armed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                signal.signal(signal.SIGALRM, prev if prev is not None else signal.SIG_DFL)


# ─────────────────────────── frontmatter ───────────────────────────

class _FrontmatterError(ValueError):
    pass


def _decode_scalar(key: str, rest: str):
    """One frontmatter value. Quoted strings follow YAML (single: `''` escapes a quote;
    double: JSON escapes). Unquoted: true/false become bools; `pattern` / `tool_matcher` are
    kept verbatim to the end of the line (a regex may legitimately contain ` #`), everything
    else drops a trailing ` # comment`."""
    rest = rest.strip()
    if rest and rest[0] in ("'", '"'):
        quote = rest[0]
        i, n = 1, len(rest)
        while i < n:
            if quote == '"' and rest[i] == "\\":
                i += 2
                continue
            if rest[i] == quote:
                if quote == "'" and i + 1 < n and rest[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            raise _FrontmatterError(f"'{key}': unterminated quoted value (wrap regexes in single quotes)")
        tail = rest[i + 1:].strip()
        if tail and not tail.startswith("#"):
            raise _FrontmatterError(f"'{key}': unexpected text after the closing quote")
        body = rest[:i + 1]
        if quote == "'":
            return body[1:-1].replace("''", "'")
        try:
            return json.loads(body)
        except ValueError:
            raise _FrontmatterError(
                f"'{key}': invalid escape in a double-quoted value (double the backslash, "
                "or use single quotes)") from None
    if key not in _REGEX_KEYS:
        m = re.search(r"(^|\s)#", rest)
        if m:
            rest = rest[:m.start()].rstrip()
    if _BLOCK_SCALAR_RE.match(rest):
        raise _FrontmatterError(f"'{key}': block scalars (| and >) are not supported")
    low = rest.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return rest


def parse_frontmatter(header: "list[str]") -> "tuple[dict, str | None]":
    """(mapping, error). Supports `key: scalar` lines and one `conditions:` block list of
    mappings. Duplicate keys are an error (a duplicate would silently change what a rule does)."""
    raw: dict = {}
    n = len(header)
    i = 0
    try:
        while i < n:
            line = header[i].rstrip("\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if line[0] in (" ", "\t"):
                raise _FrontmatterError(f"line {i + 2}: unexpected indented line {stripped[:40]!r}")
            key, sep, rest = line.partition(":")
            key = key.strip()
            if not sep or not key:
                raise _FrontmatterError(f"line {i + 2}: expected 'key: value'")
            if key in raw:
                raise _FrontmatterError(f"line {i + 2}: duplicate key '{key}'")
            if key == "conditions":
                if rest.strip() not in ("", "[]"):
                    raise _FrontmatterError(f"line {i + 2}: 'conditions' must be a block list")
                raw[key], i = _parse_conditions(header, i + 1)
                continue
            try:
                raw[key] = _decode_scalar(key, rest)
            except _FrontmatterError as exc:
                raise _FrontmatterError(f"line {i + 2}: {exc}") from None
            i += 1
    except _FrontmatterError as exc:
        return {}, str(exc)
    return raw, None


def _parse_conditions(header: "list[str]", i: int) -> "tuple[list[dict], int]":
    n = len(header)
    items: "list[dict]" = []
    current: "dict | None" = None
    while i < n:
        line = header[i].rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line[0] not in (" ", "\t"):
            break                                  # next top-level key
        if stripped.startswith("- ") or stripped == "-":
            if len(items) >= MAX_CONDITIONS:
                raise _FrontmatterError(f"line {i + 2}: at most {MAX_CONDITIONS} conditions")
            current = {}
            items.append(current)
            stripped = stripped[1:].strip()
            if not stripped:
                i += 1
                continue
        if current is None:
            raise _FrontmatterError(f"line {i + 2}: condition property outside a '- ' list item")
        key, sep, rest = stripped.partition(":")
        key = key.strip()
        if not sep or not key:
            raise _FrontmatterError(f"line {i + 2}: expected 'key: value' in a condition")
        if key in current:
            raise _FrontmatterError(f"line {i + 2}: duplicate condition key '{key}'")
        try:
            current[key] = _decode_scalar(key, rest)
        except _FrontmatterError as exc:
            raise _FrontmatterError(f"line {i + 2}: {exc}") from None
        i += 1
    return items, i


def _clean_message(text: str) -> str:
    text = _ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _CTRL_RE.sub("", text).strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS - 20].rstrip() + "\n[message truncated]"
    return text


def _valid_field(name: str) -> bool:
    if name in BASE_FIELDS:
        return True
    if name.startswith("tool_input."):
        segs = name[len("tool_input."):].split(".")
        return bool(segs) and all(_FIELD_SEGMENT_RE.match(s) for s in segs)
    return False


def parse_rule(text: str, *, stem: str) -> "tuple[RuleDef | None, str | None]":
    """(rule, None) or (None, error). Pure: no filesystem access."""
    text = text.lstrip("\ufeff")
    header, body_lines, err = _roles._split_frontmatter(text)
    if err is not None:
        return None, err
    raw, err = parse_frontmatter(header)
    if err:
        return None, err
    unknown = sorted(set(raw) - _RULE_KEYS)
    if unknown:
        return None, f"unsupported frontmatter key '{unknown[0]}' (a typo would silently weaken the rule)"

    warnings: "list[str]" = []
    name = raw.get("name", stem)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None, f"invalid rule name {name!r}: letters, digits, '.', '_', '-' (max {MAX_NAME_CHARS})"
    if name != stem:
        warnings.append(f"name '{name}' differs from the file name '{stem}.md'; "
                        "overrides and hit counters use the frontmatter name")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, "'enabled' must be true or false"
    event = raw.get("event", "all")
    if not isinstance(event, str) or event.lower() not in VALID_EVENTS:
        return None, f"'event' must be one of {list(VALID_EVENTS)}"
    action = raw.get("action", "warn")
    if not isinstance(action, str) or action.lower() not in VALID_ACTIONS:
        return None, f"'action' must be one of {list(VALID_ACTIONS)}"

    tool_matcher = None
    if "tool_matcher" in raw:
        tm = raw["tool_matcher"]
        if not isinstance(tm, str):
            return None, "'tool_matcher' must be a regex string"
        tool_matcher, why = compile_safe(tm)
        if why:
            return None, f"'tool_matcher': {why}"

    conditions_raw = raw.get("conditions", [])
    simple = raw.get("pattern")
    if conditions_raw and simple is not None:
        return None, "use either 'pattern' or 'conditions', not both"
    shortcut = None
    if simple is not None:
        if not isinstance(simple, str):
            return None, "'pattern' must be a regex string"
        shortcut, why = compile_safe(simple)
        if why:
            return None, f"'pattern': {why}"
    conditions: "list[Condition]" = []
    for idx, c in enumerate(conditions_raw, 1):
        bad = sorted(set(c) - _CONDITION_KEYS)
        if bad:
            return None, f"condition {idx}: unsupported key '{bad[0]}'"
        fld, op, pat = c.get("field"), c.get("operator", "regex_match"), c.get("pattern")
        if not isinstance(fld, str) or not _valid_field(fld):
            return None, (f"condition {idx}: 'field' must be one of {list(BASE_FIELDS)} "
                          "or tool_input.<key>")
        if not isinstance(op, str) or op not in VALID_OPERATORS:
            return None, f"condition {idx}: 'operator' must be one of {list(VALID_OPERATORS)}"
        if not isinstance(pat, str) or not pat:
            return None, f"condition {idx}: 'pattern' must be a non-empty string"
        if len(pat) > MAX_PATTERN_CHARS:
            return None, f"condition {idx}: pattern is longer than {MAX_PATTERN_CHARS} characters"
        rx = None
        if op == "regex_match":
            rx, why = compile_safe(pat)
            if why:
                return None, f"condition {idx}: {why}"
        conditions.append(Condition(fld, op, pat, rx))
    if tool_matcher is None and shortcut is None and not conditions:
        return None, ("a rule needs at least one of 'tool_matcher', 'pattern' or 'conditions' "
                      "(otherwise it would match every call)")

    message = _clean_message("\n".join(body_lines))
    if not message:
        return None, "the message body must not be empty"
    return RuleDef(name=name, enabled=enabled, event=event.lower(), action=action.lower(),
                   message=message, tool_matcher=tool_matcher, shortcut=shortcut,
                   conditions=tuple(conditions), warnings=tuple(warnings)), None


# ─────────────────────────── file loading ───────────────────────────

_FILE_CACHE: "dict[str, tuple[tuple, RuleEntry]]" = {}   # path -> (stat signature, parsed entry)
_CACHE_LOCK = threading.Lock()


def _read_rule_text(path: str, follow_symlinks: bool) -> "tuple[str | None, str | None]":
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, "symbolic links are not loaded from the project tier"
        return None, f"cannot open: {exc.strerror or exc}"
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if st.st_size > MAX_FILE_BYTES:
            return None, f"file is larger than {MAX_FILE_BYTES // 1024} KB"
        chunks, total = [], 0
        while total <= MAX_FILE_BYTES:                       # bounded: the file may grow while read
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        return None, f"cannot read: {exc.strerror or exc}"
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        return None, f"file is larger than {MAX_FILE_BYTES // 1024} KB"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "file is not valid UTF-8"


def _load_file(path: str, tier: str, sig: tuple) -> RuleEntry:
    """Parse one rule file, memoised on its (mtime, size, inode) signature."""
    with _CACHE_LOCK:
        hit = _FILE_CACHE.get(path)
    if hit is not None and hit[0] == sig:
        cached = hit[1]
        return RuleEntry(name=cached.name, tier=tier, path=path, rule=cached.rule, error=cached.error)
    stem = os.path.splitext(os.path.basename(path))[0]
    text, err = _read_rule_text(path, follow_symlinks=(tier != "project"))
    if err is not None:
        entry = RuleEntry(name=stem, tier=tier, path=path, error=err)
    else:
        rule, err = parse_rule(text, stem=stem)
        entry = RuleEntry(name=rule.name if rule else stem, tier=tier, path=path, rule=rule, error=err)
    with _CACHE_LOCK:
        if len(_FILE_CACHE) > 2000:
            _FILE_CACHE.clear()
        _FILE_CACHE[path] = (sig, entry)
    return RuleEntry(name=entry.name, tier=tier, path=path, rule=entry.rule, error=entry.error)


def _list_dir(dir_path: str) -> "list[tuple[str, tuple]]":
    """[(path, signature)] for the *.md files directly inside `dir_path`, sorted. [] if absent."""
    out = []
    try:
        with os.scandir(dir_path) as it:
            for de in it:
                if not de.name.endswith(".md") or de.name.startswith("."):
                    continue
                try:
                    st = de.stat()                        # follows a link: its TARGET's mtime counts
                except OSError:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                out.append((de.path, (st.st_mtime_ns, st.st_size, st.st_ino, de.is_symlink())))
    except (FileNotFoundError, NotADirectoryError):
        return []
    out.sort(key=lambda t: t[0])
    return out[:MAX_DIR_FILES]


# ─────────────────────────── trust (git-tracked project files) ───────────────────────────

def _has_git_marker(cwd: str) -> bool:
    cur = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def tracked_rule_files(cwd: str) -> "tuple[set[str] | None, str | None]":
    """({lower-cased rule paths relative to cwd that git tracks}, None), or (None, reason) when
    git cannot say. A directory that is not inside any git work tree has nothing tracked."""
    if not _has_git_marker(cwd):
        return set(), None
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", cwd, "ls-files", "-z", "--", PROJECT_SUBDIR],
            capture_output=True, timeout=GIT_TIMEOUT_S, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git is unavailable ({type(exc).__name__})"
    if proc.returncode != 0:
        why = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, "git ls-files failed" + (f": {why[-1][:120]}" if why else "")
    files = {p.replace("\\", "/").lower() for p in proc.stdout.decode("utf-8", "replace").split("\0") if p}
    return files, None


def trust_tracked_for(ctx: "dict | None", cwd: str) -> bool:
    """The operator's per-project opt-in (`rules_trust_tracked`), read live from the topics
    records the webapp keeps (the settings POST writes it to every record with this cwd)."""
    topics = (ctx or {}).get("topics")
    if not isinstance(topics, dict):
        return False
    for rec in topics.values():
        if isinstance(rec, dict) and rec.get("cwd") == cwd and rec.get("rules_trust_tracked") is True:
            return True
    return False


# ─────────────────────────── rule set assembly ───────────────────────────

def _tier_dirs(cwd: "str | None", extra_dirs: Iterable[str]) -> "list[tuple[str, str]]":
    # A directory reachable twice (cwd = $HOME makes the project dir the global dir) is listed
    # once, as global: the operator's own tier must not pick up the project tier's stricter
    # symlink / git-tracking checks.
    g = global_dir()
    seen = {os.path.realpath(g)}
    dirs: "list[tuple[str, str]]" = []
    if cwd:
        p = project_dir(cwd)
        if os.path.realpath(p) not in seen:
            dirs.append(("project", p))
            seen.add(os.path.realpath(p))
    dirs.append(("global", g))
    for d in extra_dirs:
        if isinstance(d, str) and d:
            d = os.path.expanduser(d)
            if os.path.realpath(d) not in seen:
                seen.add(os.path.realpath(d))
                dirs.append(("pack", d))
    return dirs


def _project_dir_problem(cwd: str) -> "str | None":
    d = project_dir(cwd)
    if not os.path.lexists(d):
        return None
    if os.path.islink(d) or os.path.realpath(d) != os.path.join(os.path.realpath(cwd), *PROJECT_SUBDIR.split("/")):
        return f"{PROJECT_SUBDIR} is (or sits behind) a symbolic link; the project tier was not loaded"
    return None


def load_ruleset(cwd: "str | None", *, trust_tracked: bool = False,
                 extra_dirs: Sequence[str] = ()) -> RuleSet:
    """Merged rules for a project: project > global > pack, whole-file override by rule name.
    Never raises on a bad file or directory; problems become `entry.error` / `diagnostics`."""
    rs = RuleSet()
    taken: "dict[str, RuleEntry]" = {}
    counted = 0
    tracked: "set[str] | None" = set()
    tracked_err: "str | None" = None
    for tier, dir_path in _tier_dirs(cwd, extra_dirs):
        if tier == "project":
            problem = _project_dir_problem(cwd)  # type: ignore[arg-type]
            if problem:
                rs.diagnostics.append(problem)
                continue
        files = _list_dir(dir_path)
        if tier == "project" and files and not trust_tracked:
            tracked, tracked_err = tracked_rule_files(cwd)  # type: ignore[arg-type]
            if tracked_err:
                rs.diagnostics.append(f"project rules disabled: {tracked_err}")
        for path, sig in files:
            stem = os.path.splitext(os.path.basename(path))[0]
            if tier == "project" and not trust_tracked:
                rel = f"{PROJECT_SUBDIR}/{os.path.basename(path)}".lower()
                if tracked is None or rel in tracked:
                    reason = (tracked_err and f"cannot verify that git does not track it ({tracked_err})"
                              or "tracked by git; set rules_trust_tracked for this project to load it")
                    rs.entries.append(RuleEntry(name=stem, tier=tier, path=path, trusted=False,
                                                untrusted_reason=reason))
                    continue
            entry = _load_file(path, tier, sig)
            rs.entries.append(entry)
            if entry.error is not None:
                continue
            prev = taken.get(entry.name)
            if prev is not None:
                entry.shadowed_by = prev.tier
                continue
            if counted >= MAX_RULES:
                entry.error = f"rule limit reached ({MAX_RULES}); file ignored"
                continue
            taken[entry.name] = entry
            counted += 1
    rs.active = tuple(e for e in rs.entries if e.active)
    return rs


# ─────────────────────────── ruleset cache ───────────────────────────

_SET_CACHE: "dict[str, tuple[tuple, float, RuleSet]]" = {}
_LOGGED: "dict[str, tuple]" = {}


def _signature(cwd: "str | None", trust_tracked: bool, extra_dirs: Sequence[str]) -> tuple:
    parts = []
    for tier, dir_path in _tier_dirs(cwd, extra_dirs):
        parts.append((tier, dir_path, tuple(_list_dir(dir_path))))
    return (trust_tracked, tuple(parts))


def get_ruleset(cwd: "str | None", *, trust_tracked: bool = False,
                extra_dirs: Sequence[str] = (), peek: bool = False) -> "RuleSet | None":
    """Cached `load_ruleset`. Rebuilt when any rule file's mtime/size/inode changes, a file is
    added or removed, the trust opt-in flips, or (with project files present) the git answer is
    older than TRACKED_TTL_S. `peek=True` never loads: it returns None when a rebuild is due
    (the async hook then rebuilds in a thread, because the rebuild may run `git`)."""
    key = cwd or ""
    sig = _signature(cwd, trust_tracked, extra_dirs)
    with _CACHE_LOCK:
        hit = _SET_CACHE.get(key)
    now = time.monotonic()
    if hit is not None and hit[0] == sig:
        has_project_files = any(t == "project" and files for t, _d, files in sig[1])
        if not has_project_files or trust_tracked or now - hit[1] < TRACKED_TTL_S:
            return hit[2]
    if peek:
        return None
    rs = load_ruleset(cwd, trust_tracked=trust_tracked, extra_dirs=extra_dirs)
    with _CACHE_LOCK:
        _SET_CACHE[key] = (sig, now, rs)
    _log_problems(key, rs)
    return rs


def _log_problems(key: str, rs: RuleSet) -> None:
    problems = tuple(rs.diagnostics) + tuple(f"{e.path}: {e.error}" for e in rs.entries if e.error)
    if problems != _LOGGED.get(key, ()):
        _LOGGED[key] = problems
        for p in problems:
            print(f"[policy-rules] {p}")


def clear_caches() -> None:
    """Test helper: forget every parsed file, ruleset and hit counter."""
    with _CACHE_LOCK:
        _FILE_CACHE.clear()
        _SET_CACHE.clear()
        _LOGGED.clear()
        _HITS.clear()


# ─────────────────────────── evaluation ───────────────────────────

@dataclass
class _Eval:
    window: int
    scan_cap: int


def _bounded_text(value, cap: int = 4 * MAX_SCAN_CHARS) -> "str | None":
    """A tool-input value as text. None = absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:cap]
    except (TypeError, ValueError):
        return str(value)[:cap]


def _dig(value, segments: "list[str]"):
    for seg in segments:
        if isinstance(value, dict):
            if seg not in value:
                return None
            value = value[seg]
        elif isinstance(value, list) and seg.isdigit() and int(seg) < len(value):
            value = value[int(seg)]
        else:
            return None
    return value


def field_value(field_name: str, tool_name: str, tool_input: dict) -> "str | None":
    """The text a condition inspects. None = the field does not exist on this call, and then NO
    operator matches (including not_contains: an absent field never satisfies a negative test)."""
    if field_name == "tool_name":
        return tool_name or None
    if field_name == "command":
        return _bounded_text(tool_input.get("command"))
    if field_name == "file_path":
        for k in ("file_path", "path", "notebook_path"):
            if tool_input.get(k) is not None:
                return _bounded_text(tool_input[k])
        return None
    if field_name == "content":
        parts = [_bounded_text(tool_input[k]) for k in ("content", "new_string", "new_source")
                 if tool_input.get(k) is not None]
        return "\n".join(p for p in parts if p is not None) if parts else None
    if field_name.startswith("tool_input."):
        return _bounded_text(_dig(tool_input, field_name[len("tool_input."):].split(".")))
    return None


def _candidates(tool_name: str, tool_input: dict) -> "tuple[list[dict], bool]":
    """The inputs a rule is tried against. A MultiEdit is split per edit so that every condition
    of a rule has to hold on the SAME edit. Returns (candidates, truncated)."""
    edits = tool_input.get("edits")
    if tool_name == "MultiEdit" and isinstance(edits, list) and edits:
        base = {k: v for k, v in tool_input.items() if k != "edits"}
        out = [{**base, **e} for e in edits[:MAX_EDIT_CANDIDATES] if isinstance(e, dict)]
        return (out or [tool_input]), len(edits) > MAX_EDIT_CANDIDATES
    return [tool_input], False


def _regex_tri(rx: "re.Pattern", text: str, ev: _Eval) -> int:
    n = len(text)
    if n <= ev.window:
        return YES if rx.search(text) else NO
    end = min(n, ev.scan_cap)
    step = ev.window - WINDOW_OVERLAP_CHARS if ev.window > 4 * WINDOW_OVERLAP_CHARS else ev.window // 2
    pos = 0
    while pos < end:
        if rx.search(text, pos, min(pos + ev.window, n)):
            return YES
        pos += step
    return NO if n <= ev.scan_cap else UNKNOWN


def _condition_tri(cond: Condition, tool_name: str, cand: dict, ev: _Eval) -> int:
    text = field_value(cond.field, tool_name, cand)
    if text is None:
        return NO
    op = cond.operator
    if op == "regex_match":
        return _regex_tri(cond.regex, text, ev)  # type: ignore[arg-type]
    if op == "contains":
        return YES if cond.pattern in text else NO
    if op == "not_contains":
        return NO if cond.pattern in text else YES
    if op == "equals":
        return YES if text == cond.pattern else NO
    if op == "starts_with":
        return YES if text.startswith(cond.pattern) else NO
    if op == "ends_with":
        return YES if text.endswith(cond.pattern) else NO
    return NO


def _shortcut_values(rule: RuleDef, tool_name: str, tool_input: dict) -> "list[str]":
    """What the `pattern:` shortcut is matched against: the command for Bash, paths and
    contents for file tools, tool name + the whole input as JSON for anything else; the
    `all` event adds the tool name to the first two."""
    values: "list[str]" = []
    if tool_name in BASH_TOOLS:
        values.append(field_value("command", tool_name, tool_input) or "")
    elif tool_name in FILE_TOOLS:
        cands, _ = _candidates(tool_name, tool_input)
        for cand in cands:
            for f in ("file_path", "content"):
                v = field_value(f, tool_name, cand)
                if v:
                    values.append(v)
    else:
        values.append(tool_name)
        values.append(_bounded_text(tool_input) or "")
    if rule.event == "all" and tool_name not in values:
        values.append(tool_name)
    return [v for v in values if v]


def rule_applies(rule: RuleDef, tool_name: str) -> bool:
    ev = rule.event
    if ev == "bash" and tool_name not in BASH_TOOLS:
        return False
    if ev == "file" and tool_name not in FILE_TOOLS:
        return False
    if ev == "mcp" and not tool_name.startswith("mcp__"):
        return False
    if rule.tool_matcher is not None and not rule.tool_matcher.search(tool_name):
        return False
    return True


def _rule_tri(rule: RuleDef, tool_name: str, tool_input: dict, ev: _Eval) -> int:
    if rule.shortcut is not None:
        saw_unknown = False
        for v in _shortcut_values(rule, tool_name, tool_input):
            r = _regex_tri(rule.shortcut, v, ev)
            if r == YES:
                return YES
            saw_unknown = saw_unknown or r == UNKNOWN
        return UNKNOWN if saw_unknown else NO
    if not rule.conditions:
        return YES                      # tool_matcher-only rule: applicability was the test
    cands, truncated = _candidates(tool_name, tool_input)
    saw_unknown = truncated
    for cand in cands:
        outcome = YES
        for cond in rule.conditions:
            r = _condition_tri(cond, tool_name, cand, ev)
            if r == NO:
                outcome = NO
                break
            if r == UNKNOWN:
                outcome = UNKNOWN
        if outcome == YES:
            return YES
        saw_unknown = saw_unknown or outcome == UNKNOWN
    return UNKNOWN if saw_unknown else NO


@dataclass(frozen=True)
class Match:
    name: str
    action: str
    message: str
    unverified: bool = False      # a block rule that could not be decided and failed closed


def evaluate(ruleset: RuleSet, tool_name: str, tool_input: "dict | None",
             *, budget_s: float = EVAL_BUDGET_S) -> "tuple[list[Match], list[tuple[str, str]]]":
    """(matches, problems). `problems` = [(rule name, what)] for timeouts / spent budget; the
    caller counts them. Must run on the main thread to get the watchdog (else smaller windows)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_name = tool_name if isinstance(tool_name, str) else ""
    matches: "list[Match]" = []
    problems: "list[tuple[str, str]]" = []
    started = time.monotonic()
    for entry in ruleset.active:
        rule = entry.rule
        if rule is None or not rule_applies(rule, tool_name):
            continue
        res = NO
        try:
            if time.monotonic() - started > budget_s:
                res = UNKNOWN
                problems.append((rule.name, "evaluation budget spent"))
            elif rule.uses_regex:
                with _watchdog(RULE_TIMEOUT_S) as armed:
                    ev = _Eval(MATCH_WINDOW_CHARS, MAX_SCAN_CHARS) if armed else \
                        _Eval(UNARMED_WINDOW_CHARS, UNARMED_SCAN_CHARS)
                    res = _rule_tri(rule, tool_name, tool_input, ev)
            else:
                res = _rule_tri(rule, tool_name, tool_input, _Eval(MATCH_WINDOW_CHARS, MAX_SCAN_CHARS))
        except _RegexTimeout:
            res = UNKNOWN
            problems.append((rule.name, "regex timed out"))
        except RecursionError:
            res = UNKNOWN
            problems.append((rule.name, "regex too deep"))
        if res == YES:
            matches.append(Match(rule.name, rule.action, rule.message))
        elif res == UNKNOWN:
            if rule.action == "block":
                matches.append(Match(rule.name, rule.action, rule.message, unverified=True))
            if not problems or problems[-1][0] != rule.name:
                problems.append((rule.name, "input could not be fully inspected"))
    return matches, problems


# ─────────────────────────── hit counters ───────────────────────────

_HITS: "dict[tuple[str, str], list]" = {}   # (cwd, rule name) -> [hits, last_hit_ts, problems]
_HITS_LOCK = threading.Lock()


def _bump(cwd: str, name: str, *, hit: bool, problem: bool) -> None:
    with _HITS_LOCK:
        rec = _HITS.setdefault((cwd, name), [0, None, 0])
        if hit:
            rec[0] += 1
            rec[1] = time.time()
        if problem:
            rec[2] += 1


def hit_stats(cwd: str) -> "dict[str, dict]":
    with _HITS_LOCK:
        return {name: {"hits": r[0], "last_hit": r[1], "problems": r[2]}
                for (c, name), r in _HITS.items() if c == cwd}


# ─────────────────────────── hook ───────────────────────────

def _one_line(text: str, cap: int) -> str:
    text = _CTRL_RE.sub(" ", _ANSI_RE.sub("", text.replace("\n", " ").replace("\r", " ")))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= cap else text[:cap - 3] + "..."


def short_input(tool_name: str, tool_input: dict) -> str:
    """What the audit line says about the call. Never file contents or non-command arguments —
    those routinely carry the very secrets a rule exists to keep out of logs."""
    if tool_name in BASH_TOOLS:
        return str(tool_input.get("command", ""))
    if tool_name in FILE_TOOLS:
        return f"{tool_name} {field_value('file_path', tool_name, tool_input) or ''}"
    keys = ",".join(sorted(str(k) for k in tool_input)[:8])
    return f"{tool_name} {{{keys}}}"


def audit_text(name: str, action: str, tool_name: str, tool_input: dict, *, unverified: bool = False) -> str:
    prefix = f"{name} {action}: " + ("[unverified] " if unverified else "")
    return prefix + _one_line(short_input(tool_name, tool_input), max(0, 200 - len(prefix)))


def build_output(matches: "list[Match]") -> dict:
    """The PreToolUse hook output for a non-empty match list (exact shapes: docs/RULES.md)."""
    blockers = [m for m in matches if m.action == "block"]
    if blockers:
        lines = []
        for m in blockers:
            note = ("\n(This input was too large or too slow to inspect fully, so the rule "
                    "failed closed.)" if m.unverified else "")
            lines.append(f"[{m.name}] {m.message}{note}")
        head = "Blocked by policy rule" + ("s" if len(blockers) > 1 else "") + ":\n"
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _cap(head + "\n\n".join(lines)),
        }}
    lines = [f"[{m.name}] {m.message}" for m in matches]
    head = "Policy warning" + ("s" if len(lines) > 1 else "") + " (the call is allowed to proceed):\n"
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": _cap(head + "\n\n".join(lines)),
    }}


def _cap(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[:MAX_MESSAGE_CHARS - 24].rstrip() + "\n[output truncated]"


def make_hook(project_name: str, cwd: str, ctx: "dict | None" = None, *,
              audit_fn: "Callable[[str, str, str], None] | None" = None,
              extra_dirs: "Sequence[str] | Callable[[], Sequence[str]] | None" = None):
    """The async PreToolUse callback for the SDK (one HookMatcher with no tool filter).

    Closes over the project, not over a rule list: the rules are looked up per call from the
    mtime-keyed cache, so edits (and the trust opt-in) apply to an already-connected live
    client. `extra_dirs` is the pack extension point — a list of directories, or a callable
    returning one (called per tool call)."""
    async def _policy_rules_hook(hook_input, tool_use_id, context) -> dict:
        try:
            if not isinstance(hook_input, dict):
                return {}
            tool_name = hook_input.get("tool_name")
            tool_input = hook_input.get("tool_input")
            extras = tuple(extra_dirs() if callable(extra_dirs) else (extra_dirs or ()))
            trust = trust_tracked_for(ctx, cwd)
            rs = get_ruleset(cwd, trust_tracked=trust, extra_dirs=extras, peek=True)
            if rs is None:
                rs = await asyncio.to_thread(get_ruleset, cwd, trust_tracked=trust, extra_dirs=extras)
            if not rs.active:
                return {}
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            matches, problems = evaluate(rs, tool_name, tool_input)
            for rname, _what in problems:
                _bump(cwd, rname, hit=False, problem=True)
            if not matches:
                return {}
            for m in matches:
                _bump(cwd, m.name, hit=True, problem=False)
                if audit_fn is not None:
                    audit_fn(project_name, "RULE",
                             audit_text(m.name, m.action, tool_name or "", tool_input, unverified=m.unverified))
            return build_output(matches)
        except Exception as exc:   # a policy bug must never break a turn
            print(f"[policy-rules] hook error ({type(exc).__name__}): {exc}")
            return {}
    return _policy_rules_hook


# ─────────────────────────── API report ───────────────────────────

def _match_summary(rule: RuleDef) -> str:
    parts = []
    if rule.tool_matcher is not None:
        parts.append(f"tool ~ {rule.tool_matcher.pattern}")
    if rule.shortcut is not None:
        parts.append(f"pattern ~ {rule.shortcut.pattern}")
    for c in rule.conditions:
        parts.append(f"{c.field} {c.operator} {c.pattern}")
    return _one_line(" AND ".join(parts), 300)


def build_report(cwd: str, *, trust_tracked: bool = False, extra_dirs: Sequence[str] = ()) -> dict:
    """The JSON behind GET /api/projects/{id}/rules."""
    rs = get_ruleset(cwd, trust_tracked=trust_tracked, extra_dirs=extra_dirs)
    stats = hit_stats(cwd)
    rows = []
    for e in rs.entries:
        st = stats.get(e.name, {}) if e.active else {}
        diags = []
        if e.error:
            diags.append(e.error)
        if e.untrusted_reason:
            diags.append(e.untrusted_reason)
        if e.rule:
            diags.extend(e.rule.warnings)
        if e.shadowed_by:
            diags.append(f"overridden by a rule with the same name in the {e.shadowed_by} tier")
        problems = st.get("problems", 0)
        if problems:
            diags.append(f"{problems} evaluation(s) timed out or could not inspect the whole input")
        rows.append({
            "name": e.name,
            "tier": e.tier,
            "path": e.path,
            "enabled": bool(e.rule.enabled) if e.rule else False,
            "trusted": e.trusted,
            "status": e.status,
            "action": e.rule.action if e.rule else None,
            "event": e.rule.event if e.rule else None,
            "match": _match_summary(e.rule) if e.rule else "",
            "message": e.rule.message[:300] if e.rule else "",
            "hits": st.get("hits", 0),
            "last_hit": st.get("last_hit"),
            "diagnostics": diags,
        })
    rows.sort(key=lambda r: (0 if r["status"] == "active" else 1, -TIER_RANK.get(r["tier"], 0), r["name"]))
    return {
        "rules": rows,
        "diagnostics": list(rs.diagnostics),
        "trust_tracked": trust_tracked,
        "global_dir": global_dir(),
        "project_dir": project_dir(cwd) if cwd else None,
        "limits": {"max_rules": MAX_RULES, "max_file_kb": MAX_FILE_BYTES // 1024,
                   "max_pattern_chars": MAX_PATTERN_CHARS},
    }

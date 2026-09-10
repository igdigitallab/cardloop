"""
roles.py — spec-091 Phase 1: declarative, cockpit-editable sub-agent roles.

Role files live in `.claude-ops/roles/` (see IMPLEMENTATION.md §0 for why), NOT in
`.claude/agents/`. Three tiers resolve by whole-file override on name: builtin (this repo's
`roles/builtin/`) → global (`$CARDLOOP_ROLES_DIR` or `~/.claude-ops/roles`) → project
(`<cwd>/.claude-ops/roles`). Files are the source of truth; the cockpit edits files, `git` is
their history.

Import hygiene: this module imports nothing from `engine.py` or `webapp.py` — both directions
would cycle. `engine.py` imports `roles`.

No new third-party dependency: the frontmatter parser below is hand-rolled (~small), following
the two existing precedents in this repo (`webapp.py` `_parse_skill_frontmatter`,
`spec_mirror.py` `_frontmatter_body_span`). No `pyyaml`.
"""
import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import get_args

from claude_agent_sdk import AgentDefinition
from claude_agent_sdk.types import PermissionMode as _SDK_PermissionMode

# ─────────────────────────── constants ───────────────────────────

ROLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
PROJECT_SUBDIR = ".claude-ops/roles"
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles", "builtin")

_SCOPES = ("builtin", "global", "project")
_SCOPE_RANK = {"builtin": 0, "global": 1, "project": 2}  # higher rank wins on override

# Frontmatter keys mapped onto explicit Role fields. Anything else lands in Role.extras.
_KNOWN_KEYS = frozenset({
    "name", "description", "enabled", "tools", "disallowedTools", "model", "effort",
    "maxTurns", "skills", "mcpServers", "memory", "permissionMode", "color",
})

_INT_RE = re.compile(r"^-?\d+$")

# I1j fix (A2-audit.md/F7 residual): enum validation used to live ONLY in webapp.py's HTTP
# write path, so a role file written by hand or by any agent with Bash reached
# AgentDefinition with a garbage effort/permissionMode/memory/maxTurns
# verbatim; the SDK's dataclass does not enforce its own Literal types at runtime. parse_role
# is the one choke point every read (and write, which re-parses) goes through, so the checks
# move here. effort/memory mirror the SDK's own Literal values; permissionMode is read straight
# from the SDK module so the allowed set tracks it instead of a hand-copied list going stale.
_ROLE_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh", "max"})
_ROLE_MEMORY_VALUES = frozenset({"user", "project", "local"})
_ROLE_PERMISSION_MODES = frozenset(get_args(_SDK_PermissionMode))


def global_dir() -> str:
    """$CARDLOOP_ROLES_DIR, else ~/.claude-ops/roles. Never auto-created on read."""
    env = os.environ.get("CARDLOOP_ROLES_DIR")
    if env:
        return env
    return os.path.expanduser("~/.claude-ops/roles")


# ─────────────────────────── data model ───────────────────────────

@dataclass(frozen=True)
class Role:
    name: str
    scope: str            # "builtin" | "global" | "project"
    path: str
    description: str
    prompt: str
    enabled: bool = True
    tools: "list[str] | None" = None
    disallowed_tools: "list[str] | None" = None
    model: "str | None" = None
    effort: "str | None" = None
    max_turns: "int | None" = None
    skills: "list[str] | None" = None
    mcp_servers: "list[str] | None" = None
    memory: "str | None" = None
    permission_mode: "str | None" = None
    color: "str | None" = None
    extras: dict = field(default_factory=dict)
    warnings: tuple = ()


# ─────────────────────────── frontmatter parsing ───────────────────────────

def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _parse_value(rest: str, header: "list[str]", start: int, n: int):
    """Parse one frontmatter value. Returns (value, extra_lines_consumed).

    Covers every form in IMPLEMENTATION.md §1: scalar, quoted scalar, bool, int, inline
    list, empty inline list, block list, folded scalar (`>`). Anything left over after
    those checks is treated as a plain scalar string (the table's "scalar" form).
    """
    if rest == "":
        # Possible block list: subsequent lines of the form "  - item".
        items: "list[str]" = []
        j = start
        while j < n:
            s = header[j].strip()
            if s.startswith("- "):
                items.append(_strip_quotes(s[2:].strip()))
                j += 1
                continue
            if s == "-":
                items.append("")
                j += 1
                continue
            break
        if j > start:
            return items, j - start
        return "", 0

    if rest.startswith(">"):
        # Folded scalar: subsequent indented lines join into one string, separated by spaces.
        text_parts: "list[str]" = []
        j = start
        while j < n:
            line = header[j]
            if line.strip() == "":
                break
            if line[:1] in (" ", "\t"):
                text_parts.append(line.strip())
                j += 1
                continue
            break
        return " ".join(text_parts), j - start

    if rest.startswith("[") and rest.endswith("]"):
        inner = rest[1:-1].strip()
        if not inner:
            return [], 0
        return [_strip_quotes(x.strip()) for x in inner.split(",")], 0

    if rest == "true":
        return True, 0
    if rest == "false":
        return False, 0
    if _INT_RE.match(rest):
        return int(rest), 0

    return _strip_quotes(rest), 0


def _split_frontmatter(text: str):
    """(header_lines, body_lines, error). header/body are None on error."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, None, "line 1: file must start with a '---' frontmatter fence"
    close_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            close_idx = idx
            break
    if close_idx is None:
        return None, None, "frontmatter is not closed with a '---' line"
    return lines[1:close_idx], lines[close_idx + 1:], None


def _parse_frontmatter_body(header: "list[str]"):
    """(raw_dict, key_line, error). key_line maps key -> 1-based physical line number."""
    raw: dict = {}
    key_line: dict = {}
    n = len(header)
    i = 0
    while i < n:
        line = header[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" not in line:
            return None, None, f"line {i + 2}: expected 'key: value', got {line!r}"
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not key:
            return None, None, f"line {i + 2}: empty key"
        value, consumed = _parse_value(rest, header, i + 1, n)
        raw[key] = value
        key_line[key] = i + 2
        i += 1 + consumed
    return raw, key_line, None


def _err_line(key_line: dict, key: str) -> str:
    ln = key_line.get(key)
    return f"line {ln}: " if ln else ""


def parse_role(text: str, *, name: str, scope: str, path: str):
    """(role, None) or (None, error). Pure: no filesystem access."""
    if not ROLE_NAME_RE.match(name):
        return None, f"invalid role name {name!r}: must match {ROLE_NAME_RE.pattern}"

    header, body_lines, err = _split_frontmatter(text)
    if err is not None:
        return None, err

    raw, key_line, err = _parse_frontmatter_body(header)
    if err is not None:
        return None, err

    warnings: "list[str]" = []
    fm_name = raw.get("name")
    if isinstance(fm_name, str) and fm_name and fm_name != name:
        warnings.append(
            f"frontmatter 'name: {fm_name}' does not match the filename '{name}.md'; "
            "the filename wins")

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, f"{_err_line(key_line, 'description')}'description' is required and must be non-empty"

    prompt = "\n".join(body_lines).strip()
    if not prompt:
        return None, "the role body (prompt) is required and must be non-empty"

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, f"{_err_line(key_line, 'enabled')}'enabled' must be a bool (true|false)"

    def _opt_str_list(key: str):
        if key not in raw:
            return None, None
        v = raw[key]
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return v, None
        return None, f"{_err_line(key_line, key)}'{key}' must be a list of strings"

    def _opt_str(key: str):
        if key not in raw:
            return None, None
        v = raw[key]
        if isinstance(v, str):
            return v, None
        return None, f"{_err_line(key_line, key)}'{key}' must be a string"

    def _opt_int(key: str):
        if key not in raw:
            return None, None
        v = raw[key]
        if isinstance(v, int) and not isinstance(v, bool):
            return v, None
        return None, f"{_err_line(key_line, key)}'{key}' must be an integer"

    tools, e = _opt_str_list("tools")
    if e:
        return None, e
    disallowed_tools, e = _opt_str_list("disallowedTools")
    if e:
        return None, e
    model, e = _opt_str("model")
    if e:
        return None, e
    effort, e = _opt_str("effort")
    if e:
        return None, e
    if effort is not None and effort not in _ROLE_EFFORT_VALUES:
        return None, (f"{_err_line(key_line, 'effort')}'effort' must be one of "
                       f"{sorted(_ROLE_EFFORT_VALUES)}, got {effort!r}")
    max_turns, e = _opt_int("maxTurns")
    if e:
        return None, e
    if max_turns is not None and max_turns <= 0:
        return None, f"{_err_line(key_line, 'maxTurns')}'maxTurns' must be a positive integer, got {max_turns!r}"
    skills, e = _opt_str_list("skills")
    if e:
        return None, e
    mcp_servers, e = _opt_str_list("mcpServers")
    if e:
        return None, e
    memory, e = _opt_str("memory")
    if e:
        return None, e
    if memory is not None and memory not in _ROLE_MEMORY_VALUES:
        return None, (f"{_err_line(key_line, 'memory')}'memory' must be one of "
                       f"{sorted(_ROLE_MEMORY_VALUES)}, got {memory!r}")
    permission_mode, e = _opt_str("permissionMode")
    if e:
        return None, e
    if permission_mode is not None and permission_mode not in _ROLE_PERMISSION_MODES:
        return None, (f"{_err_line(key_line, 'permissionMode')}'permissionMode' must be one of "
                       f"{sorted(_ROLE_PERMISSION_MODES)}, got {permission_mode!r}")
    color, e = _opt_str("color")
    if e:
        return None, e

    extras = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}

    role = Role(
        name=name,
        scope=scope,
        path=path,
        description=description.strip(),
        prompt=prompt,
        enabled=enabled,
        tools=tools,
        disallowed_tools=disallowed_tools,
        model=model,
        effort=effort,
        max_turns=max_turns,
        skills=skills,
        mcp_servers=mcp_servers,
        memory=memory,
        permission_mode=permission_mode,
        color=color,
        extras=extras,
        warnings=tuple(warnings),
    )
    return role, None


# ─────────────────────────── registry (filesystem) ───────────────────────────

def _iter_role_files(dir_path: str) -> "list[str]":
    """Sorted *.md file paths directly inside dir_path (no recursion). [] if absent."""
    if not os.path.isdir(dir_path):
        return []
    out = []
    for fn in sorted(os.listdir(dir_path)):
        if fn.endswith(".md") and not fn.startswith("."):
            out.append(os.path.join(dir_path, fn))
    return out


def _tiers(cwd: "str | None"):
    tiers = [("builtin", BUILTIN_DIR), ("global", global_dir())]
    if cwd:
        tiers.append(("project", os.path.join(cwd, PROJECT_SUBDIR)))
    return tiers


def list_roles_report(cwd: "str | None"):
    """(roles, errors). errors = [{name, scope, path, error}]. Never raises on a bad file —
    NOR on an unreadable directory (I1l fix, A2-audit.md): `_iter_role_files`'s own
    `os.listdir` used to propagate a bare PermissionError straight out of this function,
    through `engine.py`'s run_engine, killing every turn in the project even though this
    docstring already promised "never raises". Degrades to an empty list for that tier plus
    one reported error instead."""
    roles: "list[Role]" = []
    errors: "list[dict]" = []
    for scope, dir_path in _tiers(cwd):
        try:
            dir_files = _iter_role_files(dir_path)
        except OSError as exc:
            errors.append({"name": None, "scope": scope, "path": dir_path, "error": str(exc)})
            continue
        for path in dir_files:
            name = os.path.splitext(os.path.basename(path))[0]
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError as exc:
                errors.append({"name": name, "scope": scope, "path": path, "error": str(exc)})
                continue
            role, err = parse_role(text, name=name, scope=scope, path=path)
            if role is None:
                errors.append({"name": name, "scope": scope, "path": path, "error": err})
                continue
            roles.append(role)
    roles.sort(key=lambda r: (r.name, _SCOPE_RANK.get(r.scope, 9)))
    return roles, errors


def scope_rank(scope: str) -> int:
    """Precedence rank of a scope (higher wins on whole-file override). Exposed so callers
    outside this module (webapp.py's `shadowed_by` computation) don't hand-copy `_SCOPE_RANK`."""
    return _SCOPE_RANK.get(scope, 9)


def load_roles(cwd: "str | None", _all: "list[Role] | None" = None) -> "dict[str, Role]":
    """Merged effective registry: project > global > builtin, whole-file override by name.
    Only enabled roles are returned.

    `_all` lets a caller that already ran `list_roles_report(cwd)` THIS turn/request pass that
    result in, instead of re-walking the filesystem a second time — `engine.py`'s run_engine and
    `webapp.py`'s api_project_roles both do one walk per call and reuse it here (see
    A1-audit.md's per-turn/per-request filesystem-cost finding)."""
    roles_list = _all if _all is not None else list_roles_report(cwd)[0]
    best: "dict[str, Role]" = {}
    for r in roles_list:
        cur = best.get(r.name)
        if cur is None or _SCOPE_RANK[r.scope] >= _SCOPE_RANK[cur.scope]:
            best[r.name] = r
    return {name: r for name, r in best.items() if r.enabled}


def compile_agents(roles: "dict[str, Role]") -> dict:
    """{name: AgentDefinition}. Maps Role fields 1:1 onto AgentDefinition fields.
    permission_mode defaults to "bypassPermissions" when the file omits it (matches
    today's roster — see engine.py DEFAULT_AGENTS)."""
    out: dict = {}
    for name, r in roles.items():
        out[name] = AgentDefinition(
            description=r.description,
            prompt=r.prompt,
            tools=r.tools,
            disallowedTools=r.disallowed_tools,
            model=r.model,
            skills=r.skills,
            memory=r.memory,
            mcpServers=r.mcp_servers,
            maxTurns=r.max_turns,
            effort=r.effort,
            permissionMode=r.permission_mode or "bypassPermissions",
        )
    return out


def registry_fingerprint(roles: "dict[str, Role]") -> str:
    """sha256 over the identity-relevant fields of the effective registry. Feeds engine's
    _stable_append_pieces so a reused live client cannot serve a stale roster."""
    h = hashlib.sha256()
    for name in sorted(roles):
        r = roles[name]
        h.update(name.encode("utf-8"))
        h.update(repr((
            r.description, r.prompt, r.enabled, r.tools, r.disallowed_tools, r.model,
            r.effort, r.max_turns, r.skills, r.mcp_servers, r.memory, r.permission_mode,
        )).encode("utf-8"))
    return h.hexdigest()


# ─────────────────────────── writes (files are the source of truth) ───────────────────────────

def role_path(cwd: "str | None", name: str, scope: str) -> str:
    if scope == "builtin":
        return os.path.join(BUILTIN_DIR, f"{name}.md")
    if scope == "global":
        return os.path.join(global_dir(), f"{name}.md")
    if scope == "project":
        if not cwd:
            raise ValueError("project scope requires a cwd")
        return os.path.join(cwd, PROJECT_SUBDIR, f"{name}.md")
    raise ValueError(f"unknown scope: {scope!r}")


def _atomic_write(path: str, content: str) -> None:
    """tmp file in the same dir, fsync, os.replace — no partially written file on a crash."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp-role-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def write_role(cwd: "str | None", name: str, scope: str, content: str, *, overwrite: bool = False) -> Role:
    """Validate (parse must succeed) → atomic write. Refuses scope="builtin".

    I1k fix (A2-audit.md/F2 residual): the only guard against clobbering an existing file used
    to be a client-side `shadowed_by` snapshot (the last GET) — a stale tab, a second cockpit
    window, or any direct API caller defeated it outright. `overwrite=False` (the default) now
    refuses with FileExistsError when `name`/`scope` already resolves to a file on disk; the
    caller must pass `overwrite=True` to replace it deliberately."""
    if scope == "builtin":
        raise ValueError("builtin roles are read-only — save it to the project or global scope instead")
    if scope not in _SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    if not ROLE_NAME_RE.match(name):
        raise ValueError(f"invalid role name {name!r}: must match {ROLE_NAME_RE.pattern}")

    path = role_path(cwd, name, scope)
    # Content validity (parse_role) is checked BEFORE the existence guard: invalid content
    # must 400, not 409, even when a file already sits at `path` — the CHECKLIST E5 contract
    # ("a write with invalid content ... leaves the previous file unchanged") does not care
    # about overwrite at all, it only cares that garbage never reaches disk.
    role, err = parse_role(content, name=name, scope=scope, path=path)
    if role is None:
        raise ValueError(err)
    if not overwrite and os.path.isfile(path):
        raise FileExistsError(f"{path} already exists — pass overwrite=True to replace it")
    _atomic_write(path, content)
    return role


def delete_role(cwd: "str | None", name: str, scope: str) -> None:
    """Refuses scope="builtin". Missing file → FileNotFoundError."""
    if scope == "builtin":
        raise ValueError("builtin roles are read-only — save it to the project or global scope instead")
    if scope not in _SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    if not ROLE_NAME_RE.match(name):
        raise ValueError(f"invalid role name {name!r}: must match {ROLE_NAME_RE.pattern}")
    path = role_path(cwd, name, scope)
    os.remove(path)


def _toggle_enabled_text(text: str, enabled: bool) -> str:
    """Rewrite ONLY the `enabled:` key, preserving the rest of the file's lines and key order.

    I1k fix (A2-audit.md): this used to claim "byte-for-byte" preservation, which was false for
    a CRLF file — `text.split("\\n")` leaves each unchanged line's trailing "\\r" attached, but
    the two hardcoded fence lines and a rewritten `enabled:` line were re-joined with a bare
    "\\n", so a CRLF file came back with mixed line endings. Detect the file's own line ending
    once and use it consistently for every line this function writes, instead."""
    eol = "\r\n" if "\r\n" in text else "\n"
    header, body, err = _split_frontmatter(text)
    if err is not None:
        raise ValueError(err)
    header = [line.rstrip("\r") for line in header]
    body = [line.rstrip("\r") for line in body]
    value_str = "true" if enabled else "false"
    new_header = []
    found = False
    for line in header:
        if re.match(r"^enabled\s*:", line):
            new_header.append(f"enabled: {value_str}")
            found = True
        else:
            new_header.append(line)
    if not found:
        new_header.append(f"enabled: {value_str}")
    return eol.join(["---", *new_header, "---", *body])


def set_enabled(cwd: "str | None", name: str, scope: str, enabled: bool) -> Role:
    """For a builtin role, COPY it into the project scope first and toggle there — a
    builtin file is never mutated. Otherwise rewrites the file in place.

    F2 fix: if a PROJECT file for this name already exists (a prior "copy to project" or a
    hand-edited custom role), toggling `scope="builtin"` must flip ITS `enabled:` key, not
    overwrite it with the builtin's text — project role files are gitignored, so clobbering
    one is unrecoverable (see A1-audit.md F2).

    I1k fix (A2-audit.md): the check-then-open above used to be `os.path.isfile(dest_path)`
    followed by a SEPARATE `open(src_path)` — a window in which the project file could be
    created or removed between the two. Ask forgiveness instead of permission: try opening the
    project file directly and fall back to the builtin on FileNotFoundError, so there is no gap
    between "does it exist" and "read it"."""
    if scope not in _SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    if not ROLE_NAME_RE.match(name):
        raise ValueError(f"invalid role name {name!r}: must match {ROLE_NAME_RE.pattern}")

    dest_scope = "project" if scope == "builtin" else scope
    dest_path = role_path(cwd, name, dest_scope)
    # newline="" disables Python's universal-newline translation on read, so a CRLF file's
    # "\r\n" survives into `text` instead of being silently normalized to "\n" before
    # `_toggle_enabled_text` ever sees it (that normalization, not the rejoin, was the real
    # source of the CRLF-preservation claim being false).
    if scope == "builtin":
        project_path = role_path(cwd, name, "project")
        try:
            with open(project_path, encoding="utf-8", newline="") as f:
                text = f.read()
        except FileNotFoundError:
            builtin_path = role_path(cwd, name, "builtin")
            with open(builtin_path, encoding="utf-8", newline="") as f:
                text = f.read()
    else:
        src_path = role_path(cwd, name, scope)
        with open(src_path, encoding="utf-8", newline="") as f:
            text = f.read()

    new_text = _toggle_enabled_text(text, enabled)

    role, err = parse_role(new_text, name=name, scope=dest_scope, path=dest_path)
    if role is None:
        raise ValueError(err)
    _atomic_write(dest_path, new_text)
    return role

"""fs_browser.py — the file explorer's filesystem policy, free of any HTTP.

The Files tab used to be jailed to a project's cwd and a second "Server files" tab was
jailed to $HOME. This module replaces both with ONE absolute-path view whose ceiling is a
set of *roots*:

    $HOME  +  FILES_EXTRA_ROOTS (colon-separated, default "/tmp")  +  the project's cwd

so an operator can paste `/tmp/report.md` or `~/projects/other/README.md` that an agent
just printed and land on it. webapp.py owns the routes; everything that decides "may this
path be seen / written" lives here so it can be tested without a server.

Deny rules (a request is refused unless some root permits it AND nothing below denies it):
  * every path is realpath'ed first, so a symlink cannot walk out of a root;
  * under $HOME every top-level dot entry is hidden (`.ssh`, `.aws`, `.claude` credentials,
    `.git-credentials`, `.config/claude-ops/secret.key` …) — deny by default, because the
    list of places tools drop secrets never stops growing. The one exception is the native
    agent memory (`.claude/projects/<slug>/memory/`): agents report those paths constantly;
  * excluded dirs (.git, node_modules, venv …), `.env*` names and key/credential file names
    are denied at any depth;
  * a project cwd that IS $HOME or an ancestor of it grants nothing extra — only the $HOME
    rules apply, otherwise `cwd=/home/x` would reopen `.ssh`.
Writes additionally require an existing regular UTF-8 text file <= MAX_TEXT_BYTES and an
unchanged mtime (the agent edits the same files the operator does).
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

MAX_TEXT_BYTES = 1 * 1024 * 1024
MAX_ENTRIES = 3000

# Two saves must not interleave their check-then-replace (the routes run this in a thread).
_WRITE_LOCK = threading.Lock()

EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", ".worktrees", ".mypy_cache", ".pytest_cache",
})

_SECRET_EXACT: frozenset[str] = frozenset({
    ".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc",
    "credentials.json", ".credentials.json", "secret.key",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})
_SECRET_SUFFIX: tuple[str, ...] = (".pem", ".p12", ".pfx", ".kdbx")


class FsError(Exception):
    """A refusal with an HTTP-ish status; the message is safe to show the operator."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_secret_name(name: str) -> bool:
    if name.startswith(".env") and name != ".env.example":
        return True
    low = name.lower()
    return low in _SECRET_EXACT or low.endswith(_SECRET_SUFFIX)


# ── roots ─────────────────────────────────────────────────────────────────────

class Roots:
    """The set of places the explorer may show, resolved once per request."""

    def __init__(self, home: Path, extras: list[Path], project_cwd: Optional[Path] = None):
        self.home = home.resolve()
        plain: list[Path] = []
        for p in extras:
            try:
                plain.append(p.resolve())
            except OSError:
                continue
        if project_cwd is not None:
            try:
                cwd = project_cwd.resolve()
            except OSError:
                cwd = None
            # Only a cwd STRICTLY below $HOME (or outside it) is its own root; $HOME itself
            # or an ancestor would bypass the dot-entry rule (see module docstring).
            if cwd is not None and cwd != self.home and cwd not in self.home.parents:
                plain.append(cwd)
        self.plain = plain
        self.project_cwd = project_cwd.resolve() if project_cwd is not None else None

    @classmethod
    def build(cls, project_cwd: Optional[str] = None) -> "Roots":
        raw = os.environ.get("FILES_EXTRA_ROOTS")
        raw = "/tmp" if raw is None else raw
        extras = [Path(os.path.expanduser(s)) for s in raw.split(":") if s.strip()]
        return cls(Path.home(), extras, Path(project_cwd) if project_cwd else None)

    def as_list(self) -> list[dict]:
        out = [{"path": str(self.home), "label": "~"}]
        if self.project_cwd is not None:
            out.insert(0, {"path": str(self.project_cwd), "label": "project"})
        for p in self.plain:
            if p != self.project_cwd:
                out.append({"path": str(p), "label": p.name or str(p)})
        return out


def _bad_parts(parts: tuple[str, ...]) -> bool:
    return any(p in EXCLUDE_DIRS or is_secret_name(p) for p in parts)


def _home_rel_ok(parts: tuple[str, ...]) -> bool:
    """The $HOME rules: everything except top-level dot entries and the usual noise."""
    if not parts:
        return True
    top = parts[0]
    if top.startswith("."):
        # native agent memory: ~/.claude/projects/<slug>/memory/…
        if not (top == ".claude" and len(parts) >= 4
                and parts[1] == "projects" and parts[3] == "memory"):
            return False
    return not _bad_parts(parts)


def permitted(p: Path, roots: Roots) -> bool:
    """True when the (already resolved) path may be listed or read."""
    for root in roots.plain:
        if p == root or root in p.parents:
            if not _bad_parts(p.relative_to(root).parts):
                return True
    if p == roots.home or roots.home in p.parents:
        return _home_rel_ok(p.relative_to(roots.home).parts)
    return False


def resolve_checked(raw: str, roots: Roots) -> Path:
    """Resolve an absolute path from the client and refuse it unless permitted."""
    if not raw or "\x00" in raw:
        raise FsError(400, "path required")
    if not raw.startswith("/"):
        raise FsError(400, "absolute path required")
    try:
        p = Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        raise FsError(400, "invalid path")
    if not permitted(p, roots):
        raise FsError(403, "outside the folders the explorer may show")
    return p


# ── pasted-path normalisation ─────────────────────────────────────────────────

_WRAP = "\"'`<>"
_LINE_SUFFIX = re.compile(r"(?::\d+){1,2}$|#L\d+(?:-L?\d+)?$")


def normalise_input(text: str, home: Path) -> str:
    """Turn whatever the operator pasted into a bare path string (possibly relative).

    Agents print paths as `code`, "quoted", (parenthesised), [md](links), file:// URLs,
    `path:12:3`, `path#L12`, `~/x`, `$HOME/x`, with trailing punctuation.
    """
    line = ""
    for ln in (text or "").splitlines():
        if ln.strip():
            line = ln.strip()
            break
    if not line:
        return ""
    md = re.match(r"^\[[^\]]*\]\(([^)]+)\)", line)
    if md:
        line = md.group(1).strip()
    for _ in range(3):
        stripped = line.strip(_WRAP + " \t")
        if stripped.startswith("(") and stripped.endswith(")"):
            stripped = stripped[1:-1]
        stripped = stripped.rstrip(".,;)")
        if stripped == line:
            break
        line = stripped
    if line.startswith("file://"):
        line = unquote(line[len("file://"):])
    line = _LINE_SUFFIX.sub("", line)
    line = unicodedata.normalize("NFC", line)
    if line == "~" or line.startswith("~/"):
        line = str(home) + line[1:]
    elif line == "$HOME" or line.startswith("$HOME/"):
        line = str(home) + line[len("$HOME"):]
    return line


def stat_input(raw: str, roots: Roots, base: Optional[str] = None) -> dict:
    """Classify a pasted path: {input, path, kind, nearest?}.

    kind: dir | file | missing | denied. The text is tried AS TYPED first (quotes, colons and
    `#` are legal in file names), then in its cleaned-up form (see normalise_input). A relative
    input is tried against `base`, then the project cwd, then $HOME. `nearest` is the deepest
    existing permitted ancestor of a missing path, so a typo or a deleted file still lands the
    operator close.
    """
    typed = (raw or "").strip()
    if typed == "~" or typed.startswith("~/"):
        typed = str(roots.home) + typed[1:]
    texts: list[str] = []
    for t in (typed, normalise_input(raw, roots.home)):
        if t and t not in texts:
            texts.append(t)
    if not texts:
        return {"input": raw, "path": None, "kind": "missing"}
    anchors = [a for a in (base, str(roots.project_cwd) if roots.project_cwd else None,
                           str(roots.home)) if a]
    candidates: list[str] = []
    for t in texts:
        if t.startswith("/"):
            candidates.append(t)
        else:
            candidates.extend(os.path.join(a, t) for a in anchors)
    denied = False
    fallback: Optional[str] = None
    for cand in candidates:
        try:
            p = Path(cand).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not permitted(p, roots):
            denied = True
            continue
        if p.is_dir():
            return {"input": raw, "path": str(p), "kind": "dir"}
        if p.is_file():
            return {"input": raw, "path": str(p), "kind": "file"}
        if fallback is None:
            fallback = str(p)
    if fallback is None:
        return {"input": raw, "path": None, "kind": "denied" if denied else "missing"}
    q = Path(fallback)
    while q != q.parent:
        q = q.parent
        if q.is_dir() and permitted(q, roots):
            return {"input": raw, "path": fallback, "kind": "missing", "nearest": str(q)}
    return {"input": raw, "path": fallback, "kind": "missing"}


# ── listing ───────────────────────────────────────────────────────────────────

def _crumbs(p: Path, roots: Roots) -> list[dict]:
    out = []
    cur = Path("/")
    out.append({"name": "/", "path": "/", "ok": permitted(cur, roots)})
    for part in p.parts[1:]:
        cur = cur / part
        out.append({"name": part, "path": str(cur), "ok": permitted(cur, roots)})
    return out


def list_dir(path: str, roots: Roots) -> dict:
    p = resolve_checked(path, roots)
    if not p.is_dir():
        raise FsError(404, "not a directory")
    entries: list[dict] = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_symlink():
                        target = Path(e.path).resolve()
                        if not permitted(target, roots):
                            continue
                    if not permitted(p / e.name, roots):
                        continue
                    if e.is_dir():
                        entries.append({"name": e.name, "type": "dir", "size": 0})
                    elif e.is_file():  # sockets, FIFOs and devices are not files to the explorer
                        entries.append({"name": e.name, "type": "file", "size": e.stat().st_size})
                except OSError:
                    continue
    except PermissionError:
        raise FsError(403, "permission denied")
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    truncated = len(entries) > MAX_ENTRIES
    parent = p.parent if p.parent != p else None
    return {
        "path": str(p),
        "parent": str(parent) if parent is not None and permitted(parent, roots) else None,
        "crumbs": _crumbs(p, roots),
        "entries": entries[:MAX_ENTRIES],
        "truncated": truncated,
    }


# ── read / write ──────────────────────────────────────────────────────────────

def _rev(st: os.stat_result) -> str:
    """Opaque revision of a file's on-disk state. A STRING on purpose: st_mtime_ns is ~1.7e18,
    past 2^53, so a JSON number would be rounded by JavaScript and never match again."""
    return f"{st.st_mtime_ns}:{st.st_size}"


def _snapshot(p: Path) -> tuple[bytes, os.stat_result]:
    """Bytes and revision from the SAME open file, so they cannot describe two versions."""
    try:
        with open(p, "rb") as f:
            st = os.fstat(f.fileno())
            raw = f.read(MAX_TEXT_BYTES + 1)
    except OSError:
        raise FsError(500, "read failed")
    return raw, st


def read_file(path: str, roots: Roots) -> dict:
    p = resolve_checked(path, roots)
    if not p.is_file():
        raise FsError(404, "not a file")
    raw, st = _snapshot(p)
    out = {
        "path": str(p),
        "lang": p.suffix.lstrip(".") if p.suffix else "",
        "size": st.st_size,
        "rev": _rev(st),
    }
    if st.st_size > MAX_TEXT_BYTES or len(raw) > MAX_TEXT_BYTES:
        return {**out, "content": "", "error": "file too large"}
    if b"\x00" in raw[:8192]:
        return {**out, "content": "", "error": "binary file"}
    try:
        text = raw.decode("utf-8")
        editable = True
    except UnicodeDecodeError:
        # Saving the U+FFFD-replaced text would corrupt the file: view only.
        text = raw.decode("utf-8", errors="replace")
        editable = False
    return {**out, "content": text.replace("\r\n", "\n"), "editable": editable}


def write_file(path: str, content: str, base_rev: Optional[str], roots: Roots,
               force: bool = False) -> dict:
    p = resolve_checked(path, roots)
    if not p.is_file():
        raise FsError(404, "not a file")
    data = content.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise FsError(413, "file too large to edit")
    with _WRITE_LOCK:
        old, st = _snapshot(p)
        if st.st_size > MAX_TEXT_BYTES:
            raise FsError(413, "file too large to edit")
        if b"\x00" in old[:8192]:
            raise FsError(415, "binary file")
        try:
            old.decode("utf-8")
        except UnicodeDecodeError:
            raise FsError(415, "not valid UTF-8; refusing to rewrite it")
        if not force:
            if not base_rev:
                raise FsError(400, "base_rev required")
            if _rev(st) != base_rev:
                raise FsError(409, "changed on disk since it was opened")
        if b"\r\n" in old and "\r" not in content:
            data = content.replace("\n", "\r\n").encode("utf-8")
        try:
            fd, tmp = tempfile.mkstemp(prefix=".cardloop-save-", dir=str(p.parent))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.chmod(tmp, st.st_mode & 0o7777)
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            # Directory not writable but the file is: write in place instead.
            try:
                p.write_bytes(data)
            except OSError as e:
                raise FsError(500, f"write error: {e.strerror or e}")
        return {"ok": True, "path": str(p), "rev": _rev(p.stat()), "size": len(data)}

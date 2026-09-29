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

Two more surfaces share the same policy: `raw` (bytes for the image / PDF / video / audio
previews and for downloads) and `recent` (the files the agent just wrote, plus whatever changed
on disk in the project folder). Both go through resolve_checked / permitted — nothing here has
its own idea of what is reachable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

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


# The cockpit's own state: sessions, the encrypted safe, the Web Push private key, the touched-file
# log (which names paths the policy denies), search/usage databases. Not the operator's files, and
# `raw` would serve any of it up to 100 MB. Only `data/inbox/` — the files the operator uploaded
# into a chat — stays browsable. Tests move DATA_DIR.
DATA_DIR = Path(__file__).resolve().parent / "data"
_DATA_ALLOWED = frozenset({"inbox"})


def _cockpit_private(p: Path) -> bool:
    """True for the cockpit's data dir (outside `inbox`) and for a relocated secret store / key."""
    try:
        if p == DATA_DIR or DATA_DIR in p.parents:
            rel = p.relative_to(DATA_DIR).parts
            if not rel or rel[0] not in _DATA_ALLOWED:
                return True
    except ValueError:
        pass
    for var in ("CLAUDE_OPS_SECRET_STORE", "CLAUDE_OPS_SECRET_KEYFILE"):
        raw = os.environ.get(var)
        if raw:
            try:
                if p == Path(os.path.expanduser(raw)).resolve():
                    return True
            except (OSError, RuntimeError):
                continue
    return False


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
                r = p.resolve()
            except OSError:
                continue
            # An extra root that IS $HOME or above it would bypass the dot-entry rule (an extra
            # root has none) and reopen .ssh — same reasoning as the project cwd below.
            if r == self.home or r in self.home.parents:
                continue
            plain.append(r)
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
        # A cwd that IS $HOME (or is unreachable, e.g. above it) would only add a dead or
        # duplicate shortcut.
        if (self.project_cwd is not None and self.project_cwd != self.home
                and permitted(self.project_cwd, self)):
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
    if _cockpit_private(p):
        return False
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
    past 2^53, so a JSON number would be rounded by JavaScript and never match again. The inode
    is in it because an atomic rewrite (ours, or an agent's) swaps it, which catches a same-size
    rewrite inside one clock tick that mtime + size alone would miss."""
    return f"{st.st_mtime_ns}:{st.st_size}:{st.st_ino}"


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
    kind = preview_kind(p)
    if kind is not None:
        # Shown by the browser from /api/fs/raw — the bytes never go through the text pipeline.
        st = p.stat()
        return {"path": str(p), "lang": p.suffix.lstrip("."), "size": st.st_size, "rev": _rev(st),
                "kind": kind, "content": "", "editable": False}
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
        editable = os.access(p, os.W_OK)  # a read-only file is shown, not offered for editing
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
        if not os.access(p, os.W_OK):
            raise FsError(403, "file is read-only")
        if b"\r\n" in old and "\r" not in content:
            data = content.replace("\n", "\r\n").encode("utf-8")
        try:
            fd, tmp = tempfile.mkstemp(prefix=".cardloop-save-", dir=str(p.parent))
        except OSError:
            # The FOLDER is not writable but the file is: overwrite in place. Only this case —
            # once a temp file exists, a failure must leave the original untouched (a fallback
            # that truncated it on ENOSPC is how a full disk used to eat the operator's file).
            try:
                with open(p, "r+b") as f:
                    f.write(data)
                    f.truncate(len(data))
                    f.flush()
                    new_rev = _rev(os.fstat(f.fileno()))
            except OSError as e:
                raise FsError(500, f"write error: {e.strerror or e}")
            return {"ok": True, "path": str(p), "rev": new_rev, "size": len(data)}
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, st.st_mode & 0o7777)
            # A rename keeps the inode and mtime, so this is the revision the file will have.
            new_rev = _rev(os.stat(tmp))
            os.replace(tmp, p)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise FsError(500, f"write error: {e.strerror or e}")
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return {"ok": True, "path": str(p), "rev": new_rev, "size": len(data)}


# ── raw bytes: previews and downloads ─────────────────────────────────────────

RAW_MAX_BYTES = 100 * 1024 * 1024

# Extension -> MIME for the types the explorer previews itself. An explicit table, not the
# system mime database: a preview must not depend on which /etc/mime.types this host has.
_PREVIEW: dict[str, tuple[str, str]] = {
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"), ".avif": ("image", "image/avif"),
    ".bmp": ("image", "image/bmp"), ".ico": ("image", "image/x-icon"), ".svg": ("image", "image/svg+xml"),
    ".pdf": ("pdf", "application/pdf"),
    ".mp4": ("video", "video/mp4"), ".m4v": ("video", "video/mp4"), ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"), ".ogv": ("video", "video/ogg"),
    ".mp3": ("audio", "audio/mpeg"), ".wav": ("audio", "audio/wav"), ".ogg": ("audio", "audio/ogg"),
    ".oga": ("audio", "audio/ogg"), ".m4a": ("audio", "audio/mp4"), ".flac": ("audio", "audio/flac"),
    ".aac": ("audio", "audio/aac"),
}


def preview_kind(p: Path) -> Optional[str]:
    """image | pdf | video | audio for the types the explorer can show, else None."""
    hit = _PREVIEW.get(p.suffix.lower())
    return hit[0] if hit else None


def open_raw(path: str, roots: Roots, download: bool = False) -> tuple[Path, str, dict]:
    """Resolve a file for streaming. Returns (path, mime, response headers).

    Only the previewable types are ever served inline; everything else is a forced download as
    octet-stream. The headers matter as much as the policy: an SVG opened by URL would run its
    scripts on the cockpit's origin with the operator's cookie, so images and media carry
    `Content-Security-Policy: sandbox`. A PDF cannot (Chrome refuses to render a PDF under a
    sandbox CSP), and the cockpit's blanket `X-Frame-Options: DENY` would stop it being framed,
    so it gets SAMEORIGIN instead; a PDF's own scripts run in the browser's PDF viewer, not on
    our origin.
    """
    p = resolve_checked(path, roots)
    if not p.is_file():
        raise FsError(404, "not a file")
    try:
        size = p.stat().st_size
    except OSError:
        raise FsError(500, "stat failed")
    if size > RAW_MAX_BYTES:
        raise FsError(413, f"file too large to serve ({size // (1024 * 1024)} MB)")
    kind = preview_kind(p)
    headers = {"Cache-Control": "private, no-cache"}
    inline = kind is not None and not download
    mime = _PREVIEW[p.suffix.lower()][1] if inline else "application/octet-stream"
    if inline:
        headers["Content-Disposition"] = "inline"
        if kind == "pdf":
            headers["X-Frame-Options"] = "SAMEORIGIN"
        else:
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; media-src 'self'"
    else:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(p.name)}"
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return p, mime, headers


# ── recent: what the agent just wrote, what just changed ──────────────────────

_TOUCH_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
_TOUCH_KEEP = 500
_TOUCH_LOCK = threading.Lock()


def touched_path(tool: str, tool_input: object, cwd: str) -> Optional[str]:
    """The absolute file a Write/Edit-style tool call is about to change, else None."""
    key = _TOUCH_TOOLS.get(tool)
    if not key or not isinstance(tool_input, dict):
        return None
    raw = tool_input.get(key)
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    return raw if raw.startswith("/") else os.path.join(cwd, raw)


def _touch_file(data_dir: Path, cwd: str) -> Path:
    try:
        real = str(Path(cwd).resolve())
    except OSError:
        real = cwd
    return data_dir / "touched" / (hashlib.sha1(real.encode()).hexdigest()[:12] + ".jsonl")


def record_touched(data_dir: Path, cwd: str, tool: str, tool_input: object) -> None:
    """Remember that the agent is writing a file. Best effort: never raises into the run."""
    try:
        p = touched_path(tool, tool_input, cwd)
        if p is None:
            return
        f = _touch_file(data_dir, cwd)
        line = json.dumps({"t": time.time(), "p": p, "tool": tool}) + "\n"
        with _TOUCH_LOCK:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(line)
            # Trim occasionally (not on every write): keep the newest _TOUCH_KEEP entries.
            if f.stat().st_size > 200_000:
                keep = f.read_text(encoding="utf-8", errors="replace").splitlines()[-_TOUCH_KEEP:]
                tmp = f.with_suffix(".tmp")
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                os.replace(tmp, f)
    except Exception:
        pass


def _read_touched(data_dir: Path, cwd: str) -> list[dict]:
    f = _touch_file(data_dir, cwd)
    out: list[dict] = []
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("p"), str) and isinstance(d.get("t"), (int, float)):
                    out.append(d)
    except OSError:
        return []
    return out[-_TOUCH_KEEP:]


def _scan_recent(cwd: Path, roots: Roots, since: float, cap: int, budget_s: float) -> list[tuple[float, Path]]:
    """Files under `cwd` changed since `since`, newest first — the ones a shell wrote, which the
    tool log cannot know about. Bounded: prunes excluded dirs, stops at the time budget."""
    found: list[tuple[float, Path]] = []
    deadline = time.monotonic() + budget_s
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not is_secret_name(d)]
        for name in filenames:
            scanned += 1
            if scanned > 40_000 or time.monotonic() > deadline:
                return sorted(found, reverse=True)[:cap]
            fp = Path(dirpath) / name
            try:
                st = os.lstat(fp)
                if stat.S_ISLNK(st.st_mode):
                    # A link is judged by where it points: list_dir hides links that leave the roots
                    # and so must this, or the name/size/mtime of a denied target would leak here.
                    fp = fp.resolve()
                    st = os.stat(fp)
            except (OSError, RuntimeError):
                continue
            if st.st_mtime >= since and stat.S_ISREG(st.st_mode) and permitted(fp, roots):
                found.append((st.st_mtime, fp))
    return sorted(found, reverse=True)[:cap]


def recent_files(data_dir: Path, cwd: str, roots: Roots, limit: int = 40,
                 window_s: float = 48 * 3600, disk_cap: int = 25, scan_budget_s: float = 1.0) -> list[dict]:
    """Files the operator probably wants to open: the agent's Write/Edit targets (anywhere the
    explorer may show — including a report dropped in /tmp) merged with anything that changed
    on disk in the project folder within `window_s`. Newest first, one row per file.
    `src` says which: "agent" (a tool wrote it) or "disk" (only its mtime says so)."""
    rows: dict[str, dict] = {}
    for d in _read_touched(data_dir, cwd):
        try:
            p = Path(d["p"]).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not permitted(p, roots) or not p.is_file():
            continue
        prev = rows.get(str(p))
        if prev is None or d["t"] > prev["t"]:
            rows[str(p)] = {"path": str(p), "t": float(d["t"]), "src": "agent", "tool": d.get("tool", "")}
    try:
        base = Path(cwd).resolve()
    except OSError:
        base = None
    if base is not None and base.is_dir():
        for mtime, fp in _scan_recent(base, roots, time.time() - window_s, disk_cap, scan_budget_s):
            key = str(fp)
            if key in rows:
                rows[key]["t"] = max(rows[key]["t"], mtime)
            else:
                rows[key] = {"path": key, "t": mtime, "src": "disk", "tool": ""}
    out = sorted(rows.values(), key=lambda r: r["t"], reverse=True)[:limit]
    for r in out:
        try:
            r["size"] = Path(r["path"]).stat().st_size
        except OSError:
            r["size"] = 0
        r["name"] = os.path.basename(r["path"])
    return out

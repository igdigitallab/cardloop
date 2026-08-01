"""search.py — Global search index (spec-074): SQLite FTS5 over chat transcripts,
timelines, and kanban boards. The daily-use "second brain" — one search box over
everything the operator has ever discussed or planned across every project.

Design:
- Pure stdlib (sqlite3/json/re/pathlib). webapp.py imports THIS module — never the
  reverse — so the indexer stays unit-testable in isolation with tmp fixtures.
- Reuses board.py's card-line parsing (_CARD_RE / _PLAIN_CARD_RE / _extract_id_and_text)
  so a card's indexed text always matches what the board actually renders — no
  re-implementation of the card-line grammar here.
- webapp.py resolves cwd -> transcript dir / timeline path / board paths using its
  OWN helpers (_sdk_sessions_dir, _timeline_slug_from_cwd, _tasks_path, _done_path)
  and hands this module plain source descriptors ({project_id, project_name, ...}).
  This module never guesses a path layout on its own.
- Connections are opened per call (mirrors usage_scanner.py's proven pattern) and
  closed before returning, so the actual DB work always happens on the SAME thread
  that opened the connection — regardless of which executor thread aiohttp's
  run_in_executor happens to schedule it on. No shared cross-thread connection,
  no check_same_thread=False needed. PRAGMA busy_timeout absorbs the rare overlap
  between an indexer tick and a manual reindex (both writers).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import board as _board  # reuse card-line regex + ops-marker stripping — no re-implementation

# ─────────────────────────── tunables ───────────────────────────

# Bumping this drops + rebuilds the whole index on the next init_db(). That is the ONLY
# supported way to change the docs schema: FTS5 has no ALTER TABLE ADD COLUMN, and a stale
# index silently answers queries with the old shape (missing anchors → hits that look fine
# but cannot be opened). One rebuild costs a single background scan pass.
SCHEMA_VERSION = 4

BODY_CHAR_CAP = 2000                   # ~2KB cap per indexed doc body (spec-074)
MAX_FILE_BYTES = 50 * 1024 * 1024      # skip source files bigger than this

# spec-079 A2: long bodies are SPLIT, not cut. Truncating at BODY_CHAR_CAP made the tail of
# a long message permanently unfindable (7.5% of the live index was sitting at the cap).
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150                    # keeps a match that straddles a boundary findable

# spec-079 A3: project files. Two tiers — prose is what the operator searches for ("where did
# we write that down"), code is supporting evidence and is ranked below it.
DOC_EXTS = {".md", ".txt", ".rst", ".adoc"}
CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".sh", ".sql", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".json", ".css", ".scss", ".html", ".vue", ".svelte",
    ".go", ".rs", ".java", ".rb", ".php", ".swift", ".kt", ".c", ".h", ".cpp", ".hpp",
}
# Mirrors the cockpit's own file-reading cap (webapp._read_file_content) — if the UI will
# not open it, indexing it only produces hits that cannot be viewed.
MAX_INDEX_FILE_BYTES = 1024 * 1024
# Above this many candidate files in one project, the code tier is dropped and only prose is
# indexed. Reported in the scan stats — never a silent truncation.
MAX_PROJECT_FILES = 3000
RECENCY_WEIGHT = 1e-9                  # small nudge so newer docs edge out older ties in bm25 order
DEFAULT_LIMIT = 30
MAX_LIMIT = 100

# Snippet delimiters: NOT literal '<mark>' — these are private-use control chars
# (SOH/STX) chosen so they can never collide with real chat/board/timeline text.
# The frontend splits on these and renders a real <mark> React element, so a
# document that happens to contain literal HTML is never dangerouslySetInnerHTML'd.
SNIPPET_OPEN = "\x01"
SNIPPET_CLOSE = "\x02"

_WS_RE = re.compile(r"\s+")


# ─────────────────────────── schema / connection ───────────────────────────

def db_path_for(data_dir: Path) -> Path:
    return Path(data_dir) / "search.db"


def get_db(db_path: "Path | str") -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_CREATE_SQL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
        project_id UNINDEXED,
        project_name UNINDEXED,
        source UNINDEXED,
        ts UNINDEXED,
        ref UNINDEXED,
        ref2 UNINDEXED,
        path UNINDEXED,
        line UNINDEXED,
        tier UNINDEXED,
        title,
        body,
        tokenize="unicode61 remove_diacritics 2"
    );
    CREATE TABLE IF NOT EXISTS file_state (
        path    TEXT PRIMARY KEY,
        mtime   REAL,
        size    INTEGER,
        offset  INTEGER
    );
    -- spec-079: rowid ↔ (path, source, project) side index. An FTS5 table has no B-tree on
    -- its UNINDEXED columns, so "DELETE FROM docs WHERE path = ?" is a FULL SCAN — and the
    -- indexer issues one per changed file. Measured: indexing 571 files into a 12k-row table
    -- took 7.4s and grew superlinearly, which is what made a first file scan never finish.
    -- Deleting by rowid through this table is O(log n).
    CREATE TABLE IF NOT EXISTS doc_rows (
        doc_rowid  INTEGER PRIMARY KEY,
        path       TEXT NOT NULL,
        source     TEXT NOT NULL,
        project_id TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_doc_rows_path ON doc_rows(path);
    CREATE INDEX IF NOT EXISTS idx_doc_rows_scope ON doc_rows(source, project_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to call before every operation (search/scan/reindex).

    Also the migration gate: an index written by an older SCHEMA_VERSION is dropped and
    rebuilt from scratch. Dropping file_state alongside docs is what makes the rebuild
    actually happen — the indexer resumes from stored byte offsets, so leaving the state
    behind would leave the fresh table permanently empty."""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    have = None
    if row is not None:
        try:
            have = int(row[0])
        except (TypeError, ValueError):
            have = None
    docs_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs'").fetchone() is not None
    if docs_exists and have != SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS file_state; DROP TABLE IF EXISTS doc_rows;")
    conn.executescript(_CREATE_SQL)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    """Drops all indexed content + file state for a full rebuild (POST /api/search/reindex)."""
    conn.executescript("""
        DROP TABLE IF EXISTS docs;
        DROP TABLE IF EXISTS file_state;
        DROP TABLE IF EXISTS doc_rows;
    """)
    conn.commit()
    init_db(conn)


# ─────────────────────────── helpers ───────────────────────────

def _parse_iso_ts(ts) -> float:
    """ISO8601 transcript timestamp -> epoch seconds. 0.0 on anything unparsable."""
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _extract_text_blocks(content) -> str:
    """A transcript message.content is either a plain string (typical user turn) or a
    list of blocks (assistant turns, and user turns that carry tool_result/attachments).
    Only 'text' blocks / plain strings are chat content — tool_use/tool_result/thinking
    blocks are agent plumbing, never indexed."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                v = block.get("text")
                if isinstance(v, str):
                    parts.append(v)
        return "\n".join(parts)
    return ""


def _is_sidechain_record(record: dict) -> bool:
    """True for sub-agent traffic that must never leak into the main-chat index.
    Checks every plausible key spelling defensively (camelCase is the on-disk schema
    today; snake_case is guarded too in case a future schema version uses it)."""
    return bool(
        record.get("isSidechain")
        or record.get("parentToolUseId")
        or record.get("parent_tool_use_id")
    )


def _file_state(conn: sqlite3.Connection, path: str) -> "sqlite3.Row | None":
    return conn.execute(
        "SELECT mtime, size, offset FROM file_state WHERE path = ?", (path,)
    ).fetchone()


def _save_file_state(conn: sqlite3.Connection, path: str, mtime: float, size: int, offset: int) -> None:
    conn.execute(
        "INSERT INTO file_state (path, mtime, size, offset) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, offset=excluded.offset",
        (path, mtime, size, offset),
    )


def _delete_docs_for_path(conn: sqlite3.Connection, path: str) -> None:
    """Removes every chunk indexed from `path`, via the doc_rows side index.

    Never fall back to `DELETE FROM docs WHERE path = ?` — see the doc_rows comment in
    _CREATE_SQL: that predicate cannot use an index and turns a rescan into O(n²)."""
    rowids = [r[0] for r in conn.execute(
        "SELECT doc_rowid FROM doc_rows WHERE path = ?", (path,))]
    for rid in rowids:
        conn.execute("DELETE FROM docs WHERE rowid = ?", (rid,))
    conn.execute("DELETE FROM doc_rows WHERE path = ?", (path,))


def _chunks(text: str):
    """Splits a long body into overlapping windows, yielding (chunk_text, start_line).

    Prefers to break on a newline inside the last quarter of the window so a chunk tends to
    end at a paragraph/line boundary rather than mid-word. `start_line` is 1-based and is what
    a file hit scrolls to. Short bodies yield exactly one chunk — the common case stays free."""
    text = text.strip()
    if not text:
        return
    if len(text) <= CHUNK_CHARS:
        yield text, 1
        return
    pos = 0
    n = len(text)
    while pos < n:
        end = min(n, pos + CHUNK_CHARS)
        if end < n:
            nl = text.rfind("\n", pos + (CHUNK_CHARS * 3) // 4, end)
            if nl > pos:
                end = nl
        piece = text[pos:end].strip()
        if piece:
            yield piece, text.count("\n", 0, pos) + 1
        if end >= n:
            break
        pos = max(end - CHUNK_OVERLAP, pos + 1)


def _insert_doc(conn: sqlite3.Connection, project_id: str, project_name: str,
                 source: str, ts: float, ref: str, path: str, body: str,
                 ref2: str = "", title: str = "", tier: str = "",
                 line_base: int = 0) -> int:
    """Inserts one logical document, split into chunks so nothing is silently truncated.

    `ref`/`ref2` are the deep-link anchors, interpreted per source:
    chat → (session_id, message uuid) · board → (card_id, '') · timeline → ('', '') ·
    file → (repo-relative path, ''). Returns the number of rows written."""
    written = 0
    for piece, start_line in _chunks(body):
        cur = conn.execute(
            "INSERT INTO docs (project_id, project_name, source, ts, ref, ref2, path, "
            "line, tier, title, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, project_name, source, ts, ref, ref2, path,
             line_base + start_line - 1 if line_base else start_line,
             tier, title, piece),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_rows (doc_rowid, path, source, project_id) "
            "VALUES (?, ?, ?, ?)",
            (cur.lastrowid, path, source, project_id),
        )
        written += 1
    return written


# ─────────────────────────── source 1: chat transcripts ───────────────────────────

def index_transcripts(conn: sqlite3.Connection, project_id: str, project_name: str,
                       sdk_dir: Path) -> dict:
    """Indexes a project's top-level SDK transcript files: <sdk_dir>/<sid>.jsonl only.
    Sub-agent transcripts live one level deeper at <sdk_dir>/<sid>/subagents/*.jsonl —
    a non-recursive glob here never sees them, so 'skip subagents/ entirely' is
    structural, not a filter that can be forgotten."""
    sdk_dir = Path(sdk_dir)
    if not sdk_dir.exists():
        return {"files": 0, "docs": 0}
    files_scanned = 0
    docs_added = 0
    for path in sorted(sdk_dir.glob("*.jsonl")):
        if not path.is_file():
            continue
        files_scanned += 1
        docs_added += _index_one_transcript(conn, project_id, project_name, path)
    return {"files": files_scanned, "docs": docs_added}


def _index_one_transcript(conn: sqlite3.Connection, project_id: str, project_name: str,
                           path: Path) -> int:
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return 0
    if stat.st_size > MAX_FILE_BYTES:
        return 0

    row = _file_state(conn, key)
    start_offset = 0
    if row is not None:
        prev_size = row["size"] or 0
        if stat.st_size < prev_size:
            # File shrank (rotation/truncation) — the old offset is meaningless; drop
            # what we had for this path and rescan from the top.
            _delete_docs_for_path(conn, key)
        elif stat.st_size == prev_size and abs((row["mtime"] or 0) - stat.st_mtime) < 1e-6:
            return 0  # unchanged since last scan
        else:
            start_offset = row["offset"] or 0

    added = 0
    end_offset = start_offset
    with open(path, "rb") as f:
        f.seek(start_offset)
        while True:
            raw = f.readline()
            if not raw:
                break
            end_offset = f.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_sidechain_record(record):
                continue
            rtype = record.get("type")
            if rtype not in ("user", "assistant"):
                continue
            msg = record.get("message")
            if not isinstance(msg, dict):
                continue
            text = _extract_text_blocks(msg.get("content")).strip()
            if not text:
                continue
            session_id = record.get("sessionId") or path.stem
            ts = _parse_iso_ts(record.get("timestamp"))
            # The line's own uuid is the exact scroll anchor for the chat feed. It is stored
            # for both roles even though the feed currently exposes uuid on user messages
            # only — an assistant hit degrades to nearest-ts, and starts working for free the
            # day the feed carries assistant uuids too.
            uuid = record.get("uuid") or ""
            added += _insert_doc(conn, project_id, project_name, "chat", ts, session_id,
                                 key, text, ref2=uuid if isinstance(uuid, str) else "")

    _save_file_state(conn, key, stat.st_mtime, stat.st_size, end_offset)
    conn.commit()
    return added


# ─────────────────────────── source 2: timelines ───────────────────────────

def index_timeline_file(conn: sqlite3.Connection, project_id: str, project_name: str,
                         path: Path) -> dict:
    """Indexes data/timeline/<slug>.jsonl — {kind:'text', text:...} rows only.
    Append-only + byte-offset resume, same shrink-detection as transcripts (the
    timeline file is itself rotated to .jsonl.1 by webapp.py past 5MB — a shrink
    here means 'started fresh after rotation', so history in the .1 backup is
    intentionally not re-indexed, matching what the Timeline UI itself shows)."""
    path = Path(path)
    if not path.exists():
        return {"files": 0, "docs": 0}
    try:
        stat = path.stat()
    except OSError:
        return {"files": 0, "docs": 0}
    if stat.st_size > MAX_FILE_BYTES:
        return {"files": 1, "docs": 0}

    key = str(path)
    row = _file_state(conn, key)
    start_offset = 0
    if row is not None:
        prev_size = row["size"] or 0
        if stat.st_size < prev_size:
            start_offset = 0
            _delete_docs_for_path(conn, key)
        elif stat.st_size == prev_size and abs((row["mtime"] or 0) - stat.st_mtime) < 1e-6:
            return {"files": 1, "docs": 0}
        else:
            start_offset = row["offset"] or 0

    added = 0
    end_offset = start_offset
    with open(path, "rb") as f:
        f.seek(start_offset)
        while True:
            raw = f.readline()
            if not raw:
                break
            end_offset = f.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") != "text":
                continue
            text = record.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                ts = float(record.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            added += _insert_doc(conn, project_id, project_name, "timeline", ts, "", key,
                                 text.strip())

    _save_file_state(conn, key, stat.st_mtime, stat.st_size, end_offset)
    conn.commit()
    return {"files": 1, "docs": added}


# ─────────────────────────── source 3: boards (TASKS.md / DONE.md) ───────────────────────────

def _iter_card_lines(raw_text: str):
    """Yields (card_id, text) for every recognised card line, reusing board.py's own
    regexes/marker-stripping so indexed text is byte-identical to what the board renders.

    Gating mirrors board.py's _parse_tasks: a card line only counts inside a recognised
    '## <Column>' section — otherwise a preamble bullet in TASKS.md (e.g. a plain '- note'
    before the first section) would be misindexed as a card. DONE.md has no section
    headers at all (it's a flat append-only archive — see board.py's own comment), so a
    file with zero recognised headers is treated as one implicit section covering every line."""
    lines = raw_text.splitlines()
    has_sections = any(
        ln.strip().startswith("##") and ln.strip().lstrip("#").strip().lower() in _board._LABEL_TO_COL
        for ln in lines
    )
    in_section = not has_sections
    for line in lines:
        h = line.strip()
        if h.startswith("##"):
            in_section = h.lstrip("#").strip().lower() in _board._LABEL_TO_COL
            continue
        if not in_section:
            continue
        m = _board._CARD_RE.match(line)
        rest = m.group(2) if m else None
        if rest is None:
            pm = _board._PLAIN_CARD_RE.match(line)
            rest = pm.group(1) if pm else None
        if rest is None:
            continue
        card_id, text = _board._extract_id_and_text(rest)
        text = text.strip()
        if text:
            yield card_id, text


def index_board_file(conn: sqlite3.Connection, project_id: str, project_name: str,
                      path: Path) -> dict:
    """Reindexes a whole TASKS.md/DONE.md on mtime change (boards are rewritten in
    full on every edit, not appended — so, unlike transcripts/timelines, there is no
    stable byte offset to resume from; delete-then-reinsert is correct here)."""
    path = Path(path)
    key = str(path)
    if not path.exists():
        # Board was deleted (or never existed) — drop any stale docs/state for it.
        if _file_state(conn, key) is not None:
            _delete_docs_for_path(conn, key)
            conn.execute("DELETE FROM file_state WHERE path = ?", (key,))
            conn.commit()
        return {"files": 0, "docs": 0}
    try:
        stat = path.stat()
    except OSError:
        return {"files": 0, "docs": 0}
    if stat.st_size > MAX_FILE_BYTES:
        return {"files": 1, "docs": 0}

    row = _file_state(conn, key)
    if row is not None and abs((row["mtime"] or 0) - stat.st_mtime) < 1e-6 and (row["size"] or 0) == stat.st_size:
        return {"files": 1, "docs": 0}  # unchanged

    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"files": 1, "docs": 0}

    _delete_docs_for_path(conn, key)
    added = 0
    for card_id, text in _iter_card_lines(raw_text):
        added += _insert_doc(conn, project_id, project_name, "board", stat.st_mtime, card_id,
                             key, text)

    _save_file_state(conn, key, stat.st_mtime, stat.st_size, 0)
    conn.commit()
    return {"files": 1, "docs": added}


# ─────────────────────────── source 4: project files (spec-079 A3) ───────────────────────────

def _tier_for(name: str, index_code: bool) -> str:
    """'doc' | 'code' | '' (skip). Extension-based; the caller decides whether code counts."""
    suffix = Path(name).suffix.lower()
    if suffix in DOC_EXTS:
        return "doc"
    if index_code and suffix in CODE_EXTS:
        return "code"
    return ""


def _walk_candidates(root: Path, exclude_dirs: set, is_secret, index_code: bool) -> list:
    """Returns [(path, tier)] for everything indexable under root.

    Note it does NOT prune every dotted directory: `.claude-ops/memory/` is exactly the kind
    of curated prose this feature exists to surface. Only the caller's explicit exclude set
    (webapp's own _FS_EXCLUDE_DIRS, so the index and the file browser agree on what exists)
    is pruned."""
    out: list = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            if is_secret(name):
                continue
            tier = _tier_for(name, index_code)
            if not tier:
                continue
            out.append((Path(dirpath) / name, tier))
    return out


def index_project_files(conn: sqlite3.Connection, project_id: str, project_name: str,
                        root: "Path | str", exclude_dirs: "set | None" = None,
                        is_secret=None, index_code: bool = True) -> dict:
    """Indexes a project's own files (CLAUDE.md, README, docs, specs, memory articles, code).

    Rewrite-in-place semantics like boards: a changed file is deleted and re-inserted, since
    source files are edited, not appended. Files that have disappeared since the last scan are
    swept — otherwise the index keeps answering with content that no longer exists, which is
    worse than not indexing it at all."""
    root = Path(root)
    exclude_dirs = exclude_dirs if exclude_dirs is not None else set()
    is_secret = is_secret or (lambda _n: False)
    stats = {"files": 0, "docs": 0, "removed": 0, "code_skipped": False}
    if not root.is_dir():
        return stats

    candidates = _walk_candidates(root, exclude_dirs, is_secret, index_code)
    if index_code and len(candidates) > MAX_PROJECT_FILES:
        # Big repo: keep prose (what the operator actually searches for), drop the code tier.
        # Surfaced in the stats rather than silently applied.
        candidates = [(p, t) for p, t in candidates if t == "doc"]
        stats["code_skipped"] = True

    seen: set = set()
    for path, tier in candidates:
        key = str(path)
        seen.add(key)
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_INDEX_FILE_BYTES:
            continue
        stats["files"] += 1

        row = _file_state(conn, key)
        if row is not None and (row["size"] or 0) == stat.st_size \
                and abs((row["mtime"] or 0) - stat.st_mtime) < 1e-6:
            continue  # unchanged

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:8192]:
            continue  # binary despite the extension

        rel = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
        _delete_docs_for_path(conn, key)
        stats["docs"] += _insert_doc(conn, project_id, project_name, "file", stat.st_mtime,
                                     rel, key, text, title=rel, tier=tier)
        _save_file_state(conn, key, stat.st_mtime, stat.st_size, 0)

    # Sweep files that vanished. Scoped to this project's file docs so a project whose root
    # is temporarily unreadable can never wipe another project's rows.
    known = {r[0] for r in conn.execute(
        "SELECT DISTINCT path FROM doc_rows WHERE source = 'file' AND project_id = ?",
        (project_id,))}
    for gone in known - seen:
        _delete_docs_for_path(conn, gone)
        conn.execute("DELETE FROM file_state WHERE path = ?", (gone,))
        stats["removed"] += 1

    conn.commit()
    return stats


# ─────────────────────────── orchestration ───────────────────────────

def scan_all(conn: sqlite3.Connection, chat_sources: list, timeline_sources: list,
             board_sources: list, file_sources: "list | None" = None) -> dict:
    """chat_sources:     [{project_id, project_name, sdk_dir}]
    timeline_sources: [{project_id, project_name, path}]
    board_sources:    [{project_id, project_name, path}]
    file_sources:     [{project_id, project_name, root, exclude_dirs, is_secret, index_code}]
    Incremental — safe to call every tick; already-seen unchanged files are a cheap
    no-op (one file_state lookup each)."""
    stats = {"chat_docs": 0, "timeline_docs": 0, "board_docs": 0, "file_docs": 0,
             "files_removed": 0, "files_scanned": 0, "code_skipped_projects": []}
    for src in chat_sources:
        r = index_transcripts(conn, src["project_id"], src["project_name"], Path(src["sdk_dir"]))
        stats["chat_docs"] += r["docs"]
        stats["files_scanned"] += r["files"]
    for src in timeline_sources:
        r = index_timeline_file(conn, src["project_id"], src["project_name"], Path(src["path"]))
        stats["timeline_docs"] += r["docs"]
        stats["files_scanned"] += r["files"]
    for src in board_sources:
        r = index_board_file(conn, src["project_id"], src["project_name"], Path(src["path"]))
        stats["board_docs"] += r["docs"]
        stats["files_scanned"] += r["files"]
    for src in (file_sources or []):
        r = index_project_files(
            conn, src["project_id"], src["project_name"], src["root"],
            exclude_dirs=src.get("exclude_dirs"), is_secret=src.get("is_secret"),
            index_code=src.get("index_code", True))
        stats["file_docs"] += r["docs"]
        stats["files_removed"] += r["removed"]
        stats["files_scanned"] += r["files"]
        if r["code_skipped"]:
            stats["code_skipped_projects"].append(src["project_id"])
    return stats


def scan_all_at(db_path: "Path | str", chat_sources: list, timeline_sources: list,
                 board_sources: list, file_sources: "list | None" = None) -> dict:
    """One-shot, open-close-per-call entrypoint for webapp.py's run_in_executor calls."""
    conn = get_db(db_path)
    try:
        init_db(conn)
        return scan_all(conn, chat_sources, timeline_sources, board_sources, file_sources)
    finally:
        conn.close()


def full_reindex_at(db_path: "Path | str", chat_sources: list, timeline_sources: list,
                     board_sources: list, file_sources: "list | None" = None) -> dict:
    """Drops + rebuilds the whole index, then VACUUMs. POST /api/search/reindex."""
    conn = get_db(db_path)
    try:
        reset_db(conn)
        stats = scan_all(conn, chat_sources, timeline_sources, board_sources, file_sources)
        conn.execute("VACUUM")
        return stats
    finally:
        conn.close()


# ─────────────────────────── search ───────────────────────────

def _build_match_expr(q: str) -> str:
    """Builds a syntactically-safe FTS5 MATCH expression from free-form user input.
    Every whitespace-separated token becomes an independently double-quoted phrase
    with a trailing prefix wildcard ("token"*) — quoting escapes every FTS5 special
    character (colons, parens, NEAR/AND/OR keywords, unbalanced quotes...) except the
    quote character itself, which is escaped by doubling per the FTS5 string-literal
    rule. A user typing `what's "this"` therefore can never produce invalid syntax."""
    tokens = [tok for tok in _WS_RE.split(q.strip()) if tok]
    parts = [f'"{tok.replace(chr(34), chr(34) * 2)}"*' for tok in tokens]
    return " ".join(parts)


def search(conn: sqlite3.Connection, q: str, limit: int = DEFAULT_LIMIT,
           project_id: "str | None" = None) -> list:
    q = (q or "").strip()
    if not q:
        return []
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    match_expr = _build_match_expr(q)
    if not match_expr:
        return []

    sql = (
        "SELECT project_id, project_name, source, ts, ref, ref2, line, tier, "
        f"snippet(docs, -1, '{SNIPPET_OPEN}', '{SNIPPET_CLOSE}', '…', 12) AS snippet, "
        "bm25(docs) AS rank "
        "FROM docs WHERE docs MATCH ?"
    )
    params: list = [match_expr]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += f" ORDER BY (rank - (ts * {RECENCY_WEIGHT})) ASC LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Should not happen given _build_match_expr's escaping — kept as a hard
        # safety net so a user query can NEVER 500 the endpoint. Degrades to a
        # single quoted phrase (loses per-token AND/prefix semantics, but always
        # syntactically valid FTS5).
        try:
            safe = q.replace('"', '""')
            params[0] = f'"{safe}"'
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

    hits = []
    for r in rows:
        source = r["source"]
        # Deep-link anchors. Board card_id was indexed since spec-074 but never returned,
        # so a board hit could not be opened — the omission, not the data, was the gap.
        ref_obj: dict = {}
        if source == "chat":
            if r["ref"]:
                ref_obj["session_id"] = r["ref"]
            if r["ref2"]:
                ref_obj["uuid"] = r["ref2"]
        elif source == "board" and r["ref"]:
            ref_obj["card_id"] = r["ref"]
        elif source == "file" and r["ref"]:
            # Repo-relative path — the only form the file endpoints accept, and the only
            # form safe to hand to the browser (absolute paths stay server-side).
            ref_obj["path"] = r["ref"]
            if r["line"]:
                ref_obj["line"] = r["line"]
            if r["tier"]:
                ref_obj["tier"] = r["tier"]
        hits.append({
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "source": source,
            "ts": r["ts"],
            "snippet": r["snippet"],
            "ref": ref_obj,
        })
    return hits


def search_at(db_path: "Path | str", q: str, limit: int = DEFAULT_LIMIT,
              project_id: "str | None" = None) -> list:
    conn = get_db(db_path)
    try:
        init_db(conn)
        return search(conn, q, limit=limit, project_id=project_id)
    finally:
        conn.close()

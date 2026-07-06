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
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import board as _board  # reuse card-line regex + ops-marker stripping — no re-implementation

# ─────────────────────────── tunables ───────────────────────────

BODY_CHAR_CAP = 2000                   # ~2KB cap per indexed doc body (spec-074)
MAX_FILE_BYTES = 50 * 1024 * 1024      # skip source files bigger than this
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


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to call before every operation (search/scan/reindex)."""
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
            project_id UNINDEXED,
            project_name UNINDEXED,
            source UNINDEXED,
            ts UNINDEXED,
            ref UNINDEXED,
            path UNINDEXED,
            body,
            tokenize="unicode61 remove_diacritics 2"
        );
        CREATE TABLE IF NOT EXISTS file_state (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            size    INTEGER,
            offset  INTEGER
        );
    """)
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    """Drops all indexed content + file state for a full rebuild (POST /api/search/reindex)."""
    conn.executescript("""
        DROP TABLE IF EXISTS docs;
        DROP TABLE IF EXISTS file_state;
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


def _insert_doc(conn: sqlite3.Connection, project_id: str, project_name: str,
                 source: str, ts: float, ref: str, path: str, body: str) -> None:
    conn.execute(
        "INSERT INTO docs (project_id, project_name, source, ts, ref, path, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, project_name, source, ts, ref, path, body[:BODY_CHAR_CAP]),
    )


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
            conn.execute("DELETE FROM docs WHERE path = ?", (key,))
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
            _insert_doc(conn, project_id, project_name, "chat", ts, session_id, key, text)
            added += 1

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
            conn.execute("DELETE FROM docs WHERE path = ?", (key,))
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
            _insert_doc(conn, project_id, project_name, "timeline", ts, "", key, text.strip())
            added += 1

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
            conn.execute("DELETE FROM docs WHERE path = ?", (key,))
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

    conn.execute("DELETE FROM docs WHERE path = ?", (key,))
    added = 0
    for card_id, text in _iter_card_lines(raw_text):
        _insert_doc(conn, project_id, project_name, "board", stat.st_mtime, card_id, key, text)
        added += 1

    _save_file_state(conn, key, stat.st_mtime, stat.st_size, 0)
    conn.commit()
    return {"files": 1, "docs": added}


# ─────────────────────────── orchestration ───────────────────────────

def scan_all(conn: sqlite3.Connection, chat_sources: list, timeline_sources: list,
             board_sources: list) -> dict:
    """chat_sources:     [{project_id, project_name, sdk_dir}]
    timeline_sources: [{project_id, project_name, path}]
    board_sources:    [{project_id, project_name, path}]
    Incremental — safe to call every tick; already-seen unchanged files are a cheap
    no-op (one file_state lookup each)."""
    stats = {"chat_docs": 0, "timeline_docs": 0, "board_docs": 0, "files_scanned": 0}
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
    return stats


def scan_all_at(db_path: "Path | str", chat_sources: list, timeline_sources: list,
                 board_sources: list) -> dict:
    """One-shot, open-close-per-call entrypoint for webapp.py's run_in_executor calls."""
    conn = get_db(db_path)
    try:
        init_db(conn)
        return scan_all(conn, chat_sources, timeline_sources, board_sources)
    finally:
        conn.close()


def full_reindex_at(db_path: "Path | str", chat_sources: list, timeline_sources: list,
                     board_sources: list) -> dict:
    """Drops + rebuilds the whole index, then VACUUMs. POST /api/search/reindex."""
    conn = get_db(db_path)
    try:
        reset_db(conn)
        stats = scan_all(conn, chat_sources, timeline_sources, board_sources)
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
        "SELECT project_id, project_name, source, ts, ref, "
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
        ref_obj: dict = {}
        if source == "chat" and r["ref"]:
            ref_obj["session_id"] = r["ref"]
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

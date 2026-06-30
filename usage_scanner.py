"""usage_scanner.py - Scan Claude Code JSONL transcripts into SQLite + aggregate.

The complete-picture cost/usage data source for the cockpit Usage dashboard.

Where engine.append_usage_ledger() records only turns that flow through Cardloop's
own run_engine (since the ledger shipped), THIS reads the raw transcripts Claude Code
writes to ~/.claude/projects/**/*.jsonl — every CLI turn, every Cardloop turn, every
dispatched sub-agent, retroactively across all history. Pure standard library
(sqlite3/json/glob); reads transcripts, never writes them.

Scanning + parsing logic ported from phuryn/claude-usage (MIT, (c) 2026 Pawel Huryn):
incremental by (path, mtime, line-count); streaming records deduped by message.id;
sub-agents attributed via isSidechain / agentId / a `subagents/` path, with dispatch
metadata (type, status, duration, tool-use count) lifted from the parent toolUseResult.
The dashboard_data() aggregation + per-row cost (usage_pricing) is Cardloop's own.
"""

from __future__ import annotations

import json
import os
import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

import usage_pricing

# Source of truth for the transcripts. Override with --projects-dir / projects_dir=.
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# DB lives in Cardloop's data dir (gitignored), NOT ~/.claude — keeps our derived
# index out of the directory we only ever read. Override with CARDLOOP_USAGE_DB.
DEFAULT_DB_PATH = Path(
    os.environ.get("CARDLOOP_USAGE_DB", "")
    or (Path(__file__).resolve().parent / "data" / "usage.db")
)

# Higher = more capable; used to pick a session's headline model across mixed turns.
MODEL_PRIORITY = {"fable": 5, "mythos": 5, "opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model: str | None) -> int:
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def get_db(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT,
            is_subagent             INTEGER DEFAULT 0,
            agent_id                TEXT
        );
        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );
        CREATE TABLE IF NOT EXISTS agents (
            agent_id              TEXT PRIMARY KEY,
            agent_type            TEXT,
            dispatched_in_session TEXT,
            completed_at          TEXT,
            status                TEXT,
            total_tokens          INTEGER,
            total_duration_ms     INTEGER,
            tool_use_count        INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_agents_type ON agents(agent_type);
    """)
    # Additive, in-place migrations so an older DB upgrades without a rebuild.
    _ensure_column(conn, "turns", "message_id", "TEXT")
    _ensure_column(conn, "turns", "is_subagent", "INTEGER DEFAULT 0")
    _ensure_column(conn, "turns", "agent_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_subagent ON turns(is_subagent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent_id ON turns(agent_id)")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != ''
    """)
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def project_name_from_cwd(cwd: str | None) -> str:
    """Friendly project name = last two path components of cwd."""
    if not cwd:
        return "unknown"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def is_subagent_record(record: dict, source_path: str = "") -> bool:
    """True if a record belongs to a dispatched sub-agent (Task/Agent tool)."""
    if record.get("isSidechain"):
        return True
    if record.get("agentId"):
        return True
    data = record.get("data")
    if isinstance(data, dict) and data.get("agentId"):
        return True
    sp = str(source_path).replace("\\", "/").lower()
    return "/subagents/" in sp


def record_agent_id(record: dict) -> str | None:
    agent_id = record.get("agentId")
    if not agent_id:
        data = record.get("data")
        if isinstance(data, dict):
            agent_id = data.get("agentId")
    return agent_id


def extract_agent_dispatch(record: dict) -> dict | None:
    """Pull sub-agent identity + aggregate stats from a parent's tool_result record."""
    if record.get("type") != "user":
        return None
    tur = record.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    agent_id = tur.get("agentId")
    if not agent_id:
        return None
    agent_type = tur.get("agentType") or "task"
    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "dispatched_in_session": record.get("sessionId"),
        "completed_at": record.get("timestamp", ""),
        "status": tur.get("status"),
        "total_tokens": tur.get("totalTokens"),
        "total_duration_ms": tur.get("totalDurationMs"),
        "tool_use_count": tur.get("totalToolUseCount"),
    }


def upsert_agents(conn: sqlite3.Connection, agents: list[dict]) -> None:
    if not agents:
        return
    conn.executemany("""
        INSERT INTO agents
            (agent_id, agent_type, dispatched_in_session, completed_at,
             status, total_tokens, total_duration_ms, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            agent_type            = excluded.agent_type,
            dispatched_in_session = excluded.dispatched_in_session,
            completed_at          = excluded.completed_at,
            status                = excluded.status,
            total_tokens          = excluded.total_tokens,
            total_duration_ms     = excluded.total_duration_ms,
            tool_use_count        = excluded.tool_use_count
    """, [
        (a["agent_id"], a["agent_type"], a.get("dispatched_in_session"),
         a.get("completed_at"), a.get("status"),
         a.get("total_tokens"), a.get("total_duration_ms"), a.get("tool_use_count"))
        for a in agents
    ])


def _parse_record(record: dict, filepath: str, session_meta: dict,
                   seen_messages: dict, turns_no_id: list, agents: dict) -> None:
    """Fold one transcript record into the in-progress parse buffers."""
    rtype = record.get("type")
    if rtype not in ("assistant", "user"):
        return
    session_id = record.get("sessionId")
    if not session_id:
        return

    if rtype == "user":
        dispatch = extract_agent_dispatch(record)
        if dispatch is not None:
            agents[dispatch["agent_id"]] = dispatch

    timestamp = record.get("timestamp", "")
    cwd = record.get("cwd", "")
    git_branch = record.get("gitBranch", "")

    if session_id not in session_meta:
        session_meta[session_id] = {
            "session_id": session_id,
            "project_name": project_name_from_cwd(cwd),
            "first_timestamp": timestamp,
            "last_timestamp": timestamp,
            "git_branch": git_branch,
            "model": None,
        }
    else:
        meta = session_meta[session_id]
        if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
            meta["first_timestamp"] = timestamp
        if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
            meta["last_timestamp"] = timestamp
        if git_branch and not meta["git_branch"]:
            meta["git_branch"] = git_branch

    if rtype != "assistant":
        return

    msg = record.get("message", {})
    usage = msg.get("usage", {})
    model = msg.get("model", "")
    message_id = msg.get("id", "")

    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

    # Only record turns that carried real token usage.
    if input_tokens + output_tokens + cache_read + cache_creation == 0:
        return

    tool_name = None
    for item in msg.get("content", []):
        if isinstance(item, dict) and item.get("type") == "tool_use":
            tool_name = item.get("name")
            break

    if model:
        session_meta[session_id]["model"] = model

    turn = {
        "session_id": session_id,
        "timestamp": timestamp,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "tool_name": tool_name,
        "cwd": cwd,
        "message_id": message_id,
        "is_subagent": 1 if is_subagent_record(record, filepath) else 0,
        "agent_id": record_agent_id(record),
    }

    # Dedup: last record per message_id wins (it has the final usage tallies).
    if message_id:
        seen_messages[message_id] = turn
    else:
        turns_no_id.append(turn)


def parse_jsonl_file(filepath: str, start_line: int = 0):
    """Parse a JSONL file (optionally only lines after start_line).

    Returns (session_metas, turns, agents, line_count). Deduplicates streaming
    events by message.id.
    """
    seen_messages: dict = {}
    turns_no_id: list = []
    session_meta: dict = {}
    agents: dict = {}
    line_count = 0
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                if line_count <= start_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _parse_record(record, filepath, session_meta,
                              seen_messages, turns_no_id, agents)
    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, list(agents.values()), line_count


def aggregate_sessions(session_metas: list[dict], turns: list[dict]) -> list[dict]:
    """Roll turn data back up into session-level stats."""
    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_read": 0, "total_cache_creation": 0,
        "turn_count": 0, "model": None,
    })
    session_model_counts = defaultdict(Counter)
    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        if t["model"]:
            session_model_counts[t["session_id"]][t["model"]] += 1
    for sid, counts in session_model_counts.items():
        if counts:
            session_stats[sid]["model"] = counts.most_common(1)[0][0]
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        result.append({**meta, **session_stats[sid]})
    return result


def upsert_sessions(conn: sqlite3.Connection, sessions: list[dict]) -> None:
    for s in sessions:
        existing = conn.execute(
            "SELECT model FROM sessions WHERE session_id = ?", (s["session_id"],)
        ).fetchone()
        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"],
            ))
        else:
            # Keep the highest-priority model (opus over a haiku sub-agent, etc.).
            new_model = s["model"]
            model_to_set = (new_model if _model_priority(new_model) > _model_priority(existing["model"])
                            else existing["model"])
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = ?
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], model_to_set, s["session_id"],
            ))


def insert_turns(conn: sqlite3.Connection, turns: list[dict]) -> None:
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id,
             is_subagent, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], t.get("message_id", ""),
         t.get("is_subagent", 0), t.get("agent_id"))
        for t in turns
    ])


def scan(projects_dir: Path | str | None = None,
         db_path: Path | str = DEFAULT_DB_PATH, verbose: bool = False) -> dict:
    """Incrementally scan transcripts into the DB. Fast to re-run (mtime-gated)."""
    conn = get_db(db_path)
    init_db(conn)

    base = Path(projects_dir) if projects_dir else PROJECTS_DIR
    jsonl_files = sorted(glob.glob(str(base / "**" / "*.jsonl"), recursive=True)) if base.exists() else []

    new_files = updated_files = skipped_files = total_turns = 0
    total_sessions: set = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?", (filepath,)
        ).fetchone()
        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        start_line = 0 if is_new else (row["lines"] or 0)
        session_metas, turns, agents, line_count = parse_jsonl_file(filepath, start_line)

        if line_count <= start_line and not is_new:
            # mtime moved but no new content.
            conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?", (mtime, filepath))
            conn.commit()
            skipped_files += 1
            continue

        upsert_agents(conn, agents)
        if turns or session_metas:
            sessions = aggregate_sessions(session_metas, turns)
            upsert_sessions(conn, sessions)
            insert_turns(conn, turns)
            total_sessions.update(s["session_id"] for s in sessions)
            total_turns += len(turns)
        if is_new:
            new_files += 1
        else:
            updated_files += 1

        conn.execute("INSERT OR REPLACE INTO processed_files (path, mtime, lines) VALUES (?, ?, ?)",
                     (filepath, mtime, line_count))
        conn.commit()

    # Recompute session totals from actual turns (INSERT OR IGNORE may have dropped
    # duplicate message-ids that upsert_sessions had already added additively).
    if new_files or updated_files:
        conn.execute("""
            UPDATE sessions SET
                total_input_tokens = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_output_tokens = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_read = COALESCE((SELECT SUM(cache_read_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                turn_count = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id), 0)
        """)
        conn.commit()

    if verbose:
        print(f"Scan: new={new_files} updated={updated_files} skipped={skipped_files} "
              f"turns+={total_turns} sessions={len(total_sessions)}")
    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}


# ──────────────────────────── aggregation for the dashboard ────────────────────────────
#
# dashboard_data() turns the turns/sessions/agents tables into the panels the cockpit
# Usage tab renders, filtered server-side by date range + model so the client stays thin
# and pricing stays single-source (usage_pricing). Cardloop's own layer (not from the
# upstream dashboard, which costs client-side over an all-history payload).

# JOIN expression: a turn's sub-agent type, with auto-compaction surfaced explicitly.
_AGENT_TYPE_EXPR = (
    "COALESCE(a.agent_type, "
    "CASE WHEN t.agent_id LIKE 'acompact-%' THEN 'auto-compact' ELSE 'unknown' END)"
)


def _norm_model(m: str | None) -> str:
    return m if m else "unknown"


def _model_clause(models: list[str] | None, col: str = "t.model"):
    """Build an optional `AND <col> IN (...)` filter. None / empty = no filter."""
    if not models:
        return "", []
    norm = "COALESCE(NULLIF(%s, ''), 'unknown')" % col
    placeholders = ",".join("?" for _ in models)
    return f" AND {norm} IN ({placeholders})", list(models)


def dashboard_data(db_path: Path | str = DEFAULT_DB_PATH,
                   days: int | None = 30, models: list[str] | None = None,
                   sessions_limit: int = 50, dispatches_limit: int = 50) -> dict:
    """Aggregate the DB into the cockpit Usage payload (range- + model-filtered)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return {"error": "no_data", "ready": False}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # Date floor (UTC). days=None / <=0 → all time.
    start_day = None
    if days and days > 0:
        start_day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    day_clause = " AND substr(t.timestamp,1,10) >= ?" if start_day else ""
    day_args = [start_day] if start_day else []
    model_clause, model_args = _model_clause(models)
    where = "WHERE 1=1" + day_clause + model_clause
    args = day_args + model_args

    # All models present (for the filter UI) — unfiltered by model, but range-bound.
    all_models = [r["model"] for r in conn.execute(
        f"""SELECT COALESCE(NULLIF(t.model,''),'unknown') as model, SUM(t.input_tokens+t.output_tokens) tot
            FROM turns t WHERE 1=1{day_clause}
            GROUP BY model ORDER BY tot DESC""", day_args)]

    def cost(r) -> float:
        return usage_pricing.calc_cost(r["model"], r["input"], r["output"],
                                       r["cache_read"], r["cache_creation"])

    # ── by model ──────────────────────────────────────────────────────────────
    by_model = []
    for r in conn.execute(f"""
        SELECT COALESCE(NULLIF(t.model,''),'unknown') as model,
               SUM(t.input_tokens) input, SUM(t.output_tokens) output,
               SUM(t.cache_read_tokens) cache_read, SUM(t.cache_creation_tokens) cache_creation,
               COUNT(*) turns
        FROM turns t {where}
        GROUP BY COALESCE(NULLIF(t.model,''),'unknown')
        ORDER BY (SUM(t.input_tokens)+SUM(t.output_tokens)) DESC""", args):
        d = dict(r)
        d["cost"] = round(cost(r), 4)
        by_model.append(d)

    # ── by day (stacked token series) ────────────────────────────────────────
    by_day_raw = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0,
                                       "cache_creation": 0, "turns": 0, "cost": 0.0})
    for r in conn.execute(f"""
        SELECT substr(t.timestamp,1,10) day, COALESCE(NULLIF(t.model,''),'unknown') model,
               SUM(t.input_tokens) input, SUM(t.output_tokens) output,
               SUM(t.cache_read_tokens) cache_read, SUM(t.cache_creation_tokens) cache_creation,
               COUNT(*) turns
        FROM turns t {where}
        GROUP BY substr(t.timestamp,1,10), COALESCE(NULLIF(t.model,''),'unknown')
        ORDER BY day""", args):
        b = by_day_raw[r["day"]]
        for k in ("input", "output", "cache_read", "cache_creation", "turns"):
            b[k] += r[k] or 0
        b["cost"] += cost(r)
    by_day = [{"day": d, **{k: (round(v, 4) if k == "cost" else v) for k, v in b.items()}}
              for d, b in sorted(by_day_raw.items())]

    # ── by hour (UTC, 0–23, peak-hour view) ──────────────────────────────────
    hour_raw: dict[int, dict] = {h: {"input": 0, "output": 0, "cache_read": 0,
                                      "cache_creation": 0, "turns": 0, "cost": 0.0}
                                  for h in range(24)}
    for r in conn.execute(f"""
        SELECT CAST(substr(t.timestamp,12,2) AS INT) hour,
               COALESCE(NULLIF(t.model,''),'unknown') model,
               SUM(t.input_tokens) input, SUM(t.output_tokens) output,
               SUM(t.cache_read_tokens) cache_read, SUM(t.cache_creation_tokens) cache_creation,
               COUNT(*) turns
        FROM turns t {where}
        GROUP BY CAST(substr(t.timestamp,12,2) AS INT), COALESCE(NULLIF(t.model,''),'unknown')
        ORDER BY hour""", args):
        h = r["hour"] or 0
        if 0 <= h < 24:
            b = hour_raw[h]
            for k in ("input", "output", "cache_read", "cache_creation", "turns"):
                b[k] += r[k] or 0
            b["cost"] += cost(r)
    by_hour = [{"hour": h, **{k: (round(v, 4) if k == "cost" else v) for k, v in b.items()}}
               for h, b in sorted(hour_raw.items())]

    # ── by project (JOIN sessions for friendly name) ─────────────────────────
    by_project = []
    for r in conn.execute(f"""
        SELECT COALESCE(s.project_name,'unknown') project,
               COALESCE(NULLIF(t.model,''),'unknown') model,
               SUM(t.input_tokens) input, SUM(t.output_tokens) output,
               SUM(t.cache_read_tokens) cache_read, SUM(t.cache_creation_tokens) cache_creation,
               COUNT(*) turns, COUNT(DISTINCT t.session_id) sessions
        FROM turns t LEFT JOIN sessions s ON t.session_id = s.session_id
        {where}
        GROUP BY COALESCE(s.project_name,'unknown'), COALESCE(NULLIF(t.model,''),'unknown')""", args):
        by_project.append(dict(r, cost=cost(r)))
    # collapse per-(project,model) rows into per-project, summing cost
    proj_agg: dict[str, dict] = {}
    for r in by_project:
        p = proj_agg.setdefault(r["project"], {"project": r["project"], "sessions": 0,
                                               "turns": 0, "input": 0, "output": 0, "cost": 0.0})
        p["turns"] += r["turns"]; p["input"] += r["input"]; p["output"] += r["output"]
        p["sessions"] = max(p["sessions"], r["sessions"]); p["cost"] += r["cost"]
    by_project = sorted(({**p, "cost": round(p["cost"], 4)} for p in proj_agg.values()),
                        key=lambda x: x["cost"], reverse=True)

    # ── by project + branch (card 3d — for CSV export and branch-level table) ─
    pb_agg: dict[tuple, dict] = {}
    for r in conn.execute(f"""
        SELECT COALESCE(s.project_name,'unknown') project,
               COALESCE(s.git_branch,'') branch,
               COALESCE(NULLIF(t.model,''),'unknown') model,
               SUM(t.input_tokens) input, SUM(t.output_tokens) output,
               SUM(t.cache_read_tokens) cache_read, SUM(t.cache_creation_tokens) cache_creation,
               COUNT(*) turns, COUNT(DISTINCT t.session_id) sessions
        FROM turns t LEFT JOIN sessions s ON t.session_id = s.session_id
        {where}
        GROUP BY COALESCE(s.project_name,'unknown'), COALESCE(s.git_branch,''),
                 COALESCE(NULLIF(t.model,''),'unknown')""", args):
        key = (r["project"], r["branch"])
        p = pb_agg.setdefault(key, {"project": r["project"], "branch": r["branch"],
                                     "sessions": 0, "turns": 0, "input": 0, "output": 0, "cost": 0.0})
        p["turns"] += r["turns"]; p["input"] += r["input"]; p["output"] += r["output"]
        p["sessions"] = max(p["sessions"], r["sessions"]); p["cost"] += cost(r)
    by_project_branch = sorted(
        ({**p, "cost": round(p["cost"], 4)} for p in pb_agg.values()),
        key=lambda x: x["cost"], reverse=True)

    # ── sub-agent tokens by type ─────────────────────────────────────────────
    subagent_by_type: dict[str, dict] = {}
    for r in conn.execute(f"""
        SELECT {_AGENT_TYPE_EXPR} agent_type, COALESCE(NULLIF(t.model,''),'unknown') model,
               SUM(t.input_tokens) input, SUM(t.output_tokens) output,
               SUM(t.cache_read_tokens) cache_read, SUM(t.cache_creation_tokens) cache_creation,
               COUNT(DISTINCT t.agent_id) dispatches, COUNT(*) turns
        FROM turns t LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.is_subagent = 1{day_clause}{model_clause}
        GROUP BY {_AGENT_TYPE_EXPR}, COALESCE(NULLIF(t.model,''),'unknown')""", args):
        s = subagent_by_type.setdefault(r["agent_type"], {
            "agent_type": r["agent_type"], "input": 0, "output": 0,
            "cache_read": 0, "cache_creation": 0, "dispatches": 0, "turns": 0, "cost": 0.0})
        for k in ("input", "output", "cache_read", "cache_creation", "dispatches", "turns"):
            s[k] += r[k] or 0
        s["cost"] += cost(r)
    subagents = sorted(({**s, "cost": round(s["cost"], 4)} for s in subagent_by_type.values()),
                       key=lambda x: (x["input"] + x["output"] + x["cache_read"] + x["cache_creation"]),
                       reverse=True)

    # ── recent sessions ──────────────────────────────────────────────────────
    recent = []
    for r in conn.execute("""
        SELECT session_id, project_name, git_branch, first_timestamp, last_timestamp,
               total_input_tokens, total_output_tokens, total_cache_read,
               total_cache_creation, model, turn_count
        FROM sessions ORDER BY last_timestamp DESC LIMIT ?""", (max(sessions_limit, 1),)):
        try:
            t1 = datetime.fromisoformat((r["first_timestamp"] or "").replace("Z", "+00:00"))
            t2 = datetime.fromisoformat((r["last_timestamp"] or "").replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        c = usage_pricing.calc_cost(r["model"], r["total_input_tokens"], r["total_output_tokens"],
                                    r["total_cache_read"], r["total_cache_creation"])
        recent.append({
            "session_id": (r["session_id"] or "")[:8],
            "project": r["project_name"] or "unknown",
            "branch": r["git_branch"] or "",
            "last": (r["last_timestamp"] or "")[:16].replace("T", " "),
            "duration_min": duration_min,
            "model": r["model"] or "unknown",
            "turns": r["turn_count"] or 0,
            "input": r["total_input_tokens"] or 0,
            "output": r["total_output_tokens"] or 0,
            "cost": round(c, 4),
        })

    # ── overview totals (range + model filtered) ─────────────────────────────
    ov = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "turns": 0, "cost": 0.0}
    for r in by_model:
        for k in ("input", "output", "cache_read", "cache_creation", "turns"):
            ov[k] += r[k]
        ov["cost"] += r["cost"]
    sub_turns = sum(s["turns"] for s in subagents)
    sub_cost = sum(s["cost"] for s in subagents)
    distinct_sessions = conn.execute(
        f"SELECT COUNT(DISTINCT t.session_id) c FROM turns t {where}", args).fetchone()["c"]

    conn.close()
    return {
        "ready": True,
        "days": days,
        "overview": {**{k: round(v, 4) if k == "cost" else v for k, v in ov.items()},
                     "sessions": distinct_sessions,
                     "subagent_turns": sub_turns, "subagent_cost": round(sub_cost, 4)},
        "by_day": by_day,
        "by_hour": by_hour,
        "by_model": by_model,
        "by_project": by_project,
        "by_project_branch": by_project_branch,
        "subagents": subagents,
        "recent_sessions": recent,
        "all_models": all_models,
        "pricing_as_of": usage_pricing.PRICING_AS_OF,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


if __name__ == "__main__":
    import sys
    pd = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--projects-dir" and i + 2 <= len(sys.argv[1:]):
            pd = sys.argv[i + 2]
    print(scan(projects_dir=pd, verbose=True))

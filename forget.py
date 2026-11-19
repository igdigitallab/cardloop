"""spec-098 Forget: delete a session (or a reply and everything after it) everywhere the cockpit
controls.

One purge engine, per-provider adapters. This module owns the shared vocabulary (the adapter
protocol, the artifact and content records, the registry and the capability flags). The plan /
execute pipeline, the cockpit-store purges, the pending journal and the residue scan live here too
(added on top of this skeleton). Each provider module owns its own files and registers an adapter:
`forget_claude.py`, `forget_codex.py`, `forget_grok.py`.

Rollback: `FORGET_ENABLED=0` turns every capability off and the endpoints answer 404; turn marks,
run ids and the lineage log are inert without it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import fcntl
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, runtime_checkable


def forget_enabled() -> bool:
    """On unless the operator sets FORGET_ENABLED=0 (or false/no/off)."""
    return os.environ.get("FORGET_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


@dataclass
class SessionArtifacts:
    """What an adapter found for one session. `extra` is adapter-private and JSON-serialisable:
    it is written into the pending record, so it must never hold conversation text."""

    provider: str
    session_id: str
    cwd: str
    title: str = ""
    paths: list[Path] = field(default_factory=list)
    first_ts: "float | None" = None
    last_ts: "float | None" = None
    extra: dict = field(default_factory=dict)


# kinds of ContentItem, in the order they usually appear in a turn
CONTENT_KINDS = ("prompt", "assistant", "tool_use", "tool_result", "summary")


@dataclass
class ContentItem:
    """One piece of conversation text read from a provider store, for the side-effect scan, the
    old-run attribution (prompts) and the residue scan (distinctive strings)."""

    kind: str
    text: str
    ts: "float | None" = None
    tool: "str | None" = None
    tool_input: "dict | None" = None
    uuid: "str | None" = None


@runtime_checkable
class Adapter(Protocol):
    name: str
    can_delete_session: bool
    can_forget_turn: bool

    def locate(self, ctx: Mapping[str, Any], project: Mapping[str, Any],
               session_id: str) -> "SessionArtifacts | None": ...

    def busy_reason(self, ctx: Mapping[str, Any], project: Mapping[str, Any],
                    session_id: str) -> "str | None": ...

    def read_content(self, art: SessionArtifacts) -> Iterator[ContentItem]: ...

    def delete(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict: ...

    def verify_gone(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> list[str]: ...

    def residue_roots(self, ctx: Mapping[str, Any]) -> list[Path]: ...


_ADAPTERS: "dict[str, Adapter]" = {}


def register_adapter(adapter: Adapter) -> Adapter:
    if adapter.name in _ADAPTERS:
        raise ValueError(f"forget adapter {adapter.name!r} is already registered")
    _ADAPTERS[adapter.name] = adapter
    return adapter


def adapter(name: str) -> "Adapter | None":
    return _ADAPTERS.get(name)


def capabilities(name: str) -> dict:
    """The forget capability flags of one provider, merged into its `capabilities()` row."""
    a = _ADAPTERS.get(name)
    on = forget_enabled() and a is not None
    return {
        "delete_session": bool(on and a.can_delete_session),
        "forget_turn": bool(on and a.can_forget_turn),
    }


# Vendor-side limits shown in the dialog (P0 research, docs/internal/specs/p0/retention.md).
VENDOR_LIMITS: "dict[str, str]" = {
    "claude": ("Anthropic keeps copies for 30 days (or up to 5 years if 'Help improve Claude' is on); "
               "chats flagged by safety systems can be kept up to 2 years."),
    "codex": ("OpenAI does not publish how long it keeps Codex turns made with a ChatGPT login; "
              "deleting here does not delete anything on OpenAI's side."),
    "grok": ("xAI does not publish how long it keeps prompts and code; with the coding data-retention "
             "opt-out on, xAI says it discards them, but deleting here does not remove anything on "
             "xAI's side."),
    "ollama": ("Turns that ran on the local Ollama backend never left your network; its server keeps "
               "no prompt history."),
}

ALWAYS_LIMITS: "tuple[str, ...]" = (
    "Offsite backups made before today keep the cockpit's logs for up to 6 months.",
    "Notifications already delivered to your devices are not recalled.",
    "What the agent did (files, commits, sent messages) is not undone.",
)




# ───────────────────────────── engine: errors, helpers ─────────────────────────────────────────

PENDING_DIR = "forget_pending"
ATTRIBUTION_WINDOW_SEC = 120.0     # decision 8: run_start within this of a session prompt
MAX_WINDOWS = 200                  # distinctive strings kept for the residue scan
WINDOW_LEN = 32
MAX_PROMPTS = 5000
MAX_SIDE_EFFECTS = 60


class ForgetError(Exception):
    """A refusal or failure with the HTTP shape the endpoint answers with."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class SimulatedCrash(Exception):
    """Test hook: raised between steps (see `_CRASH_AFTER`)."""


_CRASH_AFTER: "int | None" = None      # tests set this to N: raise after step N completes


def _after_step(n: int) -> None:
    if _CRASH_AFTER is not None and n == _CRASH_AFTER:
        raise SimulatedCrash(f"injected crash after step {n}")


def residue_budget_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("FORGET_RESIDUE_BUDGET_SEC", "60")))
    except ValueError:
        return 60.0


def digest(raw: "bytes | str") -> str:
    """Line identity in the pending journal: a hash, never the text itself."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:20]


def collapse(text: "str | None") -> str:
    return " ".join((text or "").split())


def short(text: str, limit: int = 300) -> str:
    """Same shape as `engine.short` (the audit's TASK text)."""
    t = collapse(text)
    return t if len(t) <= limit else t[:limit] + "…"


def _read_jsonl_dicts(path: Path) -> "list[dict]":
    out: list = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


def _atomic_replace_lines(path: Path, keep_lines: "list[bytes]") -> None:
    tmp = path.with_name(path.name + ".forget-tmp")
    try:
        with open(tmp, "wb") as f:
            f.writelines(keep_lines)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ───────────────────────────── held content (derived, bounded) ─────────────────────────────────
@dataclass
class Held:
    """What is kept in memory from the content about to be deleted. Derived while streaming
    `adapter.read_content`, so memory stays bounded however large the transcript is (the spec's
    "hold up to 64 MB, then a temp file" collapses into: never hold the content at all)."""

    turns: int = 0
    items: int = 0
    prompts: "list[tuple[float | None, frozenset]]" = field(default_factory=list)  # (ts, key hashes)
    windows: "list[str]" = field(default_factory=list)            # distinctive strings (residue scan)
    media: "list[tuple[str, str]]" = field(default_factory=list)  # (project_id, file name) uploads
    side_effects: "list[dict]" = field(default_factory=list)


_ASCII_RUN = re.compile(r"[A-Za-z0-9 .,:_-]{%d,}" % WINDOW_LEN)
_RAW_RUN = re.compile(r'[^"\\\x00-\x1f\x7f]{24,}')
_MEDIA_RE = re.compile(r"chat-media/([A-Za-z0-9_.@-]+)/([A-Za-z0-9_.@%+=,()-]+\.[A-Za-z0-9]{1,8})")


def _window_candidates(text: str) -> "list[str]":
    """ASCII-safe windows (no JSON-escape ambiguity); a raw UTF-8 window for non-ASCII text."""
    out: list = []
    runs = sorted(_ASCII_RUN.findall(text), key=len, reverse=True)[:2]
    for run in runs:
        mid = max(0, (len(run) - WINDOW_LEN) // 2)
        w = run[mid:mid + WINDOW_LEN]
        if sum(c.isalnum() for c in w) >= 14 and len(set(w)) >= 8:
            out.append(w)
    if not out and any(ord(c) > 127 for c in text):
        raw = max(_RAW_RUN.findall(text) or [""], key=len)
        if len(raw) >= 24:
            mid = max(0, (len(raw) - WINDOW_LEN) // 2)
            out.append(raw[mid:mid + WINDOW_LEN])
    return out


def collect_held(items: "Iterator[ContentItem]", *, seed: str,
                 prompt_norm: "Callable[[str], str] | None" = None) -> Held:
    held = Held()
    pn = prompt_norm or (lambda s: s)
    cands: dict = {}
    seen_prompt: set = set()
    se_seen: set = set()
    for it in items:
        held.items += 1
        text = it.text or ""
        if it.kind == "prompt":
            if it.uuid:
                held.turns += 1
            variants = {collapse(text), collapse(pn(text))}
            keys = set()
            for v in variants:
                if v:
                    keys.add(digest(v))
                    keys.add(digest(short(v, 300)))
            if keys and len(held.prompts) < MAX_PROMPTS:
                tag = (it.ts, frozenset(keys))
                if tag not in seen_prompt:
                    seen_prompt.add(tag)
                    held.prompts.append(tag)
            for m in _MEDIA_RE.finditer(text):
                ref = (m.group(1), m.group(2))
                if ref not in held.media:
                    held.media.append(ref)
        elif it.kind == "tool_use" and it.tool:
            for se in classify_tool_use(it.tool, it.tool_input or {}):
                key = (se["kind"], se["detail"])
                if key not in se_seen and len(held.side_effects) < MAX_SIDE_EFFECTS:
                    se_seen.add(key)
                    held.side_effects.append(se)
        if it.kind in ("prompt", "assistant", "tool_result", "summary") and len(text) >= 40:
            for w in _window_candidates(text):
                cands[w] = hashlib.sha1((seed + w).encode("utf-8", "replace")).digest()
            if len(cands) > 4 * MAX_WINDOWS:
                keep = sorted(cands.items(), key=lambda kv: kv[1])[:MAX_WINDOWS]
                cands = dict(keep)
    held.windows = [w for w, _ in sorted(cands.items(), key=lambda kv: kv[1])[:MAX_WINDOWS]]
    if held.turns == 0:
        held.turns = len(held.prompts)
    return held


# ───────────────────────────── side-effect scan ────────────────────────────────────────────────
_GIT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|push)\b")
_DEPLOY_RE = re.compile(r"restart-self|systemctl\s+(?:--user\s+)?(?:restart|start|stop|reload)|"
                        r"docker\s+(?:compose\s+)?(?:up|restart|rm|run)\b|\bdeploy\b|"
                        r"(?:^|[\s;&|])(?:ssh|scp|rsync)\s")
_SENT_RE = re.compile(r"(?:^|[\s;&|(])(opinion|agy|ask)(?:\s|$)")
_MCP_ACTION_RE = re.compile(r"send|reply|post|sms|dial|upload|delete|create", re.I)


def classify_tool_use(tool: str, tool_input: dict) -> "list[dict]":
    """Things a tool call did that deleting the conversation does not undo."""
    out: list = []
    inp = tool_input if isinstance(tool_input, dict) else {}
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
        if path:
            mem = "/memory/" in path or "/.claude-ops/memory" in path
            out.append({"kind": "memory" if mem else "file_write",
                        "detail": path[:200] + (" (agent memory: carries into future sessions)" if mem else "")})
    elif tool == "Bash":
        cmd = str(inp.get("command") or "")
        if _GIT_RE.search(cmd):
            out.append({"kind": "git", "detail": collapse(cmd)[:160]})
        if _DEPLOY_RE.search(cmd):
            out.append({"kind": "deploy", "detail": collapse(cmd)[:160]})
        m = _SENT_RE.search(cmd)
        if m:
            who = {"opinion": "OpenAI and Google", "agy": "Google", "ask": "OpenAI or Google"}[m.group(1)]
            out.append({"kind": "sent_out", "detail": f"`{m.group(1)}` call: material sent to {who}"})
    elif tool.startswith("mcp__"):
        if "second_opinion" in tool:
            out.append({"kind": "sent_out", "detail": f"{tool}: material sent to Azure-hosted models"})
        elif _MCP_ACTION_RE.search(tool.split("__")[-1]):
            out.append({"kind": "message", "detail": tool[:120]})
    return out


# ───────────────────────────── turn marks / lineage readers ────────────────────────────────────
def turn_marks_path(data_dir: Path, session_id: str) -> Path:
    return Path(data_dir) / "turn_marks" / f"{session_id}.jsonl"


def mark_run_ids(data_dir: Path, session_id: str) -> "set[str]":
    return {str(r["run_id"]) for r in _read_jsonl_dicts(turn_marks_path(data_dir, session_id))
            if r.get("run_id")}


def lineage_rows(data_dir: Path) -> "list[dict]":
    return _read_jsonl_dicts(Path(data_dir) / "session_lineage.jsonl")


def remove_lineage(data_dir: Path, session_id: str) -> int:
    """Drop `session_lineage.jsonl` rows naming the session (atomic rewrite). Count removed."""
    p = Path(data_dir) / "session_lineage.jsonl"
    if not p.exists():
        return 0
    keep: list = []
    gone = 0
    with open(p, "rb") as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except ValueError:
                keep.append(raw if raw.endswith(b"\n") else raw + b"\n")
                continue
            if isinstance(r, dict) and session_id in (r.get("from_session"), r.get("to_session")):
                gone += 1
            else:
                keep.append(raw if raw.endswith(b"\n") else raw + b"\n")
    if gone:
        _atomic_replace_lines(p, keep)
    return gone


# ───────────────────────────── cockpit-store purges (small, injectable) ────────────────────────
def _prompt_match(prompts: "list[tuple[float | None, frozenset]]", keys_to_try: "list[str]",
                  ts: "float | None", consumed: "set[int]") -> bool:
    """Decision 8, one-to-one: the line's prompt matches the CLOSEST unconsumed session prompt with the
    same text within ATTRIBUTION_WINDOW_SEC, and consumes it. A prompt (or a line) without a time
    never matches: "any time" would adopt every run of any chat that ever typed "ok"."""
    if ts is None:
        return False
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return False
    best: "tuple[float, int] | None" = None
    for i, (pts, keys) in enumerate(prompts):
        if i in consumed or pts is None:
            continue
        if any(k in keys for k in keys_to_try):
            d = abs(ts - pts)
            if d <= ATTRIBUTION_WINDOW_SEC and (best is None or d < best[0]):
                best = (d, i)
    if best is None:
        return False
    consumed.add(best[1])
    return True


_TL_CONV_KINDS = frozenset({"run_start", "run_end", "text", "tool_result", "steer", "error"})
_TL_LINEAGE_KINDS = frozenset({"runtime_handoff", "session_rotated"})


def _jsonl_records(path: Path) -> "tuple[list[bytes], list[dict | None]]":
    try:
        raw_lines = Path(path).read_bytes().splitlines(keepends=True)
    except OSError:
        return [], []
    recs: list = []
    for raw in raw_lines:
        try:
            r = json.loads(raw)
        except ValueError:
            r = None
        recs.append(r if isinstance(r, dict) else None)
    return raw_lines, recs


def timeline_run_sessions(paths: "list[Path]") -> "dict[str, str]":
    """Pass 1 over EVERY timeline file: run id -> the session its `run_end` names. A run whose end
    names another session is that session's, whatever its prompt looks like."""
    out: dict = {}
    for p in paths:
        for r in _jsonl_records(p)[1]:
            if r and r.get("kind") == "run_end" and r.get("run_id") and r.get("session_id"):
                out[str(r["run_id"])] = str(r["session_id"])
    return out


def select_timeline_lines(path: Path, *, session_id: str, known_runs: "set[str]",
                          prompts: "list[tuple[float | None, frozenset]]",
                          prompt_norm: "Callable[[str], str] | None" = None,
                          shared: "dict | None" = None) -> dict:
    """Which lines of one timeline file belong to the session (read-only).

    Returns {"digests", "unattributed", "runs", "chat_ids"}. `known_runs` is extended in place.
    `shared` carries state ACROSS the files of one plan (call `.1` first, then the live file):
    `run_sessions` (pass 1 over all files), the per-session_key verdict of the latest run_start
    (so a run that rotated into `.1` still tags its id-less lines in the live file) and the set of
    consumed prompts. A stamped run_start belongs by its id; the decision-8 prompt match is only
    for a run the timeline cannot place (no run_end naming a session — pre-stamping runs)."""
    pn = prompt_norm or (lambda s: s)
    shared = shared if shared is not None else {}
    raw_lines, recs = _jsonl_records(path)
    if not raw_lines:
        return {"digests": [], "unattributed": 0, "runs": [], "chat_ids": []}
    run_sessions = shared.get("run_sessions")
    if run_sessions is None:
        run_sessions = timeline_run_sessions([path])
    for rid, sid in run_sessions.items():
        if sid == session_id:
            known_runs.add(rid)
    state: dict = shared.setdefault("state", {})
    consumed: set = shared.setdefault("consumed", set())
    found: set = set()
    chats: set = set()
    digests: list = []
    unattributed = 0
    for raw, r in zip(raw_lines, recs):
        if r is None:
            continue
        kind = r.get("kind") or r.get("type")
        sk = r.get("session_key")
        rid = str(r["run_id"]) if r.get("run_id") else None
        belongs: "bool | None"
        if kind == "run_start":
            if rid and rid in known_runs:
                belongs = True
            elif rid and rid in run_sessions:
                belongs = False                  # its run_end names another session
            else:
                ptxt = r.get("prompt") or ""
                keys = [digest(collapse(ptxt)), digest(collapse(pn(ptxt)))] if ptxt else []
                belongs = bool(keys) and _prompt_match(prompts, keys, r.get("ts"), consumed)
                if belongs and rid:
                    known_runs.add(rid)
            if belongs and r.get("chat_id"):
                chats.add(str(r["chat_id"]))
            state[sk] = belongs
        elif kind in _TL_CONV_KINDS:
            if rid:
                belongs = rid in known_runs      # an id is authoritative over the sequence
            else:
                belongs = state.get(sk)
                if belongs is None:
                    unattributed += 1
        elif kind in _TL_LINEAGE_KINDS:
            belongs = session_id.encode() in raw
        else:
            belongs = False
        if belongs:
            digests.append(digest(raw))
            if rid:
                found.add(rid)
    return {"digests": digests, "unattributed": unattributed, "runs": sorted(found),
            "chat_ids": sorted(chats)}


def purge_lines_by_digest(path: Path, digests: "set[str] | list[str]", lock: Any) -> int:
    """Atomic rewrite (tmp + rename) of one JSONL file without the lines whose digest is listed,
    under the writer's lock. Returns the number of lines dropped; a missing file is 0."""
    want = set(digests)
    if not want:
        return 0
    with lock:
        try:
            src = open(path, "rb")
        except OSError:
            return 0
        with src:
            # A writer in ANOTHER process (the Grok guard hook appends DENY lines to the audit month)
            # cannot take the in-process lock: it takes flock on the file and re-checks the inode
            # after it gets it, so it never appends to the inode this rewrite replaces.
            try:
                fcntl.flock(src.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            raw_lines = src.read().splitlines(keepends=True)
            keep = [raw for raw in raw_lines if digest(raw) not in want]
            gone = len(raw_lines) - len(keep)
            if gone:
                _atomic_replace_lines(path, keep)
    return gone


AUDIT_RE = re.compile(r"^(\S+) \[(.*?)\] ([A-Z_]+)(?: run=(\S+))?: (.*)$")


def _audit_ts(s: str) -> "float | None":
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def audit_months(first_ts: "float | None", last_ts: "float | None") -> "list[str] | None":
    """`YYYY-MM` months a session spans (local time, like the audit writer); None = unknown."""
    if first_ts is None or last_ts is None:
        return None
    out: list = []
    y, m = time.localtime(first_ts)[:2]
    ey, em = time.localtime(max(first_ts, last_ts))[:2]
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def select_audit_lines(path: Path, *, known_runs: "set[str]",
                       prompts: "list[tuple[float | None, frozenset]]",
                       project_names: "set[str] | None" = None,
                       consumed: "set[int] | None" = None) -> dict:
    """Audit lines of the session: by `run=` token, else decision 8 (an id-less TASK equal to
    short(prompt, 300) within 120 s of an unconsumed session prompt, then that `[Project]`'s
    RESULT/DENY/COMPACT lines until its next TASK). The month file is global, so a line of any
    other `[Project]` is never touched. Read-only; returns {"digests": [...], "unattributed": n}."""
    try:
        raw_lines = path.read_bytes().splitlines(keepends=True)
    except OSError:
        return {"digests": [], "unattributed": 0}
    names = {n for n in (project_names or set()) if n}
    consumed = consumed if consumed is not None else set()
    state: dict = {}
    digests: list = []
    unattributed = 0
    for raw in raw_lines:
        m = AUDIT_RE.match(raw.decode("utf-8", "replace").rstrip("\n"))
        if not m:
            continue
        ts_s, project, kind, rid, text = m.groups()
        if names and project not in names:
            continue
        belongs: "bool | None"
        if rid:
            belongs = rid in known_runs
            state[project] = belongs
        elif kind == "TASK":
            belongs = _prompt_match(prompts, [digest(collapse(text))], _audit_ts(ts_s), consumed)
            state[project] = belongs
        else:
            belongs = state.get(project)
            if belongs is None and kind in ("RESULT", "DENY", "COMPACT"):
                unattributed += 1
        if belongs:
            digests.append(digest(raw))
    return {"digests": digests, "unattributed": unattributed}


def purge_search(db_path: Path, *, transcript_paths: "list[str]", session_id: str,
                 reindex_paths: "list[str]", drop_hits: bool = True) -> dict:
    """search.db, one transaction under secure-delete (blocking; run it in an executor and under
    the cockpit's search lock).

    chat rows go by `doc_rows` path (never a full-scan `DELETE ... WHERE path`); the transcript's
    `file_state` row goes; every rewritten timeline path loses its rows AND its file_state so the
    indexer rebuilds it from the rewritten file; `doc_hits` for the session go. FTS5 secure-delete
    makes removed tokens leave the index at once; the WAL is truncated at the end."""
    out = {"rows": 0, "hits": 0, "checkpoint": "skipped"}
    if not Path(db_path).exists():
        return out
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA secure_delete=ON")
        has_docs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone() is not None
        if has_docs:
            conn.execute("INSERT INTO docs(docs, rank) VALUES('secure-delete', 1)")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if has_docs:
                for p in list(transcript_paths) + list(reindex_paths):
                    rids = [r[0] for r in conn.execute(
                        "SELECT doc_rowid FROM doc_rows WHERE path = ?", (p,))]
                    for rid in rids:
                        conn.execute("DELETE FROM docs WHERE rowid = ?", (rid,))
                    conn.execute("DELETE FROM doc_rows WHERE path = ?", (p,))
                    out["rows"] += len(rids)
                for p in list(transcript_paths) + list(reindex_paths):
                    conn.execute("DELETE FROM file_state WHERE path = ?", (p,))
                if drop_hits:     # B keeps the session, so its retrieval telemetry stays (no text)
                    cur = conn.execute("DELETE FROM doc_hits WHERE source = 'chat' AND ref = ?", (session_id,))
                    out["hits"] = max(cur.rowcount, 0)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        for attempt in range(4):
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0] == 0:
                out["checkpoint"] = "truncated"
                break
            out["checkpoint"] = "busy"
            time.sleep(0.5)
    finally:
        conn.close()
    return out


def purge_usage(db_path: Path, transcript_path: str, session_dir: "str | None") -> int:
    """usage.db: the transcript's `processed_files` cursor rows (itself and its sub-agent files).
    Token rows stay: they hold no text and the spend happened."""
    if not Path(db_path).exists():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA secure_delete=ON")
        gone = conn.execute("DELETE FROM processed_files WHERE path = ?", (transcript_path,)).rowcount
        if session_dir:
            like = session_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
            gone += conn.execute("DELETE FROM processed_files WHERE path LIKE ? ESCAPE '\\'", (like,)).rowcount
        conn.commit()
        return max(gone, 0)
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


# ───────────────────────────── chat-media ──────────────────────────────────────────────────────
def _rg() -> "str | None":
    return shutil.which("rg")


def referenced_elsewhere(names: "list[str]", roots: "list[Path]", exclude_prefix: str) -> "set[str]":
    """Which of `names` still appear in files under `roots`, ignoring files/dirs whose name starts
    with `exclude_prefix` (the session being deleted). `rg -l -F`, python fallback."""
    names = [n for n in names if n]
    found: set = set()
    roots = [r for r in roots if Path(r).exists()]
    if not names or not roots:
        return found
    rg = _rg()
    for name in names:
        if rg:
            try:
                res = subprocess.run(
                    [rg, "-a", "-l", "-F", "-uu", "--no-messages", "-g", f"!{exclude_prefix}*", "-e", name,
                     "--", *map(str, roots)],
                    capture_output=True, timeout=120)
                if res.returncode == 0 and res.stdout.strip():
                    found.add(name)
                continue
            except (subprocess.TimeoutExpired, OSError):
                found.add(name)       # cannot tell: keep the file
                continue
        needle = name.encode()
        hit = False
        for root in roots:
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if not d.startswith(exclude_prefix)]
                for fn in fns:
                    if fn.startswith(exclude_prefix):
                        continue
                    try:
                        with open(os.path.join(dp, fn), "rb") as f:
                            if needle in f.read():
                                hit = True
                                break
                    except OSError:
                        continue
                if hit:
                    break
            if hit:
                break
        if hit:
            found.add(name)
    return found


def plan_media(data_dir: Path, held: Held, roots: "list[Path]", exclude_prefix: str) -> "list[str]":
    """Upload files named in the deleted prompts and referenced nowhere else in the project."""
    media_root = (Path(data_dir) / "chat-media").resolve()
    cands: dict = {}
    for pid, name in held.media:
        p = (media_root / pid / name)
        try:
            rp = p.resolve()
        except OSError:
            continue
        if media_root in rp.parents and rp.is_file():
            cands[name] = str(rp)
    keep = referenced_elsewhere(list(cands), roots, exclude_prefix)
    return sorted(v for k, v in cands.items() if k not in keep)



# ───────────────────────────── forget from here (B): range search + run-id selection ───────────
_FIND_PASSES = 8


def _find_python(path: Path, start: int, end: "int | None", needles: "list[str]") -> "set[str]":
    left = {n: n.encode("utf-8", "replace") for n in needles if n}
    found: set = set()
    if not left:
        return found
    over = max(len(b) for b in left.values()) - 1
    try:
        f = open(path, "rb")
    except OSError:
        return found
    with f:
        f.seek(start)
        pos, prev = start, b""
        while left:
            want = (1 << 23) if end is None else min(1 << 23, end - pos)
            if want <= 0:
                break
            chunk = f.read(want)
            if not chunk:
                break
            buf = prev + chunk
            for n, b in list(left.items()):
                if b in buf:
                    found.add(n)
                    del left[n]
            prev = buf[-over:] if over > 0 else b""
            pos += len(chunk)
    return found


def find_in_range(path: Path, start: int, end: "int | None", needles: "list[str]") -> "set[str]":
    """Which `needles` occur inside bytes [start, end) of one file (end None = to EOF). A needle
    never contains a newline, and the range edges are line edges, so a match never straddles them.

    `rg -F -o -b` finds them in one pass at disk speed, but it reports non-overlapping matches
    only: a needle hidden under an overlapping match of another needle would be missed. So the
    found ones are dropped and rg runs again on the rest until a pass finds nothing new."""
    needles = sorted({n for n in needles if n and "\n" not in n})
    if not needles or not Path(path).is_file():
        return set()
    rg = _rg()
    if rg is None:
        return _find_python(Path(path), start, end, needles)
    found: set = set()
    left = list(needles)
    for _ in range(_FIND_PASSES):
        try:
            res = subprocess.run([rg, "-a", "-F", "-o", "-b", "--no-filename", "--no-line-number",
                                  "--no-messages", "--no-config", "-f", "-", "--", str(path)],
                                 input=("\n".join(left) + "\n").encode("utf-8", "replace"),
                                 capture_output=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError):
            return found | _find_python(Path(path), start, end, left)
        new: set = set()
        for line in res.stdout.splitlines():
            off_s, _sep, match = line.partition(b":")
            try:
                off = int(off_s)
            except ValueError:
                continue
            if off >= start and (end is None or off < end):
                new.add(match.decode("utf-8", "replace"))
        new &= set(left)
        if not new:
            break
        found |= new
        left = [n for n in left if n not in found]
        if not left:
            break
    return found


def select_run_lines(path: Path, run_ids: "set[str]") -> "list[str]":
    """Digests of the JSONL lines (timeline) stamped with one of `run_ids`. B selects ONLY by id:
    the session-level attribution of A would take the kept turns too."""
    out: list = []
    if not run_ids:
        return out
    for raw, r in zip(*_jsonl_records(path)):
        if r is not None and r.get("run_id") and str(r["run_id"]) in run_ids:
            out.append(digest(raw))
    return out


def select_audit_runs(path: Path, run_ids: "set[str]", project_names: "set[str] | None") -> "list[str]":
    """Audit lines carrying `run=<id>` of one of `run_ids`, of the project's own `[Project]` labels."""
    try:
        raw_lines = path.read_bytes().splitlines(keepends=True)
    except OSError:
        return []
    names = {n for n in (project_names or set()) if n}
    out: list = []
    for raw in raw_lines:
        m = AUDIT_RE.match(raw.decode("utf-8", "replace").rstrip("\n"))
        if not m:
            continue
        _ts, project, _kind, rid, _text = m.groups()
        if rid and rid in run_ids and (not names or project in names):
            out.append(digest(raw))
    return out


def purge_usage_cut(db_path: Path, transcript_path: str, prefix_lines: int, removed: "list[str]") -> int:
    """usage.db after a cut: the transcript's line cursor goes back to the kept line count (the
    scanner resumes by line number, so a stale count would skip the next turns), removed sub-agent
    files lose their cursor rows. Token rows stay (no text; the spend happened)."""
    if not Path(db_path).exists():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        n = conn.execute("UPDATE processed_files SET lines = ?, mtime = 0 WHERE path = ?",
                         (int(prefix_lines), transcript_path)).rowcount
        for p in removed:
            n += conn.execute("DELETE FROM processed_files WHERE path = ?", (p,)).rowcount
        conn.commit()
        return max(n, 0)
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()

# ───────────────────────────── residue scan (decision 5) ───────────────────────────────────────
# ───────────────────────────── stores found while building (not in the spec's inventory) ───────────
# chat-trace.jsonl (120-char previews of every operator message), deferred.json (prompts of fired
# deferred turns), plans/<id>.json (+ the plan file the CLI wrote under ~/.claude/plans) and the
# engine's pending rotate handoff (data/handoff.json). Each is selected at plan time (ids/digests
# only, so the pending record stays text-free) and purged in step 5b.
EXTRA_MARGIN_SEC = 300.0
TRACE_FILES = ("chat-trace.jsonl", "chat-trace.jsonl.1")


def _within(ts: Any, first_ts: "float | None", last_ts: "float | None",
            margin: float = EXTRA_MARGIN_SEC) -> bool:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return False
    if first_ts is None or last_ts is None:
        return False
    return first_ts - margin <= t <= max(first_ts, last_ts) + margin


def _chat_ok(chat_id: Any, chat_ids: "set[str]") -> bool:
    """A line tied to a chat goes only when that chat is one the session ran in; a line without a
    chat id goes only when the session itself has no chat evidence (single-chat legacy)."""
    if chat_id:
        return str(chat_id) in chat_ids
    return not chat_ids


def select_trace_lines(path: Path, *, keys: "set[str]", chat_ids: "set[str]",
                       first_ts: "float | None", last_ts: "float | None",
                       margin: float = EXTRA_MARGIN_SEC,
                       previews: "tuple[set[str], float] | None" = None) -> "list[str]":
    """chat-trace lines of the session: its session_key(s), its chat(s), inside its time span.
    `previews` = (collapsed prompt prefixes, earliest ts): a line before the span whose preview is
    one of those prompts also goes (a message queued while the previous, kept turn ran)."""
    out: list = []
    try:
        raw_lines = path.read_bytes().splitlines(keepends=True)
    except OSError:
        return out
    for raw in raw_lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if not (isinstance(r, dict) and r.get("session_key") in keys and _chat_ok(r.get("chat_id"), chat_ids)):
            continue
        if _within(r.get("ts"), first_ts, last_ts, margin):
            out.append(digest(raw))
        elif previews and isinstance(r.get("preview"), str) and _within(r.get("ts"), previews[1], last_ts, margin):
            pv = collapse(r["preview"]).rstrip("…")
            if len(pv) >= 20 and any(p.startswith(pv) or pv.startswith(p) for p in previews[0]):
                out.append(digest(raw))
    return out


def _prompt_keys(prompts: "list[tuple[float | None, frozenset]]") -> "set[str]":
    out: set = set()
    for _ts, ks in prompts:
        out |= set(ks)
    return out


def _iso_epoch(v: Any) -> "float | None":
    if not isinstance(v, str) or not v:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def select_deferred(data_dir: Path, *, keys: "set[str]",
                    prompts: "list[tuple[float | None, frozenset]]",
                    first_ts: "float | None" = None, last_ts: "float | None" = None,
                    margin: float = EXTRA_MARGIN_SEC) -> "list[str]":
    """Ids of deferred.json records that already ran (status != pending) with one of the session's
    prompts in one of its session_keys AND fired inside the session's time span (the same short
    prompt deferred in another session of the chat is not this one's). A pending record is future
    operator input: it stays."""
    try:
        recs = json.loads((Path(data_dir) / "deferred.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pk = _prompt_keys(prompts)
    out: list = []
    for r in recs if isinstance(recs, list) else []:
        if not isinstance(r, dict) or r.get("status") == "pending" or r.get("session_key") not in keys:
            continue
        when = _iso_epoch(r.get("fired_at")) or _iso_epoch(r.get("created"))
        if not _within(when, first_ts, last_ts, margin):
            continue
        text = r.get("prompt")
        if isinstance(text, str) and text and (digest(collapse(text)) in pk
                                               or digest(short(collapse(text), 300)) in pk):
            out.append(str(r.get("id")))
    return out


def _plan_file_ok(path: str, home: Path) -> bool:
    """Only a plan file the CLI itself wrote: a regular `.md` directly in ~/.claude/plans or an
    account's plans dir. Neither the file nor its dir may be a symlink (a symlinked plans dir would
    make its target deletable), and the parent is compared WITHOUT resolving."""
    p = Path(path)
    if not p.is_absolute() or p.suffix != ".md" or p.is_symlink() or not p.is_file():
        return False
    roots = [home / ".claude" / "plans"] + sorted((home / ".claude-accounts").glob("*/plans"))
    for root in roots:
        try:
            if p.parent == root and not root.is_symlink() and root.is_dir():
                return True
        except OSError:
            continue
    return False


def select_plans(data_dir: Path, home: Path, *, keys: "set[str]", chat_ids: "set[str]",
                 first_ts: "float | None", last_ts: "float | None",
                 margin: float = EXTRA_MARGIN_SEC) -> "tuple[list[str], list[str]]":
    """Plan-approval sidecars (data/plans/<id>.json) made during the session, and the plan files
    they point at. -> (sidecar ids, plan file paths)."""
    ids: list = []
    files: list = []
    pdir = Path(data_dir) / "plans"
    try:
        entries = sorted(pdir.glob("*.json"))
    except OSError:
        return ids, files
    for f in entries:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict) or r.get("session_key") not in keys:
            continue
        if not (_chat_ok(r.get("chat_id"), chat_ids) and _within(r.get("created_at"), first_ts, last_ts, margin)):
            continue
        ids.append(f.stem)
        pf = r.get("plan_file_path")
        if isinstance(pf, str) and pf and _plan_file_ok(pf, home):
            files.append(pf)
    return ids, files


def purge_deferred(data_dir: Path, ids: "list[str]", lock: Any) -> int:
    if not ids:
        return 0
    want = set(ids)
    p = Path(data_dir) / "deferred.json"
    with lock:
        try:
            recs = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(recs, list):
            return 0
        keep = [r for r in recs if not (isinstance(r, dict) and str(r.get("id")) in want)]
        gone = len(recs) - len(keep)
        if gone:
            tmp = p.with_name(p.name + ".forget-tmp")
            tmp.write_text(json.dumps(keep, indent=2), encoding="utf-8")
            os.replace(tmp, p)
    return gone


def purge_plans(data_dir: Path, home: Path, ids: "list[str]", files: "list[str]") -> int:
    n = 0
    pdir = Path(data_dir) / "plans"
    for pid in ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid or ""):
            continue
        try:
            (pdir / f"{pid}.json").unlink()
            n += 1
        except OSError:
            pass
    for pf in files:
        if _plan_file_ok(pf, home):          # re-checked: the record is not trusted blindly
            try:
                Path(pf).unlink()
                n += 1
            except OSError:
                pass
    return n


def default_residue_roots(data_dir: Path, home: "Path | None" = None) -> "list[Path]":
    """Broad roots every provider's leftovers could hide in (the adapter adds its own)."""
    home = Path(home) if home else Path.home()
    roots: list = [home / ".claude" / "history.jsonl", home / ".claude" / "plans", Path(data_dir),
                   home / ".gemini" / "antigravity-cli"]
    acc = home / ".claude-accounts"
    if acc.is_dir():
        for d in sorted(acc.iterdir()):
            for sub in ("file-history", "session-env", "history.jsonl", "plans"):
                roots.append(d / sub)
    journal = os.environ.get("FORGET_JOURNAL_DIR") or os.environ.get("JOURNAL_DIR") or str(home / "vault" / "Journal")
    roots.append(Path(journal))
    return roots


def _classify_hit(path: str, data_dir: Path, home: Path) -> str:
    if path.startswith(str(data_dir)):
        return "cockpit data"
    for frag, label in ((".claude/projects", "claude transcripts"), (".claude-accounts", "claude account"),
                        (".claude/", "claude home"), (".codex", "codex home"),
                        (".gemini", "agy / gemini"), ("Journal", "diary"), ("grok", "grok home")):
        if frag in path:
            return label
    return "other"


def _fts_phrases(windows: "list[str]", limit: int = 8) -> "list[str]":
    out: list = []
    for w in windows:
        toks = re.findall(r"\w{2,}", w)          # \w is unicode: Cyrillic content is checked too
        if len(toks) >= 4:
            out.append(" ".join(toks[1:5]))     # skip the first token: a window may start mid-word
        if len(out) >= limit:
            break
    return out


def _search_db_residue(db_path: Path, windows: "list[str]", deadline: float) -> "str | None":
    if not Path(db_path).exists():
        return None
    phrases = _fts_phrases(windows)
    if not phrases:
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        for ph in phrases:
            try:
                row = conn.execute("SELECT path FROM docs WHERE docs MATCH ? LIMIT 1",
                                   ('"' + ph.replace('"', "") + '"',)).fetchone()
            except sqlite3.Error:
                continue
            if row:
                return str(row[0])
    finally:
        conn.close()
    return None


def _scan_files_python(roots: "list[Path]", patterns: "list[bytes]", deadline: float,
                       skip_names: "tuple[str, ...]") -> "tuple[list[str], bool]":
    hits: list = []
    for root in roots:
        walk = [(str(root), [], [root.name])] if root.is_file() else os.walk(root)
        for dp, _dns, fns in walk:
            for fn in fns:
                if time.monotonic() > deadline:
                    return hits, True
                if fn.startswith(skip_names):
                    continue
                p = os.path.join(dp, fn) if not root.is_file() else str(root)
                try:
                    with open(p, "rb") as f:
                        prev = b""
                        while True:
                            chunk = f.read(8 << 20)
                            if not chunk:
                                break
                            buf = prev + chunk
                            if any(pat in buf for pat in patterns):
                                hits.append(p)
                                break
                            prev = buf[-256:]
                except OSError:
                    continue
    return hits, False


async def residue_scan(data_dir: Path, windows: "list[str]", roots: "list[Path]", *,
                       budget: "float | None" = None, home: "Path | None" = None,
                       exclude: "tuple[str, ...]" = ()) -> dict:
    """Report-only scan for distinctive strings of the deleted content. Never deletes.

    `rg -a -l -F -f -` (patterns on stdin, never written to disk) over the roots; search.db is
    queried through SQL (FTS phrase match) instead. Result: {"clean", "hits": [{path, store}],
    "truncated"}. Paths only, never text."""
    data_dir = Path(data_dir)
    home = Path(home) if home else Path.home()
    budget = residue_budget_sec() if budget is None else budget
    deadline = time.monotonic() + budget
    hits: list = []
    truncated = False
    windows = [w for w in windows if w]
    roots = [Path(r) for r in roots if Path(r).exists()]
    loop = asyncio.get_running_loop()
    if windows:
        sdb = await loop.run_in_executor(None, _search_db_residue, data_dir / "search.db", windows, deadline)
        if sdb:
            hits.append({"path": f"{data_dir / 'search.db'} (indexed row in {Path(sdb).name})",
                         "store": "search index"})
        rg = _rg()
        found: list = []
        skip = ("search.db", "forget-") + tuple(exclude)
        if rg and roots:
            args = [rg, "-a", "-l", "-F", "-f", "-", "-uu", "--no-messages",
                    "-g", "!search.db*", "-g", "!forget-*", "-g", f"!{PENDING_DIR}/*"]
            for e in exclude:
                args += ["-g", f"!{e}*"]
            args += ["--", *map(str, roots)]
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate("\n".join(windows).encode("utf-8")),
                    timeout=max(0.1, deadline - time.monotonic()))
                found = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln]
                if proc.returncode not in (0, 1):    # 2 = an error: what it did not read is unknown
                    truncated = True
            except asyncio.TimeoutError:
                truncated = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        elif roots:
            pats = [w.encode("utf-8") for w in windows]
            found, truncated = await loop.run_in_executor(
                None, _scan_files_python, roots, pats, deadline, skip)
        for p in found[:200]:
            hits.append({"path": p, "store": _classify_hit(p, data_dir, home)})
        if len(found) > 200:
            truncated = True
    # Nothing to look for (every piece of the content was too short to be distinctive) is NOT a
    # clean result: the scan simply could not check. `checked: False` tells the dialog to say so.
    checked = bool(windows)
    return {"clean": checked and not hits and not truncated, "hits": hits, "truncated": truncated,
            "checked": checked}


def write_inbox_note(data_dir: Path, provider: str, session_id: str, result: dict) -> Path:
    inbox = Path(data_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / f"forget-{session_id[:8]}.txt"
    lines = [f"Forget {provider} session {session_id[:8]}: residue scan "
             + ("clean" if result.get("clean") else f"found {len(result.get('hits', []))} place(s)"
                + (" (scan hit its time budget)" if result.get("truncated") else ""))]
    lines += [f"  {h['store']}: {h['path']}" for h in result.get("hits", [])]
    if isinstance(result.get("note"), str) and result["note"]:
        lines.append(result["note"])
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ───────────────────────────── engine wiring (injected by the webapp) ──────────────────────────
@dataclass
class Env:
    """Everything the pipeline needs from the cockpit, injectable so every store purge is
    testable alone. `state` implements the StateOps methods used below (all optional)."""

    data_dir: Path
    timeline_lock: Any = field(default_factory=threading.Lock)
    audit_lock: Any = field(default_factory=threading.Lock)
    search_lock: "Callable[[], Any] | None" = None      # -> async context manager
    prompt_norm: "Callable[[str], str] | None" = None
    state: Any = None
    publish: "Callable[[str, dict], None] | None" = None
    home: "Path | None" = None
    trace_lock: Any = field(default_factory=threading.Lock)
    deferred_lock: Any = field(default_factory=threading.Lock)


_CONFIG: dict = {}


def configure(**kw: Any) -> None:
    """Webapp startup: `env_factory(ctx) -> Env`, used by `resume_pending`."""
    _CONFIG.update(kw)


@dataclass
class Plan:
    provider: str
    session_id: str
    project_id: str
    cwd: str
    session_key: str
    art: SessionArtifacts
    held: Held
    run_ids: "list[str]"
    timeline: "dict[str, list[str]]"       # file path -> line digests to drop
    audit: "dict[str, list[str]]"
    unattributed: int
    media: "list[str]"
    token: str
    files: int = 0
    bytes: int = 0
    active_chats: "list[str]" = field(default_factory=list)
    lineage: "list[dict]" = field(default_factory=list)
    diary_days: "list[str]" = field(default_factory=list)
    bound: "list[dict]" = field(default_factory=list)
    handoffs: "list[dict]" = field(default_factory=list)   # armed runtime_handoffs built from the content
    extra: dict = field(default_factory=dict)               # stores found while building (step 5b)
    search_checkpoint: str = ""                              # set by execute: "busy" = WAL not truncated
    mode: str = "session"                                    # "session" (A) | "cut" (B, forget from here)
    chat_id: str = ""                                        # B: the chat whose session is cut
    restored_prompt: str = ""                                # B: back to the composer (never journaled)

    def record(self) -> dict:
        """The pending-journal form: ids, paths, digests. No conversation text."""
        a = self.art
        return {
            "token": self.token, "provider": self.provider, "session_id": self.session_id,
            "project_id": self.project_id, "cwd": self.cwd, "session_key": self.session_key,
            "created": time.time(),
            "art": {"provider": a.provider, "session_id": a.session_id, "cwd": a.cwd,
                    "paths": [str(p) for p in a.paths], "first_ts": a.first_ts, "last_ts": a.last_ts,
                    "extra": a.extra},
            "run_ids": self.run_ids, "timeline": self.timeline, "audit": self.audit,
            "media": self.media, "bound": self.bound, "handoffs": self.handoffs,
            "extra": self.extra, "mode": self.mode, "chat_id": self.chat_id,
        }


def _tree_size(paths: "list[Path]") -> "tuple[int, int]":
    files = size = 0
    for p in paths:
        try:
            if p.is_dir() and not p.is_symlink():
                for dp, _d, fns in os.walk(p):
                    for fn in fns:
                        try:
                            size += os.lstat(os.path.join(dp, fn)).st_size
                            files += 1
                        except OSError:
                            pass
            elif p.exists() or p.is_symlink():
                size += os.lstat(p).st_size
                files += 1
        except OSError:
            pass
    return files, size


def _diary_days(first_ts: "float | None", last_ts: "float | None", home: "Path | None") -> "list[str]":
    if first_ts is None or last_ts is None:
        return []
    journal = Path(os.environ.get("FORGET_JOURNAL_DIR") or os.environ.get("JOURNAL_DIR")
                   or str((Path(home) if home else Path.home()) / "vault" / "Journal"))
    out: list = []
    day = time.strftime("%Y-%m-%d", time.localtime(first_ts))
    end = time.strftime("%Y-%m-%d", time.localtime(last_ts))
    t = first_ts
    while True:
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if (journal / f"{day}.md").exists():
            out.append(day)
        if day >= end or len(out) > 400:
            break
        t += 86400
    return out


def compute_token(provider: str, session_id: str, run_ids: "list[str]", fingerprint: str) -> str:
    h = hashlib.sha256()
    for part in (provider, session_id, "all", ",".join(sorted(run_ids)), fingerprint):
        h.update(part.encode("utf-8", "replace") + b"\0")
    return h.hexdigest()


def build_plan(env: Env, adapter: "Adapter", ctx: Mapping[str, Any], project: Mapping[str, Any],
               session_id: str, *, timeline_files: "list[Path]", session_key: str,
               bound: "list[dict] | None" = None, lineage: "list[dict] | None" = None,
               audit_names: "set[str] | None" = None) -> Plan:
    """Dry-run computation (blocking: run it in an executor). Reads the session, derives the
    held strings, resolves exactly which timeline/audit lines belong to it."""
    art = adapter.locate(ctx, project, session_id)
    if art is None:
        raise ForgetError(404, "not_found", "session not found")
    held = collect_held(adapter.read_content(art), seed=session_id, prompt_norm=env.prompt_norm)
    known = set(mark_run_ids(env.data_dir, session_id))
    timeline: dict = {}
    unattributed = 0
    # `.1` before the live file: a run_start that rotated away still tags its later lines
    ordered = sorted({Path(p) for p in timeline_files}, key=lambda p: (p.name.endswith(".1") is False, str(p)))
    chat_ids: set = {str(b["chat_id"]) for b in (bound or []) if b.get("chat_id")}
    shared: dict = {"run_sessions": timeline_run_sessions(ordered)}
    for tp in ordered:
        sel = select_timeline_lines(tp, session_id=session_id, known_runs=known,
                                    prompts=held.prompts, prompt_norm=env.prompt_norm, shared=shared)
        unattributed += sel["unattributed"]
        chat_ids |= set(sel.get("chat_ids") or [])
        if sel["digests"]:
            timeline[str(tp)] = sel["digests"]
    for m in _read_jsonl_dicts(turn_marks_path(env.data_dir, session_id)):
        if m.get("chat_id"):
            chat_ids.add(str(m["chat_id"]))
    audit: dict = {}
    audit_dir = Path(env.data_dir) / "audit"
    # the `[Project]` label the engine writes is the project's name (topics `project`); the month file
    # is shared by every project, so only these labels are ever considered
    names = set(audit_names or ()) | {str(project.get("name") or ""), str(project.get("id") or "")}
    names.discard("")
    audit_consumed: set = set()
    months = audit_months(art.first_ts, art.last_ts)
    if audit_dir.is_dir():
        for af in sorted(audit_dir.glob("audit-*.log")):
            if months is not None and af.stem[len("audit-"):] not in months:
                continue
            sel = select_audit_lines(af, known_runs=known, prompts=held.prompts,
                                     project_names=names, consumed=audit_consumed)
            unattributed += sel["unattributed"]
            if sel["digests"]:
                audit[str(af)] = sel["digests"]
    fp_fn = getattr(adapter, "fingerprint", None)
    fingerprint = str(fp_fn(art)) if callable(fp_fn) else str(held.turns)
    run_ids = sorted(known)
    sibling = getattr(adapter, "sibling_roots", None)
    roots = sibling(ctx, art) if callable(sibling) else []
    media = plan_media(env.data_dir, held, roots, session_id) if held.media else []
    files, size = _tree_size(list(art.paths))
    finder = getattr(env.state, "find_handoffs", None)
    handoffs = finder(held.windows, session_id=session_id) if callable(finder) else []
    keys = {session_key, *[b.get("session_key") for b in (bound or [])]} - {"", None}
    home = Path(env.home) if env.home else Path.home()
    trace = {}
    for name in TRACE_FILES:
        tp = Path(env.data_dir) / name
        digs = select_trace_lines(tp, keys=keys, chat_ids=chat_ids, first_ts=art.first_ts, last_ts=art.last_ts)
        if digs:
            trace[str(tp)] = digs
    plan_ids, plan_files = select_plans(env.data_dir, home, keys=keys, chat_ids=chat_ids,
                                        first_ts=art.first_ts, last_ts=art.last_ts)
    rot_finder = getattr(env.state, "find_pending_rotate_handoffs", None)
    extra = {
        "trace": trace,
        "deferred": select_deferred(env.data_dir, keys=keys, prompts=held.prompts,
                                    first_ts=art.first_ts, last_ts=art.last_ts),
        "plans": plan_ids, "plan_files": plan_files,
        "rotate_handoffs": rot_finder(keys, held.windows) if callable(rot_finder) and held.windows else [],
    }
    return Plan(
        provider=adapter.name, session_id=session_id, project_id=str(project.get("id", "")),
        cwd=str(project.get("cwd", art.cwd)), session_key=session_key, art=art, held=held,
        run_ids=run_ids, timeline=timeline, audit=audit, unattributed=unattributed, media=media,
        token=compute_token(adapter.name, session_id, run_ids, fingerprint),
        files=files, bytes=size, bound=list(bound or []),
        active_chats=[b["chat_id"] for b in (bound or []) if b.get("chat_id")],
        lineage=list(lineage or []), diary_days=_diary_days(art.first_ts, art.last_ts, env.home),
        handoffs=handoffs, extra=extra)


def plan_view(plan: Plan) -> dict:
    """The dry-run body of endpoint A (contract shape)."""
    limits = [VENDOR_LIMITS[plan.provider]] if plan.provider in VENDOR_LIMITS else []
    limits += list(ALWAYS_LIMITS)
    warnings: list = []
    if plan.unattributed:
        warnings.append(f"{plan.unattributed} log line(s) could not be attributed to this session and stay; "
                        "the residue scan after the delete looks for what is left.")
    if plan.active_chats:
        warnings.append("This is the current session of an open chat; the chat starts fresh.")
    kept = [k for k in (plan.art.extra or {}).get("kept_copies") or [] if isinstance(k, dict)]
    if kept:
        warnings.append(f"{len(kept)} forked thread(s) copied from this one stay: a fork is an independent "
                        "thread (often your own), so it is not deleted with its source; the residue scan "
                        "will show where its copy of the text lives.")
    return {
        "provider": plan.provider, "session_id": plan.session_id, "title": plan.art.title,
        "turns": plan.held.turns, "first_ts": plan.art.first_ts, "last_ts": plan.art.last_ts,
        "files": plan.files, "bytes": plan.bytes, "active_in_chats": plan.active_chats,
        "side_effects": plan.held.side_effects, "lineage": plan.lineage,
        "diary_days": plan.diary_days, "limits": limits, "warnings": warnings,
    }


# ───────────────────────────── forget from here (B): plan ──────────────────────────────────────
CUT_LOOKBACK_SEC = 60.0      # extra-store lines between the last kept turn's end and the cut's start


def compute_cut_token(provider: str, session_id: str, cut: int, run_ids: "list[str]") -> str:
    h = hashlib.sha256()
    for part in (provider, session_id, f"cut={int(cut)}", ",".join(sorted(run_ids))):
        h.update(part.encode("utf-8", "replace") + b"\0")
    return h.hexdigest()


class _PromptTap:
    """Pass-through over adapter content that keeps the collapsed prefixes of the prompts seen
    (in memory only), so the chat-trace previews of the dropped turns can be recognised."""

    def __init__(self, items: "Iterator[ContentItem]"):
        self._items = items
        self.prefixes: set = set()

    def __iter__(self):
        for it in self._items:
            if it.kind == "prompt" and it.text:
                c = collapse(it.text)
                if len(c) >= 20:
                    self.prefixes.add(c[:200])
            yield it


def build_cut_plan(env: Env, adapter: "Adapter", ctx: Mapping[str, Any], project: Mapping[str, Any],
                   session_id: str, anchor_uuid: "str | None", *, chat_id: str, timeline_files: "list[Path]",
                   session_key: str, bound: "list[dict] | None" = None,
                   lineage: "list[dict] | None" = None, audit_names: "set[str] | None" = None,
                   anchor_run_id: "str | None" = None) -> Plan:
    """Dry run of B (blocking: run it in an executor). The adapter finds the cut and what goes; every
    cockpit store is selected by the dropped run ids or the dropped span, never by the session as a
    whole (that would take the kept turns too)."""
    locate = getattr(adapter, "locate_cut", None)
    if not callable(locate) or not getattr(adapter, "can_forget_turn", False):
        raise ForgetError(400, "no_capability", f"{adapter.name} turns cannot be forgotten here")
    art, restored_raw = locate(ctx, project, session_id, anchor_uuid, anchor_run_id=anchor_run_id)
    e = art.extra
    dropped = set(e.get("dropped_runs") or [])
    tap = _PromptTap(adapter.read_cut_content(art))
    held = collect_held(iter(tap), seed=session_id, prompt_norm=env.prompt_norm)
    held.turns = len(dropped)
    transcript = Path(e["transcript"])
    cut = int(e["cut"])
    if held.windows and cut > 0:
        # a string the kept turns also hold is not a leftover of the cut: the scan must not report it
        kept = find_in_range(transcript, 0, cut, held.windows)
        held.windows = [w for w in held.windows if w not in kept]
    timeline: dict = {}
    for tp in sorted({Path(x) for x in timeline_files}, key=str):
        digs = select_run_lines(tp, dropped)
        if digs:
            timeline[str(tp)] = digs
    names = set(audit_names or ()) | {str(project.get("name") or ""), str(project.get("id") or "")}
    names.discard("")
    audit: dict = {}
    audit_dir = Path(env.data_dir) / "audit"
    now = time.time()
    months = audit_months(art.first_ts, max(now, art.last_ts or now))
    if audit_dir.is_dir() and dropped:
        for af in sorted(audit_dir.glob("audit-*.log")):
            if months is not None and af.stem[len("audit-"):] not in months:
                continue
            digs = select_audit_runs(af, dropped, names)
            if digs:
                audit[str(af)] = digs
    # media named in the dropped turns: not in the kept prefix, not in the prompt going back to the
    # composer (its attachments go back with it), not in another session of the project
    media: list = []
    if held.media:
        restored_names = {m.group(2) for m in _MEDIA_RE.finditer(restored_raw or "")}
        cand = [(pid, n) for pid, n in held.media if n not in restored_names]
        in_prefix = find_in_range(transcript, 0, cut, [n for _pid, n in cand]) if cut > 0 else set()
        sub = Held(media=[(pid, n) for pid, n in cand if n not in in_prefix])
        sibling = getattr(adapter, "sibling_roots", None)
        roots = sibling(ctx, art) if callable(sibling) else []
        media = plan_media(env.data_dir, sub, roots, session_id) if sub.media else []
    files, size = _tree_size(list(art.paths))
    size += max(0, int(e.get("orig_size") or 0) - cut)
    chat_ids = {str(chat_id)} if chat_id else set()
    keys = {session_key, *[b.get("session_key") for b in (bound or [])]} - {"", None}
    first = float(art.first_ts or now)
    prev_end = e.get("prev_end")
    lower = first - 2.0
    if isinstance(prev_end, (int, float)) and prev_end and prev_end < lower:
        lower = max(float(prev_end), first - CUT_LOOKBACK_SEC)
    upper = now + 60.0
    trace = {}
    for name in TRACE_FILES:
        tp = Path(env.data_dir) / name
        digs = select_trace_lines(tp, keys=keys, chat_ids=chat_ids, first_ts=lower, last_ts=upper, margin=0.0,
                                  previews=(tap.prefixes, lower - 3600.0) if tap.prefixes else None)
        if digs:
            trace[str(tp)] = digs
    home = Path(env.home) if env.home else Path.home()
    plan_ids, plan_files = select_plans(env.data_dir, home, keys=keys, chat_ids=chat_ids,
                                        first_ts=lower, last_ts=upper, margin=0.0)
    rot_finder = getattr(env.state, "find_pending_rotate_handoffs", None)
    finder = getattr(env.state, "find_handoffs", None)
    handoffs = finder(held.windows, session_id=session_id, since=first) if callable(finder) else []
    extra = {
        "trace": trace,
        "deferred": select_deferred(env.data_dir, keys=keys, prompts=held.prompts,
                                    first_ts=lower, last_ts=upper, margin=0.0),
        "plans": plan_ids, "plan_files": plan_files,
        "rotate_handoffs": rot_finder(keys, held.windows) if callable(rot_finder) and held.windows else [],
    }
    lin = [x for x in (lineage or []) if not isinstance(x.get("ts"), (int, float)) or x["ts"] >= first - 2.0]
    norm = env.prompt_norm or (lambda x: x)
    restored = norm(restored_raw) if restored_raw else ""
    run_ids = sorted(dropped)
    return Plan(
        provider=adapter.name, session_id=session_id, project_id=str(project.get("id", "")),
        cwd=str(project.get("cwd", art.cwd)), session_key=session_key, art=art, held=held,
        run_ids=run_ids, timeline=timeline, audit=audit, unattributed=0, media=media,
        token=compute_cut_token(adapter.name, session_id, cut, run_ids),
        files=files, bytes=size, bound=list(bound or []),
        active_chats=[b["chat_id"] for b in (bound or []) if b.get("chat_id")],
        lineage=lin, diary_days=_diary_days(art.first_ts, art.last_ts, env.home),
        handoffs=handoffs, extra=extra, mode="cut", chat_id=str(chat_id or ""),
        restored_prompt=restored if isinstance(restored, str) else "")


def plan_view_cut(plan: Plan) -> dict:
    """The dry-run body of endpoint B (contract P4)."""
    v = plan_view(plan)
    v["warnings"] = [w for w in v["warnings"] if not w.startswith("This is the current session")]
    v.update({"chat_id": plan.chat_id, "whole_session": False, "turns": len(plan.run_ids),
              "restored_prompt": plan.restored_prompt})
    return v


# ───────────────────────────── execute / resume ────────────────────────────────────────────────
def pending_dir(data_dir: Path) -> Path:
    return Path(data_dir) / PENDING_DIR


def _write_pending(data_dir: Path, rec: dict) -> Path:
    d = pending_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rec['token']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, p)
    return p


def _art_from_record(rec: dict) -> SessionArtifacts:
    a = rec["art"]
    return SessionArtifacts(provider=a["provider"], session_id=a["session_id"], cwd=a.get("cwd", ""),
                            paths=[Path(p) for p in a.get("paths", [])], first_ts=a.get("first_ts"),
                            last_ts=a.get("last_ts"), extra=a.get("extra") or {})


def _session_dir_of(art: SessionArtifacts) -> "str | None":
    d = (art.extra or {}).get("session_dir")
    return str(d) if d else None


async def _run_steps(env: Env, adapter: "Adapter", ctx: Mapping[str, Any], rec: dict,
                     held: "Held | None") -> dict:
    """Steps 4-10 of the spec. Every step is idempotent and works from the pending record alone
    (`held` only adds the armed-handoff check, which a resumed run skips)."""
    loop = asyncio.get_running_loop()
    art = _art_from_record(rec)
    sid, provider = rec["session_id"], rec["provider"]
    counts = {"files": 0, "timeline_lines": 0, "audit_lines": 0, "search_rows": 0, "media": 0,
              "unattributed": rec.get("unattributed", 0)}
    # 4. provider files
    res = await loop.run_in_executor(None, adapter.delete, ctx, art)
    counts["files"] = int((res or {}).get("files", 0))
    left = await loop.run_in_executor(None, adapter.verify_gone, ctx, art)
    if left:
        raise ForgetError(500, "failed", f"{len(left)} path(s) survived the delete: " + ", ".join(left[:3]))
    _after_step(4)
    # 5. chat binding + labels + links + lineage + armed handoff
    st = env.state
    if st is not None and hasattr(st, "unbind"):
        await st.unbind(provider, sid)
    if st is not None and hasattr(st, "drop_labels_links"):
        st.drop_labels_links(sid)
    remove_lineage(env.data_dir, sid)
    if st is not None and rec.get("handoffs") and hasattr(st, "drop_handoffs"):
        await st.drop_handoffs(rec["handoffs"])
    if st is not None and hasattr(st, "clear_runtime"):
        st.clear_runtime(rec.get("session_key", ""), [b.get("session_key") for b in rec.get("bound", [])])
    _after_step(5)
    # 5b. stores found while building: chat-trace, fired deferred prompts, plan sidecars + plan files,
    # the engine's pending rotate handoff
    ex = rec.get("extra") or {}
    for path, digs in (ex.get("trace") or {}).items():
        counts["trace_lines"] = counts.get("trace_lines", 0) + await loop.run_in_executor(
            None, purge_lines_by_digest, Path(path), digs, env.trace_lock)
    # on the loop thread on purpose: the cockpit's deferred.json writers are synchronous loop code
    counts["deferred"] = purge_deferred(Path(env.data_dir), list(ex.get("deferred") or []), env.deferred_lock)
    counts["plans"] = await loop.run_in_executor(
        None, purge_plans, Path(env.data_dir), Path(env.home) if env.home else Path.home(),
        list(ex.get("plans") or []), list(ex.get("plan_files") or []))
    if st is not None and ex.get("rotate_handoffs") and hasattr(st, "drop_pending_rotate_handoffs"):
        st.drop_pending_rotate_handoffs(ex["rotate_handoffs"])
    # 6. chat-media
    media_root = (Path(env.data_dir) / "chat-media").resolve()
    for m in rec.get("media", []):
        try:
            mp = Path(m).resolve()
            if media_root in mp.parents and mp.is_file():
                mp.unlink()
                counts["media"] += 1
        except OSError:
            pass
    _after_step(6)
    # 7. timeline + audit. Digests are exact line hashes, so each file PAIR (live + `.1`) is purged
    # with the union of its digests: a rotation between the plan and this step moves the session's
    # lines from the live file into `.1`, and a path-keyed purge would miss them.
    pairs: dict = {}
    for path, digs in (rec.get("timeline") or {}).items():
        live = path[:-2] if path.endswith(".1") else path
        pairs.setdefault(live, set()).update(digs)
    for live, digs in sorted(pairs.items()):
        for path in (live, live + ".1"):
            counts["timeline_lines"] += await loop.run_in_executor(
                None, purge_lines_by_digest, Path(path), digs, env.timeline_lock)
    for path, digs in (rec.get("audit") or {}).items():
        counts["audit_lines"] += await loop.run_in_executor(
            None, purge_lines_by_digest, Path(path), digs, env.audit_lock)
    _after_step(7)
    # 8. search.db — every timeline file of the project's cwd is re-indexed from its rewritten form
    tl_paths = sorted({p for p in (rec.get("timeline") or {})} | _live_timeline_paths(rec))
    transcripts = [p for p in rec["art"]["paths"] if p.endswith(".jsonl")]
    lock = env.search_lock() if env.search_lock else None
    db = Path(env.data_dir) / "search.db"

    async def _search() -> dict:
        return await loop.run_in_executor(
            None, lambda: purge_search(db, transcript_paths=transcripts, session_id=sid, reindex_paths=tl_paths))

    if lock is not None:
        async with lock:
            sres = await _search()
    else:
        sres = await _search()
    counts["search_rows"] = sres["rows"]
    counts["search_checkpoint"] = sres.get("checkpoint")
    _after_step(8)
    # 9. usage.db
    if transcripts:
        await loop.run_in_executor(None, purge_usage, Path(env.data_dir) / "usage.db",
                                   transcripts[0], _session_dir_of(art))
    _after_step(9)
    # 10. turn marks, pending record
    try:
        turn_marks_path(env.data_dir, sid).unlink()
    except OSError:
        pass
    try:
        (pending_dir(env.data_dir) / f"{rec['token']}.json").unlink()
    except OSError:
        pass
    return counts


async def _run_cut_steps(env: Env, adapter: "Adapter", ctx: Mapping[str, Any], rec: dict) -> dict:
    """Steps 4-10 for B. Idempotent from the pending record alone. The session stays: no unbind, no
    label/link/lineage removal; only the dropped turns' traces go."""
    loop = asyncio.get_running_loop()
    art = _art_from_record(rec)
    e = art.extra
    sid = rec["session_id"]
    dropped = set(rec.get("run_ids") or [])
    counts = {"turns": len(dropped), "files": 0, "timeline_lines": 0, "audit_lines": 0,
              "search_rows": 0, "media": 0}
    # 4. cut the transcript, remove the dropped turns' artifacts, verify
    res = await loop.run_in_executor(None, adapter.cut, ctx, art)
    counts["files"] = int((res or {}).get("files", 0))
    left = await loop.run_in_executor(None, adapter.verify_cut, ctx, art)
    if left:
        raise ForgetError(500, "failed", f"{len(left)} path(s) survived the cut: " + ", ".join(left[:3]))
    st = env.state
    if st is not None and hasattr(st, "transcript_cut"):
        st.transcript_cut(e.get("transcript") or "")
    _after_step(4)
    # 5. armed handoffs built from the dropped turns, the runtime rings of the chat
    if st is not None and rec.get("handoffs") and hasattr(st, "drop_handoffs"):
        await st.drop_handoffs(rec["handoffs"])
    if st is not None and hasattr(st, "clear_runtime"):
        st.clear_runtime(rec.get("session_key", ""), [b.get("session_key") for b in rec.get("bound", [])])
    _after_step(5)
    # 5b. chat-trace, fired deferred prompts, plan sidecars + files, the pending rotate handoff
    ex = rec.get("extra") or {}
    for path, digs in (ex.get("trace") or {}).items():
        counts["trace_lines"] = counts.get("trace_lines", 0) + await loop.run_in_executor(
            None, purge_lines_by_digest, Path(path), digs, env.trace_lock)
    counts["deferred"] = purge_deferred(Path(env.data_dir), list(ex.get("deferred") or []), env.deferred_lock)
    counts["plans"] = await loop.run_in_executor(
        None, purge_plans, Path(env.data_dir), Path(env.home) if env.home else Path.home(),
        list(ex.get("plans") or []), list(ex.get("plan_files") or []))
    if st is not None and ex.get("rotate_handoffs") and hasattr(st, "drop_pending_rotate_handoffs"):
        st.drop_pending_rotate_handoffs(ex["rotate_handoffs"])
    # 6. chat-media
    media_root = (Path(env.data_dir) / "chat-media").resolve()
    for m in rec.get("media", []):
        try:
            mp = Path(m).resolve()
            if media_root in mp.parents and mp.is_file():
                mp.unlink()
                counts["media"] += 1
        except OSError:
            pass
    _after_step(6)
    # 7. timeline + audit lines of the dropped runs (file pairs, as in A: a rotation in between)
    pairs: dict = {}
    for path, digs in (rec.get("timeline") or {}).items():
        live = path[:-2] if path.endswith(".1") else path
        pairs.setdefault(live, set()).update(digs)
    for live, digs in sorted(pairs.items()):
        for path in (live, live + ".1"):
            counts["timeline_lines"] += await loop.run_in_executor(
                None, purge_lines_by_digest, Path(path), digs, env.timeline_lock)
    for path, digs in (rec.get("audit") or {}).items():
        counts["audit_lines"] += await loop.run_in_executor(
            None, purge_lines_by_digest, Path(path), digs, env.audit_lock)
    _after_step(7)
    # 8. search.db: the transcript (and removed sub-agent files) are re-indexed from scratch, so the
    # cut's rows go and the kept ones come back from the cut file; the session's telemetry stays
    tl_paths = sorted({p for p in (rec.get("timeline") or {})} | _live_timeline_paths(rec))
    transcripts = [str(e.get("transcript"))] + [str(x) for x in e.get("sub_files") or []]
    lock = env.search_lock() if env.search_lock else None
    db = Path(env.data_dir) / "search.db"

    async def _search() -> dict:
        return await loop.run_in_executor(
            None, lambda: purge_search(db, transcript_paths=transcripts, session_id=sid,
                                       reindex_paths=tl_paths, drop_hits=False))

    if lock is not None:
        async with lock:
            sres = await _search()
    else:
        sres = await _search()
    counts["search_rows"] = sres["rows"]
    counts["search_checkpoint"] = sres.get("checkpoint")
    _after_step(8)
    # 9. usage.db line cursor
    await loop.run_in_executor(None, purge_usage_cut, Path(env.data_dir) / "usage.db",
                               str(e.get("transcript")), int(e.get("prefix_lines") or 0),
                               [str(x) for x in e.get("sub_files") or []])
    _after_step(9)
    # 10. the dropped runs' turn marks, the pending record
    await loop.run_in_executor(None, drop_mark_runs, Path(env.data_dir), sid, dropped)
    try:
        (pending_dir(env.data_dir) / f"{rec['token']}.json").unlink()
    except OSError:
        pass
    return counts


def drop_mark_runs(data_dir: Path, session_id: str, run_ids: "set[str]") -> int:
    """Rewrite the session's turn-mark file without the rows of `run_ids` (start AND end rows)."""
    import turn_marks as _tm
    path = turn_marks_path(data_dir, session_id)
    if not run_ids or not path.exists():
        return 0
    with _tm._LOCK:
        try:
            raw_lines = path.read_bytes().splitlines(keepends=True)
        except OSError:
            return 0
        keep: list = []
        for raw in raw_lines:
            try:
                r = json.loads(raw)
            except ValueError:
                keep.append(raw)
                continue
            if not (isinstance(r, dict) and str(r.get("run_id")) in run_ids):
                keep.append(raw if raw.endswith(b"\n") else raw + b"\n")
        gone = len(raw_lines) - len(keep)
        if gone:
            _atomic_replace_lines(path, keep)
    return gone


def _live_timeline_paths(rec: dict) -> "set[str]":
    """The live (non-`.1`) timeline files whose rows must be rebuilt when any sibling changed."""
    out: set = set()
    for p in (rec.get("timeline") or {}):
        out.add(p[:-2] if p.endswith(".1") else p)
        out.add(p if p.endswith(".1") else p + ".1")
    return out


async def execute_plan(env: Env, adapter: "Adapter", ctx: Mapping[str, Any], plan: Plan) -> dict:
    """Steps 3-10 for one session. The caller has already reserved the running slots, quiesced
    the engine and recomputed `plan` after that. A failure leaves the pending record for resume."""
    rec = plan.record()
    rec["unattributed"] = plan.unattributed
    _write_pending(env.data_dir, rec)        # 3
    _after_step(3)
    if plan.mode == "cut":
        counts = await _run_cut_steps(env, adapter, ctx, rec)
        plan.search_checkpoint = str(counts.get("search_checkpoint") or "")
        return counts
    counts = await _run_steps(env, adapter, ctx, rec, plan.held)
    counts["unattributed"] = plan.unattributed
    plan.search_checkpoint = str(counts.get("search_checkpoint") or "")
    return counts


async def resume_pending(ctx: Mapping[str, Any]) -> "list[str]":
    """Startup: finish every interrupted forget from its journal. Returns the tokens finished."""
    factory = _CONFIG.get("env_factory")
    if factory is None:
        return []
    env: Env = factory(ctx)
    d = pending_dir(env.data_dir)
    done: list = []
    if not d.is_dir():
        return done
    _load_adapters()
    running = ctx.get("running") if isinstance(ctx.get("running"), dict) else None
    for p in sorted(d.glob("*.json")):
        taken: list = []
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            adapter_ = _ADAPTERS.get(rec["provider"])
            if adapter_ is None:
                continue
            # The same guard a live forget takes: a turn that starts on the still-bound session while
            # the resume runs would re-create its files and lose them to step 4. Reserve the chats'
            # slots synchronously; a busy chat waits for the next start.
            keys = sorted({rec.get("session_key") or "", *[b.get("session_key") or "" for b in rec.get("bound") or []]} - {""})
            if running is not None:
                if any(running.get(k) is not None for k in keys):
                    print(f"[forget] resume of {p.name} postponed: a chat that uses the session is busy")
                    continue
                for k in keys:
                    running[k] = True
                    taken.append(k)
            if rec.get("mode") == "cut":
                try:
                    await _run_cut_steps(env, adapter_, ctx, rec)
                except ForgetError as exc:
                    if exc.code != "changed":
                        raise
                    # the transcript is neither the measured file nor the cut one (a turn ran after a
                    # crash): cutting now could take that turn too. Give up loudly, never guess.
                    print(f"[forget] resume of {p.name} abandoned: {exc.message}")
                    try:
                        write_inbox_note(env.data_dir, rec["provider"], rec["session_id"], {
                            "clean": False, "hits": [], "truncated": True, "checked": False,
                            "note": "forget from here could not be finished after a restart: the session "
                                    "changed. Run it again from the chat."})
                    except OSError:
                        pass
                    p.unlink(missing_ok=True)
                    continue
            else:
                await _run_steps(env, adapter_, ctx, rec, None)
            done.append(rec["token"])
        except Exception as exc:   # noqa: BLE001 - a bad record must not stop the others or the startup
            print(f"[forget] resume of {p.name} failed: {exc!r}")
        finally:
            if running is not None:
                for k in taken:
                    running.pop(k, None)
    return done


async def finish_with_residue(env: Env, adapter: "Adapter", ctx: Mapping[str, Any], plan: Plan) -> dict:
    """Step 11: scan, inbox note, bus event. Runs after the response."""
    roots = (list(adapter.residue_roots(ctx)) + [Path(d) for d in (plan.art.extra or {}).get("residue_dirs", [])]
             + default_residue_roots(env.data_dir, env.home))
    seen: set = set()
    roots = [r for r in roots if not (str(r) in seen or seen.add(str(r)))]
    result = await residue_scan(env.data_dir, plan.held.windows, roots, home=env.home)
    if plan.search_checkpoint == "busy":
        # the index WAL could not be truncated: deleted rows may still sit in its bytes, which the
        # SQL check cannot see — never call that clean
        result["truncated"] = True
        result["clean"] = False
    try:
        write_inbox_note(env.data_dir, plan.provider, plan.session_id, result)
    except OSError:
        pass
    if env.publish is not None:
        env.publish(plan.session_key, {
            "kind": "forget_residue", "provider": plan.provider, "session_id": plan.session_id,
            "clean": result["clean"], "hits": result["hits"], "truncated": result["truncated"],
            "checked": result.get("checked", True)})
    return result


def _load_adapters() -> None:
    """Import the provider adapters so they register themselves. One line per provider."""
    for mod in ("forget_claude", "forget_codex", "forget_grok"):
        try:
            __import__(mod)
        except ImportError:
            pass

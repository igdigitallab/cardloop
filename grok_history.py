"""Disk reader for Grok Build sessions (spec-095 phase P3: D7, §5.5, M14).

Pure logic over files, injectable root, no webapp/engine state. It returns the SAME shapes
``codex_engine`` returns so the consumers that already accept Codex take Grok unchanged:

* ``history_messages`` -> ``[{role, text, tools, uuid}]`` (what ``codex_engine.history_messages``
  returns; ``handoff.build_handoff`` and ``_history_messages_for_display`` run on it as is).
* ``session_context``  -> ``{context_tokens, context_window}`` for the session-history response.
* ``list_sessions``    -> rows shaped like ``codex_engine.list_threads`` (``id``, ``cwd``,
  ``name``, ``preview``, ``updatedAt``/``recencyAt`` as epoch SECONDS) plus a few additive keys.
* ``search_sessions`` / ``iter_search_docs`` -> the global-search block's input.

Why the disk and not an ACP ``session/load`` replay (D7): a replay spawns the agent and
mutates ``updatedAt``; ``chat_history.jsonl`` is a plain file.

On-disk layout (measured on grok 1.0.46 and confirmed by the CLI's own bundled
``collect_sessions.py``)::

    <GROK_HOME>/sessions/<urlencode(cwd)>/<session-id>/
        chat_history.jsonl   system | user | reasoning | assistant | tool_result rows
        summary.json         info{id,cwd}, session_summary, generated_title, updated_at, ...
        signals.json         userMessageCount / assistantMessageCount (approximate counts)

* A real operator message is a ``user`` row whose text is wrapped in ``<user_query>...</user_query>``.
  Every other ``user`` row is an injected preamble (``<user_info>``, ``<system-reminder>`` skills
  and MCP blobs) and ``synthetic_reason`` rows are harness-written: both are skipped.
* A group directory whose encoded name would exceed 255 bytes is named ``<slug>-<hash>`` and the
  original path sits in a ``.cwd`` file inside it (CLI docs, 17-sessions.md) - the lookup falls
  back to a bounded scan that matches on the percent-DECODED name or on ``.cwd``.
* ``chat_history`` rows carry no per-row timestamp and no message id: ``uuid`` is the opaque
  ``<session-id>:<byte offset of the row>`` (stable across tail windows), ``ts`` is not provided.

SECURITY (session ids and cwds can originate from HTTP): the id is validated against the strict
UUID format before ANY path join, the cwd must be absolute (its encoded name then starts ``%2F``
and can never be ``.``/``..``), a symlinked GROK_HOME / ``sessions`` / group / session dir is
refused or skipped, every resolved path is re-checked to sit inside ``<home>/sessions``, and files
are opened ``O_NOFOLLOW`` + regular-file-only (a FIFO planted as ``chat_history.jsonl`` would
otherwise block the thread forever). Reads are bounded: a huge file is read from its TAIL
(``MAX_READ_BYTES``) and a single giant line is skipped without being buffered.

All functions do blocking file IO: the web layer runs them in an executor thread.
"""
from __future__ import annotations

import collections
import json
import os
import re
import stat
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import grok_engine
import grok_jsonl

PROVIDER = "grok"

# --- knobs (module constants so tests can shrink them) -----------------------------------------
MAX_MESSAGES = 100                 # Codex parity: ``out[-100:]``
MAX_LIMIT = 5000                   # hard ceiling for a caller-supplied limit (handoff asks for ~1000)
MAX_READ_BYTES = 64 * 1024 * 1024  # a larger chat_history is read from its tail window
MAX_LINE_BYTES = 8 * 1024 * 1024   # a single longer line is skipped, never buffered whole
MAX_SUMMARY_BYTES = 1024 * 1024    # summary.json / signals.json / .cwd are small; larger = ignored
HEAD_PREVIEW_BYTES = 512 * 1024    # first-user-query lookup when a session has no title yet
MAX_GROUP_SCAN = 5000              # entries inspected when the direct group lookup misses
MAX_SESSIONS_PER_CWD = 1000        # summaries read per cwd (UUIDv7 ids sort by creation time)
MAX_ARGS_BYTES = 2 * 1024 * 1024   # a tool_call `arguments` string larger than this is not parsed
SEARCH_READ_BYTES = 4 * 1024 * 1024    # per session
SEARCH_TOTAL_BYTES = 32 * 1024 * 1024  # per iter_search_docs call (one project)
SEARCH_MAX_SESSIONS = 50
SEARCH_DOC_CHARS = 200_000
PREVIEW_CHARS = 200
SNIPPET_CHARS = 300

SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SUBAGENT_KINDS = frozenset({"subagent", "subagent_resume", "subagent_fork"})  # not chats of their own

_USER_QUERY_FULL_RE = re.compile(r"\s*<user_query>(.*)</user_query>\s*", re.S)
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$")


class GrokHistoryError(RuntimeError):
    """The Grok home cannot be read safely (a symlinked home / sessions dir). Distinct from an
    absent session, which is an ordinary empty result."""


# ------------------------------------------------------------------------------------------
# validation + path resolution
# ------------------------------------------------------------------------------------------

def valid_session_id(session_id) -> bool:
    """True for the canonical lowercase UUID form Grok generates (UUIDv7) and nothing else."""
    return isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id) is not None


def _check_cwd(cwd) -> str:
    # An unencodable (lone-surrogate) cwd needs no check of its own: quote() raises
    # UnicodeEncodeError, which is a ValueError.
    if not isinstance(cwd, str) or len(cwd) > 4096 or "\x00" in cwd or not os.path.isabs(cwd):
        raise ValueError("invalid cwd: an absolute path is required")
    return cwd


def encode_cwd(cwd: str) -> str:
    """The group directory name the CLI derives from a cwd: URL-encoding with ``/`` -> ``%2F``.

    Only a CANDIDATE: edge characters (``:``, ``@``, ``+`` ...) are not provable against the CLI
    from the sessions on this host, so the lookup also matches on the percent-decoded name.
    """
    return urllib.parse.quote(_check_cwd(cwd), safe="")


def _cwd_variants(cwd: str) -> list[str]:
    """The given cwd plus its normalised and symlink-resolved forms (the CLI may record either)."""
    out = [cwd]
    for alt in (os.path.normpath(cwd), os.path.realpath(cwd)):
        if alt not in out:
            out.append(alt)
    return out


def _home(grok_home) -> Path:
    home = Path(grok_home) if grok_home is not None else grok_engine.grok_home()
    if home.is_symlink():
        raise GrokHistoryError(f"GROK_HOME {home} is a symlink - refusing")
    return home


def _sessions_root(home: Path) -> "tuple[Path, Path]":
    """``(sessions dir, its resolved real path)``; a home without sessions simply lists nothing."""
    root = home / "sessions"
    if root.is_symlink():
        raise GrokHistoryError(f"{root} is a symlink - refusing")
    return root, root.resolve()


def _inside(path: Path, root_real: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(root_real)
    except (OSError, RuntimeError, ValueError):
        return False


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)  # lstat: a symlink to a dir is NOT a dir here
    except OSError:
        return False


def _group_dirs(root: Path, root_real: Path, cwd: str) -> list[Path]:
    """Every group directory under ``root`` that belongs to ``cwd`` (usually zero or one)."""
    variants = _cwd_variants(cwd)
    found: list[Path] = []
    for variant in variants:
        cand = root / urllib.parse.quote(variant, safe="")
        if _is_real_dir(cand) and _inside(cand, root_real):
            found.append(cand)
    if found:
        return found
    try:
        scan = os.scandir(root)
    except OSError:
        return []
    with scan:
        for i, entry in enumerate(scan):
            if i >= MAX_GROUP_SCAN:
                break
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = root / entry.name
            if entry.name.lower().startswith("%2f"):
                if urllib.parse.unquote(entry.name) in variants and _inside(path, root_real):
                    found.append(path)
            elif _cwd_file(path) in variants and _inside(path, root_real):
                found.append(path)  # long-path group: slug + hash, original path in `.cwd`
    return found


def _cwd_file(group: Path) -> "str | None":
    raw = _read_small(group / ".cwd")
    if raw is None:
        return None
    try:
        return raw.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


def _session_dir(session_id: str, cwd: str, home: Path) -> "Path | None":
    if not valid_session_id(session_id):
        raise ValueError("invalid grok session id")
    _check_cwd(cwd)
    root, root_real = _sessions_root(home)
    for group in _group_dirs(root, root_real, cwd):
        cand = group / session_id
        if _is_real_dir(cand) and _inside(cand, root_real):
            return cand
    return None


# ------------------------------------------------------------------------------------------
# bounded, symlink-proof file reads (shared with grok_usage: grok_jsonl)
# ------------------------------------------------------------------------------------------

_open_regular = grok_jsonl.open_regular


def _read_small(path: Path, cap: "int | None" = None) -> "bytes | None":
    return grok_jsonl.read_small(path, MAX_SUMMARY_BYTES if cap is None else cap)


def _read_json(path: Path) -> "dict | None":
    raw = _read_small(path)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _iter_jsonl(path: Path, *, max_bytes: "int | None" = None,
                from_head: bool = False) -> Iterator[tuple[int, dict]]:
    return grok_jsonl.iter_jsonl(path, max_bytes=MAX_READ_BYTES if max_bytes is None else max_bytes,
                                 max_line=MAX_LINE_BYTES, from_head=from_head)


# ------------------------------------------------------------------------------------------
# row parsing (the D7 rules)
# ------------------------------------------------------------------------------------------

def _parts_text(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    out.append(text)
    return out


def user_query_text(row: dict) -> "str | None":
    """The operator's own words of a ``user`` row, or None for an injected/synthetic row.

    A part that is ONE ``<user_query>`` block is unwrapped greedily (so a prompt that itself
    contains the closing tag is not cut short); a part holding several blocks (a second opening
    tag inside) yields each block. Only single-block rows have been seen on real sessions.
    """
    if row.get("synthetic_reason"):
        return None
    texts: list[str] = []
    for part in _parts_text(row.get("content")):
        full = _USER_QUERY_FULL_RE.fullmatch(part)
        if full and "<user_query>" not in full.group(1):
            blocks = [full.group(1)]
        else:
            blocks = _USER_QUERY_RE.findall(part)
        texts.extend(b.strip() for b in blocks if b.strip())
    return "\n".join(texts) if texts else None


def assistant_text(row: dict) -> str:
    return "".join(_parts_text(row.get("content")))


def default_format_tool(name: str, inp: dict) -> dict:
    """Display row for one tool call: the shape of ``webapp._format_tool`` (the web layer should
    pass its own as ``format_tool=``; ``tests/test_grok_history.py`` pins this copy to it)."""
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        return {"name": name, "kind": "bash", "cmd": inp.get("command", ""), "desc": inp.get("description", "")}
    if name == "Edit":
        old, new = inp.get("old_string", ""), inp.get("new_string", "")
        if isinstance(old, str) and len(old) > 2000:
            old = old[:2000] + "…"
        if isinstance(new, str) and len(new) > 2000:
            new = new[:2000] + "…"
        return {"name": name, "kind": "edit", "file": inp.get("file_path", ""), "old": old, "new": new}
    if name == "Write":
        content = inp.get("content", "")
        preview = (content[:2000] + "…" if len(content) > 2000 else content) if isinstance(content, str) else ""
        return {"name": name, "kind": "write", "file": inp.get("file_path", ""), "preview": preview}
    if name == "Read":
        return {"name": name, "kind": "read", "file": inp.get("file_path", "")}
    if name in ("Glob", "Grep"):
        return {"name": name, "kind": "search", "pattern": inp.get("pattern", ""), "path": inp.get("path", "")}
    first = next(iter(inp.values()), "") if inp else ""
    summary = str(first)
    if len(summary) > 200:
        summary = summary[:200] + "…"
    return {"name": name, "kind": "other", "summary": summary}


def _loads(raw: str):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _tool_rows(row: dict, format_tool: Callable[[str, dict], dict]) -> list[dict]:
    """Tool rows of one assistant row, through the SAME mapping the live engine uses.

    Parity with the live ``_Mapper``: a sub-agent spawn produces no tool row (its lifecycle is a
    separate event stream there, and a replay cannot rebuild it); every other name goes through
    ``grok_engine.map_tool`` and unknown names pass through unchanged."""
    calls = row.get("tool_calls")
    if not isinstance(calls, list):
        return []
    out: list[dict] = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            continue
        name = call["name"]
        if name in grok_engine.SUBAGENT_TOOLS:
            continue
        args = call.get("arguments")
        if isinstance(args, str):
            args = _loads(args) if len(args) <= MAX_ARGS_BYTES else None
        mapped, inp = grok_engine.map_tool(name, args)  # a non-dict `args` becomes {} in there
        try:
            tool = format_tool(mapped, inp if isinstance(inp, dict) else {})
        except Exception:  # noqa: BLE001 - a formatter bug must not take the whole history down
            tool = default_format_tool(mapped, inp if isinstance(inp, dict) else {})
        if isinstance(tool, dict):
            out.append(tool)
    return out


def _messages(path: Path, session_id: str, limit: int, format_tool: Callable[[str, dict], dict],
              max_bytes: "int | None" = None) -> list[dict]:
    """The last ``limit`` user/assistant messages of one ``chat_history.jsonl``."""
    keep: "collections.deque[dict]" = collections.deque(maxlen=limit)
    for offset, row in _iter_jsonl(path, max_bytes=max_bytes):
        kind = row.get("type")
        if kind == "user":
            text = user_query_text(row)
            if text:
                keep.append({"role": "user", "text": text, "tools": [], "uuid": f"{session_id}:{offset}"})
        elif kind == "assistant":
            text = assistant_text(row)
            tools = _tool_rows(row, format_tool)
            if text.strip() or tools:
                keep.append({"role": "assistant", "text": text, "tools": tools,
                             "uuid": f"{session_id}:{offset}"})
        # system / reasoning / tool_result (and anything unknown) are not conversation
    return list(keep)


def _clamp_limit(limit) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return MAX_MESSAGES
    return max(1, min(n, MAX_LIMIT))


# ------------------------------------------------------------------------------------------
# public API
# ------------------------------------------------------------------------------------------

def history_messages(session_id: str, cwd: str, *, grok_home=None, limit: int = MAX_MESSAGES,
                     format_tool: "Callable[[str, dict], dict] | None" = None,
                     max_bytes: "int | None" = None) -> list[dict]:
    """``[{role, text, tools, uuid}]`` of one Grok session, oldest first, newest ``limit`` kept.

    Raises ``ValueError`` for a malformed session id / cwd (the caller maps it to a 400) and
    ``GrokHistoryError`` for an unsafe home; an absent session is ``[]``. ``format_tool`` turns a
    mapped ``(name, input)`` into the display row (pass ``webapp._format_tool``); the default is a
    parity-tested copy.
    """
    home = _home(grok_home)
    sdir = _session_dir(session_id, cwd, home)
    if sdir is None:
        return []
    return _messages(sdir / "chat_history.jsonl", session_id, _clamp_limit(limit),
                     format_tool or default_format_tool, max_bytes)


def session_exists(session_id: str, cwd: str, *, grok_home=None) -> bool:
    """Does ``session_id`` exist under ``cwd``'s group? Never raises: a malformed id or cwd, a
    refused home and a missing session are all ``False``."""
    try:
        sdir = _session_dir(session_id, cwd, _home(grok_home))
    except (ValueError, GrokHistoryError):
        return False
    if sdir is None:
        return False
    for name in ("chat_history.jsonl", "summary.json"):
        fh = _open_regular(sdir / name)
        if fh is not None:
            fh.close()
            return True
    return False


def _iso_to_epoch(value) -> "float | None":
    if not isinstance(value, str):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    day, clock, frac, tz = m.groups()
    try:  # 3.11+: 'Z', '+HHMM' and a fraction of any length (truncated to microseconds) all parse
        dt = datetime.fromisoformat(f"{day}T{clock}.{frac or '0'}{tz or 'Z'}")
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).timestamp()


def _count(signals: "dict | None") -> "int | None":
    if not isinstance(signals, dict):
        return None
    u, a = signals.get("userMessageCount"), signals.get("assistantMessageCount")
    if isinstance(u, int) and isinstance(a, int) and not isinstance(u, bool) and not isinstance(a, bool):
        return u + a
    return None


def _signal_int(signals: "dict | None", key: str) -> "int | None":
    value = signals.get(key) if signals else None  # _read_json hands back a dict or None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def session_context(session_id: str, cwd: str, *, grok_home=None) -> dict:
    """``{context_tokens, context_window}`` of a session from its ``signals.json`` (the size of
    the LAST request's context and the model's window), None for whatever is not recorded - the
    two numbers ``api_project_session_history`` returns for a Codex thread. Raises like
    ``history_messages`` for a malformed id/cwd or an unsafe home."""
    sdir = _session_dir(session_id, cwd, _home(grok_home))
    signals = _read_json(sdir / "signals.json") if sdir is not None else None
    return {"context_tokens": _signal_int(signals, "contextTokensUsed"),
            "context_window": _signal_int(signals, "contextWindowTokens")}


def _first_query_preview(chat: Path) -> str:
    """Head of a session that has no title yet: its first operator message."""
    for _offset, row in _iter_jsonl(chat, max_bytes=HEAD_PREVIEW_BYTES, from_head=True):
        if row.get("type") == "user":
            text = user_query_text(row)
            if text:
                return " ".join(text.split())[:PREVIEW_CHARS]
    return ""


def _session_row(sdir: Path, sid: str, cwd: str) -> "dict | None":
    summary = _read_json(sdir / "summary.json")
    chat = sdir / "chat_history.jsonl"
    if summary is None:
        # Created but never summarised (or summary unreadable): list it only if it has a history.
        try:
            mtime = os.lstat(chat).st_mtime
        except OSError:
            return None
        summary = {}
    elif summary.get("session_kind") in SUBAGENT_KINDS:
        return None
    else:
        mtime = None
    title = ""
    for key in ("session_summary", "generated_title"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            title = " ".join(value.split())
            break
    updated = _iso_to_epoch(summary.get("updated_at")) or _iso_to_epoch(summary.get("last_active_at"))
    if updated is None:
        if mtime is None:
            try:
                mtime = os.lstat(sdir / "summary.json").st_mtime
            except OSError:
                mtime = 0.0
        updated = float(mtime)
    return {
        "id": sid, "provider": PROVIDER, "cwd": cwd,
        "name": title or None,
        "preview": title[:PREVIEW_CHARS],
        "updatedAt": updated, "recencyAt": updated,
        "createdAt": _iso_to_epoch(summary.get("created_at")),
        "model": summary.get("current_model_id") if isinstance(summary.get("current_model_id"), str) else None,
        "message_count": None,
    }


def _decorate(row: dict, sdir: Path) -> None:
    """The two fields that cost a second file read: the first-query preview of an untitled session
    (up to ``HEAD_PREVIEW_BYTES``) and the message count. Filled only for rows that survived the
    listing limit - a cockpit turn is often killed before Grok writes a title, so untitled
    sessions are the common case and every one of them would otherwise be opened."""
    if row["name"] is None:
        row["preview"] = _first_query_preview(sdir / "chat_history.jsonl")
    row["message_count"] = _count(_read_json(sdir / "signals.json"))


def _list(cwd: str, limit: int, home: Path, *, decorate: bool) -> "list[tuple[dict, Path]]":
    """``(row, session dir)`` pairs of ``cwd``, newest first - the one scan behind both
    ``list_sessions`` (``decorate=True``) and ``iter_search_docs`` (which needs no preview)."""
    _check_cwd(cwd)
    root, root_real = _sessions_root(home)
    found: dict[str, tuple[dict, Path]] = {}
    for group in _group_dirs(root, root_real, cwd):
        try:
            names = sorted((e.name for e in os.scandir(group) if valid_session_id(e.name)), reverse=True)
        except OSError:
            continue
        for sid in names[:MAX_SESSIONS_PER_CWD]:
            sdir = group / sid
            if sid in found or not _is_real_dir(sdir) or not _inside(sdir, root_real):
                continue
            row = _session_row(sdir, sid, cwd)
            if row is not None:
                found[sid] = (row, sdir)
    ranked = sorted(found.values(), key=lambda pr: pr[0]["updatedAt"], reverse=True)[:limit]
    if decorate:
        for row, sdir in ranked:
            _decorate(row, sdir)
    return ranked


def list_sessions(cwd: str, limit: int = 30, *, grok_home=None) -> list[dict]:
    """Sessions of ``cwd``, newest first, shaped like ``codex_engine.list_threads`` rows.

    Title / ``updatedAt`` come from each session's ``summary.json`` (the on-disk equivalent of
    ACP ``session/list``; no agent process is spawned). Sub-agent sessions are not chats and are
    left out. ``updatedAt`` is epoch SECONDS like Codex's. ``message_count`` is approximate
    (``signals.json`` counters) or None.
    """
    return [row for row, _sdir in _list(cwd, _clamp_limit(limit), _home(grok_home), decorate=True)]


def iter_search_docs(cwd: str, *, grok_home=None, max_sessions: int = SEARCH_MAX_SESSIONS,
                     max_chars: int = SEARCH_DOC_CHARS) -> Iterator[dict]:
    """One document per recent session of ``cwd``: ``{id, cwd, title, updatedAt, text}`` where
    ``text`` is the user/assistant text (no tool output), newest content kept when capped. One
    call reads at most ``SEARCH_TOTAL_BYTES`` in all (newest sessions first), so a query cannot
    stall the thread that serves it."""
    budget = SEARCH_TOTAL_BYTES
    for row, sdir in _list(cwd, _clamp_limit(max_sessions), _home(grok_home), decorate=False):
        if budget <= 0:
            break
        chat = sdir / "chat_history.jsonl"
        try:
            budget -= min(os.lstat(chat).st_size, SEARCH_READ_BYTES)
        except OSError:
            pass
        msgs = _messages(chat, row["id"], MAX_LIMIT, default_format_tool, SEARCH_READ_BYTES)
        text = "\n".join(m["text"] for m in msgs if m["text"].strip())
        yield {"id": row["id"], "cwd": cwd, "title": row["name"] or "",
               "updatedAt": row["updatedAt"], "text": text[-max_chars:]}


def _snippet(title: str, text: str, terms: list[str]) -> str:
    """Around the first term found in the conversation text, else the title."""
    flat = " ".join(text.split())
    low = flat.casefold()
    hits = [p for p in (low.find(t) for t in terms) if p >= 0]
    if not hits:
        return title[:SNIPPET_CHARS]
    start = max(0, min(hits) - SNIPPET_CHARS // 4)
    return flat[start:start + SNIPPET_CHARS]


def search_sessions(query: str, cwd: str, *, limit: int = 30, grok_home=None) -> list[dict]:
    """Sessions of ``cwd`` whose title or text contain EVERY whitespace-separated term of
    ``query`` (case-insensitive), as ``list_threads``-shaped rows with ``preview`` = the snippet
    around the first hit - what ``api_search`` builds its Codex hits from."""
    terms = [t for t in str(query or "").casefold().split() if t][:8]
    if not terms:
        return []
    out: list[dict] = []
    for doc in iter_search_docs(cwd, grok_home=grok_home):
        hay = f"{doc['title']}\n{doc['text']}".casefold()
        if all(t in hay for t in terms):
            out.append({"id": doc["id"], "provider": PROVIDER, "cwd": cwd,
                        "name": doc["title"] or None,
                        "preview": _snippet(doc["title"], doc["text"], terms),
                        "updatedAt": doc["updatedAt"], "recencyAt": doc["updatedAt"]})
            if len(out) >= _clamp_limit(limit):
                break
    return out

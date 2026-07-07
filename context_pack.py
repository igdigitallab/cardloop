"""context_pack.py — spec-075 Phase A.

Assembles a deterministic, bounded "context pack" for the first turn of a
fresh SDK session, so the agent is not blank after /reset or /rotate.

Public API:
    assemble(cwd, session_key, query, *, data_dir, project_id=None,
             char_budget=CONTEXT_PACK_CHAR_BUDGET) -> str | None

Never raises: every source read is individually guarded; a failing source is
simply omitted. Returns None when there is nothing substantive to inject.

Design ref: docs/internal/specs/spec-075-context-pack.md §3-4
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Public constants
# ─────────────────────────────────────────────────────────────────────────────

CONTEXT_PACK_CHAR_BUDGET = 6000  # hard char cap (≈1500 tokens, <2% of 200K window)

# Per-section soft sub-caps (spec-075 §3). These keep any ONE section — notably a
# large memory file — from eating the whole budget and crowding out the freshest
# "what are we doing now" signal (board / recent activity / commits / recall). The
# curated memory INDEX is always kept (it points the agent at the full file to read).
_TRUNC_MARKER = " … [truncated]"
_SECTION_CAP = {
    "memory_index": 1800,     # curated one-liners; usually well under this
    "relevant_memory": 1500,  # total across files (per-file capped below)
    "board": 1200,
    "activity": 900,
    "commits": 500,
    "recall": 1100,
}
_RELEVANT_MEMORY_PER_FILE_CAP = 1000

# ─────────────────────────────────────────────────────────────────────────────
# Slug rule — must match webapp._timeline_slug_from_cwd exactly.
# webapp.py ~1179: return cwd.replace("/", "-")
# ─────────────────────────────────────────────────────────────────────────────

def _timeline_slug_from_cwd(cwd: str) -> str:
    """Stable slug from cwd: '/' → '-'. Mirrors webapp._timeline_slug_from_cwd."""
    return cwd.replace("/", "-")


# ─────────────────────────────────────────────────────────────────────────────
# Source readers — each returns "" / [] / {} on any error (fail-open)
# ─────────────────────────────────────────────────────────────────────────────

def _read_memory(cwd: str) -> tuple[str, list[dict[str, str]]]:
    """Read *.md from <cwd>/.claude-ops/memory/.

    Returns (index_text, other_files) where:
      index_text — full content of MEMORY.md (or "" if absent)
      other_files — list of {name, body} for every other *.md
    """
    mem_dir = Path(cwd) / ".claude-ops" / "memory"
    index_text = ""
    others: list[dict[str, str]] = []
    try:
        if not mem_dir.is_dir():
            return "", []
        for fpath in sorted(mem_dir.glob("*.md")):
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if fpath.name == "MEMORY.md":
                index_text = text
            else:
                others.append({"name": fpath.name, "body": text})
    except Exception:
        pass
    return index_text, others


def _relevant_memory(files: list[dict[str, str]], query: str, k: int = 3) -> list[dict[str, str]]:
    """Rank memory files by keyword overlap with query (title + body).

    Ties broken by descending body size. Returns up to k files.
    Empty query → returns files sorted by size desc (largest first).
    """
    if not files:
        return []

    q_words = set(re.findall(r"\w+", query.lower())) if query else set()

    def score(f: dict[str, str]) -> tuple[int, int]:
        if not q_words:
            return (0, len(f.get("body", "")))
        combined = (f.get("name", "") + " " + f.get("body", "")).lower()
        f_words = set(re.findall(r"\w+", combined))
        overlap = len(q_words & f_words)
        return (overlap, len(f.get("body", "")))

    ranked = sorted(files, key=score, reverse=True)
    return ranked[:k]


def _read_timeline(data_dir: str, cwd: str, session_key: str, limit: int = 8) -> list[dict[str, Any]]:
    """Read last `limit` timeline events from data/timeline/<slug>.jsonl.

    Returns [] on any error or absent file. Malformed lines are skipped.
    """
    try:
        slug = _timeline_slug_from_cwd(cwd)
        path = Path(data_dir) / "timeline" / f"{slug}.jsonl"
        if not path.exists():
            return []
        lines: list[str] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if raw:
                        lines.append(raw)
        except Exception:
            return []
        # Take last `limit` lines
        tail = lines[-limit:]
        events: list[dict[str, Any]] = []
        for line in tail:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    events.append(obj)
            except Exception:
                continue  # skip malformed lines
        return events
    except Exception:
        return []


def _git_log(cwd: str, n: int = 5) -> str:
    """Run git log --oneline -n inside cwd. Returns "" on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "log", "--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _recall(data_dir: str, query: str, project_id: str | None, k: int = 3) -> list[dict[str, Any]]:
    """FTS5 recall via search.search_at. Returns [] on any error or absent db."""
    if not query or not query.strip():
        return []
    try:
        from search import search_at, db_path_for  # local import — no circular dep
        db = db_path_for(Path(data_dir))
        hits = search_at(db, query, limit=k, project_id=project_id)
        return hits if isinstance(hits, list) else []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Highlight-marker stripping (search.py uses \x01 / \x02)
# ─────────────────────────────────────────────────────────────────────────────

def _strip_highlight(text: str) -> str:
    return text.replace("\x01", "").replace("\x02", "")


def _cap(text: str, limit: int) -> str:
    """Truncate text to `limit` chars on a char boundary, adding a marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNC_MARKER))
    return text[:keep].rstrip() + _TRUNC_MARKER


# ─────────────────────────────────────────────────────────────────────────────
# Section formatters
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_timeline_event(ev: dict[str, Any]) -> str:
    """Render one timeline event as a compact one-liner."""
    parts: list[str] = []
    kind = ev.get("type") or ev.get("kind") or "event"
    parts.append(kind)
    # Include short text if present
    text = ev.get("text") or ev.get("tool") or ""
    if text and isinstance(text, str):
        snippet = text[:80].replace("\n", " ")
        parts.append(snippet)
    return " | ".join(parts)


def _fmt_recall_hit(hit: dict[str, Any]) -> str:
    """Render one FTS recall hit as a compact entry."""
    source = hit.get("source", "")
    snippet = _strip_highlight(str(hit.get("snippet", "")))[:200].replace("\n", " ")
    ref = hit.get("ref")
    ref_str = ""
    if isinstance(ref, dict):
        ref_str = ref.get("file", "") or ref.get("session_key", "") or ""
    elif isinstance(ref, str):
        ref_str = ref
    parts = [f"[{source}]"]
    if ref_str:
        parts.append(ref_str)
    parts.append(snippet)
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Budget enforcement
# ─────────────────────────────────────────────────────────────────────────────

_HEADER = (
    "This project state was assembled at session start (you begin fresh here). "
    "Ground your reply in it; open the referenced memory files or board cards for detail."
)

# Priority order for DROP (lowest priority dropped first when over budget):
# 7=recall, 6=commits, 5=activity, 4=board, 3=relevant_memory, 2=memory_index, 1=header
# Spec §3: "drop from the LOWEST priority up (recall → commits → activity → board →
#           relevant memory), never the memory index or header"
_SECTION_PRIORITY = {
    "header": 1,
    "memory_index": 2,
    "relevant_memory": 3,
    "board": 4,
    "activity": 5,
    "commits": 6,
    "recall": 7,
}
_DROP_ORDER = sorted(_SECTION_PRIORITY.keys(), key=lambda k: _SECTION_PRIORITY[k], reverse=True)


def _format(
    *,
    memory_index: str,
    relevant_memory_files: list[dict[str, str]],
    board_text: str,
    timeline_events: list[dict[str, Any]],
    git_log_text: str,
    recall_hits: list[dict[str, Any]],
    char_budget: int,
) -> str | None:
    """Assemble sections, apply budget, wrap in <context-pack> tag.

    Returns None if only the header would remain (no substantive content).
    """

    # ── Build raw section strings ──────────────────────────────────────────
    sections: dict[str, str] = {}

    sections["header"] = _HEADER

    if memory_index.strip():
        sections["memory_index"] = _cap(
            f"## Memory index\n{memory_index.rstrip()}", _SECTION_CAP["memory_index"])

    if relevant_memory_files:
        parts_rm: list[str] = []
        used = 0
        total_cap = _SECTION_CAP["relevant_memory"]
        for f in relevant_memory_files:
            if used >= total_cap:
                break
            name = f.get("name", "")
            body = f.get("body", "").rstrip()
            if not body:
                continue
            per_file = min(_RELEVANT_MEMORY_PER_FILE_CAP, total_cap - used)
            entry = _cap(f"### {name}\n{body}", per_file)
            parts_rm.append(entry)
            used += len(entry)
        if parts_rm:
            sections["relevant_memory"] = "## Relevant memory files\n" + "\n\n".join(parts_rm)

    if board_text.strip():
        sections["board"] = _cap(
            f"## Board (open cards)\n{board_text.rstrip()}", _SECTION_CAP["board"])

    if timeline_events:
        lines = [_fmt_timeline_event(ev) for ev in timeline_events if ev]
        if lines:
            sections["activity"] = _cap(
                "## Recent activity\n" + "\n".join(f"- {l}" for l in lines),
                _SECTION_CAP["activity"])

    if git_log_text.strip():
        sections["commits"] = _cap(
            f"## Recent commits\n{git_log_text.rstrip()}", _SECTION_CAP["commits"])

    if recall_hits:
        lines_r = [_fmt_recall_hit(h) for h in recall_hits if h]
        if lines_r:
            sections["recall"] = _cap(
                "## Relevant recall\n" + "\n".join(lines_r), _SECTION_CAP["recall"])

    # ── Substantive content check ─────────────────────────────────────────
    # If nothing beyond header, return None
    substantive = {k for k in sections if k != "header"}
    if not substantive:
        return None

    # ── Budget enforcement: drop lowest-priority sections until within budget ─
    # Build body in priority order (lowest number = highest priority)
    def _assemble(secs: dict[str, str]) -> str:
        ordered = sorted(secs.keys(), key=lambda k: _SECTION_PRIORITY.get(k, 99))
        body = "\n\n".join(secs[k] for k in ordered)
        return f"<context-pack>\n{body}\n</context-pack>"

    full = _assemble(sections)
    if len(full) <= char_budget:
        return full

    # Drop sections from lowest priority (highest number) until fits.
    # Spec: "each truncated section ends with … [truncated]"
    working = dict(sections)
    for key in _DROP_ORDER:
        if key in ("header", "memory_index"):
            # Never drop these
            continue
        if key not in working:
            continue
        # Try truncating this section first (add [truncated] marker)
        original = working[key]
        # Build tentative pack without this section entirely, check if it fits
        without = {k: v for k, v in working.items() if k != key}
        candidate = _assemble(without)
        if len(candidate) <= char_budget:
            # Dropping the section fits — but first try truncating it to use budget
            # Calculate how much room this section can have
            budget_remaining = char_budget - len(candidate)
            if budget_remaining > 60:  # only worth including if there's meaningful space
                # Try to fit a truncated version
                marker = " … [truncated]"
                max_section_chars = budget_remaining - 20  # leave room for wrapping overhead
                if len(original) > max_section_chars:
                    truncated_section = original[:max_section_chars] + marker
                    working[key] = truncated_section
                    candidate_trunc = _assemble(working)
                    if len(candidate_trunc) <= char_budget:
                        return candidate_trunc
            # Drop section entirely
            working = without
            candidate = _assemble(working)
            if len(candidate) <= char_budget:
                return candidate
        else:
            # Even without this section we're over budget — keep dropping
            working = without

    # After dropping all droppable sections, if still over budget, truncate memory_index
    result = _assemble(working)
    if len(result) > char_budget:
        # Truncate the memory_index to fit
        if "memory_index" in working:
            marker = " … [truncated]"
            overhead = len(_assemble({k: v for k, v in working.items() if k != "memory_index"}))
            room = char_budget - overhead - len(marker) - 30
            if room > 20 and len(working["memory_index"]) > room:
                working["memory_index"] = working["memory_index"][:room] + marker
        result = _assemble(working)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def assemble(
    cwd: str,
    session_key: str,
    query: str,
    *,
    data_dir: str,
    project_id: str | None = None,
    char_budget: int = CONTEXT_PACK_CHAR_BUDGET,
) -> str | None:
    """Assemble and return the <context-pack>…</context-pack> string, or None if empty.

    Never raises: every source read is individually guarded; a failing source
    is simply omitted.

    Args:
        cwd: project working directory (absolute path)
        session_key: e.g. "1001:42" — used for timeline slug fallback
        query: the user's first message (used for FTS recall + memory ranking)
        data_dir: path to the Cardloop data directory (for timeline, search.db)
        project_id: project id for scoping FTS recall (optional)
        char_budget: hard char cap (default CONTEXT_PACK_CHAR_BUDGET=6000)
    """
    try:
        memory_index, other_files = _read_memory(cwd)
    except Exception:
        memory_index, other_files = "", []

    try:
        relevant_files = _relevant_memory(other_files, query)
    except Exception:
        relevant_files = []

    try:
        from board import board_summary  # local import — no circular dep
        board_text = board_summary(cwd)
    except Exception:
        board_text = ""

    try:
        events = _read_timeline(data_dir, cwd, session_key)
    except Exception:
        events = []

    try:
        git_text = _git_log(cwd)
    except Exception:
        git_text = ""

    try:
        hits = _recall(data_dir, query, project_id)
    except Exception:
        hits = []

    return _format(
        memory_index=memory_index,
        relevant_memory_files=relevant_files,
        board_text=board_text,
        timeline_events=events,
        git_log_text=git_text,
        recall_hits=hits,
        char_budget=char_budget,
    )

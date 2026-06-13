"""
board.py — shared kanban board primitives for Claude-Ops-Bot.

Extracted from webapp.py (spec-034 L0) so that both webapp.py and bot.py
share one source of truth for reading and writing TASKS.md / DONE.md.

All names are re-exported from webapp.py for backward compatibility.
"""

# spec-034: https://github.com/igor/claude-ops-bot/specs/spec-034-board-centric-os.md

import asyncio
import re
import secrets
from pathlib import Path

# ─────────────────────────── board columns ───────────────────────────
#
# Spec=Kanban=2 files. TASKS.md (sections = columns) — the only file sessions read.
# DONE.md (archive) — append-only, agents do NOT read it (context hygiene).
# Source of truth = markdown in the project repo; no DB for the plan.

BOARD_COLUMNS = [
    ("backlog",     "Backlog",     " "),
    ("in_progress", "In Progress", "~"),
    ("review",      "Review",      "?"),
    ("failed",      "Failed",      "!"),
]
_LABEL_TO_COL = {lbl.lower(): key for key, lbl, _ in BOARD_COLUMNS}

# One lock per cwd — serialises all cockpit writes to the board (GET canonicalise + mutations).
# The agent writes the file directly and does not participate in the lock, so the lock only
# protects the cockpit<->cockpit race.
_board_locks: dict[str, asyncio.Lock] = {}


def _get_board_lock(cwd: str) -> asyncio.Lock:
    if cwd not in _board_locks:
        _board_locks[cwd] = asyncio.Lock()
    return _board_locks[cwd]


_CARD_RE = re.compile(r"^\s*[-*]\s*\[(.)\]\s*(.*)$")
# Lines like "- text" without a checkbox — agents often write this way.
# Inside a column section we treat these as Backlog cards (default status).
_PLAIN_CARD_RE = re.compile(r"^\s*[-*]\s+(?!\[)(.+)$")
# Single marker: <!--ops:ID--> — ID can be any word (including non-hex aliases).
_MARKER_RE = re.compile(r"\s*<!--\s*ops:([\w-]+)\s*-->")
# Description lines: '  > text' (2 spaces + '>') immediately following a card
_DESC_LINE_RE = re.compile(r"^  > (.*)$")


def _extract_id_and_text(rest: str) -> tuple[str, str]:
    """Extract ID and strip ALL ops markers from text. First marker = canonical ID."""
    matches = list(_MARKER_RE.finditer(rest))
    if not matches:
        return _new_card_id(), rest.strip()
    cid = matches[0].group(1)
    clean = _MARKER_RE.sub("", rest).strip()
    return cid, clean


def _tasks_path(cwd: str) -> Path:
    return Path(cwd) / "TASKS.md"


def _done_path(cwd: str) -> Path:
    return Path(cwd) / "DONE.md"


def _new_card_id() -> str:
    return secrets.token_hex(3)


# Regular card = hex(+dash) OR alphanumeric slug like jan-9e2d; incident = 'err-<hash6>'.
# The err- prefix is explicitly allowed. No dots/slashes -> traversal impossible.
# Extended to [a-z0-9-] so user-defined IDs like "jan-9e2d" pass validation.
_CARD_ID_RE = re.compile(r"^(err-)?[a-z0-9-]{4,20}$")


def _valid_card_id(card_id: str) -> bool:
    """True if card_id matches the expected format (hex+dash, 4-20 chars)."""
    return bool(_CARD_ID_RE.fullmatch(card_id))


def _count_potential_cards(raw: str) -> int:
    """How many lines in raw COULD be cards (any format).
    Used as a guard: if after parse+serialize the card count dropped —
    the parser didn't recognise some format and a write would destroy data.
    Counts lines like '- ...' or '* ...' INSIDE a ## section (not preamble)."""
    count = 0
    in_section = False
    for line in raw.splitlines():
        h = line.strip()
        if h.startswith("##"):
            in_section = True
            continue
        if not in_section:
            continue
        s = h
        if s.startswith(("- ", "* ")) and len(s) > 2:
            count += 1
    return count


def _parse_tasks(text: str):
    """(preamble, cols) — preamble = everything before the first recognised '## <Column>'.
    Cards with checkbox '- [ ] text' — parsed into the matching column.
    Cards without checkbox '- text' — parsed as Backlog (agents sometimes write this way).
    Description lines '  > text' immediately after a card — collected into card['description'].
    Non-card lines inside sections are discarded on re-serialisation."""
    cols = {key: [] for key, _, _ in BOARD_COLUMNS}
    preamble_lines: list[str] = []
    cur = None
    seen_header = False
    last_card: dict | None = None  # last added card — description receiver
    for line in text.splitlines():
        h = line.strip()
        if h.startswith("##"):
            name = h.lstrip("#").strip().lower()
            cur = _LABEL_TO_COL.get(name)  # None for unknown sections
            last_card = None  # new section resets receiver
            if cur is not None:
                seen_header = True
            elif not seen_header:
                preamble_lines.append(line)
            continue
        # Description line — '  > text', immediately after a card
        if cur is not None and last_card is not None:
            dm = _DESC_LINE_RE.match(line)
            if dm:
                desc_line = dm.group(1)
                if last_card.get("description") is None:
                    last_card["description"] = desc_line
                else:
                    last_card["description"] += "\n" + desc_line
                continue
            # Any other line — end of description block
            last_card = None
        m = _CARD_RE.match(line)
        if m and cur is not None:
            cid, cardtext = _extract_id_and_text(m.group(2))
            if cardtext:
                card: dict = {"id": cid, "text": cardtext}
                cols[cur].append(card)
                last_card = card
        elif cur is not None:
            # No checkbox match — try plain '- text' (agent style)
            pm = _PLAIN_CARD_RE.match(line)
            if pm:
                cid, cardtext = _extract_id_and_text(pm.group(1))
                if cardtext:
                    # Plain cards always go to the current column (agent chose the section)
                    card = {"id": cid, "text": cardtext}
                    cols[cur].append(card)
                    last_card = card
        elif not seen_header:
            preamble_lines.append(line)
    return "\n".join(preamble_lines).rstrip(), cols


def _serialize_tasks(preamble: str, cols: dict, project_name: str) -> str:
    if not preamble.strip():
        preamble = f"# Tasks — {project_name}"
    out = [preamble, ""]
    for key, label, status in BOARD_COLUMNS:
        out.append(f"## {label}")
        for card in cols[key]:
            out.append(f"- [{status}] {card['text']} <!--ops:{card['id']}-->")
            desc = card.get("description")
            if desc:
                for desc_line in desc.splitlines():
                    out.append(f"  > {desc_line}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _load_board(cwd: str):
    tp = _tasks_path(cwd)
    raw = tp.read_text(encoding="utf-8") if tp.exists() else ""
    preamble, cols = _parse_tasks(raw)
    return raw, preamble, cols


def _save_board(cwd: str, name: str, preamble: str, cols: dict) -> None:
    _tasks_path(cwd).write_text(_serialize_tasks(preamble, cols, name), encoding="utf-8")


def _pop_card(cols: dict, card_id: str):
    for k in cols:
        for i, c in enumerate(cols[k]):
            if c["id"] == card_id:
                return cols[k].pop(i)
    return None


def _board_payload(cwd: str) -> dict:
    tp, dp = _tasks_path(cwd), _done_path(cwd)
    _, _, cols = _load_board(cwd)
    columns = [{"key": k, "label": l, "cards": cols[k]} for k, l, _ in BOARD_COLUMNS]
    done_count = 0
    if dp.exists():
        done_count = sum(1 for ln in dp.read_text(encoding="utf-8", errors="replace").splitlines()
                         if _CARD_RE.match(ln))
    return {"columns": columns, "done_count": done_count, "exists": tp.exists()}


# ─────────────────────────── board_summary ───────────────────────────

# Open columns (not failed, not done — those are handled separately)
_OPEN_COLUMNS = {"backlog", "in_progress", "review"}

# Caps for board_summary to stay token-cheap
_SUMMARY_MAX_CARDS = 40
_SUMMARY_MAX_CHARS = 4000

# Column display names for board_summary output
_COLUMN_LABEL = {key: label for key, label, _ in BOARD_COLUMNS}


def board_summary(cwd: str) -> str:
    """Return a compact, token-cheap rendering of open cards (backlog/in_progress/review).

    Each line: '- [<id>] <text>', grouped by column header.
    Capped at ~40 cards / ~4000 chars. Returns '' if TASKS.md does not exist.
    Returns 'Board is empty.' when TASKS.md exists but has no open cards.
    """
    tp = _tasks_path(cwd)
    if not tp.exists():
        return ""

    _, _, cols = _load_board(cwd)

    lines: list[str] = []
    card_count = 0
    char_count = 0
    truncated = False

    for col_key in ("backlog", "in_progress", "review"):
        cards = cols.get(col_key, [])
        if not cards:
            continue
        header = f"### {_COLUMN_LABEL[col_key]}"
        lines.append(header)
        char_count += len(header) + 1
        for card in cards:
            if card_count >= _SUMMARY_MAX_CARDS or char_count >= _SUMMARY_MAX_CHARS:
                truncated = True
                break
            line = f"- [{card['id']}] {card['text']}"
            lines.append(line)
            char_count += len(line) + 1
            card_count += 1
        if truncated:
            break

    if not lines:
        return "Board is empty."

    if truncated:
        lines.append(f"… (truncated at {_SUMMARY_MAX_CARDS} cards)")

    return "\n".join(lines)

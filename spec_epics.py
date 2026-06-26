"""Epic-lens: aggregate docs/internal/specs/*.md as epics with their linked board cards.

Read-only. Powers the cockpit Specs tab (spec-049 Workstream B / spec-059 Move 1):
each spec file is an epic, every card carrying a `spec=<id>` ops-marker is one of its
subtasks, and progress = done / total. Pure functions over board.py + spec_mirror
helpers — never writes anything.

The `spec=` marker value is the BARE id ("060", not "spec-060"); `_norm_spec_id`
tolerates a stray "spec-" prefix so a mistagged card still links.
"""
from __future__ import annotations

import re
from pathlib import Path

from board import _load_board, _COLUMN_LABEL, _OPEN_COLUMNS
from spec_mirror import _default_specs_dir, _done_cards_for_spec

_YAML_STATUS_RE = re.compile(r"^status:\s*(.+)$", re.MULTILINE)
_BOLD_STATUS_RE = re.compile(r"^\*\*(?:Status|Статус):\*\*\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _norm_spec_id(s: str | None) -> str:
    """Canonical bare id. Tolerate a stray 'spec-' prefix (e.g. 'spec-060' -> '060')."""
    s = (s or "").strip()
    return s[5:] if s.startswith("spec-") else s


def _spec_id_from_name(name: str) -> str:
    """'spec-060-multi-provider.md' -> '060'. The id is the first '-'-delimited segment
    after the 'spec-' prefix (matches the board's `spec=<id>` convention)."""
    m = re.match(r"^spec-([^-.]+)", name)
    return m.group(1) if m else name[:-3] if name.endswith(".md") else name


def _parse_spec_meta(path: Path) -> dict:
    """Extract {title, status} from a spec file. Handles YAML frontmatter (`status:`),
    inline bold (`**Status:**` / `**Статус:**`), or neither. Title = first '# ' heading."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"title": path.stem, "status": ""}

    title_m = _HEADING_RE.search(text)
    title = title_m.group(1).strip() if title_m else path.stem

    status = ""
    if text.startswith("---"):  # YAML frontmatter wins
        end = text.find("\n---", 3)
        if end != -1:
            ym = _YAML_STATUS_RE.search(text[3:end])
            if ym:
                status = ym.group(1).strip()
    if not status:
        bm = _BOLD_STATUS_RE.search(text)
        if bm:
            status = bm.group(1).strip()

    return {"title": title[:200], "status": status[:200]}


def build_epic_specs(cwd: str) -> list[dict]:
    """All spec files under <cwd>/docs/internal/specs/, each annotated with its linked
    cards (open by column + done) and progress. Newest first. Empty list if no specs dir.

    Shape per spec:
        {spec_id, title, status, name, cards:{open:[{id,text,column}], done:[{id,text}]},
         done_count, total, progress}
    """
    specs_dir = _default_specs_dir(cwd)
    if not specs_dir.is_dir():
        return []

    # One board read; bucket OPEN cards by normalized spec id.
    _, _, cols = _load_board(cwd)
    open_by_spec: dict[str, list[dict]] = {}
    for col_key in _OPEN_COLUMNS:  # backlog / in_progress / review
        label = _COLUMN_LABEL.get(col_key, col_key)
        for card in cols.get(col_key, []):
            sid = _norm_spec_id(card.get("spec"))
            if not sid:
                continue
            open_by_spec.setdefault(sid, []).append(
                {"id": card["id"], "text": card["text"], "column": label})

    out: list[dict] = []
    for path in specs_dir.glob("spec-*.md"):
        sid = _spec_id_from_name(path.name)
        meta = _parse_spec_meta(path)
        open_cards = open_by_spec.get(sid, [])
        done = _done_cards_for_spec(cwd, sid)  # {id: title}
        done_cards = [{"id": cid, "text": txt} for cid, txt in done.items()]
        total = len(open_cards) + len(done_cards)
        out.append({
            "spec_id": sid,
            "title": meta["title"],
            "status": meta["status"],
            "name": path.name,
            "cards": {"open": open_cards, "done": done_cards},
            "done_count": len(done_cards),
            "total": total,
            "progress": (len(done_cards) / total) if total else 0.0,
        })

    # Newest first: numeric spec ids descending, non-numeric after, by name.
    def _key(s: dict):
        sid = s["spec_id"]
        return (0, -int(sid)) if sid.isdigit() else (1, 0)
    out.sort(key=lambda s: (_key(s), s["name"]))
    return out


def get_epic_spec_content(cwd: str, name: str) -> str | None:
    """Markdown of one spec file under <cwd>/docs/internal/specs/. Basename + .md guard
    against path traversal; None if missing/disallowed."""
    name = Path(name).name
    if not name.endswith(".md"):
        return None
    specs_dir = _default_specs_dir(cwd)
    candidate = (specs_dir / name).resolve()
    try:
        if not str(candidate).startswith(str(specs_dir.resolve())):
            return None
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return None

#!/usr/bin/env python3
"""Wire an existing project into the mattpocock/skills engineering flow, Cardloop-style.

New projects get this automatically at creation (software/ops archetypes). This tool retrofits an
EXISTING project on demand — we deliberately do NOT mass-migrate every project (most old/test/
personal projects never use /to-tickets, /triage, /implement, and some live in separate OPSEC
zones). Run it per project when you actually want the engineering skills there.

It is idempotent and additive:
  - writes board-mapped docs/agents/{issue-tracker,domain,triage-labels}.md if missing
  - appends a `## Agent skills` block to the project's CLAUDE.md if the file exists and lacks it

It never overwrites existing files and never touches git.

Usage:
    python tools/wire-agent-skills.py <project-dir> [--dry-run]
    python tools/wire-agent-skills.py ~/projects/orator
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEMPLATES = HERE / "templates" / "agents"
AGENT_FILES = ("issue-tracker.md", "domain.md", "triage-labels.md")

CLAUDE_BLOCK = """
## Agent skills

Engineering skills from `mattpocock/skills` (installed globally in `~/.claude/skills/`) read the
files under `docs/agents/` to fit this project's workflow — keep those files current if the workflow changes.

### Issue tracker

Issues are **Cardloop board cards** (`TASKS.md`), not GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The five triage roles are a board vocabulary, not GitHub labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: goal/rules in `CLAUDE.md`, decisions in `docs/specs/`. See `docs/agents/domain.md`.
"""


def wire(project_dir: Path, dry_run: bool = False) -> list[str]:
    if not project_dir.is_dir():
        raise SystemExit(f"not a directory: {project_dir}")
    actions: list[str] = []

    agents_dir = project_dir / "docs" / "agents"
    for fn in AGENT_FILES:
        dest = agents_dir / fn
        if dest.exists():
            actions.append(f"skip (exists): docs/agents/{fn}")
            continue
        src = TEMPLATES / fn
        if not src.is_file():
            raise SystemExit(f"template missing: {src}")
        actions.append(f"write: docs/agents/{fn}")
        if not dry_run:
            agents_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    claude = project_dir / "CLAUDE.md"
    if not claude.exists():
        actions.append("skip: no CLAUDE.md (block not added)")
    elif "## Agent skills" in claude.read_text(encoding="utf-8"):
        actions.append("skip (block present): CLAUDE.md")
    else:
        actions.append("append ## Agent skills block: CLAUDE.md")
        if not dry_run:
            with claude.open("a", encoding="utf-8") as fh:
                fh.write("\n" + CLAUDE_BLOCK.lstrip("\n"))

    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_dir", type=Path, help="path to the project to wire")
    ap.add_argument("--dry-run", action="store_true", help="print actions without writing")
    args = ap.parse_args()

    actions = wire(args.project_dir.expanduser(), dry_run=args.dry_run)
    tag = "[dry-run] " if args.dry_run else ""
    for a in actions:
        print(f"{tag}{a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

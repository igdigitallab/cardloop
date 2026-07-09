#!/usr/bin/env python3
"""memory-lint — the missing "lint" operation for an LLM-maintained memory wiki.

Cardloop (and the SDK's native auto-memory) only ever INGEST memory: new facts are
appended, never pruned. Over weeks the wiki rots — stale gotchas, duplicate notes,
orphaned files, dead index links, oversized entries — and every one of them is paid
for on each session bootstrap. This is the Karpathy "LLM wiki" method's third
operation (ingest / query / **lint**), which we never had.

This tool READS a memory directory and reports what a human should prune. It never
deletes or rewrites anything — curation stays with the operator (the whole point of
the method: the LLM does bookkeeping, the human keeps authority).

Usage:
    memory-lint.py [--dir DIR] [--index MEMORY.md] [--repo REPO_ROOT]
                   [--max-bytes N] [--stale-days N] [--dup 0.0-1.0] [--json]

    --dir         memory directory to lint (default: ./.claude-ops/memory)
    --index       the index filename inside DIR (default: MEMORY.md)
    --repo        if given, check file-path references in bodies against this root
                  and flag ones that no longer exist (heuristic; off by default)
    --max-bytes   flag a memory body larger than this (default: 6000)
    --stale-days  flag a memory not modified in this many days (default: 90)
    --dup         title-similarity threshold to flag near-duplicates (default: 0.6)
    --json        emit machine-readable JSON instead of the markdown report

Exit code is always 0 (a report, not a gate). Grep the report or parse --json to act.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path


# ── index parsing ────────────────────────────────────────────────────────────

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")   # [title](file.md)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")          # [[slug]]


def _index_links(index_path: Path) -> tuple[set[str], list[str]]:
    """Links from the index, split into (local_basenames, broken_external_targets).

    A markdown link may point INSIDE the memory dir (`file.md`) or outside it
    (`../../../repo/docs/START-HERE.md`). Comparing everything by basename against the
    memory dir's own files — the old behaviour — reported every external link as dead.
    Resolve external targets against the memory dir instead and report only the ones that
    genuinely do not exist. (`iggo-llc` had three that were one `../` short of real files.)
    """
    if not index_path.exists():
        return set(), []
    text = index_path.read_text(encoding="utf-8", errors="replace")
    local: set[str] = set()
    broken_external: list[str] = []
    base = index_path.parent
    for m in _LINK_RE.finditer(text):
        target = m.group(1)
        if "/" in target:
            if not (base / target).resolve().exists():
                broken_external.append(target)
        else:
            local.add(os.path.basename(target))
    for m in _WIKILINK_RE.finditer(text):
        slug = m.group(1).strip()
        local.add(slug if slug.endswith(".md") else f"{slug}.md")
    return local, sorted(set(broken_external))


# ── per-file signals ─────────────────────────────────────────────────────────

_TITLE_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "was",
    "with", "by", "from", "at", "as", "it", "be", "this", "that", "fix", "bug",
}
# Only source-code files under the repo — narrow on purpose. Broad matching (json/md/
# vault paths / ~/.claude paths / prose "A.md/B.md" joins) produced mostly false positives.
_PATH_RE = re.compile(r"\b([\w][\w/-]*\.(?:py|tsx?|jsx?|sh))\b")
# Leading segments that live OUTSIDE the repo — never flag a "missing" ref rooted here.
_OUT_OF_REPO = ("claude/", "vault/", "tmp/", "data/", "dataset/", "dist/",
                "node_modules/", "~", ".claude", "home/")


def _title_words(name: str, body: str) -> set[str]:
    """Significant words from a memory's title (frontmatter name / first heading / filename)."""
    title = ""
    m = re.search(r"^name:\s*(.+)$", body, re.MULTILINE)
    if m:
        title = m.group(1)
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if m:
            title = m.group(1)
    if not title:
        title = name.replace("-", " ").replace(".md", "")
    words = set(re.findall(r"\w+", title.lower()))
    return {w for w in words if len(w) > 2 and w not in _TITLE_STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "dist", "__pycache__",
              ".worktrees", ".mypy_cache", ".pytest_cache"}


def _repo_basenames(repo: Path) -> set[str]:
    """All source-file basenames under repo, skipping heavy/vendored dirs. Built once."""
    names: set[str] = set()
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            names.add(f)
    return names


def _stale_refs(body: str, repo: Path, repo_index: set[str]) -> list[str]:
    """Source-file tokens in the body whose basename is not found under repo (heuristic)."""
    missing: list[str] = []
    seen: set[str] = set()
    for m in _PATH_RE.finditer(body):
        ref = m.group(1)
        if ref in seen or "/" not in ref:  # require a path separator to reduce noise
            continue
        seen.add(ref)
        if ref.startswith(_OUT_OF_REPO):   # lives outside the repo — not our concern
            continue
        if os.path.basename(ref) in repo_index:  # basename exists somewhere → treat as live
            continue
        missing.append(ref)
    return missing


# ── main lint pass ───────────────────────────────────────────────────────────

# The bundled CLI loads MEMORY.md verbatim on every bootstrap and hard-truncates it:
# `lines after ${VK} will be truncated` with VK=200, plus a 25000-byte ceiling
# (`wasLineTruncated` / `wasByteTruncated` in the binary). Past either limit, index entries
# vanish from the model's context with NO signal to the operator. Warn well before that.
CLI_INDEX_MAX_LINES = 200
CLI_INDEX_MAX_BYTES = 25_000
_BUDGET_WARN_AT = 0.80


def _index_budget(index_path: Path) -> dict:
    """How close the index is to the CLI's silent truncation ceiling."""
    if not index_path.exists():
        return {}
    raw = index_path.read_bytes()
    lines = raw.decode("utf-8", errors="replace").count("\n") + 1
    b = len(raw)
    line_pct = lines / CLI_INDEX_MAX_LINES
    byte_pct = b / CLI_INDEX_MAX_BYTES
    return {
        "lines": lines, "max_lines": CLI_INDEX_MAX_LINES,
        "bytes": b, "max_bytes": CLI_INDEX_MAX_BYTES,
        "pct_of_cap": round(max(line_pct, byte_pct) * 100),
        "over_budget": max(line_pct, byte_pct) >= _BUDGET_WARN_AT,
    }


def _oversized_entries(index_path: Path, max_chars: int) -> list[dict]:
    """Index lines that are summaries, not pointers.

    The single most expensive kind of rot, and the one nothing checked: the index is loaded
    verbatim every bootstrap, so a 300-char "hook" is paid forever. The CLI's own memory prompt
    asks for a one-line hook; the house rule is ~100 chars.
    """
    if not index_path.exists():
        return []
    out = []
    for n, line in enumerate(index_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.lstrip().startswith("- ") and len(line) > max_chars:
            out.append({"line": n, "chars": len(line), "text": line[:60] + "…"})
    return out


def lint(dir_path: Path, index_name: str, *, repo: Path | None,
         max_bytes: int, stale_days: int, dup_threshold: float,
         max_entry_chars: int = 150) -> dict:
    index_path = dir_path / index_name
    linked, broken_external = _index_links(index_path)

    # log.md is the wiki's chronology, not an article: it is never linked from the index and is
    # never loaded at bootstrap. Counting it as a page makes it a permanent phantom orphan.
    _NOT_ARTICLES = {index_name, "log.md"}
    files = sorted(p for p in dir_path.glob("*.md") if p.name not in _NOT_ARTICLES)
    now = time.time()
    repo_index = _repo_basenames(repo) if repo else set()

    entries = []
    for p in files:
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        size = len(body.encode("utf-8"))
        age_days = int((now - p.stat().st_mtime) / 86400)
        entries.append({
            "name": p.name,
            "bytes": size,
            "age_days": age_days,
            "title_words": _title_words(p.name, body),
            "orphan": p.name not in linked,
            "oversized": size > max_bytes,
            "stale_age": age_days > stale_days,
            "stale_refs": _stale_refs(body, repo, repo_index) if repo else [],
        })

    # near-duplicate detection (pairwise on title words)
    dups: list[dict] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            sim = _jaccard(entries[i]["title_words"], entries[j]["title_words"])
            if sim >= dup_threshold:
                dups.append({"a": entries[i]["name"], "b": entries[j]["name"],
                             "similarity": round(sim, 2)})

    # dead index links: local names with no file here, plus external targets that don't resolve
    present = {p.name for p in files} | {index_name}
    dead_links = sorted([l for l in linked if l not in present] + broken_external)

    for e in entries:
        e.pop("title_words", None)  # not serialisable / not needed downstream

    return {
        "dir": str(dir_path),
        "index": index_name,
        "total_files": len(files),
        "index_links": len(linked),
        "orphans": [e["name"] for e in entries if e["orphan"]],
        "dead_index_links": dead_links,
        "oversized": [{"name": e["name"], "bytes": e["bytes"]} for e in entries if e["oversized"]],
        "stale_by_age": [{"name": e["name"], "age_days": e["age_days"]} for e in entries if e["stale_age"]],
        "stale_refs": [{"name": e["name"], "missing": e["stale_refs"]} for e in entries if e["stale_refs"]],
        "near_duplicates": dups,
        "oversized_entries": _oversized_entries(index_path, max_entry_chars),
        "index_budget": _index_budget(index_path),
        # The method's second special file: an append-only chronology of ingests / lints / queries.
        # Absent it, nobody (agent or operator) can see how the wiki got to its current state.
        "has_log": (dir_path / "log.md").exists(),
    }


def _render(report: dict) -> str:
    L: list[str] = []
    L.append(f"# memory-lint report — {report['dir']}")
    L.append("")
    L.append(f"- files: **{report['total_files']}**  ·  index links: {report['index_links']}")
    n_issues = (len(report["orphans"]) + len(report["dead_index_links"])
                + len(report["oversized"]) + len(report["stale_by_age"])
                + len(report["stale_refs"]) + len(report["near_duplicates"])
                + len(report["oversized_entries"]))
    L.append(f"- flagged: **{n_issues}** (nothing was deleted — curate manually)")
    b = report.get("index_budget") or {}
    if b:
        warn = " ⚠️ **entries will be silently truncated**" if b["over_budget"] else ""
        L.append(f"- index budget: {b['lines']}/{b['max_lines']} lines · "
                 f"{b['bytes']}/{b['max_bytes']} bytes · **{b['pct_of_cap']}% of the CLI cap**{warn}")
    if not report.get("has_log", True):
        L.append("- ⚠️ no `log.md` — the wiki has no chronology (`tools/memory-wiki-init.sh <dir>`)")
    L.append("")

    def section(title: str, items: list[str]) -> None:
        L.append(f"## {title} ({len(items)})")
        if not items:
            L.append("_none_")
        else:
            L.extend(f"- {it}" for it in items)
        L.append("")

    section("Orphans — not linked from the index", report["orphans"])
    section("Dead index links — index points at a missing file", report["dead_index_links"])
    section("Oversized bodies", [f"{o['name']} — {o['bytes']} bytes" for o in report["oversized"]])
    section("Stale by age", [f"{s['name']} — {s['age_days']}d" for s in report["stale_by_age"]])
    section("Stale references — body cites files that no longer exist",
            [f"{s['name']} → {', '.join(s['missing'])}" for s in report["stale_refs"]])
    section("Near-duplicates — candidates to merge",
            [f"{d['a']} ≈ {d['b']} ({d['similarity']})" for d in report["near_duplicates"]])
    section("Oversized index entries — summaries, not pointers (paid every bootstrap)",
            [f"L{e['line']} — {e['chars']} chars — {e['text']}" for e in report["oversized_entries"]])
    return "\n".join(L)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Lint an LLM-maintained memory wiki (read-only).")
    ap.add_argument("--dir", default=".claude-ops/memory")
    ap.add_argument("--index", default="MEMORY.md")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--max-bytes", type=int, default=6000)
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--dup", type=float, default=0.6)
    # The CLI's own memory prompt asks for a one-line hook; the house rule is ~100 chars.
    # 150 is the forgiving default — anything past it is a summary living in the index.
    ap.add_argument("--max-entry-chars", type=int, default=150)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    dir_path = Path(os.path.expanduser(args.dir))
    if not dir_path.is_dir():
        print(f"memory-lint: no such directory: {dir_path}", file=sys.stderr)
        return 0
    repo = Path(os.path.expanduser(args.repo)) if args.repo else None

    report = lint(dir_path, args.index, repo=repo,
                  max_bytes=args.max_bytes, stale_days=args.stale_days,
                  dup_threshold=args.dup, max_entry_chars=args.max_entry_chars)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else _render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

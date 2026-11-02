#!/usr/bin/env python3
"""Where the prompt-cache re-reads go, by context size — the measurement behind the auto-compact window.

Every API call of a long session re-reads its whole context from the prompt cache, so the share of
cache-read tokens spent at a large context is the share an earlier compaction can save. Reads the
Claude Code transcripts under ~/.claude/projects (main sessions and sub-agents), last N days by file
mtime, one row per assistant message id (its final usage). Read-only; prints a table.

    venv/bin/python -I tools/context-spend-buckets.py [--days 7] [--root ~/.claude/projects]

Baseline 2026-10-03..11-02 (window unset, compaction at ~967K): 25.3 % of cache reads at <200K,
43.2 % at 200-400K, 23.2 % at 400-600K, 8.3 % at >=600K.
"""
import argparse
import glob
import os
import re
import time

_ID = re.compile(r'"id":"(msg_[A-Za-z0-9]+)"')
_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens", "input_tokens")
_NUM = {k: re.compile(r'"%s":(\d+)' % k) for k in _KEYS}
_BUCKETS = ((0, 200_000), (200_000, 400_000), (400_000, 600_000), (600_000, 10**9))


def _usage(line: str) -> "dict[str, int]":
    out = {}
    for k, rx in _NUM.items():
        m = rx.search(line)
        out[k] = int(m.group(1)) if m else 0
    return out


def scan(root: str, days: float) -> "tuple[list[int], list[int]]":
    cutoff = time.time() - days * 86400
    calls, reads = [0] * len(_BUCKETS), [0] * len(_BUCKETS)
    for pattern in ("/*/*.jsonl", "/*/*/subagents/*.jsonl"):
        for path in glob.glob(root + pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
                seen: "dict[str, dict[str, int]]" = {}
                with open(path, "r", errors="replace") as f:
                    for line in f:
                        if '"type":"assistant"' not in line or '"usage"' not in line:
                            continue
                        m = _ID.search(line)
                        if m:
                            seen[m.group(1)] = _usage(line)   # last occurrence = final usage
            except OSError:
                continue
            for u in seen.values():
                ctx = sum(u.values())
                for i, (lo, hi) in enumerate(_BUCKETS):
                    if lo <= ctx < hi:
                        calls[i] += 1
                        reads[i] += u["cache_read_input_tokens"]
    return calls, reads


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    a = ap.parse_args()
    calls, reads = scan(a.root, a.days)
    total = sum(reads) or 1
    print(f"last {a.days:g} days, {sum(calls)} API calls, {sum(reads) / 1e6:.0f}M cache-read tokens")
    for (lo, hi), c, r in zip(_BUCKETS, calls, reads):
        label = f">={lo // 1000}K" if hi >= 10**9 else f"{lo // 1000}-{hi // 1000}K"
        print(f"  ctx {label:>10}  calls {c:>7}  cache_read {r / 1e6:8.0f}M  ({100 * r / total:4.1f}%)")


if __name__ == "__main__":
    main()

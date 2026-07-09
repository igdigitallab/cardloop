#!/usr/bin/env bash
# Bootstrap a memory dir into a Karpathy-style LLM wiki. Idempotent — safe to re-run.
#
# The method has three layers: raw sources (immutable), the wiki (LLM-owned markdown), and the
# schema — the document that tells the LLM the conventions. The schema is deliberately NOT copied
# into each wiki: it lives once in ~/CLAUDE.md, which is loaded into every session of every
# project. Sixteen copies would cost the same tokens and rot independently.
#
# What this script does create is the article's second special file, `log.md`: an append-only
# chronology of ingests, lint passes and notable queries. It is NOT loaded at bootstrap (only
# MEMORY.md is), so it costs nothing until something reads it — and it gives both the operator and
# the agent a timeline of how the wiki evolved.
#
# Usage: memory-wiki-init.sh <memory-dir> [<memory-dir> ...]
#        memory-wiki-init.sh --all        # every native wiki under ~/.claude/projects
set -uo pipefail

init_one() {
  local dir="$1"
  mkdir -p "$dir" || return 1
  local index="$dir/MEMORY.md" log="$dir/log.md" today
  today="$(date +%Y-%m-%d)"
  local made=""

  # A brand-new project has memory/ but no MEMORY.md until the first memory is written.
  # Seed it so the index exists (and is lint-visible) from day one.
  if [ ! -f "$index" ]; then
    printf '# Memory Index\n\nRouting table, not a summary. One line per article; detail lives in the article.\n\n' > "$index"
    made="index"
  fi

  if [ ! -f "$log" ]; then
    {
      echo "# Wiki log"
      echo
      echo "Append-only chronology: one entry per ingest / lint pass / notable query."
      echo 'Keep the prefix parseable — `grep "^## \[" log.md | tail -5` should give the last five.'
      echo
      echo "## [$today] init | wiki bootstrapped"
    } > "$log"
    made="${made:+$made+}log"
  fi

  printf '  %-10s %s\n' "${made:-ok}" "$dir"
}

if [ "${1:-}" = "--all" ]; then
  for d in "$HOME"/.claude/projects/*/memory; do
    [ -d "$d" ] || continue
    init_one "$d"
  done
else
  [ $# -ge 1 ] || { echo "usage: $0 <memory-dir>... | --all" >&2; exit 2; }
  for d in "$@"; do init_one "$d"; done
fi

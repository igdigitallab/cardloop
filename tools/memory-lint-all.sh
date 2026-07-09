#!/usr/bin/env bash
# Weekly memory-wiki lint across BOTH memory systems on this machine.
#
# The Karpathy LLM-wiki method has three operations: ingest / query / lint. Auto-memory only ever
# ingests — it appends what a session learned and never prunes — so without a scheduled lint the
# index rots and every bootstrap pays for it. This is the lint, on a timer.
#
# Two systems, both loaded into live prompts, both worth linting:
#   1. ~/.claude/projects/<slug>/memory/   — native auto-memory. MEMORY.md is loaded VERBATIM at
#      every bootstrap and hard-truncated by the CLI past 200 lines / 25000 bytes.
#   2. <repo>/.claude-ops/memory/          — curated, injected by context_pack.py.
#
# It never deletes: it reports, and curation stays with the operator (that is the whole point).
#
# Usage: memory-lint-all.sh [report_path]
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/venv/bin/python"
LINT="$REPO/tools/memory-lint.py"
REPORT="${1:-$HOME/logs/memory-lint.md}"

[ -x "$PY" ] || PY="$(command -v python3)"
mkdir -p "$(dirname "$REPORT")"

total_flagged=0

emit_row() {  # $1 = label, $2 = memory dir
  local label="$1" dir="$2" index="$2/MEMORY.md"
  [ -f "$index" ] || return 0

  local json
  json="$("$PY" "$LINT" --dir "$dir" --json 2>/dev/null)" || return 0

  local files flagged pct
  read -r files flagged pct <<<"$("$PY" - "$json" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
# Keys must match tools/memory-lint.py exactly; a typo silently under-counts to zero.
flagged = sum(len(d.get(k) or []) for k in
              ("orphans", "dead_index_links", "oversized", "stale_by_age",
               "stale_refs", "near_duplicates", "oversized_entries"))
print(d.get("total_files", 0), flagged, (d.get("index_budget") or {}).get("pct_of_cap", 0))
PYEOF
)"
  local bytes; bytes="$(stat -c%s "$index")"
  local warn=""; [ "${pct:-0}" -ge 80 ] && warn=" ⚠️"
  printf '| %s | %s | %s | %s%%%s | %s |\n' "$label" "$files" "$bytes" "$pct" "$warn" "$flagged" >> "$REPORT"
  total_flagged=$(( total_flagged + flagged ))
}

{
  echo "# memory-lint — all wikis"
  echo
  echo "Generated: $(date '+%Y-%m-%d %H:%M %Z')"
  echo
  echo '`% of cap` = how close MEMORY.md is to the CLI ceiling (200 lines / 25000 bytes),'
  echo 'past which index entries are dropped from context with no warning to the operator.'
  echo
  printf '| wiki | files | index bytes | %% of cap | flagged |\n'
  printf '|---|---:|---:|---:|---:|\n'
} > "$REPORT"

# 1. Native auto-memory. The ./ prefix matters: slug dirs start with '-' and would parse as options.
for dir in "$HOME"/.claude/projects/*/memory; do
  [ -d "$dir" ] || continue
  emit_row "$(basename "$(dirname "$dir")")" "$dir"
done

# 2. Curated per-repo memory — injected into live prompts by context_pack.py and, until now,
#    never linted at all.
printf '| | | | | |\n' >> "$REPORT"
for dir in "$HOME"/*/.claude-ops/memory "$HOME"/projects/*/.claude-ops/memory; do
  [ -d "$dir" ] || continue
  repo_name="$(basename "$(dirname "$(dirname "$dir")")")"
  emit_row "$repo_name (curated)" "$dir"
done

{
  echo
  echo "**Total flagged: $total_flagged**"
  echo
  echo "Nothing was deleted. Curate by hand: merge related articles, distil progress notes into"
  echo "durable facts, and keep index entries as POINTERS (hook under ~100 chars) — the index is"
  echo "loaded verbatim on every single bootstrap. Detail: \`tools/memory-lint.py --dir <dir>\`"
} >> "$REPORT"

echo "memory-lint: $total_flagged flagged across all wikis → $REPORT"

#!/usr/bin/env bash
# Weekly memory-wiki lint across every project's native auto-memory.
#
# The Karpathy LLM-wiki method has three operations: ingest / query / lint. Auto-memory only ever
# ingests — it appends what a session learned and never prunes — so without a scheduled lint the
# index rots and every bootstrap pays for it. This is the lint, on a timer.
#
# It never deletes: it reports, and curation stays with the operator (that is the whole point).
# The report lands in a dated file; the cockpit's Schedules tab surfaces the cron entry itself.
#
# Usage: memory-lint-all.sh [report_path]
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/venv/bin/python"
LINT="$REPO/tools/memory-lint.py"
PROJECTS="$HOME/.claude/projects"
REPORT="${1:-$HOME/logs/memory-lint.md}"

[ -x "$PY" ] || PY="$(command -v python3)"
mkdir -p "$(dirname "$REPORT")"

{
  echo "# memory-lint — all projects"
  echo
  echo "Generated: $(date '+%Y-%m-%d %H:%M %Z')"
  echo
  printf '| project | files | index bytes | flagged |\n'
  printf '|---|---:|---:|---:|\n'
} > "$REPORT"

total_flagged=0

# ./ prefix: project dirs start with '-' and would otherwise parse as options.
for dir in "$PROJECTS"/*/memory; do
  [ -d "$dir" ] || continue
  slug="$(basename "$(dirname "$dir")")"
  index="$dir/MEMORY.md"
  [ -f "$index" ] || continue

  json="$("$PY" "$LINT" --dir "$dir" --json 2>/dev/null)" || continue
  read -r files flagged <<<"$("$PY" - "$json" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
# Keys must match tools/memory-lint.py exactly; a typo silently under-counts to zero.
flagged = sum(len(d.get(k) or []) for k in
              ("orphans", "dead_index_links", "oversized",
               "stale_by_age", "stale_refs", "near_duplicates"))
print(d.get("total_files", 0), flagged)
PYEOF
)"
  bytes="$(stat -c%s "$index")"
  total_flagged=$(( total_flagged + flagged ))
  printf '| %s | %s | %s | %s |\n' "$slug" "$files" "$bytes" "$flagged" >> "$REPORT"
done

{
  echo
  echo "**Total flagged: $total_flagged**"
  echo
  echo "Nothing was deleted. Curate by hand: merge related articles, distill progress notes into"
  echo "durable facts, keep index hooks under ~100 chars. Re-run: \`tools/memory-lint.py --dir <dir>\`"
} >> "$REPORT"

echo "memory-lint: $total_flagged flagged across all projects → $REPORT"

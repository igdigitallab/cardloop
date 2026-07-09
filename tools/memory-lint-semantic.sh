#!/usr/bin/env bash
# The SEMANTIC half of the lint operation — the half a script cannot do.
#
# tools/memory-lint.py checks structure: orphans, dead links, sizes, budget, age. Karpathy's lint
# is broader: "contradictions between pages, stale claims that newer sources have superseded,
# orphan pages, important concepts mentioned but lacking their own page, missing cross-references,
# data gaps". Those need a reader, not a regex.
#
# On 2026-07-09 three articles in the claude-ops-bot wiki asserted things the code had long since
# contradicted (MemoryHigh=4G, auto-rotate default OFF, "the handoff injection is never fed").
# Every structural check passed. A human found them by reading. This is that pass, automated.
#
# Read-only: the agent may Read/Grep/Glob and run git, and cannot Write/Edit. It reports; the
# operator curates. Never wire this to auto-apply.
#
# Usage: memory-lint-semantic.sh <memory-dir> [repo-dir] [report-path]
set -uo pipefail

DIR="${1:?usage: memory-lint-semantic.sh <memory-dir> [repo-dir] [report-path]}"
REPO="${2:-}"
REPORT="${3:-$HOME/logs/memory-lint-semantic-$(basename "$DIR").md}"

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$SELF/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
CLAUDE="$("$PY" -c 'import claude_agent_sdk,os;print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled/claude"
[ -x "$CLAUDE" ] || { echo "memory-lint-semantic: bundled CLI not found" >&2; exit 1; }

mkdir -p "$(dirname "$REPORT")"

read -r -d '' PROMPT <<EOF || true
You are linting an LLM-maintained memory wiki at: $DIR
${REPO:+The code it describes lives at: $REPO}

Read MEMORY.md (the index) and then read the articles. Where a claim is checkable against the code
or git history, CHECK IT — do not take the wiki's word for it.

Report, in this order, only what you can evidence:

1. CONTRADICTIONS — two articles (or an article and its index hook) that assert incompatible things.
2. STALE CLAIMS — statements the code, config or git history has since superseded. Cite file:line or
   a commit. This is the highest-value section; a wrong memory is loaded and believed.
3. MISSING PAGES — concepts referenced repeatedly across articles but having no article of their own.
4. MISSING CROSS-REFERENCES — articles that clearly bear on each other but do not link.
5. LEDGERS — progress/status notes for work that has shipped. git is the record; say what durable
   fact should be distilled out before the note is deleted.
6. QUESTIONS WORTH INVESTIGATING — gaps a future session should close.

For each finding: the file, the exact claim, the evidence that refutes or supports it, and the
suggested action. Be specific and short. If a section has nothing, write "none".
Do not modify any file. Your final message IS the report.
EOF

echo "memory-lint-semantic: reading $DIR …" >&2
timeout 900 "$CLAUDE" -p "$PROMPT" \
  --model claude-sonnet-5 \
  --permission-mode bypassPermissions \
  --disallowed-tools "Write,Edit,NotebookEdit" \
  > "$REPORT" 2>/dev/null

if [ -s "$REPORT" ]; then
  echo "memory-lint-semantic: $(wc -l < "$REPORT") lines → $REPORT"
else
  echo "memory-lint-semantic: empty report (model call failed) → $REPORT" >&2
  exit 1
fi

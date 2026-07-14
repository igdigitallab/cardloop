#!/usr/bin/env bash
# Lint the CLAUDE.md layer — the half a regex cannot do.
#
# Same disease as the memory wiki, different organ: CLAUDE.md files are loaded into EVERY session
# and only ever grow. Nothing checks whether what they assert is still true. On 2026-07-14 the main
# file still called the assistant "Fable 5" (it is Opus 4.8), pinned `gemini-2.5-flash` as the
# default model (used by no project, and contradicted two lines above), and pinned Playwright 1.59.
# Every one of those was believed, verbatim, at every bootstrap. A wrong instruction is worse than
# a missing one — it is loaded and obeyed.
#
# What it checks that a script cannot:
#   - claims refuted by the code/filesystem (paths, UUIDs, versions, "X goes through Y")
#   - contradictions between the main file and a project file, or inside one file
#   - duplication: a project file restating what the main file already says (the inheritance
#     contract says project files ADD, never repeat — a dupe rots independently)
#   - facts that belong in vault (UUIDs, tokens, long curl) squatting in a routing file
#
# Read-only: the agent may Read/Grep/Glob and run git, and cannot Write/Edit. It reports; the
# operator curates. Never wire this to auto-apply.
#
# Usage: claude-md-lint.sh [report-path]
set -uo pipefail

REPORT="${1:-$HOME/logs/claude-md-lint.md}"
MAIN="$HOME/CLAUDE.md"
LINE_CAP=120

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$SELF/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
CLAUDE="$("$PY" -c 'import claude_agent_sdk,os;print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled/claude"
[ -x "$CLAUDE" ] || { echo "claude-md-lint: bundled CLI not found" >&2; exit 1; }

mkdir -p "$(dirname "$REPORT")"

# The file list: main + every project file, one per line, with its length. The agent gets this so it
# does not have to go looking, and so an over-cap file is flagged even if the agent runs out of room.
FILES="$(printf '%s\n' "$MAIN" "$HOME"/*/CLAUDE.md "$HOME"/projects/*/CLAUDE.md 2>/dev/null \
  | awk '!seen[$0]++' | while read -r f; do
      [ -f "$f" ] && printf '%s (%s lines)\n' "$f" "$(wc -l < "$f")"
    done)"

read -r -d '' PROMPT <<EOF || true
You are linting the CLAUDE.md layer on this machine. These files are injected into EVERY Claude
session, verbatim. A stale claim here is not dead weight — it is actively believed and acted on.

The main file is: $MAIN (cap: $LINE_CAP lines). It is a ROUTER: role, discipline, gates, and
trigger→file routes. Facts (UUIDs, tokens, IPs, versions, long curl) are supposed to live in
~/vault or a project file, NOT in it.

Project files inherit the main file and must only ADD to it — never restate it. On conflict the
project file wins.

Files:
$FILES

Read the main file first, then the project files. Where a claim is checkable against the code, the
filesystem or git — CHECK IT. Do not take a file's word for it. Grep for the thing it asserts.

Report, in this order, only what you can evidence:

1. STALE / WRONG — claims the code, config, filesystem or git has superseded. Model names and
   versions, paths that no longer exist, UUIDs that no longer resolve, "X is done via Y" where the
   code does Z. Cite file:line or a commit. HIGHEST VALUE: a wrong instruction is obeyed.
2. CONTRADICTIONS — two files (or two lines in one file) asserting incompatible things.
3. DUPLICATION — a project file repeating what the main file already says. Name both locations;
   the fix is always to delete the copy in the project file.
4. MISPLACED — facts squatting in a routing file: UUIDs, tokens, IPs, versions, long curl in
   $MAIN. Say which vault/project file each belongs in.
5. OVER CAP — any file over its budget (main: $LINE_CAP lines). Say what to move out, and where.
6. SECRETS IN PLAINTEXT — any credential written literally instead of as \`secret get <name>\`.

For each finding: the file, the exact claim, the evidence that refutes it, and the suggested action.
Be specific and short. If a section has nothing, write "none".
Do not modify any file. Your final message IS the report.
EOF

echo "claude-md-lint: reading $(printf '%s' "$FILES" | wc -l) files …" >&2
timeout 900 "$CLAUDE" -p "$PROMPT" \
  --model claude-sonnet-5 \
  --permission-mode bypassPermissions \
  --disallowed-tools "Write,Edit,NotebookEdit" \
  > "$REPORT" 2>/dev/null

if [ -s "$REPORT" ]; then
  echo "claude-md-lint: $(wc -l < "$REPORT") lines → $REPORT"
else
  echo "claude-md-lint: empty report (model call failed) → $REPORT" >&2
  exit 1
fi

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
# Usage: claude-md-lint.sh [--quick-only] [report-path]
#   --quick-only   run only the deterministic checks below (no LLM call). Useful for fast
#                   smoke-testing the regex-based checks without paying the ~1800s agent pass.
set -uo pipefail

QUICK_ONLY=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--quick-only" ]; then
    QUICK_ONLY=1
  else
    ARGS+=("$a")
  fi
done

REPORT="${ARGS[0]:-$HOME/logs/claude-md-lint.md}"
MAIN="$HOME/CLAUDE.md"
LINE_CAP=120

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$SELF/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$(dirname "$REPORT")"

# The file list: main + every project file, one per line, with its length. The agent gets this so it
# does not have to go looking, and so an over-cap file is flagged even if the agent runs out of room.
FILES="$(printf '%s\n' "$MAIN" "$HOME"/*/CLAUDE.md "$HOME"/projects/*/CLAUDE.md 2>/dev/null \
  | awk '!seen[$0]++' | while read -r f; do
      [ -f "$f" ] && printf '%s (%s lines)\n' "$f" "$(wc -l < "$f")"
    done)"

# ─── deterministic pre-pass: the half a regex CAN do ───────────────────────────
# Three checks straight out of Anthropic's Claude 5-gen context-engineering guidance: a
# CLAUDE.md is loaded verbatim into every session, so bulk (long inline code, ledger framing)
# and staleness (an un-migrated retrofit marker) cost real tokens on every single bootstrap.
# Warnings only — never fails the run, never blocks the LLM pass below.
CODE_BLOCK_MAX=30
RU_BOILERPLATE_MARKER="Правила работы в кокпите"
# "✅ ... 2026-07-14"-style checkmarks, or "готово/сделано/verified/проверено 2026-..." — a dated
# ledger entry. Deliberately narrow (requires the verb/emoji immediately before the year) so it
# does not fire on legit changelog-style gotcha references such as a bare commit hash + date
# ("a1f0c0 (2026-07-14) squash decision") or a plain "as of 2026-07-14" note.
LEDGER_RE='✅.*20[0-9]{2}-[0-9]{2}-[0-9]{2}|(готово|сделано|verified|проверено) 20[0-9]{2}-'

quick_checks() {
  local warn_count=0
  local out=""
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    local f="${entry% (*}"
    [ -f "$f" ] || continue

    # (i) inline fenced code block over the line cap → should live in reference/<topic>.md.
    local blocks
    blocks="$(awk -v max="$CODE_BLOCK_MAX" '
      /^```/ {
        if (open) {
          len = NR - start - 1
          if (len > max) print start ":" len
          open = 0
        } else {
          open = 1
          start = NR
        }
        next
      }
    ' "$f")"
    if [ -n "$blocks" ]; then
      while IFS=: read -r line_no len; do
        [ -n "$line_no" ] || continue
        out+="- LONG CODE BLOCK — $f:$line_no ($len lines, cap $CODE_BLOCK_MAX). Move it to reference/<topic>.md and leave a pointer.\n"
        warn_count=$((warn_count + 1))
      done <<< "$blocks"
    fi

    # (ii) legacy Russian onboarding boilerplate that never got retrofitted.
    if grep -qF "$RU_BOILERPLATE_MARKER" "$f" 2>/dev/null; then
      out+="- LEGACY BOILERPLATE — $f contains \"$RU_BOILERPLATE_MARKER\". Re-run the retrofit or replace with the canonical template blocks.\n"
      warn_count=$((warn_count + 1))
    fi

    # (iii) dated ledger markers — status notes belong in git log, not the router file.
    local ledger_hits
    ledger_hits="$(grep -noE "$LEDGER_RE" "$f" 2>/dev/null)"
    if [ -n "$ledger_hits" ]; then
      while IFS=: read -r line_no _match; do
        [ -n "$line_no" ] || continue
        out+="- DATED LEDGER MARKER — $f:$line_no. Remove the ledger framing; git log is the source of truth.\n"
        warn_count=$((warn_count + 1))
      done <<< "$ledger_hits"
    fi
  done <<< "$FILES"

  if [ "$warn_count" -eq 0 ]; then
    out="none\n"
  fi
  printf '## Quick checks (deterministic, pre-LLM)\n\n%b\n' "$out"
  echo "claude-md-lint: quick checks — $warn_count warning(s)" >&2
}

QUICK_REPORT="$(quick_checks)"
printf '%s\n' "$QUICK_REPORT" > "$REPORT"

if [ "$QUICK_ONLY" -eq 1 ]; then
  echo "claude-md-lint: --quick-only → $REPORT (LLM pass skipped)"
  exit 0
fi

CLAUDE="$("$PY" -c 'import claude_agent_sdk,os;print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled/claude"
[ -x "$CLAUDE" ] || { echo "claude-md-lint: bundled CLI not found" >&2; exit 1; }

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
Do not modify any file.

OUTPUT CONTRACT — your final message IS the report, and nothing else is captured. It must be the
full report itself: no preamble, no "compiling now", no status line. If you are running out of room
or time, STOP READING and emit the report for what you have checked so far, marking the files you
did not reach under a "NOT CHECKED" heading. A truncated report is useful; a status line is not.
EOF

echo "claude-md-lint: reading $(printf '%s' "$FILES" | wc -l) files …" >&2
# 40+ files is a long read. At 900s the agent hit the wall mid-pass and its last message was a
# status line ("compiling the report…") — which sailed out as a zero-exit "success" and would have
# written that to the cron report every Monday. Hence both the wider budget and the floor below.
LLM_REPORT="$(mktemp)"
timeout 1800 "$CLAUDE" -p "$PROMPT" \
  --model claude-sonnet-5 \
  --permission-mode bypassPermissions \
  --disallowed-tools "Write,Edit,NotebookEdit" \
  > "$LLM_REPORT" 2>/dev/null

# A report that fits in a few lines is not a report — it is a truncation, and silence about it reads
# as "nothing found". Fail loudly instead. (Measured on the LLM section alone — the quick-checks
# section above is deterministic and would otherwise mask a truncated LLM pass.)
MIN_LINES=10
lines="$(wc -l < "$LLM_REPORT" 2>/dev/null || echo 0)"
cat "$LLM_REPORT" >> "$REPORT"
rm -f "$LLM_REPORT"

if [ "$lines" -ge "$MIN_LINES" ]; then
  echo "claude-md-lint: $lines lines → $REPORT"
else
  echo "claude-md-lint: TRUNCATED report ($lines lines, expected ≥$MIN_LINES) — the pass did not finish. → $REPORT" >&2
  exit 1
fi

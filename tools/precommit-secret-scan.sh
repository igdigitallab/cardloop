#!/usr/bin/env bash
# tools/precommit-secret-scan.sh
#
# Pre-commit guard for repos without gitleaks installed. Cardloop's agents commit and push
# autonomously (bypassPermissions, no human review before the commit lands), so this is the
# last barrier before a leaked credential is recorded forever in git history.
#
# Scans the STAGED diff (git diff --cached) for secret-shaped content, plus a filename guard
# that refuses to stage .env / *.pem / credentials.json / .credentials.json at all.
#
# Exit 0 = clean, commit proceeds.
# Exit 1 = a match was found; commit is blocked with a "file:line: reason" report.
#
# Escape hatches:
#   SKIP_SECRET_SCAN=1   bypasses the whole scan (for a confirmed false positive).
#   .secretscanignore    repo-root file, one shell glob per line ('#' comments allowed),
#                         matched against the staged file path — whole file is skipped.
set -euo pipefail

if [[ "${SKIP_SECRET_SCAN:-}" == "1" ]]; then
    echo "precommit-secret-scan: SKIPPED (SKIP_SECRET_SCAN=1)" >&2
    exit 0
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
IGNORE_FILE="$REPO_ROOT/.secretscanignore"

found=0

# ── 1. Filename guard: these files may never enter a commit, content aside ─────────────────
BLOCKED_FILENAME_RE='(^|/)(\.env|[^/]*\.pem|credentials\.json|\.credentials\.json)$'

is_ignored_path() {
    local f="$1"
    [[ -f "$IGNORE_FILE" ]] || return 1
    local pattern
    while IFS= read -r pattern; do
        pattern="${pattern%%#*}"
        # trim surrounding whitespace
        pattern="${pattern#"${pattern%%[![:space:]]*}"}"
        pattern="${pattern%"${pattern##*[![:space:]]}"}"
        [[ -z "$pattern" ]] && continue
        # shellcheck disable=SC2053
        [[ "$f" == $pattern ]] && return 0
    done < "$IGNORE_FILE"
    return 1
}

while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    is_ignored_path "$f" && continue
    if [[ "$f" =~ $BLOCKED_FILENAME_RE ]]; then
        echo "BLOCKED: $f — this filename is never allowed in a commit (env/credentials/key file)." >&2
        found=1
    fi
done < <(git diff --cached --name-only --diff-filter=ACMR)

# ── 2. Content guard: scan added lines of the staged diff against secret signatures ────────
# name:regex:flags  (flags: "i" = case-insensitive, "" = case-sensitive). ERE syntax (grep -E).
PATTERNS=(
  'private key:BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY:'
  'AWS access key:AKIA[0-9A-Z]{16}:'
  'AWS secret key:aws_secret_access_key[[:space:]]*[=:][[:space:]]*[A-Za-z0-9/+=]{30,}:i'
  'Stripe live key:(sk|rk)_live_[A-Za-z0-9]{16,}:'
  'GitHub token:gh[po]_[A-Za-z0-9]{30,}:'
  'Telegram bot token:[0-9]{8,10}:[A-Za-z0-9_-]{35}:'
  'Slack token:xox[abpr]-[0-9A-Za-z-]{10,}:'
  'Cloudflare token:cloudflare[_-]?(api[_-]?)?(token|key)[[:space:]]*[=:][[:space:]]*.{0,3}[A-Za-z0-9_-]{30,}:i'
  'JWT:eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}:'
  'generic api key:api[_-]?key[[:space:]]*[=:][[:space:]]*['"'"'"][A-Za-z0-9_-]{20,}['"'"'"]:i'
)

scan_added_lines() {
    # Reads a unified diff (context=0) on stdin, prints "path:lineno: name" for every added
    # line that matches one of PATTERNS. Pure text processing, no git calls inside.
    local cur_file="" cur_line=0
    while IFS= read -r diff_line; do
        case "$diff_line" in
            "+++ "*)
                cur_file="${diff_line#+++ }"
                cur_file="${cur_file#b/}"
                ;;
            "@@ "*)
                # @@ -a,b +c,d @@  — c is the first new-file line number of this hunk
                cur_line="$(sed -E 's/^@@ -[0-9]+(,[0-9]+)? \+([0-9]+)(,[0-9]+)? @@.*/\2/' <<<"$diff_line")"
                [[ "$cur_line" =~ ^[0-9]+$ ]] || cur_line=0
                ;;
            "+"*)
                [[ "$cur_file" == "/dev/null" ]] && continue
                local content="${diff_line#+}"
                for entry in "${PATTERNS[@]}"; do
                    local name="${entry%%:*}"
                    local rest="${entry#*:}"
                    local flags="${rest##*:}"
                    local regex="${rest%:*}"
                    local grep_flags=(-E -q)
                    [[ "$flags" == "i" ]] && grep_flags+=(-i)
                    if grep "${grep_flags[@]}" -- "$regex" <<<"$content"; then
                        echo "${cur_file}:${cur_line}: possible ${name}"
                    fi
                done
                cur_line=$((cur_line + 1))
                ;;
        esac
    done
}

# Build git pathspec excludes from .secretscanignore so a file that is excluded from the
# filename guard is ALSO excluded from the content scan below (one ignore list, both checks).
exclude_pathspecs=(":(exclude)$(basename "$IGNORE_FILE")")
if [[ -f "$IGNORE_FILE" ]]; then
    while IFS= read -r pattern; do
        pattern="${pattern%%#*}"
        pattern="${pattern#"${pattern%%[![:space:]]*}"}"
        pattern="${pattern%"${pattern##*[![:space:]]}"}"
        [[ -z "$pattern" ]] && continue
        exclude_pathspecs+=(":(exclude,glob)$pattern")
    done < "$IGNORE_FILE"
fi

diff_output="$(git diff --cached -U0 --no-color -- . "${exclude_pathspecs[@]}" 2>/dev/null || true)"
if [[ -n "$diff_output" ]]; then
    while IFS= read -r hit; do
        [[ -z "$hit" ]] && continue
        echo "BLOCKED: $hit" >&2
        found=1
    done < <(scan_added_lines <<<"$diff_output")
fi

if [[ "$found" -ne 0 ]]; then
    echo "" >&2
    echo "precommit-secret-scan: commit rejected — secret-shaped content or a blocked filename" \
         "is staged (see BLOCKED lines above)." >&2
    echo "  - Remove/rotate the secret and re-stage, OR" >&2
    echo "  - if this is a false positive, add the path to $IGNORE_FILE, OR" >&2
    echo "  - re-run with SKIP_SECRET_SCAN=1 git commit ... to bypass this one time." >&2
    exit 1
fi

exit 0

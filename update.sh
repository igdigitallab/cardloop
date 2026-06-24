#!/usr/bin/env bash
#
# Cardloop updater — pull the latest version, reinstall changed deps,
# rebuild the frontend, and restart the service. Safe to run anytime.
#
# Usage:   ./update.sh [--no-restart]
#
#   --no-restart   apply the update (pull/deps/build) but do NOT restart the
#                  service. Used by the cockpit's detached updater, which then
#                  restarts via restart-self.sh (the only cgroup-safe path).
#
# The whole body lives in main() so bash parses the entire script before
# running it — `git pull` may replace this very file mid-run, and a function
# definition is fully read (to its closing brace) before execution.
#
set -euo pipefail

log()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

main() {
  cd "$(dirname "$0")"

  local no_restart=0
  for arg in "$@"; do
    case "$arg" in
      --no-restart) no_restart=1 ;;
      *) die "unknown argument: $arg" ;;
    esac
  done

  git rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout — clone the repo instead of copying it"
  git remote get-url origin >/dev/null 2>&1 || die "no 'origin' remote — add one: git remote add origin <repo-url>"

  local branch before remote
  branch=$(git rev-parse --abbrev-ref HEAD)
  [ "$branch" != "HEAD" ] || die "detached HEAD — check out a branch first (e.g. git checkout master)"

  log "Fetching origin/${branch}"
  git fetch --quiet origin "$branch"

  before=$(git rev-parse HEAD)
  remote=$(git rev-parse "origin/${branch}")

  if [ "$before" = "$remote" ]; then
    log "Already up to date (${before:0:7}) — nothing to do."
    exit 0
  fi

  # Decide what to rebuild from the diff BEFORE we move HEAD.
  local reqs_changed=0 web_changed=0
  git diff --quiet "$before" "$remote" -- requirements.txt requirements-dev.txt || reqs_changed=1
  git diff --quiet "$before" "$remote" -- web/ || web_changed=1

  log "Updating ${before:0:7} → ${remote:0:7}"
  git merge --ff-only "origin/${branch}"

  if [ "$reqs_changed" = 1 ]; then
    log "Requirements changed → updating venv"
    venv/bin/pip install --quiet -r requirements.txt -r requirements-dev.txt
  fi

  if [ "$web_changed" = 1 ]; then
    log "Frontend changed → rebuilding"
    ( cd web && npm ci && npm run build )
  fi

  if [ "$no_restart" = 1 ]; then
    log "Applied (--no-restart) — caller is responsible for restarting."
    exit 0
  fi

  # `systemctl cat` exits 0 iff the unit exists — and avoids the pipefail trap of
  # `list-unit-files | grep -q` (grep closes the pipe early → systemctl gets SIGPIPE
  # → pipefail marks the whole pipeline failed even though the unit WAS found).
  if systemctl cat claude-ops-bot.service >/dev/null 2>&1; then
    log "Restarting claude-ops-bot.service"
    sudo systemctl restart claude-ops-bot
    systemctl --no-pager -n 5 status claude-ops-bot || true
  else
    warn "No systemd unit found — restart manually: venv/bin/python bot.py"
  fi

  log "Done."
}

main "$@"

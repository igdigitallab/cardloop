#!/usr/bin/env bash
#
# Detached cockpit-triggered updater (spec-047 workstream A).
#
# Invoked by POST /api/update as a detached process. It applies the update
# WITHOUT restarting (update.sh --no-restart), then — only on success —
# restarts via restart-self.sh (the only cgroup-safe restart path; an inline
# `systemctl restart` from the service's own process is a self-suicide).
#
# On build/install failure it does NOT restart: the currently running version
# stays live and the error is recorded in data/update-status.json so the
# cockpit can surface it.
#
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

DATA="data"
STATUS="$DATA/update-status.json"
LOG="$DATA/update.log"
mkdir -p "$DATA"

write_status() {  # state, detail
  printf '{"state":"%s","detail":"%s","ts":%s}\n' "$1" "$2" "$(date +%s)" > "$STATUS"
}

{
  echo "===== self-update $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
} >> "$LOG" 2>&1

write_status "updating" "applying update"

if ./update.sh --no-restart >> "$LOG" 2>&1; then
  write_status "restarting" "update applied — restarting service"
  ./restart-self.sh >> "$LOG" 2>&1
  exit 0
else
  write_status "failed" "update failed (see data/update.log) — kept the running version"
  exit 1
fi

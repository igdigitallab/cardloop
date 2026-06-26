#!/usr/bin/env bash
# Safe self-restart for Cardloop.
#
# Why: calling `sudo systemctl restart/stop cardloop` directly from a shell
# inside the bot kills that shell — it lives in the service's cgroup, and systemctl
# tears down the entire cgroup MID-COMMAND. So `stop && start` never reaches start
# and the bot stays down (this is how it killed itself on 2026-05-30, see CLAUDE.md).
#
# Solution: systemd-run launches the restart in a SEPARATE transient unit outside
# the service's cgroup — it survives the bot's death and reliably completes the restart.
set -euo pipefail

# Systemd unit name — OSS default "cardloop"; overridable via CARDLOOP_SERVICE (env or .env) so an
# existing deployment installed under a different unit name keeps restarting correctly.
DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="${CARDLOOP_SERVICE:-}"
if [ -z "$SERVICE" ] && [ -f "$DIR/.env" ]; then
  # `|| true`: a no-match grep exits 1, which under `set -e`+pipefail would abort
  # this script before scheduling the restart. Default installs DON'T set
  # CARDLOOP_SERVICE in .env, so without this the self-update never restarts.
  SERVICE="$(grep -E '^[[:space:]]*CARDLOOP_SERVICE=' "$DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | awk '{print $1}' | tr -d '"')" || true
fi
SERVICE="${SERVICE:-cardloop}"

# --on-active=6: the delay must be comfortably LONGER than the time the harness
# needs to commit the current turn/response. At 1s the restart SIGTERM-ed the agent
# turn process (which lives in the same cgroup) BEFORE the tool-call result was
# committed, causing the harness to show a spurious "Command failed with exit code 143".
# 6s gives the turn enough time to complete and be delivered; the restart fires later
# with no visible error.
sudo systemd-run --collect --quiet \
  --unit="cardloop-self-restart-$(date +%s)" \
  --on-active=6 --timer-property=AccuracySec=200ms \
  systemctl restart "$SERVICE"

echo "Restart of ${SERVICE} scheduled (~6s, detached via systemd-run, outside cgroup)."
echo "   Finish your turn IMMEDIATELY after this command (no further bash) — web cockpit (:8787) will come back up on its own."

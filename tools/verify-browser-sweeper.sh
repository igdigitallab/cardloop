#!/usr/bin/env bash
# One-shot post-restart check for the orphan-tab sweeper (fix bb46e8b). Writes its
# verdict into data/inbox/ so the result is visible in the cockpit even though the
# session that scheduled the restart did not survive it.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/data/inbox/browser-sweeper-check-$(date +%s).txt"
# Optional: ssh target of the browser host (user@host) to report its load; unset = skipped.
BROWSER_HOST_SSH="${BROWSER_HOST_SSH:-}"
{
  echo "Browser orphan sweeper — post-restart check ($(date '+%Y-%m-%d %H:%M:%S %Z'))"
  echo
  echo "## service"
  echo "active: $(systemctl is-active cardloop 2>/dev/null)"
  echo "started: $(systemctl show cardloop -p ActiveEnterTimestamp --value 2>/dev/null)"
  echo
  echo "## did the sweeper start?"
  journalctl -u cardloop --since "-6 min" --no-pager 2>/dev/null \
    | grep -E "browser orphan sweeper|Cardloop started" | tail -5
  echo
  echo "## import/runtime errors touching the browser modules"
  journalctl -u cardloop --since "-6 min" --no-pager 2>/dev/null \
    | grep -iE "browser_pane|browser_backends|orphan|ImportError|NameError|AttributeError" \
    | grep -viE "browser orphan sweeper started" | tail -10
  echo
  echo "## tabs the cockpit believes it owns"
  cat "$REPO/data/cloak-pages-owned.json" 2>/dev/null \
    || echo "(no registry file yet — it is written on the first pane open)"
  echo
  echo "## browser VM load"
  if [ -n "$BROWSER_HOST_SSH" ]; then
    ssh -o ConnectTimeout=8 -o BatchMode=yes "$BROWSER_HOST_SSH" 'uptime' 2>&1 | tail -1
  else
    echo "(BROWSER_HOST_SSH not set — skipped)"
  fi
} > "$OUT" 2>&1
chmod 644 "$OUT"

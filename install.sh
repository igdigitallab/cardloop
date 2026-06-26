#!/usr/bin/env bash
#
# Cardloop one-shot installer (bare-metal / systemd path) — idempotent.
# Docker users: skip this and use `docker compose up --build` instead.
#
# Usage:   ./install.sh
# Re-running is safe: it reuses an existing venv and never overwrites .env.
#
set -euo pipefail
cd "$(dirname "$0")"

log()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 1. Prerequisites ───────────────────────────────────────────────────────
command -v python3 >/dev/null || die "python3 not found — install Python >= 3.11"
python3 - <<'PY' || die "Python >= 3.11 required (found $(python3 -V))"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
command -v node >/dev/null || die "node not found — install Node 20+ (https://nodejs.org), then re-run"
command -v npm  >/dev/null || die "npm not found — comes with Node 20+"

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
[ "$NODE_MAJOR" -ge 18 ] || warn "Node $NODE_MAJOR detected; 20+ recommended"

# The engine drives the native `claude` binary at runtime, and `claude login`
# (a later step) needs it too. Not fatal here so the build can still finish.
if ! command -v claude >/dev/null; then
  warn "Claude Code CLI not found — install it before the first run:"
  warn "    curl -fsSL https://claude.ai/install.sh | bash   (or: npm install -g @anthropic-ai/claude-code)"
  warn "    then make sure its install dir (e.g. ~/.local/bin) is on your PATH"
fi

# ── 2. Python venv + dependencies ──────────────────────────────────────────
log "Python virtualenv + dependencies"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt -r requirements-dev.txt

# ── 3. .env scaffold (never clobber an existing one) ───────────────────────
if [ ! -f .env ]; then
  log "Creating .env from .env.example"
  cp .env.example .env
  SALT=$(venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')
  if grep -q '^WEB_COOKIE_SALT=' .env; then
    sed -i "s|^WEB_COOKIE_SALT=.*|WEB_COOKIE_SALT=${SALT}|" .env
  else
    printf '\nWEB_COOKIE_SALT=%s\n' "$SALT" >> .env
  fi
  warn "Edit .env and set WEB_PASSWORD before the first run."
else
  log ".env already present — leaving it untouched"
fi

# ── 4. Frontend build ──────────────────────────────────────────────────────
log "Building web frontend (npm ci && npm run build)"
( cd web && npm ci && npm run build )

# ── 5. Next steps ──────────────────────────────────────────────────────────
cat <<'EOF'

✅ Install complete.

Next:
  1. install the Claude Code CLI if you don't have it yet:
       curl -fsSL https://claude.ai/install.sh | bash   (or: npm install -g @anthropic-ai/claude-code)
       then make sure its install dir (e.g. ~/.local/bin) is on your PATH
  2. claude login                  # one-time Claude subscription auth
  3. edit .env  →  set WEB_PASSWORD  (CHANGE_ME is rejected at startup)
  4. run it:
       venv/bin/python bot.py      # cockpit → http://localhost:8787
     or install as a service:
       make service                # renders + enables the systemd unit

To update later:  ./update.sh   (or  make update)
EOF

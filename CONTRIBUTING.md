> CONTRIBUTING = как настроить окружение, тесты, lint и сделать коммит. Карта кода → ARCHITECTURE.md. Правила работы → CLAUDE.md.

# Contributing to Claude-Ops

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_GITHUB/claude-ops-bot.git
cd claude-ops-bot

# 2. Python environment
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt

# 3. Config
cp .env.example .env
# Edit .env: set BOT_TOKEN, GROUP_CHAT_ID, ALLOWED_USERS, WEB_PASSWORD, WEB_COOKIE_SALT

# 4. Frontend
cd web
npm install
npm run build
cd ..

# 5. Run
venv/bin/python bot.py
# Cockpit available at http://localhost:8787
```

## Auth note

Claude-Ops uses **subscription auth** via `~/.claude/.credentials.json` (claudeAiOauth).
Do **not** set `ANTHROPIC_API_KEY` — the engine explicitly removes it; setting it would
switch billing to API pay-per-token mode instead of using your Claude subscription.

## Tests

```bash
venv/bin/python -m pytest -q
# or
make test
```

## Frontend lint & format

```bash
cd web
npm run lint      # ESLint check
npm run format    # Prettier format
npm run build     # Production build → web/dist/
```

After editing `web/`, always rebuild before testing or deploying.

## Commit style

```
type(scope): short description (ops:ID)
```

- **type:** `feat` | `fix` | `docs` | `refactor` | `test` | `chore`
- **scope:** `bot` | `webapp` | `web` | `docs` | `tests` | `ci`
- **ops:ID** — kanban card ID from `TASKS.md` (e.g. `ops:c05lic`)

Examples:
```
feat(webapp): add project rename endpoint (ops:s03rename)
fix(bot): retry on NetworkError in _tg_call (ops:b12retry)
docs: add API reference (ops:m12apidoc)
```

## Secrets

- Never commit `.env` (it is gitignored).
- Never hardcode tokens, passwords, or IPs in tracked files.
- Before adding a new file: verify it does not contain secrets or personal data.

## Project layout

```
bot.py          — Telegram handlers + run_engine() (Claude Agent SDK)
webapp.py       — aiohttp cockpit, 57 HTTP routes, event bus
web/            — React + Vite SPA (build → web/dist/)
templates/      — new-project starters (*.tpl) + vault reference copies (reference/)
tests/          — pytest suite (300 passed / 6 skipped)
data/           — runtime state (gitignored: topics.json, sessions.json, audit/, runs/)
docs/API.md     — HTTP API reference
```

See ARCHITECTURE.md for a full code map with file:line references.

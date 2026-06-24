> CONTRIBUTING = how to set up the environment, run tests/lint, and commit. Code map → ARCHITECTURE.md. Working rules → CLAUDE.md.

# Contributing to Cardloop

## Quick Start

One command does everything (venv + deps + .env + frontend build):

```bash
git clone https://github.com/igdigitallab/cardloop.git
cd cardloop
./install.sh            # or: make install
claude login            # one-time Claude subscription auth
# edit .env → set WEB_PASSWORD
venv/bin/python bot.py  # cockpit → http://localhost:8787
```

Prefer the manual steps? They are equivalent to what `install.sh` runs:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # runtime + dev
cp .env.example .env       # set WEB_PASSWORD; WEB_COOKIE_SALT auto-generates if blank
cd web && npm ci && npm run build && cd ..
venv/bin/python bot.py
```

> To update later: `./update.sh` (or `make update`).

## Auth note

Cardloop uses **subscription auth** via `~/.claude/.credentials.json` (claudeAiOauth).
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
bot.py          — web-only launcher (loads env/auth, builds ctx, starts the cockpit); engine lives in engine.py
webapp.py       — aiohttp cockpit, 57 HTTP routes, event bus
web/            — React + Vite SPA (build → web/dist/)
templates/      — new-project starters (*.tpl) + vault reference copies (reference/)
tests/          — pytest suite (1400+ tests; run via venv/bin/python -m pytest)
data/           — runtime state (gitignored: topics.json, sessions.json, audit/, runs/)
docs/API.md     — HTTP API reference
```

See ARCHITECTURE.md for a full code map with file:line references.

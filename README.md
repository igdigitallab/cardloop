> README = what it is and how to run it. Code map → ARCHITECTURE.md. Working rules and gotchas → CLAUDE.md. API → docs/API.md. Contributing and setup → CONTRIBUTING.md.

# Claude-Ops

**A browser-based IDE for managing projects via the Claude Agent SDK** — no terminal required, accessible from any device. One engine, three input channels: a web cockpit, Telegram, and kanban cards. Fully autonomous: describe a task → the agent diagnoses, edits code, deploys, and reports back.

```
 Cockpit   ──┐
 Telegram  ──┼──→  run_engine()  ──→  Claude Agent SDK  ──→  files / git / deploy
 Card      ──┘     (async generator)     (subscription)        (full-auto)
```

---

## Three channels

### Cockpit (YOUR_DOMAIN)

A browser IDE — React + Vite SPA with an aiohttp backend.

**Sidebar:** projects with drag-and-drop sorting, collapse, unread badges. **Project tabs** at the top — switch between projects without losing state.

**Tabs per project (left panel ~55%):**

| Tab | What it does |
|---|---|
| **Overview** | Git status, health card (6 checks), "↑ Sync" button (commit+push), run tests |
| **CLAUDE.md** | View + inline editing (double-click) |
| **Logs** | Configurable log command (`log_cmd` in topics.json) |
| **Board** | Kanban from `TASKS.md` — Backlog / In Progress / Review / Failed |
| **Files** | Project file tree + viewer (MD render, code mono) |
| **Memory** | Agent memory files |

**Chat (right panel ~45%, persistent):**
- SSE stream, CLI-style tool rendering (Bash/Edit/Read/Write with diff).
- **Shared sessions** — start in Telegram, continue in the browser (and vice versa).
- Model (sonnet/opus/haiku) switchable on the fly.
- Message queue, pulse indicator, token statistics.
- Prompt library with categories and variables.
- "Stop" button actually interrupts the agent (`client.interrupt`).
- Session selection and management.

**Kanban board:**
- `TASKS.md` in the repo is the source of truth. Sections = columns, lines = cards.
- Move cards with buttons, drag-and-drop, or inline editing.
- **Auto-run:** moving a card to In Progress → engine executes the task → result + git-diff → Review / Failed → Telegram notification.
- Three-layer data-loss protection.

**Additional features:**
- Free-form chats (not tied to a project).
- Global file browser (`$HOME`) with inline editing.
- Attachments: 📎, drag-and-drop, Ctrl+V.
- Usage badge: subscription limits (5h + week).
- Project creation (templates + onboarding agent), audit, upgrade, health-check, rename.

### Telegram channel

Forum group "Development", @YOUR_BOT. **Each topic = a project** (mapped `thread_id → cwd`).

- Write a task → the agent works in the project directory.
- Forward an alert or screenshot → the agent diagnoses and fixes it.
- Files (up to 20MB): documents and photos are handled by the agent.
- Commands: `/reset` `/resume` `/model` `/project` `/newtopic` `/diff` `/cost` `/usage` `/stop` `/whoami`

### Kanban auto-run

Moving a card to In Progress → `_run_card` in webapp.py triggers `run_engine` → result written to `data/runs/<card>.md` → card moves to Review/Failed → notification sent to the TG topic.

---

## Authorization: important warning

> **Claude-Ops uses subscription-based auth, not an API key.**

The engine reads `~/.claude/.credentials.json` (claudeAiOauth, issued on `claude login`).

**Do not set `ANTHROPIC_API_KEY`** in `.env` or the environment — `bot.py` explicitly removes this variable at startup. If it is present, the SDK will switch to pay-per-token API billing instead of the subscription.

**Access is strictly controlled by `ALLOWED_USERS`** — only the listed Telegram user IDs can interact with the bot and cockpit.

---

## Quickstart

The minimum required setup is Claude subscription auth + a web password. Telegram is optional.

```bash
# 1. Clone
git clone https://github.com/YOUR_GITHUB/claude-ops-bot.git && cd claude-ops-bot

# 2. Python
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt

# 3. Config
cp .env.example .env
# Required: WEB_PASSWORD, WEB_COOKIE_SALT
# Optional: BOT_TOKEN + GROUP_CHAT_ID + ALLOWED_USERS  (Telegram channel)
# Claude auth: run `claude login` once — credentials stored in ~/.claude/.credentials.json

# 4. Frontend
cd web && npm install && npm run build && cd ..

# 5. Run
venv/bin/python bot.py  # Cockpit → http://localhost:8787
```

**Minimal (web only):** set `WEB_PASSWORD` and `WEB_COOKIE_SALT`, leave `BOT_TOKEN` empty — the bot starts without Telegram.

**With Telegram:** additionally set `BOT_TOKEN`, `GROUP_CHAT_ID`, and `ALLOWED_USERS`.

**Public access:** put the service behind a reverse proxy (Cloudflare Tunnel, nginx, Caddy, etc.) and point your domain to `localhost:8787`. By default it only listens on localhost.

Details (tests, lint, deploy) → [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Access

| Channel | Address |
|---|---|
| **Cockpit** | `https://YOUR_DOMAIN` (Cloudflare Tunnel / reverse proxy) / `localhost:8787` (local) |
| **Telegram** | Forum group "Development", @YOUR_BOT *(optional)* |

- **Cockpit auth:** `WEB_PASSWORD` in `.env`.
- **Telegram auth:** `ALLOWED_USERS` (user ID whitelist).
- **SDK auth:** subscription (`~/.claude/.credentials.json`), **not `ANTHROPIC_API_KEY`**.

---

## Operations

```bash
# Logs
sudo journalctl -u claude-ops-bot -f

# Restart from inside the agent (THE ONLY safe method)
bash $HOME/claude-ops-bot/restart-self.sh

# Restart from terminal
sudo systemctl restart claude-ops-bot

# Frontend (after editing web/)
cd $HOME/claude-ops-bot/web && npm run build

# Tests
cd $HOME/claude-ops-bot && venv/bin/python -m pytest -q
```

---

## Documentation

| File | Purpose |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Code map: where to find what, flow diagram |
| [CLAUDE.md](CLAUDE.md) | Working rules and gotchas for agents |
| [docs/API.md](docs/API.md) | HTTP API reference (56 routes) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contributing: setup, tests, lint, commit style |
| `TASKS.md` | Live board (kanban) — backlog and current tasks |
| `DONE.md` | Archive of completed work. Sessions do NOT read this. |

---

## Tech stack

Python 3.11 · aiohttp · python-telegram-bot · Claude Agent SDK · React 18 · Vite · TypeScript · systemd · Cloudflare Tunnel · pytest

---

## Credits

The built-in default prompt templates (`spec-writer`, `debug-triage`, `pre-deploy-gate`) and the
executor sub-agent addendums (planning mode, source-driven development, doubt-check) are adapted
from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills), published under the
[MIT License](https://github.com/addyosmani/agent-skills/blob/main/LICENSE) by Addy Osmani et al.

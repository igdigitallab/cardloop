> API = таблица HTTP-роутов. Карта кода → ARCHITECTURE.md. Правила работы → CLAUDE.md. Запуск → CONTRIBUTING.md.

# Claude-Ops HTTP API Reference

Backend: `aiohttp`, port `WEB_PORT` (default `8787`).

**Auth:** All `/api/*` endpoints require a valid `cops_auth` cookie (scrypt-derived from `WEB_PASSWORD`)
except `/api/health` and `/api/login` which are public.
Cookie is obtained via `POST /api/login` and cleared via `POST /api/logout`.

---

## Auth / Session

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/health` | Health check — always returns `{"ok":true}` | No |
| `POST` | `/api/login` | Authenticate with `{"password":"..."}`, sets `cops_auth` cookie | No |
| `POST` | `/api/logout` | Clear `cops_auth` cookie | Yes |
| `GET` | `/api/me` | Current auth status | Yes |

---

## Проекты (Projects)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects` | List all projects (from `data/topics.json`, deduped by cwd) | Yes |
| `POST` | `/api/projects/new` | Create new project: makes `~/projects/untitled-<ts>/`, adds to `topics.json`, spawns onboarding card in In Progress | Yes |
| `GET` | `/api/projects/{id}/claude-md` | Read project `CLAUDE.md` | Yes |
| `POST` | `/api/projects/{id}/claude-md` | Write project `CLAUDE.md` | Yes |
| `GET` | `/api/projects/{id}/readme` | Read project `README.md` | Yes |
| `POST` | `/api/projects/{id}/readme` | Write project `README.md` | Yes |
| `GET` | `/api/projects/{id}/specs` | List spec files in project | Yes |
| `GET` | `/api/projects/{id}/specs/{name}` | Read a specific spec file by name | Yes |
| `GET` | `/api/projects/{id}/logs` | Run `log_cmd` from `topics.json` (timeout 8s, last 300 lines) — `{lines, configured, cmd}` | Yes |
| `GET` | `/api/projects/{id}/activity` | Recent activity log for the project | Yes |
| `GET` | `/api/projects/{id}/running` | Whether the agent is currently running for this project | Yes |
| `POST` | `/api/projects/{id}/model` | Set active model for next request — `{"model":"sonnet\|opus\|haiku"}` | Yes |
| `POST` | `/api/projects/{id}/git/sync` | Commit dirty files + push (one-button sync) | Yes |
| `POST` | `/api/projects/{id}/test` | Run tests (auto-detects pytest / npm test / make test) | Yes |
| `POST` | `/api/projects/{id}/upload` | Upload file attachment (multipart, max 20MB) to `data/inbox/` | Yes |
| `GET` | `/api/projects/{id}/skills` | List available agent skills (global `~/.claude/skills/` + project `.claude/skills/`) | Yes |
| `POST` | `/api/projects/{id}/scan-errors` | Trigger error scanner: creates Failed cards for new incidents | Yes |
| `GET` | `/api/projects/{id}/incidents` | Count active error/incident cards in the project | Yes |
| `POST` | `/api/projects/{id}/self-heal` | Toggle self-healing for a project — `{"enabled": bool}`. Writes `self_heal` to all `topics.json` entries for this cwd. Returns `{ok, self_heal, topics_updated}`. **OFF by default — never auto-applies.** | Yes |
| `POST` | `/api/projects/{id}/rename` | Rename project folder: `{"slug":"new-name"}` (kebab-case, `^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$`); 409 if busy or folder exists | Yes |
| `GET` | `/api/projects/{id}/health` | Structural health check (6 points: CLAUDE.md, cockpit rules, TASKS.md preamble, README, .gitignore/.env, .git) — `{color:"green\|yellow\|red", checks:[...]}` | Yes |
| `POST` | `/api/projects/{id}/audit` | Spawn audit card in In Progress; agent walks `templates/reference/audit-prompt.md` and creates issue cards | Yes |
| `POST` | `/api/projects/{id}/upgrade` | Spawn upgrade card: supplements existing CLAUDE.md / TASKS.md / README / .gitignore from templates without overwriting | Yes |

---

## Доска / Tasks (Kanban)

Source of truth: `TASKS.md` in the project root. Sections `## Backlog / In Progress / Review / Failed` are columns; cards are markdown list items `- [x] text <!--ops:ID-->`.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/tasks` | Parse `TASKS.md` → return all cards grouped by column | Yes |
| `POST` | `/api/projects/{id}/tasks` | Create new card in Backlog — `{"text":"..."}` | Yes |
| `GET` | `/api/projects/{id}/tasks/done` | Read archived cards from `DONE.md` | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/move` | Move card to another column — `{"to":"Backlog\|In Progress\|Review\|Failed\|done"}`. Moving to **In Progress** auto-starts `run_engine`; moving to `done` archives to `DONE.md` | Yes |
| `PATCH` | `/api/projects/{id}/tasks/{card}` | Edit card text in-place — `{"text":"..."}` | Yes |
| `DELETE` | `/api/projects/{id}/tasks/{card}` | Delete card from `TASKS.md` | Yes |
| `GET` | `/api/projects/{id}/tasks/{card}/run` | Get sidecar result of a card auto-run from `data/runs/<card>.md`. Also returns `meta` field (mode, has_changes, applied, discarded) from JSON sidecar | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/apply` | **C2-gate**: merge worktree branch `card-<id>` into base branch via `git merge --no-ff`. Moves card Review→Done. 400 if legacy/no meta; 409 if merge conflict (abort is automatic, worktree stays). Requires worktree mode | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/discard` | **C2-gate**: discard worktree changes — removes worktree + branch `card-<id>`. Moves card Review→Backlog. 400 if legacy/no meta | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/check` | **Spec 009 quality gate**: run tests in worktree (`_detect_test_cmd` auto-detect) and return verdict. Response: `{verdict:"safe\|risky\|unknown", tests:{detected,ok,cmd,exit_code,output,timed_out}, lint:null}`. Legacy/no-worktree → `{verdict:"unknown",reason:"legacy"}`. Result saved to `meta.gate={verdict,ts}`. 400 if bad card_id; 404 if project or worktree not found. Timeout: 300s. Secrets injected from `.claude-ops/secrets/secrets.env`. Does NOT block apply — user decides | Yes |

---

## Чат / SSE (Chat & Streaming)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/projects/{id}/chat` | Start agent task — returns `text/event-stream` SSE stream of `{type:"tool\|text\|result\|error", ...}`. Shared session + lock with Telegram and board auto-runs. 409 if project is busy | Yes |
| `POST` | `/api/projects/{id}/chat/stop` | Interrupt the current agent run (`client.interrupt()`). Note: server-side generator runs to completion; only client fetch is disconnected | Yes |
| `GET` | `/api/projects/{id}/activity-stream` | SSE stream of board bus events for this project (`run_start / tool / text / run_end`), heartbeat 25s | Yes |
| `GET` | `/api/activity-stream` | SSE stream of ALL projects' bus events (for unread indicators in sidebar) | Yes |

---

## Файлы проекта (Project Files)

Read-only file explorer within project `cwd`. `.env*` files (except `.env.example`) and internal dirs (`.git`, `venv`, `node_modules`, `dist`, `__pycache__`) are blocked. Anti-traversal enforced.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/files` | Directory listing — `?path=<rel>` relative to project cwd | Yes |
| `GET` | `/api/projects/{id}/file` | File contents — `?path=<rel>`, max 1MB; binary files rejected | Yes |

---

## Глобальные файлы (Global File Browser)

File browser rooted at `$HOME`. Same security rules as project files. Supports inline editing.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/global/files` | Directory listing — `?path=<abs or rel to $HOME>` | Yes |
| `GET` | `/api/global/file` | Read file contents | Yes |
| `POST` | `/api/global/file` | Write file contents — `?path=<path>`, body = raw text | Yes |

---

## Промты (Prompt Library)

Global prompt templates stored in `data/prompts.json` (not in git). Supports categories and `[VARIABLE]` placeholders.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/prompts` | List all prompts `[{id, title, category, text}, ...]` | Yes |
| `POST` | `/api/prompts` | Create prompt — `{"title":"...", "category":"...", "text":"..."}` | Yes |
| `PATCH` | `/api/prompts/{id}` | Update prompt fields | Yes |
| `DELETE` | `/api/prompts/{id}` | Delete prompt | Yes |

---

## Сессии (Sessions)

Claude SDK sessions (`~/.claude/projects/<cwd-encoded>/*.jsonl`). Session is shared across Telegram, cockpit, and board auto-runs.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/sessions` | List SDK sessions for project — `[{id, preview, ts}, ...]` | Yes |
| `POST` | `/api/projects/{id}/sessions/{sid}/label` | Set human-readable label on a session | Yes |
| `POST` | `/api/projects/{id}/session` | Switch active session — `{"action":"new\|resume", "session_id":"..."}`. 409 if project is busy | Yes |
| `GET` | `/api/projects/{id}/session-history` | Full conversation history of the active session (from SDK `.jsonl` transcript) | Yes |
| `GET` | `/api/projects/{id}/session-context` | Current session context summary (Фича A — context read) | Yes |

---

## Память (Memory)

Project memory lives in **`<cwd>/.claude-ops/memory/`** — committed to git, travels with the repo.
Response format for all endpoints: `{files:[{name, content}], exists}`. `MEMORY.md` is always first (index).
File names: `^[a-z0-9][a-z0-9-]{0,60}\.md$` or `MEMORY.md`. Max size per file: 256 KB.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/memory` | Read all memory files. Reads `.claude-ops/memory/`; fallback to old `~/.claude/projects/<cwd>/memory/` if new path absent. Returns `{files, exists}`. | Yes |
| `POST` | `/api/projects/{id}/memory/{name}` | Create or update a memory entry. Body: `{"content":"..."}`. Validates slug, checks size limit, atomic write, auto-reindexes `MEMORY.md`. Returns updated `{files, exists}`. Errors: 400 bad name/size, 404 project not found. | Yes |
| `DELETE` | `/api/projects/{id}/memory/{name}` | Delete a memory entry. Auto-reindexes `MEMORY.md`. Returns updated `{files, exists}`. Cannot delete `MEMORY.md` directly (400). 404 if entry not found. | Yes |

---

## Секреты проекта (Project Secrets — Spec 007)

Project-scoped secrets stored in `<cwd>/.claude-ops/secrets/secrets.env` (chmod 600, gitignored).
**Security**: values are NEVER returned by the API — only key names. Values are injected into the agent process env at runtime.
Key format: `^[A-Z_][A-Z0-9_]*$`. Max value size: 8 KB. Max keys: 100.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/secrets` | List secret key **names** (not values!). Returns `{keys:["KEY1","KEY2",...], exists:bool}`. | Yes |
| `POST` | `/api/projects/{id}/secrets/{key}` | Set a secret — body `{"value":"..."}`. Validates key format, checks limits. Returns updated key list (no values). 400 bad key or limits exceeded; 404 project not found. | Yes |
| `DELETE` | `/api/projects/{id}/secrets/{key}` | Delete a secret key. Returns updated key list. 404 if key/project not found; 400 invalid key. | Yes |

---

## Timeline — лента событий (Spec 008)

Persistent event log for a project. Every event published via `_bus_publish` is appended to `data/timeline/<slug>.jsonl`.
Rotation: when file exceeds **5 MB** it is renamed to `.jsonl.1` (single backup, overwrites previous). Reading merges both files.
Event schema: `{ts, session_key, kind, source?, run_id?, prompt?, text?, tool?, outcome?}`. **env field is never written.**

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/timeline` | Chronological list of events (newest at bottom). Query params: `limit` (default 200, max 500), `before=<ts>` (Unix float, for pagination — return events with ts < before). Returns `{events:[...]}`. 404 if project not found. | Yes |

---

## Usage

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/usage` | Subscription usage — 5h and 7-day limits with utilisation 0–1 and `resets_at`. Source: `GET https://api.anthropic.com/api/oauth/usage` (cached 60s). Falls back to passive `RateLimitEvent` snapshot if oauth endpoint fails | Yes |

---

## Свободные чаты (Free Chats)

Free-form chats not tied to a project (`cwd=$HOME`). Shown in tab bar, hidden from sidebar.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/free` | Create a new free chat — returns `{id, ...}` | Yes |
| `POST` | `/api/free/{id}/rename` | Rename free chat — `{"name":"..."}` | Yes |
| `DELETE` | `/api/free/{id}` | Delete free chat | Yes |

---

## SPA Fallback

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `*` | `/{path:.*}` | Serve `web/dist/index.html` for all non-API routes (React SPA) | No |

---

## Summary

Total registered routes: **60** API routes + 1 SPA catch-all (61 total).

Public (no cookie): `GET /api/health`, `POST /api/login`.
All other `/api/*` routes require a valid `cops_auth` session cookie.

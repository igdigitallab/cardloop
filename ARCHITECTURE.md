> ARCHITECTURE = code map (where to find what). Gotchas → CLAUDE.md. HTTP contract → docs/API.md. Running → CONTRIBUTING.md.

# ARCHITECTURE.md — Claude-Ops

Navigation guide for the codebase. Source of truth = the code; this file is the map. Changing behavior → find the right file and line here.

> Claude-Ops — a browser IDE for managing projects via the Claude Agent SDK. Three input channels, one engine, full-auto.
> **Single process** (aiohttp + python-telegram-bot): `bot.py` imports `webapp.py` and runs the cockpit in the same event loop. Shared `running` lock → no race condition between channels on the same cwd.

```
┌─────────────────────────────────────────────────────────────────┐
│                      SINGLE PYTHON PROCESS                       │
│                                                                  │
│  Telegram (@YOUR_BOT) ─┐                                         │
│  Cockpit (YOUR_DOMAIN) ─┼─► run_engine() ─► Claude SDK          │
│  Kanban auto-run (card) ──┘   (async event generator)           │
│                                                                  │
│  Shared state: running{} · sessions{} · topics{} (via ctx)      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Core: `bot.py` (~1020 lines, 45 functions)

TG channel + **engine** + process entry point.

### Engine (transport-independent core)
- **`run_engine(...)` (bot.py:419)** — `async def -> AsyncGenerator[dict, None]`. Drives the Claude Agent SDK, yields events `{tool|text|result|rate_limit|error}`. **Transport-agnostic.** All channels are its consumers. Change agent logic → here.
- **Engine consumers:**
  - `run_agent(context, update, prompt)` (bot.py:509) — TG adapter: status message, watchdog, audit, final send.
  - `_run_card(...)` in **webapp.py** — card auto-run.
  - `api_project_chat` in **webapp.py** — web chat (SSE consumer).

### Concurrency / state
- **`running{key: bool}`** — per-`cwd` lock. Reserved SYNCHRONOUSLY in `on_message` (bot.py:766) before the first await, released in `safe_run` (bot.py:806) `finally`. Guards against two parallel processes on the same project.
- **`sessions{key: session_id}`** (LAYER 2, `data/sessions.json`, `save_sessions` bot.py:196) — SDK sessions, cleared by `/reset`.
- **`topics{key: {project,cwd,model,log_cmd,...}}`** (LAYER 1, `data/topics.json`, `save_topics` bot.py:192) — channel→project mapping, permanent.
- `key_of(update)` (bot.py:200), `binding_for(update)` (bot.py:206) — resolve key `"chat:thread"`.

### Project registry
- `build_registry()` (bot.py:154), `resolve_project(name)` (bot.py:166), `_home_sub(*parts)` (bot.py:122) — paths from `Path.home()` (no hardcoded `/home/<user>`). New project → alias in `_REG_RAW` or auto-scan `~`.

### TG commands (bot.py:859–1062)
`cmd_start · cmd_whoami · cmd_reset · cmd_resume · cmd_model · cmd_project · cmd_newtopic · cmd_diff · cmd_cost · cmd_usage · cmd_stop`. Handlers: `on_message`, `on_topic_created` (auto-bind project), `on_error`.

### Rendering / utilities
- `md_to_html(text)` (bot.py:278) + `_render_code_block` (bot.py:264) — markdown→TG HTML with code folding. ALL responses go through it (otherwise HTML parse_mode crashes).
- `send()` (bot.py:244) + `_tg_call()` (bot.py:226) — send with retry on transient errors; `_smart_chunks` (bot.py:347) — split by `TG_CHUNK=4000`.
- `audit()` (bot.py:390) + `_is_destructive()` (bot.py:385) — full-auto audit log in `data/audit/`.

### Startup
- **`_on_start(app)` (bot.py:976)** — post_init: launches `webapp.start(app, ctx)`. **`ctx` is built here** — a dict of shared state references, passed into webapp.
- `main()` (bot.py:996) — assembles the PTB application, registers handlers, calls `_load_env()`.

---

## Cockpit: `webapp.py` (~3730 lines, 57 routes)

aiohttp server. **Does NOT import `bot.py`** (would double the state!) — everything comes via `ctx` (passed from `bot.py:_on_start`).

- **`AppCtx(TypedDict, total=False)` (webapp.py:1479)** — types for `ctx`: `topics/sessions/running/resolve_project/run_engine/DATA/HERE/...`. At runtime it's a plain dict; the annotation exists for readability. **Want to know what's available in webapp — see AppCtx.**
- `start(app, ctx)` — registers all routes + middleware. **Full route list → `docs/API.md`.**
- **Auth:** cookie `cops_auth`, `_derive_token` via `hashlib.scrypt` (salt `WEB_COOKIE_SALT`), `secure/httponly/samesite`, rate-limit 5 fails/5min → 429. Middleware on `/api/*` except `/api/health`, `/api/login`.

### Key handler groups (named `api_*`)
| Area | Handlers | Notes |
|---|---|---|
| Projects | `api_projects`, `api_new_project`, `api_rename`, `api_health`, `api_git_sync` | rename migrates SDK sessions+Timeline (`_migrate_cwd_keyed_state`) |
| Settings (f2ba02) | `api_settings_get/post` (global `data/settings.json`), `api_project_settings_get/post` (topics.json) | `_get_global_setting`/`_git_enabled`/`_effective_default_model`; git_enabled=false → run-mode legacy |
| Board/Tasks | `api_project_tasks`, `api_create_task`, `api_move_task`, `api_delete_task`, `api_update_task`, `api_card_run`, `api_tasks_done` | `card_id` validated by `_valid_card_id`/`_CARD_ID_RE` |
| Auto-run | **`_run_card`** → `_write_sidecar` + `_move_card_after_run` + `_notify_tg` | split into 3 helpers. Move to In Progress → run_engine |
| Chat/SSE | `api_project_chat`, `api_chat_stop`, `_sse_stream`, `api_activity_stream` | shared `_sse_stream` |
| Files | `api_project_files`, `api_project_file`, `api_global_files`, `api_global_file` | shared `_read_file_content`; anti-traversal `_resolve_safe`/`_resolve_global_safe` |
| Prompts | `api_prompts` (CRUD) | `data/prompts.json` |
| Sessions | `api_sessions`, `api_session` (new/resume), `api_session_history`, `api_session_context` | shared with TG |
| Usage | `api_usage` | oauth endpoint, 60s cache |
| Project memory | `api_project_memory` (GET), `api_project_memory_write` (POST), `api_project_memory_delete` (DELETE) | Path: `<cwd>/.claude-ops/memory/` (new) + fallback to `~/.claude/projects/<cwd>/memory/` (legacy). Agent writes via normal Write. Helpers: `_project_memory_dir`, `_memory_read_all`, `_memory_write`, `_memory_delete`, `_memory_reindex`. Names validated by `_valid_memory_name` (slug-regex). |
| **Project secrets** (Spec 007) | `api_project_secrets` (GET), `api_project_secrets_set` (POST), `api_project_secrets_delete` (DELETE) | Path: `<cwd>/.claude-ops/secrets/secrets.env` (chmod 600, gitignored). **Values are NEVER returned via API** — only key names. Helpers: `_project_secrets_path`, `_secrets_read`, `_secrets_write`, `_secrets_set`, `_secrets_delete`, `_secrets_ensure_gitignore`. Keys validated by `_SECRETS_KEY_RE = ^[A-Z_][A-Z0-9_]*$`. Limits: 8KB/value, 100 keys. |
| **Timeline** (Spec 008) | `api_project_timeline` (GET) | Persistent event bus log. Helpers: `_timeline_init`, `_timeline_path`, `_timeline_append`, `_timeline_slug_from_cwd`, `_timeline_read_events`. Hook in `_bus_publish` — single write point. File: `data/timeline/<slug>.jsonl` (+ `.jsonl.1` backup). env field is never written. |
| Misc | `api_logs`, `api_claude_md`, `api_audit`, `api_upgrade`, `api_scan_errors`, `_ingest_errors_to_board` | |
| Subprocess | `_run_log_cmd`, `_run_test_cmd`, `api_project_logs` | `create_subprocess_exec(*shlex.split())` — NOT shell |

---

## Frontend: `web/src/` (React + Vite + TS, 39 files)

```
web/src/
├── main.tsx                  entry point
├── App.tsx                   root: projects, tabs, polling
├── api.ts                    HTTP client (VITE_BACKEND_URL || localhost:8787)
├── types.ts                  types (ChatSSEEvent etc.)
├── i18n/
│   ├── ru.ts                 ~110 UI string keys
│   └── index.ts              export const t = ru
├── lib/
│   └── storage.ts            readLS/writeLS (localStorage)
├── hooks/
│   ├── useChatStream.ts      ⭐ chat SSE stream (reader, chunk-safe parsing)
│   ├── useAsyncLoad.ts       generic loading/error/data
│   ├── useClickOutside.ts
│   ├── useProjectActivity.tsx  activity bus
│   └── useUnreadTracker.ts
├── components/
│   ├── ProjectView.tsx       project container (tabs left + chat right)
│   ├── ProjectTabBar.tsx · Sidebar.tsx (DnD)
│   ├── ChatTab parts:        ToolBlock · SessionSelector · SessionContextPanel
│   ├── FileExplorer.tsx      ⭐ shared for Files/GlobalFiles
│   ├── Modal.tsx · ConfirmModal.tsx · Toast.tsx
│   ├── ErrorBoundary.tsx     ⭐ wraps ProjectView + each tab
│   ├── PromptPicker · SkillPicker · UsageBadge · ProjectStructureCard
│   ├── EditableMarkdown · HealthDot · LoginScreen · Spinner
├── tabs/                     overview | claude-md | logs | board | files | memory | secrets | timeline
│   ├── ChatTab.tsx           core + useChatStream
│   ├── BoardTab.tsx          kanban (isActive-guard on polling)
│   ├── FilesTab / GlobalFilesTab  (thin wrappers over FileExplorer)
│   ├── OverviewTab · LogsTab · MemoryTab · ClaudeMdTab · SecretsTab
│   └── TimelineTab.tsx       ⭐ Spec 008: history from GET /timeline + live via useProjectActivity (SSE reuse)
└── styles/                   ⭐ styles.css split into 11 partials
    ├── base.css (vars/theme/light) · layout · sidebar · tabbar · overview
    └── board · chat · files · modal · forms · timeline
        (root styles.css = 11 @import in cascade order)
```

---

## Timeline — event bus persistence (Spec 008)

Every event from `_bus_publish(session_key, event)` is additionally written to the project's JSONL log.

**Architecture:**
- Single write point — hook in `_bus_publish` calls `_timeline_append(session_key, event)`.
- `_timeline_init(ctx)` — called from `start()`, stores the `DATA/timeline/` path and a reference to `ctx["topics"]` in module-level variables `_TIMELINE_DATA_DIR` / `_TIMELINE_TOPICS`.
- `_timeline_path(session_key)` — resolves `session_key → cwd` via `_TIMELINE_TOPICS`, builds path `DATA/timeline/<slug>.jsonl`. If session_key not found — `_unknown.jsonl`.
- `_timeline_slug_from_cwd(cwd)` — `cwd.replace('/', '-')`, analogous to `_sdk_sessions_dir`.
- `_timeline_append(session_key, event)` — adds `ts=time.time()`, truncates `text` >2000 chars, excludes the `env` field (always), rotates >5MB → `.jsonl.1`. Swallows ALL exceptions.
- `_timeline_read_events(session_key, limit, before)` — reads `.jsonl` + `.jsonl.1`, parses gracefully (broken lines → skip), sorts by ts, paginates.

**Security:** the `env` field is excluded in `_timeline_append` — project secrets never reach the log. Verified by test `test_api_timeline_env_not_in_response`.

**Frontend:** `TimelineTab.tsx` — history via `GET /api/projects/{id}/timeline`, live events via `useProjectActivity` (reuses the existing SSE connection, no new socket opened).

---

## Secrets flow (Spec 007)

Project secrets are injected into the agent's env on every `run_engine` call:
- **TG channel** (`bot.py:run_agent`): `{**_secrets_read(cwd), "TG_CHAT_ID":..., "TG_THREAD_ID":...}` — TG variables always override secrets with the same names (take priority).
- **Cockpit chat** (`webapp.py:api_project_chat`): `env=_secrets_read(cwd)`.
- **Cards** (`webapp.py:_run_card`): `env=_secrets_read(cwd)` from the main project cwd (not worktree).
- Agent sees secrets as `os.environ["MY_KEY"]` — standard environment variables.
- Secrets are NOT logged in `audit()` — that function only accepts (project, kind, text). env is never passed there.
- Secrets do NOT reach transcripts/sessions/sidecars — they live in the process env, not in text.

---

## Tests: `tests/` (21 files, 496 passed / 6 skipped)

`venv/bin/python -m pytest -q` (or `make test`). Fixtures — `conftest.py` (aiohttp client, tmp-cwd, mock ctx, `_auth_token`).
- **Critical:** `test_board_parser` (regression = lost tasks in production), `test_security` + `test_security_regressions` (path-traversal, card_id, rate-limit), `test_board_api`, `test_run_card`, `test_chat_sse`, `test_project_rename`, `test_ingest_errors`.
- **New (Spec 007):** `test_secrets` — 47 tests: path, round-trip, chmod 600, gitignore, key validation, limits, cwd isolation, audit non-leak, API GET/POST/DELETE with critical value non-leak test.
- **New (Spec 008):** `test_timeline` — 32 tests: slug stability, path resolve, append+ts+truncate+env-exclusion, 5MB rotation, bus_publish integration, graceful broken lines, backup .jsonl.1, API GET/limit/before/env-not-in-response.
- **New (Spec 010):** `test_self_healing` — 28 tests: `_self_heal_enabled` (flag/env/default False); heal_attempted meta; OFF default = critical regression guard; heal_attempted set BEFORE run; safe→Review, risky→Failed; heal_attempted incident not restarted; non-git→skip; busy→skip; concurrency limit; Timeline self_heal; API toggle (auth/enable/disable/404).

---

## Data and operations

- `data/topics.json` (LAYER 1, permanent; per-project settings: model/self_heal/notify_on_error/log_cmd/test_cmd/git_enabled) · `data/sessions.json` (LAYER 2, `/reset` clears) · `data/settings.json` (global settings f2ba02, mtime hot-reload) · `data/prompts.json` · `data/runs/<card>.md` (sidecars) · `data/audit/` · `data/inbox/` (files from TG) · `data/timeline/<slug>.jsonl` (Timeline Spec 008). **`data/` is in .gitignore.**
- `.env` (secrets, not in git) · `.env.example` + `web/.env.example` (placeholders).
- `claude-ops-bot.service` (systemd) · **`restart-self.sh`** (THE ONLY way to restart from inside the agent — detached via systemd-run; details in CLAUDE.md).
- `TASKS.md` (board, sessions read this) · `DONE.md` (archive, sessions do NOT read this) · `docs/API.md` · `CONTRIBUTING.md` · `LICENSE` (MIT).

---

## Self-healing loop (Spec 010)

Connects existing building blocks into an autonomous cycle. **Agent prepares — human applies.**

```
scanner catches failure → creates err-card (already worked)
  → [NEW] _self_heal_enabled(project)? → yes
  → asyncio.create_task(_self_heal_card(ctx, project, card))
      1. Write heal_attempted=true to description BEFORE run (prevents loops)
      2. Build heal_prompt from title + incident excerpt
      3. _card_run_mode → worktree? → no → skip (safety guard #5)
      4. _card_worktree_setup → .worktrees/card-<id>
      5. ctx["running"][session_key] = True (blocks TG from parallel run)
      6. Move card to In Progress
      7. _run_card(..., worktree, wt_info) → agent fixes, auto-commit
         → _run_card releases running in finally, moves to Review/Failed
      8. _run_quality_gate(wt_path) → verdict safe/risky/unknown
      9. safe → stays in Review + heal_badge ✓; risky → Failed + heal_badge ✗
     10. Timeline kind:"self_heal" phase:start/fixed/gate_ok/gate_fail
     11. TG ping to operator (result)
```

**Safety guards (never bypass):**
1. `_self_heal_enabled` = False by default — only `self_heal: true` in topics or `SELF_HEAL_ENABLED=1`
2. `api_card_apply` is NEVER called from self-healing — agent only reaches Review
3. `heal_attempted=true` written BEFORE agent starts — a crash won't loop
4. `_self_heal_active_count <= _SELF_HEAL_MAX_CONCURRENT (2)` — global counter
5. `_card_run_mode == "worktree"` required — non-git/dirty skipped
6. Full observability — Timeline `kind:"self_heal"` + TG ping

**Key functions (webapp.py):**
- `_self_heal_enabled(project)` — reads flag; False by default
- `_send_tg_ping(ctx, project, msg)` — TG notification to operator
- `_self_heal_card(ctx, project, incident_card)` — async repair loop
- `_error_scanner_loop` — integration: after scan_and_ingest → create_task if enabled
- `api_project_self_heal_toggle` — POST `/api/projects/{id}/self-heal {enabled}`

---

## Single task flow (end-to-end)

```
TG message / card→In Progress / web chat
  → reserve running[cwd] (synchronously)
  → (card C2) mode detector: git+clean → worktree, else legacy
  → (worktree) git worktree add .worktrees/card-<id> -b card-<id>
  → run_engine(cwd=effective_cwd) drives SDK, yields events
  → adapter renders (TG: send+md_to_html / web: SSE / card: sidecar)
  → (worktree) auto-commit on branch card-<id>, diff vs base_branch
  → session_id saved, running released in finally
  → (card) → Review/Failed + TG ping
  → (C2-gate) user in Review sees diff + buttons:
      🧪 Check  → POST /check → _run_quality_gate(wt_path) → verdict safe/risky/unknown
                  (tests run IN the worktree; apply is NOT blocked — user decides)
      ✓ Apply   → git merge --no-ff card-<id> → Done (worktree removed)
      ✗ Discard → worktree+branch removed → Backlog
      Conflict  → 409, merge --abort, worktree intact, card stays in Review
```

### C2-gate: files
- `data/runs/<card_id>.md` — human-readable sidecar (agent response, diff)
- `data/runs/<card_id>.json` — machine-readable metadata (mode, branch, wt_path, has_changes, applied, discarded, gate:{verdict,ts})

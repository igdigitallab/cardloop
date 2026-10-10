> API = HTTP route reference. Code map → ARCHITECTURE.md. Working rules → CLAUDE.md. Running → CONTRIBUTING.md.

# Cardloop HTTP API Reference

> This guide covers the main flows by hand. **The complete route index — every method and path the
> server registers, with its auth requirement and handler — is [API-routes.md](API-routes.md)**,
> generated from the live router (`venv/bin/python tools/gen_route_index.py`) and checked by a test,
> so it cannot drift.

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

## Projects

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
| `POST` | `/api/projects/{id}/rename` | Rename project folder: `{"slug":"new-name"}` (kebab-case, `^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$`); 409 if busy or folder exists | Yes |
| `GET` | `/api/projects/{id}/health` | Structural health check (6 points: CLAUDE.md, cockpit rules, TASKS.md preamble, README, .gitignore/.env, .git) — `{color:"green\|yellow\|red", checks:[...]}` | Yes |
| `POST` | `/api/projects/{id}/audit` | Spawn audit card in In Progress; agent walks `templates/reference/audit-prompt.md` and creates issue cards | Yes |
| `POST` | `/api/projects/{id}/upgrade` | Spawn upgrade card: supplements existing CLAUDE.md / TASKS.md / README / .gitignore from templates without overwriting | Yes |

---

## Board / Tasks (Kanban)

Source of truth: `TASKS.md` in the project root. Sections `## Backlog / In Progress / Review / Failed` are columns; cards are markdown list items `- [x] text <!--ops:ID-->`.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/tasks` | Parse `TASKS.md` → return all cards grouped by column | Yes |
| `POST` | `/api/projects/{id}/tasks` | Create new card in Backlog — `{"text":"...","provider":"claude\|codex\|grok"?,"model":"..."?}`. `provider:"grok"` needs no per-project flag (see [Grok](#grok-spec-095)) | Yes |
| `GET` | `/api/projects/{id}/tasks/done` | Read archived cards from `DONE.md` | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/move` | Move card to another column — `{"to":"Backlog\|In Progress\|Review\|Failed\|done"}`. Moving to **In Progress** auto-starts `run_engine`; moving to `done` archives to `DONE.md`. A Grok card whose project is not opted in is refused (`409`) before it moves; if the flag is revoked later the run ends in **Failed** with the reason in the sidecar — never on another provider | Yes |
| `PATCH` | `/api/projects/{id}/tasks/{card}` | Edit card text and optional provider/model override. Run precedence: card provider → project `board_provider` → Claude. Choosing Grok needs no per-project flag | Yes |
| `DELETE` | `/api/projects/{id}/tasks/{card}` | Delete card from `TASKS.md` | Yes |
| `GET` | `/api/projects/{id}/tasks/{card}/run` | Get sidecar result of a card auto-run from `data/runs/<card>.md`. Also returns `meta` field (mode, has_changes, applied, discarded) from JSON sidecar | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/apply` | **C2-gate**: merge worktree branch `card-<id>` into base branch via `git merge --no-ff`. Moves card Review→Done. 400 if legacy/no meta; 409 if merge conflict (abort is automatic, worktree stays). Requires worktree mode | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/discard` | **C2-gate**: discard worktree changes — removes worktree + branch `card-<id>`. Moves card Review→Backlog. 400 if legacy/no meta | Yes |
| `POST` | `/api/projects/{id}/tasks/{card}/check` | **Spec 009 quality gate**: run tests in worktree (`_detect_test_cmd` auto-detect) and return verdict. Response: `{verdict:"safe\|risky\|unknown", tests:{detected,ok,cmd,exit_code,output,timed_out}, lint:null}`. Legacy/no-worktree → `{verdict:"unknown",reason:"legacy"}`. Result saved to `meta.gate={verdict,ts}`. 400 if bad card_id; 404 if project or worktree not found. Timeout: 300s. Secrets injected from `.claude-ops/secrets/secrets.env`. Does NOT block apply — user decides | Yes |

---

## Chat / SSE (Chat & Streaming)

Chats are provider-pinned at creation. Missing `provider` in legacy records means `claude`.
Claude continuity is stored in `session_id`; Codex continuity is stored separately in
`codex_thread_id`; Grok's in `grok_session_id` (always present on a chat, `null` when unset).
`CODEX_ENABLED=false` / `GROK_ENABLED=false` preserve those records but reject their runs
(`provider_status:"unavailable"`, never a fallback to Claude). Grok is documented in its own
[section](#grok-spec-095) below.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/projects/{id}/chat` | Start agent task — returns `text/event-stream` SSE stream of `{type:"tool\|text\|result\|error", ...}`. Shared session + lock with board auto-runs. 409 if project is busy, or if the chat's provider is refused ("temporarily unavailable", plan/ask mode on a provider without it). The final `result` frame carries `provider`, the three continuity ids (`session_id`, `codex_thread_id`, `grok_session_id`) and `context_tokens` / `context_window` | Yes |
| `POST` | `/api/projects/{id}/chat/stop` | Interrupt the current agent run (`client.interrupt()`). Note: server-side generator runs to completion; only client fetch is disconnected | Yes |
| `POST` | `/api/projects/{id}/agents/stop` | spec-089 §1: stop every running Workflow/sub-agent monitor row. Flips targets to `stopping` immediately, then steers (or queues) a synthetic instruction asking the model to call `TaskStop` for each — there is no server-side kill for a sub-agent task. A row still `stopping` after 60s is flipped to `stopped` without waking the orchestrator. No running rows → no-op. Returns `{ok, stopped:[ids], via:"steer"\|"queue"\|"none"}` | Yes |
| `GET` | `/api/projects/{id}/monitors/{mid}/tail?n=20` | spec-089 §7: last `n` (clamped 1-200, default 20) steps of one monitor row, for the panel's click-to-expand transcript peek. Agent rows tail the SDK transcript; Workflow rows summarise `journal.jsonl` instead (no transcript of their own). Returns `{kind, status, path, lines:[...]}`. 404 `{"error":"project not found"}` / `{"error":"monitor not found"}` / `{"error":"no transcript"}` (stream-only row, nothing ever written) | Yes |
| `POST` | `/api/projects/{id}/chat/steer` | spec-086: inject `{text, chat_id?}` into the RUNNING turn (CLI steering, like typing mid-turn in the terminal). Returns `{steered:true}`, or falls back to the chat queue → `201 {steered:false, item}` when the turn is not steerable (plan/ask gate, codex, rotation, no live client). spec-089 §5: `{urgent:true}` takes a third path for local CLI commands that only take effect at a turn boundary (`/goal`, `/clear`, `/compact`, `/model`, `/effort`, `/mcp`) — interrupts the running turn (same as `/chat/stop`, recorded as the same `operator_stop` timeline event) and enqueues at the HEAD of the chat queue instead of the tail. Always `201 {steered:false, urgent:true, interrupted:<bool>, item}`; `interrupted:false` when the session was already idle (nothing to interrupt, item still queued at head) | Yes |
| `GET` | `/api/projects/{id}/activity-stream` | SSE stream of board bus events for this project (`run_start / tool / text / run_end`), heartbeat 25s | Yes |
| `GET` | `/api/activity-stream` | SSE stream of ALL projects' bus events (for unread indicators in sidebar) | Yes |
| `GET` | `/api/agent-providers` | Provider availability, subscription auth status, discovered models, reasoning levels, and capabilities. The Grok row is listed **only while `GROK_ENABLED=true`** (a default install's payload is unchanged); its extras are `version`, `warnings[]` and `sandbox:{profile, deny_count, bwrap, probe}`, `plan_type` is the subscription tier. While the sandbox probe runs the row is `available:false` with an explanatory `error` (the call waits at most 4 s, never fails) | Yes |
| `GET` | `/api/projects/{id}/chats` | List provider-pinned chats, including `provider`, `provider_status`, `model`, `session_id`, `codex_thread_id` and `grok_session_id` | Yes |
| `POST` | `/api/projects/{id}/chats` | Create chat — optional `{"name":"...","provider":"claude\|codex\|grok","model":"..."}`; defaults to Claude | Yes |
| `PATCH` | `/api/projects/{id}/chats/{chat_id}` | Rename or activate a chat, or switch its runtime — `{name?, active?, provider?, model?, backend?, account?, expected_revision?}`. The switch is validated against the RESULTING state and applied as a compare-and-swap on `runtime_revision`: `409` while a turn is in flight or on a stale revision, `400` for an invalid combination, and `409` when the resulting provider is Grok in a project that is not opted in (a model-only patch cannot keep a chat on a provider the project no longer allows; moving off Grok is always allowed) | Yes |
| `DELETE` | `/api/projects/{id}/chats/{chat_id}` | Delete a non-final chat; provider threads/sessions are not deleted | Yes |
| `POST` | `/api/projects/{id}/chats/{chat_id}/handoff` | spec-092 runtime handoff — `{messages:[{role,text,tools}], from_label, to_label, commit?, text?}`. `commit` false/absent previews `{handoff:{text, ...}}` and stores nothing; `commit:true` arms the (possibly edited) `text` on the chat as `runtime_handoff`, answered `{armed, handoff}`. When the chat being left is on **Grok** the server ignores the posted `messages`, re-reads the session file and builds from it: user rows not matching the send ledger are left out, the text carries `## Warning: N unverified user row(s) left out`, and the response `handoff.unverified` holds up to 5 previews (≤ 200 chars) for the operator only | Yes |

### Approval gates

A gated turn (plan mode, or ask mode's per-tool gate) parks a **decision** and waits for the
operator. Both kinds live in one store; `/plan/...` and `/decision/...` hit the same handlers.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/decision/{decision_id}` | Full decision record — `kind: "plan"\|"tool"`, `status`, and the payload (`plan_text` / `tool_name` + `tool_preview`). Alias: `/plan/{plan_id}` | Yes |
| `POST` | `/api/projects/{id}/decision/{decision_id}/decide` | Decide. `kind="plan"` → `{"decision":"approve\|reject","feedback?":""}`; `kind="tool"` → `{"decision":"allow\|allow_always\|deny","feedback?":""}`. `allow_always` adds the tool to that project's `ask_always_allow`. Idempotent — a second decide returns `{ok, noop:true}`. Alias: `/plan/{plan_id}/decide` | Yes |

### Grok (spec-095)

Off unless `GROK_ENABLED=true` (then the `/api/agent-providers` row exists and `grok` is a valid `provider` everywhere). Operator runbook → [GROK.md](GROK.md).

- **No privacy gate.** Choosing Grok is the consent, exactly as for Codex and Claude: chat create, free-chat create, runtime PATCH, queue accept, chat POST, card create/edit/move and settings `board_provider` accept `grok` in any project, a chat rooted at `$HOME` included. (The former `409 grok is not enabled for this project`, the `grok_allowed` field and `GROK_ALLOW_ALL_PROJECTS` were removed 2026-10-03; a `grok_allowed` key in a settings POST is now an unknown key, `400`.) A run that cannot start (disabled, not signed in, login names a different account than the pinned one, sandbox check failed) is an `error` event / Failed card, never a run on another provider.
- **Fields.** Projects (`GET /api/projects`, settings): `grok_model`. Chats and free chats: `grok_session_id`. Cards: `provider:"grok"`; `board_provider:"grok"`. The registry model list comes from `grok models`; `reasoning_levels` are `low|medium|high|xhigh`.
- **Capabilities.** `chat, board, history, search, usage, interrupt, multi_agent` are true; `ask_mode, plan_mode, skills, plugins` are `false`. Plan or ask mode on a Grok chat is the usual capability conflict (`409` on the chat POST, cleared loudly at a queue drain) — never a silent downgrade.
- **Ledger and `verified`.** Grok's session file is writable by its own model, so the cockpit records a SHA-256 of every prompt it sends into `<DATA>/grok_sent/<session-id>`; a history row is `verified:true` only if its text matches. Assistant rows carry no tag. A handoff out of a Grok chat leaves unverified user rows out (see the handoff row above).
- **Errors on the run path** arrive as SSE `error` events: `Grok sandbox check failed — refusing to run: …` (probe verdict `failed`; the registry row is `available:false`), `Grok sign-in expired — run tools/grok-acct login`, `refusing to run Grok in <dir>` (cwd is `$HOME` or above), `Grok does not support plan mode …`.

---

## File Explorer (Files tab, Server files)

One absolute-path view for the project Files tab and the Server-files tab. Policy lives in
`fs_browser.py`: reachable roots are `$HOME`, `FILES_EXTRA_ROOTS` (colon-separated, default
`/tmp`) and — with `?project=<id>` — that project's cwd. Everything is realpath'ed first;
top-level dot entries under `$HOME` are hidden (only the native agent memory
`~/.claude/projects/<slug>/memory/` is reachable), and `.env*`, key/credential file names and
`.git`/`venv`/`node_modules`/… are denied at any depth. Refusals are `403`, never a partial listing.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/fs/info` | `?project=<id>` optional → `{home, start, roots[]}`; `start` is the project cwd (or `$HOME`) | Yes |
| `GET` | `/api/fs/list` | `?path=<abs>` → `{path, parent, crumbs[], entries[], truncated}`; `parent` is `null` at the ceiling | Yes |
| `GET` | `/api/fs/stat` | `?path=<pasted text>&base=<abs>` → `{kind: dir\|file\|missing\|denied, path, nearest?}`; strips quotes/backticks/`file://`/`:line`, tries the text as typed first | Yes |
| `GET` | `/api/fs/file` | `?path=<abs>` → `{path, content, lang, size, rev, editable}`; max 1 MB; binary → `error` | Yes |
| `PUT` | `/api/fs/file` | `?path=<abs>`, body `{content, base_rev, force?}` → `{ok, rev, size}`. Existing UTF-8 text files only. `409` when `base_rev` no longer matches the disk (the agent rewrote it); `force` overwrites. Atomic; CRLF files stay CRLF | Yes |

| `GET` | `/api/fs/raw` | `?path=<abs>[&download=1]` → the file's bytes. Images, PDF, video and audio stream inline (Range-capable); everything else is a forced `attachment` as octet-stream. Images/media carry `Content-Security-Policy: sandbox` (an SVG opened by URL cannot run script on the cockpit origin), PDFs `X-Frame-Options: SAMEORIGIN`. Max 100 MB | Yes |
| `GET` | `/api/fs/recent` | `?project=<id>` → `{items[]}`: files the agent's Write/Edit calls touched (recorded by the engine, incl. reports dropped in `/tmp`) merged with files changed on disk in the project in the last 48 h; `src: agent\|disk` | Yes |

The cockpit's own `data/` is never browsable (the safe, the Web Push private key, the touched-file log…) except `data/inbox/` — the files uploaded into chats.

`rev` is an opaque **string** (`mtime_ns:size:inode`) — nanosecond mtimes exceed 2^53 and do not survive a JSON number.

### Legacy (still routed, no longer used by the UI)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/files` · `/file` | cwd-jailed listing / read, `?path=<rel>` | Yes |
| `GET` | `/api/global/files` · `/file` | `$HOME`-jailed listing / read | Yes |
| `POST` | `/api/global/file` | `$HOME`-jailed write (now refuses the sensitive dirs like its GET twin) | Yes |

---

## Prompt Library

Global prompt templates stored in `data/prompts.json` (not in git). Supports categories and `[VARIABLE]` placeholders.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/prompts` | List all prompts `[{id, title, category, text}, ...]` | Yes |
| `POST` | `/api/prompts` | Create prompt — `{"title":"...", "category":"...", "text":"..."}` | Yes |
| `PATCH` | `/api/prompts/{id}` | Update prompt fields | Yes |
| `DELETE` | `/api/prompts/{id}` | Delete prompt | Yes |

---

## Sessions and Codex threads

Claude history is read from SDK transcripts under `~/.claude`. For an active Codex chat,
the same endpoints use native Codex `thread_list`/`thread_read` data instead; for an active Grok
chat they read Grok's session files under its own home (no agent process, works with
`GROK_ENABLED=false`). The identifiers and histories are never mixed.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/sessions` | List SDK sessions for project — `[{id, preview, ts}, ...]`. Grok chat: `{sessions:[{session_id, grok_session_id, provider:"grok", last_used, label, preview, message_count, context_tokens:null, is_active}], provider:"grok"}` — `message_count` is approximate or `null`; a failed read is `{sessions:[], provider:"grok", error}` | Yes |
| `POST` | `/api/projects/{id}/sessions/{sid}/label` | Set human-readable label on a session | Yes |
| `POST` | `/api/projects/{id}/session` | Switch active session — `{"action":"new\|resume", "session_id":"..."}`. 409 if project is busy. On a Grok chat `new` clears `grok_session_id` and `resume` needs a real session of THIS project's directory (`400 invalid Grok session id` / `400 session not found`) | Yes |
| `GET` | `/api/projects/{id}/session-history` | Active provider history. Accepts `session_id` for Claude, `codex_thread_id` for Codex or `grok_session_id` for Grok (an explicit id wins over the chat's own; an explicit `codex_thread_id` beats an active Grok chat). Grok answer: `{messages, session_id:null, grok_session_id, provider:"grok", context_tokens, context_window}`; at most 100 rows, no timestamps; every `user` row carries `verified` (true only if the text matches a prompt this cockpit sent — see Grok below); no id → `messages:[]`, `grok_session_id:null`. `400 invalid grok_session_id`, `502 Grok history unavailable: …` | Yes |
| `GET` | `/api/search` | `?q=…&limit=30[&project=id]` — ranked hits across chat/timeline/board plus live Codex and Grok session hits. A Grok hit is `{project_id, project_name, source:"chat", provider:"grok", ts, snippet, ref:{grok_session_id, provider:"grok"}}`; Grok is scanned per project the gate allows, with a 2.5 s wall-clock budget, only while `GROK_ENABLED=true` | Yes |
| `GET` | `/api/projects/{id}/session-context` | Current session context summary (Feature A — context read) | Yes |

---

## Memory

Project memory lives in **`<cwd>/.claude-ops/memory/`** — committed to git, travels with the repo.
Response format for all endpoints: `{files:[{name, content}], exists}`. `MEMORY.md` is always first (index).
File names: `^[a-z0-9][a-z0-9-]{0,60}\.md$` or `MEMORY.md`. Max size per file: 256 KB.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/memory` | Read all memory files. Reads `.claude-ops/memory/`; fallback to old `~/.claude/projects/<cwd>/memory/` if new path absent. Returns `{files, exists}`. | Yes |
| `POST` | `/api/projects/{id}/memory/{name}` | Create or update a memory entry. Body: `{"content":"..."}`. Validates slug, checks size limit, atomic write, auto-reindexes `MEMORY.md`. Returns updated `{files, exists}`. Errors: 400 bad name/size, 404 project not found. | Yes |
| `DELETE` | `/api/projects/{id}/memory/{name}` | Delete a memory entry. Auto-reindexes `MEMORY.md`. Returns updated `{files, exists}`. Cannot delete `MEMORY.md` directly (400). 404 if entry not found. | Yes |

---

## Policy rules

Declarative Markdown rules (`block` / `warn`) evaluated before every tool call of a Claude run. Files are edited as files (Files tab); the API is read-only. Format, tiers, trust rule, limits → [RULES.md](RULES.md).

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/rules` | One row per rule file across the project / global / pack tiers: `{rules:[{name, tier, path, enabled, trusted, status, action, event, match, message, hits, last_hit, diagnostics:[...]}], diagnostics:[...], trust_tracked, global_dir, project_dir, limits}`. `status` = `active\|disabled\|untrusted\|shadowed\|invalid`; `trusted:false` = a project file that git tracks while `rules_trust_tracked` is off (not read, not enforced); `hits`/`last_hit` count in memory since the cockpit started; top-level `diagnostics` = directory-level problems (symlinked dir, git could not answer). 404 if the project is unknown. | Yes |

---

## Project Secrets (Spec 007)

Project-scoped secrets stored in `<cwd>/.claude-ops/secrets/secrets.env` (chmod 600, gitignored).
**Security**: values are NEVER returned by the API — only key names. Values are injected into the agent process env at runtime.
Key format: `^[A-Z_][A-Z0-9_]*$`. Max value size: 8 KB. Max keys: 100.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/secrets` | List secret key **names** (not values!). Returns `{keys:["KEY1","KEY2",...], exists:bool}`. | Yes |
| `POST` | `/api/projects/{id}/secrets/{key}` | Set a secret — body `{"value":"..."}`. Validates key format, checks limits. Returns updated key list (no values). 400 bad key or limits exceeded; 404 project not found. | Yes |
| `DELETE` | `/api/projects/{id}/secrets/{key}` | Delete a secret key. Returns updated key list. 404 if key/project not found; 400 invalid key. | Yes |

---

## Timeline — Event Feed (Spec 008)

Persistent event log for a project. Every event published via `_bus_publish` is appended to `data/timeline/<slug>.jsonl`.
Rotation: when file exceeds **5 MB** it is renamed to `.jsonl.1` (single backup, overwrites previous). Reading merges both files.
Event schema: `{ts, session_key, kind, source?, run_id?, prompt?, text?, tool?, outcome?}`. **env field is never written.**

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/timeline` | Chronological list of events (newest at bottom). Query params: `limit` (default 200, max 500), `before=<ts>` (Unix float, for pagination — return events with ts < before). Returns `{events:[...]}`. 404 if project not found. | Yes |

---

## Settings (card f2ba02)

Global — `data/settings.json` (mtime hot-reload, wired into runtime: scan interval, default model, watchdog). Per-project — fields in `topics.json` (`git_enabled`, `model`, `notify_on_error`, `log_cmd`, `test_cmd`, `board_provider`, `codex_model`, `grok_model`). `git_enabled=false` → cockpit does not use git (legacy cards, git-sync returns 409, health does not require .git).

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/settings` | Global settings: `{stored, effective, spec}`. `effective` = active values (override or env default), `spec` = types/ranges. | Yes |
| `POST` | `/api/settings` | Partial update of global settings (validated against spec). `null`/`""` for a key resets it to default. 400 on unknown key/type/range. | Yes |
| `GET` | `/api/projects/{id}/settings` | Per-project settings: `{git_enabled, model, notify_on_error, log_cmd, test_cmd, board_provider, codex_model, grok_model, ...}`. `grok_model` falls back to the built-in default (`GROK_MODEL`, `grok-4.7`) | Yes |
| `POST` | `/api/projects/{id}/settings` | Partial update of per-project settings (writes to topics.json for all entries with this cwd). Type/model validation. Returns `{ok, topics_updated, settings}`. 400 on unknown key/type. `grok_model` takes a provider-native id (`[A-Za-z0-9._-]{2,100}`). `rules_trust_tracked` is a strict bool (`false` resets it; 400 for a free chat): the opt-in to git-tracked policy rule files, see [RULES.md](RULES.md). | Yes |

---

## Usage

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/usage/dashboard` | Token/turn dashboard (`?days=30\|all`, `?models=`). `providers.claude` / `providers.codex` as before, and **`providers.grok` only while `GROK_ENABLED=true`**: `{turns, input, output, cached, reasoning, notional_usd, by_model:{<model>:{turns,input,output}}, limits:null, local_counters:{five_hour:{turns,tokens}, seven_day:{turns,tokens}}, last_limit_error:{ts,text}\|null}`. `input` already INCLUDES `cached`; `notional_usd` is the API-list-price equivalent of the tokens, never spend (no cost key exists in the block); `limits` is always `null` because Grok reports none; `by_model` is a record (Codex's is an array). Built from the engine's own ledger `<DATA>/grok_usage.jsonl` | Yes |
| `GET` | `/api/usage` | Subscription usage — 5h and 7-day limits with utilisation 0–1 and `resets_at`. Source: `GET https://api.anthropic.com/api/oauth/usage` (cached 60s). Falls back to passive `RateLimitEvent` snapshot if oauth endpoint fails. Also returns `account` (active account id) and, once a second account is registered, `accounts[]` with each one's own limits (`null` when that account's token is stale) | Yes |

---

## Load monitor (spec-094)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/system-load` | How loaded this host is, judged against its OWN limits. Returns `{level: ok\|warn\|crit\|unknown, score 0-100, at, age_s, chats:{live,max}, signals:[{id, level, pressure 0-1, value, text, hint}], top:[{kind, project, rss_mb}], host:{os, cpus, mem_gb}}`. Sizes are binary (`mem_gb` is GiB, `rss_mb` is MiB). The `disk` signal also carries a runway (`89% · 3.7d`, text `…filling 6.0 GiB/day over the last 3 d — full in ~3.7 days`) once a day of history exists (`data/load_disk_history.json`). `age_s` is the sampler's age on the SERVER clock. Process detail is redacted to project names — no command lines or paths. Before the first sample (`warming_up: true`) the level is `unknown`. 404 when the module is off (`LOAD_MONITOR=0`). | Yes |

---

## Project health check (feature `project_health`)

Read-only checks for real, actionable ailments — never a score; a healthy project returns `findings: []`
and the UI shows nothing. Checks: `memory_index_near_cap`, `context_floor`, `no_test_cmd`, `stale_work`,
`orphan_worktrees`, `env_exposed`, `project_settings_untrusted`, `invisible_unicode`. Software-only checks
(`no_test_cmd`, `stale_work`, `env_exposed`) skip `content` / `scratchpad` projects. Separate from the
Tests verdict: `POST /api/projects/{id}/test` is unchanged. 404 on all three routes when the module is off.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/projects/{id}/health-check` | Findings for one project: `{project_id, name, findings:[{id, severity:"warn"\|"crit", title, detail, fix_hint, subject, ackable?, ack_sha256?}], checked_at, took_ms, errors, skipped}`. Cached ~5 min; `?fresh=1` re-runs (the Tests button and the modal's Re-check use it). One project finishes in under 3 s; checks past the budget land in `skipped`, a crashing check in `errors` — never in `findings`. Free chats always return `[]` | Yes |
| `POST` | `/api/projects/{id}/health-check/ack` | `{"check_id":"project_settings_untrusted","sha256":"<64 hex>"}` — stores the settings file's hash in `data/project_health_ack.json`; the finding stays silent until the file's hash changes. 400 on a bad body or a check that cannot be acknowledged; 409 (with the fresh result) when the hash is not one of the files as they are now. Returns `{ok:true, ...fresh result}` | Yes |
| `GET` | `/api/health-check` | Fleet view of the last daily sweep: `{mode, interval_sec, last_sweep_at, projects:[<per-project result, only those with findings>], counts:{projects, crit, warn}}` | Yes |

The sweep (`HEALTH_CHECK_MODE=on`, every `HEALTH_CHECK_INTERVAL_SEC`) writes ONE digest `data/inbox/project-health-<day>.md`
and pushes at most once a day, only for findings it has not announced before (`data/project_health_state.json`).
Knobs: `HEALTH_CHECK_MODE`, `HEALTH_CHECK_INTERVAL_SEC`, `HEALTH_CONTEXT_FLOOR_WARN_TOKENS`, `HEALTH_STALE_WORK_DAYS` (see `.env.example`).

---

## Accounts (multiple subscriptions)

An extra Claude subscription is a separate `CLAUDE_CONFIG_DIR` under `~/.claude-accounts/<id>/`
holding only its own `.credentials.json`; `projects/`, `skills/`, `hooks/`, `settings.json` … are
symlinked back to `~/.claude` so history and session resume stay shared. `main` is virtual and
injects no env at all. Login happens in a terminal: `tools/claude-acct login <id>`.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/accounts` | All selectable accounts: `{id,label,is_main,active,ok,reason,email,plan,shared_ok,shared_broken}` + `active`, `accounts_root`, `login_hint` | Yes |
| `POST` | `/api/accounts` | `{id,label?}` → scaffold `~/.claude-accounts/<id>` with shared symlinks and register it. Returns `linked[]` and `next_step` (the login command). Not usable until logged in | Yes |
| `POST` | `/api/accounts/active` | `{id}` → every SUBSEQUENT run uses that subscription. `400` + reason if the account has no readable credentials. Response `in_flight` = runs still executing on the previous account | Yes |
| `POST` | `/api/accounts/remove` | `{id}` → forget the account (files on disk are left untouched); active falls back to `main` | Yes |

Per-project pinning rides on the ordinary project settings: `POST /api/projects/{id}/settings`
with `{"account": "<id>"}` pins that project (chat, board cards and deferred runs) to one
subscription; `""`/`null` clears it back to "inherit the global choice". An unknown or
not-logged-in id is rejected with `400`. Resolution order at run time is
project override → global active → `main`, and a broken override degrades instead of failing
the run.

---

## Free Chats

Free-form chats not tied to a project (`cwd=$HOME`). Shown in tab bar, hidden from sidebar.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/free` | Create a free chat — optional `provider`, provider-native `model` and `cwd` (default `$HOME`); returns every continuity-id field | Yes |
| `POST` | `/api/free/{id}/rename` | Rename free chat — `{"label":"..."}` | Yes |
| `DELETE` | `/api/free/{id}` | Delete free chat | Yes |

---

## Schedules Registry (Spec 019)

Global view of all scheduled tasks on the server (cron, systemd timers, Claude jobs,
Coolify, n8n, in-process).  The registry is **read-only** from the API layer.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/schedules` | Returns full normalised schedule registry. Query params: `?project=<id>` (filter by project), `?status=broken,stale` (comma-separated), `?source=cron,systemd` (comma-separated). Response: `{scanned_at, source_statuses, records[]}` | Yes |
| `POST` | `/api/schedules/scan` | Trigger immediate background re-scan. Response: `{queued: true}` | Yes |
| `POST` | `/api/schedules/{id}/investigate` | Create a Backlog investigation card for the schedule entry. Response: `{card_id: "..."}` | Yes |

### Record schema

```json
{
  "id": "<stable 12-char hex>",
  "source": "cron|systemd|claude_jobs|coolify|n8n|in_process",
  "schedule": "0 4 * * *",
  "command": "bash ~/scripts/backup.sh >> ~/logs/backup.log 2>&1",
  "project": "networking-os",
  "last_run": "2026-06-10T04:00:01+00:00",
  "next_run": "2026-06-11T04:00:00+00:00",
  "status": "ok|stale|broken|unknown",
  "purpose": "Daily backup of Docker volumes to NAS",
  "annotations": {}
}
```

Cache file: `data/schedules_cache.json` (gitignored). Annotations overlay: `data/schedules_annotations.json`.
Scan interval: env `SCHEDULES_SCAN_INTERVAL` (default 300s).

---

---

## Deferred Runs (Spec 020)

Queue a prompt to execute at a specific time or after the 5-hour rate-limit window resets.
Records are persisted in `data/deferred.json` (survives restarts).

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/deferred` | Create a deferred run. Body: `{project, prompt, fire_at?, fire_on_reset?}`. Exactly one of `fire_at` (ISO-8601 UTC string) or `fire_on_reset: true` required. Returns `{id, status}` with HTTP 201. | Yes |
| `GET` | `/api/deferred` | List all deferred records. Query params: `?status=pending` (filter by status), `?project=<name>` (filter by project). | Yes |
| `DELETE` | `/api/deferred/{id}` | Cancel a pending deferred run. Returns `{cancelled: true}` (200) or 404 (not found) or 409 (already fired/failed). | Yes |

### Deferred record schema

```json
{
  "id": "def-a3f7c2b1",
  "project": "networking-os",
  "session_key": "chat:thread",
  "prompt": "Run a full audit…",
  "fire_at": "2026-06-12T09:00:00Z",
  "fire_on_reset": false,
  "created": "2026-06-11T14:00:00Z",
  "status": "pending|fired|cancelled|failed",
  "fired_at": null,
  "error": null,
  "attempts": 0
}
```

**Status lifecycle:** `pending` → `fired` (started) or `cancelled` (via DELETE) or `failed` (project busy after max attempts or execution error).

**fire_on_reset behaviour:** At poll time the `five_hour` limit is checked via the OAuth usage cache.
- If `utilization < DEFERRED_FREE_THRESHOLD` (default 0.10): fires immediately.
- Otherwise: fires when `time.time() >= resets_at + jitter` (jitter 30–90 s, stable per record).

**Busy re-queue:** If the project slot is busy at fire time, `fire_at` is pushed forward 5 minutes and `attempts` is incremented. After `DEFERRED_MAX_ATTEMPTS` (default 5) the record is marked `failed` and surfaced in the cockpit.

**Env vars:** `DEFERRED_POLL_SEC` (default 30), `DEFERRED_MAX_ATTEMPTS` (default 5), `DEFERRED_FREE_THRESHOLD` (default 0.10).

**Schedules integration:** Pending deferred records appear in `GET /api/schedules` with `source: "deferred"`.
Cancel via `DELETE /api/deferred/{id}`.

---

## SPA Fallback

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `*` | `/{path:.*}` | Serve `web/dist/index.html` for all non-API routes (React SPA) | No |

---

## Summary

Total registered routes: **66** API routes + 1 SPA catch-all (67 total).

Public (no cookie): `GET /api/health`, `POST /api/login`.
All other `/api/*` routes require a valid `cops_auth` session cookie.

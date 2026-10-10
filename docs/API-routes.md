<!-- GENERATED FILE - do not edit by hand. -->
<!-- Regenerate: venv/bin/python tools/gen_route_index.py -->

> Complete HTTP route index, generated from the live aiohttp router. Guide to the main flows → [API.md](API.md).

# Cardloop HTTP route index

**This file is generated** by `tools/gen_route_index.py` from the routes the cockpit actually registers; `tests/test_route_index.py` fails when it is stale. After adding, removing or renaming a route, regenerate it:

```bash
venv/bin/python tools/gen_route_index.py
```

197 routes on 169 paths (192 need the session cookie, 5 do not). Methods and paths are exact; the description is the first line of the handler's docstring, or the matching [API.md](API.md) row when the handler has no docstring (a dash means neither has one). Request and response shapes for the main flows are in [API.md](API.md); for the rest, the handler named in the table is the reference.

## Authentication

The `Auth` column is computed by running every route through the real `auth_middleware` with a cookie-less request. `cookie` means the request is answered `401` unless it carries a valid `cops_auth` cookie (obtained from `POST /api/login`); `none` means the middleware lets it through. The middleware's own contract:

```text
Guards /api/* — passes /api/health, /api/login and /api/build without a cookie.
Spec-012 Ph3: also passes POST /api/projects/{id}/incident (it has its own
token-auth in the body/header). Match is TIGHT via pre-compiled _INCIDENT_PATH_RE —
endpoints without trailing id, /incident/evil, or GET will not be exempt.
```

Answered without the cookie: `GET /api/build`, `GET /api/health`, `POST /api/login`, `POST /api/projects/{id}/incident`, `GET /dl/{name}`. Every other route in this table returns `401` without it.

## Feature routes

Routes marked `feature: <name>` belong to an optional feature package under `features/` and exist only while that feature is enabled (its `register()` gate: Modules panel or env flag). Core routes are always present.

## Routes

| Method | Path | Auth | Handler | Description |
|--------|------|------|---------|-------------|
| `GET` | `/api/accounts` | cookie | `api_accounts` | All selectable accounts: `{id,label,is_main,active,ok,reason,email,plan,shared_ok,shared_broken}` + `active`, `accounts_root`, `login_hint` |
| `POST` | `/api/accounts` | cookie | `api_accounts_create` | `{id,label?}` → scaffold `~/.claude-accounts/<id>` with shared symlinks and register it. Returns `linked[]` and `next_step` (the login command). Not… |
| `POST` | `/api/accounts/active` | cookie | `api_accounts_activate` | `{id}` → every SUBSEQUENT run uses that subscription. `400` + reason if the account has no readable credentials. Response `in_flight` = runs still ex… |
| `POST` | `/api/accounts/remove` | cookie | `api_accounts_remove` | `{id}` → forget the account (files on disk are left untouched); active falls back to `main` |
| `GET` | `/api/activity-stream` | cookie | `api_activity_stream_all` | Unified stream of ALL bus events (unread indicators in sidebar). |
| `GET` | `/api/agent-providers` | cookie | `api_agent_providers` | Live provider/auth/model capabilities. |
| `DELETE` | `/api/auth/totp` | cookie | `api_totp_disable` | Disable TOTP (authenticated break-glass via cockpit). |
| `POST` | `/api/auth/totp/activate` | cookie | `api_totp_activate` | Confirm enrollment with a valid TOTP code. |
| `POST` | `/api/auth/totp/enroll` | cookie | `api_totp_enroll` | Begin TOTP enrollment. |
| `GET` | `/api/auth/totp/status` | cookie | `api_totp_status` | Report whether TOTP is currently active. |
| `GET` | `/api/autonomy` | cookie | `api_autonomy_get` (feature: board_janitor) | Is unattended activity currently allowed? |
| `POST` | `/api/autonomy` | cookie | `api_autonomy_set` (feature: board_janitor) | {"paused": bool} — the fleet-wide stop switch. |
| `GET` | `/api/autopilot/decisions` | cookie | `api_autopilot_decisions` (feature: autopilot) | Most-recent shadow decisions, newest first. |
| `POST` | `/api/autopilot/director/{id}` | cookie | `api_autopilot_director` (feature: autopilot) | Manually trigger a director plan run. |
| `POST` | `/api/autopilot/global` | cookie | `api_autopilot_global` (feature: autopilot) | {enabled: bool} — flip global_enabled flag. |
| `POST` | `/api/autopilot/pause` | cookie | `api_autopilot_pause` (feature: autopilot) | Set paused=True. |
| `POST` | `/api/autopilot/resume` | cookie | `api_autopilot_resume` (feature: autopilot) | Set paused=False. |
| `GET` | `/api/autopilot/status` | cookie | `api_autopilot_status` (feature: autopilot) | Global autopilot state + per-project modes. |
| `POST` | `/api/autopilot/tick` | cookie | `api_autopilot_tick` (feature: autopilot) | Run one shadow tick immediately (manual trigger). |
| `GET` | `/api/board/janitor` | cookie | `api_janitor_status` (feature: board_janitor) | Current policy plus the latest digest, if any. |
| `POST` | `/api/board/janitor/run` | cookie | `api_janitor_run` (feature: board_janitor) | Run one sweep now (operator-triggered). |
| `GET` | `/api/browser/backends` | cookie | `api_browser_backends` | Backend availability + current selection. |
| `GET` | `/api/browser/input-ws` | cookie | `api_browser_input_ws` | Input-ONLY channel. |
| `POST` | `/api/browser/install-cloak` | cookie | `api_browser_install_cloak` | Install the free CloakBrowser tier (detached). |
| `POST` | `/api/browser/manager-token` | cookie | `api_browser_manager_token` | {token} — store the Cloak Manager token in the safe. |
| `GET` | `/api/browser/profile-usage` | cookie | `api_browser_profile_usage` | Which Manager profiles this cockpit is |
| `GET` | `/api/browser/profiles` | cookie | `api_browser_profiles` | List Cloak Manager profiles (empty if unconfigured). |
| `POST` | `/api/browser/profiles/{id}/{action}` | cookie | `api_browser_profile_action` | Launch \| stop a Manager profile. |
| `GET` | `/api/browser/ws` | cookie | `api_browser_ws` | Live browser screencast over WebSocket. |
| `GET` | `/api/build` | none | `api_build` | (unauthenticated) — {"bundle": "index-&lt;hash&gt;.js"} of the served build. |
| `GET` | `/api/chat-trace` | cookie | `api_chat_trace` | The message-lifecycle log. |
| `GET` | `/api/deferred` | cookie | `api_deferred_list` | List deferred runs with optional filters. |
| `POST` | `/api/deferred` | cookie | `api_deferred_create` | Queue a deferred run. |
| `PATCH` | `/api/deferred/{id}` | cookie | `api_deferred_update` | Edit a pending deferred run (prompt and/or trigger). |
| `DELETE` | `/api/deferred/{id}` | cookie | `api_deferred_delete` | Cancel a pending deferred run. |
| `POST` | `/api/deferred/{id}/confirm` | cookie | `api_deferred_confirm` | Spec-051: resolve an awaiting_confirmation |
| `POST` | `/api/free` | cookie | `api_free_create` | Create a free chat — optional `provider`, provider-native `model` and `cwd` (default `$HOME`); returns every continuity-id field |
| `DELETE` | `/api/free/{id}` | cookie | `api_free_delete` | Delete free chat |
| `POST` | `/api/free/{id}/rename` | cookie | `api_free_rename` | Rename free chat — `{"label":"..."}` |
| `GET` | `/api/fs/file` | cookie | `api_fs_file` | Text content + revision. |
| `PUT` | `/api/fs/file` | cookie | `api_fs_file_write` | {content, base_rev, force?} — save a text file. |
| `GET` | `/api/fs/info` | cookie | `api_fs_info` | Where the explorer starts and what it may reach. |
| `GET` | `/api/fs/list` | cookie | `api_fs_list` | Directory listing. |
| `GET` | `/api/fs/raw` | cookie | `api_fs_raw` | The file's bytes. |
| `GET` | `/api/fs/recent` | cookie | `api_fs_recent` | Files the agent just wrote + files changed on disk. |
| `GET` | `/api/fs/stat` | cookie | `api_fs_stat` | Text&gt;[&base=&lt;abs&gt;][&project=&lt;id&gt;] — what did the operator paste? |
| `GET` | `/api/global/claude-md` | cookie | `api_global_claude_md` | Read the global (home) agent-rules CLAUDE.md. |
| `POST` | `/api/global/claude-md` | cookie | `api_global_claude_md_write` | Overwrite the global (home) agent-rules CLAUDE.md. |
| `GET` | `/api/global/file` | cookie | `api_global_file` | File contents from $HOME. |
| `POST` | `/api/global/file` | cookie | `api_global_file_write` | Write file contents. |
| `GET` | `/api/global/files` | cookie | `api_global_files` | Directory listing from $HOME. |
| `GET` | `/api/health` | none | `api_health` | (unauthenticated — see auth_middleware exempt list). |
| `POST` | `/api/login` | none | `api_login` | Authenticate with `{"password":"..."}`, sets `cops_auth` cookie |
| `POST` | `/api/logout` | cookie | `api_logout` | Clear `cops_auth` cookie |
| `GET` | `/api/me` | cookie | `api_me` | Current auth status |
| `GET` | `/api/models` | cookie | `api_models` | Live model registry (cached ~6h). Fully best-effort → static fallback on any error. |
| `GET` | `/api/modules` | cookie | `api_modules_list` | List all built-in modules with their enabled state. |
| `POST` | `/api/modules/{id}` | cookie | `api_modules_set` | Enable/disable a module and/or update its config. |
| `GET` | `/api/project-groups` | cookie | `api_project_groups_get` | — |
| `POST` | `/api/project-groups` | cookie | `api_project_groups_manage` | — |
| `POST` | `/api/project-groups/create` | cookie | `api_project_groups_create` | Body: {name} |
| `POST` | `/api/project-groups/delete` | cookie | `api_project_groups_delete` | Body: {name} |
| `POST` | `/api/project-groups/rename` | cookie | `api_project_groups_rename` | Body: {from, to} |
| `POST` | `/api/project-groups/reorder` | cookie | `api_project_groups_reorder` | Body: {order: [...]} |
| `GET` | `/api/projects` | cookie | `api_projects` | List all projects (from `data/topics.json`, deduped by cwd) |
| `GET` | `/api/projects/archived` | cookie | `api_projects_archived` | — |
| `POST` | `/api/projects/new` | cookie | `api_new_project` | Creates a new project folder with starter templates and |
| `GET` | `/api/projects/{id}/activity` | cookie | `api_project_activity` | Recent activity log for the project |
| `GET` | `/api/projects/{id}/activity-stream` | cookie | `api_project_activity_stream` | Bus event stream for a specific project. |
| `POST` | `/api/projects/{id}/agents/stop` | cookie | `api_project_agents_stop` | Spec-089 §1: stop every running Workflow/sub-agent. |
| `POST` | `/api/projects/{id}/archive` | cookie | `api_project_archive` | — |
| `POST` | `/api/projects/{id}/audit` | cookie | `api_project_audit` | Creates an audit card and launches it via run_engine. |
| `PUT` | `/api/projects/{id}/autopilot` | cookie | `api_autopilot_set_project_mode` (feature: autopilot) | {mode: "off"\|"propose"\|"auto"} |
| `POST` | `/api/projects/{id}/cards/accept-review` | cookie | `api_accept_review` (feature: board_janitor) | Archive Review cards in one go. |
| `POST` | `/api/projects/{id}/cards/run-batch` | cookie | `api_run_batch` | Queues multiple cards. |
| `GET` | `/api/projects/{id}/cards/{card}/spec` | cookie | `api_card_spec_get` | Read card spec sidecar. |
| `PUT` | `/api/projects/{id}/cards/{card}/spec` | cookie | `api_card_spec_put` | Write (or delete) card spec sidecar. |
| `POST` | `/api/projects/{id}/chat` | cookie | `api_project_chat` | Start agent task — returns `text/event-stream` SSE stream of `{type:"tool\|text\|result\|error", ...}`. Shared session + lock with board auto-runs. 409… |
| `GET` | `/api/projects/{id}/chat/queue` | cookie | `api_chat_queue_list` | Return pending queued messages. |
| `POST` | `/api/projects/{id}/chat/queue` | cookie | `api_chat_queue_add` | Enqueue a message (called when project is busy). |
| `PATCH` | `/api/projects/{id}/chat/queue/{msg_id}` | cookie | `api_chat_queue_edit` | Edit queued message text. |
| `DELETE` | `/api/projects/{id}/chat/queue/{msg_id}` | cookie | `api_chat_queue_delete` | Remove a queued message. |
| `POST` | `/api/projects/{id}/chat/steer` | cookie | `api_chat_steer` | Inject a message into the running turn (spec-086). |
| `POST` | `/api/projects/{id}/chat/stop` | cookie | `api_project_chat_stop` | Interrupts the current agent run. |
| `GET` | `/api/projects/{id}/chats` | cookie | `api_project_chats_list` | → {active, chats:[{id,name,session_id,created_at}]} |
| `POST` | `/api/projects/{id}/chats` | cookie | `api_project_chats_create` | {name?} → created chat entry |
| `PATCH` | `/api/projects/{id}/chats/{chat_id}` | cookie | `api_project_chats_patch` | Rename or activate a chat, or switch its runtime — `{name?, active?, provider?, model?, backend?, account?, expected_revision?}`. The switch is valid… |
| `DELETE` | `/api/projects/{id}/chats/{chat_id}` | cookie | `api_project_chats_delete` | Delete a non-final chat; provider threads/sessions are not deleted |
| `POST` | `/api/projects/{id}/chats/{chat_id}/handoff` | cookie | `api_project_chat_handoff` | Spec-092 runtime handoff — `{messages:[{role,text,tools}], from_label, to_label, commit?, text?}`. `commit` false/absent previews `{handoff:{text, ..…` |
| `GET` | `/api/projects/{id}/claude-md` | cookie | `api_project_claude_md` | Read project `CLAUDE.md` |
| `POST` | `/api/projects/{id}/claude-md` | cookie | `api_project_claude_md_write` | Overwrite CLAUDE.md. |
| `GET` | `/api/projects/{id}/context-pack` | cookie | `api_project_context_pack` | Preview the context pack that would be injected. |
| `GET` | `/api/projects/{id}/decision/{decision_id}` | cookie | `api_plan_get` | Full decision record (card render + reload). |
| `POST` | `/api/projects/{id}/decision/{decision_id}/decide` | cookie | `api_plan_decide` | {decision: approve\|reject, feedback?} |
| `POST` | `/api/projects/{id}/delete` | cookie | `api_project_delete` | Body: {confirm_name} |
| `GET` | `/api/projects/{id}/delete-precheck` | cookie | `api_project_delete_precheck` | — |
| `GET` | `/api/projects/{id}/epic-specs` | cookie | `api_project_epic_specs` | Spec-049 B / spec-059 Move 1: the project's |
| `GET` | `/api/projects/{id}/epic-specs/{name}` | cookie | `api_project_epic_spec_content` | Markdown of one epic spec file. |
| `POST` | `/api/projects/{id}/favorite` | cookie | `api_project_favorite` | Spec-031: POST /api/projects/{id}/favorite — toggle favorite status. |
| `GET` | `/api/projects/{id}/file` | cookie | `api_project_file` | File contents. |
| `GET` | `/api/projects/{id}/files` | cookie | `api_project_files` | Directory listing. |
| `POST` | `/api/projects/{id}/git/sync` | cookie | `api_project_git_sync` | Commit dirty files + push (one-button sync) |
| `POST` | `/api/projects/{id}/group` | cookie | `api_project_group_set` | — |
| `GET` | `/api/projects/{id}/health` | cookie | `api_project_health` | Connected capabilities and security check. |
| `POST` | `/api/projects/{id}/incident` | none | `api_project_incident` | Spec-012 Ph3: optional incident push. |
| `GET` | `/api/projects/{id}/incidents` | cookie | `api_project_incidents` | Count of active incidents (for sidebar badge). |
| `POST` | `/api/projects/{id}/label` | cookie | `api_project_label` | {name: str} |
| `GET` | `/api/projects/{id}/live` | cookie | `api_project_live` | Snapshot of the current (or last) LiveTurn buffer. |
| `GET` | `/api/projects/{id}/logs` | cookie | `api_project_logs` | Runtime logs via log_cmd from topics.json. |
| `GET` | `/api/projects/{id}/media/{filename}` | cookie | `api_project_media` | Serve agent screenshot to the cockpit. |
| `GET` | `/api/projects/{id}/memory` | cookie | `api_project_memory` | Read all memory files. Reads `.claude-ops/memory/`; fallback to old `~/.claude/projects/<cwd>/memory/` if new path absent. Returns `{files, exists}`. |
| `POST` | `/api/projects/{id}/memory/{name}` | cookie | `api_project_memory_write` | Create or update a memory entry. Body: `{"content":"..."}`. Validates slug, checks size limit, atomic write, auto-reindexes `MEMORY.md`. Returns upda… |
| `DELETE` | `/api/projects/{id}/memory/{name}` | cookie | `api_project_memory_delete` | Delete a memory entry. Auto-reindexes `MEMORY.md`. Returns updated `{files, exists}`. Cannot delete `MEMORY.md` directly (400). 404 if entry not foun… |
| `POST` | `/api/projects/{id}/model` | cookie | `api_project_set_model` | Set active model for next request — `{"model":"sonnet\|opus\|haiku"}` |
| `GET` | `/api/projects/{id}/monitors` | cookie | `api_project_monitors` | Snapshot of the session's background-task monitors. |
| `DELETE` | `/api/projects/{id}/monitors/{mid}` | cookie | `api_project_monitor_dismiss` | Operator dismisses a monitor row. |
| `GET` | `/api/projects/{id}/monitors/{mid}/tail` | cookie | `api_project_monitor_tail` | Spec-089 §7: last N steps of a sub-agent |
| `POST` | `/api/projects/{id}/notify-on-error` | cookie | `api_project_notify_toggle` | {enabled: bool} — TG notifications on new errors. |
| `GET` | `/api/projects/{id}/plan/{plan_id}` | cookie | `api_plan_get` | Full decision record (card render + reload). |
| `POST` | `/api/projects/{id}/plan/{plan_id}/decide` | cookie | `api_plan_decide` | {decision: approve\|reject, feedback?} |
| `GET` | `/api/projects/{id}/readme` | cookie | `api_project_readme` | Read project `README.md` |
| `POST` | `/api/projects/{id}/readme` | cookie | `api_project_readme_write` | Overwrite existing README (or create README.md). |
| `POST` | `/api/projects/{id}/rename` | cookie | `api_project_rename` | {slug: str} |
| `POST` | `/api/projects/{id}/rewind` | cookie | `api_project_rewind` | Body {"message_uuid": "..."}. |
| `POST` | `/api/projects/{id}/rewind-conversation` | cookie | `api_project_rewind_conversation` | Body {"message_uuid": "..."}. |
| `GET` | `/api/projects/{id}/roles` | cookie | `api_project_roles` | — |
| `GET` | `/api/projects/{id}/roles/{name}` | cookie | `api_project_role_get` | — |
| `POST` | `/api/projects/{id}/roles/{name}` | cookie | `api_project_role_write` | — |
| `DELETE` | `/api/projects/{id}/roles/{name}` | cookie | `api_project_role_delete` | — |
| `POST` | `/api/projects/{id}/roles/{name}/enabled` | cookie | `api_project_role_enabled` | — |
| `POST` | `/api/projects/{id}/rotate` | cookie | `api_project_rotate` | Cockpit "Wrap & reset" button. |
| `GET` | `/api/projects/{id}/rules` | cookie | `api_project_rules` | The policy rules (docs/RULES.md) this project runs under. |
| `GET` | `/api/projects/{id}/running` | cookie | `api_project_running` | Whether an agent run is active for this project. |
| `POST` | `/api/projects/{id}/scan-errors` | cookie | `api_project_scan_errors` | Manual scanner run for one project. |
| `GET` | `/api/projects/{id}/secrets` | cookie | `api_project_secrets` | List of key NAMES (no values). |
| `POST` | `/api/projects/{id}/secrets/{key}` | cookie | `api_project_secrets_set` | Set a secret. |
| `DELETE` | `/api/projects/{id}/secrets/{key}` | cookie | `api_project_secrets_delete` | Delete a secret. |
| `POST` | `/api/projects/{id}/seen` | cookie | `api_project_seen` | Operator opened/focused the project tab. |
| `POST` | `/api/projects/{id}/session` | cookie | `api_project_set_session` | Switch or reset session. |
| `GET` | `/api/projects/{id}/session-context` | cookie | `api_project_session_context` | Current session context summary (Feature A — context read) |
| `GET` | `/api/projects/{id}/session-history` | cookie | `api_project_session_history` | Feed for active (or specified) session. |
| `GET` | `/api/projects/{id}/sessions` | cookie | `api_project_sessions` | List of SDK sessions for the project. |
| `POST` | `/api/projects/{id}/sessions/{sid}/label` | cookie | `api_project_session_label` | {label} |
| `GET` | `/api/projects/{id}/settings` | cookie | `api_project_settings_get` | Per-project settings. |
| `POST` | `/api/projects/{id}/settings` | cookie | `api_project_settings_post` | Partial update of per-project settings in topics.json. |
| `GET` | `/api/projects/{id}/skills` | cookie | `api_project_skills` | → {global: [...], project: [...]}. |
| `GET` | `/api/projects/{id}/specs` | cookie | `api_project_specs` | List spec files in project |
| `GET` | `/api/projects/{id}/specs/{name}` | cookie | `api_project_spec_content` | Read a specific spec file by name |
| `GET` | `/api/projects/{id}/tasks` | cookie | `api_project_tasks` | Parse `TASKS.md` → return all cards grouped by column |
| `POST` | `/api/projects/{id}/tasks` | cookie | `api_create_task` | Create new card in Backlog — `{"text":"...","provider":"claude\|codex\|grok"?,"model":"..."?}`. `provider:"grok"` needs no per-project flag (see [Grok]… |
| `GET` | `/api/projects/{id}/tasks/done` | cookie | `api_tasks_done` | Contents of the DONE.md archive — loaded on demand (sessions don't read it). |
| `PATCH` | `/api/projects/{id}/tasks/{card}` | cookie | `api_update_task` | Edit card text and optional provider/model override. Run precedence: card provider → project `board_provider` → Claude. Choosing Grok needs no per-pr… |
| `DELETE` | `/api/projects/{id}/tasks/{card}` | cookie | `api_delete_task` | Delete card from `TASKS.md` |
| `POST` | `/api/projects/{id}/tasks/{card}/apply` | cookie | `api_card_apply` | Apply worktree branch (merge --no-ff) into the main tree. |
| `POST` | `/api/projects/{id}/tasks/{card}/check` | cookie | `api_card_check` | Run quality gate in card worktree. |
| `POST` | `/api/projects/{id}/tasks/{card}/discard` | cookie | `api_card_discard` | Discard worktree card (branch deleted). |
| `POST` | `/api/projects/{id}/tasks/{card}/move` | cookie | `api_move_task` | Move card to another column — `{"to":"Backlog\|In Progress\|Review\|Failed\|done"}`. Moving to \*\*In Progress\*\* auto-starts `run_engine`; moving to `done`… |
| `GET` | `/api/projects/{id}/tasks/{card}/run` | cookie | `api_card_run` | Sidecar from DATA/runs/&lt;card&gt;.md (404-safe). |
| `POST` | `/api/projects/{id}/test` | cookie | `api_project_test` | Run tests (auto-detects pytest / npm test / make test) |
| `GET` | `/api/projects/{id}/timeline` | cookie | `api_project_timeline` | Project event history. |
| `POST` | `/api/projects/{id}/unarchive` | cookie | `api_project_unarchive` | — |
| `POST` | `/api/projects/{id}/upgrade` | cookie | `api_project_upgrade` | '🔧 Bring up to standard' card: supplements CLAUDE.md/TASKS.md/README/.gitignore from templates without overwriting existing content. |
| `POST` | `/api/projects/{id}/upload` | cookie | `api_project_upload` | Multipart file → data/inbox/ → {path, name, size}. |
| `GET` | `/api/projects/{id}/upload/{filename}` | cookie | `api_project_upload_file` | Serve a user-uploaded inbox file. |
| `GET` | `/api/prompts` | cookie | `api_prompts_list` | List all prompts `[{id, title, category, text}, ...]` |
| `POST` | `/api/prompts` | cookie | `api_prompt_create` | Create prompt — `{"title":"...", "category":"...", "text":"..."}` |
| `PATCH` | `/api/prompts/{id}` | cookie | `api_prompt_update` | Update prompt fields |
| `DELETE` | `/api/prompts/{id}` | cookie | `api_prompt_delete` | Delete prompt |
| `POST` | `/api/push/subscribe` | cookie | `api_push_subscribe` | Store a PushSubscription (deduplicated by endpoint). |
| `POST` | `/api/push/test` | cookie | `api_push_test` | Send a test Web Push to all stored subscriptions so the |
| `POST` | `/api/push/unsubscribe` | cookie | `api_push_unsubscribe` | Remove a PushSubscription by endpoint. |
| `GET` | `/api/push/vapid-public` | cookie | `api_push_vapid_public` | Returns the VAPID public key for the browser to subscribe. |
| `GET` | `/api/schedules` | cookie | `api_schedules_get` | Returns normalised schedule registry. |
| `POST` | `/api/schedules/scan` | cookie | `api_schedules_scan` | Triggers immediate background re-scan. |
| `POST` | `/api/schedules/{id}/investigate` | cookie | `api_schedules_investigate` | Create Backlog card for investigation. |
| `GET` | `/api/search` | cookie | `api_search` | Ranked (bm25 + recency) hits |
| `POST` | `/api/search/reindex` | cookie | `api_search_reindex` | Drops + rebuilds the whole index. Manual escape |
| `GET` | `/api/secrets` | cookie | `api_vault_list` | List names and categories (NEVER values). |
| `POST` | `/api/secrets` | cookie | `api_vault_set` | Create or update a secret. |
| `GET` | `/api/secrets/{name}` | cookie | `api_vault_get` | Reveal a single secret (value + metadata). |
| `DELETE` | `/api/secrets/{name}` | cookie | `api_vault_delete` | Remove a secret. |
| `GET` | `/api/settings` | cookie | `api_settings_get` | Global settings: stored + effective values + spec. |
| `POST` | `/api/settings` | cookie | `api_settings_post` | Partial update of global settings (validated). |
| `GET` | `/api/system-load` | cookie | `api_system_load` (feature: load_monitor) | How loaded this host is, judged against its OWN limits. Returns `{level: ok\|warn\|crit\|unknown, score 0-100, at, age_s, chats:{live,max}, signals:[{id…` |
| `GET` | `/api/terminal/ws` | cookie | `api_terminal_ws` | Bidirectional PTY terminal over WebSocket. |
| `GET` | `/api/trash` | cookie | `api_trash_list` | List trashed projects. |
| `POST` | `/api/trash/{entry}/restore` | cookie | `api_trash_restore` | Move folder back, rebind topics.json. |
| `GET` | `/api/ui-state` | cookie | `api_ui_state_get` | → {state: {...}} — cockpit layout for this user. |
| `PUT` | `/api/ui-state` | cookie | `api_ui_state_put` | {state: {...}} — save layout. Body is an opaque |
| `POST` | `/api/update` | cookie | `api_update` | — |
| `GET` | `/api/usage` | cookie | `api_usage` | Subscription usage — 5h and 7-day limits with utilisation 0–1 and `resets_at`. Source: `GET https://api.anthropic.com/api/oauth/usage` (cached 60s).… |
| `GET` | `/api/usage/dashboard` | cookie | `api_usage_dashboard` | Token/turn dashboard (`?days=30\|all`, `?models=`). `providers.claude` / `providers.codex` as before, and \*\*`providers.grok` only while `GROK_ENABLED=…` |
| `GET` | `/api/usage/export.csv` | cookie | `api_usage_export` | — |
| `GET` | `/api/usage/ledger` | cookie | `api_usage_ledger` | — |
| `POST` | `/api/usage/scan` | cookie | `api_usage_scan` | Force an immediate (awaited) incremental scan and return its stats. |
| `GET` | `/api/version` | cookie | `api_version` | — |
| `GET` | `/dl/{name}` | none | `public_download` | Hand over a file from data/public, unauthenticated. |

## Not listed one by one

- **HEAD**: aiohttp registers a HEAD twin for every GET route (89 of them). It has the same path, auth and handler as its GET.
- **`/{path_info}` (any method)**: the SPA fallback. It serves the built web UI (`web/dist`) for every path that no route above matched. It is outside `/api/`, so the auth middleware does not guard it.

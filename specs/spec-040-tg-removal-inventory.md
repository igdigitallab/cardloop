---
created: 2026-06-15
status: verified-inventory (read-only analysis; no code changed)
relates_to: spec-040-decouple-telegram.md
---

# Spec 040 — TG Removal: Full Code Inventory

Verified against real code (2526-line bot.py, 9408-line webapp.py, 60 test files).
Spec-040 design is mostly accurate; corrections and additions noted inline.

---

## Phase B — Extract `engine.py` from `bot.py`

### bot.py block map (by line range)

| Block | Lines | Class | Notes |
|-------|-------|-------|-------|
| Stdlib imports | 8–19 | ENGINE | asyncio, json, Path, etc. |
| claude_agent_sdk imports | 21–38 | ENGINE | All SDK types |
| `import webapp` | 39 | GLUE | Needed for `_timeline_append`/`_bus_publish` callbacks |
| `from board import ...` | 40–50 | ENGINE | transport-neutral |
| `from telegram import ...` | 51–61 | **TG** | All PTB imports — 11 lines |
| HERE, DATA paths | 63–66 | ENGINE | |
| `_load_env()` | 69–83 | ENGINE | |
| CLAUDE_AUTH_MODE + API key pop | 85–96 | ENGINE | |
| `BOT_TOKEN` env read | 98 | **TG** | |
| `GROUP_CHAT_ID` env read | 99 | **TG** | |
| `ALLOWED_USERS` env read | 100 | **TG** | |
| DEFAULT_CWD, DEFAULT_MODEL, WEB_PORT, WEB_PASSWORD, MODELS | 101–107 | ENGINE | |
| `_EXECUTOR_MODEL`, `_RESEARCHER_MODEL`, `_QUICK_MODEL` | 112–114 | ENGINE | |
| `DEFAULT_AGENTS` dict | 124–177 | ENGINE | |
| `_build_agents_kwargs()` | 180–225 | ENGINE | |
| `CONDUCTOR_PROMPT` | 228–236 | ENGINE | |
| `MAX_SUBAGENT_PROGRESS` | 239 | ENGINE | |
| `OPERATOR_NAME`, `RESPONSE_LANGUAGE` | 242–243 | ENGINE | |
| `TG_CHUNK = 4000` | 246 | **TG** | |
| **`TELEGRAM_NUDGE`** | 254–269 | **TG** | ⚠️ used as default in run_engine (line 1284) |
| `DISALLOWED_TOOLS` | 271 | ENGINE | |
| `BOARD_PROTOCOL` | 273–280 | ENGINE | |
| `TOPICS_F` | 282 | GLUE | shared by TG + engine state |
| `SESSIONS_F` | 283 | ENGINE | |
| `TG_QUEUE_F` | 284 | **TG** | data/tg_queue.json |
| `TG_QUEUE_MAX` | 286 | **TG** | |
| `_norm()`, `_home_sub()` | 288–294 | ENGINE | |
| `_load_registry_json()`, `_REG_RAW`, `build_registry()`, `REGISTRY`, `resolve_project()` | 297–340 | ENGINE | |
| `_read()` helper | 344–348 | ENGINE | |
| `topics`, `sessions`, `costs`, `running`, `rate_limits` state dicts | 351–355 | ENGINE | ⚠️ `topics` is GLUE — format tied to TG keys until Phase 0 |
| `_LiveEntry` dataclass | 360–368 | ENGINE | spec-028 |
| `_live_clients`, `pending_handoff`, `context_warned` | 370–377 | ENGINE | spec-028 |
| **`_TG_QUEUE`** | 384 | **TG** | |
| **TG queue functions** (`_tg_queue_flush/enqueue/pop/clear/len`) | 387–436 | **TG** | 50 lines |
| `save_topics()` | 439–440 | GLUE | |
| `save_sessions()` | 443–444 | ENGINE | |
| **`key_of()`** | 447–450 | **TG** | takes PTB Update; format `chat:thread` is ENGINE concern |
| **`binding_for()`** | 453–463 | **TG** | takes PTB Update |
| **`authorized()`** | 467–469 | **TG** | checks ALLOWED_USERS |
| **`_tg_call()`** | 473–488 | **TG** | |
| **`send()`** | 491–504 | **TG** | TG message sender |
| `CODE_MAX_LINES`, `CODE_PREVIEW_LINES` | 507–508 | **TG** | (or move to utils if needed) |
| **`_render_code_block()`** | 511–522 | **TG** | |
| **`md_to_html()`** | 525–562 | **TG** | ⚠️ tested in test_md_to_html.py — delete or keep in utils |
| **`report_error()`** | 565–587 | **TG** | |
| **`_chunks()`, `_smart_chunks()`** | 590–621 | **TG** | |
| `short()` | 619–621 | GLUE | used in audit AND TG adapter; move to engine utils |
| `AUDIT_DIR`, `STALL_SECONDS`, `MAX_SECONDS` | 625–627 | ENGINE | |
| `PERSISTENT_CLIENT`, `LIVE_CLIENT_TTL_SEC`, `LIVE_CLIENT_MAX` | 633–637 | ENGINE | |
| `_DESTRUCTIVE`, `_is_destructive()` | 638–646 | ENGINE | |
| `audit()` | 649–657 | ENGINE | |
| `_HOOK_OUTPUT_TRUNCATE`, `_tool_response_to_str()` | 672–707 | ENGINE | |
| **`_make_post_tool_use_hook()`** | 710–759 | GLUE | calls `webapp._timeline_append` at line 746 |
| **`_make_pre_compact_hook()`** | 764–801 | GLUE | calls `webapp._bus_publish` at line 789 |
| `_compute_fingerprint()` | 809–829 | ENGINE | |
| `_evict_live_client()` | 832–851 | ENGINE | |
| `_get_or_create_live_client()` | 854–913 | ENGINE | |
| `_schedule_idle_eviction()` | 916–930 | ENGINE | |
| `_build_board_append()` | 939–949 | ENGINE | |
| `_RECONCILE_OPS_CAP`, `_RECONCILE_SYSTEM` | 959–979 | ENGINE | |
| `_norm_title()`, `_apply_reconcile_ops()`, `reconcile_board()` | 982–1211 | ENGINE | ~230 lines |
| `run_engine()` | 1232–1519 | ENGINE | **~288 lines; TELEGRAM_NUDGE default at line 1284** |
| **`run_agent()`** | 1531–1789 | **TG** | TG adapter ~259 lines; calls `webapp._bus_publish` at 1619/1671/1687/1735/1748 |
| **`fetch_files()`** | 1793–1811 | **TG** | file inbox |
| **`on_message()`** | 1814–1873 | **TG** | |
| **`_drain_tg_queue()`, `_safe_run_queued()`, `safe_run()`** | 1876–1945 | **TG** | |
| **`on_error()`, `on_topic_created()`** | 1948–1979 | **TG** | |
| **`cmd_start/whoami/reset/resume/model/project/newtopic/diff/cost/usage/stop/later`** | 1983–2280 | **TG** | 13 handlers, ~298 lines |
| `format_usage()`, `_RL_LABELS`, `_RL_ICON`, `_fmt_reset` | 2132–2166 | GLUE | formatting logic; move to engine utils or cockpit |
| `_parse_time_spec()` | 2196–2230 | GLUE | pure time parsing; useful for deferred runs in cockpit |
| **`_build_ctx()`** | 2285–2334 | GLUE | wires ENGINE→webapp; injects `ptb_app`, `GROUP_CHAT_ID` |
| `_graceful_shutdown()` | 2337–2368 | ENGINE | no TG refs |
| **`_amain()` PTB branch** | 2370–2468 | GLUE | PTB setup + webapp start on same loop |
| `_amain()` web-only branch | 2471–2502 | ENGINE | becomes the entry point after Phase D |
| `_check_web_password()`, `main()` | 2505–2522 | ENGINE | |

---

### Circular-import hazard (GLUE blocks)

`_make_post_tool_use_hook()` (line 746) and `_make_pre_compact_hook()` (line 789) call `webapp._timeline_append` and `webapp._bus_publish` directly.

`webapp.py` does NOT import `bot.py` — access is one-way via `ctx`. Moving these hooks to `engine.py` would create a new circular import: `engine → webapp`.

**Resolution (per spec-040 Phase 1):** inject `_timeline_append` and `_bus_publish` as callback parameters into `_build_ctx()` / `run_engine()`, keeping engine.py free of webapp imports.

---

### TELEGRAM_NUDGE usage (⚠️ latent cockpit bug)

- Defined: lines 254–269 (TG-specific text about message-length limits, HTML formatting, etc.)
- Used as **default** in `run_engine()` at line 1284 → every cockpit run inherits TG-flavoured prompt UNLESS caller passes explicit `system_prompt`.
- `api_project_chat` in webapp.py does pass an explicit `system_prompt`, so cockpit runs are currently safe — but this is fragile.
- **Fix before Phase B:** Replace `TELEGRAM_NUDGE` default with `DEFAULT_NUDGE` (neutral) or require callers to always pass `system_prompt`. See spec-040 open question #4.

---

### `_build_ctx()` TG-specific keys to remove (Phase D)

- `ptb_app` (line 2317) — remove after Phase D; webapp guards on it
- `GROUP_CHAT_ID` (line 2319) — remove after Phase D

---

## Phase C — Replace TG push with cockpit push

### webapp.py TG surfaces (all with exact lines)

| Function | Lines | Guarded? | Replacement |
|----------|-------|----------|-------------|
| `_send_tg_ping()` | 3414–3428 | Yes (`if ptb_app`) | Cockpit SSE banner / Web Push |
| `_sync_forum_topic_name()` | 3430–3446 | Yes | Drop entirely (no forum in cockpit) |
| `_notify_new_incidents()` | 3447–3465 | Wraps _send_tg_ping | Cockpit push (already in SSE pipeline?) |
| `_notify_tg()` | 3859–3878 | Yes (`if ptb is None`) | Cockpit push or Web Push |
| `_notify_operator()` | 5038–5055 | Yes (`if ptb_app is None`) | **NOT fully dead** — 6 callers at lines 4916/4954/5129/5149/5217/5234/5319 (deferred-run lifecycle) |

**Inline TG send** at lines 2709–2715 inside `_scan_and_ingest_errors`: needs cockpit replacement.

### `chat:thread` key parsing sites in webapp.py

| Line | Context | What changes |
|------|---------|-------------|
| 1579–1583 | `api_project_delete` | After Phase 0: key is slug, no split needed; forum delete gone in Phase D |
| 1762–1763 | `api_trash_restore` | After Phase 0: rebuild from slug, not `tg_chat:tg_thread` |
| 2709–2710 | `_scan_and_ingest_errors` inline | Remove with TG inline send |
| 3419–3420 | `_send_tg_ping` | Remove with function |
| 3435–3437 | `_sync_forum_topic_name` | Remove with function |
| 3865 | `_notify_tg` | Remove with function |
| 8647 | `api_new_project` | After Phase 0: assign `session_key = _project_id(cwd)` |

**Spec-040 cited lines 3853/3425/2697 — actual lines are 3865/3419/2709 (off by 6–12 lines; spec was approximate).**

### `tg_thread` field usage (⚠️ biggest surgery)

The field `tg_thread` is the **universal session key alias** used as project identifier throughout webapp.py at 20+ sites. It is NOT just a TG artifact — it's the primary lookup key. Phase 0 must rename this field in both data files and all code before any TG removal.

Key sites (non-exhaustive): lines 1021, 1044, 1251, 1402, 1438, 4114, 4156, 4225, 4260, 4300, 4332, 4386, 4538, 4573, 4676, 6493–6839, 6986, 7119–7244, 7566–7626, 7737, 8718, 8931, 8988.

**Estimated: 25–30 sites in webapp.py alone.**

---

## Phase D — Remove PTB entirely

### Checklist

- [ ] Delete TG imports (bot.py lines 51–61)
- [ ] Delete `BOT_TOKEN`, `GROUP_CHAT_ID`, `ALLOWED_USERS` env reads (lines 98–100)
- [ ] Delete `TG_CHUNK`, `TELEGRAM_NUDGE` (lines 246, 254–269)
- [ ] Delete `TG_QUEUE_F`, `TG_QUEUE_MAX` (lines 284, 286)
- [ ] Delete entire TG queue machinery (lines 384–436, ~53 lines)
- [ ] Delete `key_of()`, `binding_for()`, `authorized()` (lines 447–469)
- [ ] Delete `_tg_call()`, `send()`, `_render_code_block()`, `md_to_html()`, `report_error()`, `_chunks()`, `_smart_chunks()` (lines 473–621, ~149 lines)
- [ ] Delete `run_agent()` TG adapter (lines 1531–1789, ~259 lines)
- [ ] Delete `fetch_files()` (lines 1793–1811)
- [ ] Delete all `on_*` event handlers (lines 1814–1979)
- [ ] Delete all `cmd_*` command handlers (lines 1983–2280, ~298 lines)
- [ ] Delete PTB Application setup from `_amain()` (lines 2402–2468)
- [ ] Remove `ptb_app`, `GROUP_CHAT_ID` from `_build_ctx()`
- [ ] Remove `python-telegram-bot` from `requirements.txt` (check filename)
- [ ] Archive `data/tg_queue.json` (currently 0 items)
- [ ] Archive `data/inbox/` (6 files — decide: cockpit file-upload or defer)
- [ ] Update `claude-ops-bot.service`: ExecStart → `engine.py` (or `bot.py --no-tg`)
- [ ] Remove TG env vars from `.env.example`: `BOT_TOKEN`, `GROUP_CHAT_ID`, `ALLOWED_USERS`, `TG_QUEUE_MAX`
- [ ] Update `.env.example` comments mentioning "TG warning", etc.
- [ ] Remove TG push functions from webapp.py (5 functions, ~70 lines total)
- [ ] Remove inline TG send from `_scan_and_ingest_errors` (lines 2709–2715)
- [ ] Remove `GROUP_CHAT_ID` from `api_new_project` (line 8634–8648)
- [ ] Remove `tg_chat`/`tg_thread_id` extraction from `api_project_delete` and `api_trash_restore`
- [ ] Remove `AppCtx.GROUP_CHAT_ID` field (line 3740 approx)

---

## Phase E — Tests

### Purely TG → delete (3 files, ~34 test functions)

| File | Functions | Rationale |
|------|-----------|-----------|
| `tests/test_tg_queue.py` | ~17 | Tests `_tg_queue_*` and `on_message` — 100% PTB |
| `tests/test_forum_topic.py` | ~7 | Tests `_sync_forum_topic_name` and `create_forum_topic` |
| `tests/test_tg_session_resume.py` | ~10 | Tests TG-specific session resume via `run_agent`; 3 functions test `system_prompt` construction — extract to engine tests first |

### Import bot for engine helpers → redirect to engine.py (12 files)

| File | What it imports from bot | Action |
|------|--------------------------|--------|
| `test_default_prompt_templates.py` | prompt templates | → engine.py |
| `test_post_tool_use_hook.py` | post-tool-use hook | → engine.py |
| `test_spec017_orchestrator.py` | orchestrator logic | → engine.py |
| `test_context_rotation.py` | rotation helpers | → engine.py |
| `test_spec034_board_os.py` | `_build_board_append`, `reconcile_board` | → engine.py |
| `test_md_to_html.py` | `md_to_html`, `CODE_MAX_LINES` | Delete OR move md_to_html to utils |
| `test_is_destructive.py` | `_is_destructive` | → engine.py |
| `test_security_phase0.py` | `_check_web_password` | → engine.py or webapp |
| `test_secrets.py` | secrets resolution | → engine.py |
| `test_spec028_persistent_client.py` | live-client registry | → engine.py |
| `test_spec029_streaming.py` | conditional import bot | → engine.py |
| `test_spec039_stop_killing_sessions.py` | session stop | → engine.py |

### Transport-neutral → keep as-is (~45 files, ~1100 test functions)

All files importing only `webapp` directly — no changes needed beyond import paths if engine.py exposes same API.

**Total test inventory: 60 files, ~1153 test functions.**

---

## Phase F — Docs and prompt strings

### Files with TG content to update

| File | TG density | What changes |
|------|-----------|-------------|
| `README.md` | Heavy — 15+ TG mentions | Rewrite "three channels" → "web cockpit + kanban"; remove bot setup section |
| `ARCHITECTURE.md` | Heavy — full TG section | Remove TG architecture section; add engine.py block |
| `CONTRIBUTING.md` | Medium — BOT_TOKEN in quickstart, bot.py as entry | Update setup: no BOT_TOKEN, entry → engine.py |
| `GOTCHAS.md` | Medium — TG formatting, forum, no-interactivity | Remove TG-specific gotchas (lines 7–13, 38, 59, 73) |
| `CHANGELOG.md` | Light — spec-040 mention + "TG notifications" setting | Update after Phase D |
| `CLAUDE.md` | Medium — "три канала: кокпит/Telegram/канбан" (first paragraph), `bot.py` references | Update project description; map → engine.py |
| `.env.example` | Heavy — BOT_TOKEN/GROUP_CHAT_ID/ALLOWED_USERS/TG_QUEUE_MAX | Remove TG env vars |
| `claude-ops-bot.service` | Line 2 description + ExecStart=bot.py | Update description + entry point |
| `specs/spec-040-decouple-telegram.md` | This is the design doc — update as phases complete | |

### Prompt strings with TG content in code

| Symbol | Lines | Issue |
|--------|-------|-------|
| `TELEGRAM_NUDGE` | 254–269 | Delete entirely; replace with `DEFAULT_NUDGE` |
| `CONDUCTOR_PROMPT` | 228–236 | Check for TG mentions (spec says "three channels") |
| `BOARD_PROTOCOL` | 273–280 | Check for TG mentions |
| `_RECONCILE_SYSTEM` | 959–979 | Check for TG mentions |
| CLAUDE.md project instructions | Line 1 | "Три канала: кокпит (@YOUR_BOT), Telegram, канбан" → remove TG channel |

---

## Phase 0 prerequisite — Data migration

### Current state

| File | Entries | Key format |
|------|---------|-----------|
| `data/topics.json` | 31 | ALL are `"chat:thread"` format (e.g., `"-100xxxx:3"`) |
| `data/sessions.json` | 35 | 26 `"chat:thread"` + 8 `"free-<hex8>"` + 1 `"glasses:claude-ops-bot"` |
| `data/tg_queue.json` | 0 | Empty — safe to archive immediately |
| `data/inbox/` | 6 files | TG file inbox — decide before Phase D |

### Migration needed

- Rename 31 `topics.json` keys: `"chat:thread"` → `_project_id(cwd)` slug (value from the entry's `cwd` field)
- Rename 26 `sessions.json` keys: same mapping (preserve `session_id` values for SDK resume)
- The `"glasses:claude-ops-bot"` key is already non-TG custom key — keep as-is or normalize
- The 8 `"free-<hex8>"` keys — already neutral, no change

**Decision needed (spec-040 open question #3):** single `_project_id(cwd)` slug per project, or `{user_id}:{cwd_slug}` for future multi-user? This determines the migration target format.

---

## Risks and Hidden Dependencies

### R1 — `_notify_operator` is NOT dead after TG removal (HIGH)

6 callers in webapp.py (lines 4916, 4954, 5129, 5149, 5217, 5234, 5319) handle deferred-run lifecycle (queued/started/complete/failed/rate-limit). Removing TG without a cockpit replacement leaves these events silently dropped. **Must implement cockpit push BEFORE Phase D, not after.**

### R2 — `tg_thread` field is the universal session key (HIGH)

25–30 sites in webapp.py read `project["tg_thread"]` or `session_key` as the project's canonical identifier. This field name is misleading but central. Renaming it (Phase 0) touches most of the webapp. Do NOT start Phase B/C before Phase 0 is complete — otherwise partial rename creates runtime key mismatch.

### R3 — `_make_post_tool_use_hook` / `_make_pre_compact_hook` circular import (MEDIUM)

These ENGINE functions call `webapp._timeline_append` and `webapp._bus_publish` directly. Moving to engine.py without injection refactor creates `engine → webapp` import. Solution: inject callbacks at `_build_ctx()` time. Must be done as part of Phase B, not after.

### R4 — `test_tg_session_resume.py` contains mixed tests (LOW-MEDIUM)

3 of ~10 functions test `system_prompt` construction logic that is also cockpit-relevant. Deleting the whole file loses those assertions. Extract to engine tests before deleting.

### R5 — `data/inbox/` has 6 real files (LOW)

No cockpit file-upload-to-agent path exists. After Phase D, users lose ability to feed files to agents. Spec-040 open question #2. Decide before Phase D: build cockpit upload or explicitly defer.

### R6 — `md_to_html()` tested independently (LOW)

`test_md_to_html.py` tests this TG-only function. If any cockpit rendering reuses markdown→HTML conversion logic, extract to utils first. Otherwise delete both.

### R7 — `TELEGRAM_NUDGE` default is a silent correctness bug TODAY (MEDIUM)

Cockpit callers currently pass explicit `system_prompt`, so the bug is masked. Any new caller that passes `system_prompt=None` will silently get TG-flavoured instructions. Fix in Phase B as the first commit.

### R8 — Service description advertises Telegram (LOW)

`claude-ops-bot.service` line 2: description says "Claude Code via Telegram". After Phase D, OSS users will see misleading service description. Minor but affects first impression.

### R9 — `glasses:claude-ops-bot` session key (LOW)

Non-standard key in sessions.json. If Even G2 glasses integration writes sessions this way, it survives Phase 0 migration (not a `chat:thread` key). But need to verify it won't break when `tg_thread` field is renamed.

### R10 — `_parse_time_spec()` is useful for deferred runs (LOW)

This function (lines 2196–2230) lives in the TG handler section but is pure time logic. The cockpit's deferred-run feature likely needs it. Classify as GLUE and move to engine utils, not delete.

---

## Recommended execution order

```
Phase 0:  Migrate session keys (topics/sessions.json + webapp.py tg_thread rename)
Phase 0b: Fix TELEGRAM_NUDGE default → DEFAULT_NUDGE in run_engine (line 1284)
Phase B:  Move ENGINE blocks to engine.py; inject webapp callbacks; bot.py → thin shim
Phase C:  Build cockpit push (SSE/Web Push) replacing _notify_operator/_notify_tg/_send_tg_ping
Phase D:  Delete PTB; archive queue/inbox; update service/requirements
Phase E:  Clean up tests (delete 3 TG files; redirect 12 import-bot files to engine)
Phase F:  Update docs/prompts
```

**Do NOT start Phase B before Phase 0 is complete.** The `tg_thread` rename touches webapp so broadly that doing it mid-engine-extraction causes merge conflicts and runtime key mismatch.

**Do NOT start Phase D before Phase C is complete.** The `_notify_operator` callers will silently fail otherwise.

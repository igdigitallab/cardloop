# Changelog

All notable changes to Claude-Ops. Format — reverse chronological.
Versions follow semver-like conventions (0.x while the project is under active development).

> Discipline: when a new feature ships — add a line here + mark the card in TASKS.md → DONE.md. A tag is placed on a stable point (`git tag vX.Y.Z`).

## [Unreleased]

### Added
- **Cockpit settings — "⚙️ Settings" tab + global settings (card f2ba02).** Per-project (topics.json, hot-reload): **git on/off** (flagship — off → cards run in legacy mode without worktree, git-sync returns 409, health doesn't require .git, `.git` is not physically touched; sessions are preserved), model, self-healing, TG notifications, log_cmd, test_cmd. Global (new `data/settings.json`, mtime hot-reload, wired into runtime): self-healing master kill, max concurrent repairs, scanner interval, default model for new projects, watchdog stall/max. API: `GET/POST /api/settings`, `GET/POST /api/projects/{id}/settings` (type/range validation). Helpers `_get_global_setting`/`_git_enabled`/`_effective_default_model`. 20 tests (`test_settings.py`).

### Fixed
- **Project rename lost all conversation history and Timeline.** `api_project_rename` moved the folder (`shutil.move`) and updated `topics.json`, but SDK history (`~/.claude/projects/<slug>/`) and Timeline (`data/timeline/<slug>.jsonl`) are keyed by `slug = cwd.replace('/','-')` — after changing cwd the cockpit read an empty new slug, and "all sessions appeared to disappear" (files were intact under the old slug). Added `_migrate_cwd_keyed_state(old_cwd, new_cwd, ctx)`: moves the SDK sessions directory + Timeline (+`.jsonl.1`) to the new slug, best-effort, warnings in response `warnings`. Tests: `test_rename_migrates_sdk_sessions`, `test_rename_migrates_timeline`. Already-lost projects recovered by moving orphaned directories.

## [v0.8.1] — 2026-06-01
### Fixed
- **Memory: 404 on deleting a legacy entry** (bug since v0.4.0). `_memory_read_all` read the old location (`~/.claude/projects/<cwd>/memory/`) as a fallback, but `_memory_delete`/write only operated on the new location (`.claude-ops/memory/`) → deleting a legacy entry returned 404. Now, on first read, legacy memory is **auto-migrated** to the new location (for all projects at once), and delete/write operations work correctly. Test: `test_memory_read_all_migrates_legacy`.

## [v0.8.0] — 2026-05-31
Step 5 of the roadmap (final): Self-healing (Spec 010). Repair agent in a worktree + quality gate + human approval. **"Full development service" roadmap complete** (5/5 steps).

### Added
- **Self-healing** (Spec 010): `_self_heal_enabled(project)` — per-project flag (`self_heal`) or env `SELF_HEAL_ENABLED`. **OFF by default — NEVER enabled for any project automatically.**
- **`_self_heal_card(ctx, project, incident_card)`** — repair loop: mark `heal_attempted=true` BEFORE starting (loop prevention guard), build repair prompt, run via existing C2 path (`_card_worktree_setup` + `_run_card`), run `_run_quality_gate`, move to Review (safe) or Failed (risky), ping operator on Telegram.
- **Integration in `_error_scanner_loop`**: after `_scan_and_ingest`, if `self_heal=True` and new incidents exist → `asyncio.create_task(_self_heal_card(...))`. Limits: active repair counter ≤2, running lock, heal_attempted.
- **Timeline `kind:"self_heal"`**: phases `start / fixed / gate_ok / gate_fail / gate_unknown / skipped` published to the bus.
- **`POST /api/projects/{id}/self-heal {enabled}`** — per-project toggle. Auth-protected. Does not enable any project by default.
- **UI: "🔧 Self-heal" toggle** in OverviewTab + label "Nothing is applied without you". CSS badge `🔧 auto-repair · gate ✓/✗` on BoardTab cards.
- **28 new tests** (`tests/test_self_healing.py`): `_self_heal_enabled` (flag/env/default); `heal_attempted` meta; OFF default = critical regression guard; heal_attempted set before run; safe→Review, risky→Failed; heal_attempted incident not re-run; non-git→skip; busy→skip; concurrency limit; Timeline receives self_heal; API toggle (auth, enable, disable, 404). **496 passed** (was 468).

### Safety guards (inviolable)
1. OFF by default — `self_heal` in topics or `SELF_HEAL_ENABLED` env
2. NEVER auto-apply — agent only reaches Review; merge is always done by hand
3. 1 attempt per incident — `heal_attempted=true` set BEFORE agent starts
4. Concurrency limit — max 2 auto-repairs at once
5. git+clean only — non-git/dirty trees are skipped
6. Full visibility — Timeline kind:"self_heal" + TG ping

## [v0.7.0] — 2026-05-31
Step 4 of the roadmap: quality gate (Spec 009). C2 "Apply" is no longer blind: you can run tests in the card's worktree and get a verdict before merging.

### Added
- **Quality gate** (Spec 009): `_run_quality_gate(wt_path, env)` — runs tests in the card's worktree via `_detect_test_cmd` (reuse). Timeout 300s, output truncated to 20k. Verdict: `safe` (rc=0) / `risky` (rc≠0 or timeout) / `unknown` (no test config).
- **`POST /api/projects/{id}/tasks/{card}/check`** — gate endpoint: reads meta, runs `_run_quality_gate(wt_path)` with project secrets, returns verdict, writes `meta.gate={verdict,ts}` to JSON sidecar, publishes `{kind:"gate", verdict}` to Timeline. Legacy/no worktree → `{verdict:"unknown", reason:"legacy"}`. 400 bad card_id; 404 no project or no worktree on disk.
- **UI: "🧪 Check" button** in the card result modal (worktree mode, next to ✓Apply/✗Discard). After check: 🟢 Safe / 🔴 Risky / ⚪ No tests. On risky — collapsible test output (`<details>`). "Apply" button gets visual emphasis by verdict (green for safe, warning style for risky) — **but is NOT blocked**. ARIA: `aria-live=polite` on verdict.
- **15 new tests** (`tests/test_quality_gate.py`): safe/risky/unknown; tests run in wt_path; secrets in env; output truncated; API check: verdict, legacy→unknown, bad card_id→400, no worktree→404, no project→404, meta.gate updated. **468 passed** (was 453).
- **Lint:** out of scope in this iteration (spec-009, item 2). `lint: null` in response. Add in a future iteration if needed.

## [v0.6.0] — 2026-05-31
Step 3 of the roadmap: observability — Timeline (Spec 008). The event bus is now persisted; the cockpit gets a "🕒 Activity" tab.

### Added
- **Timeline persistence** (Spec 008): `_bus_publish` now calls `_timeline_append` — single write point. Each event is written to `data/timeline/<slug>.jsonl` (append-only, slug = `cwd.replace('/', '-')`). Rotation: >5MB → `.jsonl.1` (one copy). Writes swallow exceptions, env field is never written. `_timeline_init(ctx)` called from `start()`.
- **`GET /api/projects/{id}/timeline?limit=N&before=<ts>`** — history endpoint: reads JSONL (current + .1), parses gracefully (broken lines → skip), returns array in chronological order. Paginated by `before=<ts>` (Unix float). Auth-protected, anti-traversal via `_find_project_by_id`.
- **TimelineTab** (`web/src/tabs/TimelineTab.tsx`): history from `GET /timeline` + live events via `useProjectActivity` (reuses existing SSE connection, no new socket opened). "Load earlier" button with `before=<oldest_ts>`. Icons by kind (▶/✅/❌/🔧/💬), live badge with 4s pulse, ARIA (`role=log`, `aria-live=polite`). CSS: `styles/timeline.css`.
- **32 new tests** (`tests/test_timeline.py`): slug stability, path resolve, append+ts+truncate+env-exclusion, 5MB rotation, bus_publish integration, graceful broken JSONL, backup read, API GET/limit/before/env-not-in-response. **453 passed** (was 421).

## [v0.5.0] — 2026-05-31
Step 2 of the roadmap: isolated project key store (OSS mechanism; operator's personal vault is untouched).

### Added
- **Project key store** (Spec 007): `.claude-ops/secrets/secrets.env` — `chmod 600`, gitignored automatically on first write. Secrets are injected into the agent's `env` on every run (`run_engine`, `run_agent`, `_run_card`, `api_project_chat`). Isolated by cwd. **Values are NEVER returned via API** — only the list of key names. CRUD via cockpit: "🔑 Secrets" tab (SecretsTab) with add (password-input), list (masked ••••••) and delete (ConfirmModal). 47 new tests (421 passed). +3 endpoints: `GET/POST/DELETE /api/projects/{id}/secrets/{key}`.

## [v0.4.0] — 2026-05-31
Step 1 of the roadmap "full development service": accumulated project memory.

### Added
- **Project memory** (spec-006): moved into the project repo (`.claude-ops/memory/` — committed to git, travels with the project, OSS-friendly). POST/DELETE endpoints for CRUD from the cockpit. MemoryTab became editable (create/edit/delete entries). Agent writes memory itself via normal Write (nudge + section in CLAUDE.md template). `MEMORY.md` — auto-index. Entry types: decision / gotcha / rejected / convention. 49 new tests (374 passed).

## [v0.3.0] — 2026-05-31
Stable point after a major refactoring, cleanup, and C2 cycle.

### Added
- **C2-gate** — "Apply / Discard" gate + worktree-per-task: a card in a git project runs in an isolated `git worktree`, in Review you get ✓/✗ buttons, merge --no-ff or rollback. Safe rollback = foundation for future autonomy.
- `ARCHITECTURE.md` — code map for new developers/agents.
- OSS scaffold: `LICENSE` (MIT), `CONTRIBUTING.md`, `docs/API.md` (56 routes).
- ESLint + Prettier (`npm run lint` / `format`), i18n dictionary (`web/src/i18n/ru.ts`).
- Tests: 207 → 325 (board, chat, rename, concurrency, security, C2).

### Changed / Cleaned
- Glasses/G2 transport removed entirely (no longer relevant).
- Documentation rewritten into a hierarchy without duplication (README / ARCHITECTURE / CLAUDE.md / CONTRIBUTING).
- CLAUDE.md cleared of ledger history → forward rules + gotchas only.
- `styles.css` (3000+ lines) split into 10 partials.
- Backend: removed user path hardcodes, command-injection in log_cmd/test_cmd, path-traversal in card_id, auth → scrypt + secure cookie + rate-limit.
- systemd unit: added `EnvironmentFile=` (fix — `.env` was not being loaded).

## [v0.2.x] — before 2026-05-31
Cockpit (tabs, chat SSE, kanban board with auto-run, files, prompts), shared sessions cockpit↔TG, `run_engine` engine, test scaffold. (History — in git log.)

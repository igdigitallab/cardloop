> CLAUDE.md = working rules and gotchas for agents. Code map → ARCHITECTURE.md. API → docs/API.md. Setup → CONTRIBUTING.md. Subsystem gotchas → GOTCHAS.md.

# CLAUDE.md — Cardloop

An IDE environment for managing projects via the Claude Agent SDK. Two channels: the cockpit (`YOUR_DOMAIN`) and kanban auto-run. One engine, `run_engine()`, full-auto.

Design history & specs: `docs/internal/specs/` (gitignored).

---

## What goes where (quick map)

- `bot.py` — web-only launcher: loads env/auth, builds ctx, starts the web cockpit. The engine lives in `engine.py` (async event generator `{tool|text|result|rate_limit|error}`, transport-independent). Consumers: `_run_card` and `api_project_chat` (webapp.py). `running[k]=True` is reserved SYNCHRONOUSLY before the first await.
- `codex_engine.py` — the optional SECOND provider (`run_codex_engine`), same event schema as `engine.py` so consumers stay provider-agnostic. Off unless `CODEX_ENABLED=true`; the SDK import is lazy and auth is **subscription-only** (`type == "chatgpt"`) — an API key is refused, never silently billed. Continuity is a `codex_thread_id` per chat, parallel to Claude's `session_id`; a project/card picks its provider via `board_provider` / `provider`, default `claude`.
- `accounts.py` — multiple Claude subscriptions in one cockpit. An extra account = its own `CLAUDE_CONFIG_DIR` (`~/.claude-accounts/<id>/`) with private credentials and everything else symlinked back to `~/.claude`. `main` is virtual and injects no env — a single-account install behaves exactly as before. Login is the ordinary CLI flow via `tools/claude-acct login <id>`. ⚠️ Read the accounts gotcha in GOTCHAS.md before touching it: `CLAUDE_CODE_OAUTH_TOKEN` does NOT override a credentials file, and the account must stay in the live-client fingerprint.
- `webapp.py` — the aiohttp cockpit. It does **NOT** import `bot.py` — everything comes through `ctx` (a dict of references: topics/sessions/running/resolve_project/run_engine/DATA/…) passed in from `bot.py`.
- `data/topics.json` — **LAYER 1**: binding `"chat:thread" → {project,cwd,model}`. Permanent; `/reset` does not touch it.
- `data/sessions.json` — **LAYER 2**: `"chat:thread" → session_id`. Cleared only by `/reset`.
- `data/prompts.json` — cockpit prompt templates (CRUD via `/api/prompts`). **Not in git.**
- `cardloop.service` → `/etc/systemd/system/` (unit name overridable via `CARDLOOP_SERVICE`).
- `web/src/components/markdown.tsx` — the shared `mdComponents` for ALL `<ReactMarkdown>` instances (Files/CLAUDE.md/Board/Memory/Chat). Renders ```mermaid blocks as live SVG: `mermaid@11` lazily (`await import` → its own chunk, doesn't bloat the main bundle), `securityLevel:'strict'`, `suppressErrorRendering:true` (on a syntax error, falls back to the source, no "bomb"). ⚠️ A new `<ReactMarkdown>` must be wired with `components={mdComponents}`, otherwise diagrams won't render.
- `captcha_solver.py` — the 2captcha bridge behind `browser_solve_captcha`. Knows nothing about Playwright (page-side detection/injection lives in `browser_pane.solve_captcha`). Off unless `TWOCAPTCHA_API_KEY` / the safe's `twocaptcha_api_key` is set, and gated behind `agent_actions=full`. ⚠️ Only solves captcha **widgets** — a full-page Cloudflare interstitial is refused on purpose (IP-bound token); don't "fix" that by removing the guard, it would just burn balance on tokens Cloudflare rejects.
- `web/src/components/Lightbox.tsx` — the shared fullscreen viewer with zoom (pinch/wheel/buttons) + pan (pointer events, `touch-action:none`). Used by both chat images/videos (`ChatImage`, `video` prop) and mermaid diagrams (`svg` prop, ⤢ button + tap). Do NOT spawn a second lightbox.

More detail in ARCHITECTURE.md.

---

## Git

- Repo: `github.com/igdigitallab/cardloop`.
- `.gitignore` excludes: `.env`, `data/` (chat IDs/sessions/audit/logs), `venv/`, `web/node_modules`, `web/dist`, `.worktrees/`, and per-instance state (`TASKS.md`, `DONE.md`, `docs/internal/`).
- ⚠️ Before committing anything new: verify no secret/value landed in tracked files.
- ⚠️ **Anti-hardcode (the project ships as OSS).** No personal/infra hardcoding in tracked code/docs: paths → `$HOME`/relative (not `/home/<user>/…`), IDs/tokens/passwords → `.env` (+ a placeholder in `.env.example`), the project registry → `data/registry.json` (gitignored), operator name/language → env (`OPERATOR_NAME`/`RESPONSE_LANGUAGE`). The real operator value lives only in a gitignored config; the code reads it from there. Do not write a new personal/infra constant into code — parameterize it. Details & inventory → `docs/internal/specs/spec-014-oss-hardening.md`; multi-user → `spec-013-multi-user.md`.
- ⚠️ **English-only (the project ships in English).** All NEW code, comments, docstrings, log/print output, user-facing strings, UI, and docs MUST be in English. Do not add Russian text to the codebase. The agent's **reply** language is controlled separately by the `RESPONSE_LANGUAGE` env var (not hardcoded) — an operator may set it to any language, so the agent can still answer in that language while the code/UI stay English. Plan & progress → `docs/internal/specs/spec-015-oss-runtime.md`.
- Parallel agents → `isolation: worktree` (the Agent tool creates the worktree itself). A manual `git worktree add .worktrees/<name> -b <branch>` is only for a worktree needed without the Agent tool. Afterwards — `git worktree prune`.

---

## Operations

- Logs: `sudo journalctl -u cardloop -f` (or your unit name — see `CARDLOOP_SERVICE` in `.env`).
- Restart from an agent: `bash ./restart-self.sh` from the repo root (the ONLY safe way).
- Restart from a terminal: `sudo systemctl restart cardloop` (or your `CARDLOOP_SERVICE`).
- After editing `bot.py`/`webapp.py` — a service restart is mandatory.
- After editing `web/` — rebuild: `cd web && npm run build`.
- **Broken cockpit? `make doctor`** (`tools/doctor.py`, read-only, < 5s): versions/auth/config/service/runtime/data with a ✗/⚠ verdict + remedy per finding, `--json` for machine-readable, secrets always redacted, exit 1 on any ✗. Paste its output into a bug report before digging by hand.
- ⚠️ **A "frozen chat" / "dead browser pane" is often an OOM kill, not a UI bug.** Every live client is a whole CLI subprocess (~0.4-0.6 GB RSS, more with sub-agents), so `LIVE_CLIENT_MAX` bounds the COUNT while memory keeps climbing: on ops the cgroup hit `MemoryMax` twice (2026-08-26, 2026-08-27), the kernel killed a `claude` child mid-turn and systemd restarted the service — with `systemctl is-active` still saying `active` the whole time. `doctor` now escalates `MemoryCurrent` (⚠ ≥75%, ✗ ≥90%) and reports the cgroup's `oom_kill` counter plus the unit restart count; check those BEFORE debugging the frontend. Knobs: `LIVE_CLIENT_MAX`, `LIVE_CLIENT_TTL_SEC`, `LIVE_CLIENT_MEM_GUARD` (fraction of the cgroup limit above which idle clients are LRU-evicted before a new one connects).
- **Tests: `venv/bin/python -m pytest tests/`** (~2500, should be green). ⚠️ ONLY via the venv — it has `pytest-aiohttp` (requirements-dev.txt); the system `python` does NOT, so ~237 endpoint tests fall into a false `error`. Do not trust such a run and do NOT rewrite tests to fit it.
- **E2E smoke suite (spec-072, `tests/e2e/`):** `venv/bin/python -m pytest tests/e2e -m e2e` — opt-in, excluded from the default run above (`pytest.ini: addopts = -m "not e2e"`). Boots a REAL cockpit subprocess (own tmp `data/`+`$HOME`, random port/password, `E2E_FAKE_ENGINE=1` → scripted `e2e_fake_engine.py`, no SDK/tokens) and drives it with headless Playwright (`playwright install chromium` once). Requires `web/dist` to exist (`cd web && npm run build`) — the harness fails with a clear message otherwise.
- ⚠️ **Model-alias ground truth (`tools/verify_model_aliases.py`):** the UI label comes from the LIVE `/v1/models` listing, but the cockpit sends the BARE alias (`opus`) to the SDK and the **bundled CLI** decides what that alias means — a stale bundle silently runs an older model (`is_error=False`). Run `venv/bin/python tools/verify_model_aliases.py` (exit 1 = mismatch) or `venv/bin/python -m pytest tests/test_model_aliases.py -m aliases` after ANY model release, and bump the `claude-agent-sdk` floor when it flags. The offline half — the three static label spots staying in lockstep — is asserted by `test_static_label_spots_stay_in_sync` in the default run. A daily cron (08:25) runs `--watch`: ONE read-only `/v1/models` GET and zero model calls unless a model id we have never seen appears (state: `data/model-release-seen.json`), and only then does it probe. Mismatch → exit 1 (healthchecks alert) + `data/inbox/model-alias-mismatch.txt`.
- **SDK release watch (`_sdk_watch_loop` in webapp.py).** The alias watch above fires on new MODELS; this one fires on new SDK RELEASES, which ship far more often. One read-only GET to PyPI, cached 6h in `data/sdk-version.json`, surfaced in the sidebar version badge, as a once-per-release toast + Web Push, and as a `doctor` line. ⚠️ `doctor`'s other SDK check compares against **requirements.txt's own floor** — meeting the floor is not the same as being current, which is how the venv sat 17 releases behind before spec-085. Never auto-installs. `SDK_UPDATE_CHECK=0` disables it (no outbound request at all).
- **Deploy canary (spec-072, `restart-self.sh`):** pre-restart wait-for-idle (`GET /api/health?deep=1`, unauthenticated, `{ok, running:N}`) up to 10 min; post-restart health/log/smoke canary runs inside the detached transient unit and rolls back to the previous git tag ONCE on failure (rebuilds `web/`, restarts again, writes a red incident to the journal + `data/inbox/`). `CANARY_DRY_RUN=1` generates the canary script without invoking `systemd-run` (for testing). ⚠️ Smoke GETs retry up to 12×5s per endpoint — a one-shot smoke false-rolled-back a healthy deploy (2026-08-21: port binds ~3s after start but the event loop is busy with post-listen init for 10-15s); don't "simplify" it back to a single curl.

---

## Memory wiki (ingest / query / lint)

Native auto-memory (`~/.claude/projects/<slug>/memory/`) is per-project — a project never loads
another project's memory. What every session DOES load is `~/CLAUDE.md` plus that project's
`MEMORY.md` index, verbatim. So the index is a **routing table, not a summary**: one line per
article, hook under ~100 chars, detail in the article.

Auto-memory only ingests — it appends and never prunes. The missing third operation is lint:
`tools/memory-lint.py --dir <memory-dir>` (single) or `tools/memory-lint-all.sh` (every project,
weekly cron, report at `~/logs/memory-lint.md`). It never deletes; curation stays with the operator.

Rules that keep it lean:
- **No ledgers.** Progress/status notes for shipped work are what git is for. Distill the decisions
  and caveats into one durable article and delete the trackers (see `shipped-specs-durable-facts`).
- **Merge, don't blind-delete.** A "progress" note often hides a real gotcha; read before removing,
  and repoint inbound `[[wiki-links]]`.
- **Fix stale bodies.** A wrong memory is worse than none — it is loaded and believed.
- ⚠️ **Never `sed -i` across the whole memory dir.** `sed -i` rewrites every file it opens, match or
  not, so a bulk link-repoint stamps today's mtime on all of them and blinds the lint's
  `stale_by_age` check. Edit only the files that actually contain the pattern (`grep -l … | xargs sed -i`),
  or restore mtimes afterwards from a backup with `touch -r`.
- `agents_config.memory = "project"` disables native auto-memory for a project, leaving the curated
  `./.claude-ops/memory/` as its only brain (spec-078 Phase 3a).

## Agent skills

Engineering skills from `mattpocock/skills` (installed globally in `~/.claude/skills/`) read the
files under `docs/agents/` to fit this repo's workflow — keep those files current if the workflow changes.

### Issue tracker

Issues are **Cardloop board cards** (`TASKS.md`), not GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The five triage roles are a board vocabulary, not GitHub labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: architecture in `ARCHITECTURE.md`, decisions in `docs/internal/specs/`, traps in
`GOTCHAS.md`. See `docs/agents/domain.md`.

## Gotchas (don't step on these again)

### Auth & environment
- **Auth = subscription, NOT the API.** The SDK reads `~/.claude/.credentials.json` (claudeAiOauth). `ANTHROPIC_API_KEY` must NOT be set anywhere — `bot.py` explicitly `pop`s it, and it is not in the unit. Otherwise billing goes to the API.
- **systemd PATH.** The unit sets `PATH=$HOME/.npm-global/bin:...` and `HOME=/home/<user>` for access to the credentials. Note: the SDK does NOT find `claude` via PATH; it prefers its own bundled binary at `venv/lib/python*/site-packages/claude_agent_sdk/_bundled/claude` (PATH is only a fallback if the bundle is absent).
- **bypassPermissions + full-auto.** The bot pushes/deploys/deletes on its own. Irreversible actions are reported after the fact (⚠️ footer). Access is gated by `WEB_PASSWORD` (web cockpit login) + optional TOTP.

### Restart & cgroup
- **SELF-restart = suicide.** The bot lives in its systemd service's cgroup. Any `systemctl stop/restart/kill` OR `kill/pkill` of its own process from its own shell tears down the cgroup MID-command → `stop && start` never reaches `start`. **Guard:** the PreToolUse hook `~/.claude/hooks/guard-self-lifecycle.sh` blocks such Bash commands. **For edits — use only `bash restart-self.sh`** (detached via `systemd-run`, outside the cgroup).
- **A restart ABORTS the current turn + all sub-agents.** Even a correct `bash restart-self.sh` kills the agent's Python process. Rules: (1) Before `restart-self.sh` — send the operator the full summary and finish the turn. (2) If there are `in_progress` sub-agents — wait for them to finish. (3) After `restart-self.sh` — no more Bash commands in this turn. (4) Smoke / `curl /api/health` — in the next message.
- **pkill footgun.** Do NOT `pkill -f "bot.py"` — the pattern matches the command line of the command itself and kills the shell (exit 144). Stop via systemd or by PID.
- **`MemoryHigh` below `MemoryMax` = whole-cockpit livelock.** `MemoryHigh` throttles *every* task in the cgroup instead of killing the offender, so the cgroup never reaches `MemoryMax` and the OOM killer never fires: one runaway sub-agent parks `bot.py` in uninterruptible sleep (`wchan: mem_cgroup_handle_over_high`) and the cockpit stops answering — while `systemctl is-active` still says `active`. Keep `MemoryHigh=infinity` and let `MemoryMax` bound the blast radius to the single offending process. Diagnose with `memory.pressure` (`full avg10` near 100 = frozen), not with CPU or service status.
- **Wide-context grep on a minified bundle eats gigabytes.** A pattern like `.{0,500}TOKEN.{0,500}` against a one-line bundle (`node_modules/**/*.js`) makes `ugrep` buffer the whole file per match — 3–4 GB RSS in seconds, enough to blow the cgroup above. The spawned process reports `comm=claude` (bundled binary), so `pkill -x ugrep` will NOT match it — kill by PID. To read a minified file, slice it (`python -c` / `head -c`) instead of grepping with context.
- **`claude-agent-sdk` >= 0.2.144 is required (history: >=0.2.96 fable/spec-017, >=0.2.110, >=0.2.129, >=0.2.143 spec-085).** An old SDK silently substitutes a different model with no error (`is_error=False`). **The SDK's BUNDLED CLI determines model-alias resolution** — old bundles resolve `sonnet`→`claude-sonnet-4-6` and lack Sonnet 5; keep the SDK fresh after model releases. After recreating the venv: `pip install -U "claude-agent-sdk>=0.2.144"`. Symptoms of a stale bundle: sub-agents billed on a previous-generation model, or session replies "issue with the selected model".

Subsystem gotchas (concurrency, security/detectors, C2-gate/worktree, memory, secrets, misc, audit, project binding, templates) → **GOTCHAS.md**.

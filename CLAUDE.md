> CLAUDE.md = working rules and gotchas for agents. Code map → ARCHITECTURE.md. API → docs/API.md. Setup → CONTRIBUTING.md. Subsystem gotchas → GOTCHAS.md.

# CLAUDE.md — Cardloop

An IDE environment for managing projects via the Claude Agent SDK. Two channels: the cockpit (`YOUR_DOMAIN`) and kanban auto-run. One engine, `run_engine()`, full-auto.

Design history & specs: `docs/internal/specs/` (gitignored).

---

## What goes where (quick map)

- `bot.py` — web-only launcher: loads env/auth, builds ctx, starts the web cockpit. The engine lives in `engine.py` (async event generator `{tool|text|result|rate_limit|error}`, transport-independent). Consumers: `_run_card` and `api_project_chat` (webapp.py). `running[k]=True` is reserved SYNCHRONOUSLY before the first await.
- `webapp.py` — the aiohttp cockpit. It does **NOT** import `bot.py` — everything comes through `ctx` (a dict of references: topics/sessions/running/resolve_project/run_engine/DATA/…) passed in from `bot.py`.
- `data/topics.json` — **LAYER 1**: binding `"chat:thread" → {project,cwd,model}`. Permanent; `/reset` does not touch it.
- `data/sessions.json` — **LAYER 2**: `"chat:thread" → session_id`. Cleared only by `/reset`.
- `data/prompts.json` — cockpit prompt templates (CRUD via `/api/prompts`). **Not in git.**
- `cardloop.service` → `/etc/systemd/system/` (unit name overridable via `CARDLOOP_SERVICE`).
- `web/src/components/markdown.tsx` — the shared `mdComponents` for ALL `<ReactMarkdown>` instances (Files/CLAUDE.md/Board/Memory/Chat). Renders ```mermaid blocks as live SVG: `mermaid@11` lazily (`await import` → its own chunk, doesn't bloat the main bundle), `securityLevel:'strict'`, `suppressErrorRendering:true` (on a syntax error, falls back to the source, no "bomb"). ⚠️ A new `<ReactMarkdown>` must be wired with `components={mdComponents}`, otherwise diagrams won't render.
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
- **Tests: `venv/bin/python -m pytest tests/`** (~1400, should be green). ⚠️ ONLY via the venv — it has `pytest-aiohttp` (requirements-dev.txt); the system `python` does NOT, so ~237 endpoint tests fall into a false `error`. Do not trust such a run and do NOT rewrite tests to fit it.
- **E2E smoke suite (spec-072, `tests/e2e/`):** `venv/bin/python -m pytest tests/e2e -m e2e` — opt-in, excluded from the default run above (`pytest.ini: addopts = -m "not e2e"`). Boots a REAL cockpit subprocess (own tmp `data/`+`$HOME`, random port/password, `E2E_FAKE_ENGINE=1` → scripted `e2e_fake_engine.py`, no SDK/tokens) and drives it with headless Playwright (`playwright install chromium` once). Requires `web/dist` to exist (`cd web && npm run build`) — the harness fails with a clear message otherwise.
- **Deploy canary (spec-072, `restart-self.sh`):** pre-restart wait-for-idle (`GET /api/health?deep=1`, unauthenticated, `{ok, running:N}`) up to 10 min; post-restart health/log/smoke canary runs inside the detached transient unit and rolls back to the previous git tag ONCE on failure (rebuilds `web/`, restarts again, writes a red incident to the journal + `data/inbox/`). `CANARY_DRY_RUN=1` generates the canary script without invoking `systemd-run` (for testing).

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
- **`claude-agent-sdk` >= 0.2.110 is required (was >=0.2.96 for fable/spec-017).** An old SDK silently substitutes a different model with no error (`is_error=False`). **The SDK's BUNDLED CLI determines model-alias resolution** — old bundles resolve `sonnet`→`claude-sonnet-4-6` and lack Sonnet 5; keep the SDK fresh after model releases. After recreating the venv: `pip install -U "claude-agent-sdk>=0.2.110"`. Symptoms of a stale bundle: sub-agents billed on a previous-generation model, or session replies "issue with the selected model".

Subsystem gotchas (concurrency, security/detectors, C2-gate/worktree, memory, secrets, misc, audit, project binding, templates) → **GOTCHAS.md**.

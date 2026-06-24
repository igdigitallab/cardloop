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
- `claude-ops-bot.service` → `/etc/systemd/system/`.
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

- Logs: `sudo journalctl -u claude-ops-bot -f`
- Restart from an agent: `bash $HOME/claude-ops-bot/restart-self.sh` (the ONLY safe way).
- Restart from a terminal: `sudo systemctl restart claude-ops-bot`.
- After editing `bot.py`/`webapp.py` — a service restart is mandatory.
- After editing `web/` — rebuild: `cd web && npm run build`.
- **Tests: `venv/bin/python -m pytest tests/`** (~1400, should be green). ⚠️ ONLY via the venv — it has `pytest-aiohttp` (requirements-dev.txt); the system `python` does NOT, so ~237 endpoint tests fall into a false `error`. Do not trust such a run and do NOT rewrite tests to fit it.

---

## Gotchas (don't step on these again)

### Auth & environment
- **Auth = subscription, NOT the API.** The SDK reads `~/.claude/.credentials.json` (claudeAiOauth). `ANTHROPIC_API_KEY` must NOT be set anywhere — `bot.py` explicitly `pop`s it, and it is not in the unit. Otherwise billing goes to the API.
- **systemd PATH.** The unit sets `PATH=$HOME/.npm-global/bin:...` — otherwise the SDK won't find the native `claude` binary. And `HOME=/home/<user>` for access to the credentials.
- **bypassPermissions + full-auto.** The bot pushes/deploys/deletes on its own. Irreversible actions are reported after the fact (⚠️ footer). Access is gated by `WEB_PASSWORD` (web cockpit login) + optional TOTP.

### Restart & cgroup
- **SELF-restart = suicide.** The bot lives in its systemd service's cgroup. Any `systemctl stop/restart/kill` OR `kill/pkill` of its own process from its own shell tears down the cgroup MID-command → `stop && start` never reaches `start`. **Guard:** the PreToolUse hook `~/.claude/hooks/guard-self-lifecycle.sh` blocks such Bash commands. **For edits — use only `bash restart-self.sh`** (detached via `systemd-run`, outside the cgroup).
- **A restart ABORTS the current turn + all sub-agents.** Even a correct `bash restart-self.sh` kills the agent's Python process. Rules: (1) Before `restart-self.sh` — send the operator the full summary and finish the turn. (2) If there are `in_progress` sub-agents — wait for them to finish. (3) After `restart-self.sh` — no more Bash commands in this turn. (4) Smoke / `curl /api/health` — in the next message.
- **pkill footgun.** Do NOT `pkill -f "bot.py"` — the pattern matches the command line of the command itself and kills the shell (exit 144). Stop via systemd or by PID.
- **`claude-agent-sdk` >= 0.2.96 is required for fable (spec-017).** An old SDK (<=0.2.87) does NOT know the `fable`/`claude-fable-5` model and SILENTLY substitutes opus (no error, `is_error=False`) — the orchestrator quietly degrades. The CLI does know the alias, which is misleading. After recreating the venv: `pip install -U "claude-agent-sdk>=0.2.96"`. Symptom: the session replies "issue with the selected model" or introduces itself as Opus.

Subsystem gotchas (concurrency, security/detectors, C2-gate/worktree, memory, secrets, misc, audit, project binding, templates) → **GOTCHAS.md**.

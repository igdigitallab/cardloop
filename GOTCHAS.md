# GOTCHAS.md — Cardloop subsystem gotchas

Subsystem-level gotchas. Turn-1 safety guards (Auth, Restart/cgroup) live in CLAUDE.md.

---

### Concurrency & state
- **Concurrency race.** The slot reservation `running[k]=True` is set SYNCHRONOUSLY in `on_message` before the first `await`. `safe_run` clears it in `finally`. Two fast messages → the second gets "already working".
- **The board wipes agents' tasks.** `GET /tasks` parses → canonicalizes → rewrites. If an agent wrote bullets `- text` without `[ ]`, `_CARD_RE` didn't match → 0 cards → the whole file got wiped. Three layers of protection: (1) `_PLAIN_CARD_RE` accepts checkbox-less bullets; (2) `_count_potential_cards(raw)` skips the write if `parsed < potential`; (3) a per-cwd `asyncio.Lock` serializes write operations.
- **Front-state hygiene.** Don't reset `activeId === '__global__'` in cleanup; a mounted tab uses `display:none`; `busActiveRef` is restored from `GET /api/projects/{id}/running` on ChatTab mount; the TASKS.md write is skipped if the file changed externally.

### Security
- **The "irreversible" detector — exact substrings.** Do NOT use `-f `/`rm `/`kill ` (they catch `tail -f`, `perform`, etc.). Only `rm -rf`/`rm -f`/`git push`/`--force` and the like.
- **Anti-traversal.** `_resolve_safe` / `_resolve_global_safe` — resolve+startswith with a trailing slash. `.env*` → 403 (except `.env.example`). `.git/venv/node_modules/dist/__pycache__` are hidden + 403.
- **card_id is validated** by `_valid_card_id`/`_CARD_ID_RE` (prevents path injection via card_id).

### C2-gate: worktree mode for cards
- **Mode detector**: git repo + clean tree → `worktree`; otherwise → `legacy` (run directly in cwd).
- **Worktree lifecycle**: setup in `.worktrees/card-<id>` → run the agent on branch `card-<id>` → auto-commit → a `.json` sidecar with `mode/has_changes/applied/discarded`.
- **The worktree is NOT deleted** after the run — it stays until apply/discard.
- **apply**: `merge --no-ff card-<id>` into main; conflict → 409, `merge --abort`, worktree survives. apply-success → worktree+branch deleted, card → Done.
- **discard**: worktree+branch deleted, card → Backlog.
- **Orphan worktrees** after a crash: they stay on disk in `.worktrees/`. Cleanup is in Backlog (not this iteration).
- **NEVER** `git branch -D` on branches other than `card-*` (the pattern is validated by `_valid_card_id`).
- **Quality gate (Spec 009):** `POST .../check` → `_run_quality_gate(wt_path)` runs the tests IN the worktree (not the main tree). The verdict `safe/risky/unknown` is stored in `meta.gate`. Apply is **NOT blocked** — the user decides. The gate is not built into apply — only via an explicit "🧪 Check". Linting is out of scope (iteration 1).

### Project memory (Spec 006)
- **Memory lives in the repo, NOT in `~/.claude`.** New location: `<cwd>/.claude-ops/memory/` — committed to git. The old one (`~/.claude/projects/<cwd>/memory/`) is a read-only fallback for GET (backward compatibility). Don't confuse them.
- **The agent writes via Write.** No special agent API needed — it writes `.claude-ops/memory/<slug>.md` with a normal Write. The engine system prompt reminds it in one line.
- **MEMORY.md = an auto-index.** Rebuilt on every write/delete. Do NOT edit by hand — it gets overwritten. Entries go in slug files with frontmatter (type/created).
- **Slug validation:** `^[a-z0-9][a-z0-9-]{0,60}\.md$` + `MEMORY.md`. Uppercase / traversal (`../`) → 400.

### Project secrets (Spec 007)
- **We never return values via the API.** GET `/secrets` returns key names only (`keys:[...]`). No `values`, `data`, or `secrets_map` — names only. The test `test_api_secrets_get_returns_only_names` locks this in as a regression.
- **Secrets are not in audit/git.** `audit()` accepts only (project, kind, text) — env is never passed to it. `secrets.env` is gitignored automatically on the first write.
- **Keys are strictly `^[A-Z_][A-Z0-9_]*$`.** Lowercase, hyphen, space, traversal `..` → 400. This is env-injection protection.
- **cwd isolation is hard.** `_secrets_read(cwd)` reads only `.claude-ops/secrets/secrets.env` inside this project's cwd — no leakage between projects.
- **Current TabIds:** `claude-md | logs | board | files | memory | timeline | settings` (7 tabs; `secrets` is now a section in "Settings", not a tab; `overview` moved to "Settings" → "Project info"; "Feed" → "Activity" — Spec 011 Ph2).

### Misc
- **error_middleware catches EVERYTHING → a benign disconnect = false incidents.** The global `error_middleware` (Ph0) logs unhandled exceptions as the line `UNHANDLED exc_class=...`, which the scanner parses → a card in Failed. A client closing an SSE tab → `ConnectionResetError`/`ClientConnectionResetError` ("Cannot write to closing transport"). These are benign: the middleware RE-RAISES them (no 500, no log), and the stream handlers themselves (`_sse_stream` heartbeat, `api_project_chat._send`) wrap `resp.write` in `try/except (ConnectionResetError, ConnectionAbortedError)`. When you add a new stream endpoint — do the same, otherwise you'll flood the board with false err-cards (it was: 124+ overnight). `asyncio.CancelledError` is a BaseException and passes `except Exception` on its own.
- **Incident card_id = `err-<hash6>`.** `_CARD_ID_RE = ^(err-)?[a-f0-9-]{4,20}$` — the `err-` prefix is allowed explicitly (non-hex letters would otherwise break validation → move/delete/update of incidents returned 400 and they piled up in Failed). A body with no dots/slashes → traversal is impossible.
- **Limit percentages are NOT from the SDK.** The passive `RateLimitEvent` from the SDK gives only `status`+`resets_at`, with `utilization=None`. The source of % is the oauth endpoint `GET https://api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`). `webapp.py:api_usage` fetches it (60s cache).
- **LogsTab: `log_cmd` in topics.json.** The "Logs" tab runs `log_cmd` via subprocess (8s timeout, takes the last 300 lines). If unset — empty state. To set it: add `"log_cmd": "journalctl -u my-service -n 300 --no-pager"` for the project in `data/topics.json`. journalctl works without sudo when the service user is in the `adm` group; the services run under that same user.
  - **`topics.json` is now hot-reload (no restart needed).** Originally `topics` was loaded once at startup into the in-memory dict `ctx["topics"]`, and a direct Edit/Write of the file was invisible until a restart (an agent got burned by exactly this). Fixed: `_maybe_reload_topics(ctx)` (webapp.py, called at the start of `_collect_projects`) re-reads the file from disk behind an mtime gate and updates `ctx["topics"]` IN-PLACE (`clear()`+`update()`). Disk is authoritative (`save_topics()` always writes there). A broken/partial file during a race → JSONDecodeError → we silently keep the current version. **A direct edit of topics.json is picked up on the fly.**
  - **The project id in the API = basename of cwd, NOT the `project` field.** `/api/projects/<id>/logs` expects `networking-os`, not `Networking-OS` (`_project_id(cwd)`). The frontend sends the basename itself; this matters for manual curl.
  - **The "configure logs" button (LogsTab.tsx) hands the agent a full instruction.** The empty state creates a backlog card: a short `text` (title) + a detailed `description` (how to choose log_cmd/test_cmd: systemd/docker/file, exec-without-sudo-without-shell, mandatory output check, test_cmd relative to the project cwd, hot-reload instead of restart). `_run_card` joins the prompt = `text + "\n\n" + description`. A multi-line description round-trips through TASKS.md (`  > line` per line; blank lines too, `_DESC_LINE_RE=^  > (.*)$`). Do NOT squash it back into a one-liner — the agent would then do it wrong again.
- **Timeline (Spec 008): `data/timeline/<slug>.jsonl`.** Every `_bus_publish` event is persisted. Slug = `cwd.replace('/', '-')`. Rotation at >5MB → `.jsonl.1` (one; the old `.1` is overwritten). The write swallows all exceptions (the run never breaks). The env field is never written. Init: `_timeline_init(ctx)` in `start()`. `_TIMELINE_DATA_DIR` / `_TIMELINE_TOPICS` are module variables (None until init — correct).
- **Current TabIds:** `claude-md | logs | board | files | memory | timeline | settings` (7 tabs; `secrets` is a section in "Settings", not a tab; `overview` moved to "Settings" → "Project info" — Spec 011 Ph2).

---

## Audit / watchdog / files

- **Audit log:** `data/audit/audit-YYYY-MM.log` — per task: `TASK` (prompt), `BASH`/`BASH⚠️` (⚠️=irreversible), `EDIT/WRITE` (files), `DONE`.
- **Watchdog:** no SDK events for `STALL_SECONDS` (300s) OR total > `MAX_SECONDS` (1800s) → `client.interrupt()` + "⚠️ auto-interrupted by watchdog".
- **File intake:** files uploaded via the cockpit are stored in `data/inbox/` (max 20 MB). The inbox grows — add cleanup if desired.

---

## Project binding

Projects are registered in `data/registry.json` (gitignored) or auto-scanned from `~` by basename. A new project → add an alias in the registry or let the scan pick it up.

---

## Project templates

`templates/*.tpl` — starters for new projects (the "+ New project" button):
- `CLAUDE.md.tpl` · `TASKS.md.tpl` · `README.md.tpl` · `.gitignore.tpl`
- Variables `{{name}}` / `{{date}}` / `{{slug}}` → `_render_template` in webapp.py.
- **`CLAUDE.md.tpl` contains a "Cockpit Rules" section** — copied into every new project. Do NOT remove it (the conformance check in `webapp.py` greps for that exact heading).

`templates/reference/` — reference templates bundled with the project:
- `project-baseline.md` · `audit-prompt.md` · `triage-prompt.md` · `refactor-prompt.md` · `spec.md` · `project.md`
- Loaded at runtime by the cockpit's audit feature, so they must stay in English.

# GOTCHAS.md — Cardloop subsystem gotchas

Subsystem-level gotchas. Turn-1 safety guards (Auth, Restart/cgroup) live in CLAUDE.md.

---

### Concurrency & state
- **Concurrency race.** The slot reservation `running[k]=True` is set SYNCHRONOUSLY in `on_message` before the first `await`. `safe_run` clears it in `finally`. Two fast messages → the second gets "already working".
- **The board wipes agents' tasks.** `GET /tasks` parses → canonicalizes → rewrites. If an agent wrote bullets `- text` without `[ ]`, `_CARD_RE` didn't match → 0 cards → the whole file got wiped. Three layers of protection: (1) `_PLAIN_CARD_RE` accepts checkbox-less bullets; (2) `_count_potential_cards(raw)` skips the write if `parsed < potential`; (3) a per-cwd `asyncio.Lock` serializes write operations.
- **Front-state hygiene.** Don't reset `activeId === '__global__'` in cleanup; a mounted tab uses `display:none`; `busActiveRef` is restored from `GET /api/projects/{id}/running` on ChatTab mount; the TASKS.md write is skipped if the file changed externally.

### Security
- **`can_use_tool` is SHADOWED under `bypassPermissions`** (the SDK says so out loud:
  `CanUseToolShadowedWarning`, `types._get_can_use_tool_shadowed_warning`). A gated turn — plan
  mode or spec-082 ask mode — must connect with `permission_mode="plan"` / `"default"`, never
  bypass, or the gate is never consulted and every tool runs full-auto **with no error**. Two
  more shadow sources on `default`: a whole-tool entry in `allowed_tools` (including the bare
  `Skill` the SDK appends when `skills="all"`), and allow rules in the operator's own settings
  files — those are invisible to the warning. `permission_mode` is part of the live-client
  fingerprint, so toggling a gate reconnects the client; a client PINNED by running background
  children is reused instead, which is why a gated turn aborts/queues in that case.
  Verified live: under `"default"` the gate IS consulted for `Write` **even when the operator's
  `~/.claude/settings.json` sets `permissions.defaultMode: "bypassPermissions"`** (the flag
  outranks the settings file — no inline `--settings` needed), but it is NOT consulted for a
  harmless `Bash(echo …)`: the CLI auto-approves commands it classifies as safe before the
  callback. Ask mode therefore gates mutations, not literally every tool call.
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
- **The browser module needs `playwright` at RUNTIME, but it only ships in `requirements-dev.txt`.** Every tier imports `playwright.async_api` — `builtin` and `external-cdp` directly, and the CloakBrowser tier still routes through it whenever a Manager profile is configured. A venv built from `requirements.txt` alone therefore fails every browser call with `No module named 'playwright'` while `modules.json` still reports the browser module as enabled, so the pane looks configured and is dead. Fix: `venv/bin/pip install playwright`. `playwright install chromium` is NOT needed for `external-cdp`/Manager profiles — those attach to a REMOTE Chrome; only the `builtin` tier downloads a local browser.
- **`default_profile` silently overrides the selected backend.** `resolve()` returns `external-cdp` for every project the moment a Manager profile is set (per-project mapping, else `default_profile`), so `"backend": "cloakbrowser"` in `modules.json` is inert while a default profile exists — the local `cloakbrowser` package is never even imported. Debug against the backend the acquire log line reports, not the one the settings UI shows.
- **Cloak Manager over a CDN: REST answers 200, the CDP WebSocket gets 403.** `GET /api/profiles/<id>/cdp` through `https://cloak.coscore.us` returns its JSON descriptor fine, but the WebSocket upgrade to the same URL is rejected by Cloudflare — so the failure looks like a Manager/auth problem and is not one (auth failures come back as 401, seen separately when the token is stale). Point `CLOAK_MANAGER_CDP_BASE` at an address that bypasses the CDN (the Manager's Tailscale peer works) and leave the REST base on the public URL. Verify a candidate address with a real `connect_over_cdp` before enabling it — a stale override that no longer resolves breaks the pane just as thoroughly.
- **The screencast's CDP session dies independently of Playwright's own page session — agent control survives, the operator's view goes dark, silently.** `browser_pane.py`'s `_bind_active()` opens a SECOND, manual CDP session (`context.new_cdp_session(page)`) just to carry `Page.startScreencast` + the operator's raw mouse/key input; the agent's tools (`navigate`/`click`/`type_text`/`snapshot` in `browser_tools.py`) never touch it — they ride Playwright's OWN, separately managed session on `self._page`. A renderer crash, a cross-process navigation target swap, or a blip reattaching to the remote external-cdp host can kill the manual session alone (Playwright's `CDPSession` emits a `"close"` event for it) while the browser/page stay perfectly alive: the agent keeps driving successfully, and the screencast — a passive event stream — just stops being fed with nothing to raise or log. `_rearm_screencast()` (spec: 2026-08-27 self-heal) re-creates that session on the SAME page with bounded retries (`_REARM_DELAYS`), gen-guarded (`_cdp_gen`) so our OWN intentional detach on a normal tab switch — which fires the identical `"close"` event — doesn't trigger a needless re-arm; a subscriber WebSocket is also resolved to one `BrowserSession` object for its whole life (`api_browser_ws` in webapp.py, never re-resolved), so `close()` now notifies + closes every subscriber before tearing a session down, or an already-open pane WS would sit silent forever pointed at a corpse while a fresh session quietly took over for the agent. Only once every retry fails does the pane show a visible error — never a silently frozen frame.

### Misc
- **The cockpit goal overlay (spec-076) was REMOVED (2026-07-22).** The custom per-chat goal — pinned bar, `/goal` chat-interception, `chats.json` `goal` record, the `run_engine(goal=...)` Stop-hook composed into `--settings`, and the `goal_status` events — is gone: it never kicked off work on set and never updated status on completion, so the operator cut it. `_compose_settings` now takes only `ultracode`. What remains is the CLI's OWN native `/goal` (typed text passes straight through to the bundled CLI). ⚠️ That native goal lives ONLY in CLI session memory — the cockpit can't see or clear it, so a stray `/goal` becomes an unclearable "ghost" Stop hook that only a session reset drops (this is exactly the bug the overlay was built to avoid; removing the overlay re-exposes it).
- **Ultracode = the CLI's NATIVE settings switch, not our prompt (spec-058 v2).** `run_engine(ultracode=True)` passes `ClaudeAgentOptions.settings='{"ultracode": true}'` (inline JSON → CLI `--settings`) and NO `--effort` — the flag pins xhigh internally and a CLI effort flag would OVERRIDE that pin. Do not "simplify" to `effort="ultracode"` (headless `--print` rejects it: "Unknown --effort value") and do not re-grow ULTRACODE_PROMPT into an orchestration contract — the Workflow tool's own Ultracode section is the contract; our append is a thin complement (roster + reporting rules). Works on opus (Workflow tool verified live on `claude-opus-4-8`).
- **error_middleware catches EVERYTHING → a benign disconnect = false incidents.** The global `error_middleware` (Ph0) logs unhandled exceptions as the line `UNHANDLED exc_class=...`, which the scanner parses → a card in Failed. A client closing an SSE tab → `ConnectionResetError`/`ClientConnectionResetError` ("Cannot write to closing transport"). These are benign: the middleware RE-RAISES them (no 500, no log), and the stream handlers themselves (`_sse_stream` heartbeat, `api_project_chat._send`) wrap `resp.write` in `try/except (ConnectionResetError, ConnectionAbortedError)`. When you add a new stream endpoint — do the same, otherwise you'll flood the board with false err-cards (it was: 124+ overnight). `asyncio.CancelledError` is a BaseException and passes `except Exception` on its own.
- **Incident card_id = `err-<hash6>`.** `_CARD_ID_RE = ^(err-)?[a-f0-9-]{4,20}$` — the `err-` prefix is allowed explicitly (non-hex letters would otherwise break validation → move/delete/update of incidents returned 400 and they piled up in Failed). A body with no dots/slashes → traversal is impossible.
- **Multiple subscriptions: `CLAUDE_CONFIG_DIR` is the only switch that works.** `accounts.py` binds a run to an account by injecting `CLAUDE_CONFIG_DIR` into `ClaudeAgentOptions.env`; `main` injects nothing, so a single-account install is unchanged. ⚠️ `CLAUDE_CODE_OAUTH_TOKEN` is **silently ignored whenever a `.credentials.json` sits in the config dir** (verified against the bundled CLI 2026-08-20: a deliberately invalid token + a valid file still ran fine) — "just pass another token" would keep billing the first account with no error. ⚠️ The account is part of the live-client fingerprint (`_compute_fingerprint(..., account=)`): `env` is deliberately excluded from that hash, so without the explicit field a connected subprocess would keep running on the OLD subscription after a switch. An extra config dir MUST symlink `projects/` back to `~/.claude/projects` — `engine._transcript_exists()` looks there, so a non-shared dir makes resume self-heal on wrong evidence and splits chat history in two. A non-active account's `accessToken` is only refreshed while that account actually runs, so its usage percentage is often unavailable — the UI shows `—`, it does not invent a number. Per-project pinning is a `account` key in `topics.json` (all topics with that cwd), threaded to `run_engine(project_account=...)` from all FOUR Claude call sites (chat, queue drain, card, deferred) — miss one and that entry point silently ignores the pin.
- **Limit percentages are NOT from the SDK.** The passive `RateLimitEvent` from the SDK gives only `status`+`resets_at`, with `utilization=None`. The source of % is the oauth endpoint `GET https://api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`). `webapp.py:api_usage` fetches it (60s cache).
- **LogsTab: `log_cmd` in topics.json.** The "Logs" tab runs `log_cmd` via subprocess (8s timeout, takes the last 300 lines). If unset — empty state. To set it: add `"log_cmd": "journalctl -u my-service -n 300 --no-pager"` for the project in `data/topics.json`. journalctl works without sudo when the service user is in the `adm` group; the services run under that same user.
  - **`topics.json` is now hot-reload (no restart needed).** Originally `topics` was loaded once at startup into the in-memory dict `ctx["topics"]`, and a direct Edit/Write of the file was invisible until a restart (an agent got burned by exactly this). Fixed: `_maybe_reload_topics(ctx)` (webapp.py, called at the start of `_collect_projects`) re-reads the file from disk behind an mtime gate and updates `ctx["topics"]` IN-PLACE (`clear()`+`update()`). Disk is authoritative (`save_topics()` always writes there). A broken/partial file during a race → JSONDecodeError → we silently keep the current version. **A direct edit of topics.json is picked up on the fly.**
  - **The project id in the API = basename of cwd, NOT the `project` field.** `/api/projects/<id>/logs` expects `networking-os`, not `Networking-OS` (`_project_id(cwd)`). The frontend sends the basename itself; this matters for manual curl.
  - **The "configure logs" button (LogsTab.tsx) hands the agent a full instruction.** The empty state creates a backlog card: a short `text` (title) + a detailed `description` (how to choose log_cmd/test_cmd: systemd/docker/file, exec-without-sudo-without-shell, mandatory output check, test_cmd relative to the project cwd, hot-reload instead of restart). `_run_card` joins the prompt = `text + "\n\n" + description`. A multi-line description round-trips through TASKS.md (`  > line` per line; blank lines too, `_DESC_LINE_RE=^  > (.*)$`). Do NOT squash it back into a one-liner — the agent would then do it wrong again.
- **Timeline (Spec 008): `data/timeline/<slug>.jsonl`.** Every `_bus_publish` event is persisted. Slug = `cwd.replace('/', '-')`. Rotation at >5MB → `.jsonl.1` (one; the old `.1` is overwritten). The write swallows all exceptions (the run never breaks). The env field is never written. Init: `_timeline_init(ctx)` in `start()`. `_TIMELINE_DATA_DIR` / `_TIMELINE_TOPICS` are module variables (None until init — correct).
- **Current TabIds:** `claude-md | logs | board | files | memory | timeline | settings` (7 tabs; `secrets` is a section in "Settings", not a tab; `overview` moved to "Settings" → "Project info" — Spec 011 Ph2).

---

## Audit / files

- **Audit log:** `data/audit/audit-YYYY-MM.log` — per task: `TASK` (prompt), `BASH`/`BASH⚠️` (⚠️=irreversible), `EDIT/WRITE` (files), `DONE`.
- **There is NO turn watchdog.** The stall interrupt was removed in spec-039 and the
  `MAX_SECONDS` "task ceiling" never had an enforcement site (both were deleted, along with
  their settings sliders, in the root-fix C cleanup). A stuck turn is bounded only by
  `LIVE_CLIENT_MAX_PIN_SEC` (4h, persistent clients) and `CARD_LINGER_MAX_SEC` (card linger).
  Do not assume a 5/30-minute watchdog will rescue a hung turn — it will not.
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

## spec-071: persistent-client stream drain (concurrency)

- **Exactly ONE consumer of `client.receive_messages()` at any time.** Between turns the
  drain (`engine._drain_between_turns`) owns the stream; `run_engine`'s live branch stops it
  before `client.query()` and restarts it in `finally`. NEVER add another reader (a second
  `receive_response`/`receive_messages` steals messages from the active consumer).
- Why the drain exists: the SDK's internal reader pushes messages into a BOUNDED buffer
  (`max_buffer_size=100`); unconsumed between turns it fills → reader blocks → CLI stdout
  pipe backs up → the CLI stalls (~1 tool round / 10 min for background sub-agents).
- The chat heartbeat pump in `api_project_chat` must never cancel the engine generator's
  `__anext__` mid-turn (that cancels the SDK receive) — pings are written while the pump
  task is pending; the task is only cancelled in the handler's `finally`.
- Terminal task states can arrive ONLY as `TaskUpdatedMessage.patch.status` (e.g. TaskStop →
  "killed", notification suppressed) — always handle BOTH message types.
- Test fakes: `MagicMock(spec=AssistantMessage)` MUST set `parent_tool_use_id = None`, or the
  spec-071 chat-lane filter silently skips the fake (truthy Mock attribute).

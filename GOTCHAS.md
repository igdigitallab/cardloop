# GOTCHAS.md — Cardloop subsystem gotchas

Subsystem-level gotchas. Turn-1 safety guards (Auth, Restart/cgroup) live in CLAUDE.md.

---

### Concurrency & state
- **Concurrency race.** The slot reservation `running[k]=True` is set SYNCHRONOUSLY in `on_message` before the first `await`. `safe_run` clears it in `finally`. Two fast messages → the second gets "already working".
- **The board wipes agents' tasks.** `GET /tasks` parses → canonicalizes → rewrites. If an agent wrote bullets `- text` without `[ ]`, `_CARD_RE` didn't match → 0 cards → the whole file got wiped. Three layers of protection: (1) `_PLAIN_CARD_RE` accepts checkbox-less bullets; (2) `_count_potential_cards(raw)` skips the write if `parsed < potential`; (3) a per-cwd `asyncio.Lock` serializes write operations.
- **⚠️ Never cancel the current task inside `_evict_live_client` (idle-TTL leaked every CLI it evicted).** `_idle_waiter` awaits the evictor from inside itself, so `entry.idle_task.cancel()` cancelled the very task running `disconnect()`. The raw asyncio cancel lands on the first suspension in the SDK's `close()` — its shield only defers anyio cancellation — and the terminate/kill escalation is skipped: the CLI + its MCP servers (~450 MB) live on, unregistered, with nothing logged (`CancelledError` is a `BaseException`, `except Exception` never sees it). Measured on ops 2026-10-01: 21/21 TTL evictions leaked, 0/138 memory-guard and 0/4 fingerprint evictions did; the cgroup sat at 97-100 %, so the memory guard then evicted the operator's real idle chats and every next message was a cold resume ("sessions keep dropping"). Symptom check: `claude` processes whose parent is the cockpit outnumber `LIVE_CLIENT_MAX`, with ages ≫ `LIVE_CLIENT_TTL_SEC`. Tests that fake `disconnect` with `AsyncMock` never yield, so the cancel has nowhere to land — use a client that suspends before it "reaps" (`tests/test_idle_evict_reaps_subprocess.py`).
- **Load monitor (spec-094) traps.** (a) `Monitor.sample()` does blocking `/proc` reads and a process walk: it runs in `asyncio.to_thread`, never on the loop it measures; the 1 s heartbeat that measures loop lag keeps the **max** over a minute (a p95 of 60 samples would discard one 3 s stall). (b) `oom` and `evictions` are sliding 15-minute windows — a lifetime delta would latch the meter red until restart. (c) `agents` is a headcount (observed `claude` processes vs `LIVE_CLIENT_MAX + chats in flight`), NOT a PID match against the registry: `_LiveEntry` has no PID, card runs are ephemeral and never registered, and `_evict_live_client` pops the entry before `disconnect()` finishes. (d) The quiet "elevated" notice fires only at warn — a crit episode already has its loud alert. (e) A RAM-backed temp dir is a signal on its own: ops had `/tmp` as a 12 GB tmpfs at 97 % (agent scratch), which is RAM/swap pressure AND `No space left` for every tool. (f) Pop-out windows must NOT mount the meter (they stay stream-free by design). (g) The debounce `_Tracker` steps DOWN without a sustain but never below the signal's current raw level, and a not-yet-sustained spike is never counted as a "clear" sample. (h) Memory-class crit (mem / mem_psi / oom) alerts after 20 s, everything else after 120 s; a crit/warn dip shorter than 30 s does not restart the clock; the quiet "elevated" notice only runs while the level sits at warn. (i) Alert cooldowns persist in `data/load_alert_state.json` so a deploy under a persistent red level does not re-push on every restart. (j) The client never "refreshes" stale data on tab wake: after a long pause what it holds IS old, so the meter says stale until the first poll lands; one 404 is a proxy hiccup, three in a row hide the meter, and it re-probes every ~60 s. (k) Known limit: `oom_kill` is per cgroup instance, so an OOM that killed the cockpit itself is invisible after the restart (`make doctor` shows `NRestarts`); a limit set on an ANCESTOR slice is not read. (l) **Disk is a RUNWAY, not only a percentage.** The same 89 % is a quiet weekend or a one-day problem, so `disk` also grades days-until-full. `disk_trend()` computes one rate per WHOLE-day baseline k = 1..3 (median free k days ago − median free now, over k days) and reports the MINIMUM: the rate has to hold across all of them, which is what separates a trend from a one-off step (100 GiB copied yesterday = 100/day over 1 d but 33/day over 3 d). Baselines are multiples of 24 h on purpose — a nightly backup that writes 10 GiB at 02:00 and prunes at 03:00 is at the same phase at both ends and reads as zero growth (a 12 h baseline reads it as +20 GiB/day). A SHORT window is deliberately NOT used to catch a sudden fill: the nightly ~6 GiB `stacks.tar.gz` pass would read as 60 GiB/day at 4 am and cry wolf every night; a fast fill is the static fullness rule's job, and the 1-day rate is shown in the text when it is > 1.5× the sustained one. It needs two baselines (≥ 2 days of history), goes silent when the newest point is > 2 h old, a hole in one baseline just drops that baseline, and it only speaks when free < 15 % and the rate ≥ 256 MiB/day (below that it is churn). Warn < 7 d, crit < 2 d; the old static rule (≥ 90 % & < 20 GiB = warn, ≥ 97 % = crit) still wins when worse. The runway's own debounce (600 s warn / 120 s crit) applies ONLY to runway-driven levels (a private `_sustain` key on the signal, stripped before the API): the static rule stays immediate, which `make doctor` relies on (fresh Monitor, two samples 1 s apart). History = `data/load_disk_history.json` (one point/10 min, 4 days, keyed by `st_dev`; a `total` change > 1 % starts over, smaller jitter — ZFS/btrfs/NFS — does not, or the runway would never accumulate and the file would be rewritten every sample; a wall-clock step back drops the "future" points; a corrupt file is ignored with a journal line). It is persisted because every deploy restarts the process and would otherwise reset a 3-day baseline. Measured 2026-10-02 on ops: mtime churn of 1–12 GiB/day is mostly nightly tarballs being OVERWRITTEN, not growth — do not infer a fill rate from `find -newermt`; let the history say. (m) **`slab_reclaimable` is not working set.** dentry/inode caches fill when anything walks the disk (a `find /`, a backup) — measured: slab 1.0 → 1.9 GiB during a scan moved the old formula by +1.9 GiB while the real working set stayed put. `_working_set` now subtracts it (as the kernel's own `MemAvailable` counts it); this changes the memory GUARD too, which shares the function, and `make doctor` (it judged raw `MemoryCurrent` and called a healthy host "88 % of MemoryMax"). (n) Every signal transition, alert (full text), delivery hand-over (inbox / toast `queued for N open tab(s)` / push `attempted for N subscription(s), no delivery receipts` — neither leg gives a receipt, so the line never claims "delivered"), event-loop stall ≥ 0.5 s (always written when ≥ 2 s) and a 15-min status line go to the journal under `[load-monitor]`; transition lines are capped per signal (8 per 10 min, then one "changing level often" line) and `journal.say()` never raises (an EPIPE must not kill the heartbeat — a frozen heartbeat reads as a growing loop stall) — before this the log said only "ALERT: server overloaded" with no reason, and 8 of those fired for one /tmp tmpfs. `journalctl -u cardloop | grep load-monitor` is the history. (o) Sizes are labelled GiB/MiB (they were always binary); the `agents` row shows the headcount only, because `X/Y` sat next to "chats live 3/8" with a different Y (cap + chats in flight).
- **⚠️ `bot._amain` must be the ROOT task (CI red on 3.11 from 2026-10-03 until spec-096 P0).** Its Phase 3 cancels every task except `current_task()`. `asyncio.wait_for(coro)` runs `coro` in a CHILD task on Python 3.11 (inline only from 3.12), so a test that wrapped `_amain` in `wait_for` had its own task cancelled and died with `CancelledError` (then "Event loop is closed" in teardown) on 3.11 only. Production is unaffected (`main()` = `asyncio.run(_amain())`). Tests `await bot._amain()` directly in the test task with a `loop.call_later(..., task.cancel)` watchdog; never `wait_for`/`create_task` it.
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
- **`allowed_tools=[]` grants EVERY tool, it does not remove them.** The SDK only emits
  `--allowedTools` for a non-empty list, and `allowed_tools` is an auto-approve list, not a
  whitelist anyway. A "no tools" helper built that way got the CLI's full default toolset plus
  every user/claude.ai MCP server (183 tools incl. Bash/Edit/Write, mail, SMS; ~18.5k schema
  tokens per call), and the board reconciler ran it under `bypassPermissions` on text that can
  carry untrusted web content. Zero tools = `engine.HELPER_NO_TOOLS` (`tools=[]` →
  `--tools ""`, plus `--strict-mcp-config`); `tests/test_helper_no_tools.py` asserts the argv.
  Found by `/claude-api prompt-audit`, 2026-09-23.
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
- **Every live client spawns its OWN copy of every user-scope stdio MCP server.** A server registered
  in `~/.claude.json` → `mcpServers` starts once per CLI subprocess, i.e. once per live chat (and per
  account profile the accounts mirror copies it into). On ops six such servers cost ~360 MB per chat,
  more than the `claude` process itself; at the 2026-09-24 OOM they were 87 processes / 6.5 GB of the
  10 GB cgroup. Keep only broadly used servers in user scope; put narrow ones in the `.mcp.json` of
  the projects that use them, and pre-approve their names with `enabledMcpjsonServers` in the shared
  `~/.claude/settings.json` — an unapproved project server stays "Pending approval" and never starts
  in a headless SDK session. HTTP servers (`type: http`) spawn nothing and cost no memory.
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

### Grok provider (spec-095)

Operator runbook → `docs/GROK.md`. Everything below was measured on grok 1.0.46 unless it says otherwise.

- ⚠️ **`tools/grok-acct login` runs in another process and cannot reset the cockpit's registry cache.** A negative `provider_info` row is therefore re-probed after `_REGISTRY_FAIL_TTL_SEC` (15 s); only an AVAILABLE row keeps the 300 s TTL. Before this (2026-10-03) the picker said "off" for five minutes after a successful login.

**Protocol (ACP over stdio)**
- ⚠️ **An unanswered `session/request_permission` HANGS the turn** (no stopReason, no error; seen ≥ 75 s). Only OUR `session/cancel`, a `reject-once` reply, a `-32601` reply or `outcome:"cancelled"` end it, and all four end as `stopReason:"cancelled"`. `_meta.yoloMode:true` on `session/new` suppresses the request, so it is mandatory on every turn; the engine still answers `allow_once` if one arrives. A `cancelled` we did not ask for is an ERROR event, never a clean result (it would look like a truncated success); the decision is our own `cancel_requested` flag, `cancellationCategory` (`MidTurnAbort` / `PermissionRejected` / `PermissionCancelled`) is diagnostics only.
- ⚠️ **`authenticate` HANGS without a login** (no error object). Every handshake step has its own 20 s timeout and a timed-out `authenticate` maps to "Grok sign-in expired". A scratch `GROK_HOME` without `auth.json` looks like a protocol bug.
- ⚠️ **Numeric JSON-RPC id `0` is falsy.** The agent's permission request carries `id: 0`; `if msg.get("id")` drops it. Ids are not always integers either (an unprompted response with id `"skills-reload"` arrives at ~3 s).
- ⚠️ **A sub-agent is a second session on the SAME stdio.** Its `session/update`s carry `params.sessionId` = the child's id; a mapper that does not filter on it mixes the child's text/tools into the parent's chat and its `response_completed` into usage. The parent's aggregate usage already includes the child. Lifecycle = `_x.ai/session_notification` `subagent_spawned|progress|finished`.
- **Tool calls:** `tool_call` has `title` (= the tool name on the live path, a human string in `session/load` replays) and `_meta["x.ai/tool"].name`, no `toolName`. `read_file{target_file}`, `list_dir{target_directory}`; there is no `glob` tool. `web_search` is a backend tool: the query is only in the completed update's `rawOutput.action.query`. A failed shell command is still `status:"completed"` with `rawOutput.exit_code != 0`.
- **No assembled-message event** — only chunks; an assistant message ends at `_x.ai/session_notification` `response_completed` (which also carries per-call usage) or at the first `tool_call`.
- **`session/resume`'s result has no top-level `sessionId` and no `models` block** (id is in `_meta.sessionId`); the context window is remembered per model from an earlier `session/new`. The account email / `subscription_tier` / `is_zdr` live only in the `authenticate` result `_meta`. An unknown resume id answers `-32603 "Path not found."` on EVERY turn — run sites drop a stale id (`session_exists` hook) and start a new session. `session/new` with a missing cwd succeeds; the engine checks the directory itself.
- ⚠️ **`session/resume` keeps the system prompt the session was born with.** `_meta.rules` is neither duplicated nor re-read: an edited `CLAUDE.md` is ignored until a NEW session, so a long-lived Grok chat never sees a newly added "never touch X" rule.
- ⚠️ **Usage fields disagree by source:** in `response_completed.usage` (snake_case) `input_tokens` EXCLUDES the cache; in `result._meta.usage` (camelCase) `inputTokens` INCLUDES the cache reads, so `cached` is a subset of `input` and nothing adds it on top. `costUsdTicks` is 1e-10 USD and notional (flat subscription); the `modelUsage` key is `<model>-build`, not the model id.
- **`grok models` has no machine-readable mode** (text parse); `grok inspect --json` is the only countable inspect form (the text headings count disabled rows).
- ⚠️ **`--sandbox` is a TOP-LEVEL flag only.** `grok agent [stdio] --sandbox …` exits "unexpected argument"; the profile travels in env `GROK_SANDBOX`. The sandbox is process-wide, so the process must be spawned with `cwd` = the session's cwd (card worktrees included). `agent stdio` also rejects `--no-auto-update`; `GROK_DISABLE_AUTOUPDATER=1` + `[cli] auto_update=false` carry it.

**Sandbox and isolation**
- ⚠️ **A `deny` entry that does not exist is created ON THE HOST as an empty 0444 file**, which later breaks the real tool (`~/.config/gh`…). The list is built from `os.path.lexists` entries only; the probe canary dir is created BEFORE the list is built or the probe would prove nothing.
- ⚠️ **Nested deny entries stop bwrap from starting** (`~/.config` + `~/.config/gcloud`: "Can't create file … Read-only file system"); the engine prunes nested, duplicate and symlink-alias entries. An invalid glob (`{}`, `\`, empty segment) makes Grok refuse to START, taking every turn with it — the engine refuses the entry first.
- ⚠️ **Never put the `~/.grok` DIRECTORY on the deny list:** the binary lives under it and bwrap fails `execvp … Permission denied`. Deny the file `~/.grok/auth.json` instead (skipped when Cardloop's home IS `~/.grok`). A custom `GROK_SANDBOX_DENY` also drops that default.
- ⚠️ **`GROK_SANDBOX_DENY` REPLACES the defaults but cannot remove the floor** (`~/.claude`, `~/.claude-accounts`, `~/.claude.json`, `~/.cursor`) or the canary dir. Without the floor, a custom list that omitted `~/.claude` let a Claude PLUGIN's SessionStart hook fire inside a Grok turn even in an untrusted folder: user-scope plugins resolve through `~/.claude/plugins`, folder trust does not gate them, the kernel deny does.
- ⚠️ **The cockpit's own data dir is hidden as ONE directory entry — and `GROK_HOME` lives NEXT TO it (`<data>-grok-home`), never inside.** Grok hides a denied path with a mount over it, set up when the sandbox starts. A mount over a FILE is detached when the cockpit renames a temp file over it (`chats.json`, `crash-recovery-state.json` are rewritten that way while any chat runs): the model's shell then reads the new contents for the rest of the turn (reproduced with plain bwrap, pinned by `tests/test_grok_mask_kernel.py`; with the real CLI by `test_files_the_cockpit_rewrites_by_rename_during_the_turn_stay_hidden` and its positive control). A mount over the DIRECTORY survives every write underneath it, refuses new names and cannot be renamed away. A deny entry that CONTAINS `GROK_HOME` makes `grok agent` exit 1, which is why the home has to be outside; a layout with `GROK_HOME` or the CLI inside the data dir is refused with the reason (the per-child layout of the first P7 cut was dropped for this). The entry is always applied, `GROK_SANDBOX_DENY` notwithstanding, together with `<repo>/.env`.
- **A project that CONTAINS the data dir or `GROK_HOME` runs like any other** (the cockpit's own checkout, a chat rooted at `$HOME`): MEASURED with the real CLI (`tests/test_grok_live.py::test_a_project_that_contains_the_data_dir_and_home_cannot_reach_or_unmake_them`), the data dir and `.env` inside the workspace are unreadable and unwritable, `mv`/`rm -rf` on them is EBUSY, and `mv data-grok-home` is EBUSY (the CLI pins the home's parents). The ONE refused layout is a data dir or home reached through a SYMLINK inside the workspace (`_reached_through_workspace_symlink`): the model can rewrite names in its workspace, and the cockpit would follow the re-pointed link. A project INSIDE `GROK_HOME` is refused too.
- ⚠️ **There is no per-project gate and no `$HOME` backstop** (removed 2026-10-03 on the operator's decision: choosing Grok in the picker is the consent, like Codex/Claude; `grok_allowed` and `GROK_ALLOW_ALL_PROJECTS` are gone). The generic `ProviderSpec.gate` seam and `webapp._provider_gate_refusal` stay, inert, with registry-level tests only — if a provider is ever gated again, the wiring tests of commit e82bc08 (`tests/test_grok_wiring.py`, `test_grok_p3p4_wiring.py`) are the template. What the gate used to contain is now the per-project residual risk: a hostile clone can steer the model via its `CLAUDE.md`.
- ⚠️ **`auth.json` is writable by the model's shell — the account is pinned out of its reach.** MEASURED: an append from a turn lands on the host. A prompt-injected turn could swap in another account's login, after which every later turn in every project would send its code to that account. The engine therefore records the first verified account in `<data>/grok_account.json` (hidden from the shell) and refuses a login that names another one before any process starts (`_account_pin_problem`); `tools/grok-acct login` re-pins, `logout` clears it. A nameless login is refused once a pin exists. The token itself stays readable (residual below).
- ⚠️ **`GROK_HOME` is writable by the model's shell, and what it writes there is loaded by the NEXT turn of ANY project.** MEASURED on grok 1.0.46 (`tests/test_grok_live.py::test_what_the_models_shell_can_and_cannot_write_in_grok_home` and the sweep test + positive control): a turn in project A wrote `rules/`, `AGENTS.md`, `skills/`, `agents/`, `lsp.json`, `settings.json` into the home, and a turn in project B then listed the planted rules as its own global user rules (they also appear in `grok inspect` as `scope: global`). The layers that RUN code are kernel write-protected by the CLI itself (`hooks/`, `config.toml`, `managed_config.toml`, `requirements.toml`, `trusted_folders.toml`, `sandbox.toml`: "Read-only file system"). So `ensure_home` sweeps `FOREIGN_LAYERS` before EVERY turn (a symlink is unlinked, never followed; a layer that cannot be removed makes the turn an error). The engine never writes or needs any of them. Residual: two turns running at the same moment can still plant for each other inside that window.
- ⚠️ **Residual, measured: the Grok login in `GROK_HOME` (`auth.json`) is READABLE by the model's shell** (the agent and its shell share one sandbox; denying the file denies the agent — a deny entry over it makes the CLI exit). A prompt-injected turn can therefore read the SuperGrok OIDC token and, with child network allowed, send it away; the egress canary measures size per connection, not destination. There is no per-project switch to keep an untrusted clone out (do not pick Grok there), the account pin stops a swapped login, and `tools/grok-acct logout && login` rotates the token if a turn did something odd. Claude Code's own `~/.claude/.credentials.json` has the same property.
- ⚠️ **The probe's `ok` needs the END token.** The probe command ends with `cat end.txt`; its token reaches the wire only if the WHOLE command ran. Without it a model that read `control.txt` and never attempted the canary read earned a cached 7-day `ok` for a read nobody made. A model that declined the "security self-test" (no tool call) is retried ONCE at once with plainer wording; an "authorised by the operator / expected to be denied" preamble was refused 3 of 3 (the model calls it a social-engineering frame) while the original wording passed 10 of 10 and the plain one 5 of 5. A turn that ran a command is never retried.
- **`grok inspect --json` reports `projectTrusted=true` for a project with NO config of its own** (empty trust store, `GROK_FOLDER_TRUST=1` pinned) and `false` the moment it gains a `.mcp.json`. The doctor judges trust by what would START: trusted with nothing listed is `ok`, trusted with an active MCP server/hook/skill is `fail`.
- ⚠️ **Deny globs also hide TRACKED files:** `git add -A` / `commit -a` fail inside the sandbox in a repo that tracks a match of `**/.env`, `**/secrets.env`, `**/*.pem`, `**/*.key` (`error: open(".mcp.json"): Permission denied`). Credential protection beats it — do not "fix" it by dropping the globs.
- ⚠️ **`grok inspect` LISTS what it does not START.** It shows a project's `.mcp.json` / `.grok/config.toml` MCP servers, hooks and skills as active while a real turn started none (marker-file proof, with a positive control). The real neutralisers are folder trust, the deny floor and the compat env. `GROK_FOLDER_TRUST` is inverted from what the name suggests: `=1` (and unset) = gate ON, nothing project-owned starts; `=0` starts everything. It is pinned `=1` in the child env; a hostile parent value never reaches the child (allowlist). `trusted_folders.toml` in Cardloop's home must stay empty — any entry makes the engine refuse the turn and `provider_info` go unavailable.
- **The wire tripwire detects, it does not prevent:** `_x.ai/mcp/servers_updated` non-empty, `mcp_initialized.mcpToolCount > 0`, `hook_execution` or a tool name containing `__` aborts the turn (names only in the message), but a server can already have started. Prevention is trust + deny; re-run `-m grok_live` after every CLI bump (its positive controls fail loudly if the mechanism moves).
- **Tool removal is not containment.** Under the hermetic env `available_commands_update._meta.tools` still lists `scheduler_*`, `monitor`, `enter_plan_mode`, `exit_plan_mode`, `image_*` (only `ask_user_question` and `workflow` are gone); `agent stdio` has no `--tools` flag. Containment = one process per turn + `GROK_AUTO_WAKE=0` + the sandbox. `x.ai/memoryMode:"v2"` is not a switch indicator (`GROK_MEMORY=0` wrote no memory files, but the MODEL may write an `AGENTS.md` into the project).
- **`~/.agents/skills/*` and the bundled `resume-claude/-codex/-cursor` skills load whatever the compat env says**; they are hidden by the generated `config.toml` (`[skills] ignore` + `disabled`). A `config.toml` carrying `folder_trust`, `mcp_servers`, `hooks`, `compat`, `plugins` or `marketplace` is regenerated, not trusted.
- **Every sandboxed spawn leaves `sandbox-blocked.<pid>` + `sandbox-blocked-dir.<pid>` (mode 000) in `GROK_HOME`.** The reaper removes those of its own pid and of DEAD pids only (a live concurrent turn's placeholders may still be a mount source); `rm -rf` needs a `chmod u+rwx` walk first.
- ⚠️ **An unprobed sandbox is an unavailable provider.** The denial probe is ONE real model turn against a canary in the production deny list, cached by CLI version + deny list + home; unprobed / failed / `inconclusive` all fail closed (a model that refuses the self-test is retried once at once, then `inconclusive` and retried after 15 min). Never replace it with a static check: only a custom profile fails closed, a built-in one that cannot be applied logs a warning and runs unsandboxed.
- **`provider_info()` has no ctx:** it resolves the data dir from `_CARDLOOP_DATA_DIR`, else `<repo>/data`. Keep `ctx["DATA"]` the same directory or the canary path and the probe cache fork. The registry read caps the probe wait at 4 s; the probe itself keeps running behind a shield.
- **Doctor must not write.** `grok --version` creates a missing `GROK_HOME` and `grok inspect --json` rewrites `<home>/docs/` on every run, so doctor runs both in a throwaway home and never produces a probe verdict itself (that costs a model turn).

**History, handoff, usage**
- ⚠️ **A Grok session file is model-writable** (the sandboxed shell can append a forged `<user_query>` row to its own `chat_history.jsonl`; the cockpit's data dir beside `GROK_HOME` is hidden from the shell as one directory). Reading user rows as operator words would let a prompt-injected turn hand orders to Claude. A user row is trusted only if it matches the send ledger `<DATA>/grok_sent/<session-id>` (written at the `result` event, and BEFORE the run when the resume id is known — `restart-self.sh` aborts live turns, so `result` may never come). The browser cannot carry the `verified` flag (it maps messages to `{role,text,tools}`), so a handoff OUT of a Grok chat re-reads the file server-side and ignores the posted rows. A tool-call path with a control character is dropped from "Files this session touched" — it could otherwise print a forged heading.
- ⚠️ **`summary.json`, `signals.json` and `rewind_points.jsonl` sit in the same writable session directory:** usage is built only from the engine's ledger `grok_usage.jsonl` (outside the shell's reach), never from session files; `signals.json` is read only for context numbers.
- **History row `uuid` is `<sessionId>:<byte offset>`** (no per-row id or timestamp exists); user rows carrying `synthetic_reason` are skipped even if they contain `<user_query>`; session ids are strictly lowercase UUIDs, so the fake ACP server's wrapper must hand out UUIDs for any test that goes through the history reader.
- **Not inherited, on purpose:** Claude account pinning, ultracode, auto-rotate, the board reconciler and the flat `ctx["sessions"]` map stay `== "claude"`; any fallback that reads `ctx["sessions"]` is Claude-only; the pending rotation summary is consumed only by a Claude run. A refused or failed Grok card ends in **Failed** with the reason in its sidecar (there is no "blocked" column).

---

## Audit / files

- **Audit log:** `data/audit/audit-YYYY-MM.log` — per task: `TASK` (prompt), `BASH`/`BASH⚠️` (⚠️=irreversible), `EDIT/WRITE` (files), `DONE`.
- **There is NO turn watchdog.** The stall interrupt was removed in spec-039 and the
  `MAX_SECONDS` "task ceiling" never had an enforcement site (both were deleted, along with
  their settings sliders, in the root-fix C cleanup). A stuck turn is bounded only by
  `LIVE_CLIENT_MAX_PIN_SEC` (4h, persistent clients) and `CARD_LINGER_MAX_SEC` (card linger).
  Do not assume a 5/30-minute watchdog will rescue a hung turn — it will not.
- **File intake:** files uploaded via the cockpit are stored in `data/inbox/` (max 20 MB). The inbox grows — add cleanup if desired.
- **Files explorer reach = `fs_browser.py`, not the route handlers.** The Files tab can leave the project cwd, so the deny rules are the security boundary: `$HOME` + `FILES_EXTRA_ROOTS` (default `/tmp`; empty string disables) + the project cwd; **every top-level dot entry under `$HOME` is hidden by default** (deny-by-default, because the list of places tools drop secrets never ends) with ONE exception, `~/.claude/projects/<slug>/memory/`, since agents report those paths constantly. A project cwd equal to `$HOME` or an ancestor grants nothing extra (it would reopen `.ssh`). Saves are `PUT /api/fs/file` with a string `rev`; a stale `rev` is a 409, never a silent clobber — the agent edits the same files. ⚠️ `POST /api/global/file` shipped for months without the sensitive-dir gate its GET twin had (it could overwrite `~/.ssh/authorized_keys`); if you add another write path, copy the policy from `fs_browser.py`, not from the legacy handlers.
- **Files `raw` and the cockpit's own `data/`.** `permitted()` denies `data/` (safe, VAPID private key, `touched/` log, accounts) except `data/inbox/` — a security review found all of it reachable because `data/` sits inside `$HOME` with no dot component, and `raw` serves 100 MB, not 1 MB. Traps that are easy to reintroduce: (1) aiohttp `FileResponse` answers `Accept-Encoding: gzip` with `<file>.gz` when it exists, a name that never went through the policy — `_PlainFileResponse` strips the header; (2) `raw` headers ARE the XSS defence (CSP `sandbox` on images/media, forced attachment for html/xml/svgz), and the blanket `X-Frame-Options: DENY` must be overridden to SAMEORIGIN for PDFs or the preview shows nothing; Chrome will not render a PDF under a sandbox CSP; (3) `recent` judges a symlink by its target, like `list_dir`.
- **Chat file links must never throw at render.** `[x](#cardloop-file=%ZZ)` in an agent message survives react-markdown verbatim; an uncaught `decodeURIComponent` in `ChatLink` would crash the chat into its error boundary on every reload (the message is in the history). Decode through `decodeFileRef` (never throws). The regex must stay lookbehind-free (esbuild turns it into `new RegExp` at module load; Safari < 16.4 throws and blanks the cockpit) — `fileRefs.test.ts` guards it. `fileRefs.render.test.tsx` runs the plugin through the real react-markdown pipeline.
- **`useBackDismiss` layers share ONE stack (`web/src/lib/backStack.ts`).** Closing a modal from the UI calls `history.back()`, whose `popstate` every other layer used to hear as a real Back gesture: dismissing a confirm dialog flipped the project tab underneath it (and unmounted the Files tab with the unsaved draft in it). Now own pops are counted and swallowed, and only the top layer answers a real Back. Never detach the popstate listener when the stack empties — `history.back()` is async, and a detached listener leaves the counter set so the next real Back is eaten.

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

## spec-088: background work visibility (2026-09-01 incident)

- **A frontend commit is NOT deployed by `restart-self.sh` alone.** The service serves `web/dist`;
  a restart after a `web/src`-only commit ships nothing (fa1b12b: committed 10:28, restarted
  10:44, dist still 10:26). `restart-self.sh` now rebuilds when `web/src` is newer than
  `web/dist/index.html`, but the rule stands: after editing `web/`, `cd web && npm run build`
  BEFORE the restart, and verify from the browser, not from the build log.
- **`_live_turn_drop` used to kill the NEXT turn's buffer.** `_live_turn_finish` schedules a
  drop 300 s later; any turn started inside that window lost its live buffer at the mark
  (events without `seq`, steer bubbles invisible until reload, `/live` empty mid-turn). The
  drop is now bound to the turn object it was scheduled for. Symptom to recognise: a
  `run_end` timeline row without `seq`.
- **`stopped` is not a completion.** The auto-continue wake fires on done/failed only (plus
  crash-recovery flips). A model-issued `TaskStop` used to wake the orchestrator ~6 min later
  with "background task finished → stopped", and that phantom turn re-entered a `/goal`
  Stop-hook loop (9 more blocked stops).
- **spec-089 §2: the completion wake carries results, not just labels.** The wake prompt
  used to name only "<label> → <status>", so the orchestrator spent 3-5 Bash calls grepping
  `journal.jsonl` / `agent-*.jsonl` to find out what actually finished. Now the SDK's own
  `output_file`/`summary` ride the monitor record from `_notification_monitor_delta` through
  `_monitor_update` into the prompt, plus a bounded tail of the output file and (for a
  Workflow row) the run's journal counts (`_workflow_journal_summary`, best-effort match on
  the row id vs. the `wf_<runId>` directory name, else newest-mtime fallback). Prompt capped
  at `WAKE_PROMPT_BYTE_CAP` (default 6 KB) — degrades by dropping tails, then summaries.
- **`/api/health?deep=1` reports `bg_turns`.** A CLI-autonomous turn (task-notification wake
  surfaced by the drain) is not in `ctx["running"]`; `restart-self.sh` waits for `bg_turns`
  too and needs 2 consecutive idle polls — one sample sat inside the ~3 s TaskStop→relaunch
  window and restarted the service under a live orchestration.
- **Workflow sub-agents are invisible between turns (open, spec-088 P1).** After the launching
  turn ends, `_drain_between_turns` drops `TaskStartedMessage`/`TaskProgressMessage` and every
  `parent_tool_use_id` message; the one `kind=workflow` monitor row never gets a tail; the
  chat's ⚙ chip is wiped on `run_end`. The operator sees an idle composer while 12 agents run.
- **Roster `maxTurns` is a runaway guard, not a work budget.** Was researcher/skeptic 20,
  executor 40 — 6 of 37 workflow agents died there with `null` and no partial. Now researcher
  120, skeptic 80, executor 200, quick 25, and every role's prompt carries PROGRESS ON DISK:
  append findings to `/tmp/cardloop-scratch/<task-slug>.md` every ~15 tool calls (read-only
  roles via a Bash heredoc), so a capped agent still leaves its work. Never remove the cap: a
  looping agent without one burns until the 30-min TTL eviction at a growing context per turn.

## Browser pane: a dropped CDP connection used to leak the tab (2026-09-19 incident)

`_on_disconnected` sets `_closed = True` **before** routing to `close()`. While `close()`
still began with `if self._closed: return`, that made it a no-op on exactly the path it
exists for, and every dropped connection leaked three things at once: our tab in the
shared Cloak profile, the local Playwright driver process, and the profile refcount — the
last of which kept the profile pinned open so its idle-stop never fired either.

Eight stranded WebGL tabs (fly.igdigi.com + its dev server) held the **GPU-less** browser
VM at ~670 % CPU for four hours and set off a run of xyOps "High CPU Load" alerts. Chrome
there runs `--use-angle=swiftshader`, so WebGL is rasterised on the CPU, and it is launched
with `--disable-backgrounding-occluded-windows`, so `document.hidden` never goes true and
a page's own pause-when-hidden logic never engages. Assume any leaked animated tab costs
~80 % of a core, forever.

**There were two independent teardown bugs, and the second one was worse.** `close()` also
cancelled `self._watchdog` unconditionally — but `_idle_watch` reaches close() from *inside*
that very task (`_idle_watch -> close_session -> close`). Cancelling the current task throws
`CancelledError` at the next `await` inside close(), and `CancelledError` is a `BaseException`,
so the `contextlib.suppress(Exception)` blocks there never stopped it: `_teardown()` was not
reached on ANY idle close. Every pane that simply timed out leaked its tab and its profile
refcount. Two symptoms to recognise it by: `GET /api/browser/profile-usage` shows `sessions: {}`
while `lifecycle.attached` still lists the project, and a pane's tab outlives the 120s idle
grace by minutes.

Rules that follow:

- **Never cancel a task from code that task might be running in.** Check
  `is not asyncio.current_task()` first. `suppress(Exception)` will not save you —
  `CancelledError` is not an `Exception`.

- **`close()` guards on `_close_ran`, never on `_closed`.** Keep those two meanings apart:
  `_closed` = "this session is dead", `_close_ran` = "close() already ran".
- **A tab that failed to close stays registered.** A dead connection cannot close a remote
  tab; recording it as closed is what made a leak unrecoverable.
- **Only ids we wrote down may be swept.** In a shared profile our tabs look exactly like
  the operator's logged-in ones — `data/cloak-pages-owned.json` is the only licence to
  close anything, and `browser_pane.live_target_ids()` protects sessions in flight.
- Sweep on a timer (`CLOAK_ORPHAN_SWEEP_SEC`, default 600s), not only on the next pane
  open: nobody opens a pane overnight.

To inspect the profile by hand: `secret get cloak-manager-token`, then
`GET https://cloak.coscore.us/api/profiles/<id>/cdp/json/list` (the browser runs
`--remote-debugging-pipe`, so its local CDP port is closed — go through the Manager).

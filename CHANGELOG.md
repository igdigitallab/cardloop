# Changelog

All notable changes to Cardloop. Format — reverse chronological.
Versions follow semver-like conventions (0.x while the project is under active development).

> Discipline: when a new feature ships — add a line here + mark the card in TASKS.md → DONE.md. A tag is placed on a stable point (`git tag vX.Y.Z`).

## [Unreleased]

### Added — policy rules in Markdown (`docs/RULES.md`)
- Declarative `block` / `warn` rules in `*.md` files (project `.claude-ops/rules/`, global `~/.claude-ops/rules/`
  or `$CARDLOOP_RULES_DIR`, plus pack directories), evaluated by one PreToolUse hook before every tool call of a
  Claude run, with no code change or restart. Hit counts, status and diagnostics per rule in the Agents tab and
  `GET /api/projects/{id}/rules`. Claude engine only.
- A project rule file that git tracks is ignored until the project opts in (`rules_trust_tracked`): a cloned
  repository cannot install policy. Regex work runs under a SIGALRM watchdog; a block rule fails closed on input
  it cannot inspect.

### Added — project health check
- The project's **Tests** button also runs a read-only health check (and one sweep a day covers every project):
  memory index near the CLI's 200-line / 25,000-byte cap, heavy context floor, `test_cmd` missing while tests
  exist, work left uncommitted/unpushed, orphan card worktrees, `.env` exposed, project settings that run code
  (hooks, `ANTHROPIC_*` env, `Bash` allow — acknowledgeable until the file changes), invisible characters in
  CLAUDE.md / memory / roles. No score: a finding is a real, actionable risk with a one-line fix, and a healthy
  project shows nothing. A compact `⚠ N` pill opens the findings; the Tests verdict itself is unchanged.
  `GET /api/projects/{id}/health-check`, `POST .../ack`, `GET /api/health-check`; knobs `HEALTH_CHECK_*` in `.env.example`.

### Security — the last-resort Bash deny guard reads commands, not text
- `command_guard.py` replaces the regex-over-text matcher with a shell tokenizer (quotes, `$(...)`,
  backticks, heredocs, env prefixes, separators) and per-command argv rules. A quoted mention of a
  denied shape is data and passes; `bash -c`, `eval`, `ssh host "<cmd>"`, `sudo`/`env`/`xargs`/`timeout`
  payloads and heredocs fed to a shell are parsed as programs. Linear time with a hard budget (the old
  matcher needed 2.7 s for 24 KB of `dd `; now ~25 ms for 72 KB) and a coarse keyword fallback.
- New denies: skipping git hooks (`--no-verify` and its prefixes, `-n` on `git commit`/`git am`,
  `-c core.hooksPath=...`, `git config core.hooksPath`, `SKIP_SECRET_SCAN=...`), plus-refspec force
  pushes into master/main (`git push origin +master`), and `git reset --hard` / force pushes behind
  git global options (`git -C <path> ...`).
- Every deny is audited (`[project] DENY: <rule-id>: <command>`). See GOTCHAS.md, Security (5).

## [v0.18.0] — 2026-10-07

### Security — vulnerable dependencies fixed (spec-096 P1)
Every publicly known vulnerability in the shipped dependencies is fixed; `osv-scanner` (the version
OpenSSF Scorecard uses) and `npm audit` report zero.
- **Server runtime** — aiohttp 3.13.5 -> 3.14.4: CVE-2026-34993, CVE-2026-47265, CVE-2026-50269,
  CVE-2026-54273, CVE-2026-54274, CVE-2026-54275, CVE-2026-54276, CVE-2026-54277, CVE-2026-54278,
  CVE-2026-54279, CVE-2026-54280, CVE-2026-59881, CVE-2026-69243, CVE-2026-69244 (GHSA-cq5v-8q36-5273).
  Transitive floors: PyJWT >= 2.15.1 (CVE-2026-101917, CVE-2026-101918, CVE-2026-102265 to CVE-2026-102275,
  CVE-2026-103001), urllib3 >= 2.8.0 (CVE-2026-97687, CVE-2026-97688, CVE-2026-97689), multidict >= 6.9.1
  (CVE-2026-104874).
- **Shipped in the browser bundle** — mermaid 11.17.2 (CVE-2026-50159, CVE-2026-71436, CVE-2026-71437,
  CVE-2026-71438, CVE-2026-71439), DOMPurify 3.4.16 (CVE-2026-66010, CVE-2026-75838, GHSA-6688-9rhm-gjv2),
  KaTeX 0.18.2 (CVE-2026-103923).
- **Android wrapper** — @capacitor/android 7.6.9 (CVE-2026-103922).
- **Build/dev only** — vite 5 -> 6.4 (esbuild 0.25), @xmldom/xmldom, js-yaml, fast-uri, nanoid, postcss,
  source-map-js, browserslist, baseline-browser-mapping, brace-expansion: lockfile updates.

### Security — private files and linear parsers (spec-096 P2)
- One writer (`fsutil.atomic_write`) for every file that holds a secret: the vault key and store, per-project
  `secrets.env`, the Web Push VAPID private key (it was written 0644 and never tightened), push subscriptions,
  account and Grok files. The temp file is 0600 from creation (`mkstemp`), so no reader ever sees a
  world-readable copy; an existing loose VAPID file is tightened at start; `doctor` warns when the data dir is
  group/other-accessible.
- The board parser's regexes are linear (a 40 000-space card took 4.7 s); output is identical on 140 real
  TASKS.md/DONE.md files and 200k fuzz strings.

### Fixed — hostile review of the provider seam, Grok and the load meter (spec-096 P8)
- Grok: the GROK_HOME sweep never follows a symlink planted by the model (it chmod'ed the TARGET); `auth.json`
  is read capped and without following links; the deny list adds the Docker socket and every registered
  project's `.env`, `.env.*` and `.claude-ops/secrets/`; history, session lists and search show a Grok session
  only for a directory the engine recorded; the JSONL reader stops at the size it saw at open; failed turns
  still write their usage row; `doctor` no longer reports unmeasured Grok facts as green.
- A deny entry reached through a symlinked parent (`/var/run/docker.sock` with `/var/run -> /run`) is handed
  to Grok canonical: the real CLI refused to start on it ("is not the mountpoint for its visible mount").
- Handoff: model text inside a handoff block is never promoted to "Standing constraints (verbatim, from the
  operator)" on the next engine crossing.
- The queue drain fails closed on a provider pin it cannot honour (it ran the message on Claude); the Grok send
  ledger is written off the event loop; a provider gate is now tested at all three run sites.
- Load meter: a one-off disk step (a large copy) is no longer extrapolated into "full in 0.8 days"; a cleanup
  no longer masks a real fill; the journal records how a flapping signal settled; a static disk WARN is not
  delayed by the runway debounce.

### Added — API route index, CI hardening (spec-096 P4/P6)
- `docs/API-routes.md`: every HTTP route (method, path, auth, summary), generated by
  `tools/gen_route_index.py` from the router itself; `tests/test_route_index.py` fails when it drifts.
- CodeQL runs the `security-extended` suite; every GitHub Action is pinned by commit SHA (tested); the
  findings it raised (a Cloak Manager profile id that could re-aim the request, log lines that a crafted path
  could forge) are fixed.

### Upgrade notes — spec-096 hardening (read before updating)
Things that can surprise an existing install; each is deliberate.
- **An absolute path to an allowlisted program in `log_cmd` / `test_cmd`** (for example
  `/srv/app/venv/bin/python -m pytest`) is no longer accepted on the strength of its file name: it must be
  the file the bare name resolves to under the service's `PATH`, or live in a directory listed in
  **`DIAG_CMD_ALLOW_DIRS`** (add the venv's `bin/`). Until then the command yields nothing: the board
  janitor has no test signal and Review cards stop auto-archiving (a rejected `log_cmd` logs
  `log_cmd rejected by allowlist`; a rejected `test_cmd` is silent, so look at any project whose cards
  stopped leaving Review). A project-relative `venv/bin/python` and a bare `python3` keep working.
- **A vault key that exists ONLY in the environment** (`CLAUDE_OPS_SECRET_KEY` in `.env`, no key file) is no
  longer inherited by agents, so the `secret` CLI they run cannot decrypt the vault. Add
  `CLAUDE_OPS_SECRET_KEY` to **`AGENT_ENV_PASSTHROUGH`**, or use a key file (`CLAUDE_OPS_SECRET_KEYFILE`).
  Installs with a key file are unaffected.
- **A blank or placeholder `WEB_COOKIE_SALT`** is replaced by a generated salt in `data/cookie_salt`: every
  browser signed in on the old placeholder is signed out ONCE. A salt you set yourself changes nothing.
- **A symlink in place of a secret file is replaced, not followed.** Files written through the shared atomic
  writer (the vault key and store, per-project `secrets.env`, the push key and subscriptions, account and
  Grok config, role files) are swapped in with a rename, so a link you made on purpose (a role or
  `secrets.env` symlinked to a shared file) is cut loose on the next save; keep such a file as a real file.
- **Grok work gets no host-side test verdict.** The janitor's `test_cmd`, the card quality gate and the
  autopilot signal would execute the project's own test code (conftest.py, Makefile, package.json
  scripts, `venv/bin/pytest`) on the host, unsandboxed — which a Grok-edited tree turns into a sandbox
  escape. A card whose run record says Grok, and (conservatively) every card of a project with live Grok
  work (a Grok card in progress / Review / Failed, or a chat on Grok), now reads `no test signal: Grok work
  is not executed on the host`: the gate answers `unknown`, the janitor never auto-archives it (accept
  it yourself). The manual "Run tests" button is unchanged.

### Security — final-review fixes (spec-096 P9)
- A Grok agent that names no account in its `authenticate` reply is refused while an account is pinned
  (it used to be waved through, skipping the one check the model's shell cannot forge).
- Resuming a Grok session — the Resume action and every run site — requires the cockpit's witness for that
  directory (`grok_history.resumable`): a session directory planted by another project's turn can no longer be
  made permanent by resuming it. A stored id nobody vouches for is dropped with a timeline row and a new
  session starts.
- The handoff reader drops only blocks the cockpit wrote: an operator line that starts with `# Handoff:`
  no longer swallows the rest of the message, and every service wrapper the display strips
  (`<task-notification>`, `<system-reminder>`, agent/teammate messages, wake rows) is also kept out of
  "Standing constraints"; an unclosed service block fails closed.
- A turn clears only the handoff block it delivered (a block armed for the other engine mid-turn survives);
  a pinned queued message with no chat of its own is refused when the visible chat runs another provider.
- Two racing starts agree on one cookie salt; role files keep mode 0644; the `2fa_state_unreadable` journal
  line names the store to move aside when the key is lost; Grok's config writer tightens a loose file; the
  load-alert journal says `push sent to N subscriber(s), delivery not confirmed` instead of `delivered`
  when only a push (no receipt) went out.

### Security — secrets out of reach of children and same-user processes (spec-096 P3b)
- **No inherited secrets.** At start the cockpit moves every secret (`WEB_PASSWORD`, `WEB_COOKIE_SALT`,
  `BOT_TOKEN`, `COOLIFY_API_TOKEN`, `AZURE_FOUNDRY_KEY`, `N8N_API_KEY`, `TWOCAPTCHA_API_KEY`,
  `OLLAMA_AUTH_TOKEN`, `CLAUDE_OPS_SECRET_KEY`, `JOURNAL_TG_BOT_TOKEN`, and any `*_PASSWORD` / `*_SALT` /
  `*_TOKEN` / `*_SECRET` / `*_API_KEY`) out of `os.environ` into a private snapshot (`runtime_secrets.py`),
  so no agent, terminal or test runner it spawns inherits them. `ANTHROPIC_*` and
  `CLAUDE_CODE_OAUTH_TOKEN` are left alone. **Behaviour change:** a variable such as `GITHUB_TOKEN` in
  `.env` is no longer visible to agents; list it in the new `AGENT_ENV_PASSTHROUGH` to keep it. The
  startup journal line names what was removed (never a value).
- **Non-dumpable process.** `prctl(PR_SET_DUMPABLE, 0)` at the first line of `bot.py`: `/proc/<pid>/environ`
  (the systemd `EnvironmentFile=` block the scrub cannot clear), `mem`, `maps` and `fd/` are no longer
  readable by other processes of the same user, and a same-user ptrace attach is refused. Debugging the
  cockpit with `py-spy`/`gdb` now needs `sudo`. `doctor` reports it as "Process hardening" and notes the
  host's `kernel.yama.ptrace_scope`.

### Security — login hardening (spec-096 P3a)
- **Cookie salt.** A blank or `CHANGE_ME...` `WEB_COOKIE_SALT` (what a plain `cp .env.example .env`
  left behind: a salt published in the repo) is now replaced by a random salt generated on first start
  and kept in `data/cookie_salt` (0600), never printed. A salt you set yourself is used unchanged, so
  existing sessions stay valid; an install that was running on the placeholder signs everyone out once.
  `.env.example` ships it blank, `doctor` warns on the placeholder.
- **2FA fails closed.** When the vault cannot be read (lost key, corrupt store) the login answers
  `503 2fa_state_unreadable` instead of letting the password alone in; the same for the recovery-code
  list. Break-glass from the host shell is in SECURITY.md.
- **WebSocket `Origin` check.** The terminal and the browser pane refuse an upgrade from a foreign
  origin (another page on the same host rides the `SameSite=Lax` cookie). Clients that send no `Origin`
  are unaffected. New `WS_ALLOWED_ORIGINS` adds extra origins; a proxy that rewrites `Host` needs
  `X-Forwarded-Host` (with `TRUSTED_PROXIES`) or that list — see docs/remote-access.md.
- **`log_cmd` / `test_cmd`.** `/tmp/tail` no longer passes on its file name: the program must be a bare
  allowlisted name, the path of the file that name resolves to, or a project-relative path such as
  `venv/bin/python`. An absolute path to a venv interpreter now needs `DIAG_CMD_ALLOW_DIRS`.
- `push-subscriptions.json` is written 0600 and atomically; the DONE.md title extraction is linear.
- README states what the command deny list covers (and what it does not).

### Changed — Haiku 5.5
- `haiku` now runs Claude Haiku 5.5 (`claude-haiku-5-5`, released 2026-10-07): static labels, the
  pricing row (short-prompt tier $0.10/$0.50 per MTok, 10x below Haiku 4.5; prompts over 100k tokens
  cost 5x more and are not modelled) and the keyword fallback for unknown Haiku ids. No SDK release
  bundles a CLI that resolves the alias yet (0.2.164 bundles 2.1.292, still Haiku 4.5), so
  `CLAUDE_CLI_PATH` points at CLI 2.1.293 until one does. `claude-agent-sdk` floor raised to 0.2.164.
- `tools/skills-lint.py` flags `claude-haiku-4*` / `haiku-4*` as a stale model reference.

### Added — Grok Build as the third provider (spec-095)
Off unless `GROK_ENABLED=true`; with it off nothing about Grok is visible (no registry row, no UI,
no doctor line). A chat, free chat, board card or project default can be pinned to Grok, running the
official `grok` binary on a SuperGrok subscription — never the xAI API. Runbook: [docs/GROK.md](docs/GROK.md).
- **Engine (`grok_engine.py`).** One `grok agent --no-leader stdio` process per turn over ACP, in its
  own process group that is always killed afterwards; Stop = `session/cancel`. Subscription-only and
  fail closed: an OIDC login with `coding_data_retention_opt_out`, not API-billed, same account as
  `auth.json`, re-checked against the agent's own report on every turn. Plan mode and "Ask me" are
  unsupported (a visible error, not a silent downgrade); limits are not reported by Grok, so the pill
  says "limits not reported" instead of a bar.
- **Isolation.** The child gets an environment allowlist (never the cockpit's own env) plus switches
  that stop Grok importing Claude/Cursor/Codex MCP servers, hooks, skills, rules and sessions; every
  turn runs under a generated custom sandbox profile whose deny list hides credential paths (an
  always-on floor for the Claude/Cursor surface survives a custom `GROK_SANDBOX_DENY`); folder trust
  is pinned on so a repo's own `.mcp.json` / `.grok` hooks and skills never start, with a wire
  tripwire as a second line; the sandbox is proven on the host by a self-test turn before Grok is
  offered. Cardloop's Grok login, sessions and profile live in their own `GROK_HOME`
  (`tools/grok-acct login|status|logout`), by default `<data dir>-grok-home` — next to the data dir,
  never inside it. The cockpit's own data dir and `.env` are hidden from the model's shell as one
  directory entry (measured with the real CLI: unreadable, unwritable, cannot be renamed or removed,
  also when the project contains it); only a data dir or home reached through a symlink in the
  workspace is refused. The model-writable instruction layers of `GROK_HOME`
  (rules, `AGENTS.md`, skills, agents, …) are deleted before every turn, because one project's turn
  could otherwise plant rules that every other project's next turn loads (measured with the real CLI).
- **Choosing Grok is the consent.** Like Codex and Claude, Grok is picked in the provider menu of a chat,
  free chat, board card or board default and runs in any project (a chat rooted at `$HOME` and the
  cockpit's own checkout included): no per-project switch, no `grok_allowed`, no
  `GROK_ALLOW_ALL_PROJECTS`. It is listed only while `GROK_ENABLED=true`. Because a turn's shell can
  rewrite `auth.json` (measured), the first verified login is pinned in `<data>/grok_account.json` and
  a login naming another account is refused (`tools/grok-acct login` re-pins on purpose).
- **History, sessions, search, usage, handoff.** Read from Grok's session files; `providers.grok` in
  `/api/usage/dashboard` (tokens, turns, local 5 h / 7 d counters, `notional_usd` labelled
  API-equivalent). Because the model's own shell can write its session file, the cockpit keeps a
  send ledger (`data/grok_sent/`): a handoff out of Grok never carries a user row it did not send.
- **`make doctor`** gains a Grok section (CLI/version, auth, bubblewrap, GROK_HOME, sandbox profile
  and probe, compat, folder trust, per-project compat, leftover processes, litter, ledger sizes).
- **Tooling and tests.** `tools/grok-verify check` is the gate after every CLI update (real-binary
  isolation tests, egress canary, fixture-skeleton drift; it prints the `KNOWN_GOOD_VERSIONS` edit and
  never applies it) and `tools/grok-verify soak` a long run of small real turns watching processes,
  litter, size and egress. `tools/grok_record_fixtures.py` re-records the 26 fixtures in
  `tests/fixtures/grok/` from the real CLI; `tests/fake_grok_acp.py` replays them; opt-in markers
  `grok_live` (real binary, real turns) and `grok_canary` (egress canary); a Grok e2e suite against a
  real cockpit with the fake CLI.
- **Frontend.** One provider table (`web/src/lib/providers.ts`) drives every picker, label, tag and
  usage card; Grok appears in the new-chat / free-chat / board pickers, the model menu, Settings
  (privacy toggle, board model) and the Usage tab.

### Changed — the provider seam
- `providers.py` is the one table of what differs per engine (engine factory, continuity field,
  resume kwarg, result id, model field, capabilities, per-project gate, send ledger); it replaced
  dozens of `provider == "codex"` branches in `webapp.py` and `board.py`. An unknown provider on a run site
  is now an error, never a silent run on Claude.
- Manual `/rotate` on a Codex or Grok chat clears that provider's own session id and arms a
  chat-scoped handoff built locally (no model call). It used to summarise the Claude session, leave
  the adapter's id in place and report `reset: true`.
- The settings validator accepts every adapter's `<name>_model`; provider-validation errors name
  every registered provider; `chat.ask_codex_conflict` became `chat.ask_unsupported` in the UI.

### Fixed — found while adding the third provider
- The first POST to a free Codex chat that had never been listed ran on Claude with a Codex model id.
- A pending rotation summary (Claude's) was consumed by Codex runs; only a Claude run takes it now.
- Clicking a model in the model menu of a Codex chat did nothing (the handler was gated on Claude).
- A failed history read erased the reply that had just streamed; the conversation now stays.
- A refused runtime switch left no visible trace; a 409 was shown as "project busy" or as raw JSON
  where the server had sent a sentence. The server's sentence is shown.
- Project Settings could not be saved while `context_pack_enabled` was unset (the tab posted its
  `null` back and the server answered 400).
- The Settings tab offered a "Grok board model" row on every project even with Grok off.
- The file-rewind button appeared on Codex and Grok history rows, where it has no checkpoint behind it.
- A tool-call path containing a newline could print a forged heading into a handoff block.
- Layout at 360 px: provider buttons wrapped and swallowed their row, the card editor row overflowed,
  the privacy toggle was a 13 px checkbox, the usage card head squeezed its title, the new-chat
  refusal was tiny inset text, and a toast covered the Create button of a phone dialog.

### Added — Load meter v2: disk runway, a journal trail for every signal
- **Disk runway.** The `disk` signal now also says how long until the volume is full at the rate
  that has HELD over the last 1-3 days (`89% · 3.7d`): warn under 7 days, crit under 2. It comes from
  a persisted history (`data/load_disk_history.json`), uses whole-day baselines so nightly jobs do not
  read as growth, takes the minimum across baselines so a one-off copy is not a trend, and starts
  speaking after two days of history, only for a disk with less than 15 % free. A percentage alone
  cannot tell a quiet weekend from a one-day problem.
- **Everything goes to the journal.** `[load-monitor]` lines for every signal/level transition (and
  recovery), every alert with its full text (it used to say only "server overloaded"), the delivery
  outcome of each leg (inbox / toast / push, incl. "no subscribers"), event-loop stalls of 0.5 s or
  more, and a status line every 15 minutes.

### Fixed
- The working set no longer counts reclaimable slab (dentry/inode caches): a disk walk or backup
  inflated the meter — and the memory guard that shares it — by gigabytes of memory the kernel
  hands back on demand.
- `make doctor` judges the service's memory by the working set too: it used to report raw `memory.current`
  (page cache included) and called a healthy host "88% of MemoryMax, little headroom".
- Sizes are labelled GiB/MiB (they were always binary); the agent-process row shows the count
  instead of a ratio whose denominator differed from the header's `chats live 3/8`.

## [v0.17.0] — 2026-10-02

### Added — Load meter: a vertical LED bar for "is this server overloaded" (spec-094)
A small vertical meter in the top-right corner, next to the rate-limit pill (and in the composer
bar on mobile). Its height is the worst signal's pressure — green below the warn line, amber up to
the crit line, red at it. Hover or tap for the detail: what is wrong, why it matters, what to do,
every normal signal, and the heaviest chats. Signals: working-set memory (reclaimable page cache
excluded), memory stalls (PSI), swap-in rate, memory-guard evictions, stray agent processes (a
headcount against the live-client cap), OOM kills, event-loop lag, file descriptors, data disk,
a RAM-backed temp dir, and CPU. Every threshold is relative to the limits detected on THIS host
(cgroup `memory.max` or physical RAM, `RLIMIT_NOFILE`, free-space fractions), so it adapts to
whatever machine Cardloop runs on; a signal that cannot be measured is omitted, never shown green.
A cockpit that stops answering is drawn as a hollow red bar, distinct from "signed out". A red level
that persists for two minutes sends a toast, a Web Push (where subscribed) and an inbox file, so the
meter does not depend on anyone watching it. `GET /api/system-load`; `make doctor` gains a `Load`
section; `LOAD_MONITOR=0` turns everything off.

### Fixed — idle-TTL eviction leaked every CLI subprocess it evicted
`_idle_waiter` awaited the evictor from inside itself and the evictor cancelled `entry.idle_task`,
i.e. its own task; the cancel landed inside `disconnect()` and the SDK never reached its
terminate/kill step. 21 of 21 TTL evictions left a `claude` process (+ MCP children, ~450 MB each)
alive for a day, which held the cgroup at 97-100 % and made the memory guard evict real idle chats
("sessions keep dropping"). The disconnect also runs shielded now, so a slow teardown outliving the
10 s wait is no longer cancelled by it.

### Fixed — the memory guard counted reclaimable page cache as pressure
`LIVE_CLIENT_MEM_GUARD` compared raw `memory.current` to `memory.max`; on a git-heavy host the page
cache alone fills the limit while nothing is wrong, and idle chats were evicted for it. The guard
now uses the working set (`memory.current - inactive_file`), the same measure as the load meter.

### Added — Ask mode: per-tool approval from the phone (spec-082 A)
A third per-chat turn mode next to normal / plan: "🙋 Ask me". Every action that changes
something — each Bash command, edit, fetch — pins an **Allow once / Always allow here /
Deny** card above the composer and sends a Web Push naming the tool, so the turn is
answerable from a phone in one tap; reads and searches (`Read`, `Glob`, `Grep`,
`NotebookRead`, `TodoWrite`, `Task`) run freely. Deny takes feedback and hands it to the
model, "Always allow" persists per project (never global), and an unanswered request
auto-denies after `ASK_GATE_TIMEOUT_SEC` (default 900) with a model-readable message so a
turn can never hang forever. Off by default; board/card runs stay full-auto.
⚠️ The turn connects with `permission_mode="default"` — under `bypassPermissions` the SDK
*shadows* `can_use_tool` (`CanUseToolShadowedWarning`) and every tool would run ungated with
no error at all. Verified live end-to-end (allow → the write lands in the same turn; deny with
feedback → the model adapts and the file is never created). Note the CLI auto-approves Bash
commands it classifies as harmless (`echo …`) before the callback, so ask mode gates
mutations, not literally every tool call. Implementation reuses the spec-080 approval machinery: the pending-plan
store is now a pending-**decision** store keyed by kind (`plan` | `tool`) with kind-agnostic
`/api/projects/{id}/decision/{id}[/decide]` routes; the shipped `/plan/...` routes still work.

### Added — Cockpit Plan Mode (spec-080)
Terminal-grade plan mode in the chat: a per-chat "🗺 Plan mode" toggle runs the turn in the
CLI's NATIVE `permission_mode="plan"` (hard read-only + the CLI's own 5-phase Explore/Plan
workflow + plan file), and `ExitPlanMode` surfaces as an Approve/Reject card above the
composer (markdown plan body, reject-with-feedback, Web Push, reload-durable via a
`plan_id` pointer on the chat). Approve → the same turn executes (the `can_use_tool` gate
rubber-stamps post-approval; a runtime flip INTO bypassPermissions is illegal and the
plan+`--dangerously-skip-permissions` combo disables plan-blocking — both verified live,
so no mode flip exists); Reject → the model revises in-turn and re-submits. Guards: plan
turns are queued while background children pin the live client (an ungated reuse would
silently run full-auto); ultracode is suppressed during plan turns (the Workflow tool IS
callable inside plan mode — verified); rotate refuses while a plan is pending; deploys
wait for `plan_pending==0`; a restart orphans the pending plan loudly (boot reconcile +
operator notification); the 4h pin cap cancels a forgotten card cleanly. E2E-covered with
zero SDK via the fake engine (`e2e:plan`).

### Fixed — root-fix: background sub-agents dying / orchestrator never reporting back
Three-wave fix for the standing complaint "I launch background agents, they get killed
mid-work, and the orchestrator never comes back until I ping it". Evidence-driven
(journalctl since 07-05: 37 SDK buffer-overflow turn kills in 11 days, 10 whole-service
OOM kills in 4 weeks, zero auto-continue activity after any crash):
- **SDK reader buffer 1 MiB → 32 MiB** (`SDK_MAX_BUFFER_BYTES`, all four
  `ClaudeAgentOptions` sites) — closes the `err-d54afa` "Fatal error in message reader"
  class; buffer-overflow aborts now log an actionable hint naming the cause.
- **Memory: cgroup alert loop** (`MEMORY_ALERT_PCT=80`, names top-3 RSS offenders in
  `data/inbox/`) + PreToolUse guard denying the known OOM-fatal wide-context-grep-on-bundle
  shape + `MemoryMax` 6G→10G (live `systemctl set-property`, host headroom checked).
- **Crash-surviving completion wake (the core fix)** — `data/crash-recovery-state.json`
  (atomic, 2s-coalesced) persists the monitor registry + pending wakes + last-turn options;
  boot reconcile flips orphaned monitors (transcript-first, blind-failed fallback) through
  the SAME `_monitor_update → _schedule_completion_wake` machinery, so after ANY restart or
  OOM the orchestrator itself reports what happened — no operator ping needed. Crash flips
  carry a "verify against disk" note so the model doesn't present reconciliation as fact.
- **Deploys wait for background children** — `/api/health?deep=1` gained `agents` (any-session
  running agent/workflow/monitor count); `restart-self.sh` waits for `running==0 AND
  agents==0` (soft cap 600→1800s). Prerequisite: workflow/monitor kinds gained a staleness
  flip (`MONITOR_STALE_WF_SEC=3600`) so a zombie can't tax every deploy.
- **Manual "Wrap & reset" guard** — refuses (409) while background children are running
  (`force:true` overrides); it used to silently SIGTERM them with no wake possible.
- **Honest abort labels** — `operator_stop` / `turn_aborted(reason)` timeline records at the
  actual call sites; the rotation-handoff digest now blames infrastructure for infra aborts
  instead of trusting the CLI's ambiguous "[Request interrupted by user]" marker.
- **Card linger** — ephemeral (card) runs wait up to `CARD_LINGER_MAX_SEC=300s` for still-open
  deferring background tasks before disconnecting; on timeout the monitors flip failed
  immediately so the wake fires without the 15-min staleness detour.
- **Hygiene** — SDK 0.2.129 (bundled CLI 2.1.221 = terminal parity), once-per-subtype logging
  of unknown SystemMessages + `SDK_DEBUG_UNKNOWN_MESSAGES=1` for the Agent-Teams blind zone,
  dead `STALL_SECONDS`/`MAX_SECONDS` watchdog + its lying settings sliders removed, stale
  GOTCHAS watchdog claim fixed.

### Changed
- **The custom session-goal overlay (spec-076) was removed** — pinning a goal never started the work and its status never flipped to "done", so the whole cockpit layer was cut: the pinned bar, the `/goal` chat-interception, the `chats.json` goal record, the `run_engine(goal=…)` Stop-hook composed into `--settings`, and the `goal_status` events all deleted (`_compose_settings` now takes only `ultracode`). The CLI's OWN native `/goal` is untouched — typed text still passes through to the bundled CLI — but note it lives only in CLI session memory (the cockpit can't see or clear it; a stray native `/goal` needs a session reset to drop).
- **Cost auto-rotation is now opt-OUT (default ON)** — the 2026-07-08 ledger audit found the opt-in default made the 280K auto-rotation effectively dead: no chat ever enabled it, sessions ballooned to 470K and turns above 200K context were 58% of a week's spend. The composer "+" toggle now DISABLES rotation for a chat instead of enabling it (absent field → ON; explicit `auto_rotate:false` → off; `CONTEXT_ROTATION=0` stays the global kill-switch). Queued/drained turns gained rotation parity (the flag rides the queue item like effort/ultracode), and rotation now defers while background children (agents/workflows/monitors) are still running — it fires on the next quiet turn end instead of SIGTERMing live sub-agents. Operator default effort dropped xhigh → high (CLI parity; xhigh/max stay one think-mode click away).
- **Ultracode goes native (spec-058 v2)** — the ⚡ toggle now flips the CLI's own ultracode switch (`--settings '{"ultracode": true}'`) instead of imitating it with a prompt: the CLI injects its standing opt-in reminders, exposes the Workflow tool's Ultracode contract (deterministic multi-agent pipelines, adversarial verification, judge panels, loop-until-dry) and pins effort to xhigh internally. Works on any model incl. Opus (verified live: Workflow tool served + a 2-agent workflow executed end-to-end on `claude-opus-4-8`). `run_engine` passes NO `--effort` under ultracode (a CLI effort flag would override the native pin); the old ULTRACODE_PROMPT contract shrank to a thin Cardloop complement (roster names + "final message carries the full synthesis"). The `--settings` payload joined the live-client fingerprint so toggling still reconnects.

### Added
- **`skeptic` sub-agent (spec-058 v2)** — read-only adversarial verifier in the default roster (Task tool + Workflow `agentType`): tries to REFUTE a claim with an evidence trail, defaults to REFUTED on inconclusive evidence — so ultracode verify stages don't rubber-stamp their own findings.

## [v0.16.1] — 2026-07-05

### Fixed
- **Deploy canary (spec-072)** — the post-restart journal scan now reads only the NEW process's log lines (by `MainPID`), so shutdown noise from the previous run can no longer trigger a false rollback; the rollback's git/npm steps run as the repo owner.

## [v0.16.0] — 2026-07-05

The "make the chat smarter" batch: one stream to rule the canvas, background runs as a
first-class citizen, file undo, real diffs, global search, and a deploy safety net.

### Added
- **Global search Cmd/Ctrl+K (spec-074)** — FTS5 index over every project's chat transcripts, timelines and boards (RU+EN); grouped results with highlighted snippets, keyboard navigation, mobile sheet; incremental background indexing + on-demand reindex endpoint.
- **File rewind (spec-073)** — SDK file checkpointing is on; every user message in history carries a ⏪ hover action that restores all agent-touched files to their pre-message state (chat history untouched; guarded against mid-turn and dead-client calls).
- **Real inline diffs (spec-073)** — Edit tool rows expand into a line-level LCS diff with add/del coloring (server-side old/new payload raised to 2000 chars); Write previews raised to match.
- **Background runs as first-class turns (spec-063 §bg)** — autonomous CLI wake-ups render live as 🌙 "while you were away" bubbles (tinted, streamed, replayable) and push a preview notification; no more answers silently waiting for your next visit.
- **E2E smoke harness (spec-072)** — scripted fake engine (`E2E_FAKE_ENGINE=1`) + Playwright suite driving the real cockpit UI (streaming, tool rows, mid-run reload reattach, busy-path queue) against a throwaway instance; opt-in via `pytest tests/e2e -m e2e`.
- **Deploy canary (spec-072)** — restart-self.sh now waits for idle before restarting (no more killed in-flight turns), then health-polls + journal-scans the new process and rolls back to the previous git tag ONCE on failure, leaving a loud incident marker.

### Changed
- **spec-063 Stage 2a** — the seq-ordered activity stream is the single render source for every turn (own sends included); the direct POST body is a control channel only. The four-writer canvas (direct SSE / bus / poll / hydrate) that bred duplicate-and-chopped-bubble bugs is gone; sub-agent lane and model-fallback strips now render live from the bus. Stage 2b (single vocabulary + dead-code deletion) remains.


## [v0.15.0] — 2026-07-05

Structural fix-pack for the spec-069-era regressions (chopped chat bubbles, replies invisible
until the next send, agents starving between turns, monitors spinning over dead work). Full
root-cause writeup: `docs/internal/diagnosis-2026-07-05-spec069-regressions.md` (spec-071).

### Added
- **Between-turns stream drain (spec-071)** — a per-client reader services the SDK stream while no turn is active: background sub-agents run at full speed between turns (they used to stall to ~1 tool round / 10 min against the SDK's bounded buffer), completion notifications flip monitors in real time, and the CLI's autonomous wake turns surface in the cockpit (`bg_turn_end` hydrate) instead of terminating the operator's next turn. `LIVE_CLIENT_DRAIN=0` to disable.
- **Completion-driven auto-continue (spec-069 P2 v2)** — the wake fires from a monitor's running→terminal transition (debounced, names the finished children, suppressed while a turn runs) instead of the blind 60s×5 poll; the budget resets on every operator turn and on rotate (it used to exhaust on phantom wakes and stay dead forever).
- **Chat-stream heartbeat + stall watchdog** — the POST /chat SSE pings every 20 s (the tunnel silently killed idle streams) and the client aborts+recovers after 75 s of silence, ending the "reply appears only when I send the next message" freeze.

### Fixed
- **Chopped mid-word chat bubbles** — background sub-agent messages (`parent_tool_use_id`) are filtered out of the main chat lane (they interleaved with the streamed answer and inflated context accounting).
- **Zombie monitors** — terminal flips now also come from `TaskUpdatedMessage` (per SDK docs some terminal states arrive ONLY there) and from a superset status map (killed/cancelled were dropped); the sweeper flips stale agents (silent transcript) and reconciles card-session agents via their own parent transcript; reconcile tail window 64→256 KB.
- **Eviction guard** — counts workflow/monitor kinds too (a TTL eviction killed a live Workflow mid-run); stuck "in-flight" pins are force-evicted after 4 h (a dead turn once pinned its client for 14 h).
- **Queued/auto-continue turns** — full parity with direct chat turns: resolved secrets + media env, effort/ultracode threading (fingerprint mismatches used to SIGTERM live children), seq-tagged live-buffer events and proper turn finish (they were invisible to hydration).
- **Cross-turn event replay** — live seq is session-monotonic, so the SSE reconnect cursor no longer silently skips shorter turns.

## [v0.14.0] — 2026-06-26

First public open-source release under IG Digital Lab. Web-only cockpit + kanban auto-run.

### Added
- **Ultracode mode (spec-058)** — per-chat ⚡ toggle: max thinking effort + sub-agent fan-out/verify for harder tasks.
- **Specs-as-epics (spec-059)** — epic-lens Specs tab tracking card progress; auto-stamp spec status → shipped on close; a discoverable Save-to-board action.
- **Second opinion (spec-060)** — optional `second_opinion` tool to consult another model family via the Antigravity `agy` CLI (auto-off when absent).
- **Nested project folders (spec-061)** — path-based sidebar folders with persisted collapse and drag-into-folder.
- **Daily update re-check (spec-062)** — the version badge auto-checks once a day and pulses an accent dot when a new version appears; self-update auto-reloads the page on success.

### Changed
- Cost usage ledger + retuned context thresholds + revived (opt-in) auto-rotation.
- Mobile: one-line composer with the toolbar relocated onto the composer.

### Fixed
- Self-update reliability: `update.sh` / `restart-self.sh` no longer abort silently when `.env` omits `CARDLOOP_SERVICE`, and the "Updating…" badge no longer hangs (reloads on success / surfaces failures with a timeout).
- Terminal: render modern TUIs (guard xterm's crashing DECRQM handler); honor OSC 52 clipboard copy; PTY→WebSocket backpressure.

### Removed
- **Telegram channel (spec-040 complete).** Cardloop is now web-only: web cockpit (PWA) + kanban auto-run. Dropped python-telegram-bot, the PTB adapter in bot.py, and the BOT_TOKEN/GROUP_CHAT_ID/ALLOWED_USERS env vars. For the legacy Telegram-enabled version, use tag v0.13.x.

## [v0.13.0] — 2026-06-23

First release-cut for public OSS. The tree is publishable (the public flip itself
— the git history rewrite — remains card a1f0c0), and the very first release
already updates itself.

### Added
- **spec-047 workstream A — in-cockpit version & self-update.** `GET /api/version` returns `{current,latest,behind,update_available,channel,can_self_update,reason}` cheaply from local git; a throttled background `git fetch` (30 min, or explicit `?check=1`) keeps it fresh and the request never blocks on the network. `POST /api/update` spawns a **detached** updater (`scripts/self-update.sh` → `update.sh --no-restart` → `restart-self.sh`) and returns `202`; on build/install failure it does **not** restart (the running version stays live; the error is recorded in `data/update-status.json`). A sidebar version badge shows `Cardloop vX.Y.Z` and turns into a one-click **Update** when origin is ahead — a non-technical operator never touches a terminal. `update.sh --no-restart` added. 11 tests (`tests/test_version_update.py`).

### Changed
- **spec-047 workstream B — pre-publish gate: HEAD is now publishable.** English-only across shipped code, UI, docs, runtime templates (`templates/reference/`) and the entire test suite (Russian *data* fixtures intentionally kept). Zero personal-data / OPSEC / secret-value leaks in tracked files; one canonical placeholder set (`igdigitallab/cardloop`, `@YOUR_BOT`, `YOUR_DOMAIN`). §0.6 decision applied: internal design docs (`specs/`, `DONE.md`) and live per-instance board state (`TASKS.md`, `DONE.md`) are gitignored (a fresh clone scaffolds from `templates/*.tpl`); `CLAUDE.md` + `GOTCHAS.md` translated to English and kept public. `.coverage` untracked + ignored.
- **card 45ae3c — English-only quality-gate matchers.** `webapp.py` conformance checks now match the shipped English template markers (`"Cockpit Rules"`, `"Card format"`) instead of Russian, so a fresh English project passes the gate.

### Added (prior, since v0.12.0)
- **spec-039 — stop killing sessions (cards b1dc7d, c8a86f).** `PERSISTENT_CLIENT=1`: the `claude` CLI subprocess persists across turns so `run_in_background` Bash tasks survive, and native auto-compact replaces the old custom rotation (no more session auto-reset). Removed custom rotation, auto-resume-on-429, and the stall-watchdog (kept only a 2h max ceiling). Manual `/reset` + cockpit "Wrap & reset" now evict the live client. Graceful + fast SIGTERM shutdown (flush sessions, bounded teardown). Cockpit shows the truth (fill bar to 200K, compact toast, 200K-wall card). Spec: `specs/spec-039-stop-killing-sessions.md`. Constraint discovered: 1M context is API-key + Sonnet only → unavailable on this opus+subscription path, the 200K wall is fixed.
- **Inline video in cockpit chat (card adb7ea).** Extends spec-038: media route serves mp4/webm/mov/ogg, the `cockpit-img` helper accepts video (200 MB cap), frontend renders a `<video>` thumbnail + lightbox, Range/seeking supported.
- **Chat: durable send queue + faithful tool-log replay (card 51a612).** Queued outgoing messages persist to `data/chat-queue.json` (survive restart); replayed tool logs now carry full detail (cmd/output) identical to the live stream (the replay buffer used to store the unformatted event).
- **Chat: stick-to-bottom scroll (card d378a6).** Auto-follow only when pinned within ~80px of the bottom; otherwise a "↓ New messages" pill — reading scrolled-up history is no longer interrupted by incoming events.
- **Per-tab activity + attention badges (card b2a081).** The open-tabs strip shows a working dot while the agent runs and an attention badge when a background tab is awaiting the operator (clears on focus). Uses the single shared activity SSE — O(1) connections, no per-tab streams.
- **spec-040 — decouple core from Telegram (card 4698ec, design).** 4-phase plan (neutral session keys → extract `engine.py` → cockpit-only behind a flag → remove PTB) + full coupling inventory + open questions. Design only. Surfaced a latent bug: `TELEGRAM_NUDGE` is the default `system_prompt` for all callers including the cockpit (to fix in Phase 1).
- **Cockpit settings — "⚙️ Settings" tab + global settings (card f2ba02).** Per-project (topics.json, hot-reload): **git on/off** (flagship — off → cards run in legacy mode without worktree, git-sync returns 409, health doesn't require .git, `.git` is not physically touched; sessions are preserved), model, self-healing, TG notifications, log_cmd, test_cmd. Global (new `data/settings.json`, mtime hot-reload, wired into runtime): self-healing master kill, max concurrent repairs, scanner interval, default model for new projects, watchdog stall/max. API: `GET/POST /api/settings`, `GET/POST /api/projects/{id}/settings` (type/range validation). Helpers `_get_global_setting`/`_git_enabled`/`_effective_default_model`. 20 tests (`test_settings.py`).

### Fixed
- **Card auto-run crashed with `KeyError: 'id'` when a project dict lacked `id` (pre-existing, from spec-038 media injection).** Guarded `_run_card` with `project.get("id")` — skips the cockpit-media env injection when absent; card runs proceed normally. Full test suite now green (0 failures, was 9). Test `test_run_card_no_project_id_does_not_crash`.
- **Backlog "add task" truncated long text (card d1ebd5).** Removed a 120-char client-side cap in `BoardTab.addCard()`; full multi-line text now round-trips through the board.
- **Modes/session bar wrapped to a second line; project cards too tall (card 29b29a).** `.chat-session-bar` no longer wraps (`flex-wrap:nowrap` + horizontal scroll + `nowrap` buttons); `.project-item` padding tightened 7→5px.
- **spec-039 SIGTERM shutdown hung ~90s then SIGKILL (regression, fixed same session).** The handler flushed sessions but the process never exited: the aiohttp `AppRunner` was never cleaned up and 5 webapp background loops were never cancelled, so `asyncio.run()` waited until the systemd stop timeout. `webapp.stop()` now cancels the loops + `runner.cleanup()`, and `_amain` bounds the whole teardown with `asyncio.wait_for(12s)` + cancels lingering tasks. Verified: restart 93s→6s, clean "Deactivated successfully".
- **Project rename lost all conversation history and Timeline.** `api_project_rename` moved the folder (`shutil.move`) and updated `topics.json`, but SDK history (`~/.claude/projects/<slug>/`) and Timeline (`data/timeline/<slug>.jsonl`) are keyed by `slug = cwd.replace('/','-')` — after changing cwd the cockpit read an empty new slug, and "all sessions appeared to disappear" (files were intact under the old slug). Added `_migrate_cwd_keyed_state(old_cwd, new_cwd, ctx)`: moves the SDK sessions directory + Timeline (+`.jsonl.1`) to the new slug, best-effort, warnings in response `warnings`. Tests: `test_rename_migrates_sdk_sessions`, `test_rename_migrates_timeline`. Already-lost projects recovered by moving orphaned directories.

## [v0.8.2 – v0.12.0] — 2026-06-11 → 2026-06-23

Five tags cut as stable points without an individual note each; what shipped, from the tagged commits: v0.8.2 (06-11) spec-026 phase 0+1 — login rate-limit hardening + LAN firewall; v0.9.0 (06-12) spec-026 security hardening complete, all phases deployed; v0.10.0 (06-13) spec-039 — a persistent CLI client so sessions stop being killed, plus a batch of backlog cards and the spec-040 design; v0.11.0 (06-23) board reconcile; v0.12.0 (06-23) compact, kebab-only card actions. Full detail: `git log v0.8.1..v0.12.0`.

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

> GROK = operator runbook for the Grok Build provider. Traps → GOTCHAS.md §Grok. HTTP contract → docs/API.md. Code map → ARCHITECTURE.md.

# Grok Build in Cardloop (third provider, off by default)

## What it is — and is not

- A chat, a board card or a project default can be pinned to `provider="grok"`. The turn runs the **official `grok` binary** on your **SuperGrok subscription**; Cardloop never calls the xAI API.
- **Transport:** one `grok agent --no-leader stdio` process per turn, spoken to over ACP (JSON-RPC lines). It lives in its own process group and is killed in `finally` — nothing outlives a turn or a deploy. Stop = `session/cancel`, then `killpg` after 5 s. There is **no turn timeout** (same as Codex): a model that goes silent runs until you press Stop.
- **Subscription-only, fail closed.** No API-key path exists: `XAI_API_KEY` is never passed, and the engine refuses a login that is not the grok.com OIDC kind, is API-billed, belongs to another account than `auth.json`, or lacks `coding_data_retention_opt_out`. All four are re-checked against what the agent itself reports on **every** turn.
- **Capabilities:** chat, board cards, history, search, usage, interrupt, multi-agent hint. **Not supported (v1):** plan mode (a plan turn is an error, never a silent run — ACP's plan mode blocks the edit tools but not shell side effects) and "Ask me" (a visible 409). Skills/plugins are off.
- **Limits are NOT reported by Grok.** The runtime pill shows `—` / "limits not reported", never a bar; the Usage tab shows turns and tokens plus a local 5 h / 7 d counter. The first quota-shaped failure is saved raw to `<DATA>/grok_limit_errors.jsonl`; no parser exists until a real sample does.
- Not inherited from Claude/Codex: account pinning, ultracode, auto-rotate, board reconciler, rate-limit auto-resume, the FTS search index, the legacy flat `sessions.json` map.

## Prerequisites

- The Grok CLI: `curl -fsSL https://x.ai/cli/install.sh | bash` (lands in `~/.grok/bin/grok`; `GROK_BIN` overrides, then `grok` on `PATH`).
- **bubblewrap** (`bwrap`) on `PATH`. Without it Grok refuses to start and so does Cardloop — there is no unsandboxed fallback.
- A SuperGrok subscription, signed in under the cockpit's **own** `GROK_HOME` (below).

## Enable (once)

1. `.env`: `GROK_ENABLED=true`. Optional knobs are in `.env.example` (`GROK_BIN`, `GROK_HOME`, `GROK_SANDBOX_DENY`, `GROK_ALLOW_ALL_PROJECTS`, `GROK_MODEL`).
2. `tools/grok-acct login` — the ordinary `grok login --device-auth` flow run with `GROK_HOME` set (default `<DATA>-grok-home` — next to the data dir, e.g. `<repo>/data-grok-home`; mode 700, must not be a symlink, must not be inside the data dir). Approve the printed code in any browser signed in to the grok.com account. `tools/grok-acct` never copies tokens from anywhere (the test tools borrow a login into a throwaway home and delete it); your interactive `~/.grok` is untouched. `tools/grok-acct status` shows the verdict without secret values; `logout` signs out that home only.
3. Restart the cockpit. A background startup probe journals `[grok] ready via oidc auth (N models, CLI <version>)` or `[grok] unavailable; Claude remains active: <reason>`. The probe includes **one real model turn** (~15k tokens, effort low) that tries to read a canary file from the model's shell; its verdict is cached on disk by CLI version + deny list + home (ok for 7 days, failed/inconclusive for 15 minutes). First start, every CLI bump and every deny-list change cost one probe turn. Until it says `ok`, Grok is unavailable.
4. `make doctor` — the `Grok …` facts must all be ✓ (table below).
5. Opt a project in (next section). Only then can a chat or card choose Grok there.

With `GROK_ENABLED` unset the provider is invisible: no registry row, no UI element, no doctor line, and the payloads equal a Grok-free install.

## Privacy: the per-project opt-in (default OFF)

Every Grok turn sends that project's prompts, the files and command output the model reads, and the project's `CLAUDE.md` (as session rules) to xAI. It cannot be recalled afterwards, and zero data retention is not available to individual accounts.

- **Gate:** `grok_allowed: true` on the project (Settings → "Allow Grok in this project"; strictly the JSON boolean — `"true"` or `1` in a hand-edited record does not count). Without it every selection and every run is refused: HTTP **409** `{"error":"grok is not enabled for this project"}` on chat/free-chat create, runtime switch, queue accept, chat POST, card create/edit/move and the board default; a queued message or a card whose project lost the flag fails visibly (chat error / Failed column) and **never** falls back to another provider. Revoking is never refused.
- **`GROK_ALLOW_ALL_PROJECTS=true`** skips the gate for single-tenant boxes where every project may go to xAI. Read live.
- **Free chats are rooted at `$HOME`.** A record whose `cwd` is `$HOME` or an ancestor passes only under that hatch (the 409 then appends why): one opt-in would otherwise give Grok every project and dotfile in the home directory. The engine refuses such a `cwd` a second time as a backstop.
- **Keep untrusted clones out.** A repo's `CLAUDE.md` rides in the *system* prompt, so a cloned third-party repo can steer the model. `grok_allowed` is the only control against that. Two measured reasons it matters: the model's shell can read Grok's own login (`<GROK_HOME>/auth.json` — the agent and its shell share one sandbox), so a prompt-injected turn could send the SuperGrok token away (`tools/grok-acct logout` + `login` if a turn did something odd); and it can write instruction files into `GROK_HOME` that every other project's next turn would load — the engine deletes those (`rules/`, `AGENTS.md`, `skills/`, `agents/`, `lsp.json`, `settings.json`, …) before every turn.
- **What the egress canary proves — and does not.** `pytest tests/test_grok_live.py -m grok_canary` builds a repo with an 8 MB incompressible blob in git history, sends "reply OK, use no tools", samples the connections of the Grok process group and fails above 1 MiB on any one. On grok 1.0.46: max 63.7 KB per connection. One turn on one build on one account: xAI's retention is server-side and can change without a CLI update, and the canary measures upload volume, not what xAI keeps. Re-run it after **every** CLI update and before enabling a new project. The engine's own `coding_data_retention_opt_out` check relies on xAI's flag.

## Isolation model in plain words

- **Hermetic environment.** The child gets an allowlist (`PATH`, `HOME`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `TZ`, proxy and TLS-cert vars), never the cockpit's own env — no `WEB_PASSWORD`, VAPID keys, `ANTHROPIC_*` or `XAI_*`. On top: switches that stop Grok importing the Claude/Cursor/Codex compat surface (MCP servers, hooks, skills, rules, agents, foreign sessions), telemetry, memory, `ask_user_question`, auto-wake and workflows. `GROK_SANDBOX=cardloop` selects the custom profile; auto-update is off in env and config.
- **Custom sandbox, always.** `<GROK_HOME>/sandbox.toml` is regenerated every turn: profile `cardloop` extends the built-in `workspace` (writes only in the project and `/tmp`) plus a deny list of credential paths the model's shell cannot read: `~/.claude`, `~/.claude-accounts`, `~/.claude.json`, `~/.cursor` (the **always-denied floor** — `GROK_SANDBOX_DENY` cannot remove it), `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.config/gcloud`, `~/.git-credentials`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.docker`, `~/.kube`, `~/.azure`, `~/.oci`, `~/.codex`, shell history, the operator's own `~/.grok/auth.json`, the probe canary, the cockpit's own `.env` and its whole data dir (one directory entry, always applied), and globs `**/.env`, `**/secrets.env`, `**/*.pem`, `**/*.key`. `GROK_SANDBOX_DENY` (comma list) **replaces** the defaults, not the floor. Entries that do not exist are skipped (Grok would create them as files); nested entries are pruned; an entry that would hide the binary, `GROK_HOME` or `$HOME` is refused, never silently dropped. It is a blacklist, not a boundary: a secret in a path nobody listed is readable. The boundary is the opt-in plus the scrubbed env.
- **Tracked files matching a deny glob** become unreadable to `git add -A` inside the sandbox: a repo that tracks a `*.pem`/`.env` fixture cannot be committed by a Grok turn. Credential protection wins; commit yourself.
- **Folder trust is THE gate for a project's own MCP servers, hooks and skills.** Headless ACP treats every folder as untrusted and the engine pins `GROK_FOLDER_TRUST=1` (gate **on**; `=0` would start everything — measured). `grok inspect` **lists** a project's `.mcp.json` / `.grok/` entries as active but a real turn starts none of them; do not read inspect's listing as "will run". The engine refuses a turn when `<GROK_HOME>/trusted_folders.toml` holds any entry, the model's shell cannot write that file or `config.toml` (measured), and a project's own config cannot lift trust.
- **Wire tripwire.** If the agent ever reports an MCP server, a hook execution or an MCP-style tool name (`__`), the turn aborts with an isolation error. It detects; it does not prevent the start — prevention is trust + the deny floor.
- **`GROK_HOME` + litter.** Login, sessions, config and sandbox profile live in `GROK_HOME` (default `<DATA>-grok-home`, beside the data dir). Every sandboxed spawn leaves `sandbox-blocked*` placeholders (mode 000) there; the engine's reaper removes those of its own pid and of dead pids after each turn.
- **Rules.** `<project>/CLAUDE.md` (≤ 24 KiB) is delivered as session rules; `~/CLAUDE.md` never is. A resumed Grok session keeps the system prompt it was born with, so an edited `CLAUDE.md` applies only to a **new** session.
- **Model writes.** Told to "remember", the model may write an `AGENTS.md` into the project; a later session there loads it as rules. Review it.

## Handoff between providers, and the send ledger

Switching a chat to or from Grok goes through the spec-092 handoff (the preview is shown first and editable; the block is armed on the chat and cleared when the new runtime answers with an id).

- **A Grok session file is not evidence of what you said.** The model's own shell can write under `GROK_HOME` (measured: it appended a forged `<user_query>` row to its own history). So the cockpit keeps a ledger, `<DATA>/grok_sent/<session-id>` (SHA-256 of every prompt it sent, including context pack and handoff block; dir 0700, files 0600), and a user row is **verified** only if it matches. Handoff **out of** a Grok chat is built by the server from the session file, ignoring the rows the browser holds; unverified user rows are never carried as "Standing constraints" and the block says `## Warning: N unverified user row(s) left out` (previews go to you only, never into the block). Assistant lines are framed as that model's output.
- Fails closed: sessions that predate the ledger, and turns that died before a new session's id was known, show their user rows as unverified.
- A project whose own directory **contains** the cockpit's data dir or `GROK_HOME` (the cockpit repo itself) is refused outright — the shell would be able to write cockpit state — so the cockpit's own checkout can never be a Grok project (`GROK_ALLOW_ALL_PROJECTS` lifts only that check).
- Manual rotate on a Grok chat clears Grok's own session id and arms a chat-scoped handoff built locally from Grok's history (no model call; the Claude summariser is a cloud call). A resume id Grok no longer has is dropped at the run site: new session, journal line `[grok] … no longer exists`.

## History, search, usage

- **History / session list / switch** read the session files (`<GROK_HOME>/sessions/<urlencoded cwd>/<id>/`) with no agent process, also with Grok off. Rows have no timestamps; the rewind button is hidden; at most 100 messages by default. User rows carry `verified`.
- **Search** scans Grok sessions live, one project at a time, only for projects the gate allows (2.5 s wall-clock budget, 32 MiB byte budget). Not in the FTS index.
- **Usage:** `providers.grok` in `GET /api/usage/dashboard`, built from the engine's own ledger `<DATA>/grok_usage.jsonl`. `notional_usd` is the API-list-price equivalent, never spend. Session files and `signals.json` are model-writable, so usage is **never** read from them.

## Troubleshooting (keyed by `make doctor`)

| Doctor fact | Meaning | Do |
|---|---|---|
| `Grok CLI` ✗ | binary missing / not executable / bad `--version` | install, or set `GROK_BIN` |
| `Grok CLI` ⚠ | version not in `KNOWN_GOOD_VERSIONS` | verify (CLI-bump section) before trusting it |
| `Grok auth` ✗ | no login, not OIDC, or retention opt-out not true | `tools/grok-acct login`; fix the account setting at xAI |
| `Grok sandbox (bwrap)` ✗ | bubblewrap not on `PATH` | install it |
| `Grok sandbox (GROK_HOME)` ✗ | symlink or not a dir | use a plain directory |
| `Grok sandbox (profile)` ✗ / ⚠ | invalid `GROK_SANDBOX_DENY`, or `sandbox.toml` stale/not generated yet | fix the list; the engine rewrites the file on the next turn |
| `Grok sandbox (probe)` ✗ | deny did NOT hide the canary: turns are refused | check bubblewrap/Landlock, delete `<DATA>/grok_sandbox_probe.json`, restart |
| `Grok sandbox (probe)` ⚠ | no verdict, `inconclusive` (model refused / never ran the command), or stale | self-heals (retry after ~15 min); `journalctl -u cardloop \| grep '\[grok\]'` has the reason |
| `Grok compat` ✗ | an MCP server is active in the global config | read the named source; the compat env switches should hide it |
| `Grok folder trust` ✗ | trust store non-empty or `GROK_FOLDER_TRUST` not pinned | empty `<GROK_HOME>/trusted_folders.toml` |
| `Grok compat (projects)` ✗ / ⚠ | a gated project's folder is trusted, or a hook/skill is active | same fix; `grok inspect --json` in the project under the engine env |
| `Grok processes` ✗ | `grok agent` older than 15 min | `kill -TERM -- -<pgid>` (the remedy prints it) |
| `Grok litter` ⚠ | > 20 `sandbox-blocked*` entries | the printed `reap_litter` one-liner |
| `Grok usage files` ⚠ | `grok_usage.jsonl` > 10 MiB or limit-error file > 2 MiB (no rotation) | archive them |

Also: `Grok sandbox check failed — refusing to run` = the probe verdict is `failed`; `Grok sign-in expired — run tools/grok-acct login` = a handshake step timed out (a missing login makes `authenticate` hang, not error); `refusing to run Grok in <dir>` = the cwd is `$HOME` or an ancestor.

## CLI bump procedure

The stream format, flags and compat behaviour of a 1.0.x CLI can change under you; auto-update is off. An unknown build only **warns** (registry `warnings`, doctor ⚠) — turns keep running — so a bump is a deliberate gate, `tools/grok-verify`:

1. Install the new build yourself (`grok update` / the installer). `make doctor` now shows `Grok CLI` ⚠ "not on KNOWN_GOOD_VERSIONS".
2. `tools/grok-verify check` (add `--login <GROK_HOME or auth.json>` to borrow a login other than `~/.grok`; `--dry-run` prints the plan and spends nothing). It copies that login into a scratch `GROK_HOME` (mode 600, deleted afterwards — never the cockpit's own home) and runs, in order: `grok_live` (isolation with marker-file positive controls, sandbox denial, one real turn), `grok_canary` (egress < 1 MiB per connection) and a fixture re-record into a temp dir whose event/field **skeletons** are diffed against `tests/fixtures/grok/` (ids and text ignored). `--drift removed` (default) fails when a method, field or event type disappeared or changed, `any` also fails on additions, `report` never fails. A skipped test counts as a failure (`--allow-skips`); skipping a step by flag makes the verdict PARTIAL (exit 1).
3. Only when every step passed and the build is not yet listed does it print the one-line edit for `KNOWN_GOOD_VERSIONS` in `grok_engine.py`. It never edits a file. Apply and commit it. The sandbox probe fingerprint includes the CLI version, so the next provider check re-runs the self-test turn by itself.
4. Optional soak: `tools/grok-verify soak` — one tiny real turn per 20 minutes for 24 h through `run_grok_engine` under the production profile (`--duration`, `--interval`, `--cycles 2 --interval 5` is the smoke run). Each cycle asserts no leftover `grok agent` process, bounded `sandbox-blocked*` litter and `GROK_HOME` growth; the egress canary runs every 12th cycle (`--canary-every`); one JSON line per cycle goes to the log. It costs subscription quota (about one turn per cycle plus one probe turn).

Exit codes: 0 = everything passed, 1 = a check failed or the run was incomplete, 128+N = stopped by signal N. Output never contains the login's values. The pieces can be run by hand: `tools/grok_record_fixtures.py --scenario all`, `pytest -m grok_live`, `pytest -m grok_canary`.

## ToS posture

Official `grok` binary as a subprocess only. Cardloop reads from `auth.json` only the non-secret fields (auth mode, retention flag, email), never passes, copies or logs token values, and never calls `api.x.ai` itself. Login is xAI's own device flow. xAI's acceptable-use terms restrict unauthorised automated access and, as read on 2026-10-02, endorse subscription OAuth only for named integrations — the residual risk is an account suspension, and Cardloop does nothing to disguise its use. Re-read the terms before relying on it.

---
created: 2026-06-21
updated: 2026-06-21
status: draft
relates_to: spec-014 (oss-hardening, prior), spec-015 (oss-runtime), spec-013 (multi-user), spec-040 (decouple-telegram)
scope: full pre-open-source audit, sequenced ship-first (0 → 1 → 2 → 3)
reviewers: backend-quality, frontend, oss-readiness, architecture, legal/ToS, competitive, security, git-history-sweep, repo-health (9 parallel read-only passes)
---

# Spec 041 — Open-Source Readiness (ship-first)

## Framing

The repo (`Zira777ru/claude-ops-bot`, currently **private**, 0 stars) is going public.
The goal is not "passes an audit" — it is "this is the coolest, best-presented version of the
project." Those are different goals and imply a different order of work.

**Guiding truth: stars are not earned by clean code.** Clean code is noticed only by the ~2%
who open a source file. The first thousand stars come from three things: the project hits a
real pain, a visitor gets "what is this and why is it cool" in 60 seconds, and it runs without
a fight. Code hygiene is third-order — it converts a visitor into a contributor.

Sequenced **ship-first**:
- **Phase 0 — Don't embarrass yourself / don't get sued** (blockers; precede any push)
- **Phase 1 — First impression** (what actually earns stars)
- **Phase 2 — Runs in 5 minutes** (retention → contributors)
- **Phase 3 — The code** (declutter, then decompose)

**Perfectionist trap to avoid:** "I'll refactor everything perfectly, then publish." That is
how projects die unpublished. Do Phase 0 + a strong Phase 1 + a working Phase 2, then **ship**.
Do Phase 3 in the open, in public issues, possibly with contributors. The star counter only
starts after the push.

## Verdict

Engineering bones are **strong** and will not draw mockery. Confirmed by a real test run:
**1396 passed, 0 failed, 8 skipped, ~53s** (`env -u WEB_COOKIE_SECURE venv/bin/python -m pytest`,
84% line coverage overall). Clean isolated modules (`board.py` 96%, `totp.py` 99%,
`secretstore.py` 93%), a transport-agnostic engine (Telegram / web / kanban-card all consume one
`run_engine` generator), correct crypto auth (scrypt + constant-time compare + exponential
backoff), double-layer path-traversal defence, atomic secret writes, a C2 destructive-command
gate, env excluded from logs.

Two structural facts to manage, not launch blockers: `webapp.py` is a **10,144-line monolith**
(330 functions, 80+ routes; 65% coverage) and the frontend `ChatTab.tsx` is **2,639 lines**.
Phase 3.

Engine question settled: **stay on the PWA, do not build native** (rationale at the end).

---

# Phase 0 — Don't embarrass yourself / don't get sued (BLOCKERS)

🔴 Non-negotiable, must precede any public push. Three sub-areas: **leaks**, **legal**, **security**.

## 0-A — Leaks: tracked files + git history

### 0.1 — `khronika` (political OPSEC zone) in HEAD — RED ALERT

| File | Line | Content |
|---|---|---|
| `tests/test_forum_topic.py` | 193, 198 | `"khronika"` as a fixture project name (worst — live code) |
| `specs/spec-011-monitoring-ui-refactor.md` | 38 | `scripts/khronika-web-logs.sh` |
| `specs/spec-018-server-janitor.md` | 31 | "loose SQL dumps from the `khronika` project" |
| `specs/spec-030-project-list-redesign.md` | 69 | `🟡 khronika-portal` |

Replace all with a neutral name. Also `line_vpn_bot` (OPSEC-linked) in
`templates/reference/project-baseline.md:16-48`.

### 0.2 — Git history rewrite (scope wider than first thought)

History is **not catastrophically dirty** — no real API tokens, committed `.env`, or private
keys. But it leaks PII/infra/OPSEC. `git-filter-repo` is sufficient; **orphan/squash not
needed**. Two operations:

**(a) `--mailmap` — the biggest miss:** the git **Author field of every commit** is
`Igor <zira777ru@gmail.com>`. `--replace-text` does not touch author headers. Run:
```
git filter-repo --mailmap mailmap.txt
# mailmap.txt:  Your Name <you@example.com> Igor <zira777ru@gmail.com>
```

**(b) `--replace-text replacements.txt`** for everything below (incl. spec-014's
`282311426`, `@ziraclaudebot`, `coscore.us`, `/home/igor`):
```
zira777ru@gmail.com==>you@example.com
282311426==>SYNTHETIC_TG_ID
@ziraclaudebot==>@your_bot
ops-igor-2026==>REDACTED_DEFAULT_PASSWORD     # former cockpit default pwd, in CLAUDE.md history
networking-os/secrets/tg.session==>YOUR_PROJECT/secrets/tg.session
1780365319==>SYNTHETIC_THREAD_ID              # real project chat id, in DONE.md/TASKS.md
pve==>your-hypervisor
proxmox-tunnel==>your-tunnel
coscore.us==>example.com
crm.coscore.us==>crm.example.com              # VaultWarden test fixtures in history
firecrawl.coscore.us==>firecrawl.example.com
config.yml.bak-claudeops==>cloudflared-config-backup
/home/igor==>/home/youruser                   # 107+ lines across history
```
Coordinate the force-push (origin already has the dirty commits). Verify after with a fresh
`git grep` over `--all`.

### 0.3 — Hardcoded production server UUID

`schedules.py:718`: fallback `"f0kgss8ccgksokkscgc0sk4s"` is a real prod Coolify server UUID
(board card `ops:58412e`). Make fallback `""`, env var required (fail loud).

### 0.4 — Committed `.service` with personal paths

`claude-ops-bot.service` (tracked) hardcodes `User=igor`, `/home/igor/claude-ops-bot`. A
`.service.template` with `__USER__` exists. `git rm --cached`, gitignore it, point docs at the template.

### 0.5 — Personal paths / domains / names in HEAD (full list)

| File | Content |
|---|---|
| `tests/test_janitor_quarantine.py:17` | `Path("/home/igor/server-janitor/janitor-quarantine")` — external path → **also breaks CI** (see 2.6) |
| `tests/test_is_destructive.py:37` | `coolify.coscore.us/...` → `coolify.example.com` |
| `tests/test_phase0_session_keys.py:33,38,108,109,119` | `/home/igor/projects/sac-tech`, `/home/igor/rightforms-app` → `/tmp/test-project` |
| `tests/test_schedules.py:875,906-968` | `/home/igor/networking-os/.venv/python`; `proxmon-bot`, `pyrogram_bot` fixtures → `example-bot` |
| `templates/reference/project-baseline.md` | `line_vpn_bot`, `pyrogram_bot`, `proxmon-bot`, `~/vault/...` → generic |
| `TASKS.md:11,13,53,63` | "племяннице", "по запросу Игоря", `~/vault/...` |
| `DONE.md:94` | `PRIVATE Zira777ru/claude-ops-bot` |
| `board.py:10` | `github.com/igor/claude-ops-bot/...` — wrong/personal URL |
| `GOTCHAS.md:53` | `igor в группе adm` → "the operator's user" |
| `CLAUDE.md:7` | `~/vault/01-Projects/Claude-Ops-Bot/specs/` → `docs/` |
| `specs/spec-013:14`, `spec-016:263`, `research-agent-skills.md:249,269` | "Игорь", "Игорь's laptop", "igdigi, SacTech" → generic |

### 0.6 — Fate of internal docs

`DONE.md` (34 KB), `TASKS.md`, `CLAUDE.md`, `GOTCHAS.md`, 40 all-Russian `specs/*` carry personal
context. Recommendation: keep `ARCHITECTURE.md` / `README.md` / `CONTRIBUTING.md` / `docs/API.md`
(English, good); sanitize `GOTCHAS.md`; move `TASKS.md`/`DONE.md` to `docs/internal/` (gitignored
or private branch); keep `specs/` only after sanitizing — as "design history" it is a plus.

## 0-B — Legal

### 0.7 — Trademark disclaimer ("Claude™")

Anthropic holds the registered "CLAUDE" trademark (US Class 42, AI software) and enforces it —
they C&D'd the "Clawd" project at scale (Nov 2025 → renamed OpenClaw). Lowercase `claude` in the
name does not remove the risk. Add to README top (and `package.json` description + repo About):
> "Claude" is a trademark of Anthropic, PBC. This project is not affiliated with, endorsed by, or
> sponsored by Anthropic. It wraps the official `claude` CLI, which you install separately.
🟡 Important — not a hard blocker, but a C&D is plausible at scale. See 0.11 (rename decision).

### 0.8 — ToS compliance notice (the real legal risk) 🔴

Anthropic Consumer ToS §3.7 prohibits automated subscription access *except* via the official
CLI — which this project uses, so the personal path is permitted. **But** hosting this as a
service for *other users* to authenticate with *their own* Claude subscriptions is **not**
permitted (the OpenClaw ban). The danger: README/docs implying multi-user or hosted use on
subscriptions. Add a "Terms of Service Notice" to README:
- this project invokes the official `claude` CLI; it does not touch the API or OAuth tokens directly;
- you are responsible for your own Anthropic subscription compliance;
- for multi-user / commercial deploys, use an **API key** (`ANTHROPIC_API_KEY`), not a subscription;
- `bot.py:65` intentionally `os.environ.pop`s `ANTHROPIC_API_KEY` to force CLI-subscription auth —
  document this and make API-key mode a first-class option, not a silent pop (ties to 2.3).

### 0.9 — Dependency licenses: clean, but NOTICE required

No GPL/AGPL anywhere — no copyleft conflict with MIT. `claude-agent-sdk` is MIT. Frontend deps
are ISC/Apache-2.0/OFL-1.1/BSD/CC-BY-4.0 — all MIT-compatible. **Apache-2.0 deps require a NOTICE
file** for distribution. Create `NOTICE` in repo root attributing: TypeScript (Microsoft),
ESLint (OpenJS), Chevrotain/mermaid parser (SAP), Geist font (Vercel, OFL-1.1), Lucide (ISC).

### 0.10 — LICENSE author de-anonymization

`LICENSE` line `Copyright (c) 2026 Igor (Claude-Ops contributors)` → `claude-ops-bot contributors`
or a neutral handle. (Pairs with the `--mailmap` rewrite in 0.2.)

### 0.11 — Project name decision (gate before push)

`claude-ops-bot` carries the trademark term and is weak marketing ("ops-bot" reads internal).
- **Option A — keep:** mandatory disclaimer (0.7), accept C&D risk at scale, zero ecosystem churn.
- **Option B — rename:** trademark-neutral, stronger identity (candidates: `agentdesk`, `cliforge`,
  `opsboard`). Do it BEFORE push — renaming after 1k stars hurts links/forks.

Recommendation: if the goal is max stars + longevity without legal friction, lean rename. If it
stays a niche personal tool, keep + disclaimer. **Decide consciously before publishing.**

## 0-C — Security: fix-before-publish (deployed by strangers)

Full findings in the security pass; the 🔴 set that should be fixed (or consciously accepted +
documented) before public deploy:

| # | Issue | File:line | Severity | Fix |
|---|---|---|---|---|
| R1 | `log_cmd`/`test_cmd` settings = arbitrary RCE — value stored raw, `shlex.split` + exec by background scanner | `webapp.py:3401, 2506` | Critical 9.8 | Allowlist formats (`journalctl -u`, `docker logs`, `tail -f <file>`); reject shell metachars |
| R2 | `GET /api/secrets/{name}` returns decrypted plaintext, cookie-only (30d) | `webapp.py:8862-8888` | High 7.5 | Re-auth before reveal; document that vault is readable by an authed session |
| R3 | Rate-limit bypass — `_client_ip` trusts `CF-Connecting-IP`/`XFF` unverified | `webapp.py:497-509` | Med-High 6.5 | `TRUSTED_PROXIES` env; don't trust CF header without `CF-Ray` |
| R4 | TOTP no replay protection — code valid full 30s window | `webapp.py:1348`, `totp.py:104` | Med 6.0 | Track last-accepted counter per secret; reject ≤ |
| R6 | Trash restore: `original_cwd` from sidecar JSON not allowlist-checked before `shutil.move` | `webapp.py:1748-1762` | Med 5.3 | Call `_path_allowlist_check()` before move |
| R7 | No HTTP security headers (X-Frame-Options, X-Content-Type-Options, CSP) | `webapp.py:10050+` | Med 4.5 | Add middleware: DENY framing, nosniff, Referrer-Policy |
| Y2 | Global file API reads all of `$HOME` incl. `~/.ssh`, `~/.claude/.credentials.json` (only `.env*` filtered) | `webapp.py:6130-6188` | Med 4.5 | Exclude `.ssh`, `.gnupg`, `.claude`, `.config/claude-ops` |

Also fold in: R5 cookie `Secure=false` default → quickstart must warn + force when not localhost;
Y5 handoff/summary agents run `bypassPermissions` (harmless now, footgun) → use `default`;
Y1 recovery codes 32→64 bit (`totp.py:161`). Done well (keep, advertise): C2-gate, scrypt+const-time,
double path-traversal defence, secrets never logged, exec-array (no `shell=True`), atomic writes.

---

# Phase 1 — First impression (the star engine)

Most people star without opening a source file. Spend a day here on presentation.

### 1.1 — README hook + "why this exists"

- Killer first line. Candidate: *"The kanban board is your agent's working memory — not a UI
  layer. Cards move themselves."* or *"A personal ops center for Claude agents: describe a task,
  watch it ship from your phone."*
- A personal-story paragraph (HN rewards narrative over feature lists).
- Badges (CI, license, Python ≥3.11), feature list, demo link.

### 1.2 — 20-second demo GIF (highest-leverage asset)

Show the wow-moment: card → In Progress → agent works it autonomously → diff appears → ping.
First 15 seconds = the hook. Embed at the top of README. Competitors' demos show UI, not this.

### 1.3 — Architecture diagram in README

Mermaid component + data-flow diagram (already produced in the audit). One glance signals "thought
with their head" — rare in this niche.

### 1.4 — Security Model section (candor builds trust)

State plainly: agents run `bypassPermissions` = full host access by design; single-user assumption;
subscription-vs-API auth; and the concrete items from 0-C the user must know (R1 RCE-by-design for
authed user, R2 vault reveal, Y2 `$HOME` read, HTTPS+`WEB_COOKIE_SECURE=true` required, in-memory
rate-limit resets on restart). Honesty here reads as maturity; hiding it and handing someone an
`rm -rf` is the real embarrassment.

### 1.5 — Competitive positioning paragraph

Market is real but the "personal AI ops center" niche is open. Neighbours (approx stars):
vibe-kanban ~27k (sunset), opcode/Claudia ~21k (abandoned Aug-2025), claudecodeui ~12k,
claude-squad ~8k, claude-code-telegram ~2.7k, amux ~200 (closest in spirit, monofile),
awesome-claude-code ~47k (the distribution hub). Add a 4-5 line "How this is different" — not a
feature grid: *"Most agent tools are a chat box — finish the chat and you lose track of what's
done. Here the board is the source of truth, and agents update it themselves."*
Our objective differentiators: kanban-as-working-memory (TASKS.md = on-disk truth, agent moves
cards); three transport channels sharing one session (TG + PWA + autorun); subscription-first (no
API key); mobile-first PWA done right; production-grade internals (1396 tests). Weaker: Claude-only
(competitors are multi-provider), webapp.py monolith (own it as a Phase-3 public issue).

### 1.6 — Reframe subscription-auth as a FEATURE, not a warning

Current README files it under "Authorization: important warning." Move to Features:
**"Runs on a Claude Max subscription — no API key, no per-token billing."** This is the killer hook
for r/ClaudeAI. (Keep the ToS nuance from 0.8 nearby.)

### 1.7 — "How the board works" section with a TASKS.md example

Show a real `TASKS.md` (Backlog / In Progress / Review, 3-4 cards) + the protocol
(card → agent → diff → Review). This unique idea is currently buried in ARCHITECTURE.md.

### 1.8 — Launch plan (make it explicit acceptance, not vibes)

Tier 1: **Show HN** (lock a title, e.g. *"Show HN: a kanban board your Claude agents run
themselves (subscription, not API key)"*); **r/ClaudeAI**; **PR to hesreallyhim/awesome-claude-code**
(~47k★, highest passive-traffic ROI — treat as a required launch step). Tier 2: r/selfhosted,
r/LocalLLaMA, X thread w/ the GIF. Iterate the README hook line and the HN title together.

---

# Phase 2 — Runs in 5 minutes (retention → contributors)

Most common OSS death: "tried it, didn't start, closed the tab."

### 2.1 — `requirements.txt` (confirmed missing)

Only `requirements-dev.txt` exists. Pin prod deps from the live venv: `claude-agent-sdk>=0.2.96`,
`python-telegram-bot==22.7`, `aiohttp==3.13.5`, `APScheduler==3.11.2`, `cryptography>=48`,
`python-dotenv>=1.2`, `anyio>=4.13`. State Python ≥3.11.

### 2.2 — Quickstart verified on a CLEAN machine

Not the author's box. `.env.example` with a human comment per variable; add `web/.env.example`
documenting `VITE_BACKEND_URL`. Walk the path as a stranger; tests must pass on a non-author
machine (several hardcode `/home/igor` — fixed in 0.5).

### 2.3 — `docker-compose.yml` as a HARD requirement (not "if smooth")

amux ships as one `python amux.py`; our venv + npm build + systemd bar is higher. A minimal
cockpit-only compose (no Telegram) is the difference between "I'll try" and "closed the README."
Also: API-key auth as a first-class documented option (ties to 0.8); document that
`restart-self.sh` is **systemd-only** (breaks on macOS / Docker-without-systemd / WSL).

### 2.4 — `.github/` + CI that actually bootstraps

No `.github/` today. Add CI workflow, issue + PR templates. CI must explicitly:
`python -m venv venv && venv/bin/pip install -r requirements-dev.txt && env -u WEB_COOKIE_SECURE
venv/bin/python -m pytest -q`. **Gotcha:** tests REQUIRE `venv/bin/python` (not system Python) or
`pytest-aiohttp` is missing and ~237 endpoint tests error. Add `make setup` (create venv + install).

### 2.5 — Dependency-scan hygiene (bad first impression if skipped)

`npm audit` flags `vite <=6.4.2` **high** (GHSA-fx2h-pf6j-xcff) — dev-server-only, not the prod
build (traffic goes through aiohttp), but it screams `high` right after `npm install`. Bump to
`vite@8` + `@vitejs/plugin-react@latest`, verify build. Add `pip-audit` as a CI step. Add
`filterwarnings = ignore::RuntimeWarning:unittest.mock` to `pytest.ini` (10 noisy warnings).

### 2.6 — Fix the CI-breaking test 🔴

`tests/test_janitor_quarantine.py:17` references `/home/igor/server-janitor/janitor-quarantine`
(external, outside repo) → `FileNotFoundError` on any CI. Skip with a reason, vendor the script,
or mock. Blocks green CI until resolved. (Also a leak — listed in 0.5.)

### 2.7 — Pull-forward candidate: conditional board injection

3.1 below is low-risk and improves the out-of-box agent experience — consider doing it in Phase 2.

---

# Phase 3 — The code (do this in the open, after shipping)

For visitors who became contributors. Important, not the first-wave star driver — ideal for public
issues / outside help.

### 3.1 — Conditional board injection (ROOT CAUSE of "agents stumble over extra code")

`engine.py:1164-1171`: every turn injects `board_summary()` (~3K tokens at 40 cards) +
`BOARD_PROTOCOL` + three `CLAUDE.md` via `setting_sources`. Agent gets "the whole rulebook" to
answer "what Python version?". Inject only when relevant (`backlog > 0` / a param). Card-runs
already pass `ephemeral=True`; plain chat does not.

### 3.2 — Remove corpses of removed features

Dead constants `CONTEXT_ROTATE_AT`/`CONTEXT_WARN_AT`/`CONTEXT_ROTATION` duplicated in
`engine.py:70-77` AND `webapp.py:67-74`, read by nobody. Dead auto-resume: `_AUTO_RESUME_*`
(`webapp.py:59-62`), `_card_last_result_event` (`~3930`), `_tg_last_result_event` (`bot.py:~489`),
`_maybe_auto_resume`. `STALL_SECONDS` + `stalled={"reason":None}` watchdog leftovers.

### 3.3 — Blocking subprocess in async handler

`bot.py:~1017` (`cmd_diff`): synchronous `subprocess.run(..., timeout=15)` blocks the whole event
loop. Use `asyncio.create_subprocess_exec` + `await communicate()`.

### 3.4 — De-duplicate definitions

`_README_CANDIDATES` (twice in webapp.py), `_ALLOWED_CARD_MODELS` (`board.py:55` ↔ webapp.py),
`_OPS_SCRATCH_CWD` (`engine.py:58` + `webapp.py:58`), `_RUNNABLE` literal, buried `import hmac`
(`webapp.py:~447`), inlined `session_key` (~15×) → helper.

### 3.5 — Frontend declutter

Split `ChatTab.tsx` (**2639 lines**) → `SessionBar`/`MessageFeed`/`ChatComposer`/`DeferredRunsModal`.
Remove dead `web/src/styles/overview.css`. Extract localStorage helpers to `lib/storage.ts`. Pin
`lucide-react` off its pre-2.0 branch. `aria-live` on Toast; `aria-hidden` on decorative emoji.
Mobile: 44px touch targets (`chat.css:1244` forces 28px); `<html lang>` fix; `maskable` icon + `id`
in `manifest.json`.

### 3.6 — Decompose `webapp.py` (the big one; gated on a baseline)

No refactor without a baseline (`triage`→`audit`→`refactor` prompts). Target `backend/` (the
`ctx`-dict contract stays → not breaking): `projects.py`, `board_api.py`, `chat.py`,
`secrets_api.py`, `files.py`, `timeline.py`, `schedules_api.py`, `core.py`. Plus: extract
`run_engine` session runner (~300-line god-function); type `AppCtx` (currently `TypedDict(total=False)`,
`webapp.py:3736`) as a dataclass; extract `storage.py` with `write_atomic` (temp+rename) for
`save_topics`/`save_sessions` (`engine.py:456-461`).

### 3.7 — Test coverage gaps

`secret.py` **0%** — the key-storage CLI has no tests; add `tests/test_secret_cli.py` (subprocess
`set/get/list/delete`) — high priority, it's the trust anchor. `bot.py` 28% (Telegram transport);
`schedules.py` 59% (systemd code skipped in CI). Two slow tests (13.3s + 8s) in rotate/handoff.

### 3.8 — Architecture debt (mostly document, not all fix pre-launch)

In-memory state lost on restart (`running{}`/`_live_clients{}` → cards stuck In Progress, no
indicator); single-user hardcoded (`key_of(cwd)=basename(cwd)`, `engine.py:303` → collision
data-loss, undocumented); circular imports held by init order, unchecked by any linter; no
data-schema versioning on `topics.json`/`sessions.json`; `secretstore.import_env` does N disk
roundtrips (load-once/save-once). Surface the first three in the Security Model (1.4).

---

## Engine decision: PWA, not native

React 18.3 + Vite 5.4 + TS 5.6 + react-markdown + mermaid + lucide + geist — modern, current.
**Keep the PWA; do NOT build React Native / Capacitor / Tauri.** Operator tool behind password +
2FA bound to a specific backend, not an app-store product. Native adds signing, store review, an
update pipeline — zero new capability. Mobile already works: SSE reconnect on
`visibilitychange`/`online`, keyboard via `visualViewport`, safe-area, `dvh`/`svh`, project swipe,
Lightbox pinch-zoom. Polish is small and lives in 3.5.

---

## What is genuinely good (calibration — do not over-correct)

1396 tests passing (0 failed), 84% coverage. Transport-agnostic `run_engine`; `ctx`-dict DI;
isolated `board.py` with regression guards; correct crypto (scrypt + constant-time + backoff);
double path-traversal defence; C2 destructive-command gate; secrets never logged + atomic writes +
exec-array (no shell); graceful SIGTERM shutdown flushing sessions; hand-rolled SSE with correct
chunk-boundary handling + mobile resume; per-tab ErrorBoundary, memo'd tickers, stable setProjects;
`totp.py`/`secretstore.py` exemplary stdlib-only self-testing modules.

---

## Acceptance

- **Phase 0:** `git grep -iI` over `--all` for `khronika|igor|zira777|coscore|igdigi|192\.168|
  /home/igor|ops-igor-2026` returns only placeholders; author identity rewritten via `--mailmap`;
  `.service` untracked; `schedules.py:718` fallback gone; README has trademark disclaimer + ToS
  notice; `NOTICE` file present; LICENSE de-anonymized; name decision made; R1/R3/R6/R7/Y2
  fixed-or-documented.
- **Phase 1:** README has hook line, "why" story, demo GIF, architecture diagram, Security Model,
  positioning paragraph, subscription-as-feature, "how the board works"; HN title + awesome-claude-code
  PR drafted.
- **Phase 2:** `requirements.txt` present; stranger installs + runs from README; CI green on a
  non-author machine (venv bootstrap, `pytest-aiohttp`); `test_janitor_quarantine` fixed;
  `docker-compose.yml` works; `npm audit` clean (vite bump); `pip-audit` in CI.
- **Phase 3:** dead code removed; board injected conditionally; no blocking subprocess; `webapp.py`
  split into `backend/` with `ctx` intact and tests green; `secret.py` covered.

## The one-line plan

**Phase 0 + a strong Phase 1 + a working Phase 2 → SHIP. Then do Phase 3 in the open.**
Each finding becomes a board card under its phase.

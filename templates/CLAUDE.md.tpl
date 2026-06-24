# CLAUDE.md — {{name}}

> Created {{date}} via "+ New project" in the Cardloop cockpit.
> This file is the primary rules and commands document for agents working in this project.
> Edit it during onboarding and as the project evolves.

## Goal

_2-3 sentences: what this project does, and why. Fill in during onboarding._

{{#if_software_ops}}
## Stack

- Language / framework: …
- Infrastructure: …
- External APIs: …

## Commands

```bash
# start / test / deploy — fill in during onboarding
```

{{/if_software_ops}}
## Gotchas

_Paste hard-won lessons here so they are never repeated._

## Project Secrets

Secrets (API keys, tokens, passwords) live in `.claude-ops/secrets/secrets.env`.

**Location:** `<cwd>/.claude-ops/secrets/secrets.env` — `chmod 600`, NOT committed to git (gitignored automatically).

**Agent access:** at every task run the secrets are injected into the agent process env — available as plain env vars (`os.environ["STRIPE_KEY"]` etc.).

**Management:** the "🔑 Keys" tab in the cockpit (key names only, values are never displayed) — or manually: `echo 'MY_KEY=value' >> .claude-ops/secrets/secrets.env && chmod 600 .claude-ops/secrets/secrets.env`.

**⚠️ Rules:**
- Key names: uppercase `A-Z`, digits, `_` only (env-compatible).
- Values are write-only from the cockpit — never returned via API.
- Not logged in the audit log, not committed to git.

## Project Memory

Accumulated knowledge that travels with the repo: `.claude-ops/memory/`.

**Structure:** `MEMORY.md` — index (one line per entry). `<slug>.md` — one entry per file.

**Entry format:**
```
---
type: decision | gotcha | rejected | convention
created: YYYY-MM-DD
---
Summary. For decision/rejected — **Why:** reason.
```

**When to write:**
- `decision` — architectural or technology choice + why this, not that.
- `gotcha` — a trap encountered, so it is never hit again.
- `rejected` — something the operator or project rejected + why (do not suggest again).
- `convention` — agreed style, naming, or approach.

Agent writes here via normal Write (relative path: `.claude-ops/memory/<slug>.md`).
Memory is committed to git — visible on clone, history preserved.

---
{{#if_software_ops}}
## Error Handler

**Required for services and bots.** The cockpit scanner greps for the string `UNHANDLED exc_class=<Type> path=<route>` — it must appear in the log. Ready snippets (FastAPI / aiohttp / PTB / CLI / incident-push) → `reference/error-handler.md`.

---

{{/if_software_ops}}
## Cockpit Rules (do NOT remove — shared across all projects)

**Board (TASKS.md):**
- Card format: `- [ ] text <!--ops:ID-->` strictly inside a column section.
- Columns: `## Backlog` / `## In Progress` / `## Review` / `## Failed`.
- NOT allowed: numbered lists (`1.`), nested sublists, tables inside sections, text outside sections (except the preamble before the first `##`).
- The `<!--ops:ID-->` marker is added automatically on the first GET — do not remove it.
- Card wording: verb + object + done-criterion. Bad: "fix logs". Good: "configure log_cmd in topics.json for X so that GET /api/projects/X/logs returns lines".
- Completed work → `DONE.md` (archive; sessions do NOT read it — context hygiene).

**Sessions:**
- One project = one shared session (TG + cockpit chat + cards all share it).
- Topic switch = `/reset` (new session), do not append to the current one — context gets polluted.
- A session in the cockpit is visible on any device (continue from phone via browser).

**Files:**
- `data/` (if present) and `.env*` — NOT in git (see .gitignore).
- README.md — short description for the future; CLAUDE.md is the primary document.

**Audit:**
- Once a week — the "🩺 Project Audit" button in the Overview tab. The agent checks the structure and creates cards for issues found.

**Self-healing (optional):**
- The "🔧 Self-heal" toggle in Overview — OFF by default. Enable deliberately.
- When enabled: new errors (from log_cmd) → agent auto-fixes in a worktree → card in Review.
- **Non-negotiable:** the agent NEVER applies changes without a human. Merge is always manual ("✓ Apply").
- Requires: git repo + clean tree + log_cmd in topics.json.

**Cardloop capabilities — what to connect:**

See `reference/cockpit-capabilities.md` for the full capabilities table.

---
{{#if_software_ops}}
## Cardloop Integration Status
<!-- Fill in during onboarding. Cockpit reads this in health. Format: "- <capability>: <yes: where / no>" -->
- error handler: no
- log_cmd: no
- test_cmd: no
- self-heal (git+clean): no
- memory (.claude-ops/memory): no
- secrets (.claude-ops/secrets): no
- notify_on_error: no
- healthz/liveness (services): no
- incident push: no
{{/if_software_ops}}

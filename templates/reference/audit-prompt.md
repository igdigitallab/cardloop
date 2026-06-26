# Audit Prompt — single-project audit

Prompt for copy-paste into a new Claude Code session. Replace `<PROJECT>` with the project name.

---

## Prompt

```
# Audit task: <PROJECT>

Project: $HOME/<PROJECT>/
Read:
1. The project's CLAUDE.md (if present)
2. $VAULT/01-Projects/<PROJECT>/README.md (if present)
3. $VAULT/03-Resources/_templates/project-baseline.md — baseline standard
4. The "Tech gotchas" section in $HOME/CLAUDE.md — operational pitfalls, use as a checklist

## Mode: AUDIT ONLY
Do NOT change code. Do NOT fix. Do NOT refactor. Findings only → report.

---

## Stage 0: Pre-audit commands (read-only, safe)

Run at the very start, before reading any code:

```bash
PROJ=$HOME/<PROJECT>
cd "$PROJ"

# Dependencies with CVEs
[ -f requirements.txt ] && pip-audit -r requirements.txt 2>&1 | tail -30 || echo "no requirements.txt"

# Secrets in git history
git log -p --all 2>/dev/null | grep -iE "(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*['\"][^x]" | head -20

# Is .env in .gitignore?
grep -E "^\.env$" .gitignore 2>/dev/null || echo "WARN: .env not in .gitignore"

# Is .env accidentally committed?
git ls-files | grep -E "^\.env$" && echo "CRITICAL: .env committed!" || echo "ok"

# Production logs for the past week — real errors, not hypothetical ones
APP_UUID=$(grep -E "<PROJECT>.*\`[a-z0-9]{20,}\`" $HOME/CLAUDE.md | grep -oE "\`[a-z0-9]{20,}\`" | tr -d '`' | head -1)
if [ -n "$APP_UUID" ]; then
  CONTAINER=$(docker ps --format "{{.Names}}" | grep "$APP_UUID" | head -1)
  [ -n "$CONTAINER" ] && docker logs --since 168h "$CONTAINER" 2>&1 | grep -iE "error|exception|traceback|critical" | tail -100
fi

# Web scraping smell — candidates for Firecrawl
grep -rE "requests\.(get|post)|BeautifulSoup|httpx\.|aiohttp\.ClientSession" --include="*.py" -l | head -5
```

Include pre-audit findings in the report (especially production logs and CVEs — these are P0).

---

## Stage 1: Baseline check (per project-baseline.md)

- [ ] Error→Claude alerting (imports error_prompt.py/error_alerts.py, sys.excepthook, asyncio exception_handler, or PTB error_handler)
- [ ] Tests — `tests/` folder or `test_*.py`. Open 1–2: what is covered? Are critical paths present?
- [ ] `.env.example` in root (and NO `.env` in the repo)
- [ ] Health-check (for web services)
- [ ] README with architecture and a list of critical paths

### Test quality check (if tests exist — verify quality)

Open 2–3 random test files and check against the checklist (`test-master/references/testing-anti-patterns.md`):

- [ ] **Testing mock behavior** — tests verify that a mock was called, not the real output. If `expect(mock).toHaveBeenCalled()` without checking the result — this is an anti-pattern
- [ ] **Test-only methods in production** — methods like `_resetForTesting()` or `__reset_state__` in production classes
- [ ] **Order-dependent tests** — a test only works if the previous test ran first; shared global state between tests without cleanup
- [ ] **Flaky tests** — tests with `time.sleep()`, race conditions, or dependency on external APIs in unit tests
- [ ] **Real API/DB in unit tests** — unit tests hitting real Telegram/Hiddify/external APIs (those belong in integration tests)
- [ ] **Production data in tests** — real user_ids, tokens, or names instead of fixtures

**IMPORTANT (if applicable to the project):** integration tests MUST hit the real database, not mock it. This is **not** an anti-pattern — it is a deliberate design decision. Distinguish unit vs integration.

If 3+ anti-patterns are found in the sample → P1 finding "Test quality: rewrite needed".

**If error alerting is absent** → automatic P0
**If `.env` is committed to git** → P0 (rotate secrets!)
**If CVEs in dependencies** → P0 for each HIGH/CRITICAL
**If no tests at all** → P1
**If tests exist but don't cover critical paths** → P1 + concrete list of tests in the "Tests to write" section
**If `.env.example` or README is missing** → P2

---

## Stage 2: Tech-gotchas checklist

Before reading code — open the "Tech gotchas" section in `$HOME/CLAUDE.md`. Walk through the project with each gotcha as a checklist:

- Telegram bot? → HTML-escape user input (parse_mode=HTML breaks on `<`), python-telegram-bot job_queue, not APScheduler
- asyncio + subprocess? → `limit=10*1024*1024` in `create_subprocess_exec`
- VPN/auth flow? → guards before `_issue_subscription`, atomic block (Hiddify lesson)
- Coolify env special characters? → `is_literal=true`
- `host.docker.internal`? → requires `extra_hosts: ["host.docker.internal:host-gateway"]` or container name in a shared network
- Web scraping? → `YOUR_FIRECRAWL_URL` is available, don't roll your own

For each applicable gotcha — verify it is addressed. If it doesn't apply — skip it.

---

## Stage 3: Deep audit (in descending order of importance)

### P0 — critical risks

**Security:**
- Hardcoded secrets in code (not in .env)
- SQL/command injection (`f"SELECT ... {var}"`, `subprocess.shell=True`)
- Missing auth/rate-limit on sensitive endpoints
- PII leaking into logs (user_id is OK; tokens/names/email are not)
- CVEs in dependencies (from Stage 0)

**Data loss / corruption:**
- Race conditions (concurrent write without a lock)
- Missing transactions where required
- Idempotency absent where a request can be retried (Hiddify auto-re-enable)
- DB migrations without a backup

**Production stability:**
- Unhandled exceptions in the hot path
- Connection leaks (aiohttp ClientSession not closed, DB connection not returned to pool)
- Memory leaks (globally growing list/dict)
- Forgotten `await` (coroutine never scheduled)

**Asyncio/concurrency:**
- `asyncio.create_task(...)` without saving the reference (GC silently collects the task)
- Blocking sync calls in async context (`requests.get`, `time.sleep`, `open()` for large files)
- `subprocess` without `limit=` in `create_subprocess_exec` (see gotcha)
- Tasks not cancelled on shutdown

**Auth/permission bypass:**
- All handlers — are guards in place? Especially user-blocking logic

### P1 — bugs and weaknesses

**Logic:**
- Errors in business flow
- Unhandled edge cases (empty input, zero payment, negative time, etc.)

**Error handling:**
- Inconsistency (some places log, some swallow, some raise)
- Overly broad `except Exception` without logging

**External calls:**
- No retry/timeout on Telegram/DB/third-party APIs
- No flood_wait handling for Telegram bots

**Telegram-bot specific:**
- HTML-escape user input in parse_mode=HTML
- Privacy mode not accounted for (if the bot is in a group and must see all text)
- File size limits (50 MB upload)

**Configuration:**
- Hardcoded values where configuration is expected
- Mismatch between prod env (Coolify) and `.env.example`

### P2 — quality and maintainability
- Dead code, unused imports/functions
- Duplication that genuinely hinders understanding
- Functions that are too long (>100 lines) with clear seams
- Magic numbers without explanation
- **Homegrown web scraper** — if `requests`/`BeautifulSoup` is used for scraping → propose migration to Firecrawl (`https://YOUR_FIRECRAWL_URL/v1/scrape`). You self-host the instance; there are no rate limits.

### P3 — style
**SKIP.** Do not mention.

---

## Stage 4: Test gap list (CONCRETE list of tests to write)

For each critical path that is not covered — a separate entry with:
- **What to test:** `file.py::function_name`
- **Scenarios:**
  - Happy path: <concrete description>
  - Error path: <concrete errors to verify>
  - Edge cases: <concrete boundary values>
- **What to mock:** Telegram API / DB / Hiddify API / etc.
- **Fixtures needed:** test DB, fake user, sample message, etc.
- **Why it matters:** what breaks if this test is absent and a regression occurs

This list goes as a **separate section** in the report — it is actionable and can be used to write tests immediately.

---

## Report format

Create `$VAULT/01-Projects/<PROJECT>/audit-<YYYY-MM-DD>.md` with the following structure:

\```markdown
# Audit <PROJECT> — <YYYY-MM-DD>

## Summary
- Total findings: N (P0: X, P1: Y, P2: Z)
- Baseline: ✓/✗ (what is missing, one line)
- Production logs (past week): N errors, top-3 types
- CVE: N HIGH/CRITICAL
- Top-3 risks in one line each

## Pre-audit results
- pip-audit: <CVE count by severity>
- Git history secrets: <found/clean>
- .env in .gitignore: ✓/✗
- .env committed: ✓/✗
- Production logs (week): top-5 error types with example lines

## Baseline check
- Error alerting: ✓/✗ (if ✗ — why it is critical)
- Tests: ✓/✗/partial (what is covered, what is missing — see "Tests to write" section)
- .env.example: ✓/✗
- Health-check: ✓/✗/N/A
- README: ✓/✗

## P0 — fix now
### [P0-1] <Short name>
- **File:** path:line
- **What's wrong:** 1–2 sentences
- **Why it's dangerous:** concrete scenario
- **How to fix:** 1–2 sentences (do NOT write fix code)

### [P0-2] ...

## P1 — put in spec (fix when the related task comes up)
(same format)

## P2 — backlog
- One line each
- If a homegrown scraper exists → "[P2-X] Migrate <file> from requests/BS4 to Firecrawl"

## Tests to write
### [TEST-1] <file.py::function_name>
- **Scenarios:**
  - Happy: <description>
  - Error: <errors>
  - Edge: <boundary values>
- **Mocks:** <list>
- **Fixtures:** <list>
- **Why:** what breaks without this test

### [TEST-2] ...

## Limits of this audit
What is NOT covered by this audit (honestly):
- Performance under load → requires a load test
- Memory growth over time → requires RSS monitoring for a week
- N+1 queries → requires query log + EXPLAIN
- Full static analysis → run separately: `ruff check`, `mypy`, `bandit`
- Regressions → requires the tests listed above to be written
\```

After creating the report, briefly report:
"Audit complete: $VAULT/01-Projects/<PROJECT>/audit-<date>.md. N findings (P0: X, P1: Y). Baseline: <status>. Production logs: <top error>. Top risk: <one phrase>. Tests to write: N."

---

## What NOT to do
- Do not propose refactoring "for aesthetics"
- Do not write fix code in the report
- Do not mention P3 style issues
- If the project is <500 lines — simplify the P2 section
- Do not touch rss-bot (intentionally disabled)
- Do not run `pytest` — only check for its presence
- **Do NOT claim you found all bugs.** List gaps honestly in Limits.

---

## After the audit

The operator decides what to do with each P0/P1:
- P0 → separate session with a fix
- P1 → spec in `$VAULT/01-Projects/<PROJECT>/specs/`
- P2 → project backlog
- TEST-* → separate session "write tests from audit-<date>.md", prioritized by critical paths
```

---

## Related templates

- [[project-baseline]] — baseline standard for a production project
- [[triage-prompt]] — choose which project to audit first

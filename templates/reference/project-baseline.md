# Project Baseline — what every production project must have

Quality standard for active homelab projects. No baseline → the project is blind and must not be refactored.

---

## 1. Error→Claude alerting (REQUIRED for production bots)

Unhandled exceptions send the admin a ready-made prompt in a `<pre>` block via Telegram — long-press → Copy → paste into a new Claude session.

**Canonical implementations:**
- `example-bot/lib/error_prompt.py`
- `example-bot/bot_lib/error_alerts.py` (with rate-limit + quiet-list for known-non-actionable errors)

**What the prompt block must contain:**
- `Project: <name>` + path to code
- `Source: file:line`
- Traceback tail (last 20–30 lines)
- Concrete commands for logs: `docker logs <container> 2>&1 | tail -50`
- Concrete commands for deployment via Coolify API

**Where it is wired in:**
- `sys.excepthook` for synchronous code
- `loop.set_exception_handler` for asyncio
- Try/except wrapper in the main handler loop (PTB error_handlers, pyrogram raw_update)

**Rate-limit:** no more than 1 alert per error type per N minutes (5–10); otherwise a hot-path failure causes a flood.
**Quiet-list:** known-non-actionable errors (e.g., SSH unreachable to a remote Keenetic) → summarize once per hour.

---

## 2. Tests (REQUIRED, `pytest`)

**Minimum (smoke):**
- The project imports without errors (`pytest --collect-only` passes)
- The main entry-point (`bot.py`, `main.py`) imports successfully
- Config is parsed from `.env.example`

**Critical paths (required for production bots):**
- Each service must have its own list of critical paths in the project's `CLAUDE.md`
- Examples:
  - `example-bot`: `_issue_subscription`, `block_user`, `payment_callback`, provider `create_or_get_user`
  - `example-bot`: `start_session`, `claude_runner.run_session_turn`, message routing
  - `example-bot`: threshold logic (hysteresis + sustained counter), `clear_alert` behavior
  - `rightforms-app`: form-validation pipeline, PDF generation
- Coverage percentage is not required — what is required is coverage of the critical paths specifically

**CI:** `pytest` locally via `make test` or directly. GitHub Actions is optional.

---

## 3. `.env.example` + git safety (REQUIRED)

- `.env.example` in the root with placeholder values (`TELEGRAM_BOT_TOKEN=xxx`)
- `.env` — in `.gitignore`
- `.env` NOT committed (`git ls-files | grep '^\.env$'` → empty)
- Git history free of secrets: `git log -p --all | grep -iE '(TOKEN|SECRET|PASSWORD|API_KEY)\s*='`. If found — rotate secrets and use BFG/git-filter-repo to clean history.

**Why:** when recreating a Coolify app, you won't have to recall which env vars were set. And secrets won't be exposed in public repos.

---

## 4. Dependency security (REQUIRED)

```bash
pip-audit -r requirements.txt
# or
pip install safety && safety check -r requirements.txt
```

- HIGH/CRITICAL CVE → P0 fix immediately (upgrade the library)
- MEDIUM → P1 in spec
- LOW → P2 backlog

**Frequency:** run manually once a month or in an audit session. Automating via `pip-audit` in a pre-commit hook is optional.

---

## 5. Health-check (REQUIRED for web services, optional for bots)

**Web services (portals, APIs):**
- `GET /health` or `GET /healthz` → 200 OK + JSON (DB is reachable, version)
- Docker `HEALTHCHECK` in Dockerfile

**Bots with long-polling:**
- Optional. If the bot correctly dies on error — `docker ps` via example-bot is sufficient.
- If the bot can "hang" alive but not respond — a heartbeat file (`/tmp/<bot>.alive` updated every minute) + check in example-bot.

---

## 6. README with architecture (REQUIRED)

Minimum:
- What the project does (1–2 sentences)
- Stack (language, framework, DB, external APIs)
- How to run locally
- How it deploys (link to the Coolify app UUID)
- Critical paths as a list (needed for tests and audit)

---

## 7. Asyncio/concurrency gotchas (REQUIRED for asyncio projects)

Checklist — each item must be verified:

- [ ] `asyncio.create_task(coro)` — reference is saved (`self._tasks.add(task)`), otherwise GC silently collects the task
- [ ] No blocking sync calls in async context (`requests.get` → `httpx`/`aiohttp`; `time.sleep` → `asyncio.sleep`; `open()` for large files → `aiofiles`)
- [ ] `aiohttp.ClientSession` created via `async with` or closed explicitly
- [ ] `asyncio.create_subprocess_exec` with `limit=10*1024*1024` (see gotcha in CLAUDE.md, 2026-05-11)
- [ ] Graceful shutdown: SIGTERM is caught, tasks are cancelled, sessions are closed

---

## 8. Telegram-bot gotchas (REQUIRED for Telegram bots)

- [ ] `parse_mode=HTML` everywhere, not Markdown (breaks on `_`)
- [ ] HTML-escape user input (`html.escape(text)` before inserting into a formatted message)
- [ ] `flood_wait` handling — on 429 from Telegram, retry with `e.retry_after`
- [ ] python-telegram-bot — `job_queue`, not APScheduler (event loop conflict)
- [ ] Privacy mode for groups — accounted for if the bot must see all text
- [ ] File size limits — upload ≤ 50 MB (Bot API), download ≤ 20 MB

---

## 9. Web scraping → Firecrawl (if applicable)

If the project does web scraping (RSS feeds, HTML parsing, content extraction):

**Do NOT write a homegrown** `requests` + `BeautifulSoup` parser. Use self-hosted Firecrawl:
```
POST https://YOUR_FIRECRAWL_URL/v1/scrape
Body: {"url": "...", "formats": ["markdown", "html"]}
```

**Why:**
- You host your own instance — no rate limits, no cost
- Returns clean markdown, JS-rendered content
- Automatically handles cookie consent and anti-bot protection

**When an exception is acceptable:** very simple case (1 URL, static HTML, no JS). Then a plain `httpx.get` + `lxml` is fine.

---

## Checking a project's baseline

```bash
PROJ=$HOME/<project>
cd "$PROJ"
echo "=== Baseline check: $PROJ ==="

[ -f .env.example ] && echo "✓ .env.example" || echo "✗ .env.example MISSING"
[ -f .env ] && ! grep -qE "^\.env$" .gitignore 2>/dev/null && echo "✗ CRITICAL: .env exists but not in .gitignore"
git ls-files 2>/dev/null | grep -qE "^\.env$" && echo "✗ CRITICAL: .env COMMITTED to git"
ls tests/ test_*.py 2>/dev/null | head -1 > /dev/null && echo "✓ tests/" || echo "✗ tests MISSING"
grep -rE "(error_prompt|error_alerts|set_exception_handler)" --include="*.py" -l > /dev/null && echo "✓ error alerting" || echo "✗ error alerting MISSING"
[ -f README.md ] && echo "✓ README" || echo "✗ README MISSING"

# CVE check
[ -f requirements.txt ] && pip-audit -r requirements.txt 2>&1 | tail -10

# Secrets in git history
SECRETS=$(git log -p --all 2>/dev/null | grep -iE "(TOKEN|SECRET|PASSWORD)\s*=\s*['\"][^x]" | wc -l)
[ "$SECRETS" -gt 0 ] && echo "✗ $SECRETS suspicious secret-strings in git history" || echo "✓ git history clean"

# Async sanity (for asyncio projects)
grep -rE "asyncio\.create_subprocess_exec" --include="*.py" | grep -v "limit=" && echo "✗ subprocess without limit= (see gotcha)"
grep -rE "time\.sleep|requests\.(get|post)" --include="*.py" | grep -v "test_" | head -5 && echo "WARN: possible blocking calls in async context"
```

---

## Related templates

- [[audit-prompt]] — project audit (uses this baseline)
- [[triage-prompt]] — ranking all projects

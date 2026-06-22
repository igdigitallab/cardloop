<!-- ORIGIN: ~/vault/03-Resources/_templates/project-baseline.md -->
<!-- Синхронизированная копия. При правке шаблона в vault → обновить здесь. -->
<!-- При правке здесь → синхронизировать обратно в vault, иначе drift. -->

# Project Baseline — что должно быть в каждом прод-проекте

Стандарт качества для активных проектов в homelab. Нет baseline → проект слепой и не подлежит рефакторингу.

---

## 1. Error→Claude alerting (ОБЯЗАТЕЛЬНО для прод-ботов)

Необработанные исключения шлют admin'у в Telegram готовый промпт в `<pre>` блоке для long-press → Copy → paste в новую Claude-сессию.

**Каноничные реализации:**
- `example-bot/lib/error_prompt.py`
- `example-bot/bot_lib/error_alerts.py` (с rate-limit + quiet-list для known-non-actionable)

**Что должно быть в промпт-блоке:**
- `Проект: <name>` + путь к коду
- `Источник: file:line`
- Хвост traceback (последние 20-30 строк)
- Конкретные команды для логов: `docker logs <container> 2>&1 | tail -50`
- Конкретные команды для деплоя через Coolify API

**Где подключается:**
- `sys.excepthook` для синхронного кода
- `loop.set_exception_handler` для asyncio
- Try/except wrapper в main handler loop (PTB error_handlers, pyrogram raw_update)

**Rate-limit:** не больше 1 алёрта на тип ошибки в N минут (5-10), иначе при падении в hot path спам.
**Quiet-list:** known-non-actionable (типа SSH unreachable до удалённого Keenetic) → суммаризировать раз в час.

---

## 2. Тесты (ОБЯЗАТЕЛЬНО, `pytest`)

**Минимум (smoke):**
- Проект импортируется без ошибок (`pytest --collect-only` проходит)
- Главный entry-point (`bot.py`, `main.py`) импортируется
- Конфиг парсится из `.env.example`

**Critical paths (обязательно для прод-ботов):**
- Каждый сервис должен иметь свой список critical paths в `CLAUDE.md` проекта
- Примеры:
  - `example-bot`: `_issue_subscription`, `block_user`, `payment_callback`, provider `create_or_get_user`
  - `example-bot`: `start_session`, `claude_runner.run_session_turn`, message routing
  - `example-bot`: threshold logic (hysteresis + sustained counter), `clear_alert` behavior
  - `rightforms-app`: form-validation pipeline, PDF generation
- НЕ требуем coverage %, требуем покрытие именно critical paths

**CI:** `pytest` локально через `make test` или напрямую. GitHub Actions опционально.

---

## 3. `.env.example` + git safety (ОБЯЗАТЕЛЬНО)

- Файл `.env.example` в корне с placeholder-значениями (`TELEGRAM_BOT_TOKEN=xxx`)
- `.env` — в `.gitignore`
- `.env` НЕ закоммичен (`git ls-files | grep '^\.env$'` → пусто)
- Git history без секретов: `git log -p --all | grep -iE '(TOKEN|SECRET|PASSWORD|API_KEY)\s*='`. Если найдено — rotate секреты и BFG/git-filter-repo для чистки.

**Зачем:** при пересоздании Coolify app не вспоминать переменные. И не светить секреты в публичных репах.

---

## 4. Dependency security (ОБЯЗАТЕЛЬНО)

```bash
pip-audit -r requirements.txt
# или
pip install safety && safety check -r requirements.txt
```

- HIGH/CRITICAL CVE → P0 fix немедленно (upgrade либы)
- MEDIUM → P1 в spec
- LOW → P2 backlog

**Регулярность:** запускать вручную раз в месяц или в audit-сессии. Автоматизация через `pip-audit` в pre-commit hook — опционально.

---

## 5. Health-check (ОБЯЗАТЕЛЬНО для web-сервисов, опционально для ботов)

**Web-сервисы (web-порталы, API):**
- `GET /health` или `GET /healthz` → 200 OK + JSON (БД доступна, версия)
- Docker `HEALTHCHECK` в Dockerfile

**Боты с long-polling:**
- Опционально. Если бот корректно умирает при ошибке — `docker ps` через example-bot достаточно.
- Если бот может "висеть" живой но не отвечать — heartbeat-файл (`/tmp/<bot>.alive` раз в минуту) + проверка в example-bot.

---

## 6. README с архитектурой (ОБЯЗАТЕЛЬНО)

Минимум:
- Что делает проект (1-2 предложения)
- Stack (язык, фреймворк, БД, внешние API)
- Как запустить локально
- Как деплоится (ссылка на Coolify app UUID)
- Critical paths списком (нужны для тестов и audit'а)

---

## 7. Asyncio/concurrency gotchas (ОБЯЗАТЕЛЬНО для asyncio проектов)

Чеклист — каждый пункт должен быть проверен:

- [ ] `asyncio.create_task(coro)` — reference сохранён (`self._tasks.add(task)`), иначе GC съест таск молча
- [ ] Нет blocking sync в async (`requests.get` → `httpx`/`aiohttp`; `time.sleep` → `asyncio.sleep`; `open()` для больших файлов → `aiofiles`)
- [ ] `aiohttp.ClientSession` создаётся через `async with` или закрывается явно
- [ ] `asyncio.create_subprocess_exec` с `limit=10*1024*1024` (см. gotcha в CLAUDE.md, 2026-05-11)
- [ ] Graceful shutdown: SIGTERM ловится, tasks отменяются, сессии закрываются

---

## 8. Telegram-bot gotchas (ОБЯЗАТЕЛЬНО для Telegram ботов)

- [ ] `parse_mode=HTML` везде, не Markdown (ломается на `_`)
- [ ] HTML escape user input (`html.escape(text)` перед вставкой в форматированное сообщение)
- [ ] `flood_wait` handling — на 429 от Telegram retry с `e.retry_after`
- [ ] python-telegram-bot — `job_queue`, не APScheduler (конфликт event loop)
- [ ] Privacy mode для групп — учтён если бот должен видеть весь текст
- [ ] File size limits — upload ≤ 50 MB (Bot API), download ≤ 20 MB

---

## 9. Web scraping → Firecrawl (если применимо)

Если проект делает web scraping (RSS-feeds, парсинг HTML, content extraction):

**НЕ писать homegrown** `requests` + `BeautifulSoup` парсер. Использовать self-hosted Firecrawl:
```
POST https://YOUR_FIRECRAWL_URL/v1/scrape
Body: {"url": "...", "formats": ["markdown", "html"]}
```

**Зачем:**
- Вы хостите свой инстанс — нет рейт-лимитов, не платите
- Возвращает чистый markdown, JS-rendered контент
- Авто-обработка cookie consent, антибот-защит

**Когда исключение:** очень простой случай (1 URL, статический HTML, никаких JS). Тогда обычный `httpx.get` + `lxml` ok.

---

## Проверка baseline'а проекта

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

# Async sanity (если asyncio проект)
grep -rE "asyncio\.create_subprocess_exec" --include="*.py" | grep -v "limit=" && echo "✗ subprocess без limit= (см. gotcha)"
grep -rE "time\.sleep|requests\.(get|post)" --include="*.py" | grep -v "test_" | head -5 && echo "WARN: возможные blocking calls в async"
```

---

## Связанные шаблоны

- [[audit-prompt]] — аудит проекта (использует этот baseline)
- [[triage-prompt]] — ранжирование всех проектов

<!-- ORIGIN: ~/vault/03-Resources/_templates/audit-prompt.md -->
<!-- Синхронизированная копия. При правке шаблона в vault → обновить здесь. -->
<!-- При правке здесь → синхронизировать обратно в vault, иначе drift. -->

# Audit Prompt — аудит одного проекта

Промпт для копипасты в новую Claude Code сессию. Замени `<PROJECT>` на имя проекта.

---

## Промпт

```
# Audit задача: <PROJECT>

Проект: /home/igor/<PROJECT>/
Прочитай:
1. CLAUDE.md проекта (если есть)
2. ~/vault/01-Projects/<PROJECT>/README.md (если есть)
3. ~/vault/03-Resources/_templates/project-baseline.md — стандарт baseline
4. Раздел "Tech gotchas" в /home/igor/CLAUDE.md — реальные грабли Игоря, используй как чеклист

## Режим: AUDIT ONLY
НЕ менять код. НЕ фиксить. НЕ рефакторить. Только findings → отчёт.

---

## Этап 0: Pre-audit команды (read-only, безопасные)

Выполни в самом начале, до чтения кода:

```bash
PROJ=/home/igor/<PROJECT>
cd "$PROJ"

# Зависимости с CVE
[ -f requirements.txt ] && pip-audit -r requirements.txt 2>&1 | tail -30 || echo "no requirements.txt"

# Секреты в git history
git log -p --all 2>/dev/null | grep -iE "(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*['\"][^x]" | head -20

# .env в .gitignore?
grep -E "^\.env$" .gitignore 2>/dev/null || echo "WARN: .env not in .gitignore"

# Случайно ли .env закоммичен?
git ls-files | grep -E "^\.env$" && echo "CRITICAL: .env committed!" || echo "ok"

# Прод-логи за неделю — реальные ошибки, не гипотетические
APP_UUID=$(grep -E "<PROJECT>.*\`[a-z0-9]{20,}\`" /home/igor/CLAUDE.md | grep -oE "\`[a-z0-9]{20,}\`" | tr -d '`' | head -1)
if [ -n "$APP_UUID" ]; then
  CONTAINER=$(docker ps --format "{{.Names}}" | grep "$APP_UUID" | head -1)
  [ -n "$CONTAINER" ] && docker logs --since 168h "$CONTAINER" 2>&1 | grep -iE "error|exception|traceback|critical" | tail -100
fi

# Web scraping smell — кандидаты на Firecrawl
grep -rE "requests\.(get|post)|BeautifulSoup|httpx\.|aiohttp\.ClientSession" --include="*.py" -l | head -5
```

Включи находки из pre-audit в отчёт (особенно прод-логи и CVE — это P0).

---

## Этап 1: Baseline check (по project-baseline.md)

- [ ] Error→Claude alerting (импорт error_prompt.py/error_alerts.py, sys.excepthook, asyncio exception_handler, или PTB error_handler)
- [ ] Тесты — папка `tests/` или `test_*.py`. Открой 1-2: что покрыто? Есть critical paths?
- [ ] `.env.example` в корне (и НЕТ `.env` в репо)
- [ ] Health-check (для web-сервисов)
- [ ] README с архитектурой и critical paths списком

### Test quality check (если тесты есть — проверь качество)

Открой 2-3 рандомных тест-файла и проверь по чек-листу (`test-master/references/testing-anti-patterns.md`):

- [ ] **Testing mock behavior** — тесты проверяют что мок вызван, а не реальный output. Если `expect(mock).toHaveBeenCalled()` без проверки результата — это анти-паттерн
- [ ] **Test-only methods в production** — методы вроде `_resetForTesting()` или `__reset_state__` в production-классах
- [ ] **Order-dependent tests** — тест работает только если предыдущий тест выполнился; общая глобальная state между тестами без cleanup
- [ ] **Flaky tests** — тесты с `time.sleep()`, race conditions, зависят от внешних API в unit-тестах
- [ ] **Real API/DB в unit-тестах** — unit-тесты бьют реально в Telegram/Hiddify/external API (это для integration-тестов)
- [ ] **Production data в тестах** — реальные user_id, токены, имена вместо fixtures

**ВАЖНО для Игоря (см. [[feedback_no_mocks_db]] в memory):** integration-тесты ДОЛЖНЫ бить в реальную БД, не мокать её. Это **не** анти-паттерн, это сознательное решение. Различай unit vs integration.

Если найдено 3+ анти-паттернов в выборке → P1 finding "Test quality: rewrite needed".

**Если error alerting отсутствует** → автоматически P0
**Если `.env` закоммичен в git** → P0 (rotate secrets!)
**Если CVE в зависимостях** → P0 за каждое HIGH/CRITICAL
**Если тестов нет вообще** → P1
**Если тесты есть но не покрывают critical paths** → P1 + конкретный список тестов в секции "Тесты которые нужно написать"
**Если `.env.example` или README нет** → P2

---

## Этап 2: Tech-gotchas чеклист

Перед чтением кода — открой раздел "Tech gotchas" в `/home/igor/CLAUDE.md`. Пройдись по проекту с каждым gotcha как чеклистом:

- Telegram-bot? → HTML escape user input (parse_mode=HTML ломается на `<`), python-telegram-bot job_queue, не APScheduler
- asyncio + subprocess? → `limit=10*1024*1024` в `create_subprocess_exec`
- VPN/auth flow? → guards перед `_issue_subscription`, атомарный block (Hiddify lesson)
- Coolify env spec-symbols? → `is_literal=true`
- `host.docker.internal`? → нужен `extra_hosts: ["host.docker.internal:host-gateway"]` или container name в общей сети
- Web scraping? → `firecrawl.coscore.us` доступен, не делай homegrown

Для каждого подходящего gotcha — проверь применение. Не подошёл — пропусти.

---

## Этап 3: Углублённый аудит (по убыванию важности)

### P0 — критичные риски

**Security:**
- Hardcoded secrets в коде (не в .env)
- SQL/command injection (`f"SELECT ... {var}"`, `subprocess.shell=True`)
- Отсутствие auth/rate-limit на чувствительных endpoint'ах
- Утечки PII в логи (user_id ОК, токены/имена/email — не ОК)
- CVE в зависимостях (из Этапа 0)

**Data loss / коррупция:**
- Race conditions (concurrent write без lock'а)
- Отсутствие транзакций где надо
- Идемпотентность отсутствует там где запрос может повториться (Hiddify auto-re-enable)
- БД миграции без бэкапа

**Прод-стабильность:**
- Unhandled exceptions в hot path
- Утечки соединений (aiohttp ClientSession не закрыт, DB connection не возвращён в pool)
- Утечки памяти (global growing list/dict)
- Забытые `await` (coroutine не запущена)

**Asyncio/concurrency:**
- `asyncio.create_task(...)` без сохранения reference (GC съест таск молча)
- Blocking sync calls в async (`requests.get`, `time.sleep`, `open()` для больших файлов)
- `subprocess` без `limit=` в `create_subprocess_exec` (см. gotcha)
- Не отменённые tasks при shutdown

**Auth/permission bypass:**
- Все handlers — есть guards? Особенно блокировки юзеров

### P1 — баги и слабости

**Логика:**
- Ошибки в business flow
- Edge cases не обработаны (пустой ввод, нулевой платёж, negative time, etc.)

**Обработка ошибок:**
- Неконсистентность (где log, где swallow, где raise)
- Слишком широкий `except Exception` без логирования

**Внешние вызовы:**
- Нет retry/timeout на Telegram/БД/сторонних API
- Нет flood_wait handling для Telegram bots

**Telegram-bot specific:**
- HTML escape user input в parse_mode=HTML
- Privacy mode не учтён (если бот в группе и должен видеть весь текст)
- File size limits (50 MB upload)

**Configuration:**
- Hardcoded values где должна быть конфигурация
- Несоответствие prod env (Coolify) и `.env.example`

### P2 — качество и поддерживаемость
- Dead code, неиспользуемые импорты/функции
- Дублирование которое реально мешает
- Слишком длинные функции (>100 строк) с явными швами
- Magic numbers без объяснения
- **Homegrown web scraper** — если используется `requests`/`BeautifulSoup` для скрейпинга → предложить миграцию на Firecrawl (`https://firecrawl.coscore.us/v1/scrape`). Игорь хостит свой инстанс, рейт-лимитов нет.

### P3 — стилистика
**ПРОПУСКАТЬ.** Не упоминать.

---

## Этап 4: Test gap list (КОНКРЕТНЫЙ список тестов которые написать)

Для каждого critical path который не покрыт — отдельная запись с:
- **Что тестировать:** `file.py::function_name`
- **Сценарии:**
  - Happy path: <конкретное описание>
  - Error path: <конкретные ошибки которые надо проверить>
  - Edge cases: <конкретные граничные значения>
- **Что замокать:** Telegram API / БД / Hiddify API / etc.
- **Fixture'ы нужны:** test DB, fake user, sample message, etc.
- **Почему важен:** что сломается если регрессия

Этот список идёт **отдельной секцией** в отчёте — он actionable, по нему можно сразу писать тесты.

---

## Формат отчёта

Создай `~/vault/01-Projects/<PROJECT>/audit-<YYYY-MM-DD>.md` со структурой:

\```markdown
# Audit <PROJECT> — <YYYY-MM-DD>

## Summary
- Всего findings: N (P0: X, P1: Y, P2: Z)
- Baseline: ✓/✗ (что отсутствует одной строкой)
- Прод-логи (за неделю): N ошибок, топ-3 типа
- CVE: N HIGH/CRITICAL
- Топ-3 риска одной строкой

## Pre-audit results
- pip-audit: <количество CVE по severity>
- Git history secrets: <found/clean>
- .env в .gitignore: ✓/✗
- .env закоммичен: ✓/✗
- Прод-логи (неделя): топ-5 типов ошибок с примерами строк

## Baseline check
- Error alerting: ✓/✗ (если ✗ — почему критично)
- Тесты: ✓/✗/частично (что покрыто, чего не хватает — см. секцию "Тесты которые написать")
- .env.example: ✓/✗
- Health-check: ✓/✗/N/A
- README: ✓/✗

## P0 — фиксить сейчас
### [P0-1] <Короткое имя>
- **Файл:** path:line
- **Что не так:** 1-2 предложения
- **Почему опасно:** конкретный сценарий
- **Как починить:** 1-2 предложения (НЕ писать код)

### [P0-2] ...

## P1 — в spec (фиксить когда зайдёт связанная задача)
(тот же формат)

## P2 — backlog
- Одной строкой каждое
- Если есть homegrown scraper → "[P2-X] Migrate <file> from requests/BS4 to Firecrawl"

## Тесты которые нужно написать
### [TEST-1] <file.py::function_name>
- **Сценарии:**
  - Happy: <описание>
  - Error: <ошибки>
  - Edge: <граничные>
- **Моки:** <список>
- **Fixtures:** <список>
- **Почему:** что сломается без этого теста

### [TEST-2] ...

## Limits of this audit
Что НЕ покрыто этим аудитом (честно):
- Performance под нагрузкой → нужен load test
- Memory growth over time → нужен мониторинг RSS неделю
- N+1 queries → нужен query log + EXPLAIN
- Полная статика → запусти отдельно: `ruff check`, `mypy`, `bandit`
- Регрессии → нужны написанные тесты (см. секцию выше)
\```

После создания отчёта — кратко доложи:
"Audit готов: vault/01-Projects/<PROJECT>/audit-<date>.md. N findings (P0: X, P1: Y). Baseline: <статус>. Прод-логи: <топ ошибка>. Топ-риск: <одна фраза>. Тестов нужно написать: N."

---

## Что НЕ делать
- Не предлагать рефакторинг "для красоты"
- Не писать код фиксов в отчёте
- Не упоминать P3 стилистику
- Если проект <500 строк — упрости P2 секцию
- Не трогать rss-bot (отключён намеренно)
- Не запускать `pytest` — только проверка наличия
- **НЕ обещать что нашёл все баги.** В Limits честно перечислить пробелы.

---

## После аудита

Игорь решает по каждому P0/P1 что делать:
- P0 → отдельная сессия с фиксом
- P1 → spec в `~/vault/01-Projects/<PROJECT>/specs/`
- P2 → backlog проекта
- TEST-* → отдельная сессия "напиши тесты по audit-<date>.md", приоритезированно по critical paths
```

---

## Связанные шаблоны

- [[project-baseline]] — стандарт baseline для прод-проекта
- [[triage-prompt]] — выбрать какой проект аудить первым

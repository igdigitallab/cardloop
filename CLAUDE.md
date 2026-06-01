> CLAUDE.md = правила работы и gotchas для агентов. Карта кода → ARCHITECTURE.md. API → docs/API.md. Запуск → CONTRIBUTING.md.

# CLAUDE.md — Claude-Ops

IDE-среда для управления проектами через Claude Agent SDK. Три канала: кокпит (`claude-ops.coscore.us`), Telegram (@ziraclaudebot), канбан-автозапуск. Один движок `run_engine()`, full-auto.

Specs: `~/vault/01-Projects/Claude-Ops-Bot/specs/`.

---

## Что где (краткая карта)

- `bot.py` — TG-канал + движок `run_engine()` (async-генератор событий `{tool|text|result|rate_limit|error}`, транспорт-независимый). Потребители: `run_agent` (TG-адаптер), `_run_card` и `api_project_chat` (webapp.py). `running[k]=True` резервируется СИНХРОННО до первого await.
- `webapp.py` — aiohttp-кокпит. **НЕ импортит `bot.py`** — всё получает через `ctx` (dict ссылок: topics/sessions/running/resolve_project/run_engine/DATA/…), переданный из `bot.py`.
- `data/topics.json` — **СЛОЙ 1**: привязка `"chat:thread" → {project,cwd,model}`. Вечная, `/reset` не трогает.
- `data/sessions.json` — **СЛОЙ 2**: `"chat:thread" → session_id`. Чистит только `/reset`.
- `data/prompts.json` — шаблоны промтов кокпита (CRUD через `/api/prompts`). **НЕ в git**.
- `claude-ops-bot.service` → `/etc/systemd/system/`.

Подробнее — ARCHITECTURE.md.

---

## Git

- Репо: `github.com/Zira777ru/claude-ops-bot` — **PRIVATE** (инфра-контрол-плейн: внутр. IP/тоннели/OPSEC-зоны в этом файле; публиковать только после санитизации).
- `.gitignore` исключает: `.env`, `data/` (chat IDs/сессии/audit/логи), `venv/`, `web/node_modules`, `web/dist`, `.worktrees/`.
- ⚠️ Перед коммитом нового: проверить, что секрет/значение не попало в трекаемые файлы.
- Параллельные агенты → `git worktree add .worktrees/<name> -b <branch>`; после — `git worktree prune`.

---

## Операции

- Логи: `sudo journalctl -u claude-ops-bot -f`
- Рестарт из агента: `bash /home/igor/claude-ops-bot/restart-self.sh` (ЕДИНСТВЕННЫЙ безопасный способ).
- Рестарт из терминала: `sudo systemctl restart claude-ops-bot`.
- После правки `bot.py`/`webapp.py` — обязательно рестарт сервиса.
- После правки `web/` — пересобрать: `cd web && npm run build`.

---

## Gotchas (не наступать повторно)

### Auth и окружение
- **Auth = подписка, НЕ API.** SDK читает `~/.claude/.credentials.json` (claudeAiOauth). `ANTHROPIC_API_KEY` НЕ задавать нигде — `bot.py` его явно `pop`-ает, в unit его нет. Иначе уйдёт на API-биллинг.
- **systemd PATH.** Юнит задаёт `PATH=/home/igor/.npm-global/bin:...` — иначе SDK не найдёт native-бинарь `claude`. И `HOME=/home/igor` для доступа к credentials.
- **bypassPermissions + full-auto.** Бот сам пушит/деплоит/удаляет. Необратимое репортит постфактум (footer ⚠️). Доступ строго `ALLOWED_USERS={282311426}`.

### Рестарт и cgroup
- **САМО-рестарт = суицид.** Бот живёт в cgroup своего systemd-сервиса. Любой `systemctl stop/restart/kill` ИЛИ `kill/pkill` своего процесса из его же шелла сносит cgroup ПОСРЕДИ команды → `stop && start` не доживает до `start`. **Защита:** PreToolUse-хук `~/.claude/hooks/guard-self-lifecycle.sh` блокирует такие Bash-команды. **Для правок — только `bash restart-self.sh`** (detached через `systemd-run` вне cgroup).
- **Рестарт ОБРЫВАЕТ текущий ход + все sub-agents.** Даже корректный `bash restart-self.sh` убивает Python-процесс агента. Правила: (1) Перед `restart-self.sh` — отправь Игорю полный итог, заверши ход. (2) Если есть `in_progress` sub-agents — жди их завершения. (3) После `restart-self.sh` — никаких Bash-команд в этом ходу. (4) Smoke / `curl /api/health` — в следующем сообщении.
- **pkill footgun.** НЕ делать `pkill -f "bot.py"` — паттерн совпадает с командной строкой самой команды и убивает шелл (exit 144). Глушить через systemd или по PID.
- **Один getUpdates.** Не запускать второй инстанс (nohup + systemd одновременно) — конфликт long-polling.

### Telegram и рендер
- **HTML parse_mode + сырой ответ модели = краш.** Ответ модели часто содержит `<title>`/`<div>` и пр. → Telegram `BadRequest: unsupported start tag`. Ответ ВСЕГДА гнать через `md_to_html()`, `send()` имеет fallback на plain при BadRequest.
- **`md_to_html` рендерит и сворачивает код.** Поддержка: `#`заголовки→жирный, `-*+`→`•`, ` ```блоки``` `→`<pre>`, инлайн `код`, `[ссылки](url)`, *курсив*, **жирный**. Блок кода > `CODE_MAX_LINES`(20) сворачивается в превью(10) + маркер «‹N строк свёрнуто›». Стратегия: код/ссылки в плейсхолдеры `\x00P#\x00` ДО `html.escape`, потом markdown, потом возврат. Не трогать порядок шагов.
- **Транзиентные сбои TG = пропажа ответа.** `_tg_call()` ретраит транзиентные (RetryAfter→sleep, Network/TimedOut→expo backoff, BadRequest НЕ ретраит — он логический). В `run_agent` финал — СНАЧАЛА `send(ответ)`, ПОТОМ `delete(status)`. Порядок не менять.
- **Privacy Mode.** Бот — админ группы → получает все сообщения независимо от Privacy Mode. Если перестанет видеть — выключить Privacy у @BotFather.
- **Нет интерактива в TG.** `AskUserQuestion` в `DISALLOWED_TOOLS`. `system_prompt` = preset `claude_code` + `append` TELEGRAM_NUDGE: только TG-специфика (нет кнопок→спроси текстом). Не передавать system_prompt строкой — затрёт личность Claude Code.
- **nudge держать тонким.** Только TG-специфика в `TELEGRAM_NUDGE`. Всё про «как работать» — в CLAUDE.md. НЕ дублировать правила в nudge: лишний контекст = агент тупее.
- **Cost убран из футера** — на подписке $ шум. Осталась только команда `/cost` по запросу.

### Конкурентность и состояние
- **Гонка конкурентности.** Резерв слота `running[k]=True` ставится СИНХРОННО в `on_message` до первого `await`. `safe_run` снимает в `finally`. Два быстрых сообщения → второе «уже работаю».
- **Доска стирает задачи агентов.** `GET /tasks` парсит → канонизирует → перезаписывает. Если агент писал буллеты `- текст` без `[ ]`, `_CARD_RE` не матчил → 0 карточек → весь файл стирался. Три слоя защиты: (1) `_PLAIN_CARD_RE` принимает буллеты без чекбокса; (2) `_count_potential_cards(raw)` пропускает запись если `parsed < potential`; (3) `asyncio.Lock` per-cwd сериализует write-операции.
- **Front-state гигиена.** `activeId === '__global__'` не сбрасывать в cleanup; mounted-таб через `display:none`; `busActiveRef` восстанавливается из `GET /api/projects/{id}/running` при mount ChatTab; TASKS.md write пропускается если файл изменился снаружи.

### Безопасность
- **Детектор «необратимого» — точные подстроки.** НЕ использовать `-f `/`rm `/`kill ` (ловят `tail -f`, `perform` и т.п.). Только `rm -rf`/`rm -f`/`git push`/`--force` и пр.
- **Анти-traversal.** `_resolve_safe` / `_resolve_global_safe` — resolve+startswith trailing-slash. `.env*` 403 (кроме `.env.example`). `.git/venv/node_modules/dist/__pycache__` скрыты+403.
- **card_id валидируется** `_valid_card_id`/`_CARD_ID_RE` (не допускать path-injection через card_id).

### C2-gate: worktree-режим карточек
- **Детектор режима**: git-репо + чистое дерево → `worktree`; иначе → `legacy` (прогон прямо в cwd).
- **Worktree жизненный цикл**: setup в `.worktrees/card-<id>` → прогон агента в ветке `card-<id>` → авто-коммит → сайдкар `.json` с `mode/has_changes/applied/discarded`.
- **Worktree НЕ удаляется** после прогона — остаётся до apply/discard.
- **apply**: `merge --no-ff card-<id>` в main; конфликт → 409, `merge --abort`, worktree жив. apply-success → worktree+ветка удалены, карточка Done.
- **discard**: worktree+ветка удалены, карточка Backlog.
- **Orphan worktrees** после краша: остаются на диске `.worktrees/`. Уборка — в Backlog (не в этой итерации).
- **НИКОГДА** не делать `git branch -D` на ветках кроме `card-*` (pattern валидирован `_valid_card_id`).

### Прочее
- **Проценты лимитов ≠ из SDK.** Пассивный `RateLimitEvent` SDK даёт только `status`+`resets_at`, `utilization=None`. Источник % — oauth-эндпоинт `GET https://api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`). `webapp.py:api_usage` тянет его (кэш 60с). TG-команда `/usage` пока на пассивном.
- **LogsTab: `log_cmd` в topics.json.** Таб «Логи» запускает `log_cmd` через subprocess (timeout 8с, берёт последние 300 строк). Если не задан — empty state. Задать: в `data/topics.json` для проекта добавить `"log_cmd": "journalctl -u my-service -n 300 --no-pager"`.
- **TabId актуальные:** `overview | claude-md | logs | board | files | memory`.
- **Тест-харнес userbot.** Слать боту только от аккаунта Игоря (282311426). pyrogram 2.0.106 — греть `get_chat(invite_link)` перед send; в топик `reply_to_message_id=<thread_id>`. Сессия — `networking-os/secrets/tg.session`.

---

## Audit / watchdog / файлы

- **Audit-лог:** `data/audit/audit-YYYY-MM.log` — каждая задача: `TASK` (промпт), `BASH`/`BASH⚠️` (⚠️=необратимое), `EDIT/WRITE` (файлы), `DONE`.
- **Watchdog:** нет событий SDK `STALL_SECONDS` (300с) ИЛИ total > `MAX_SECONDS` (1800с) → `client.interrupt()` + «⚠️ авто-прервано watchdog».
- **Приём файлов:** `on_message` ловит `Document.ALL | PHOTO`; качает в `data/inbox/` (лимит TG 20MB). Inbox растёт — при желании добавить уборку.

---

## Привязка проектов

`forum_topic_created` → авто-привязка по имени через `REGISTRY`/`_REG_RAW` в bot.py. Ручками — `/project <имя|путь>`. Новый проект → добавить алиас в `_REG_RAW` (или само подхватится сканом `~` по basename) + создать топик (`/newtopic <имя>`).

---

## Шаблоны проектов

`templates/*.tpl` — стартеры для новых проектов (кнопка «+ Новый проект»):
- `CLAUDE.md.tpl` · `TASKS.md.tpl` · `README.md.tpl` · `.gitignore.tpl`
- Переменные `{{name}}` / `{{date}}` / `{{slug}}` → `_render_template` в webapp.py.
- **`CLAUDE.md.tpl` содержит раздел «Правила работы в кокпите»** — копируется во все новые проекты. НЕ удалять.

`templates/reference/` — копии из `~/vault/03-Resources/_templates/`:
- `project-baseline.md` · `audit-prompt.md` · `triage-prompt.md` · `refactor-prompt.md` · `spec.md` · `project.md`
- ⚠️ Это **копии**: при правке шаблона в vault → ре-копировать сюда вручную.

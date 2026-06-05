> CLAUDE.md = правила работы и gotchas для агентов. Карта кода → ARCHITECTURE.md. API → docs/API.md. Запуск → CONTRIBUTING.md.

# CLAUDE.md — Claude-Ops

IDE-среда для управления проектами через Claude Agent SDK. Три канала: кокпит (`YOUR_DOMAIN`), Telegram (@YOUR_BOT), канбан-автозапуск. Один движок `run_engine()`, full-auto.

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

- Репо: `github.com/YOUR_GITHUB/claude-ops-bot` — **PRIVATE** (инфра-контрол-плейн: внутр. IP/тоннели/OPSEC-зоны в этом файле; публиковать только после санитизации).
- `.gitignore` исключает: `.env`, `data/` (chat IDs/сессии/audit/логи), `venv/`, `web/node_modules`, `web/dist`, `.worktrees/`.
- ⚠️ Перед коммитом нового: проверить, что секрет/значение не попало в трекаемые файлы.
- ⚠️ **Anti-hardcode (проект идёт в OSS).** В трекаемый код/доки — НИКАКОГО персонального/инфра-хардкода: пути → `$HOME`/относительные (не `/home/<user>/…`), ID/токены/пароли → `.env` (+ плейсхолдер в `.env.example`), реестр проектов → `data/registry.json` (gitignored), имя оператора/язык → env (`OPERATOR_NAME`/`RESPONSE_LANGUAGE`). Реальное значение оператора — только в gitignored-конфиге; в код идёт чтение из него. Новую персональную/инфра-константу не вписывать в код — параметризовать. Детали и инвентарь → `specs/spec-014-oss-hardening.md`; мультиюзер → `specs/spec-013-multi-user.md`.
- ⚠️ **English-only (the project ships in English).** All NEW code, comments, docstrings, log/print output, user-facing strings, UI, and docs MUST be in English. Do not add Russian text to the codebase. The agent's **reply** language is controlled separately by env `RESPONSE_LANGUAGE` (not hardcoded) — Igor's instance keeps it `по-русски`, so the agent still answers in Russian while the code/UI stay English. Plan & progress → `specs/spec-015-oss-runtime.md`.
- Параллельные агенты → `git worktree add .worktrees/<name> -b <branch>`; после — `git worktree prune`.

---

## Операции

- Логи: `sudo journalctl -u claude-ops-bot -f`
- Рестарт из агента: `bash $HOME/claude-ops-bot/restart-self.sh` (ЕДИНСТВЕННЫЙ безопасный способ).
- Рестарт из терминала: `sudo systemctl restart claude-ops-bot`.
- После правки `bot.py`/`webapp.py` — обязательно рестарт сервиса.
- После правки `web/` — пересобрать: `cd web && npm run build`.

---

## Gotchas (не наступать повторно)

### Auth и окружение
- **Auth = подписка, НЕ API.** SDK читает `~/.claude/.credentials.json` (claudeAiOauth). `ANTHROPIC_API_KEY` НЕ задавать нигде — `bot.py` его явно `pop`-ает, в unit его нет. Иначе уйдёт на API-биллинг.
- **systemd PATH.** Юнит задаёт `PATH=$HOME/.npm-global/bin:...` — иначе SDK не найдёт native-бинарь `claude`. И `HOME=/home/<user>` для доступа к credentials.
- **bypassPermissions + full-auto.** Бот сам пушит/деплоит/удаляет. Необратимое репортит постфактум (footer ⚠️). Доступ строго `ALLOWED_USERS={<YOUR_TELEGRAM_ID>}`.

### Рестарт и cgroup
- **САМО-рестарт = суицид.** Бот живёт в cgroup своего systemd-сервиса. Любой `systemctl stop/restart/kill` ИЛИ `kill/pkill` своего процесса из его же шелла сносит cgroup ПОСРЕДИ команды → `stop && start` не доживает до `start`. **Защита:** PreToolUse-хук `~/.claude/hooks/guard-self-lifecycle.sh` блокирует такие Bash-команды. **Для правок — только `bash restart-self.sh`** (detached через `systemd-run` вне cgroup).
- **Рестарт ОБРЫВАЕТ текущий ход + все sub-agents.** Даже корректный `bash restart-self.sh` убивает Python-процесс агента. Правила: (1) Перед `restart-self.sh` — отправь оператору полный итог, заверши ход. (2) Если есть `in_progress` sub-agents — жди их завершения. (3) После `restart-self.sh` — никаких Bash-команд в этом ходу. (4) Smoke / `curl /api/health` — в следующем сообщении.
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

### Самолечение (Spec 010)
- **OFF по умолчанию — незыблемо.** `_self_heal_enabled(project)` = False если нет `self_heal: true` в topics.json ИЛИ env `SELF_HEAL_ENABLED=1`. Ни один проект не включён автоматически.
- **НИКОГДА не auto-apply.** Чинильщик доходит только до Review. `api_card_apply` НЕ вызывается из самолечения. Merge в основное дерево — всегда руками Игоря.
- **Лимит 1 попытка/инцидент.** `heal_attempted=true` пишется в description инцидента ДО запуска агента. Повторная попытка на том же инциденте не будет запущена (предотв. зацикливание при краше).
- **Лимит конкурентности.** `_self_heal_active_count <= _SELF_HEAL_MAX_CONCURRENT (2)`. При занятом running lock — пропуск.
- **Только git+clean.** `_card_run_mode == "worktree"` обязателен. Не-git и dirty-дерево → пропускаем.
- **Полная наблюдаемость.** Timeline `kind:"self_heal"` + TG-пинг оператору на каждую попытку.
- **heal_badge в description карточки:** `heal_badge=🔧 авто-починка · гейт ✓/✗` — читается UI для CSS-бейджа.

### C2-gate: worktree-режим карточек
- **Детектор режима**: git-репо + чистое дерево → `worktree`; иначе → `legacy` (прогон прямо в cwd).
- **Worktree жизненный цикл**: setup в `.worktrees/card-<id>` → прогон агента в ветке `card-<id>` → авто-коммит → сайдкар `.json` с `mode/has_changes/applied/discarded`.
- **Worktree НЕ удаляется** после прогона — остаётся до apply/discard.
- **apply**: `merge --no-ff card-<id>` в main; конфликт → 409, `merge --abort`, worktree жив. apply-success → worktree+ветка удалены, карточка Done.
- **discard**: worktree+ветка удалены, карточка Backlog.
- **Orphan worktrees** после краша: остаются на диске `.worktrees/`. Уборка — в Backlog (не в этой итерации).
- **НИКОГДА** не делать `git branch -D` на ветках кроме `card-*` (pattern валидирован `_valid_card_id`).
- **Quality gate (Spec 009):** `POST .../check` → `_run_quality_gate(wt_path)` гоняет тесты В worktree (не в основном дереве). Вердикт `safe/risky/unknown` сохраняется в `meta.gate`. Apply **НЕ блокируется** — пользователь решает сам. Гейт НЕ встроен в apply — только через явный вызов «🧪 Проверить». Линт — out of scope (итерация 1).

### Память проекта (Spec 006)
- **Память в репо, НЕ в `~/.claude`.** Новое место: `<cwd>/.claude-ops/memory/` — коммитится в git. Старое (`~/.claude/projects/<cwd>/memory/`) — fallback при чтении GET (read-only совместимость). Не путать.
- **Агент пишет через Write.** Специальных API для агента не нужно — он пишет `.claude-ops/memory/<slug>.md` обычным Write. TELEGRAM_NUDGE напоминает об этом одной строкой.
- **MEMORY.md = авто-индекс.** Перестраивается при каждом write/delete. НЕ редактировать руками — перезапишется. Записи в slug-файлах с frontmatter (type/created).
- **Slug валидация:** `^[a-z0-9][a-z0-9-]{0,60}\.md$` + `MEMORY.md`. Uppercase / traversal (`../`) → 400.

### Секреты проекта (Spec 007)
- **Значения не отдаём через API.** GET `/secrets` возвращает только имена ключей (`keys:[...]`). Никакого `values`, `data`, `secrets_map` — только имена. Тест `test_api_secrets_get_returns_only_names` фиксирует это как регрессию.
- **Секреты не в audit/git.** `audit()` принимает только (project, kind, text) — env туда никогда не передаётся. `secrets.env` gitignored автоматически при первой записи.
- **Ключи строго `^[A-Z_][A-Z0-9_]*$`.** lowercase, дефис, пробел, traversal `..` → 400. Это env-injection защита.
- **Изоляция по cwd жёсткая.** `_secrets_read(cwd)` читает только `.claude-ops/secrets/secrets.env` внутри cwd этого проекта — никакой утечки между проектами.
- **TabId актуальные:** `overview | claude-md | logs | board | files | memory | timeline | settings` (8 вкладок; `secrets` — теперь секция в «Настройках», не вкладка; «Лента»→«Активность» — Spec 011 Ф2).

### Прочее
- **error_middleware ловит ВСЁ → benign-disconnect = ложные инциденты.** Глобальный `error_middleware` (Ф0) логирует необработанные исключения строкой `UNHANDLED exc_class=...`, которую парсит сканер → карточка в Failed. Клиент закрыл SSE-вкладку → `ConnectionResetError`/`ClientConnectionResetError` («Cannot write to closing transport»). Это benign: middleware ПРОБРАСЫВАЕТ их (не 500/не лог), а сами стрим-хендлеры (`_sse_stream` heartbeat, `api_project_chat._send`) оборачивают `resp.write` в `try/except (ConnectionResetError, ConnectionAbortedError)`. Добавляешь новый стрим-эндпоинт — делай так же, иначе зальёшь доску ложными err-карточками (было: 124+ за ночь). `asyncio.CancelledError` — BaseException, проходит мимо `except Exception` сам.
- **card_id инцидентов = `err-<hash6>`.** `_CARD_ID_RE = ^(err-)?[a-f0-9-]{4,20}$` — префикс `err-` разрешён явно (буквы вне hex иначе ломают валидацию → move/delete/update инцидентов отдавали 400 и копились в Failed). Тело без точек/слешей → traversal невозможен.
- **Проценты лимитов ≠ из SDK.** Пассивный `RateLimitEvent` SDK даёт только `status`+`resets_at`, `utilization=None`. Источник % — oauth-эндпоинт `GET https://api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`). `webapp.py:api_usage` тянет его (кэш 60с). TG-команда `/usage` пока на пассивном.
- **LogsTab: `log_cmd` в topics.json.** Таб «Логи» запускает `log_cmd` через subprocess (timeout 8с, берёт последние 300 строк). Если не задан — empty state. Задать: в `data/topics.json` для проекта добавить `"log_cmd": "journalctl -u my-service -n 300 --no-pager"`. journalctl без sudo работает (igor в группе `adm`); сервисы под igor.
  - **`topics.json` теперь hot-reload (рестарт НЕ нужен).** Исходно `topics` грузился раз на старте (`bot.py:167`) в in-memory dict `ctx["topics"]`, и прямая Edit/Write файла была невидима до рестарта (на этом агент и спалился). Починено: `_maybe_reload_topics(ctx)` (webapp.py, вызывается в начале `_collect_projects`) mtime-гейтом перечитывает файл с диска и обновляет `ctx["topics"]` IN-PLACE (`clear()`+`update()`) — тот же объект видят и кокпит, и бот. Диск авторитетен (`save_topics()` всегда туда пишет). Битый/частичный файл при гонке → JSONDecodeError → тихо оставляем текущую версию. **Прямая правка topics.json подхватывается на лету.**
  - **id проекта в API = basename cwd, НЕ поле `project`.** `/api/projects/<id>/logs` ждёт `networking-os`, а не `Networking-OS` (`_project_id(cwd)`). Фронт шлёт basename сам; важно для ручных curl.
  - **Кнопка «настроить логи» (LogsTab.tsx) отдаёт агенту полную инструкцию.** Empty-state создаёт backlog-карточку: короткий `text` (заголовок) + детальный `description` (как выбрать log_cmd/test_cmd: systemd/docker/файл, exec-без-sudo-без-shell, обязательная проверка вывода, test_cmd относителен cwd проекта, hot-reload вместо рестарта). `_run_card` склеивает промпт = `text + "\n\n" + description`. Многострочный description round-trip'ит через TASKS.md (`  > строка` на строку; пустые строки тоже, `_DESC_LINE_RE=^  > (.*)$`). НЕ ужимать обратно в однострочник — агент тогда снова сделает криво.
- **Timeline (Spec 008): `data/timeline/<slug>.jsonl`.** Каждое событие `_bus_publish` персистируется. Slug = `cwd.replace('/', '-')`. Ротация при >5MB → `.jsonl.1` (одна; старая `.1` перезаписывается). Запись глотает все исключения (прогон не ломается). env-поле никогда не пишется. Инициализация: `_timeline_init(ctx)` в `start()`. `_TIMELINE_DATA_DIR` / `_TIMELINE_TOPICS` — модульные переменные (None до init — корректно).
- **TabId актуальные:** `overview | claude-md | logs | board | files | memory | timeline | settings` (8 вкладок; `secrets` — секция в «Настройках», не вкладка — Spec 011 Ф2).
- **Тест-харнес userbot.** Слать боту только от аккаунта оператора (`<YOUR_TELEGRAM_ID>`). pyrogram 2.0.106 — греть `get_chat(invite_link)` перед send; в топик `reply_to_message_id=<thread_id>`. Сессия — `<userbot_project>/secrets/tg.session`.

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

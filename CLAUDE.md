# CLAUDE.md — Claude-Ops

IDE-среда для управления проектами через Claude Agent SDK. Три канала: кокпит (`claude-ops.coscore.us`), Telegram (@ziraclaudebot), канбан-автозапуск. Один движок `run_engine()`, full-auto. Specs: `~/vault/01-Projects/Claude-Ops-Bot/specs/`.

> ⚠️ **HTTP-транспорт для очков G2 — заглушен 2026-05-28.** Код функций (`run_for_glasses`, эндпоинты `/projects`/`/run`/`/reset`, CORS middleware) **в `bot.py` оставлен**, но отключён через пустой `GLASSES_TOKEN` в `.env` (`#disabled-2026-05-28# GLASSES_TOKEN=...`). HTTP-сервер сам не поднимается. Чтобы включить обратно: раскомментить токен в `.env` + восстановить CF tunnel ingress на pve + CNAME в Cloudflare → рестарт бота. Контекст почему свернули — `~/vault/01-Projects/even-g2/specs/claude-glasses.md`.

## Что где
- `bot.py` — TG-канал + движок. **`run_engine()`** (async-генератор событий `{tool|text|result|rate_limit|error}`, транспорт-независимый, F0 done 2026-05-29). Транспорты-потребители: `run_agent` (TG-адаптер: статус-сообщение/watchdog/audit/финал) и `run_for_glasses` (HUD-адаптер, ≤300 chars). `running[k]=True` резервируется СИНХРОННо в адаптере до первого await; `run_engine` заменяет на реальный client. Новые триггеры (web-чат C1, авто-запуск карточки F1) подключаются как потребители `run_engine`, НЕ дублируют SDK-цикл.
- `.env` — секреты (BOT_TOKEN, GROUP_CHAT_ID, ALLOWED_USERS). НЕ в git.
- `data/topics.json` — **СЛОЙ 1**: привязка `"chat:thread" → {project,cwd,model}`. Вечная, `/reset` не трогает.
- `data/sessions.json` — **СЛОЙ 2**: `"chat:thread" → session_id`. Чистит только `/reset`.
- `data/prompts.json` — шаблоны промтов кокпита (CRUD через `/api/prompts`). **НЕ в git** (`data/` в gitignore).
- `claude-ops-bot.service` → `/etc/systemd/system/`.

## Git (с 2026-05-29)
- Репо: `github.com/Zira777ru/claude-ops-bot` — **PRIVATE** (инфра-контрол-плейн: внутр. IP/тоннели/OPSEC-зоны в этом файле; публиковать только после санитизации — будущий OSS-шаг из vision-strategy.md).
- `.gitignore` исключает: `.env`, `data/` (chat IDs/сессии/audit/логи), `venv/`, `web/node_modules`, `web/dist`, `.worktrees/`. Захардкоженных секретов в коде нет (всё через `os.environ`). `.env.example` — ключи-плейсхолдеры.
- ⚠️ Перед коммитом нового: проверить, что секрет/значение не попало в трекаемые файлы.
- Агенты-параллель → `git worktree add .worktrees/<name> -b <branch>`; после — `git worktree prune`.

## Операции
- Логи: `sudo journalctl -u claude-ops-bot -f`
- Рестарт: `sudo systemctl restart claude-ops-bot`
- После правки `bot.py` — обязательно рестарт сервиса.

## Gotchas (не наступать повторно)
- **Auth = подписка, НЕ API.** SDK читает `~/.claude/.credentials.json` (claudeAiOauth). `ANTHROPIC_API_KEY` НЕ задавать нигде — bot.py его явно `pop`-ает, в unit его нет. Иначе уйдёт на API-биллинг.
- **systemd PATH.** Юнит задаёт `PATH=/home/igor/.npm-global/bin:...` — иначе SDK не найдёт native-бинарь `claude`. И `HOME=/home/igor` для доступа к credentials.
- **pkill footgun.** НЕ делать `pkill -f "bot.py"` — паттерн совпадает с командной строкой самой команды и убивает шелл (exit 144). Глушить через systemd или по PID.
- **САМО-рестарт = суицид (fix 2026-05-30).** Бот живёт в cgroup своего systemd-сервиса. Любой `systemctl stop/restart/kill` ИЛИ `kill/pkill` своего процесса из его же шелла сносит cgroup ПОСРЕДИ команды → `stop && start` не доживает до `start`, сервис остаётся выключенным (так бот вырубил себя 2026-05-30: `... stop claude-ops-bot && sleep 1 && start &` — start не выполнился). **Защита:** PreToolUse-хук `~/.claude/hooks/guard-self-lifecycle.sh` (в `~/.claude/settings.json`, грузится через `setting_sources=["user"]`) блокирует такие Bash-команды для ВСЕХ каналов и терминала. **Для применения правок `bot.py`/`webapp.py` — только `bash /home/igor/claude-ops-bot/restart-self.sh`** (detached через `systemd-run` вне cgroup, переживает смерть бота). `status`/`is-active`/`journalctl` — разрешены.
- **Рестарт ОБРЫВАЕТ текущий ход + все sub-agents (правило 2026-05-31).** Даже корректный `bash restart-self.sh` убивает Python-процесс агента: моя сессия живёт ВНУТРИ cgroup бота, и `systemd-run`-таймер сносит cgroup через ~1с. Последствия проверены на практике:
  - **Финальный ответ Игорю обрезается.** Если итог не отправлен ДО `restart-self.sh`, Игорь его не увидит (SDK переподнимет conversation в новой сессии, но текущий «in-flight» ответ улетает в exit 144 как процесс).
  - **Любая команда ПОСЛЕ `restart-self.sh` в одном ходу — не выполнится.** `&& sleep 3 && curl smoke` после рестарта = exit 144. Smoke-проверки делать в **следующем ходу/сессии**, не в одной цепочке.
  - **Параллельные sub-agents (`Agent(run_in_background=true)`) тоже умирают** — они дочерние процессы моей сессии. Незакоммиченная работа в их worktree пропадает (2026-05-31: tests-agent оставил 5 готовых файлов БЕЗ commit — пришлось коммитить за него; docs-agent worktree полностью удалился, работа потеряна).
  - **Правила:** (1) Перед `restart-self.sh` — отправь Игорю полный итог текстом, ЗАВЕРШИ ход. (2) Перед рестартом — `TaskList`: если есть `in_progress` sub-agents — ЖДИ их завершения (нотификация о completion = их commits в worktree уже есть). (3) После `restart-self.sh` — никаких больше Bash-команд в этом ходу. (4) Smoke / `curl /api/health` — в следующем сообщении, когда сессия восстановится.
- **Privacy Mode.** Бот — админ группы → получает все сообщения независимо от Privacy Mode (включая пересланные от пользователя). Если перестанет видеть — выключить Privacy у @BotFather.
- **Один getUpdates.** Не запускать второй инстанс (nohup + systemd одновременно) — конфликт long-polling.
- **bypassPermissions + full-auto.** Бот сам пушит/деплоит/удаляет. Необратимое репортит постфактум (footer ⚠️). Доступ строго `ALLOWED_USERS={282311426}`.
- **HTML parse_mode + сырой ответ модели = краш.** Ответ модели часто содержит `<title>`/`<div>` и пр. → Telegram `BadRequest: unsupported start tag`. Ответ ВСЕГДА гнать через `md_to_html()` (escape → лёгкий markdown), а `send()` имеет fallback на plain при BadRequest. Не слать сырой текст с `parse_mode=HTML`.
- **Транзиентные сбои TG = пропажа ответа (fix 2026-05-29).** На длинных задачах финальная отправка ловит `NetworkError`/`Bad Gateway`/`RetryAfter` (не `BadRequest`!). Раньше `send()` их не перехватывал + статус удалялся ДО отправки → пропадали и прогресс, и ответ. Теперь: `_tg_call()` ретраит транзиентные (RetryAfter→sleep, Network/TimedOut→expo backoff, BadRequest НЕ ретраит — он логический); `send()`/`report_error()` идут через него; в `run_agent` финал — СНАЧАЛА `send(ответ)`, ПОТОМ `delete(status)`. Порядок не менять: при сбое отправки на экране должен остаться прогресс, а не пустота.
- **Cost убран из футера** — на подписке $ это шум. Осталась только команда `/cost` по запросу.
- **Проценты лимитов ≠ из SDK (fix 2026-05-30).** Пассивный `RateLimitEvent` SDK на этой подписке даёт только `status`+`resets_at`, `utilization=None` (проверено) — потому бейдж Usage в кокпите показывал лишь время. Источник процентов — официальный oauth-эндпоинт `GET https://api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`, Bearer = `accessToken` из `~/.claude/.credentials.json`), тот же что бьёт `/usage` в Claude Code: `{five_hour,seven_day,seven_day_sonnet,…:{utilization 0-100, resets_at ISO}}`. `webapp.py:api_usage` тянет его (кэш 60с, util→0-1, ISO→unix), фоллбэк на пассивный снимок. TG-команда `/usage` (bot.py:format_usage) пока на старом пассивном источнике — % там по-прежнему нет.
- **Гонка конкурентности.** Резерв слота `running[k]=True` ставится СИНХРОННО в `on_message` до первого `await` (не в `run_agent` — иначе два быстрых сообщения проскакивают оба и затирают session_id / два full-auto процесса правят файлы разом). `safe_run` снимает резерв в `finally`. Проверено userbot'ом (два сообщения 0.4с → второе «уже работаю»).
- **Детектор «необратимого» — точные подстроки.** НЕ использовать `-f `/`rm `/`kill ` (ловят `tail -f`, `perform`, и т.п.). Только `rm -rf`/`rm -f`/`git push`/`--force` и пр.
- **Тест-харнес userbot.** Слать боту можно только от аккаунта Игоря (282311426). pyrogram 2.0.106 НЕ резолвит большой id группы из пустого in-memory кэша → греть `get_chat(invite_link)` перед send; в топик слать `reply_to_message_id=<thread_id>` (нет `message_thread_id`). Сессия — `networking-os/secrets/tg.session`.
- **nudge держать ТОНКИМ (принцип 2026-05-29).** `TELEGRAM_NUDGE` = только TG-специфика (нет кнопок→спроси текстом; краткая проза без дампа кода; `tg-reply`). Всё про «как работать» (scan/хирургичность/права/необратимое) — в CLAUDE.md (project + `~/CLAUDE.md`), агент грузит их через `setting_sources=["user","project"]` — те же файлы, что и терминал. НЕ дублировать правила в nudge: лишний контекст каждый ход = агент тупее, плюс «два хозяина». Читаемость/формат — задача рендера бота, а НЕ инструкций агенту.
- **`md_to_html` рендерит и СВОРАЧИВАЕТ код (2026-05-29).** Поддержка: `#`заголовки→жирный, `-*+`→`•`, ` ```блоки``` `→`<pre>`, инлайн `код`, `[ссылки](url)`, *курсив*, **жирный**. Блок кода >`CODE_MAX_LINES`(20) сворачивается в превью(10) + маркер «‹N строк свёрнуто›» — намеренно (в TG-ответе важна суть, не простыни; правки видны в файлах). Стратегия: код/ссылки в плейсхолдеры `\x00P#\x00` ДО `html.escape`, потом markdown, потом возврат. Не трогать порядок шагов.
- **Front-state гигиена (фиксы 2026-05-30, детали в git log d93418b).** Кейсы: `activeId === '__global__'` не сбрасывать в cleanup; mounted-таб через `display:none`, не `&&` (state дерева не уничтожается); `projectsLoadedRef` гасит flash «Загрузка…» на 15с-полле; `busActiveRef` восстанавливается из `GET /api/projects/{id}/running` при mount ChatTab; `TASKS.md` write пропускается если файл изменился снаружи между read и write.
- **Нет интерактива в TG.** `AskUserQuestion` в `DISALLOWED_TOOLS` (голосовалка некуда показать → агент зависал/решал сам). `system_prompt` = preset `claude_code` + `append` TELEGRAM_NUDGE: агент спрашивает текстом, Игорь отвечает следующим сообщением (resume продолжит). Не передавать system_prompt строкой — затрёт личность Claude Code; только `{type:preset,preset:claude_code,append:...}`.
- **Доска стирает задачи агентов (fix 2026-05-30).** `GET /tasks` парсит → канонизирует → перезаписывает. Если агент писал в TASKS.md обычными буллетами `- текст` (без `[ ]`), `_CARD_RE` не матчил → 0 карточек → весь файл стирался. Три слоя защиты: (1) `_PLAIN_CARD_RE = re.compile(r"^\s*[-*]\s+(?!\[)(.+)$")` принимает буллеты без чекбокса как карточки; (2) `_count_potential_cards(raw)` считает `- ` строки внутри секций — если `parsed < potential`, запись пропускается с WARNING; (3) `asyncio.Lock` per-cwd (`_board_locks: dict[str,Lock]`) сериализует все write-операции. Без всех трёх защит реальный прод потерял 39 задач в networking-os.
- **LogsTab: `log_cmd` в topics.json (added 2026-05-30).** Таб «Логи» (заменил Readme/Specs/Активность) — запускает `log_cmd` через subprocess (timeout 8с, берёт последние 300 строк). Если `log_cmd` не задан — empty state + кнопка «Добавить задачу в бэклог». API: `GET /api/projects/{id}/logs` → `{lines, configured, cmd}`. Задать поле: в `data/topics.json` для проекта добавить `"log_cmd": "journalctl -u my-service -n 300 --no-pager"`.
- **TabId изменился (2026-05-30).** Убраны `readme`, `specs`, `activity`. Актуальные: `overview | claude-md | logs | board | files | memory`.

## Audit / watchdog / файлы (2026-05-29)
- **Audit-лог:** `data/audit/audit-YYYY-MM.log` — каждая задача: `TASK` (промпт), `BASH`/`BASH⚠️` (⚠️=необратимое по `_is_destructive`), `EDIT/WRITE` (файлы), `DONE`. Постоянный след full-auto на проде.
- **Watchdog:** корутина в `run_agent` прерывает зависшую задачу — нет событий SDK `STALL_SECONDS` (300с) ИЛИ总 > `MAX_SECONDS` (1800с) → `client.interrupt()` + «⚠️ авто-прервано watchdog». Лимиты — env. `last_event[0]` тикает на каждом сообщении SDK.
- **Приём файлов:** `on_message` ловит `Document.ALL | PHOTO`; `fetch_files()` качает в `data/inbox/` (лимит TG getFile 20MB), путь отдаётся агенту в промпте. Фото Claude читает как изображение. Inbox растёт — при желании добавить уборку.

## Привязка проектов
`forum_topic_created` → авто-привязка по имени через `REGISTRY`/`_REG_RAW` в bot.py. Ручками — `/project <имя|путь>`. Новый проект-папка → добавить алиас в `_REG_RAW` (или само подхватится сканом `/home/igor` по basename) + создать топик (`/newtopic <имя>` умеет сам бот).

## Браузерный кокпит — webapp.py (Фаза 1, с 2026-05-29)
«Claude-Ops Control Center» — браузерная версия параллельно с TG. **В ТОМ ЖЕ процессе** (общий `running`-замок, иначе гонка по cwd). Spec → `~/vault/01-Projects/Claude-Ops-Bot/specs/spec-002-control-center.md`.
- **`webapp.py`** — aiohttp-кокпит. НЕ импортит `bot.py` (двойное состояние!) — всё состояние получает через `ctx` (dict ссылок: topics/sessions/running/resolve_project/run_for_glasses/DATA/…), переданный из `bot.py`.
- **Подключение:** `bot.py` → `import webapp` + post_init-обёртка `_on_start(app)` поднимает glasses-HTTP (выкл) и `webapp.start(app, ctx)`. **`run_agent` и TG-хендлеры НЕ тронуты** — веб чисто аддитивный.
- **Порт/доступ:** `WEB_PORT=8787` (env), `WEB_PASSWORD` (env — стойкий, в Credentials.md; **в этот файл пароль не писать**, кокпит публичен через туннель). Дефолт `ops-igor-2026` сменён 2026-05-29. Внешне: `https://claude-ops.coscore.us` через proxmox-tunnel (ингресс на pve `/etc/cloudflared/config.yml`, бэкап `config.yml.bak-claudeops`; резолв через wildcard `*.coscore.us`, своя DNS-запись НЕ нужна). LAN/Tailscale: `192.168.0.114:8787`.
- **Auth:** cookie `cops_auth` = `sha256(password+"cops")`, middleware на `/api/*` кроме `/api/health` и `/api/login`. Фронт — React+Vite SPA в `web/`, билд в `web/dist` (`npm run build`), aiohttp отдаёт статику с SPA-fallback.
- **Фаза 1 (готово):** read-only — список проектов + git-health, табы Обзор / CLAUDE.md / Логи / Доска / Файлы / Память. Вкладки Readme/Specs/Активность удалены 2026-05-30 — заменены вкладкой «Логи» (`log_cmd` из topics.json).
- **Доска (готово 2026-05-29, Spec=Kanban=2 файла):** таб «Доска» рендерит `TASKS.md` (в КОРНЕ репо проекта, не vault) как канбан. Секции `## Backlog / In Progress / Review / Failed` = колонки; строка `- [x] текст <!--ops:ID-->` = карточка (статус-символ ` ~ ? !`, `ops:ID` — стабильный якорь, дописывается автоматически при первом GET). **Истина = markdown, БД для плана НЕТ.** Завершённое уходит в `DONE.md` (append-only, дата; **сессии его НЕ читают** — гигиена контекста). Преамбула файла (до первой колонки) сохраняется; прочий текст внутри секций канонизируется при перезаписи. API: `GET/POST /api/projects/{id}/tasks`, `POST .../tasks/{card}/move {to}` (`to`=колонка или `done`→архив), `DELETE .../tasks/{card}`, `GET .../tasks/done`, `GET .../tasks/{card}/run` (сайдкар результата). UI: кнопки ←→ (перенос), ✓ (в Done), ✕ (удалить), 📄 (результат в Review/Failed); добавление — в колонке Backlog.
- **Авто-запуск карточки (F1, done 2026-05-29):** перенос карточки в **In Progress** запускает `run_engine` в проекте под session_key TG-топика → делит `running`-замок и сессию с TG (TG и карточка взаимоисключаются, гонка по cwd закрыта). Занят → 409 «проект занят». По завершении `_run_card` (фоновая задача в webapp.py) пишет результат+git-diff в `data/runs/<card_id>.md`, переносит карточку в **Review** (ok) или **Failed** (err), пингует TG-топик. Замок снимается в `finally`. Фронт поллит доску пока in_progress непуст. **Review = видимость, НЕ откат** (гейт Применить/Отмена с worktree = трек C2, позже). Прогресс пока поллингом (live-стрим = C1).
- **Чат по проекту (C1, done 2026-05-29):** постоянная панель ~45% справа в проекте (не таб — всегда видна; табы слева ~55%; адаптив <900px в столбец). `POST /api/projects/{id}/chat` → `text/event-stream` (web.StreamResponse), потребляет `run_engine`, стримит tool/text вживую. Сессия и `running`-замок **общие с TG/F1** (session_key = tg_thread) — разговор продолжается сквозь каналы (начал в телеге → продолжил в браузере). Disconnect (закрыл вкладку) → `client_gone`, генератор дотягивает в фоне, session_id сохраняется, замок в `finally`. Занят → SSE-error «проект занят». Фронт `ChatTab.tsx`: ReadableStream reader (chunk-boundary-safe парсинг `data:`), markdown-рендер. ⚠️ «Стоп» обрывает только клиентский fetch — движок на сервере дотягивает (замок держится до конца задачи).
- **Проводник файлов (done 2026-05-29):** таб «Файлы» — read-only дерево + просмотр (`.md` рендер, код моно). Эндпоинты `GET /files?path=` (листинг) и `/file?path=` (содержимое). Безопасность: `_resolve_safe` анти-traversal (resolve+startswith trailing-slash), `.env*` 403 (кроме `.env.example`), `.git/venv/node_modules/dist/__pycache__` скрыты+403 (вкл. прямую навигацию), бинарь/>1МБ отбиваются. Граница = cwd проекта (read-only ≤ возможностей агента-чата, который и так может `cat`).
- **Чат: ресайз/сворачивание + сессии (done 2026-05-29):** перетаскиваемый разделитель (ширина в localStorage `cops.chatWidth`/`cops.chatCollapsed`), кнопка свернуть. Сессии — ОБЩАЯ с TG, переключаемая: `GET /sessions` листит SDK-сессии (`~/.claude/projects/<cwd-с-/→->/*.jsonl`, preview=первое сообщение), `POST /session {new|resume}` переключает `sessions[tg_thread]` под замком (занят→409); resume санитизирует session_id (basename-only). «Новая»=/reset, «выбрать»=/resume в UI.
- **Шина активности (done 2026-05-29):** in-process `_bus[session_key→queues]` в webapp.py. `_run_card` публикует `run_start/tool/text/run_end`; `GET /activity-stream` (SSE, heartbeat 25с, отписка в `finally`) — чат-панель постоянно подписана → **прогон карточки виден в чате вживую** («🗂 карточка: …» + стрим). Двойного рендера нет (замок исключает overlap + `streamingRef`-страж). История чата грузится из SDK-транскрипта (`/session-history`). TG-вещание в шину — ещё НЕ сделано (нужен bot.py).
- **Глобальный файловый браузер (done 2026-05-30):** кнопка 📁 в шапке → вкладка «__global__» с деревом от `$HOME`. Эндпоинты `GET /api/global/files?path=` / `/api/global/file?path=` (read) / `POST /api/global/file?path=` (write). Безопасность: `_resolve_global_safe` (root=`Path.home()`, `.env*`/hidden запрещены). Инлайн-редактирование двойным кликом (Ctrl+S сохранить, Esc отмена). Состояние сохраняется в `display:none` — не сбрасывается при смене таба.
- **Промты с категориями (done 2026-05-30):** кнопка 📋 в чате → плавающий аккордеон с группировкой по `category`. CRUD: создание/редактирование (✎) / удаление (✕). Хранятся в `data/prompts.json`. API: `GET/POST /api/prompts`, `PATCH/DELETE /api/prompts/{id}`. Выбор промта вставляет текст в инпут и выделяет первый `[ПЕРЕМЕННАЯ]`.
- **DnD сайдбар (done 2026-05-30):** ручная сортировка проектов drag-and-drop; порядок в localStorage `cops.sidebarOrder`. Свободные чаты скрыты из сайдбара (только в таб-баре вверху).
- **Статус фаз:** F0 ✅ · F1 ✅ · C1 чат ✅ · проводник ✅ · ресайз+сессии ✅ · история чата ✅ · шина live-прогресс ✅ · глоб.браузер ✅ · промты ✅ · DnD-сайдбар ✅ · Логи ✅ · Board wipe protection ✅. В бэклоге: «+ Новый проект», «Контекст сессии», «Память проекта». Дальше: O1 Timeline, C2 гейт, автономный контур.
- ⚠️ После правки `web/` — пересобрать (`cd web && npm run build`); после правки `webapp.py`/`bot.py` — рестарт сервиса.

## HTTP-транспорт для очков G2 (с 2026-05-28)
Параллельно с TG long-polling бот поднимает aiohttp-сервер на `GLASSES_PORT` (default 8765). Вынесен наружу через proxmox-tunnel → `https://claude-ops.coscore.us`. Bearer-auth по `GLASSES_TOKEN` (в `.env`).

Эндпоинты:
- `GET /healthz` — без auth
- `GET /projects` — список из `topics.json` (дедуп по cwd) → меню в плагине
- `POST /run {project, prompt}` → запуск Claude в проекте, `{reply, session_id, project, cwd}`. Reply жёстко обрезан до 300 chars (G2 HUD limit).
- `POST /reset {project}` — сброс сессии очков для проекта

Ключ сессии — `glasses:<project>` (отдельно от TG-ключей `<chat>:<thread>`). Системный промпт — `GLASSES_NUDGE` (телеграфный стиль, ≤250 chars, без вопросов). Модель — `GLASSES_MODEL` (default `sonnet`).

⚠️ **Гонка по cwd**: TG и очки могут одновременно запустить Claude в одном `cwd` (ключи разные → `running` lock не сработает). Пока не критично, при возникновении проблем — добавить cwd-lock.

Запуск aiohttp подвешен через `post_init(start_glasses_http)` в `main()`. Если `GLASSES_TOKEN` пуст — HTTP отключён.

## Шаблоны проектов (с 2026-05-31)

Два уровня шаблонов в корне проекта:

**`templates/*.tpl`** — стартеры для НОВЫХ проектов (кнопка «+ Новый проект»):
- `CLAUDE.md.tpl` · `TASKS.md.tpl` · `README.md.tpl` · `.gitignore.tpl`
- Переменные `{{name}}` / `{{date}}` / `{{slug}}` → `_render_template` в webapp.py
- **`CLAUDE.md.tpl` содержит обязательный раздел «Правила работы в кокпите»** (формат карточек, сессии, файлы) — он копируется во все новые проекты, чтобы агенты знали формат и не теряли задачи. Раздел НЕ удалять при правке проекта.

**`templates/reference/`** — синхронизированные копии из `~/vault/03-Resources/_templates/`:
- `project-baseline.md` — стандарт прод-проекта (тесты, .env.example, healthz, alerting, …)
- `audit-prompt.md` — чек-лист для кнопки «🩺 Аудит проекта» (читается из файла, не хардкод)
- `triage-prompt.md` · `refactor-prompt.md` · `spec.md` · `project.md`
- ⚠️ Это **копии**: при правке шаблона в vault → ре-копировать сюда (drift сам не вылечится). Зачем дубль — проект должен быть self-contained.

## Новые endpoints (с 2026-05-31)
- `POST /api/projects/new` — `~/projects/untitled-<ts>/` со стартовыми шаблонами + спавн онбординг-карточки в In Progress.
- `POST /api/projects/{id}/rename {slug}` — kebab-case (`^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$`), `shutil.move` + апдейт topics.json. 409 если занят/папка существует.
- `GET /api/projects/{id}/health` — синхронная проверка структуры (6 пунктов: CLAUDE.md / правила кокпита / TASKS.md преамбула / README / .gitignore .env / .git), цвет green/yellow/red. Используется блоком «Структура проекта» в Overview-табе.
- `POST /api/projects/{id}/audit` — карточка «🩺 Аудит» в In Progress, агент идёт по `templates/reference/audit-prompt.md` и создаёт карточки на каждую проблему.
- `POST /api/projects/{id}/upgrade` — карточка «🔧 Подтянуть до стандарта»: ДОПОЛНЯЕТ существующие CLAUDE.md/TASKS.md/README/.gitignore по шаблонам, ничего не перезаписывая.

## Тесты (с 2026-05-31)
- Каркас pytest в `tests/`, цель `make test`. Запуск: `venv/bin/python -m pytest -q`.
- Покрыто: парсер/сериализатор доски (критика — регрессия = потеря задач в проде, см. gotcha 2026-05-30), path-traversal в `_resolve_safe`/`_resolve_global_safe`, slug-валидация ренейма, health-чек, smoke по auth/login. 62 passed.

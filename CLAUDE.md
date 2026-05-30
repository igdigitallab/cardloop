# CLAUDE.md — Claude-Ops-Bot

«Claude Code через Telegram». Бот @ziraclaudebot на docker-core запускает Claude Agent SDK по сообщениям из forum-группы «Development». Каждый топик = проект. Spec: `~/vault/01-Projects/Claude-Ops-Bot/specs/spec.md`.

> ⚠️ **HTTP-транспорт для очков G2 — заглушен 2026-05-28.** Код функций (`run_for_glasses`, эндпоинты `/projects`/`/run`/`/reset`, CORS middleware) **в `bot.py` оставлен**, но отключён через пустой `GLASSES_TOKEN` в `.env` (`#disabled-2026-05-28# GLASSES_TOKEN=...`). HTTP-сервер сам не поднимается. Чтобы включить обратно: раскомментить токен в `.env` + восстановить CF tunnel ingress на pve + CNAME в Cloudflare → рестарт бота. Контекст почему свернули — `~/vault/01-Projects/even-g2/specs/claude-glasses.md`.

## Что где
- `bot.py` — весь бот (один файл, минимальная структура). **Движок = `run_engine()`** (async-генератор событий `{tool|text|result|rate_limit|error}`, транспорт-независимый, F0 done 2026-05-29). Транспорты-потребители: `run_agent` (TG-адаптер: статус-сообщение/watchdog/audit/финал) и `run_for_glasses` (HUD-адаптер, ≤300 chars). `running[k]=True` резервируется СИНХРОННо в адаптере до первого await; `run_engine` заменяет на реальный client. Новые триггеры (web-чат C1, авто-запуск карточки F1) подключаются как потребители `run_engine`, НЕ дублируют SDK-цикл.
- `.env` — секреты (BOT_TOKEN, GROUP_CHAT_ID, ALLOWED_USERS). НЕ в git.
- `data/topics.json` — **СЛОЙ 1**: привязка `"chat:thread" → {project,cwd,model}`. Вечная, `/reset` не трогает.
- `data/sessions.json` — **СЛОЙ 2**: `"chat:thread" → session_id`. Чистит только `/reset`.
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
- **Privacy Mode.** Бот — админ группы → получает все сообщения независимо от Privacy Mode (включая пересланные от пользователя). Если перестанет видеть — выключить Privacy у @BotFather.
- **Один getUpdates.** Не запускать второй инстанс (nohup + systemd одновременно) — конфликт long-polling.
- **bypassPermissions + full-auto.** Бот сам пушит/деплоит/удаляет. Необратимое репортит постфактум (footer ⚠️). Доступ строго `ALLOWED_USERS={282311426}`.
- **HTML parse_mode + сырой ответ модели = краш.** Ответ модели часто содержит `<title>`/`<div>` и пр. → Telegram `BadRequest: unsupported start tag`. Ответ ВСЕГДА гнать через `md_to_html()` (escape → лёгкий markdown), а `send()` имеет fallback на plain при BadRequest. Не слать сырой текст с `parse_mode=HTML`.
- **Транзиентные сбои TG = пропажа ответа (fix 2026-05-29).** На длинных задачах финальная отправка ловит `NetworkError`/`Bad Gateway`/`RetryAfter` (не `BadRequest`!). Раньше `send()` их не перехватывал + статус удалялся ДО отправки → пропадали и прогресс, и ответ. Теперь: `_tg_call()` ретраит транзиентные (RetryAfter→sleep, Network/TimedOut→expo backoff, BadRequest НЕ ретраит — он логический); `send()`/`report_error()` идут через него; в `run_agent` финал — СНАЧАЛА `send(ответ)`, ПОТОМ `delete(status)`. Порядок не менять: при сбое отправки на экране должен остаться прогресс, а не пустота.
- **Cost убран из футера** — на подписке $ это шум. Осталась только команда `/cost` по запросу.
- **Гонка конкурентности.** Резерв слота `running[k]=True` ставится СИНХРОННО в `on_message` до первого `await` (не в `run_agent` — иначе два быстрых сообщения проскакивают оба и затирают session_id / два full-auto процесса правят файлы разом). `safe_run` снимает резерв в `finally`. Проверено userbot'ом (два сообщения 0.4с → второе «уже работаю»).
- **Детектор «необратимого» — точные подстроки.** НЕ использовать `-f `/`rm `/`kill ` (ловят `tail -f`, `perform`, и т.п.). Только `rm -rf`/`rm -f`/`git push`/`--force` и пр.
- **Тест-харнес userbot.** Слать боту можно только от аккаунта Игоря (282311426). pyrogram 2.0.106 НЕ резолвит большой id группы из пустого in-memory кэша → греть `get_chat(invite_link)` перед send; в топик слать `reply_to_message_id=<thread_id>` (нет `message_thread_id`). Сессия — `networking-os/secrets/tg.session`.
- **nudge держать ТОНКИМ (принцип 2026-05-29).** `TELEGRAM_NUDGE` = только TG-специфика (нет кнопок→спроси текстом; краткая проза без дампа кода; `tg-reply`). Всё про «как работать» (scan/хирургичность/права/необратимое) — в CLAUDE.md (project + `~/CLAUDE.md`), агент грузит их через `setting_sources=["user","project"]` — те же файлы, что и терминал. НЕ дублировать правила в nudge: лишний контекст каждый ход = агент тупее, плюс «два хозяина». Читаемость/формат — задача рендера бота, а НЕ инструкций агенту.
- **`md_to_html` рендерит и СВОРАЧИВАЕТ код (2026-05-29).** Поддержка: `#`заголовки→жирный, `-*+`→`•`, ` ```блоки``` `→`<pre>`, инлайн `код`, `[ссылки](url)`, *курсив*, **жирный**. Блок кода >`CODE_MAX_LINES`(20) сворачивается в превью(10) + маркер «‹N строк свёрнуто›» — намеренно (в TG-ответе важна суть, не простыни; правки видны в файлах). Стратегия: код/ссылки в плейсхолдеры `\x00P#\x00` ДО `html.escape`, потом markdown, потом возврат. Не трогать порядок шагов.
- **Нет интерактива в TG.** `AskUserQuestion` в `DISALLOWED_TOOLS` (голосовалка некуда показать → агент зависал/решал сам). `system_prompt` = preset `claude_code` + `append` TELEGRAM_NUDGE: агент спрашивает текстом, Игорь отвечает следующим сообщением (resume продолжит). Не передавать system_prompt строкой — затрёт личность Claude Code; только `{type:preset,preset:claude_code,append:...}`.

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
- **Фаза 1 (готово):** read-only — список проектов + git-health, табы Обзор / README (`/api/projects/{id}/readme`, перебор имён файла) / CLAUDE.md / Specs (из vault) / Активность (audit-лог).
- **Доска (готово 2026-05-29, Spec=Kanban=2 файла):** таб «Доска» рендерит `TASKS.md` (в КОРНЕ репо проекта, не vault) как канбан. Секции `## Backlog / In Progress / Review / Failed` = колонки; строка `- [x] текст <!--ops:ID-->` = карточка (статус-символ ` ~ ? !`, `ops:ID` — стабильный якорь, дописывается автоматически при первом GET). **Истина = markdown, БД для плана НЕТ.** Завершённое уходит в `DONE.md` (append-only, дата; **сессии его НЕ читают** — гигиена контекста). Преамбула файла (до первой колонки) сохраняется; прочий текст внутри секций канонизируется при перезаписи. API: `GET/POST /api/projects/{id}/tasks`, `POST .../tasks/{card}/move {to}` (`to`=колонка или `done`→архив), `DELETE .../tasks/{card}`, `GET .../tasks/done`, `GET .../tasks/{card}/run` (сайдкар результата). UI: кнопки ←→ (перенос), ✓ (в Done), ✕ (удалить), 📄 (результат в Review/Failed); добавление — в колонке Backlog.
- **Авто-запуск карточки (F1, done 2026-05-29):** перенос карточки в **In Progress** запускает `run_engine` в проекте под session_key TG-топика → делит `running`-замок и сессию с TG (TG и карточка взаимоисключаются, гонка по cwd закрыта). Занят → 409 «проект занят». По завершении `_run_card` (фоновая задача в webapp.py) пишет результат+git-diff в `data/runs/<card_id>.md`, переносит карточку в **Review** (ok) или **Failed** (err), пингует TG-топик. Замок снимается в `finally`. Фронт поллит доску пока in_progress непуст. **Review = видимость, НЕ откат** (гейт Применить/Отмена с worktree = трек C2, позже). Прогресс пока поллингом (live-стрим = C1).
- **Чат по проекту (C1, done 2026-05-29):** таб «Чат» в кокпите. `POST /api/projects/{id}/chat` → `text/event-stream` (web.StreamResponse), потребляет `run_engine`, стримит tool/text вживую. Сессия и `running`-замок **общие с TG/F1** (session_key = tg_thread) — разговор продолжается сквозь каналы (начал в телеге → продолжил в браузере). Disconnect (закрыл вкладку) → `client_gone`, генератор дотягивает в фоне, session_id сохраняется, замок в `finally`. Занят → SSE-error «проект занят». Фронт `ChatTab.tsx`: ReadableStream reader (chunk-boundary-safe парсинг `data:`), markdown-рендер. ⚠️ «Стоп» обрывает только клиентский fetch — движок на сервере дотягивает (замок держится до конца задачи).
- **Статус фаз:** F0 ✅ · F1 (авто-запуск карточки) ✅ · C1 (чат SSE) ✅. Дальше по roadmap: O1 Timeline, C2 гейт Применить/Отмена, автономный контур (сканеры/доктор/self-heal).
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

# Claude-Ops

**Claude Code без терминала.** Один движок (Claude Agent SDK), три входа: Telegram, браузерный кокпит, канбан-карточки. Full-auto: написал задачу → Claude сам диагностирует, правит код, деплоит, отчитывается. Проект живёт на docker-core (VM 100), systemd-сервис.

```
 Telegram  ──┐
 Браузер   ──┼──→  run_engine()  ──→  Claude Agent SDK  ──→  файлы/git/deploy
 Карточка  ──┘     (async generator)     (подписка)           (full-auto)
```

---

## Что работает сегодня

### Telegram-бот (@ziraclaudebot)
- **Forum-топики = проекты.** Каждый топик привязан к папке (`cwd`), агент работает в ней.
- Команды: `/reset` `/resume` `/model` `/project` `/newtopic` `/diff` `/cost` `/usage` `/stop` `/whoami`
- Приём файлов (документы + фото до 20MB) — агент видит и обрабатывает.
- Watchdog: зависшая задача авто-прерывается (5 мин тишины или 30 мин общих).
- Audit-лог: `data/audit/audit-YYYY-MM.log` — след каждой задачи на full-auto.
- Надёжная отправка: ретрай транзиентных сбоёв TG, ответ до удаления статуса.
- `md_to_html` рендер: заголовки, списки, код-блоки (длинный код сворачивается).
- `tg-reply <путь>` — агент шлёт файл/скриншот обратно в топик.

### Браузерный кокпит (claude-ops.coscore.us)
- **Парольная фраза**, cookie-сессия, SPA на React+Vite.
- **Сайдбар** с проектами: DnD-сортировка, collapse в иконки, unread-бейджи.
- **Вкладки-проекты** сверху (как браузер): переключение без потери состояния.

**Табы по проекту (левая панель ~55%):**

| Таб | Что делает |
|---|---|
| **Обзор** | Git-статус, health-карточка (6 пунктов), кнопка «↑ Sync» (commit+push), запуск тестов |
| **CLAUDE.md** | Просмотр + inline-редактирование (двойной клик) |
| **Логи** | `log_cmd` из topics.json (journalctl/docker logs/etc.) |
| **Доска** | Канбан из `TASKS.md` — Backlog / In Progress / Review / Failed |
| **Файлы** | Read-only дерево проекта + просмотр (MD рендер, код моно) |
| **Память** | Memory-файлы агента (`~/.claude/projects/<cwd>/memory/`) |

**Чат (правая панель ~45%, всегда видна):**
- SSE-стрим из `run_engine`, markdown-рендер, CLI-рендер инструментов (Bash/Edit/Read).
- **Сессия общая с Telegram** — начал в TG, продолжил в браузере (и наоборот).
- Модель: sonnet / opus / haiku, переключается на лету.
- Статистика: `N сообщ · ~K токенов`, предупреждение при >100K.
- Pulse-индикатор: тикающий таймер + конкретика последнего инструмента.
- Очередь: Enter во время задачи → в очередь, автоотправка после.
- История из SDK-транскрипта, выбор/сброс сессий.
- Ресайз/сворачивание (drag, localStorage).
- Библиотека промтов (📋): категории, CRUD, переменные `[ПОДСТАВЬ]`.
- Кнопка «Стоп» → `client.interrupt()` (реально прерывает).

**Канбан-доска (TASKS.md → кокпит):**
- Секции `## Backlog / In Progress / Review / Failed` = колонки.
- Перенос: кнопки ←→, DnD между колонками, ✓ в Done, ✕ удалить.
- **Авто-запуск (F1):** перенос в In Progress → `run_engine` в проекте → результат + git-diff → Review/Failed → пинг в TG.
- Inline-редактирование карточек (двойной клик).
- Защита от стирания: `_PLAIN_CARD_RE` + safety guard + asyncio.Lock.

**Ещё:**
- **Свободные чаты** (кнопка «+»): без проекта, `cwd=$HOME`, именуемые.
- **Split-view**: два чата рядом.
- **Глобальный файл-браузер** (📁): дерево от `$HOME`, inline-редактирование.
- **Вложения**: 📎 / drag-drop / Ctrl+V → `data/inbox/`.
- **Usage badge**: лимиты подписки (5ч + неделя), авто-обновление.
- **+ Новый проект**: шаблоны → папка → онбординг-карточка (агент спрашивает о проекте).
- **Аудит проекта** (🩺): агент проверяет по `audit-prompt.md`, создаёт карточки.
- **Подтянуть до стандарта** (🔧): дополняет CLAUDE.md/TASKS.md/README по шаблонам.
- **Health-check**: 6 пунктов структуры (green/yellow/red).
- **Rename проекта**: kebab-case, `shutil.move` + topics.json.

### Движок (run_engine)
- Async-генератор событий `{tool|text|result|rate_limit|error}`.
- Транспорт-независимый: TG, браузер-чат, карточка — потребители.
- Общий `running`-замок per-проект (гонка по cwd закрыта).
- Сессии общие между каналами (`session_key = tg_thread`).

---

## Архитектура

**Один systemd-процесс**, внутри:
- **PTB** (python-telegram-bot) — long-polling TG
- **aiohttp** — кокпит (webapp.py) + (опц.) HTTP для очков G2

```
claude-ops-bot.service
  └─ bot.py (1174 строк)          — TG-хендлеры, движок run_engine, реестр проектов
      ├─ webapp.py (3707 строк)    — aiohttp-кокпит, все API-эндпоинты, шина событий
      │   └─ web/dist/             — React+Vite SPA (билд)
      ├─ data/topics.json          — привязка топик→проект (вечная)
      ├─ data/sessions.json        — session_id (чистит /reset)
      ├─ data/prompts.json         — библиотека промтов
      ├─ data/audit/               — audit-лог full-auto
      ├─ data/runs/                — результаты авто-запуска карточек
      └─ data/inbox/               — загруженные файлы
```

**Фронтенд** (`web/src/`): 26 компонентов — App, Sidebar, ProjectView, ChatTab, BoardTab, FilesTab, LogsTab, OverviewTab, MemoryTab, ClaudeMdTab, GlobalFilesTab, PromptPicker, UsageBadge и др.

**Шаблоны** (`templates/`): стартеры для новых проектов + reference-копии из vault.

**Тесты** (`tests/`): pytest, 62 passed. Покрыто: парсер доски, path-traversal, slug-валидация, health, auth.

---

## Доступ

| Канал | Адрес |
|---|---|
| **Браузер** | `https://claude-ops.coscore.us` (proxmox-tunnel) или `192.168.0.114:8787` (LAN) |
| **Telegram** | Forum-группа «Development», бот @ziraclaudebot |

- **Auth TG:** `ALLOWED_USERS={282311426}` (только Игорь).
- **Auth Web:** `WEB_PASSWORD` в `.env` (пароль в Credentials.md, не здесь).
- **Auth SDK:** подписка (`~/.claude/.credentials.json`), НЕ `ANTHROPIC_API_KEY`.

---

## Операции

```bash
# Логи
sudo journalctl -u claude-ops-bot -f

# Рестарт (после правки bot.py / webapp.py)
bash /home/igor/claude-ops-bot/restart-self.sh   # если из бота
sudo systemctl restart claude-ops-bot             # если из терминала

# Фронт (после правки web/)
cd /home/igor/claude-ops-bot/web && npm run build

# Тесты
cd /home/igor/claude-ops-bot && venv/bin/python -m pytest -q
```

---

## Состояние (два слоя)

- **Слой 1** — `data/topics.json`: привязка `"chat:thread" → {project, cwd, model}`. Вечная.
- **Слой 2** — `data/sessions.json`: `"chat:thread" → session_id`. Чистит `/reset`.

Переключение проекта → Слой 1. Сброс контекста → Слой 2. Топик помнит проект после `/reset`.

---

## Документация

| Файл | Назначение |
|---|---|
| `CLAUDE.md` | **Истина по эксплуатации:** gotchas, операции, состояние фич |
| `TASKS.md` | Живая доска (канбан) — бэклог и текущие задачи |
| `DONE.md` | Архив завершённого (72+ карточки). **Сессии НЕ читают.** |
| `specs/roadmap.md` | Последовательность: милстоуны M1–M5, зависимости треков |
| `specs/spec.md` | Spec v1 (TG-бот) |
| `specs/spec-002-*.md` | Spec v2 (Control Center) |
| `specs/spec-003-*.md` | Копилка автономии (30 блоков, сырая) |
| `specs/vision-strategy.md` | Стратегия/монетизация — **заморожено до M3** |

---

## Технологии

Python 3.11 · python-telegram-bot (PTB) · aiohttp · Claude Agent SDK (подписка) · React 18 + Vite + TypeScript · systemd · Cloudflare Tunnel

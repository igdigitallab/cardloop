# Claude-Ops

**Полноценная IDE-среда для управления проектами через Claude Agent SDK** — без терминала, с любого устройства. Один движок, три канала ввода: браузерный кокпит, Telegram, канбан-карточки. Full-auto: сформулировал задачу → агент сам диагностирует, правит код, деплоит, отчитывается.

```
 Кокпит    ──┐
 Telegram  ──┼──→  run_engine()  ──→  Claude Agent SDK  ──→  файлы / git / deploy
 Карточка  ──┘     (async generator)     (подписка)           (full-auto)
```

---

## Кокпит (claude-ops.coscore.us)

Браузерная среда разработки — SPA на React + Vite, бэкенд aiohttp.

**Сайдбар:** проекты с DnD-сортировкой, collapse, unread-бейджи. **Вкладки-проекты** сверху (как браузер) — переключение без потери состояния.

**Табы по проекту (левая панель ~55%):**

| Таб | Что делает |
|---|---|
| **Обзор** | Git-статус, health-карточка (6 пунктов), кнопка «↑ Sync» (commit+push), запуск тестов |
| **CLAUDE.md** | Просмотр + inline-редактирование (двойной клик) |
| **Логи** | Настраиваемая команда логов (`log_cmd` в topics.json) |
| **Доска** | Канбан из `TASKS.md` — Backlog / In Progress / Review / Failed |
| **Файлы** | Дерево проекта + просмотр (MD рендер, код моно) |
| **Память** | Memory-файлы агента |

**Чат (правая панель ~45%, постоянная):**
- SSE-стрим, CLI-рендер инструментов (Bash/Edit/Read/Write с diff).
- **Сессия сквозная** — начал в Telegram, продолжил в браузере (и наоборот).
- Модель (sonnet/opus/haiku) переключается на лету.
- Очередь сообщений, pulse-индикатор, статистика токенов.
- Библиотека промтов с категориями и переменными.
- Кнопка «Стоп» реально прерывает агента (`client.interrupt`).
- Выбор и управление сессиями.

**Канбан-доска:**
- `TASKS.md` в репо = источник истины. Секции = колонки, строки = карточки.
- Перенос кнопками, DnD, inline-редактирование.
- **Авто-запуск:** перенос карточки в In Progress → движок выполняет задачу → результат + git-diff → Review / Failed → уведомление в Telegram.
- Защита от потери данных (три слоя).

**Дополнительно:**
- Свободные чаты (без привязки к проекту), Split-view.
- Глобальный файл-браузер (`$HOME`) с inline-редактированием.
- Вложения: 📎, drag-drop, Ctrl+V.
- Usage badge: лимиты подписки (5ч + неделя).
- Создание проекта (шаблоны + онбординг-агент), аудит, upgrade, health-check, rename.

---

## Telegram-канал

Forum-группа «Development», @ziraclaudebot. **Каждый топик = проект** (привязка `thread_id → cwd`).

- Написал задачу → агент работает в папке проекта.
- Переслал алерт / скрин → агент диагностирует и чинит.
- Файлы (до 20MB): документы и фото обрабатываются агентом.
- Команды: `/reset` `/resume` `/model` `/project` `/newtopic` `/diff` `/cost` `/usage` `/stop` `/whoami`

---

## Движок

`run_engine()` — async-генератор событий `{tool|text|result|rate_limit|error}`.

- **Транспорт-независимый:** кокпит, Telegram, карточка — потребители одного потока.
- **Общий замок** per-проект (гонка по cwd закрыта).
- **Сквозные сессии** между каналами.
- **Watchdog:** 5 мин тишины или 30 мин общих → авто-прерывание.
- **Audit-лог:** каждая задача, каждая команда — `data/audit/`.

---

## Архитектура

Один systemd-процесс: PTB (Telegram long-polling) + aiohttp (кокпит).

```
claude-ops-bot.service
  └─ bot.py             — Telegram-хендлеры, движок run_engine, реестр проектов
      ├─ webapp.py       — aiohttp-кокпит, API, шина событий
      │   └─ web/dist/   — React+Vite SPA
      ├─ data/
      │   ├─ topics.json     — привязка канал→проект (вечная)
      │   ├─ sessions.json   — session_id (чистит /reset)
      │   ├─ prompts.json    — библиотека промтов
      │   ├─ audit/          — audit-лог
      │   ├─ runs/           — результаты авто-запуска карточек
      │   └─ inbox/          — загруженные файлы
      ├─ templates/          — шаблоны новых проектов + reference
      └─ tests/              — pytest (62 passed)
```

---

## Доступ

| Канал | Адрес |
|---|---|
| **Кокпит** | `https://claude-ops.coscore.us` (Cloudflare Tunnel) / `192.168.0.114:8787` (LAN) |
| **Telegram** | Forum-группа «Development», @ziraclaudebot |

- **Auth кокпит:** `WEB_PASSWORD` в `.env` (пароль в Credentials.md).
- **Auth Telegram:** `ALLOWED_USERS` (whitelist по user ID).
- **Auth SDK:** подписка (`~/.claude/.credentials.json`), **НЕ `ANTHROPIC_API_KEY`**.

---

## Операции

```bash
# Логи
sudo journalctl -u claude-ops-bot -f

# Рестарт
bash /home/igor/claude-ops-bot/restart-self.sh   # из агента (безопасный)
sudo systemctl restart claude-ops-bot             # из терминала

# Фронтенд
cd /home/igor/claude-ops-bot/web && npm run build

# Тесты
cd /home/igor/claude-ops-bot && venv/bin/python -m pytest -q
```

---

## Документация

| Файл | Назначение |
|---|---|
| `CLAUDE.md` | **Истина по эксплуатации:** gotchas, операции, состояние фич |
| `TASKS.md` | Живая доска (канбан) — бэклог и текущие задачи |
| `DONE.md` | Архив завершённого (72+ карточки). Сессии НЕ читают. |
| `specs/roadmap.md` | Милстоуны M1–M5, зависимости треков |
| `specs/spec-002-*.md` | Спецификация кокпита |
| `specs/spec-003-*.md` | Спецификация автономного контура |
| `specs/vision-strategy.md` | Стратегия — **заморожено до M3** |

---

## Технологии

Python 3.11 · aiohttp · python-telegram-bot · Claude Agent SDK · React 18 · Vite · TypeScript · systemd · Cloudflare Tunnel · pytest

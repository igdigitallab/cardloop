> README = что это и как запустить. Карта кода → ARCHITECTURE.md. Правила работы и gotchas → CLAUDE.md. API → docs/API.md. Вклад и настройка → CONTRIBUTING.md.

# Claude-Ops

**IDE-среда для управления проектами через Claude Agent SDK** — без терминала, с любого устройства. Один движок, три канала ввода: браузерный кокпит, Telegram, канбан-карточки. Full-auto: сформулировал задачу → агент диагностирует, правит код, деплоит, отчитывается.

```
 Кокпит    ──┐
 Telegram  ──┼──→  run_engine()  ──→  Claude Agent SDK  ──→  файлы / git / deploy
 Карточка  ──┘     (async generator)     (подписка)           (full-auto)
```

---

## Три канала

### Кокпит (YOUR_DOMAIN)

Браузерная IDE — SPA на React + Vite, бэкенд aiohttp.

**Сайдбар:** проекты с DnD-сортировкой, collapse, unread-бейджи. **Вкладки-проекты** сверху — переключение без потери состояния.

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
- Свободные чаты (без привязки к проекту).
- Глобальный файл-браузер (`$HOME`) с inline-редактированием.
- Вложения: 📎, drag-drop, Ctrl+V.
- Usage badge: лимиты подписки (5ч + неделя).
- Создание проекта (шаблоны + онбординг-агент), аудит, upgrade, health-check, rename.

### Telegram-канал

Forum-группа «Development», @YOUR_BOT. **Каждый топик = проект** (привязка `thread_id → cwd`).

- Написал задачу → агент работает в папке проекта.
- Переслал алерт / скрин → агент диагностирует и чинит.
- Файлы (до 20MB): документы и фото обрабатываются агентом.
- Команды: `/reset` `/resume` `/model` `/project` `/newtopic` `/diff` `/cost` `/usage` `/stop` `/whoami`

### Канбан-автозапуск

Перенос карточки в In Progress → `_run_card` в webapp.py запускает `run_engine` → результат в `data/runs/<card>.md` → карточка переходит в Review/Failed → пинг в TG-топик.

---

## Авторизация: важное предупреждение

> **Claude-Ops использует подписочную авторизацию, а не API-ключ.**

Движок читает `~/.claude/.credentials.json` (claudeAiOauth, выдаётся при входе через `claude login`).

**Не задавай `ANTHROPIC_API_KEY`** ни в `.env`, ни в окружении — `bot.py` явно удаляет эту переменную при старте. Если она будет присутствовать, SDK переключится на API pay-per-token биллинг вместо подписки.

**Доступ строго по `ALLOWED_USERS`** — только перечисленные Telegram user ID могут взаимодействовать с ботом и кокпитом.

---

## Быстрый старт

```bash
# 1. Clone
git clone https://github.com/YOUR_GITHUB/claude-ops-bot.git && cd claude-ops-bot

# 2. Python
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt

# 3. Config
cp .env.example .env   # Edit: BOT_TOKEN, GROUP_CHAT_ID, ALLOWED_USERS, WEB_PASSWORD, WEB_COOKIE_SALT

# 4. Frontend
cd web && npm install && npm run build && cd ..

# 5. Run
venv/bin/python bot.py  # Cockpit → http://localhost:8787
```

Детали (тесты, lint, deploy) → [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Доступ

| Канал | Адрес |
|---|---|
| **Кокпит** | `https://YOUR_DOMAIN` (Cloudflare Tunnel / reverse proxy) / `localhost:8787` (LAN) |
| **Telegram** | Forum-группа «Development», @YOUR_BOT |

- **Auth кокпит:** `WEB_PASSWORD` в `.env`.
- **Auth Telegram:** `ALLOWED_USERS` (whitelist по user ID).
- **Auth SDK:** подписка (`~/.claude/.credentials.json`), **НЕ `ANTHROPIC_API_KEY`**.

---

## Операции

```bash
# Логи
sudo journalctl -u claude-ops-bot -f

# Рестарт из агента (ЕДИНСТВЕННЫЙ безопасный способ)
bash $HOME/claude-ops-bot/restart-self.sh

# Рестарт из терминала
sudo systemctl restart claude-ops-bot

# Фронтенд (после правки web/)
cd $HOME/claude-ops-bot/web && npm run build

# Тесты
cd $HOME/claude-ops-bot && venv/bin/python -m pytest -q
```

---

## Документация

| Файл | Назначение |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Карта кода: где что искать, схема потока |
| [CLAUDE.md](CLAUDE.md) | Правила работы и gotchas для агентов |
| [docs/API.md](docs/API.md) | HTTP API reference (56 роутов) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Вклад: setup, тесты, lint, commit-стиль |
| `TASKS.md` | Живая доска (канбан) — бэклог и текущие задачи |
| `DONE.md` | Архив завершённого. Сессии НЕ читают. |

---

## Технологии

Python 3.11 · aiohttp · python-telegram-bot · Claude Agent SDK · React 18 · Vite · TypeScript · systemd · Cloudflare Tunnel · pytest

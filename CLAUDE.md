> CLAUDE.md = правила работы и gotchas для агентов. Карта кода → ARCHITECTURE.md. API → docs/API.md. Запуск → CONTRIBUTING.md. Subsystem gotchas → GOTCHAS.md.

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
- **Тесты: `venv/bin/python -m pytest tests/`** (≈950, должно быть green). ⚠️ ТОЛЬКО через venv — в нём `pytest-aiohttp` (requirements-dev.txt); системный `python` его НЕ имеет → ~237 endpoint-тестов падают в ложный `error`. Такому прогону не верить и тесты под него НЕ переписывать.

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
- **`claude-agent-sdk` >= 0.2.96 обязателен для fable (spec-017).** Старый SDK (<=0.2.87) НЕ знает модель `fable`/`claude-fable-5` и МОЛЧА подменяет её на opus (без ошибки, `is_error=False`) — дирижёр тихо деградирует. CLI при этом алиас знает — обманчиво. После пересоздания venv: `pip install -U "claude-agent-sdk>=0.2.96"`. Симптом: сессия отвечает «issue with the selected model» или представляется Opus.

Subsystem gotchas (Telegram/рендер, конкурентность, безопасность/детекторы, C2-gate/worktree, память, секреты, прочее, audit, привязка проектов, шаблоны) → **GOTCHAS.md**.

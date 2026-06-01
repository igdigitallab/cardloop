# Changelog

Все заметные изменения Claude-Ops. Формат — обратный хронологический.
Версии — semver-подобно (0.x пока проект в активной разработке).

> Дисциплина: при появлении новой функции — добавить строку сюда + отметить карточку в TASKS.md → DONE.md. Тег ставится на стабильную точку (`git tag vX.Y.Z`).

## [Unreleased]
_(текущая работа)_

## [v0.6.0] — 2026-05-31
Шаг 3 roadmap: наблюдаемость — Timeline (Spec 008). Шина событий теперь персистируется; кокпит получает вкладку «🕒 Лента».

### Добавлено
- **Timeline persistence** (Spec 008): `_bus_publish` теперь вызывает `_timeline_append` — единая точка записи. Каждое событие пишется в `data/timeline/<slug>.jsonl` (append-only, slug = `cwd.replace('/', '-')`). Ротация: >5MB → `.jsonl.1` (одна копия). Запись глотает исключения, env-поле никогда не пишется. `_timeline_init(ctx)` вызывается из `start()`.
- **`GET /api/projects/{id}/timeline?limit=N&before=<ts>`** — эндпоинт истории: читает JSONL (текущий + .1), парсит gracefully (битые строки → skip), возвращает массив в хронологическом порядке. Пагинация по `before=<ts>` (Unix float). Auth-protected, anti-traversal через `_find_project_by_id`.
- **TimelineTab** (`web/src/tabs/TimelineTab.tsx`): история из `GET /timeline` + live-события через `useProjectActivity` (переиспользует существующий SSE-коннект, новый сокет не открывается). Кнопка «Загрузить ранее» с `before=<oldest_ts>`. Иконки по kind (▶/✅/❌/🔧/💬), live-badge с пульсацией на 4с, ARIA (`role=log`, `aria-live=polite`). CSS: `styles/timeline.css`.
- **32 новых теста** (`tests/test_timeline.py`): slug стабильность, path резолв, append+ts+truncate+env-exclusion, ротация 5MB, bus_publish интеграция, graceful broken JSONL, backup read, API GET/limit/before/env-not-in-response. **453 passed** (было 421).

## [v0.5.0] — 2026-05-31
Шаг 2 roadmap: изолированное хранилище ключей проекта (OSS-механизм; Vault Игоря не трогаем).

### Добавлено
- **Хранилище ключей проекта** (Spec 007): `.claude-ops/secrets/secrets.env` — `chmod 600`, gitignored автоматически при первой записи. Секреты подмешиваются в `env` агента при каждом запуске (`run_engine`, `run_agent`, `_run_card`, `api_project_chat`). Изоляция по cwd. **Значения НИКОГДА не возвращаются через API** — только список имён ключей. CRUD через кокпит: вкладка «🔑 Ключи» (SecretsTab) с добавлением (password-input), списком (маска ••••••) и удалением (ConfirmModal). 47 новых тестов (421 passed). +3 эндпоинта: `GET/POST/DELETE /api/projects/{id}/secrets/{key}`.

## [v0.4.0] — 2026-05-31
Шаг 1 roadmap «полноценный сервис разработки»: накапливаемая память проекта.

### Добавлено
- **Память проекта** (spec-006): переехала в репо проекта (`.claude-ops/memory/` — коммитится в git, путешествует с проектом, OSS-friendly). POST/DELETE эндпоинты для CRUD из кокпита. MemoryTab стал редактируемым (создание/редактирование/удаление записей). Агент пишет память сам через обычный Write (nudge + раздел в шаблоне CLAUDE.md). `MEMORY.md` — авто-индекс. Типы записей: decision / gotcha / rejected / convention. 49 новых тестов (374 passed).

## [v0.3.0] — 2026-05-31
Стабильная точка после большого цикла рефакторинга, чистки и C2.

### Добавлено
- **C2-gate** — гейт «Применить / Отмена» + worktree-per-task: карточка в git-проекте прогоняется в изолированном `git worktree`, в Review кнопки ✓/✗, merge --no-ff или откат. Безопасный откат = фундамент для будущей автономии.
- `ARCHITECTURE.md` — карта кода для новых разработчиков/агентов.
- OSS-каркас: `LICENSE` (MIT), `CONTRIBUTING.md`, `docs/API.md` (56 роутов).
- ESLint + Prettier (`npm run lint` / `format`), i18n-словарь (`web/src/i18n/ru.ts`).
- Тесты: 207 → 325 (доска, чат, rename, конкурентность, security, C2).

### Изменено / Очищено
- Полностью удалён Glasses/G2-транспорт (не актуально).
- Документация переписана в иерархию без дублей (README / ARCHITECTURE / CLAUDE.md / CONTRIBUTING).
- CLAUDE.md очищен от ledger-истории → только forward-правила + gotchas.
- `styles.css` (3000+ строк) разбит на 10 partials.
- Backend: убраны хардкоды `/home/igor`, command-injection в log_cmd/test_cmd, path-traversal в card_id, auth → scrypt + secure cookie + rate-limit.
- systemd-юнит: добавлен `EnvironmentFile=` (фикс — `.env` не грузился).

## [v0.2.x] — до 2026-05-31
Кокпит (табы, чат SSE, доска-канбан с автозапуском, файлы, промты), сквозные сессии кокпит↔TG, движок `run_engine`, тесты-каркас. (История — в git log.)

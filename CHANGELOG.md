# Changelog

Все заметные изменения Claude-Ops. Формат — обратный хронологический.
Версии — semver-подобно (0.x пока проект в активной разработке).

> Дисциплина: при появлении новой функции — добавить строку сюда + отметить карточку в TASKS.md → DONE.md. Тег ставится на стабильную точку (`git tag vX.Y.Z`).

## [Unreleased]

### Исправлено
- **Rename проекта терял всю историю диалогов и Timeline.** `api_project_rename` двигал папку (`shutil.move`) и обновлял `topics.json`, но SDK-история (`~/.claude/projects/<slug>/`) и Timeline (`data/timeline/<slug>.jsonl`) ключуются по `slug = cwd.replace('/','-')` — после смены cwd кокпит читал пустой новый slug, и «пропадали все сессии общения» (файлы при этом целы под старым slug). Добавлен `_migrate_cwd_keyed_state(old_cwd, new_cwd, ctx)`: переносит SDK-каталог сессий + Timeline (+`.jsonl.1`) на новый slug, best-effort, предупреждения в `warnings` ответа. Тесты `test_rename_migrates_sdk_sessions`, `test_rename_migrates_timeline`. Уже потерянные проекты (`family-emergency`, `autotopic-test`) восстановлены переносом осиротевших каталогов.

## [v0.8.1] — 2026-06-01
### Исправлено
- **Память: 404 при удалении legacy-записи** (баг v0.4.0). `_memory_read_all` читал старое место (`~/.claude/projects/<cwd>/memory/`) как fallback, но `_memory_delete`/write работали только с новым (`.claude-ops/memory/`) → удаление legacy-записи давало 404. Теперь при первом чтении legacy-память **авто-мигрируется** в новое место (для всех проектов разом), удаление/запись начинают работать. Тест `test_memory_read_all_migrates_legacy`.

## [v0.8.0] — 2026-05-31
Шаг 5 roadmap (вершина): Самолечение (Spec 010). Агент-чинильщик в worktree + гейт + одобрение. **Roadmap «полноценный сервис разработки» завершён** (5/5 шагов).

### Добавлено
- **Самолечение** (Spec 010): `_self_heal_enabled(project)` — флаг per-project (`self_heal`) или env `SELF_HEAL_ENABLED`. **OFF по умолчанию — НИКОГДА не включён ни для одного проекта.**
- **`_self_heal_card(ctx, project, incident_card)`** — петля починки: пометить `heal_attempted=true` ДО запуска (предохранитель зацикливания), сформировать промпт чинильщику, запустить через существующий C2-путь (`_card_worktree_setup` + `_run_card`), прогнать `_run_quality_gate`, перенести в Review (safe) или Failed (risky), пинг Игорю в TG.
- **Интеграция в `_error_scanner_loop`**: после `_scan_and_ingest` при `self_heal=True` и новых инцидентах → `asyncio.create_task(_self_heal_card(...))`. Лимиты: счётчик активных починок ≤2, running lock, heal_attempted.
- **Timeline `kind:"self_heal"`**: фазы `start / fixed / gate_ok / gate_fail / gate_unknown / skipped` публикуются в шину.
- **`POST /api/projects/{id}/self-heal {enabled}`** — тумблер включения per-project. Auth. Не включает ни одного проекта по умолчанию.
- **UI: тумблер «🔧 Самолечение»** в OverviewTab + подпись «Ничего не применяется без тебя». CSS-бейдж `🔧 авто-починка · гейт ✓/✗` на карточках BoardTab.
- **28 новых тестов** (`tests/test_self_healing.py`): `_self_heal_enabled` (флаг/env/default); `heal_attempted` мета; OFF default = критичный регрессия-страж; heal_attempted ставится ДО прогона; safe→Review, risky→Failed; heal_attempted инцидент не перезапускается; не-git→пропуск; занятый→пропуск; лимит конкурентности; Timeline получает self_heal; API-тумблер (auth, enable, disable, 404). **496 passed** (было 468).

### Предохранители (незыблемо)
1. OFF по умолчанию — `self_heal` в topics или `SELF_HEAL_ENABLED` env
2. НИКОГДА не auto-apply — агент доходит только до Review; merge руками
3. Лимит 1 попытка/инцидент — `heal_attempted=true` ДО запуска
4. Лимит конкурентности — max 2 авто-починки одновременно
5. Только git+clean — не-git/dirty пропускаются
6. Всё видно — Timeline kind:"self_heal" + TG-пинг

## [v0.7.0] — 2026-05-31
Шаг 4 roadmap: авто-гейт качества (Spec 009). C2-«Применить» теперь не вслепую: можно прогнать тесты в worktree карточки и получить вердикт перед merge.

### Добавлено
- **Quality gate** (Spec 009): `_run_quality_gate(wt_path, env)` — прогон тестов в worktree-карточки через `_detect_test_cmd` (переиспользование). Таймаут 300с, вывод обрезается до 20k. Вердикт: `safe` (rc=0) / `risky` (rc≠0 или таймаут) / `unknown` (нет тест-конфига).
- **`POST /api/projects/{id}/tasks/{card}/check`** — эндпоинт гейта: читает meta, прогоняет `_run_quality_gate(wt_path)` с секретами проекта, возвращает вердикт, записывает `meta.gate={verdict,ts}` в JSON-сайдкар, публикует `{kind:"gate", verdict}` в Timeline. Legacy/нет worktree → `{verdict:"unknown", reason:"legacy"}`. 400 bad card_id; 404 нет проекта или нет worktree на диске.
- **UI: кнопка «🧪 Проверить»** в модалке результата карточки (worktree-режим, рядом с ✓Применить/✗Отмена). После check: 🟢 Безопасно / 🔴 Рискованно / ⚪ Тестов нет. При risky — сворачиваемый вывод тестов (`<details>`). Кнопка «Применить» получает визуальный акцент по вердикту (зелёная при safe, предупреждающий стиль при risky) — **но НЕ блокируется**. ARIA: `aria-live=polite` на вердикте.
- **15 новых тестов** (`tests/test_quality_gate.py`): safe/risky/unknown; тесты гоняются в wt_path; секреты в env; вывод обрезается; API check: вердикт, legacy→unknown, bad card_id→400, нет worktree→404, нет проекта→404, meta.gate обновляется. **468 passed** (было 453).
- **Линт:** out of scope в этой итерации (spec-009, п.2). `lint: null` в ответе. Добавить в следующей итерации при необходимости.

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

# Changelog

Все заметные изменения Claude-Ops. Формат — обратный хронологический.
Версии — semver-подобно (0.x пока проект в активной разработке).

> Дисциплина: при появлении новой функции — добавить строку сюда + отметить карточку в TASKS.md → DONE.md. Тег ставится на стабильную точку (`git tag vX.Y.Z`).

## [Unreleased]
_(текущая работа)_

### Добавлено
- **Spec 006 — Память проекта** (ветка `feature-project-memory`): память переехала в репо проекта (`.claude-ops/memory/` — коммитится в git). POST/DELETE эндпоинты для CRUD из кокпита. MemoryTab стал редактируемым (создание/редактирование/удаление записей). Агент пишет память через обычный Write. `MEMORY.md` — авто-индекс. Шаблоны `CLAUDE.md.tpl` / `.gitignore.tpl` обновлены. 49 новых тестов (374 passed).

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

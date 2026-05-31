# Рефакторинг Claude-Ops — прогресс (сессия 2026-05-31)

Ветка: `refactor-2026-05-31` (база: 1660e7c). НА ПРОД НЕ ВЫКАЧЕНО — сервис на старом коде.

## ✅ Сделано (23 коммита, 3 агента)
- **backend (10 карточек):** c01dead, c02sec1 (command injection→exec+shlex), c03sec2 (card_id regex), m06auth (scrypt+rate-limit+secure cookie), s03dead-be (glasses→glasses_transport.py, run_for_glasses ОСТАЛСЯ в bot.py), m01arch (_run_card split + AppCtx), m05dedup, c04hard (Path.home), s01magic-be, s02types-be. pytest 207 passed/6 skipped.
- **frontend (11):** s04hooks, c06errb, m04files (FileExplorer), m07modal (Toast), m02chat (ChatTab 1242→330), m03app (useTabManager/useSplitView созданы НО не до конца внедрены), m10perf, m08aria, s03dead-fe, s06vite, s02types-fe. build OK.
- **cssi18n (2):** m09css (styles.css→10 partials в web/src/styles/), m11i18n (i18n/ru.ts + ESLint/Prettier).

## ⏳ ОСТАЛОСЬ (продолжить отсюда)
1. **docs-агент** (НЕ запущен): c05lic (LICENSE MIT), s05oss (.env.example sanitize + web/.env.example + CONTRIBUTING.md + README warning про credentials), m12apidoc (docs/API.md — 54 роута). Домен: корневые файлы + docs/. Ветка refactor-2026-05-31, коммит `git add <конкретные файлы>`.
2. **tests-агент** (НЕ запущен): m13tests (покрытие api_move_task/card_run/chat/rename + замок конкурентности 409). Домен tests/. DoD pytest зелёный.
3. **ARCHITECTURE.md** — карта кода. Данные собраны: webapp 139 def/54 routes, bot 45 def, web/src 39 файлов. Скелеты в /tmp/cops_webapp_skel.txt, cops_bot_skel.txt (могли стереться).
4. **Чистка TASKS.md** — перенести все 23 сделанные карточки из Backlog в DONE.md. Оставить в Backlog только фичи: a7b2c1, 4df23a, 3a00f3, 2a0a1a, 1a4662(C2), 541008(Timeline), cb8518(Schedules), 72a567(СЕВЕР). Review: 13c785, 10f166. Failed: e46770, e4affa.
5. **WEB_COOKIE_SALT в .env** — backend-агент: scrypt-соль не персистится, токены истекут после рестарта. Сгенерить и добавить в .env ДО рестарта.
6. **Деплой:** `bash /home/igor/claude-ops-bot/restart-self.sh` (НЕ systemctl!). После рестарта — в СЛЕДУЮЩЕМ ходу smoke: `curl -s localhost:8787/api/health`. ⚠️ merge ветки в master перед рестартом (прод читает рабочее дерево).
7. **Финальный отчёт Игорю.**

## Внимание
- glasses run_for_glasses не вынесен (module-globals) — ОК, отдельная задача.
- Параллельные агенты на одном дереве = риск git index.lock. Держать ПОСЛЕДОВАТЕЛЬНО.
- Канал вывода tool периодически флапает — читать результаты через файл-дампы в /tmp/cops_*.txt.

# Tasks — claude-ops-bot

Живая доска проекта. Эта карта — единственная, что читают сессии.
Завершённое уходит в DONE.md (его сессии НЕ читают — гигиена контекста).

Порядок Backlog = порядок выполнения (сверху вниз). Группы помечены жирным комментарием — это просто навигация, парсер их игнорирует.

## Backlog

**🔴 Critical — первым делом**

- [ ] Удалить дубликат `api_new_project` (webapp.py:1555–1623) — мёртвый код, перекрыт вторым определением на строке 3238; также двойная регистрация роута в `start()` <!--ops:c01dead-->
- [ ] Security: command injection через `log_cmd`/`test_cmd` — `create_subprocess_shell` с данными из topics.json; заменить на `subprocess_exec` + `shlex.split` или allowlist команд <!--ops:c02sec1-->
- [ ] Security: path traversal в `api_card_run` (webapp.py:2582) — `card_id` из URL без валидации, можно прочитать произвольный файл; добавить regex `^[a-f0-9-]{4,20}$` <!--ops:c03sec2-->
- [ ] OSS: убрать все хардкоды `/home/igor` из кода — bot.py:113–134 (`_REG_RAW`), webapp.py:361,1865,3514,3522; заменить на `Path.home()` / env / динамический путь в промптах <!--ops:c04hard-->
- [ ] OSS: добавить LICENSE (MIT или Apache 2.0) <!--ops:c05lic-->
- [ ] Frontend: добавить ErrorBoundary — любое исключение валит весь UI белым экраном; минимум на уровне ProjectView + вокруг каждого таба <!--ops:c06errb-->

**🟡 Major — архитектура и качество**

- [ ] Рефакторинг webapp.py (3707 строк): разбить `_run_card` (140 строк, 5 ответственностей) на `_write_sidecar`, `_move_card_after_run`, `_notify_tg`; ввести `TypedDict AppCtx` вместо god-dict `ctx` <!--ops:m01arch-->
- [ ] Рефакторинг ChatTab.tsx (1242 строки): вынести `SessionSelector`, `SessionContextPanel`, `ToolBlock` в `components/`; логику стрима — в хук `useChatStream` <!--ops:m02chat-->
- [ ] Рефакторинг App.tsx (596 строк): извлечь хуки `useTabManager`, `useSplitView`, `useUnreadTracker`; или React Context (`ProjectsContext`, `TabsContext`) <!--ops:m03app-->
- [ ] Дедупликация файловых браузеров: `FilesTab` и `GlobalFilesTab` — полные копии; вынести общий `FileExplorer` с пропами `fetchDir`/`fetchFile`/`onSave` <!--ops:m04files-->
- [ ] Дедупликация бэкенд: SSE-потоки (activity_stream × 2), чтение файлов (api_project_file ≈ api_global_file), листинг (api_project_files ≈ api_global_files) — вынести общие хелперы <!--ops:m05dedup-->
- [ ] Security: cookie без `Secure` флага + слабый SHA-256 хеш без KDF + нет rate limiting на `/api/login`; для OSS — bcrypt/argon2, rate limit per-IP, `secure=True` <!--ops:m06auth-->
- [ ] Frontend: заменить `alert()`/`confirm()`/`prompt()` (8 мест) на UI-компоненты `<Dialog>` / `<ConfirmModal>` <!--ops:m07modal-->
- [ ] Frontend: ARIA — нет `role="dialog"`, `aria-expanded`, `aria-selected`, `aria-label` на иконочных кнопках; keyboard-навигация на доске и файловом дереве <!--ops:m08aria-->
- [ ] CSS: 3018 строк в одном файле без изоляции; переход на CSS Modules или разбивка по компонентам; убрать 50+ inline-стилей с магическими числами <!--ops:m09css-->
- [ ] Performance: `JSON.stringify` для сравнения проектов при polling (App.tsx:134); все открытые ProjectView примонтированы с SSE/polling — передавать `isActive` в BoardTab; `setTick` каждую секунду ре-рендерит весь ChatTab <!--ops:m10perf-->
- [ ] OSS: i18n — весь UI на русском; вынести строки в `src/i18n/ru.ts`; добавить ESLint + Prettier + lint-staged в package.json <!--ops:m11i18n-->
- [ ] OSS: API-документация — 40+ эндпоинтов без описания; создать `docs/API.md` с таблицей (можно сгенерировать из `start()`) <!--ops:m12apidoc-->
- [ ] Тесты: нет покрытия API-хендлеров (api_move_task/F1, api_project_chat, api_project_rename, _ingest_errors_to_board); нет интеграционного теста замка конкурентности (running → 409) <!--ops:m13tests-->

**🟢 Minor / Cosmetic**

- [ ] Магические числа без констант: 4000 (TG chunk), 300 (glasses reply), 2592000 (cookie age), maxsize=100/200 (bus queues) — вынести в именованные константы <!--ops:s01magic-->
- [ ] Type hints: добавить `-> web.Response` на публичные хендлеры, аннотировать `run_engine` как `AsyncGenerator`; фронт — убрать `catch (e: any)` (8 мест), расширить `ChatSSEEvent` <!--ops:s02types-->
- [ ] Мёртвый код: glasses HTTP-транспорт (~80 строк, отключен 2026-05-28) — вынести в `glasses_transport.py` или удалить; мёртвые табы `SpecsTab`/`ReadmeTab`/`ActivityTab` в `web/src/tabs/` <!--ops:s03dead-->
- [ ] Дедупликация фронт-паттернов: хук `useAsyncLoad<T>` (5 мест), хук `useClickOutside` (3 мест), компонент `<Modal>` (2 дубля в BoardTab), `lib/storage.ts` (readLS в 2 файлах) <!--ops:s04hooks-->
- [ ] OSS: `.env.example` — заменить `/home/igor` на плейсхолдер; добавить `web/.env.example`; CONTRIBUTING.md + Quick Start; предупреждение в README про `~/.claude/.credentials.json` <!--ops:s05oss-->
- [ ] Vite config: `http://localhost:8787` хардкод — заменить на `process.env.VITE_BACKEND_URL || 'http://localhost:8787'`; добавить light theme (`prefers-color-scheme`) <!--ops:s06vite-->

**📦 Фичи (из предыдущего бэклога)**

- [ ] Хранение ключей: продумать и реализовать хранилище credentials — проектные ключи (API keys, tokens сервисов) + глобальные ключи; UI в кокпите; безопасное хранение; доступ агенту <!--ops:a7b2c1-->
- [ ] Модели + thinking mode: убрать дубль селектора (оставить в чате), добавить версии моделей и режимы мышления (max/min/default) <!--ops:4df23a-->
- [ ] multi-chat: несколько чатов на один проект, каждый со своим session_id; полоса вкладок чатов <!--ops:3a00f3-->
- [ ] Карточки: своя свежая сессия на карточку + cwd-замок (развести с общей сессией чата перед автономией) <!--ops:2a0a1a-->
- [ ] ⭐ C2: гейт «Применить / Отмена» + worktree-per-task — Review из видимости в безопасный откат (мост к автономии) <!--ops:1a4662-->
- [ ] M1/O1: Timeline — единая шина событий (JSONL+индекс, таб в кокпите, SSE) <!--ops:541008-->
- [ ] Единый Schedules: cron + systemd timers + /schedule + celery + n8n <!--ops:cb8518-->
- [ ] ⭐ СЕВЕР: автономный самоулучшающийся контур (сканер придумывает задачи, гейт ОК/Отмена) — specs/spec-003-autonomy.md <!--ops:72a567-->

## In Progress

## Review
- [?] Running-state пропадает при смене вкладки — возвращаясь на проект с работающим агентом, не видно что он занят; нужно восстанавливать состояние из API <!--ops:13c785-->
- [?] Глобальные скиллы: показывать в кокпите + подготовить проект к публикации на GitHub (закрывать потребности разных пользователей) <!--ops:10f166-->

## Failed
- [!] Кнопка Стоп пропадает при переключении вкладки <!--ops:e46770-->
- [!] Настроить логи для claude-ops-bot: добавить log_cmd в topics.json <!--ops:e4affa-->

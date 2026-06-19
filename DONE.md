# Done — claude-ops-bot

Архив завершённых карточек (append-only). **Сессии его НЕ читают** — гигиена контекста.

## 2026-06-13
- [x] Spec-039: переписан + реализован + задеплоен (LIVE) <!--ops:b1dc7d--> — переписан под «stop killing sessions»: убраны авто-ротация (spec-021), auto-resume на 429, stall-watchdog (остался только аварийный max 2ч); `PERSISTENT_CLIENT=1` → фоновый `run_in_background` выживает между ходами + нативный авто-compact сжимает на месте ~190K без сброса сессии; ручной `/reset` + кокпит «Wrap&reset» реально евиктят live-client; SIGTERM graceful (сохраняет сессии при рестарте); кокпит говорит правду (бар к 200K краснеет, тост на compact, карточка «стена 200K — Reset»). 1227 тестов pass, сборка чистая. Спека: `specs/spec-039-stop-killing-sessions.md`.
- [x] Investigate & fix: background processes / other sessions get killed <!--ops:c8a86f--> — root cause найден: каждый ход = отдельный CLI-подпроцесс (`async with ClaudeSDKClient`), фоновый `run_in_background` — его detached-ребёнок, гибнет с подпроцессом на конце хода (SIGTERM→SIGKILL). НЕ watchdog и НЕ ротация. Фикс = `PERSISTENT_CLIENT=1` (подпроцесс живёт между ходами), в составе spec-039. Закрыто тем же деплоем. Остаточный edge: idle-evict (TTL 1ч) / `/reset` / рестарт.

- [x] Chat: tool-логи не теряют детали при свитче вкладок/рефреше + очередь сообщений переживает рестарт <!--ops:51a612--> — root cause: live SSE форматировал tool-события через `_format_tool`, а в буфер реплея клался СЫРОЙ ивент → при переподключении детали (cmd/output) терялись. Фикс: буферим форматированное (live==replay). Очередь `_CHAT_QUEUE` была in-memory → персист в `data/chat-queue.json` (atomic). 18 тестов.
- [x] Chat: отправка ВИДЕО в чат (не только фото) <!--ops:adb7ea--> — расширен spec-038: Content-Type карта (mp4/webm/mov/ogg), хелпер `cockpit-img` принимает видео (cap 200MB), фронт детектит видео по расширению → `<video controls>` + лайтбокс, Range/seeking из коробки. 24 теста. Хелпер синхронизирован в /usr/local/bin.
- [x] Chat: авто-скролл больше не дёргает вниз при чтении <!--ops:d378a6--> — stick-to-bottom: авто-следование только если ≤80px от низа; иначе пилюля «↓ Новые сообщения»; отправка ре-пинит.
- [x] Board: длинный текст задачи не обрезается при добавлении <!--ops:d1ebd5--> — снят клиентский обрез 120 символов в `BoardTab.addCard()`; full-text round-trips. 3 теста.
- [x] Вкладки: индикатор активности + «ждёт ответа» <!--ops:b2a081--> — working-dot пока AI работает, attention-badge когда закончил на фоновой вкладке, гаснет при открытии вкладки. Один общий SSE (O(1), без per-tab стримов = не воскрешён баг исчерпания соединений); бэкенд `_awaiting`/`_seen` + поля в `/api/projects` + POST `/seen`. 13 тестов.
- [x] Spec: decouple core from Telegram <!--ops:4698ec--> — написана `specs/spec-040-decouple-telegram.md`: 4-фазный план (нейтральные session-ключи → extract engine.py → cockpit-only за флагом → выпил PTB), полный inventory связности, open questions. ДИЗАЙН (реализация — отдельные карточки по фазам). Попутно найден латентный баг: `TELEGRAM_NUDGE` = дефолтный system_prompt для ВСЕХ каналов, включая кокпит (фикс запланирован в Phase 1).
- [x] Fix: modes/session-bar wrapping + высота карточек <!--ops:29b29a--> — `.chat-session-bar`: `flex-wrap:nowrap`+`overflow-x:auto`+`white-space:nowrap` на кнопках (не переносится на 2-ю строку); `.project-item` padding 7→5px. Не сломаны новые activity-dot/reply-badge и мобайл.
- [x] Fix: spec-039 shutdown повис на 90с (регресс этой же сессии) <!--ops:spec039fix--> — SIGTERM-handler флашил сессии, но процесс не выходил: aiohttp `AppRunner` не закрывался + 5 фоновых циклов webapp не отменялись → `asyncio.run` висел до SIGKILL (`timeout`). Фикс: `webapp.stop()` (cancel циклов + `runner.cleanup()`), `_amain` оборачивает teardown в `asyncio.wait_for(12с)` + добивает зависшие таски. Подтверждено на реальном кейсе: рестарт 93с→6с, лог «clean teardown complete» / «Deactivated successfully». 4 теста.

## 2026-06-12
- [x] Cards: own fresh session per card + cwd-lock <!--ops:2a0a1a--> — `_run_card` стартует с `resume_sid=None`, session_id не пишется обратно. `cwd_locks[effective_cwd]` блокирует параллельный запуск в одном cwd через `ephemeral=True`.
- [x] TG-канал: контекст не дублируется <!--ops:9aa43f--> — NO BUG: session resume корректный, system_prompt — свежий dict на каждый вызов, рост контекста 1.01x/ход. 9 тестов в `tests/test_tg_session_resume.py`.
- [x] Вкладка «Обзор» удалена, инфо перенесена в Настройки <!--ops:d124ae--> — `ProjectView.tsx` убрал таб, контент (git-state, структура) в `SettingsTab.tsx`. Кнопки аудита одинаковы для всех проектов. Файл-зомби `OverviewTab.tsx` удалён.
- [x] Выделение сообщений пользователя в чате <!--ops:c14fec--> — `.chat-msg-user`: `background: rgba(107,104,245,0.07)` + `border-left: 2px solid var(--accent)`.

## 2026-06-02
- [x] ⭐ Настройки проекта + глобальные настройки (UI): таб «⚙️ Настройки» <!--ops:f2ba02--> — per-project (git on/off флагман + модель/self_heal/notify/log_cmd/test_cmd, topics.json hot-reload) + глобальные (data/settings.json, провязаны в рантайм: self_heal master-kill, max_concurrent, scan_interval, дефолт-модель, watchdog stall/max). API GET/POST `/api/settings` + `/api/projects/{id}/settings` с валидацией. 20 тестов (test_settings.py).
- [x] rename проекта сохраняет историю диалогов + Timeline (миграция SDK-каталога по slug); восстановлены family-emergency/autotopic-test; tests test_project_rename + test_forum_topic.
- [x] Авто-создание forum-топика в Telegram при создании проекта <!--ops:89f1cd--> — api_new_project зовёт create_forum_topic, в topics.json реальный ключ `<chat>:<thread_id>`; покрыто test_forum_topic.py. (Опц. welcome-сообщение / closeForumTopic при удалении — не делалось, не критично.)
- [x] «Переименовал — пропали сессии / вопрос создания нового проекта» <!--ops:ac3ff0--> — закрыто фиксом rename выше; new-project (untitled + forum-топик + онбординг) работает.
- [x] Снят ложный [TEST]-инцидент `test_memory_read_all_new_takes_priority` (err-5e9db6) — реальный pytest зелёный (529 passed), сканер поймал транзиент во время правок дерева.

## 2026-05-30 (Послесессионная уборка)
- [x] LogsTab: Readme/Specs убраны, Активность переименована в Логи; тянет log_cmd из topics.json; empty state + кнопка «добавить задачу в бэклог» <!--ops:701bd1-->
- [x] Board wipe protection: _PLAIN_CARD_RE + _count_potential_cards safety guard + asyncio.Lock per-cwd; восстановлены 39 задач networking-os <!--ops:d00913-->
- [x] Идея «Новый проект» — захвачена и переработана в карточку newproj (backlog) <!--ops:8d08bb-->

## 2026-05-30 (Split-view)
- [x] split-view: кнопка ⊞ Split в free-чате открывает второй чат рядом; перетаскиваемый разделитель; ширина в localStorage; ✕ Закрыть на правой панели; splitPairs персистируются между сессиями <!--ops:faea4a-->

## 2026-05-30 (Chat files)
- [x] chat-files: кнопка 📎, drag-and-drop на input-зону, Ctrl+V вставка из буфера → upload в data/inbox/ (POST /upload), путь добавляется в промпт; чипы вложений с прогрессом и ✕; поддержка очереди сообщений с файлами <!--ops:180b2c-->

## 2026-05-30 (TG шина)
- [x] tg-bus: TG-прогоны публикуются в шину активности webapp — видны в кокпит-чате вживую (📱 TG: префикс); `webapp._bus_publish` / `webapp._format_tool` вызываются напрямую из `run_agent` в bot.py <!--ops:tgbus--> <!--ops:91b24d-->

## 2026-05-30 (UX-патчи + Board + DnD)
- [x] board-dnd: drag-and-drop карточек между колонками (HTML5 DnD); карточка тускнеет при захвате, целевая колонка подсвечивается пунктиром; In Progress не перетаскивается; кнопки ←→ сохранены для планшета <!--ops:01f140-->

## 2026-05-30 (UX-патчи + Board)
- [x] board-add-top: новая карточка вставляется в начало Backlog (`insert(0,...)`), а не в конец <!--ops:878165-->
- [x] board-inline-edit: двойной клик на карточке → inline-редактирование текста; работает во всех колонках кроме In Progress <!--ops:a2b1c3-->
- [x] activity-sort: вкладка Активность — новые записи сверху (backend разворачивает tail) <!--ops:6cc26f-->

## 2026-05-30 (UX-патчи)
- [x] tab-close-active: крестик ✕ появляется только на активной вкладке (не невидимый, а отсутствует в DOM) — нельзя случайно закрыть неактивную <!--ops:c1d7d8-->
- [x] ctx-refresh-on-session: панель «Контекст сессии» обновлялась только после отправки сообщения; теперь сбрасывается сразу при смене/сбросе сессии (bug fix, ctxRefreshKey)

## 2026-05-29 (UX-волна кокпита)
- [x] tabs-projects: открытые проекты как вкладки сверху (✕ закрыть, клик в сайдбаре открывает); рендер всех ProjectView с display:none для неактивных — сохраняет state чата и SSE при переключении <!--ops:2fd121-->
- [x] chat-noscroll: мгновенный скролл к низу (behavior:auto), без раздражающей анимации на каждый чанк <!--ops:861379-->
- [x] chat-model-switch: селектор модели (sonnet/opus/haiku) в шапке чата; POST /api/projects/{id}/model + save_topics, для free-чата пишет в free_chats.json <!--ops:93a2db-->
- [x] chat-stats: мини-статистика «N сообщ · ~K токенов» в верхней полосе чата; жёлтый >100K, красный >180K <!--ops:61763b-->
- [x] chat-pulse: тикающий таймер + конкретика последнего инструмента (🔧 Bash · git status · 5с) + жёлтый при тишине >30с, красный пульсирующий >120с. Единый индикатор для chat-POST и card-run из шины <!--ops:153036-->
- [x] git-live: polling /api/projects каждые 15с (только visible), refresh при focus/visibility, refresh на run_end из шины и после chat-стрима. Точка/сайдбар обновляются сами без F5 <!--ops:cd7877-->
- [x] chat-queue: пока агент работает — Enter ставит в очередь, поле сразу очищается; «⏭ в очереди: N» в status-bar; следующее автоматически уходит после finally (через 150мс); стоп/смена проекта чистят очередь <!--ops:3b02de-->
- [x] git-sync кнопка в шапке проекта: точка-индикатор (зелёная/жёлтая/серая) + кнопка «↑ Sync» (commit «wip: дата» если dirty + push); POST /api/projects/{id}/git/sync — E2E (этой кнопкой и был запушен прошлый коммит)
- [x] UsageBadge в правом углу полосы вкладок: 5ч окно + неделя (utilization% и время сброса); GET /api/usage из bot.py:rate_limits; обновление 30с + при focus
- [x] Свободные чаты (free): виртуальный «проект» с cwd=$HOME, без git/TG/табов; кнопка «+» в полосе вкладок (как браузер) создаёт новый со своим session_id; двойной клик по вкладке → inline rename; rename free-вкладки автоматически переименовывает активную сессию (свой слой data/session_labels.json); удаление через ✕ в сайдбаре. Для free отрендерен только чат на всю ширину
- [x] Live-сегментация: ответ агента в чате сегментируется по границам text↔tool (новое assistant-message), порядок «текст → файл → текст → файл» сохраняется в реальном времени как после reload
- [x] Tool-блоки Edit/Write: кнопка «▼ diff»/«▼ содержимое» переехала на ту же строку что имя файла (освободило строку)
- [x] Старый «агент думает…» внутри сообщения убран — статус показывается богатым pulse-баром внизу
- [x] Сайдбар: collapse в полоску 56px с буквенными иконками и точкой непрочитанного; recent-сортировка проектов (LS); глобальный SSE /api/activity-stream с unread-бейджами в сайдбаре и табах
- [x] Чат: богатый CLI-рендер инструментов — полная команда Bash / файл+дифф Edit / превью Write / путь Read (Ур.1) <!--ops:richtool--> — E2E
- [x] «Память проекта» — таб: memory-файлы агента из ~/.claude/projects/<cwd>/memory/ <!--ops:projmem--> — E2E
- [x] «Контекст сессии» — панель у чата: 📖 прочитано / ✏️ изменено / ⚙ команды (из транскрипта) <!--ops:ctxsess--> — E2E
- [x] Кнопка «Стоп» реально прерывает прогон (POST /chat/stop → client.interrupt) — E2E: sleep-60 оборван за 7с <!--ops:chatstop-->
- [x] Чат: история сессии грузится из SDK-транскрипта (на reload видно прошлую переписку) <!--ops:chathist-->
- [x] Чат: ресайз/сворачивание панели (localStorage) + выбор сессий (общая с TG, /reset+/resume в UI) <!--ops:chatui-->
- [x] Чат: компактный терминальный вид (без пузырей/плашек) <!--ops:chatcompact-->
- [x] Чат-панель постоянная (~45%, не вкладка) <!--ops:chatpanel-->
- [x] Проводник файлов — read-only дерево + просмотр (.md рендер, код моно), анти-traversal <!--ops:filesexp-->
- [x] Specs-таб читает локальную <cwd>/specs/ + vault <!--ops:specslocal-->
- [x] Доска: рендер TASKS.md/DONE.md как канбан в кокпите (создание/перенос/архив) <!--ops:571c91--> — работает
- [x] Live-прогресс карточек в чат-панели через шину событий (activity-stream): прогон карточки виден вживую как «🗂 карточка» <!--ops:livep1--> — E2E на проде
- [x] C1: чат по проекту в кокпите (SSE-стрим, сессия общая с TG) <!--ops:d616af--> — E2E на проде (sandbox резюмит session topic 369)
- [x] F1: авто-запуск карточки — перенос Backlog→In Progress запускает run_engine, авто→Review/Failed + сайдкар + TG-пинг <!--ops:8c3888--> — E2E на проде (sandbox)
- [x] F1: кросс-пинг в TG-топик при завершении карточки (внутри F1) <!--ops:650fb7-->
- [x] F0: рефактор run_agent → async-генератор `run_engine` (TG/glasses/web — один движок) <!--ops:f65e6e--> — E2E на проде, sandbox+Networking-OS
- [x] M1/B1+B2: baseline-каркас + уровни зрелости L0–L4 → specs/baseline.md <!--ops:b1b2-->
- [x] M1/O2+O3: перепись парка + реестр → ~/vault/01-Projects/_park-inventory.md + _registry.yaml <!--ops:o2o3-->
- [x] git репо создан (PRIVATE Zira777ru/claude-ops-bot), гигиена секретов <!--ops:gitinit-->
- [x] кокпит: MD-таблицы в Specs/README/CLAUDE/Board (remark-gfm) <!--ops:gfm-->
- [x] кокпит: inline-редактор CLAUDE.md/README во вкладке (двойной клик→textarea, POST-запись, Ctrl+Enter/Esc) <!--ops:455557--> — 2026-05-30, round-trip проверен на проде
- [x] кокпит: ручной rename ЛЮБОЙ сессии из SessionSelector (✎ → label, POST /sessions/{sid}/label, sidecar) <!--ops:3c9499--> — 2026-05-30, set/clear проверен на проде
- [x] кокпит: пустое состояние «Память» (что это/как создаётся) + пояснение пола контекста ~11-14K (системный промпт+инструменты) <!--ops:fa3c3a--> — 2026-05-30
- [x] кокпит: запуск тестов проекта (автодетект pytest/npm/make, POST /test, вывод в Обзоре) <!--ops:f30032--> — 2026-05-30, детект+роутинг+not-detected проверены; реальный прогон суиты — на usere
- [x] ⭐ F: «+ Новый проект» — кнопка в сайдбаре без форм/вопросов. Создаёт проект «Без названия» (cwd=$HOME/projects/untitled-<ts>, запись в topics.json). Авто-создаёт TASKS.md с одной карточкой «🚀 Инициализировать проект» и сразу запускает её (In Progress). Промт карточки — интерактивная сессия-онбординг: агент СНАЧАЛА спрашивает о проекте (что за проект, есть ли уже наработки в других папках/чатах, цели), сканирует упомянутые папки, потом вместе с пользователем создаёт: CLAUDE.md (описание + правила канбана + как формулировать задачи), TASKS.md с реальными задачами, specs/, .gitignore, README. Свободные чаты для инициации не нужны — этот флоу их заменяет. · 2026-05-31
- [x] А ещё у меня такая идея. Когда создаёшь новую сессию в проекте, чтобы задался вопрос, допустим, такой отправить Промт, завершающий сессию? И да или нет, то есть такое, вот знаешь сообщение, модульное кно какое-нибудь, и там будет Промт, завершись ещё сохрани все там, допустим. Отметь выполненные задачи в этой сессии. Ну что-нибудь такое, вот у меня, кстати, есть такой примерно Промт. И жмут, да или нет, то есть. Просто иногда бывает, что там забываю сессии закрыть правильно, и некоторые задачи не получаются, не отмечены выполненными, которые были сделаны за период сессии. Или же там, допустим, лишние файлы лишнее что-нибудь такое созданное? Короче, надо каждую сессию правильно закрывать и прежде чем получается создать новую сессию, может быть, задавать вопрос, то есть отправить такой Промт или нет? · 2026-05-31

## 2026-05-31 — Рефакторинг-проход (25 карточек, 5 Sonnet-агентов)
### Security
- Удалён дубликат api_new_project + двойной роут /api/projects/new (ops:c01dead)
- Command injection в log_cmd/test_cmd: create_subprocess_shell → exec+shlex.split (ops:c02sec1)
- Path traversal: валидация card_id (regex) во всех task-эндпоинтах (ops:c03sec2)
- Auth: sha256 → scrypt + secure/httponly cookie + rate-limit 5/5min → 429 (ops:m06auth)
### Backend refactor
- glasses HTTP-транспорт вынесен в glasses_transport.py (run_for_glasses остался в bot.py) (ops:s03dead-be)
- _run_card разбит на _write_sidecar/_move_card_after_run/_notify_tg + AppCtx TypedDict (ops:m01arch)
- Дедуп: общий _sse_stream + _read_file_content (ops:m05dedup)
- Убраны хардкоды /home/igor → Path.home() (ops:c04hard)
- Магические числа → именованные константы (ops:s01magic)
- Type hints: -> web.Response, run_engine -> AsyncGenerator (ops:s02types)
### Frontend refactor
- Общие примитивы: useAsyncLoad/useClickOutside/Modal/lib/storage (ops:s04hooks)
- ErrorBoundary вокруг ProjectView и всех табов (ops:c06errb)
- FileExplorer: дедуп FilesTab+GlobalFilesTab (300→20 LOC каждый) (ops:m04files)
- alert/confirm/prompt → Toast/ConfirmModal/inline-модалки (ops:m07modal)
- ChatTab 1242→330: ToolBlock/SessionSelector/SessionContextPanel + useChatStream (ops:m02chat)
- App.tsx: useTabManager/useSplitView/useUnreadTracker (частично внедрены) (ops:m03app)
- Perf: убран JSON.stringify-сравнение, isActive-guard на polling (ops:m10perf)
- ARIA: role/aria-label/aria-selected + keyboard-навигация (ops:m08aria)
- Удалены мёртвые табы SpecsTab/ReadmeTab/ActivityTab (ops:s03dead-fe)
- VITE_BACKEND_URL env + light-тема (ops:s06vite)
- catch(e:any)→catch(e)+instanceof; ChatSSEEvent (ops:s02types-fe)
- styles.css 3084 стр → 10 partials в styles/ (ops:m09css)
- i18n/ru.ts (~110 ключей) + ESLint/Prettier (ops:m11i18n)
### OSS docs + тесты
- LICENSE MIT (ops:c05lic)
- .env.example sanitize + web/.env.example + CONTRIBUTING.md + README auth-warning (ops:s05oss)
- docs/API.md — 56 роутов (ops:m12apidoc)
- +93 теста (API доски/чат/rename/ingest/конкурентность/security): 207→300 passed (ops:m13tests)
- ARCHITECTURE.md — карта кода для будущих сессий
- [x] ⭐ C2: гейт «Применить / Отмена» + worktree-per-task — Review из видимости в безопасный откат (мост к автономии) · 2026-06-01
- [x] M1/O1: Timeline — единая шина событий (JSONL+индекс, таб в кокпите, SSE) · 2026-06-01
- [x] ⭐ СЕВЕР: автономный самоулучшающийся контур (сканер придумывает задачи, гейт ОК/Отмена) — specs/spec-003-autonomy.md · 2026-06-01
- [x] Настроить логи для claude-ops-bot: добавить log_cmd в topics.json · 2026-06-01
- [x] Running-state пропадает при смене вкладки — возвращаясь на проект с работающим агентом, не видно что он занят; нужно восстанавливать состояние из API · 2026-06-01
- [x] ⭐ Рефакторинг Ф0: безопасные фиксы багов + error-middleware кокпита (spec-011) · 2026-06-04
- [x] ⭐ Рефакторинг Ф1: мониторинг через ошибки рантайма, авто-прогон тестов OFF (spec-011) · 2026-06-04
- [x] ⭐ Рефакторинг Ф2: UI три зоны (Шапка/Обзор/Настройки), вкладки 9→7 (spec-011) · 2026-06-04
- [x] ⭐ Рефакторинг Ф3: чистка data/runs + рефактор-долг + дыры в тестах (spec-011) · 2026-06-04
- [x] Кнопка Стоп пропадает при переключении вкладки · 2026-06-04
- [x] Глобальные скиллы: показывать в кокпите + подготовить проект к публикации на GitHub (закрывать потребности разных пользователей) · 2026-06-04
- [x] Сейчас на Доске у нас есть Бэклог и можно отправить агенту на выполнение т.е. перенести In Progress. Там кнопка стрелочк · 2026-06-04
- [x] У меня уже есть возможность двигать проекты в списке меню, но почему-то через планшет это не получается. · 2026-06-05
- [x] [ERR] ERROR: telegram.ext.Updater Exception happened while polling for updates. · 2026-06-05
- [x] Кнопочка выделения в бклок очень стрёмно стоит. Нужно немножко сокращать пространство.. · 2026-06-05
- [x] [ERR] ERROR: telegram.ext.Updater Exception happened while polling for updates. <!--ops:err-b73031-->
  > Closed: self-healing removed (spec-010 dropped in v0.9)
- [x] Единый Schedules: cron + systemd timers + /schedule + celery + n8n · 2026-06-10
- [x] [feature] Deferred prompt runs — schedule agent start at a set time or after rate-limit window reset · 2026-06-11
- [x] [research] addyosmani/agent-skills as source of default executor playbooks for ClaudeOps · 2026-06-11
- [x] TG-канал: очередь TG-сообщений (bot.py on_message — второе сообщение в очередь, не «уже работаю») · 2026-06-11
- [x] Убого сделаны ... в списке проектов. Убрать вообще эти точки. Отправить в Архив нужно через настройки проекта во вкладке · 2026-06-12
- [x] Live-trace: видеть что делают субагенты внутри сессии + не терять историю/таймер после обновления страницы → spec-035 <!--ops:847153-->
- [x] spec-034 Фаза 2 — спека-как-деталь-карточки (sidecar по card-id) + live-активность агента прямо на карточке доски <!--ops:5e1c0a-->
- [x] Token economy: model routing per work type — board cards default to sonnet (cheap execution), chat stays on project model, per-card model field + global default in settings. Biggest untouched cost lever (idea 2026-06-11, conductor session) <!--ops:43665f-->
- [x] Карточки, при двойном клике можно редатировать карту прямо в доске. При нажатии на кнопку Description, можно редактирова <!--ops:0adff6-->
- [x] В терминале Claude CLI умеет делать карточки в выбором вариант 1, 2, 3 или 4. и просто стралками (или мышкой) выбираешь. <!--ops:c728cf-->
- [x] Сделать карточки Failed не карточками а горизонтальными строками. <!--ops:adf1c9-->
- [x] Когда скрываешь меню с проектами, остается маленькая полоска, это ОК и там иконка + (добавить проект). Она там не нужно. · 2026-06-13
- [x] В самом низу доски есть Archive. Его часто вообще не вижно. Можно его вынести куда то отдельно. Например где строка COLU · 2026-06-13
- [x] Сделать возможность редактировать/удалять сообщения которые в очереди на отправку. Пока агент что-то делает можно ему до · 2026-06-13
- [x] Thinking mode: селектор режима мышления (max/min/default) в чате · 2026-06-13
- [x] multi-chat: несколько чатов на один проект, каждый со своим session_id; полоса вкладок чатов · 2026-06-13
- [x] Решить вопрос с удалением проекта. В том числе и, допустим, надо при удалении решать полностью удалять проекты, все смен · 2026-06-13
- [x] Глобальные ключи + общий UI хранилища credentials · 2026-06-13
- [x] Добавить уведомления - что ответ от AI готов во вкладки наверху. · 2026-06-13
- [x] Mobile: add light theme and improve text contrast <!--ops:721575-->
- [x] Mobile: fix tab switching and enlarge close button <!--ops:5947aa-->
- [x] Mobile: fix status bar layout wrapping to single line <!--ops:c4fe16-->
- [x] Mobile: hide icons (secrets, schedule, files) in project tab bar <!--ops:3575a4-->
- [x] Commit recovered frontend <!--ops:4317f6-->
- [x] E2E: verify send/queue behavior (regression 056ffb) <!--ops:b2f3c1-->
- [x] Verify spec-039 with Antigravity client <!--ops:df95f7-->
- [x] Antigravity: spec-041 pointwise verification <!--ops:9e794a-->
- [x] spec-041 block A: Fix ChatTab.tsx A1/A2/A4 <!--ops:bea94c-->
- [x] UX audit: identify cockpit rough edges <!--ops:60100f-->
- [x] spec-042 written: reset/handoff and session UX <!--ops:7d3c77-->
- [x] Implement spec-042: cheap handoff reset (+ closes spec-041 C5 + session-label clarity) <!--ops:5b042c-->
- [x] Spec-043 research: session context floor analysis <!--ops:4345ee-->
- [x] Spec-043: Implement context cost and caching strategy <!--ops:444758-->
- [x] Feature: auto-label closed sessions via haiku in _build_handoff <!--ops:e73583-->

# ARCHITECTURE.md — Claude-Ops

Карта кода для будущих сессий: **где что искать**. Истина = код; этот файл — навигатор.
Подробные gotchas — в `CLAUDE.md`. HTTP-контракт — в `docs/API.md`.

> Claude-Ops — IDE-среда управления проектами через Claude Agent SDK. Три канала входа, один движок, full-auto.
> **Один процесс** (aiohttp + python-telegram-bot): `bot.py` импортит `webapp.py` и поднимает кокпит в том же event-loop. Общий `running`-замок → нет гонки по cwd между каналами.

```
┌─────────────────────────────────────────────────────────────────┐
│                      ОДИН PYTHON-ПРОЦЕСС                          │
│                                                                   │
│  Telegram (@ziraclaudebot) ─┐                                     │
│  Кокпит (claude-ops.coscore.us) ─┼─► run_engine() ─► Claude SDK   │
│  Канбан-автозапуск (карточка) ──┘    (async-генератор событий)    │
│  [Glasses HTTP — заглушен] ─────┘                                 │
│                                                                   │
│  Общее состояние: running{} · sessions{} · topics{} (через ctx)  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Ядро: `bot.py` (~1170 строк, 45 функций)

TG-канал + **движок** + точка старта процесса.

### Движок (транспорт-независимое сердце)
- **`run_engine(...)` (bot.py:419)** — `async def -> AsyncGenerator[dict, None]`. Гоняет Claude Agent SDK, yield-ит события `{tool|text|result|rate_limit|error}`. **Не знает про транспорт.** Все каналы — его потребители. Меняешь логику работы агента → здесь.
- **Потребители движка:**
  - `run_agent(context, update, prompt)` (bot.py:509) — TG-адаптер: статус-сообщение, watchdog, audit, финальная отправка.
  - `run_for_glasses(name, prompt)` (bot.py:687) — HUD-адаптер для очков G2 (≤300 chars). Сейчас отключён, но код живой; передаётся в `ctx`.
  - `_run_card(...)` в **webapp.py** — автозапуск карточки (тоже потребитель run_engine).
  - `api_project_chat` в **webapp.py** — веб-чат (SSE-потребитель).

### Конкурентность / состояние
- **`running{key: bool}`** — замок per-`cwd`. Резерв СИНХРОННО в `on_message` (bot.py:766) до первого await, снятие в `safe_run` (bot.py:806) `finally`. Это защита от двух full-auto процессов на одном проекте.
- **`sessions{key: session_id}`** (СЛОЙ 2, `data/sessions.json`, `save_sessions` bot.py:196) — SDK-сессии, `/reset` чистит.
- **`topics{key: {project,cwd,model,log_cmd,...}}`** (СЛОЙ 1, `data/topics.json`, `save_topics` bot.py:192) — привязка канал→проект, вечная.
- `key_of(update)` (bot.py:200), `binding_for(update)` (bot.py:206) — резолв ключа `"chat:thread"`.

### Реестр проектов
- `build_registry()` (bot.py:154), `resolve_project(name)` (bot.py:166), `_home_sub(*parts)` (bot.py:122) — пути от `Path.home()` (после рефактора c04hard, БЕЗ хардкодов `/home/igor`). Новый проект → алиас в `_REG_RAW` или авто-скан `~`.

### TG-команды (bot.py:859–1062)
`cmd_start · cmd_whoami · cmd_reset · cmd_resume · cmd_model · cmd_project · cmd_newtopic · cmd_diff · cmd_cost · cmd_usage · cmd_stop`. Хендлеры: `on_message`, `on_topic_created` (авто-привязка проекта), `on_error`.

### Рендер/утилиты
- `md_to_html(text)` (bot.py:278) + `_render_code_block` (bot.py:264) — markdown→TG-HTML со сворачиванием кода. ВСЕ ответы через него (иначе краш HTML parse_mode).
- `send()` (bot.py:244) + `_tg_call()` (bot.py:226) — отправка с ретраем транзиентных сбоев; `_smart_chunks` (bot.py:347) — нарезка по `TG_CHUNK=4000`.
- `audit()` (bot.py:390) + `_is_destructive()` (bot.py:385) — audit-лог full-auto в `data/audit/`.

### Старт
- **`_on_start(app)` (bot.py:1062)** — post_init: поднимает glasses-HTTP (выкл) + `webapp.start(app, ctx)`. **Здесь формируется `ctx`** — словарь ссылок на общее состояние, передаётся в webapp.
- `main()` (bot.py:1083) — сборка PTB-приложения, регистрация хендлеров, `_load_env()`.

---

## Кокпит: `webapp.py` (~3700 строк, 56 API-роутов)

aiohttp-сервер. **НЕ импортит `bot.py`** (двойное состояние!) — всё получает через `ctx` (передан из `bot.py:_on_start`).

- **`AppCtx(TypedDict, total=False)` (webapp.py:1479)** — типизация `ctx`: `topics/sessions/running/resolve_project/run_for_glasses/DATA/HERE/...`. Рантайм — обычный dict, аннотация для читаемости. **Хочешь понять что доступно из webapp — смотри AppCtx.**
- `start(app, ctx)` — регистрация всех 56 роутов + middleware. **Полный список роутов → `docs/API.md`.**
- **Auth:** cookie `cops_auth`, `_derive_token` на `hashlib.scrypt` (соль `WEB_COOKIE_SALT`), `secure/httponly/samesite`, rate-limit 5 fail/5min → 429 (после рефактора m06auth). Middleware на `/api/*` кроме `/api/health`, `/api/login`.

### Ключевые группы хендлеров (имена `api_*`)
| Область | Хендлеры | Примечание |
|---|---|---|
| Проекты | `api_projects`, `api_new_project`, `api_rename`, `api_health`, `api_git_sync` | дубль api_new_project удалён (c01dead) |
| Доска/Tasks | `api_project_tasks`, `api_create_task`, `api_move_task`, `api_delete_task`, `api_update_task`, `api_card_run`, `api_tasks_done` | `card_id` валидируется `_valid_card_id`/`_CARD_ID_RE` (c03sec2) |
| Автозапуск | **`_run_card`** → `_write_sidecar` + `_move_card_after_run` + `_notify_tg` | разбит на 3 хелпера (m01arch). Перенос в In Progress → run_engine |
| Чат/SSE | `api_project_chat`, `api_chat_stop`, `_sse_stream`, `api_activity_stream` | общий `_sse_stream` (m05dedup) |
| Файлы | `api_project_files`, `api_project_file`, `api_global_files`, `api_global_file` | общий `_read_file_content`; анти-traversal `_resolve_safe`/`_resolve_global_safe` |
| Промты | `api_prompts` (CRUD) | `data/prompts.json` |
| Сессии | `api_sessions`, `api_session` (new/resume), `api_session_history`, `api_session_context` | общие с TG |
| Usage | `api_usage` | oauth-эндпоинт, кэш 60с |
| Прочее | `api_logs`, `api_memory`, `api_claude_md`, `api_audit`, `api_upgrade`, `api_scan_errors`, `_ingest_errors_to_board` | |
| Subprocess | `_run_log_cmd`, `_run_test_cmd`, `api_project_logs` | `create_subprocess_exec(*shlex.split())` — НЕ shell (c02sec1) |

---

## Glasses: `glasses_transport.py` (новый, после s03dead-be)
HTTP-роуты/CORS/`start()` для очков G2 вынесены сюда (намеренно отключены пустым `GLASSES_TOKEN`).
`run_for_glasses` ОСТАЛСЯ в `bot.py` (тесно связан с module-globals `running/sessions/costs` + `run_engine`) — передаётся в transport через `ctx`. Полный вынос = отдельная задача (нужен ctx-рефактор bot.py).

---

## Фронтенд: `web/src/` (React + Vite + TS, 39 файлов)

После рефактора (m02chat/m03app/m04files/s04hooks): крупные файлы разбиты, дубли вынесены.

```
web/src/
├── main.tsx                  точка входа
├── App.tsx                   корень: проекты, табы, split-view, polling
├── api.ts                    HTTP-клиент (VITE_BACKEND_URL || localhost:8787)
├── types.ts                  типы (ChatSSEEvent и пр.)
├── i18n/
│   ├── ru.ts                 ~110 ключей UI-строк (подготовка к переводу)
│   └── index.ts              export const t = ru
├── lib/
│   └── storage.ts            readLS/writeLS (localStorage)
├── hooks/
│   ├── useChatStream.ts      ⭐ SSE-стрим чата (reader, chunk-safe парсинг)
│   ├── useAsyncLoad.ts       generic loading/error/data
│   ├── useClickOutside.ts    ·  useProjectActivity.tsx  (шина активности)
│   ├── useTabManager.ts · useSplitView.ts · useUnreadTracker.ts  (созданы; частично внедрены — см. ниже)
├── components/
│   ├── ProjectView.tsx       контейнер проекта (табы слева + чат справа)
│   ├── ProjectTabBar.tsx · Sidebar.tsx (DnD)
│   ├── ChatTab-части:        ToolBlock · SessionSelector · SessionContextPanel
│   ├── FileExplorer.tsx      ⭐ общий для Files/GlobalFiles (был дубль)
│   ├── Modal.tsx · ConfirmModal.tsx · Toast.tsx  (заменили alert/confirm/prompt)
│   ├── ErrorBoundary.tsx     ⭐ оборачивает ProjectView + каждый таб
│   ├── PromptPicker · SkillPicker · UsageBadge · ProjectStructureCard
│   ├── EditableMarkdown · HealthDot · LoginScreen · Spinner
├── tabs/                     overview | claude-md | logs | board | files | memory
│   ├── ChatTab.tsx           (1242→ разбит; ядро + useChatStream)
│   ├── BoardTab.tsx          канбан (isActive-guard на polling)
│   ├── FilesTab / GlobalFilesTab  (тонкие обёртки над FileExplorer)
│   ├── OverviewTab · LogsTab · MemoryTab · ClaudeMdTab
│   └── (удалены: SpecsTab, ReadmeTab, ActivityTab — s03dead-fe)
└── styles/                   ⭐ styles.css (3084 стр) разбит на 10 partials
    ├── base.css (vars/тема/light) · layout · sidebar · tabbar · overview
    └── board · chat · files · modal · forms
        (корневой styles.css = 10 @import в порядке каскада)
```

⚠️ **Не до конца внедрено:** `useTabManager`/`useSplitView` созданы как хуки, но в `App.tsx` логика частично осталась инлайн (риск регрессии при полном переносе) — хуки импортируемы, доработка = отдельная карточка.

---

## Тесты: `tests/` (17 файлов, 300 passed / 6 skipped)
`venv/bin/python -m pytest -q` (или `make test`). Фикстуры — `conftest.py` (aiohttp client, tmp-cwd, mock ctx, `_auth_token`).
- **Критичное:** `test_board_parser` (регрессия = потеря задач в проде), `test_security` + `test_security_regressions` (path-traversal, card_id, rate-limit), `test_board_api`, `test_run_card`, `test_chat_sse`, `test_project_rename`, `test_ingest_errors`.

---

## Данные и эксплуатация
- `data/topics.json` (СЛОЙ 1, вечный) · `data/sessions.json` (СЛОЙ 2, /reset чистит) · `data/prompts.json` · `data/runs/<card>.md` (сайдкары) · `data/audit/` · `data/inbox/` (файлы из TG). **`data/` в .gitignore.**
- `.env` (секреты, не в git) · `.env.example` + `web/.env.example` (плейсхолдеры).
- `claude-ops-bot.service` (systemd) · **`restart-self.sh`** (ЕДИНСТВЕННЫЙ способ рестарта — detached через systemd-run; `systemctl`/`kill` своего процесса = суицид cgroup, см. CLAUDE.md).
- `TASKS.md` (доска, читают сессии) · `DONE.md` (архив, сессии НЕ читают) · `docs/API.md` · `CONTRIBUTING.md` · `LICENSE` (MIT).

## Поток одной задачи (end-to-end)
```
TG-сообщение / карточка→In Progress / веб-чат
  → резерв running[cwd] (синхронно)
  → run_engine() гоняет SDK, yield events
  → адаптер рендерит (TG: send+md_to_html / web: SSE / карточка: sidecar)
  → session_id сохранён, running снят в finally
  → (карточка) → Review/Failed + пинг TG
```

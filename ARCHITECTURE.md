> ARCHITECTURE = карта кода (где что искать). Gotchas → CLAUDE.md. HTTP-контракт → docs/API.md. Запуск → CONTRIBUTING.md.

# ARCHITECTURE.md — Claude-Ops

Навигатор по кодовой базе. Истина = код; этот файл — карта. Меняешь поведение → ищи здесь нужный файл и строку.

> Claude-Ops — IDE-среда управления проектами через Claude Agent SDK. Три канала входа, один движок, full-auto.
> **Один процесс** (aiohttp + python-telegram-bot): `bot.py` импортит `webapp.py` и поднимает кокпит в том же event-loop. Общий `running`-замок → нет гонки по cwd между каналами.

```
┌─────────────────────────────────────────────────────────────────┐
│                      ОДИН PYTHON-ПРОЦЕСС                        │
│                                                                  │
│  Telegram (@ziraclaudebot) ─┐                                    │
│  Кокпит (claude-ops.coscore.us) ─┼─► run_engine() ─► Claude SDK │
│  Канбан-автозапуск (карточка) ──┘    (async-генератор событий)  │
│                                                                  │
│  Общее состояние: running{} · sessions{} · topics{} (через ctx) │
└─────────────────────────────────────────────────────────────────┘
```

---

## Ядро: `bot.py` (~1020 строк, 45 функций)

TG-канал + **движок** + точка старта процесса.

### Движок (транспорт-независимое сердце)
- **`run_engine(...)` (bot.py:419)** — `async def -> AsyncGenerator[dict, None]`. Гоняет Claude Agent SDK, yield-ит события `{tool|text|result|rate_limit|error}`. **Не знает про транспорт.** Все каналы — его потребители. Меняешь логику работы агента → здесь.
- **Потребители движка:**
  - `run_agent(context, update, prompt)` (bot.py:509) — TG-адаптер: статус-сообщение, watchdog, audit, финальная отправка.
  - `_run_card(...)` в **webapp.py** — автозапуск карточки.
  - `api_project_chat` в **webapp.py** — веб-чат (SSE-потребитель).

### Конкурентность / состояние
- **`running{key: bool}`** — замок per-`cwd`. Резерв СИНХРОННО в `on_message` (bot.py:766) до первого await, снятие в `safe_run` (bot.py:806) `finally`. Защита от двух параллельных процессов на одном проекте.
- **`sessions{key: session_id}`** (СЛОЙ 2, `data/sessions.json`, `save_sessions` bot.py:196) — SDK-сессии, `/reset` чистит.
- **`topics{key: {project,cwd,model,log_cmd,...}}`** (СЛОЙ 1, `data/topics.json`, `save_topics` bot.py:192) — привязка канал→проект, вечная.
- `key_of(update)` (bot.py:200), `binding_for(update)` (bot.py:206) — резолв ключа `"chat:thread"`.

### Реестр проектов
- `build_registry()` (bot.py:154), `resolve_project(name)` (bot.py:166), `_home_sub(*parts)` (bot.py:122) — пути от `Path.home()` (без хардкодов `/home/igor`). Новый проект → алиас в `_REG_RAW` или авто-скан `~`.

### TG-команды (bot.py:859–1062)
`cmd_start · cmd_whoami · cmd_reset · cmd_resume · cmd_model · cmd_project · cmd_newtopic · cmd_diff · cmd_cost · cmd_usage · cmd_stop`. Хендлеры: `on_message`, `on_topic_created` (авто-привязка проекта), `on_error`.

### Рендер/утилиты
- `md_to_html(text)` (bot.py:278) + `_render_code_block` (bot.py:264) — markdown→TG-HTML со сворачиванием кода. ВСЕ ответы через него (иначе краш HTML parse_mode).
- `send()` (bot.py:244) + `_tg_call()` (bot.py:226) — отправка с ретраем транзиентных сбоев; `_smart_chunks` (bot.py:347) — нарезка по `TG_CHUNK=4000`.
- `audit()` (bot.py:390) + `_is_destructive()` (bot.py:385) — audit-лог full-auto в `data/audit/`.

### Старт
- **`_on_start(app)` (bot.py:976)** — post_init: поднимает `webapp.start(app, ctx)`. **Здесь формируется `ctx`** — словарь ссылок на общее состояние, передаётся в webapp.
- `main()` (bot.py:996) — сборка PTB-приложения, регистрация хендлеров, `_load_env()`.

---

## Кокпит: `webapp.py` (~3730 строк, 57 роутов)

aiohttp-сервер. **НЕ импортит `bot.py`** (двойное состояние!) — всё получает через `ctx` (передан из `bot.py:_on_start`).

- **`AppCtx(TypedDict, total=False)` (webapp.py:1479)** — типизация `ctx`: `topics/sessions/running/resolve_project/run_engine/DATA/HERE/...`. Рантайм — обычный dict, аннотация для читаемости. **Хочешь понять что доступно из webapp — смотри AppCtx.**
- `start(app, ctx)` — регистрация всех роутов + middleware. **Полный список роутов → `docs/API.md`.**
- **Auth:** cookie `cops_auth`, `_derive_token` на `hashlib.scrypt` (соль `WEB_COOKIE_SALT`), `secure/httponly/samesite`, rate-limit 5 fail/5min → 429. Middleware на `/api/*` кроме `/api/health`, `/api/login`.

### Ключевые группы хендлеров (имена `api_*`)
| Область | Хендлеры | Примечание |
|---|---|---|
| Проекты | `api_projects`, `api_new_project`, `api_rename`, `api_health`, `api_git_sync` | rename мигрирует SDK-сессии+Timeline (`_migrate_cwd_keyed_state`) |
| Настройки (f2ba02) | `api_settings_get/post` (глобальные `data/settings.json`), `api_project_settings_get/post` (topics.json) | `_get_global_setting`/`_git_enabled`/`_effective_default_model`; git_enabled=false → run-mode legacy |
| Доска/Tasks | `api_project_tasks`, `api_create_task`, `api_move_task`, `api_delete_task`, `api_update_task`, `api_card_run`, `api_tasks_done` | `card_id` валидируется `_valid_card_id`/`_CARD_ID_RE` |
| Автозапуск | **`_run_card`** → `_write_sidecar` + `_move_card_after_run` + `_notify_tg` | разбит на 3 хелпера. Перенос в In Progress → run_engine |
| Чат/SSE | `api_project_chat`, `api_chat_stop`, `_sse_stream`, `api_activity_stream` | общий `_sse_stream` |
| Файлы | `api_project_files`, `api_project_file`, `api_global_files`, `api_global_file` | общий `_read_file_content`; анти-traversal `_resolve_safe`/`_resolve_global_safe` |
| Промты | `api_prompts` (CRUD) | `data/prompts.json` |
| Сессии | `api_sessions`, `api_session` (new/resume), `api_session_history`, `api_session_context` | общие с TG |
| Usage | `api_usage` | oauth-эндпоинт, кэш 60с |
| Память проекта | `api_project_memory` (GET), `api_project_memory_write` (POST), `api_project_memory_delete` (DELETE) | Путь: `<cwd>/.claude-ops/memory/` (новое) + fallback на `~/.claude/projects/<cwd>/memory/` (старое). Агент пишет через обычный Write. Хелперы: `_project_memory_dir`, `_memory_read_all`, `_memory_write`, `_memory_delete`, `_memory_reindex`. Имена — `_valid_memory_name` (slug-regex). |
| **Секреты проекта** (Spec 007) | `api_project_secrets` (GET), `api_project_secrets_set` (POST), `api_project_secrets_delete` (DELETE) | Путь: `<cwd>/.claude-ops/secrets/secrets.env` (chmod 600, gitignored). **Значения НИКОГДА не возвращаются через API** — только имена ключей. Хелперы: `_project_secrets_path`, `_secrets_read`, `_secrets_write`, `_secrets_set`, `_secrets_delete`, `_secrets_ensure_gitignore`. Ключи — `_SECRETS_KEY_RE = ^[A-Z_][A-Z0-9_]*$`. Лимиты: 8KB/значение, 100 ключей. |
| **Timeline** (Spec 008) | `api_project_timeline` (GET) | Персистентная лента событий шины. Хелперы: `_timeline_init`, `_timeline_path`, `_timeline_append`, `_timeline_slug_from_cwd`, `_timeline_read_events`. Хук в `_bus_publish` — единая точка записи. Файл: `data/timeline/<slug>.jsonl` (+ `.jsonl.1` backup). env-поле никогда не записывается. |
| Прочее | `api_logs`, `api_claude_md`, `api_audit`, `api_upgrade`, `api_scan_errors`, `_ingest_errors_to_board` | |
| Subprocess | `_run_log_cmd`, `_run_test_cmd`, `api_project_logs` | `create_subprocess_exec(*shlex.split())` — НЕ shell |

---

## Фронтенд: `web/src/` (React + Vite + TS, 39 файлов)

```
web/src/
├── main.tsx                  точка входа
├── App.tsx                   корень: проекты, табы, polling
├── api.ts                    HTTP-клиент (VITE_BACKEND_URL || localhost:8787)
├── types.ts                  типы (ChatSSEEvent и пр.)
├── i18n/
│   ├── ru.ts                 ~110 ключей UI-строк
│   └── index.ts              export const t = ru
├── lib/
│   └── storage.ts            readLS/writeLS (localStorage)
├── hooks/
│   ├── useChatStream.ts      ⭐ SSE-стрим чата (reader, chunk-safe парсинг)
│   ├── useAsyncLoad.ts       generic loading/error/data
│   ├── useClickOutside.ts
│   ├── useProjectActivity.tsx  шина активности
│   └── useUnreadTracker.ts
├── components/
│   ├── ProjectView.tsx       контейнер проекта (табы слева + чат справа)
│   ├── ProjectTabBar.tsx · Sidebar.tsx (DnD)
│   ├── ChatTab-части:        ToolBlock · SessionSelector · SessionContextPanel
│   ├── FileExplorer.tsx      ⭐ общий для Files/GlobalFiles
│   ├── Modal.tsx · ConfirmModal.tsx · Toast.tsx
│   ├── ErrorBoundary.tsx     ⭐ оборачивает ProjectView + каждый таб
│   ├── PromptPicker · SkillPicker · UsageBadge · ProjectStructureCard
│   ├── EditableMarkdown · HealthDot · LoginScreen · Spinner
├── tabs/                     overview | claude-md | logs | board | files | memory | secrets | timeline
│   ├── ChatTab.tsx           ядро + useChatStream
│   ├── BoardTab.tsx          канбан (isActive-guard на polling)
│   ├── FilesTab / GlobalFilesTab  (тонкие обёртки над FileExplorer)
│   ├── OverviewTab · LogsTab · MemoryTab · ClaudeMdTab · SecretsTab
│   └── TimelineTab.tsx       ⭐ Spec 008: история из GET /timeline + live via useProjectActivity (SSE reuse)
└── styles/                   ⭐ styles.css разбит на 11 partials
    ├── base.css (vars/тема/light) · layout · sidebar · tabbar · overview
    └── board · chat · files · modal · forms · timeline
        (корневой styles.css = 11 @import в порядке каскада)
```

---

## Timeline — персистентность шины (Spec 008)

Каждое событие `_bus_publish(session_key, event)` дополнительно записывается в JSONL-лог проекта.

**Архитектура:**
- Единая точка записи — хук в `_bus_publish` вызывает `_timeline_append(session_key, event)`.
- `_timeline_init(ctx)` — вызывается из `start()`, сохраняет `DATA/timeline/` path и ссылку на `ctx["topics"]` в модульные переменные `_TIMELINE_DATA_DIR` / `_TIMELINE_TOPICS`.
- `_timeline_path(session_key)` — резолвит `session_key → cwd` через `_TIMELINE_TOPICS`, строит путь `DATA/timeline/<slug>.jsonl`. Если session_key не найден — `_unknown.jsonl`.
- `_timeline_slug_from_cwd(cwd)` — `cwd.replace('/', '-')`, аналогично `_sdk_sessions_dir`.
- `_timeline_append(session_key, event)` — добавляет `ts=time.time()`, обрезает `text` >2000 симв, исключает поле `env` (никогда), ротация >5MB → `.jsonl.1`. Глотает ВСЕ исключения.
- `_timeline_read_events(session_key, limit, before)` — читает `.jsonl` + `.jsonl.1`, парсит gracefully (битые строки → skip), сортирует по ts, пагинирует.

**Безопасность:** `env`-поле исключается в `_timeline_append` — секреты проекта не попадают в лог. Проверено тестом `test_api_timeline_env_not_in_response`.

**Фронт:** `TimelineTab.tsx` — история via `GET /api/projects/{id}/timeline`, live через `useProjectActivity` (переиспользует существующий SSE-коннект, новый сокет НЕ открывается).

---

## Поток env-секретов (Spec 007)

Секреты проекта подмешиваются в env агента при КАЖДОМ запуске `run_engine`:
- **TG-канал** (`bot.py:run_agent`): `{**_secrets_read(cwd), "TG_CHAT_ID":..., "TG_THREAD_ID":...}` — TG-переменные всегда перетирают секреты с теми же именами (приоритет).
- **Кокпит чат** (`webapp.py:api_project_chat`): `env=_secrets_read(cwd)`.
- **Карточки** (`webapp.py:_run_card`): `env=_secrets_read(cwd)` из cwd основного проекта (не worktree).
- Агент видит секреты как `os.environ["MY_KEY"]` — стандартные переменные окружения.
- Секреты НЕ логируются в `audit()` — тот принимает только (project, kind, text). env туда не передаётся.
- Секреты НЕ попадают в транскрипт/сессии/сайдкары — они в env процесса, не в тексте.

---

## Тесты: `tests/` (21 файл, 496 passed / 6 skipped)

`venv/bin/python -m pytest -q` (или `make test`). Фикстуры — `conftest.py` (aiohttp client, tmp-cwd, mock ctx, `_auth_token`).
- **Критичное:** `test_board_parser` (регрессия = потеря задач в проде), `test_security` + `test_security_regressions` (path-traversal, card_id, rate-limit), `test_board_api`, `test_run_card`, `test_chat_sse`, `test_project_rename`, `test_ingest_errors`.
- **Новое (Spec 007):** `test_secrets` — 47 тестов: путь, round-trip, chmod 600, gitignore, валидация ключей, лимиты, изоляция cwd, audit non-leak, API GET/POST/DELETE с критичным тестом на не-утечку значений.
- **Новое (Spec 008):** `test_timeline` — 32 теста: slug стабильность, path резолв, append+ts+truncate+env-exclusion, ротация 5MB, bus_publish интеграция, read graceful (битые строки), backup .jsonl.1, API GET/limit/before/env-not-in-response.
- **Новое (Spec 010):** `test_self_healing` — 28 тестов: `_self_heal_enabled` (флаг/env/default False); heal_attempted мета; OFF default = критичный регрессия-страж; heal_attempted ДО прогона; safe→Review, risky→Failed; heal_attempted не перезапускается; не-git→пропуск; занятый→пропуск; лимит конкурентности; Timeline self_heal; API-тумблер (auth/enable/disable/404).

---

## Данные и эксплуатация

- `data/topics.json` (СЛОЙ 1, вечный; per-project настройки: model/self_heal/notify_on_error/log_cmd/test_cmd/git_enabled) · `data/sessions.json` (СЛОЙ 2, /reset чистит) · `data/settings.json` (глобальные настройки f2ba02, mtime hot-reload) · `data/prompts.json` · `data/runs/<card>.md` (сайдкары) · `data/audit/` · `data/inbox/` (файлы из TG) · `data/timeline/<slug>.jsonl` (Timeline Spec 008). **`data/` в .gitignore.**
- `.env` (секреты, не в git) · `.env.example` + `web/.env.example` (плейсхолдеры).
- `claude-ops-bot.service` (systemd) · **`restart-self.sh`** (ЕДИНСТВЕННЫЙ способ рестарта из агента — detached через systemd-run; подробности в CLAUDE.md).
- `TASKS.md` (доска, читают сессии) · `DONE.md` (архив, сессии НЕ читают) · `docs/API.md` · `CONTRIBUTING.md` · `LICENSE` (MIT).

---

## Петля самолечения (Spec 010)

Соединяет уже готовые кирпичи в автономный цикл. **Агент готовит — человек применяет.**

```
сканер ловит падение → создаёт err-карточку (уже работало)
  → [НОВОЕ] _self_heal_enabled(project)? → да
  → asyncio.create_task(_self_heal_card(ctx, project, card))
      1. heal_attempted=true в description ДО запуска (предотв. зацикливание)
      2. Формируем heal_prompt из title + excerpt инцидента
      3. _card_run_mode → worktree? → нет → skip (предохранитель №5)
      4. _card_worktree_setup → .worktrees/card-<id>
      5. ctx["running"][session_key] = True (блокируем TG от параллельного запуска)
      6. Перемещаем карточку в In Progress
      7. _run_card(..., worktree, wt_info) → агент чинит, авто-коммит
         → _run_card снимает running в finally, переносит в Review/Failed
      8. _run_quality_gate(wt_path) → verdict safe/risky/unknown
      9. safe → остаётся в Review + heal_badge ✓; risky → Failed + heal_badge ✗
     10. Timeline kind:"self_heal" phase:start/fixed/gate_ok/gate_fail
     11. TG-пинг Игорю (результат)
```

**Предохранители (никогда не нарушать):**
1. `_self_heal_enabled` = False по умолчанию — только `self_heal: true` в topics или `SELF_HEAL_ENABLED=1`
2. `api_card_apply` НИКОГДА не вызывается из самолечения — только до Review
3. `heal_attempted=true` пишется ДО запуска агента — краш не зациклит
4. `_self_heal_active_count <= _SELF_HEAL_MAX_CONCURRENT (2)` — глобальный счётчик
5. `_card_run_mode == "worktree"` обязателен — не-git/dirty пропускается
6. Timeline `kind:"self_heal"` + TG-пинг — полная наблюдаемость

**Ключевые функции (webapp.py):**
- `_self_heal_enabled(project)` — читает флаг; по умолчанию False
- `_send_tg_ping(ctx, project, msg)` — TG-уведомление Игорю
- `_self_heal_card(ctx, project, incident_card)` — асинхронная петля починки
- `_error_scanner_loop` — интеграция: после scan_and_ingest → create_task если включено
- `api_project_self_heal_toggle` — POST `/api/projects/{id}/self-heal {enabled}`

---

## Поток одной задачи (end-to-end)

```
TG-сообщение / карточка→In Progress / веб-чат
  → резерв running[cwd] (синхронно)
  → (карточка C2) режим-детектор: git+clean → worktree, иначе legacy
  → (worktree) git worktree add .worktrees/card-<id> -b card-<id>
  → run_engine(cwd=effective_cwd) гоняет SDK, yield events
  → адаптер рендерит (TG: send+md_to_html / web: SSE / карточка: sidecar)
  → (worktree) авто-коммит в ветке card-<id>, diff vs base_branch
  → session_id сохранён, running снят в finally
  → (карточка) → Review/Failed + пинг TG
  → (C2-gate) пользователь в Review видит diff + кнопки:
      🧪 Проверить → POST /check → _run_quality_gate(wt_path) → вердикт safe/risky/unknown
                     (тесты гоняются В worktree; apply НЕ блокируется — пользователь решает)
      ✓ Применить → git merge --no-ff card-<id> → Done (worktree удалён)
      ✗ Отмена    → worktree+ветка удалены → Backlog
      Конфликт    → 409, merge --abort, worktree жив, карточка остаётся в Review
```

### C2-gate: файлы
- `data/runs/<card_id>.md` — человекочитаемый сайдкар (ответ агента, diff)
- `data/runs/<card_id>.json` — машиночитаемые мета (mode, branch, wt_path, has_changes, applied, discarded, gate:{verdict,ts})

---
created: 2026-06-04
status: design (нужны продуктовые решения Игоря до реализации)
---

# Spec 013 — Multi-user / Multi-tenant

> **Статус: ДИЗАЙН, НЕ РЕАЛИЗАЦИЯ.** Это самый крупный архитектурный сдвиг проекта.
> До кода нужны ответы Игоря на «Открытые вопросы» (раздел в конце) — прежде всего
> модель Claude-кредов. Без неё мультиюзер не взлетает.

## Зачем

Сейчас claude-ops-bot — строго **single-tenant**: один пользователь (Игорь), одна
Claude-подписка, один `WEB_PASSWORD`, один `$HOME`. Для OSS это нормальный режим по
умолчанию («one bot per one Claude subscription»), но для команд/семьи/SaaS нужен
мультиюзер. Этот спек проектирует переход — **правильно**, чтобы не переписывать
дважды. Связан с [[spec-014-oss-hardening]] (де-хардкод — предпосылка) и с
ui-state namespace-хуком (`_ui_state_ns()` в webapp.py — уже заложен под user_id).

## Текущее состояние (факты)

### Аутентификация кокпита — бинарная, без идентичности
- `webapp.py:auth_middleware` — проверяет cookie `cops_auth` на каждый `/api/*`
  через `hmac.compare_digest` с одним `ctx["_auth_token"]` (scrypt от `WEB_PASSWORD`).
- `api_login` сравнивает с единственным `ctx["password"]`; `api_me` → `{"authed": True}`.
- Cookie **не несёт идентичности** — только «знает пароль / нет». Нет понятия user.
- `LoginScreen.tsx` — одно поле password, без username.

### Telegram-канал
- `bot.py: ALLOWED_USERS = {int(x) for x in os.environ["ALLOWED_USERS"].split(",")}`.
- `authorized(update) → u.id in ALLOWED_USERS` — единственная проверка.
- `key_of(update) → f"{chat_id}:{thread_id}"` — ключ топика → `topics[key]`.

### Single-tenant в данных (всё плоское, без user_id)
`data/topics.json` (`"chat:thread" → {project,cwd,model}`), `sessions.json`,
`prompts.json`, `settings.json`, `data/audit/`, `data/timeline/<slug>.jsonl`,
`data/runs/`, `data/inbox/`, `<cwd>/.claude-ops/{memory,secrets}` — ни в одной
структуре нет ключа пользователя.

### In-memory глобалы процесса (синглтоны)
`_bus` / `_bus_global` (шина SSE — любой подписчик видит события всех),
`_QUEUE` (очередь карточек), `_self_heal_active_count` + `_SELF_HEAL_MAX_CONCURRENT`,
`_GIT_VIS_CACHE`, `_login_attempts`, `_incident_push_history`, `running[session_key]`.

### Файловая модель проектов
`build_registry()` сканирует `Path.home()`; `api_new_project` создаёт в
`Path.home()/projects/`. `_resolve_safe`/`_resolve_global_safe` ограничивают
traversal в рамках cwd/`$HOME`, но **не проверяют принадлежность проекта юзеру**.

### Claude-креды (ключевой факт)
SDK работает **только на OAuth-подписке**: `ANTHROPIC_API_KEY` явно `pop()`-ается
(`bot.py`). Креды берутся из `~/.claude/.credentials.json` одного OS-пользователя
(`HOME=/home/igor` в unit). Один процесс → один токен → одни лимиты (5h/7d).

## Блокеры мультиюзера (по убыванию критичности)

1. **Один Claude-токен на процесс.** `~/.claude/.credentials.json` — один файл, SDK
   не принимает чужой OAuth-токен. N юзеров → общий биллинг и общие лимиты Игоря.
   **Это центральный архитектурный блокер.**
2. **Нет user identity ни в одном слое.** `ctx["password"]` один; `topics/sessions`
   без `user_id`. Введение user_id протягивается через все ~57 роутов.
3. **`bypassPermissions` + общий `$HOME`.** Агент юзера A с полным доступом к ФС
   может прочитать `~/.claude/.credentials.json`, `secrets.env` чужих проектов,
   `~/.claude/projects/` (история SDK), vault. Нет изоляции между агентами.
4. **`_bus_global` доставляет события всем.** Глобальный SSE-стрим `/api/activity`
   отдаёт ход задач юзера A юзеру B. Нужна фильтрация по user_id.
5. **Плоские файлы `data/`.** audit/runs/timeline/prompts/settings — без namespace.
6. **Реестр проектов привязан к одному `$HOME`.** `build_registry()` + `api_new_project`.
7. **Глобальные счётчики/кэши.** `_self_heal_active_count` (лимит на всех),
   `_GIT_VIS_CACHE`, rate-limit по IP, а не по юзеру.
8. **Изоляция SDK-сессий** — по физическому cwd; не enforced на уровне юзера.

## Центральная развилка: модель Claude-кредов

| | A. Bring-Your-Own API Key | B. Shared subscription | C. Per-user OAuth |
|---|---|---|---|
| Как | каждый юзер даёт `ANTHROPIC_API_KEY`, `run_engine(api_key=...)` | один OAuth-токен на всех | у каждого свой `~/.claude` (свой HOME при вызове SDK) |
| Биллинг | честный, per-user | всё на Игоря | честный, per-user |
| Лимиты | независимые | общие (быстро упираются) | независимые |
| Подписка Max/Pro | ❌ только API-план | ✅ работает | ✅ |
| Сложность | низкая | нулевая | высокая (OS-профили/HOME-swap) |
| Кому | **OSS по умолчанию** | self-host, 3-5 доверенных | production-grade |

**Рекомендация:** **Вариант A** как дефолт OSS (прозрачно, просто, масштабируемо),
с документированным Вариантом B для маленьких доверенных инсталляций. C — опционально
для production. Цена A: и Игорю придётся перейти на API-ключ (или поддерживать оба пути:
single-tenant=OAuth-подписка, multi=API-ключи).

## Предлагаемая архитектура

### Identity: users-реестр + per-user креды
`data/users.json` (gitignored): `{username: {password_hash(scrypt), claude_auth,
claude_api_key, home_dir, tg_user_ids:[...]}}`. Без OAuth/SSO в Ф1 — простой
self-host-friendly multi-account.

### Namespacing: `user_id` — первичный ключ
```
data/users.json
data/users/<user_id>/{topics,sessions,prompts,settings}.json
data/users/<user_id>/{audit,runs,timeline,inbox}/
projects/<user_id>/<slug>/.claude-ops/{memory,secrets}
data/shared/settings.json            # admin-глобальные
```
В `ctx` добавляется `user_id`; все хелперы (`_collect_projects`, `_timeline_path`,
`_data_path(user_id, *parts)`, …) фильтруют по нему.

### Auth: cookie несёт user_id
JWT (`sub=user_id`) или scoped-token `scrypt(f"{user_id}:{password}")`.
`auth_middleware` кладёт `request["user_id"]`. TG: `ALLOWED_USERS` → dict
`{tg_user_id: username}`; `authorized()` возвращает user_id или None;
`key_of` → `f"{tg_user_id}:{chat_id}:{thread_id}"`.

### Изоляция агентов
- **Ф1 (простая):** scoped root `projects/<user_id>/`; `_resolve_safe` дополнительно
  проверяет `cwd.startswith(user_root)`. bypassPermissions остаётся, но физически
  заперт в директории юзера.
- **Ф2 (сильная, для production):** OS-уровень — `sudo -u <user>` / Docker-namespace /
  отдельный контейнер на юзера. Полная изоляция ядром.

## Фазированный rollout

**Ф0 — Подготовка (невидимо для юзера, `user_id="default"`):**
- Все обращения к `topics/sessions` — через хелперы с `user_id`-параметром (пока `"default"`).
- Все файловые пути — через `_data_path(user_id, *parts)`.
- Убрать прямой `Path.home()` из `api_new_project`.
- `_ui_state_ns()` уже готов (точка свапа на user_id).

**Ф1 — Multi-account (минимальный мультиюзер):**
- `data/users.json`; `auth_middleware` → cookie с user_id; `ctx["user_id"]`.
- `_collect_projects` фильтрует по user_id; `api_new_project` → `projects/<user_id>/`.
- `run_engine(api_key=...)` (Вариант A); `_bus_global` фильтрует по подписчику.
- TG: `ALLOWED_USERS` → dict; `authorized()` → user_id.
- LoginScreen: добавить username.

**Ф2 — Full isolation:**
- Миграция исторических данных в `users/<id>/`.
- Per-user `_self_heal_active_count`, `_GIT_VIS_CACHE`, rate-limit.
- Admin-UI (управление юзерами); tenant-aware лимиты.
- (Опц.) OS-level изоляция агентов.

## Открытые вопросы (решения Игоря — БЛОКИРУЮТ реализацию)

1. **Модель Claude-кредов:** A (BYO API key) / B (shared) / C (per-user OAuth)?
2. **Self-hosted only или SaaS?** SaaS требует OS-изоляции агентов (Ф2 Вариант 2).
3. **Scope:** несколько доверенных (семья/команда) vs публичный multi-tenant (незнакомцы)?
4. **Приватность между юзерами:** полная изоляция всегда, или возможны shared-проекты?
5. **Admin-роль:** нужен ли суперюзер (видит всё, управляет юзерами)?
6. **TG:** одна группа на всех (топики разграничивают) vs группа на юзера?
7. **Обратная совместимость:** мигрировать текущие данные Игоря в `users/default/`?

## Не-цели (явно вне скоупа)
- OAuth/SSO провайдеры (GitHub/Google) — пост-Ф2.
- Не-Telegram транспорты (Discord/Slack/Matrix) — отдельный спек.
- Биллинг/подписки SaaS — отдельный продукт.

## Связанные
- [[spec-014-oss-hardening]] — де-хардкод (предпосылка; `_REG_RAW`, `VAULT_PROJECTS` и пр.).
- [[spec-004-oss-release]] — стратегия OSS-релиза (multi-user там помечен «пост-релиз»).
- `webapp.py: _ui_state_ns()` — заложенная точка свапа `"default"` → `user_id`.

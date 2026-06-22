---
created: 2026-06-04
status: in-progress (исполняется агентами 2026-06-04)
---

# Spec 014 — OSS-hardening (де-хардкод и санитизация)

> Конкретизирует «Фазу A (санитизация)» из [[spec-004-oss-release]] до уровня
> `file:line` + исполнимых задач. Это **исполняемый** спек: агенты выполняют Ф1–Ф3
> автономно. Ф4 (rewrite истории, переключение PUBLIC) — **ГЕЙТ, только руками Игоря**.

## Зачем
Репо `Zira777ru/claude-ops-bot` — **PRIVATE**. Чтобы однажды открыть его, нужно убрать
из трекаемых файлов всё, что (а) привязывает код к личной инсталляции Игоря и сломает
чужую, (б) раскрывает персональные данные/OPSEC. Цель — **portable + безопасно**, не
сломав работающий инстанс Игоря.

## Принципы безопасности (незыблемо)
1. **Никаких ключей.** Реальные секреты уже в gitignored `.env` (BOT_TOKEN, GROUP_CHAT_ID,
   WEB_PASSWORD, WEB_COOKIE_SALT) — НЕ в git-истории, `.env.example` = только плейсхолдеры.
   Де-хардкод **не извлекает живые креды** — только персонализацию (пути/ID/имена).
   Ни один реальный секрет/значение не попадает в трекаемые файлы и не печатается в лог.
2. **Runtime сохраняется.** Любое значение, которое сейчас захардкожено и нужно боту,
   переносится в gitignored конфиг (`.env` / `data/registry.json`) с **реальным значением
   Игоря**, а в трекаемый код идёт чтение из конфига + плейсхолдер/`.example`. Бот после
   рестарта работает идентично.
3. **Репо остаётся PRIVATE.** Переключение в PUBLIC — НЕ в этом спеке (явный запрет).
4. **Без rewrite истории автономно.** `git-filter-repo` (force-push) — деструктив,
   только по явному решению Игоря (Ф4).
5. **pytest зелёный + import OK + build OK** до любого рестарта.

## Инвентарь (категории → `file:line`)

### BLOCKER (убрать/параметризовать до публикации)
- `CLAUDE.md` (репо): личный TG ID, путь к userbot-сессии `…/secrets/tg.session`,
  упоминание OPSEC-проекта `example-portal`.
- `bot.py: _REG_RAW` (~108–130): полный список личных проектов оператора (rightforms,
  example-project, even-g2, …) вшит как static registry.
- `bot.py: VAULT_PROJECTS = Path.home()/"vault"/"01-Projects"` (~1010) + webapp.py TypedDict —
  личная PARA-структура vault.
- `bot.py: TELEGRAM_NUDGE` (~81–90): имя «Игорь» ×3 + директива «по-русски» в системном
  промпте каждой сессии.
- `specs/spec-011…`: упоминание `example-portal` (OPSEC).

### Обязательно, но не блокер (сломает чужую инсталляцию)
- `claude-ops-bot.service`: `User=youruser`, `Group=youruser`, все пути `/home/youruser/…`,
  `PATH=/home/youruser/.npm-global/…`.
- `web/src/tabs/LogsTab.tsx:61,64,66,67`: захардкоженный `/home/youruser/claude-ops-bot/
  data/topics.json` + имя `igor` в prompt-тексте для агента.
- `README.md` / `ARCHITECTURE.md` / `CONTRIBUTING.md`: `@ziraclaudebot`, `Zira777ru`,
  `claude-ops.example.com`, `192.168.0.114:8787`, `/home/youruser/…`.
- `templates/` + `templates/reference/`: «Игорь», `/home/youruser/`, `~/vault/`,
  `firecrawl.example.com`, имена проектов как примеры.

### Nice-to-have
- i18n: вынести язык в `RESPONSE_LANGUAGE`/UI-locale (структура `i18n/ru.ts` есть; en + picker).
- Имя «Игорь» в docstrings/комментариях кода (`webapp.py:2358`, `bot.py:298`, …) → «operator».
- Примеры имён проектов в шаблонах → generic `my-project`.

## Фазы исполнения

### Ф1 — Параметризация кода (runtime-safe)
Каждый шаг: сначала перенести реальное значение Игоря в gitignored конфиг, потом
заменить хардкод чтением из конфига.

1. **`_REG_RAW` → `data/registry.json`** (gitignored). Создать `data/registry.json` с
   текущим содержимым `_REG_RAW` (реальные алиасы Игоря). В `bot.py`: `_REG_RAW = {}` +
   загрузка из `data/registry.json` если есть. Добавить `data/registry.example.json`
   (generic пример). `build_registry()` авто-скан `$HOME` остаётся.
2. **`VAULT_PROJECTS` → env.** Добавить `VAULT_PROJECTS` в `.env` Игоря со значением
   `/home/youruser/vault/01-Projects`; код читает env, дефолт пусто = фича отключена.
   Добавить в `.env.example` с комментарием.
3. **`OPERATOR_NAME` + `RESPONSE_LANGUAGE` → env.** В `TELEGRAM_NUDGE` подставлять
   `OPERATOR_NAME` (дефолт нейтральный, напр. «the operator») и `RESPONSE_LANGUAGE`
   (пусто = без языковой директивы). В `.env` Игоря: `OPERATOR_NAME=Игорь`,
   `RESPONSE_LANGUAGE=ru`. В `.env.example` — нейтральные дефолты.
4. **`LogsTab.tsx`** — убрать `/home/youruser/...` и имя hardcoded username из prompt-текста (динамика/
   относительные пути / `$(whoami)`).
5. **`claude-ops-bot.service` → `claude-ops-bot.service.template`** с systemd-спецификаторами
   (`%h`, `User=__USER__`) или плейсхолдерами. **Живой unit в `/etc/systemd/` НЕ трогать.**
6. **`.env.example`** — дописать новые переменные (`VAULT_PROJECTS`, `OPERATOR_NAME`,
   `RESPONSE_LANGUAGE`) с безопасными дефолтами и комментариями.
7. Тесты на загрузку `registry.json` + env-фолбэки; pytest зелёный.

### Ф2 — Санитизация документации (без runtime-эффекта)
8. `README.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`: заменить `@ziraclaudebot` →
   `@YOUR_BOT`, `Zira777ru` → `YOUR_GITHUB`, `claude-ops.example.com` → `YOUR_DOMAIN`,
   `192.168.0.114:8787` → `localhost:8787`, `/home/youruser/…` → относительные/`$HOME`.
9. `templates/` + `templates/reference/`: «Игорь» → «оператор/you», `/home/youruser/` →
   `$HOME/`, `~/vault/` → обобщить, `firecrawl.example.com` → `YOUR_FIRECRAWL_URL`,
   примеры проектов → generic. **Сохранить технический смысл** (это рабочие шаблоны).
10. `CLAUDE.md` (репо): убрать личный TG ID, путь к userbot-сессии, OPSEC-имя
    `example-portal`. **Сохранить технические gotchas** — это операционный гайд агентов,
    не выхолащивать. Личные имена оператора в gotchas → «оператор», где не теряется смысл.
11. (nice) Имя «Игорь» в docstrings/комментариях кода → «operator/you».

### Ф3 — Верификация
12. `python -c "import bot, webapp"` — импорт без ошибок.
13. `pytest -q` — всё зелёное (включая новые тесты Ф1).
14. `cd web && npm run build && npm run lint` — чисто.
15. Grep-аудит трекаемых файлов: не осталось `282311426`, personal domain leaks,
    `192.168.`, `Zira777ru`, `/home/youruser`, `ziraclaudebot` (кроме самого этого спека
    и spec-013, где они в инвентаре). Отчёт — что осталось и почему.

## ГЕЙТ Ф4 — только руками Игоря (НЕ автономно)
- **Rewrite git-истории** (`git-filter-repo --replace-text` для `282311426`): деструктив,
  force-push, ломает форки. ID — не секрет (публичный TG user id); вариант «принять как
  есть + `SECURITY.md`» допустим. Решает Игорь.
- **Переключение репо в PUBLIC** + `LICENSE` (MIT/Apache) + первый release. Только Игорь.
- Финальный ручной просмотр diff санитизации перед публикацией.

## Чеклист «перед PUBLIC» (для Игоря, потом)
- [ ] Ф1–Ф3 сделаны, pytest/build зелёные, grep-аудит чист.
- [ ] Решена модель Claude-кредов (см. [[spec-013-multi-user]]) — или явно «single-tenant only».
- [ ] `LICENSE` добавлен.
- [ ] Git-история: rewrite или осознанно «как есть» + `SECURITY.md`.
- [ ] Ручной просмотр финального diff.
- [ ] `git remote` PUBLIC переключение.

## Связанные
- [[spec-004-oss-release]] — стратегия (этот спек = его Фаза A в деталях).
- [[spec-013-multi-user]] — мультиюзер (де-хардкод — предпосылка).

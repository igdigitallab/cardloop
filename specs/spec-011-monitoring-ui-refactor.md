# spec-011 — Рефакторинг: мониторинг через ошибки + перестройка UI + чистка

> Статус: ПЛАН (не реализовано). Источник: аудит 4 агентами 2026-06-03 (bot.py, webapp.py, фронт, observability/шаблоны) + серия решений Игоря.
> Истина по коду — `ARCHITECTURE.md` / `docs/API.md`. Этот файл — что и зачем менять, по фазам.

## Зачем (три большие идеи)

1. **Мониторинг — на РЕАЛЬНЫХ ошибках рантайма, а не на слепом прогоне тестов.** Сейчас `_error_scanner_loop` гоняет `test_cmd` каждые 5 мин (claude-ops и networking-os — весь сьют 500+ тестов круглосуточно). Это расточительно и плодит ложные инциденты. Правильная модель: каждый проект имеет **error handler → пишет ошибку в лог**, кокпит ловит её через `log_cmd`. Тесты — только по кнопке.
2. **UI — три зоны с одной ролью у каждой.** ШАПКА (постоянный контекст+действия), ОБЗОР (read-only дашборд), НАСТРОЙКИ (все крутилки + ключи). Сейчас Обзор и Настройки дублируются.
3. **Чистота.** Снести мёртвый код/дубли, закрыть дыры в тестах и error-handling.

---

## ФАЗА 0 — Безопасные фиксы багов + наблюдаемость (делать первой, риск низкий)

**Backend bot.py:**
- **[P1] running-утечка:** `bot.py:~731` — `send_chat_action`/`create_task` после `running[k]=True` без try/except. Сбой TG API → слот занят навсегда (топик заблокирован до рестарта). Обернуть в try/except → `running.pop(k); return`. **S**
- **[P1] watchdog:** `bot.py:~545` — при `watchdog_stall_sec < 20` порог не сработает (sleep=20с больше порога); текст «0 мин» при <60с. Адаптивный sleep + текст в секундах. **S**
- **watchdog не репортит срабатывание** — после `interrupt()` нет TG-пинга/`print`. Добавить немедленный пинг (chat/thread есть в scope `run_agent`) + `print`. **M**
- **двойной `running.pop`** (`run_agent.finally` + `safe_run.finally`) — оставить один. **S**
- **мёртвые импорты** `ThinkingBlock`, `ToolResultBlock` (bot.py:27-28), `from aiohttp import web` (bot.py:31). **S**

**Backend webapp.py:**
- **[P1] мёртвый дубль `_NEW_PROJECT_PROMPT`** (webapp.py:2644 — старый без `{cwd}`; живой на 5072). Снести 2644-блок. **S**
- **req.json() без try/except:** `api_prompt_create` (~421), `api_prompt_update` (~443), `api_global_file_write` (~3798). Обернуть как у соседей. **S**
- **дублированный блок worktree-переменных** в `_run_card` (~2598-2614). Убрать второй блок. **S**
- **[P1 observability] глобальный error-middleware для aiohttp** — сейчас только `auth_middleware`; необработанное исключение хендлера → пустой 500 без лога. Добавить middleware: `except Exception → logging.exception + json {"error":...,"request_id":...}`. Это и есть «error handler самого кокпита». **M**

**Khronika — вынести из claude-ops-bot (РЕШЕНО):**
- `scripts/khronika-web-logs.sh` (untracked, не в git) → перенести туда где ей место: vault/Homelab или log_cmd самого проекта Khronika. ClaudeOps и Khronika — НЕсвязанные проекты, чужую специфику в репо оркестратора не держим. После переноса — поправить `log_cmd` Khronika в topics.json, чтобы логи в кокпите не отвалились. **S**

---

## ФАЗА 1 — Сдвиг модели мониторинга (тесты off → ошибки из логов)

**Выключить авто-прогон тестов:**
- `_scan_project_errors` (webapp.py:~1314) — удалить ветку `if test_cmd:` (1327-1333). Сканер становится только лог-сканером.
- `_error_scanner_loop` (~2076) — из условия skip убрать `or test_cmd` (сканить только тех, у кого `log_cmd`).
- `api_project_scan_errors` guard (~1439) — убрать `not test_cmd`.
- `_run_test_cmd` остаётся ТОЛЬКО для кнопки «Прогнать тесты» (`api_project_test`) и quality gate. Если после правок нигде не зовётся из сканера — проверить и при необходимости удалить.

**Стандарт error handler в шаблонах/онбординге** (главное — чтобы ошибки попадали в логи):
- `templates/CLAUDE.md.tpl` — добавить секцию `## Error Handler` со сниппетами по типам: FastAPI/aiohttp (`@exception_handler`/middleware → `logging.ERROR` + traceback), PTB-бот (`add_error_handler`), CLI (`try/except main → logger.error+exit`), библиотека (логировать перед re-raise). Эталон — `networking-os/networking/crm/` (`@app.exception_handler` + `services/alerts.py`, rate-limit по `(path, exc_class)`, fire-and-forget TG).
- `_NEW_PROJECT_PROMPT` (5072) ШАГ 2 — пункт «если веб-сервис/бот → добавь глобальный error handler, иначе кокпит не видит рантайм-ошибки».
- `templates/TASKS.md.tpl` — стартовая карточка «Настроить log_cmd + глобальный error handler».
- **Стандартная лог-строка** `UNHANDLED exc_class=<...> path=<...>` + расширить `_parse_log_errors` маркером `UNHANDLED` (ловит инцидент даже без полного трейсбека, напр. из systemd `OnFailure`).

**ClaudeOps capability conformance — проект должен СООТВЕТСТВОВАТЬ возможностям кокпита (а не только иметь файлы).**
Сейчас health (`api_project_health`, 6 пунктов) проверяет ТОЛЬКО файлы (CLAUDE.md/cockpit-rules/TASKS/README/.gitignore/git). Проект может быть «6/6», но без хендлеров и log_cmd → кокпит к нему слеп, а его агент даже не знает, что должен их завести. Расширяем в две стороны:
- **(контракт — учим проект) `templates/CLAUDE.md.tpl`:** в «Правила работы в кокпите» добавить блок **«Возможности ClaudeOps — что подключить»**: error handler (ОБЯЗАТЕЛЕН для сервисов/ботов — пишет ошибки в лог), логи (log_cmd), тесты (test_cmd, по кнопке), самолечение (git+clean+test/gate), notify_on_error, healthz/liveness для сервисов. Чтобы агент проекта ЗНАЛ что завести. Плюс **само-декларация**: чеклист `## ClaudeOps conformance` который онбординг заполняет (handler: где/нет; log_cmd: да/нет; и т.д.).
- **(проверка — health-апгрейд) `api_project_health`:** добавить capability-пункты сверх файловых — log_cmd задан (из topics.json, легко)? test_cmd задан (опц.)? **error handler присутствует** (эвристика: grep по проекту `@app.exception_handler` / `add_error_handler` / `error_middleware` / `logging.*error` + само-декларация в CLAUDE.md)? git для самолечения? Health становится «файлы + возможности».
- **Показывать ТОЛЬКО недобор** (в Обзоре): всё подключено → скрыто; недобор → «доделай: нет log_cmd / нет error handler / …» с actionable-хинтами (можно → карточка в Backlog).
- **Полный набор для conformance:** хендлеры · логи · тесты · самолечение · память · секреты · notify · (сервисам) healthz.

---

## ФАЗА 2 — Перестройка UI (три зоны)

**Новая ШАПКА проекта** (рефактор существующего `ProjectView.tsx:~287-395`):
- git-чип (ветка/dirty/unpushed) + кнопка **Git sync постоянно** (убрать условие `gitNeedsSync`); индикатор «идёт агент» (из `useProjectActivity` run_start/run_end); счётчик инцидентов (`project.incidents` → чип, клик в Доску); кнопка **«Прогнать тесты»** (перенос `TestRunner` из Обзора). **Модель в шапку НЕ выносим** (решение: только в чате). Все — **S**, кроме общей сборки шапки **M**.

**ОБЗОР → чистый дашборд** (`OverviewTab.tsx`):
- убрать `SelfHealToggle` (306) и `NotifyOnErrorToggle` (309) — дубль с Настройками.
- убрать `IncidentScanner` плашку «авто-скан 5 мин» (173); саму кнопку ручного скана — в шапку или оставить read-only.
- `TestRunner` → в шапку (статус последнего прогона остаётся read-only в Обзоре).
- health-блок — условный (Фаза 1).

**НАСТРОЙКИ — все крутилки + Ключи** (`SettingsTab.tsx`):
- влить `SecretsTab` как секцию «Секреты проекта» (≈260 строк → `<section>`); убрать вкладку «Ключи».

**Вкладки 9 → 7:** `Обзор · Логи · Доска · Файлы · Память · Активность · Настройки(+Ключи)`.
- «Лента» → переименовать в **«Активность»** (`ru.ts:56`) — путается с «Логи».
- **CLAUDE.md** — РЕШЕНИЕ Игоря: отдельная вкладка или в «Файлы» (агент рекомендует оставить — частый доступ).

**Дедуп:**
- **модель — ТОЛЬКО в чате** (`ChatTab:~529`): убрать инфо-карточку модели из Обзора + select из Настроек; в шапку не добавлять. Селектор в чате — РЕАЛЬНЫЕ версии (Opus 4.8 / Sonnet 4.6 / Haiku 4.5 → model-id), реестр версий + (опц.) thinking mode (карточка 4df23a). **M**
- `projectHealth` — двойной fetch (`ProjectView:126` + `ProjectStructureCard:21`) → передавать `health` пропсом, убрать внутренний fetch. **S**

**Мёртвый фронт-код на снос:** `api.readme/saveReadme/specs/spec/activity` (api.ts — нет вкладок-потребителей); `DisabledTab` (ProjectView:457, не используется); `useUnreadTracker` (импортится, результат не используется — проверить); hardcoded-строки в OverviewTab → i18n; `LogsTab` — объединить `reload`+`useEffect` через `useAsyncLoad`.

---

## ФАЗА 3 — Чистка + покрытие тестами

**Housekeeping:** `data/runs/` — orphan-сайдкары (карточек уже нет) + `newproj.md` (тестовый артефакт) + `err-*` после закрытия. Прунить те, чьих `card_id` нет на доске (Review-карточки НЕ трогать). **S**

**Рефактор-долг webapp.py:** унифицировать `api_project_logs`(809) через `_run_log_cmd`(1273); ленивый `_usage_lock` (asyncio.Lock на модуле); вынести in-function импорты (`shutil`×3, `datetime`@4129) в топ; хранить ссылки на `create_task` (особенно `_error_scanner_loop`).

**Дыры в тестах (добавить):** `api_new_project` (не покрыт совсем — точка входа!); sessions API (`api_sessions/session/history/context`); free chats (`api_free_*`); роут `api_project_health` (тестируется только функция); `api_project_upgrade/audit`; `_run_log_cmd` timeout; `api_global_file` POST (traversal/`.env` на запись).

---

## Решения Игоря (ПРИНЯТЫ 2026-06-03)
1. **CLAUDE.md** — оставить ОТДЕЛЬНОЙ вкладкой (не трогать).
2. **Модель** — ТОЛЬКО в чате. Убрать из Настроек, инфо-карточку из Обзора, в шапку НЕ добавлять. Селектор в чате показывает РЕАЛЬНЫЕ версии: «Opus 4.8» / «Sonnet 4.6» / «Haiku 4.5» (маппинг на model-id). Связано с карточкой 4df23a (версии + thinking mode).
3. **Khronika** — вынести из claude-ops-bot ПОЛНОСТЬЮ (см. Ф0). ClaudeOps и Khronika — несвязанные проекты.

## Промпт для новой сессии (копипастнуть в свежую сессию)
> Делай spec-011, фазы Ф0→Ф3. На каждую фазу запускай агентов-исполнителей (Sonnet, полноценные) + отдельных агентов-ревьюеров, которые ищут баги и несостыковки в сделанном и в самом плане. Синтезируй, повторяй пока ревью не чистое (после проходов всплывают новые замечания). Каждая фаза: pytest → npm build → коммит → restart-self.sh. Сначала прочти ARCHITECTURE.md + CLAUDE.md.

## Порядок и принципы
- Фазы 0→1→2→3. Фаза 0 — без споров (баги/наблюдаемость). Фаза 1 — поведенческая (тесты off). Фаза 2 — UI. Фаза 3 — долг.
- Каждая фаза: правки → `pytest -q` зелёный → (если фронт) `npm run build` → коммит → `restart-self.sh`. UI без рестарта не активируется.
- Дисциплина: bypassPermissions, но необратимое (удаление файлов) — подтверждение Игоря.

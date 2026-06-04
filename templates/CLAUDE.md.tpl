# CLAUDE.md — {{name}}

> Создан {{date}} через «+ Новый проект» в кокпите Claude-Ops.
> Этот файл — главные правила и команды для агентов, работающих в этом проекте.

## Что это
_2-3 предложения: что делает проект, для кого. Заполнить во время онбординга._

## Стек
- Язык/фреймворк: …
- Инфра: …
- Внешние API: …

## Команды
```bash
# запуск/тесты/деплой — заполнить
```

## Gotchas
- _Сюда — грабли, на которые наступили, чтобы не повторять._

## Секреты проекта

Секреты (API-ключи, токены, пароли) хранятся в `.claude-ops/secrets/secrets.env`.

**Расположение:** `<cwd>/.claude-ops/secrets/secrets.env` — `chmod 600`, НЕ коммитится в git (gitignored автоматически).

**Доступ агента:** при каждом запуске задачи секреты подмешиваются в `env` процесса агента — доступны как обычные переменные окружения (`os.environ["STRIPE_KEY"]` и т.п.).

**Управление:** вкладка «🔑 Ключи» в кокпите (только имена ключей, значения не отображаются) — или вручную: `echo 'MY_KEY=value' >> .claude-ops/secrets/secrets.env && chmod 600 .claude-ops/secrets/secrets.env`.

**⚠️ Gotcha:**
- Имена ключей: только заглавные `A-Z`, цифры, `_` (env-совместимые).
- Значения не возвращаются через API — только write-only из кокпита.
- Не логируются в audit-лог, не попадают в git.

## Память проекта

Накапливаемые знания, которые путешествуют с репо: `.claude-ops/memory/`.

**Структура:** `MEMORY.md` — индекс (одна строка на запись). `<slug>.md` — одна запись.

**Формат записи:**
```
---
type: decision | gotcha | rejected | convention
created: YYYY-MM-DD
---
Суть. Для decision/rejected — **Почему:** причина.
```

**Когда писать:**
- `decision` — архитектурное или технологическое решение + почему так, а не иначе.
- `gotcha` — грабли, на которые наступили, чтобы не наступить снова.
- `rejected` — что Игорь или проект отвергли + почему (не предлагать снова).
- `convention` — договорённость о стиле, именовании, подходе.

Агент пишет сюда через обычный Write (путь относительный: `.claude-ops/memory/<slug>.md`).
Память коммитится в git — видна при клоне, история сохраняется.

---

## Error Handler

Каждый сервис/бот обязан писать необработанные исключения в лог — иначе кокпит не видит рантайм-ошибки.
Кокпит-сканер грепает строку: `UNHANDLED exc_class=<Type> path=<route>` — она обязана быть в логе.
`logging` должен достигать журнала (journald/stdout — куда смотрит `log_cmd`).

### FastAPI (эталон: networking-os CRM)

```python
import logging, traceback, uuid
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, (HTTPException, RequestValidationError)):
        raise exc
    request_id = str(uuid.uuid4())
    log.error(
        "UNHANDLED exc_class=%s path=%s request_id=%s\n%s",
        type(exc).__name__, request.url.path, request_id,
        traceback.format_exc(),
    )
    # Опционально: fire-and-forget TG-алерт с rate-limit по (path, exc_class)
    # asyncio.create_task(alert_exception(exc, request, request_id))
    return JSONResponse(status_code=500,
                        content={"error": "internal", "request_id": request_id})
```

### aiohttp (middleware)

```python
import logging, traceback
from aiohttp import web

log = logging.getLogger(__name__)

@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:
        log.error(
            "UNHANDLED exc_class=%s path=%s\n%s",
            type(exc).__name__, request.path,
            traceback.format_exc(),
        )
        return web.json_response({"error": "internal"}, status=500)

app = web.Application(middlewares=[error_middleware])
```

### python-telegram-bot (PTB)

```python
import logging, traceback
from telegram.ext import Application

log = logging.getLogger(__name__)

async def error_handler(update, context):
    log.error(
        "UNHANDLED exc_class=%s path=tg_update\n%s",
        type(context.error).__name__,
        "".join(traceback.format_exception(type(context.error),
                                           context.error,
                                           context.error.__traceback__)),
    )

application = Application.builder().token("...").build()
application.add_error_handler(error_handler)
```

### CLI / скрипт

```python
import logging, sys, traceback

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

def main(): ...

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.error("UNHANDLED exc_class=%s path=__main__\n%s",
                  sys.exc_info()[0].__name__, traceback.format_exc())
        sys.exit(1)
```

### Библиотека / фоновая задача

```python
import logging, sys, traceback

log = logging.getLogger(__name__)

def do_work():
    try:
        ...
    except Exception:
        log.error("UNHANDLED exc_class=%s path=%s\n%s",
                  sys.exc_info()[0].__name__, "do_work", traceback.format_exc())
        raise  # re-raise после логирования
```

---

## Правила работы в кокпите (НЕ удалять — общие для всех проектов)

**Доска (TASKS.md):**
- Карточка: `- [ ] текст <!--ops:ID-->` строго внутри секции колонки.
- Колонки: `## Backlog` / `## In Progress` / `## Review` / `## Failed`.
- НЕЛЬЗЯ: нумерованные списки (`1.`), вложенные подсписки, таблицы внутри секций, текст вне секций (кроме преамбулы файла до первой `##`).
- Маркер `<!--ops:ID-->` ставится автоматически при первом GET — не убирать.
- Формулировка задачи: глагол + объект + критерий «готово». Плохо: «починить логи». Хорошо: «настроить log_cmd в topics.json для X, GET /api/projects/X/logs возвращает строки».
- Выполненное → `DONE.md` (архив, сессии его НЕ читают — гигиена контекста).

**Сессии:**
- Один проект = одна общая сессия (TG + чат в кокпите + карточки делят её).
- Переключение темы = `/reset` (новая сессия), не дописывать в текущую — контекст забивается.
- Сессия в кокпите видна на любом устройстве (продолжать с телефона из браузера).

**Файлы:**
- `data/` (если есть) и `.env*` — НЕ в git (см. .gitignore).
- README.md — короткое описание для будущего; CLAUDE.md — главное.

**Аудит:**
- Раз в неделю — кнопка «🩺 Аудит проекта» в Overview-табе. Агент проверит структуру и создаст карточки на исправления.

**Самолечение (опция):**
- Тумблер «🔧 Самолечение» в Overview — OFF по умолчанию. Включать осознанно.
- При включении: новые ошибки (из log_cmd) → агент авто-чинит в worktree → карточка в Review.
- **Незыблемо:** агент НИКОГДА не применяет изменения без человека. Merge — всегда руками («✓ Применить»).
- Требует: git-репо + clean tree + log_cmd в topics.json.

**Возможности ClaudeOps — что подключить:**

| Возможность | Что делает | Как завести |
|---|---|---|
| **error handler** | **ОБЯЗАТЕЛЕН для сервисов/ботов.** Пишет необработанные исключения в лог → кокпит ловит инцидент. | Добавить по типу проекта (см. `## Error Handler` выше). |
| **log_cmd** | Кокпит читает логи проекта (вкладка «Логи», основа сканера ошибок). | В `topics.json` для проекта: `"log_cmd": "journalctl -u my-svc -n 300 --no-pager"`. |
| **test_cmd** | Кнопка «Прогнать тесты» + quality gate при самолечении. НЕ гоняется в фоне. | В `topics.json`: `"test_cmd": "pytest -q"`. Путь относителен cwd. |
| **самолечение (git+clean)** | При новом инциденте агент авто-чинит в worktree → карточка в Review для ревью человеком. | Тумблер в Overview. Требует git-репо + clean tree + log_cmd. |
| **notify_on_error** | TG-пинг Игорю при новом инциденте. | Тумблер «🔔 Уведомлять» в Overview. |
| **healthz/liveness** | Для сервисов: проект отдаёт эндпоинт `/healthz` (или `/_health`) — кокпит сможет пинговать (на будущее). | Добавить роут, отдающий 200 + `{"ok":true}`. |
| **память** | `.claude-ops/memory/` — знания, путешествующие с репо (решения, gotchas). | Создаётся автоматически при первой записи агента. |
| **секреты** | `.claude-ops/secrets/secrets.env` — ключи/токены в env агента. | Вкладка «🔑 Ключи» в кокпите, или `echo 'KEY=val' >> ...secrets.env`. |

---

## ClaudeOps conformance
<!-- Заполняется при онбординге. Кокпит читает это в health. Формат строки: "- <возможность>: <да: где / нет>" -->
- error handler: нет
- log_cmd: нет
- test_cmd: нет
- самолечение (git+clean): нет
- память (.claude-ops/memory): нет
- секреты (.claude-ops/secrets): нет
- notify_on_error: нет
- healthz/liveness (сервисам): нет

# Error Handler — Reference

Copied from `CLAUDE.md.tpl` (the cockpit requires the `UNHANDLED exc_class=...` log line to detect runtime incidents).

## Error Handler

Каждый сервис/бот обязан писать необработанные исключения в лог — иначе кокпит не видит рантайм-ошибки.
Кокпит-сканер грепает строку: `UNHANDLED exc_class=<Type> path=<route>` — она обязана быть в логе.
`logging` должен достигать журнала (journald/stdout — куда смотрит `log_cmd`).

### FastAPI

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

### Опционально: мгновенный push инцидента в кокпит

> **Базовый мониторинг работает и БЕЗ этого (по логам).** Push нужен только если хочется
> доставки инцидента в кокпит мгновенно, без ожидания следующего скана (≤60с).
> Требует двойного opt-in: (1) оператор включил `incident_push_enabled` в глобальных
> настройках кокпита, (2) секрет `CLAUDEOPS_INCIDENT_TOKEN` задан проекту в кокпите
> И доступен в env самого сервиса.

```python
# fire-and-forget push инцидента в кокпит (Python, framework-agnostic)
# Вставить в глобальный обработчик исключений рядом с logging.error(...)
import asyncio, os
import aiohttp  # или httpx, или urllib.request — swallow ALL errors

_COCKPIT_URL = os.environ.get("CLAUDEOPS_URL", "")        # https://YOUR_DOMAIN
_COCKPIT_PROJECT = os.environ.get("CLAUDEOPS_PROJECT", "") # basename cwd проекта
_COCKPIT_TOKEN = os.environ.get("CLAUDEOPS_INCIDENT_TOKEN", "")

async def _push_incident(exc_class: str, where: str, excerpt: str = "") -> None:
    """Fire-and-forget push инцидента в кокпит. Глотает все ошибки — сеть не должна
    ронять сервис. Дедуп по hash гарантирован кокпитом: лог-сканер не задвоит."""
    if not (_COCKPIT_URL and _COCKPIT_PROJECT and _COCKPIT_TOKEN):
        return
    url = f"{_COCKPIT_URL}/api/projects/{_COCKPIT_PROJECT}/incident"
    payload = {"exc_class": exc_class, "where": where, "excerpt": excerpt}
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json=payload,
                         headers={"X-Incident-Token": _COCKPIT_TOKEN},
                         timeout=aiohttp.ClientTimeout(total=5))
    except Exception:
        pass  # push сугубо опционален; потеря уведомления некритична

# В обработчике исключений (рядом с log.error("UNHANDLED ...")):
# asyncio.create_task(_push_incident(type(exc).__name__, request.path, str(exc)[:200]))
```

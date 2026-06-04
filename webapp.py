"""
webapp.py — браузерный кокпит Claude-Ops-Bot.

Поднимается в том же процессе/loop, что и PTB-бот.
Все объекты состояния передаются через ctx — мутации видны боту.
НЕ импортирует bot.py напрямую (повторный импорт создаст второй экземпляр).
"""

import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import time
import traceback as _tb
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, TypedDict

import aiohttp
from aiohttp import web


# ─────────────────────────── именованные константы ───────────────────────────

_BUS_QUEUE_SIZE = 100   # maxsize per-session очереди шины; переполнена → drop (не блокирует)
_BUS_GLOBAL_SIZE = 200  # maxsize глобальной очереди шины (все сессии)

# Strong references for long-lived background tasks created via asyncio.create_task.
# Prevents GC from collecting tasks before they complete (Python docs warning).
_BG_TASKS: set = set()


def _spawn_bg(coro):
    """Создаёт fire-and-forget задачу, защищённую от GC через _BG_TASKS.
    Результат задачи не используется вызывающим кодом — только для фоновых эффектов."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t



# ─────────────────────────── activity bus ───────────────────────────
#
# Лёгкая in-process шина событий: dict[session_key -> set[asyncio.Queue]].
# Всё в одном event loop → обычные set/dict, без asyncio.Lock.
# Очередь maxsize=_BUS_QUEUE_SIZE: переполнена → drop (put_nowait в try/except), продюсер не блокируется.

_bus: dict[str, set[asyncio.Queue]] = {}
# Глобальные подписчики — получают ВСЕ события всех сессий, с инжектированным session_key.
# Используется для общего activity-stream приложения (unread-индикаторы в сайдбаре).
_bus_global: set[asyncio.Queue] = set()


def _bus_subscribe(session_key: str) -> "asyncio.Queue[dict]":
    """Создаёт очередь и регистрирует подписчика на session_key."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_BUS_QUEUE_SIZE)
    _bus.setdefault(session_key, set()).add(q)
    return q


def _bus_unsubscribe(session_key: str, q: "asyncio.Queue[dict]") -> None:
    """Удаляет подписчика; очищает ключ, если подписчиков не осталось."""
    subscribers = _bus.get(session_key)
    if subscribers is not None:
        subscribers.discard(q)
        if not subscribers:
            _bus.pop(session_key, None)


def _bus_subscribe_global() -> "asyncio.Queue[dict]":
    """Подписка на ВСЕ события всех сессий (события приходят с полем session_key)."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_BUS_GLOBAL_SIZE)
    _bus_global.add(q)
    return q


def _bus_unsubscribe_global(q: "asyncio.Queue[dict]") -> None:
    _bus_global.discard(q)


def _bus_publish(session_key: str, event: dict) -> None:
    """Публикует событие во все очереди подписчиков. Переполнена → drop (не блокирует)."""
    subscribers = _bus.get(session_key)
    if subscribers:
        for q in list(subscribers):  # list() — снимок, т.к. _bus_unsubscribe может вызываться параллельно
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer — drop, не блокировать продюсера
    # Глобальная рассылка — обогащаем событие session_key, чтобы фронт мог сматчить с проектом
    if _bus_global:
        enriched = {**event, "session_key": session_key}
        for q in list(_bus_global):
            try:
                q.put_nowait(enriched)
            except asyncio.QueueFull:
                pass
    # Timeline persistence — единая точка записи для всех событий шины
    _timeline_append(session_key, event)


# ─────────────────────────── timeline persistence ─────────────────────────────
#
# Каждое событие шины персистируется в JSONL: DATA/timeline/<slug>.jsonl.
# Slug = cwd.replace('/', '-'), как в _sdk_sessions_dir.
# Ротация: файл >5MB → переименовать в .jsonl.1 (одна копия; перезатирает старую .1).
# Запись глотает ошибки — не ломает прогон.
# Инициализация: start() вызывает _timeline_init(ctx) — передаёт DATA и topics-dict.

_TIMELINE_DATA_DIR: "Path | None" = None   # DATA/timeline/ — задаётся в start()
_TIMELINE_TOPICS: "dict | None" = None     # ссылка на ctx["topics"] — для session_key→cwd
_TIMELINE_MAX_SIZE = 5 * 1024 * 1024       # 5 MB — ротация
_TIMELINE_TEXT_LIMIT = 2000                # симв — обрезка text-поля


def _timeline_init(ctx: dict) -> None:
    """Вызывается из start() — сохраняет ссылки для _timeline_append."""
    global _TIMELINE_DATA_DIR, _TIMELINE_TOPICS
    _TIMELINE_DATA_DIR = ctx["DATA"] / "timeline"
    _TIMELINE_TOPICS = ctx["topics"]
    try:
        _TIMELINE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _timeline_slug_from_cwd(cwd: str) -> str:
    """Стабильный slug из cwd (идентично _sdk_sessions_dir): '/' → '-'."""
    return cwd.replace("/", "-")


def _timeline_path(session_key: str) -> "Path | None":
    """Возвращает Path к .jsonl-файлу для session_key, или None если DATA не инициализирован.
    Резолвит session_key → cwd через _TIMELINE_TOPICS; если не найден — пишет в _unknown.jsonl."""
    if _TIMELINE_DATA_DIR is None:
        return None
    cwd: str | None = None
    if _TIMELINE_TOPICS:
        topic_data = _TIMELINE_TOPICS.get(session_key)
        if topic_data:
            cwd = topic_data.get("cwd")
    if cwd:
        slug = _timeline_slug_from_cwd(cwd)
    else:
        # session_key может быть free-chat id или неизвестным топиком — кодируем безопасно
        safe = session_key.replace("/", "-").replace(":", "-")
        slug = safe if safe else "_unknown"
    return _TIMELINE_DATA_DIR / f"{slug}.jsonl"


def _timeline_append(session_key: str, event: dict) -> None:
    """Добавляет событие в JSONL-лог. Ошибки глотает (не ломает прогон).
    Никогда не логирует поля env — их в событиях нет, защита на случай будущих изменений."""
    try:
        path = _timeline_path(session_key)
        if path is None:
            return
        # Собираем запись: добавляем ts, обрезаем text, исключаем env
        record: dict = {"ts": time.time(), "session_key": session_key}
        for k, v in event.items():
            if k == "env":
                continue  # env — секреты, никогда в timeline
            if k == "text" and isinstance(v, str) and len(v) > _TIMELINE_TEXT_LIMIT:
                record[k] = v[:_TIMELINE_TEXT_LIMIT] + "…"
            else:
                record[k] = v
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # Ротация: если файл уже существует и > 5MB — переименовать в .1
        try:
            if path.exists() and path.stat().st_size > _TIMELINE_MAX_SIZE:
                backup = path.with_suffix(".jsonl.1")
                path.rename(backup)
        except Exception:
            pass
        # Append
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # никогда не ломаем прогон


# ─────────────────────────── tool formatter ───────────────────────────

def _format_tool(name: str, inp: dict) -> dict:
    """Единый форматтер tool-события: возвращает богатую структуру по типу инструмента.
    Используется во всех трёх точках: chat SSE, bus publish, session-history."""
    if not isinstance(inp, dict):
        inp = {}

    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        return {"name": name, "kind": "bash", "cmd": cmd, "desc": desc}

    elif name in ("Edit", "MultiEdit", "NotebookEdit"):
        file_path = inp.get("file_path", "")
        if name == "Edit":
            old_str = inp.get("old_string", "")
            new_str = inp.get("new_string", "")
            if isinstance(old_str, str) and len(old_str) > 400:
                old_str = old_str[:400] + "…"
            if isinstance(new_str, str) and len(new_str) > 400:
                new_str = new_str[:400] + "…"
            return {"name": name, "kind": "edit", "file": file_path, "old": old_str, "new": new_str}
        elif name == "MultiEdit":
            edits = inp.get("edits", [])
            count = len(edits) if isinstance(edits, list) else 0
            return {"name": name, "kind": "edit", "file": file_path, "count": count}
        else:  # NotebookEdit
            cell_type = inp.get("cell_type", "")
            return {"name": name, "kind": "edit", "file": file_path, "cell_type": cell_type}

    elif name == "Write":
        file_path = inp.get("file_path", "")
        content = inp.get("content", "")
        if isinstance(content, str) and len(content) > 600:
            preview = content[:600] + "…"
        else:
            preview = content if isinstance(content, str) else ""
        return {"name": name, "kind": "write", "file": file_path, "preview": preview}

    elif name == "Read":
        file_path = inp.get("file_path", "")
        return {"name": name, "kind": "read", "file": file_path}

    elif name in ("Glob", "Grep"):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return {"name": name, "kind": "search", "pattern": pattern, "path": path}

    else:
        # прочее: берём первое значение как summary
        first = next(iter(inp.values()), "") if inp else ""
        summary = str(first)
        if len(summary) > 200:
            summary = summary[:200] + "…"
        return {"name": name, "kind": "other", "summary": summary}


COOKIE_MAX_AGE = 2592000  # 30 дней в секундах


# ─────────────────────────── auth ───────────────────────────
#
# Схема: cookie cops_auth = hex(scrypt(password, salt=AUTH_SALT, n=2^14, r=8, p=1)).
# Соль — AUTH_SALT из env (первый запуск → авто-генерация и вывод в stderr).
# Сравнение — hmac.compare_digest (constant-time).
# Rate-limit: ≥5 неудачных попыток с одного IP за 5 мин → 429 на 5 мин.

import hmac as _hmac

# Spec-012 Ф3: паттерн для точного матча пути /api/projects/{id}/incident.
# Прекомпилирован один раз — используется в auth_middleware для tight-exempt.
# Специально НЕ endswith("/incident"): не пропустит ../incident/evil или GET.
_INCIDENT_PATH_RE = re.compile(r"^/api/projects/[^/]+/incident$")

# Rate-limit push-эндпоинта: не более _INCIDENT_PUSH_MAX вызовов за _INCIDENT_PUSH_WINDOW сек
# с валидным токеном, per-project. Предотвращает шторм heal-запусков.
_INCIDENT_PUSH_MAX = 30
_INCIDENT_PUSH_WINDOW = 60  # секунды
_INCIDENT_IP_MAX = 300      # per-IP backstop (до резолва проекта/чтения секрета — против unauth-флуда)
_incident_ip_history: dict[str, list[float]] = {}
# {project_id: [timestamp, ...]} — история успешных вызовов
_incident_push_history: dict[str, list[float]] = {}

# Соль для scrypt: берётся из env WEB_COOKIE_SALT или генерируется при старте.
AUTH_SALT: bytes = os.environ.get("WEB_COOKIE_SALT", "").encode() or (
    lambda s: (print(f"[auth] сгенерирована соль WEB_COOKIE_SALT={s} — добавь в .env", flush=True), s.encode())[1]
)(secrets.token_hex(16))


def _derive_token(password: str) -> str:
    """Деривация cookie-токена через scrypt (stdlib, без новых зависимостей)."""
    dk = hashlib.scrypt(
        password.encode(),
        salt=AUTH_SALT,
        n=1 << 14,  # 16384 — баланс скорости и безопасности (< 100ms на сервере)
        r=8,
        p=1,
        dklen=32,
    )
    return dk.hex()


# Обратная совместимость для middleware (используется в тестах)
def _make_token(password: str) -> str:
    return _derive_token(password)


# Rate-limit: {ip: [(timestamp, ok:bool), ...]}
_login_attempts: dict[str, list[tuple[float, bool]]] = {}
_LOGIN_WINDOW = 300   # 5 минут
_LOGIN_MAX_FAIL = 5   # макс неудачных попыток


def _check_rate_limit(ip: str) -> bool:
    """True если IP превысил лимит неудачных попыток. Чистит старые записи."""
    now = time.monotonic()
    attempts = _login_attempts.get(ip, [])
    # Оставляем только записи в пределах окна
    attempts = [(t, ok) for t, ok in attempts if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    fails = sum(1 for _, ok in attempts if not ok)
    return fails >= _LOGIN_MAX_FAIL


def _record_attempt(ip: str, success: bool) -> None:
    now = time.monotonic()
    bucket = _login_attempts.setdefault(ip, [])
    bucket.append((now, success))
    # Не даём расти бесконечно (атака с одного IP)
    if len(bucket) > 200:
        _login_attempts[ip] = bucket[-200:]


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Внешний middleware: логирует необработанные исключения и возвращает JSON 500."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ConnectionResetError, ConnectionAbortedError):
        # Клиент закрыл соединение (типично для SSE/long-poll: закрыл вкладку, туннель оборвался).
        # Это НЕ инцидент. Ответ уже мог начать стримиться → json_response невозможен. Пробрасываем
        # (aiohttp сам приберёт транспорт; CancelledError — BaseException, проходит мимо и так).
        raise
    except Exception as exc:
        request_id = _uuid.uuid4().hex[:8]
        logging.exception("UNHANDLED exc_class=%s path=%s request_id=%s", type(exc).__name__, request.path, request_id)
        # Spec-012 Ф1: своя ошибка кокпита → карточка in-process, мгновенно (без круга
        # через лог-сканер). Fire-and-forget; дедуп по hash не даст сканеру задвоить.
        try:
            _spawn_bg(_report_incident(request.app["ctx"], type(exc).__name__, request.path))
        except Exception:
            pass
        return web.json_response(
            {"error": type(exc).__name__, "request_id": request_id},
            status=500,
        )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Защита /api/* — пропускает /api/health и /api/login без cookie.
    Spec-012 Ф3: также пропускает POST /api/projects/{id}/incident (у него свой
    токен-авт в теле/заголовке). Матч TIGHT через прекомпилированный _INCIDENT_PATH_RE —
    ни эндпоинты без trailing-id, ни /incident/evil, ни GET не попадут в exempt."""
    path = request.path
    # Незащищённые эндпоинты
    if path in ("/api/health", "/api/login"):
        return await handler(request)
    # Spec-012 Ф3: push-инцидент — свой auth (token). Только POST, только точный путь.
    if request.method == "POST" and _INCIDENT_PATH_RE.match(path):
        return await handler(request)
    # Только пути /api/* проверяем
    if path.startswith("/api/"):
        password = request.app["ctx"]["password"]
        expected = request.app["ctx"]["_auth_token"]
        token = request.cookies.get("cops_auth", "")
        if not _hmac.compare_digest(token, expected):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


# ─────────────────────────── git helpers ───────────────────────────

async def _git_cmd(cwd: str, *args, timeout: float = 3.0):
    """Запускает git-команду в cwd, возвращает stdout или None при ошибке."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0:
                return stdout.decode().strip()
            return None
        except asyncio.TimeoutError:
            proc.kill()
            return None
    except Exception:
        return None


async def _git_info(cwd: str) -> dict | None:
    """Возвращает {branch, dirty, unpushed} или None если не git-репо."""
    branch = await _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return None

    status_out = await _git_cmd(cwd, "status", "--porcelain") or ""
    dirty = len([l for l in status_out.splitlines() if l.strip()])

    unpushed_out = await _git_cmd(cwd, "rev-list", "@{u}..", "--count")
    try:
        unpushed = int(unpushed_out) if unpushed_out is not None else 0
    except ValueError:
        unpushed = 0

    return {
        "branch": branch, "dirty": dirty, "unpushed": unpushed,
        "visibility": _git_visibility_cached(cwd),
    }


# ── GitHub visibility (private/public) — кэш + фоновый gh, чтобы НЕ блокировать поллинг ──
_GIT_VIS_CACHE: "dict[str, tuple[str | None, float]]" = {}   # cwd → (visibility, ts)
_GIT_VIS_TTL = 3600.0   # видимость репо меняется редко → кэш на час


async def _git_visibility_refresh(cwd: str) -> None:
    """Узнаёт private/public через gh, кладёт в кэш. Сетевой вызов → только в фоне.
    Глотает всё (нет remote / не на GitHub / gh не авторизован → None)."""
    vis: "str | None" = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "repo", "view", "--json", "visibility", "-q", ".visibility",
            cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if proc.returncode == 0:
            v = out.decode(errors="replace").strip().lower()
            if v in ("private", "public"):
                vis = v
    except Exception:
        vis = None
    _GIT_VIS_CACHE[cwd] = (vis, time.time())


def _git_visibility_cached(cwd: str) -> "str | None":
    """Кэш видимости; при промахе/протухании — фоновый refresh, отдаёт текущее (stale/None),
    НЕ блокируя поллинг. Вызывается из async-контекста (нужен running loop для _spawn_bg)."""
    entry = _GIT_VIS_CACHE.get(cwd)
    if entry is None or (time.time() - entry[1]) > _GIT_VIS_TTL:
        try:
            _spawn_bg(_git_visibility_refresh(cwd))
        except Exception:
            pass
    return entry[0] if entry else None


# ─────────────────────────── project helpers ───────────────────────────

def _project_id(cwd: str) -> str:
    """id проекта = basename cwd без хвостового /."""
    return Path(cwd.rstrip("/")).name


def _session_labels_path(ctx: dict) -> Path:
    return ctx["DATA"] / "session_labels.json"


def _load_session_labels(ctx: dict) -> dict:
    """{session_id → user_label}. SDK сам lable не умеет — это наш слой."""
    p = _session_labels_path(ctx)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session_labels(ctx: dict, data: dict) -> None:
    _session_labels_path(ctx).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _inherit_label_from_free_chat(ctx: dict, session_key: str, sid: str) -> None:
    """Если session_key — это free-чат с label, и у sid ещё нет своего лейбла —
    наследует label вкладки. Вызывается когда SDK впервые присвоил session_id."""
    if not (session_key and session_key.startswith("free-") and sid):
        return
    free = _load_free_chats(ctx)
    entry = free.get(session_key)
    if not entry or not entry.get("label"):
        return
    labels = _load_session_labels(ctx)
    if sid in labels:
        return  # уже подписана (ручной rename) — не трогаем
    labels[sid] = entry["label"]
    _save_session_labels(ctx, labels)


def _free_chats_path(ctx: dict) -> Path:
    return ctx["DATA"] / "free_chats.json"


# ── Prompt templates ──────────────────────────────────────────────────────────

def _prompts_path(ctx: dict) -> Path:
    return ctx["DATA"] / "prompts.json"

def _load_prompts(ctx: dict) -> list:
    p = _prompts_path(ctx)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []

def _save_prompts(ctx: dict, prompts: list):
    _prompts_path(ctx).write_text(json.dumps(prompts, ensure_ascii=False, indent=2))

async def api_prompts_list(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    return web.json_response({"prompts": _load_prompts(ctx)})

async def api_prompt_create(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    title = (data.get("title") or "").strip()
    text  = (data.get("text")  or "").strip()
    if not title or not text:
        raise web.HTTPBadRequest(text="title and text required")
    category = (data.get("category") or "").strip() or None
    prompt = {"id": str(_uuid.uuid4())[:8], "title": title, "text": text, **({"category": category} if category else {})}
    prompts = _load_prompts(ctx)
    prompts.append(prompt)
    _save_prompts(ctx, prompts)
    return web.json_response({"prompt": prompt})

async def api_prompt_delete(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    prompts = [p for p in _load_prompts(ctx) if p.get("id") != pid]
    _save_prompts(ctx, prompts)
    return web.json_response({"ok": True})

async def api_prompt_update(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    prompts = _load_prompts(ctx)
    for p in prompts:
        if p.get("id") == pid:
            if "title" in data: p["title"] = (data["title"] or "").strip()
            if "text" in data: p["text"] = (data["text"] or "").strip()
            if "category" in data:
                cat = (data.get("category") or "").strip() or None
                if cat: p["category"] = cat
                else: p.pop("category", None)
            _save_prompts(ctx, prompts)
            return web.json_response({"prompt": p})
    return web.json_response({"error": "not found"}, status=404)


def _load_free_chats(ctx: dict) -> dict:
    """{free_id → {label, cwd, model, created_at}}. Файл может отсутствовать — вернёт {}."""
    p = _free_chats_path(ctx)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_free_chats(ctx: dict, data: dict) -> None:
    p = _free_chats_path(ctx)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_TOPICS_MTIME: "float | None" = None  # mtime последней подхваченной версии topics.json


def _maybe_reload_topics(ctx: dict) -> None:
    """Hot-reload topics.json с диска при внешней правке (без рестарта процесса).

    Зачем: `topics` грузится один раз на старте бота (bot.py) и живёт как
    in-memory dict в ctx["topics"]. Прямая Edit/Write файла (агентом из кокпита)
    проходила мимо этого dict → правка не видна до рестарта. Диск авторитетен —
    runtime-команды бота всегда вызывают save_topics() — поэтому чтение с диска
    безопасно. Обновляем dict IN-PLACE (clear+update), чтобы и бот, и кокпит
    видели один и тот же объект. mtime-гейт: парсим только при изменении файла.
    Битый/частично записанный файл (гонка с save_topics) → JSONDecodeError →
    тихо оставляем текущую версию, повторим на следующем запросе."""
    global _TOPICS_MTIME
    try:
        path = ctx["DATA"] / "topics.json"
        mtime = path.stat().st_mtime
    except OSError:
        return
    if _TOPICS_MTIME is not None and mtime == _TOPICS_MTIME:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        ctx["topics"].clear()
        ctx["topics"].update(data)
        _TOPICS_MTIME = mtime


def _collect_projects(ctx: dict) -> list[dict]:
    """Дедуп по cwd, собирает список проектов из ctx["topics"].
    Добавляет free-чаты как virtual projects (id=free-<uuid>, tg_thread=сам id)."""
    _maybe_reload_topics(ctx)
    seen: set[str] = set()
    out = []
    for key, b in ctx["topics"].items():
        cwd = b.get("cwd", "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        pid = _project_id(cwd)
        # tg_thread — строковый ключ "chat:thread"
        out.append({
            "id": pid,
            "name": b.get("project", pid),
            "cwd": cwd,
            "model": b.get("model", ctx.get("DEFAULT_MODEL", "sonnet")),
            "tg_thread": key,
            "is_free": False,
            "log_cmd": b.get("log_cmd"),
            "test_cmd": b.get("test_cmd"),
            "notify_on_error": bool(b.get("notify_on_error", False)),
            "self_heal": bool(b.get("self_heal", False)),
            "heal_ignore": b.get("heal_ignore") if isinstance(b.get("heal_ignore"), list) else None,
            "git_enabled": b.get("git_enabled", True) is not False,
        })
    out.sort(key=lambda x: x["name"].lower())

    # Free chats — отдельная секция, сортировка по времени создания
    free = _load_free_chats(ctx)
    free_items = sorted(free.items(), key=lambda kv: kv[1].get("created_at", 0))
    for fid, b in free_items:
        out.append({
            "id": fid,
            "name": b.get("label", fid),
            "cwd": b.get("cwd", str(Path.home())),
            "model": b.get("model", ctx.get("DEFAULT_MODEL", "sonnet")),
            "tg_thread": fid,  # session_key для free = его id (строка с префиксом free-)
            "is_free": True,
        })
    return out


def _find_project_by_id(ctx: dict, pid: str) -> dict | None:
    """Ищет проект по id (basename cwd)."""
    for p in _collect_projects(ctx):
        if p["id"] == pid:
            return p
    return None


def _find_vault_specs_dir(ctx: dict, project_name: str, cwd: str) -> Path | None:
    """Пробует несколько вариантов имён для папки в VAULT_PROJECTS."""
    vault: Path = ctx["VAULT_PROJECTS"]
    if not vault.is_dir():
        return None
    candidates = [
        project_name,
        project_name.lower(),
        Path(cwd).name,
        Path(cwd).name.lower(),
    ]
    # Регистронезависимый перебор реальных папок
    try:
        existing = {d.name: d for d in vault.iterdir() if d.is_dir()}
    except Exception:
        return None
    for c in candidates:
        if c in existing:
            return existing[c]
        # case-insensitive
        cl = c.lower()
        for name, path in existing.items():
            if name.lower() == cl:
                return path
    return None


# ─────────────────────────── API handlers ───────────────────────────

async def api_health(req: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def api_login(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    # Rate-limit по IP (пеерд чтением тела)
    ip = req.remote or "unknown"
    if _check_rate_limit(ip):
        return web.json_response({"error": "too many attempts, try later"}, status=429)
    try:
        body = await req.json()
        password = body.get("password", "")
    except Exception:
        _record_attempt(ip, False)
        return web.json_response({"error": "bad request"}, status=400)
    if not _hmac.compare_digest(password, ctx["password"]):
        _record_attempt(ip, False)
        return web.json_response({"error": "bad password"}, status=401)
    _record_attempt(ip, True)
    token = ctx["_auth_token"]
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        "cops_auth", token,
        httponly=True,
        secure=True,
        path="/",
        max_age=COOKIE_MAX_AGE,
        samesite="Lax",
    )
    return resp


async def api_logout(req: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie("cops_auth", path="/")
    return resp


async def api_me(req: web.Request) -> web.Response:
    return web.json_response({"authed": True})


async def api_projects(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    projects = _collect_projects(ctx)

    def _count_incidents(cwd: str) -> int:
        try:
            _, _, cols = _load_board(cwd)
        except Exception:
            return 0
        return sum(1 for col_cards in cols.values() for c in col_cards if _is_incident_card(c))

    async def enrich(p: dict) -> dict:
        # Для свободных чатов git-проверка бессмысленна (cwd обычно $HOME, не репо проекта)
        if p.get("is_free"):
            return {**p, "health": {"git": None}, "incidents": 0}
        # git отключён настройкой проекта — не показываем git-статус
        if not _git_enabled(p):
            return {**p, "health": {"git": None}, "incidents": _count_incidents(p["cwd"])}
        try:
            git = await _git_info(p["cwd"])
        except Exception:
            git = None
        return {**p, "health": {"git": git}, "incidents": _count_incidents(p["cwd"])}

    try:
        enriched = await asyncio.gather(*[enrich(p) for p in projects])
    except Exception:
        enriched = [{**p, "health": {"git": None}} for p in projects]

    return web.json_response({"projects": list(enriched)})


async def api_project_claude_md(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    path = Path(project["cwd"]) / "CLAUDE.md"
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            exists = True
        else:
            content = ""
            exists = False
    except Exception as e:
        content = f"[ошибка чтения: {e}]"
        exists = False
    return web.json_response({"path": str(path), "content": content, "exists": exists})


async def api_project_readme(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd = Path(project["cwd"])
    # перебор популярных вариантов имени README
    candidates = ["README.md", "readme.md", "Readme.md", "README.MD",
                  "README.markdown", "README.rst", "README.txt", "README"]
    path, content, exists = cwd / "README.md", "", False
    try:
        for name in candidates:
            p = cwd / name
            if p.exists():
                path, content, exists = p, p.read_text(encoding="utf-8"), True
                break
    except Exception as e:
        content, exists = f"[ошибка чтения: {e}]", False
    return web.json_response({"path": str(path), "content": content, "exists": exists})


_README_CANDIDATES = ["README.md", "readme.md", "Readme.md", "README.MD",
                      "README.markdown", "README.rst", "README.txt", "README"]


async def _write_doc(req: web.Request, resolve_path):
    """Общий писатель для CLAUDE.md/README: POST {content} → перезаписать файл.
    resolve_path(cwd)→Path выбирает целевой файл (учитывает существующий вариант имени)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be a string"}, status=400)
    path = resolve_path(Path(project["cwd"]))
    try:
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        return web.json_response({"error": f"ошибка записи: {e}"}, status=500)
    return web.json_response({"path": str(path), "content": content, "exists": True})


async def api_project_claude_md_write(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/claude-md — перезаписать CLAUDE.md."""
    return await _write_doc(req, lambda cwd: cwd / "CLAUDE.md")


async def api_project_readme_write(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/readme — перезаписать существующий README (или создать README.md)."""
    def _pick(cwd: Path) -> Path:
        for name in _README_CANDIDATES:
            if (cwd / name).exists():
                return cwd / name
        return cwd / "README.md"
    return await _write_doc(req, _pick)


def _spec_dirs(ctx: dict, project: dict) -> list[tuple[Path, str]]:
    """Папки со спеками проекта: ЛОКАЛЬНАЯ <cwd>/specs/ (приоритет) + vault <name>/specs/.
    Возвращает [(dir, source)] только существующих. Агент часто пишет спеки локально,
    а человек — в vault; кокпит показывает и то, и то."""
    dirs: list[tuple[Path, str]] = []
    local = Path(project["cwd"]) / "specs"
    if local.is_dir():
        dirs.append((local, "local"))
    vault_proj = _find_vault_specs_dir(ctx, project["name"], project["cwd"])
    if vault_proj is not None:
        vdir = vault_proj / "specs"
        if vdir.is_dir():
            dirs.append((vdir, "vault"))
    return dirs


async def api_project_specs(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    specs = []
    seen: set[str] = set()  # дедуп по имени; локальная папка идёт первой → выигрывает
    for d, src in _spec_dirs(ctx, project):
        try:
            for f in sorted(d.glob("*.md")):
                if f.name in seen:
                    continue
                seen.add(f.name)
                specs.append({"name": f.name, "path": str(f), "source": src})
        except Exception:
            pass
    return web.json_response({"specs": specs})


async def api_project_spec_content(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    spec_name = req.match_info["name"]

    # Защита от path traversal: только basename, только .md
    spec_name = Path(spec_name).name
    if not spec_name.endswith(".md"):
        return web.json_response({"error": "only .md files allowed"}, status=400)

    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Ищем по имени в локальной, затем в vault (та же приоритетность, что в списке)
    for d, _src in _spec_dirs(ctx, project):
        try:
            candidate = (d / spec_name).resolve()
            if not str(candidate).startswith(str(d.resolve())):
                continue  # path traversal — пропускаем
            if candidate.is_file():
                content = candidate.read_text(encoding="utf-8")
                return web.json_response({"name": spec_name, "content": content})
        except Exception:
            continue
    return web.json_response({"error": "not found"}, status=404)


async def api_project_logs(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/logs — runtime logs via log_cmd from topics.json."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    log_cmd: str | None = project.get("log_cmd") or None
    if not log_cmd:
        return web.json_response({"lines": [], "configured": False, "cmd": None})

    # Delegates subprocess execution to _run_log_cmd (same streams: PIPE+STDOUT, no cwd).
    # Timeout matched to original (8 s). On timeout → 504 (restores original behaviour).
    # raise_on_timeout=True so TimeoutError propagates here (not swallowed in _run_log_cmd).
    try:
        raw = await _run_log_cmd(log_cmd, timeout=8.0, raise_on_timeout=True)
        lines = raw.splitlines()
        # last 300 lines, newest first
        tail = lines[-300:] if len(lines) > 300 else lines
        tail.reverse()
        return web.json_response({"lines": tail, "configured": True, "cmd": log_cmd})
    except asyncio.TimeoutError:
        return web.json_response({"error": "log_cmd timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─────────────────────────── Skills picker ───────────────────────────

def _parse_skill_frontmatter(text: str) -> dict | None:
    """Парсит YAML-frontmatter SKILL.md → {name, description}.
    Минимальный парсер: ищет '---\n...---', берёт строки 'key: value'.
    Многострочные values (через '|' или '>') не поддерживаем — в SKILL.md они редки в шапке."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    block = text[4:end]
    out: dict[str, str] = {}
    for line in block.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key and val:
            out[key] = val
    if "name" not in out:
        return None
    return {"name": out["name"], "description": out.get("description", "")}


def _scan_skills_dir(skills_dir: Path) -> list[dict]:
    """Возвращает список {name, description} из <dir>/<skill>/SKILL.md (case-insensitive имя файла)."""
    out: list[dict] = []
    if not skills_dir.is_dir():
        return out
    for sub in sorted(skills_dir.iterdir()):
        if not sub.is_dir():
            continue
        # SKILL.md или skill.md
        skill_file = None
        for candidate in ("SKILL.md", "skill.md"):
            p = sub / candidate
            if p.is_file():
                skill_file = p
                break
        if skill_file is None:
            continue
        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = _parse_skill_frontmatter(text)
        if meta:
            out.append(meta)
    return out


async def api_project_skills(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/skills → {global: [...], project: [...]}.
    Парсит SKILL.md из ~/.claude/skills/ (global) и <cwd>/.claude/skills/ (project)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    global_skills = _scan_skills_dir(Path.home() / ".claude" / "skills")
    cwd = Path(project["cwd"])
    project_skills = _scan_skills_dir(cwd / ".claude" / "skills")
    return web.json_response({"global": global_skills, "project": project_skills})


async def api_project_activity(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    project_name = project["name"]
    audit_dir: Path = ctx["DATA"] / "audit"
    marker = f"[{project_name}]"
    lines: list[str] = []

    try:
        if audit_dir.is_dir():
            # Берём все audit-*.log, сортируем по имени (хронология)
            log_files = sorted(audit_dir.glob("audit-*.log"))
            for log_file in log_files:
                try:
                    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                        if marker in line:
                            lines.append(line)
                except Exception:
                    pass
    except Exception:
        pass

    # Последние 120 строк, новые сверху
    tail = lines[-120:] if len(lines) > 120 else lines
    tail.reverse()

    return web.json_response({"lines": tail})


# ─────────────────────────── доска задач (TASKS.md / DONE.md) ───────────────────────────
#
# Spec=Kanban=2 файла. TASKS.md (секции = колонки) — единственный, что читают сессии.
# DONE.md (архив) — append-only, агент его НЕ читает (гигиена контекста).
# Истина = markdown в репо проекта; БД для плана НЕ используется.

BOARD_COLUMNS = [
    ("backlog",     "Backlog",     " "),
    ("in_progress", "In Progress", "~"),
    ("review",      "Review",      "?"),
    ("failed",      "Failed",      "!"),
]
_LABEL_TO_COL = {lbl.lower(): key for key, lbl, _ in BOARD_COLUMNS}

# Один lock на cwd — сериализует все cockpit-записи доски (GET canonicalize + mutations).
# Агент пишет файл напрямую и не участвует в lock, поэтому lock защищает только кокпит↔кокпит гонку.
_board_locks: dict[str, asyncio.Lock] = {}

def _get_board_lock(cwd: str) -> asyncio.Lock:
    if cwd not in _board_locks:
        _board_locks[cwd] = asyncio.Lock()
    return _board_locks[cwd]

_CARD_RE = re.compile(r"^\s*[-*]\s*\[(.)\]\s*(.*)$")
# Строки вида "- текст" без checkbox — агент часто пишет именно так.
# Внутри секции-колонки распознаём как Backlog-карточку (статус по умолчанию).
_PLAIN_CARD_RE = re.compile(r"^\s*[-*]\s+(?!\[)(.+)$")
# Один маркер: <!--ops:ID--> — ID может быть любым словом (включая нехексовые алиасы).
_MARKER_RE = re.compile(r"\s*<!--\s*ops:([\w-]+)\s*-->")
# Description строки: '  > текст' (2 пробела + '>') идущие сразу после карточки
_DESC_LINE_RE = re.compile(r"^  > (.*)$")


def _extract_id_and_text(rest: str) -> tuple[str, str]:
    """Извлечь ID и очистить текст от ВСЕХ маркеров ops. Первый маркер = canonical ID."""
    matches = list(_MARKER_RE.finditer(rest))
    if not matches:
        return _new_card_id(), rest.strip()
    cid = matches[0].group(1)
    clean = _MARKER_RE.sub("", rest).strip()
    return cid, clean


def _tasks_path(cwd: str) -> Path:
    return Path(cwd) / "TASKS.md"


def _done_path(cwd: str) -> Path:
    return Path(cwd) / "DONE.md"


def _new_card_id() -> str:
    return secrets.token_hex(3)


# Обычная карточка = hex(+дефис); инцидент = 'err-<hash6>'. Префикс err- разрешён явно,
# тело остаётся [a-f0-9-] (без точек/слешей → traversal невозможен). Без err- в классе
# был баг: err-карточки не проходили валидацию → их нельзя было закрыть/удалить из UI.
_CARD_ID_RE = re.compile(r"^(err-)?[a-f0-9-]{4,20}$")


def _valid_card_id(card_id: str) -> bool:
    """True если card_id соответствует ожидаемому формату (hex+dash, 4-20 символов)."""
    return bool(_CARD_ID_RE.fullmatch(card_id))



def _count_potential_cards(raw: str) -> int:
    """Сколько строк в raw МОГУТ быть карточками (любого формата).
    Используется как guard: если после parse+serialize карточек стало меньше —
    значит парсер не распознал какой-то формат и перезапись уничтожит данные.
    Считаем строки вида '- ...' или '* ...' ВНУТРИ секции ## (не преамбула)."""
    count = 0
    in_section = False
    for line in raw.splitlines():
        h = line.strip()
        if h.startswith("##"):
            in_section = True
            continue
        if not in_section:
            continue
        s = h
        if s.startswith(("- ", "* ")) and len(s) > 2:
            count += 1
    return count


def _parse_tasks(text: str):
    """(preamble, cols) — preamble = всё до первого распознанного '## <Колонка>'.
    Карточки с checkbox '- [ ] text' — парсятся в соответствующую колонку.
    Карточки без checkbox '- text' — парсятся как Backlog (агент иногда пишет так).
    Description строки '  > текст' сразу после карточки — собираются в card['description'].
    Строки, не являющиеся карточками, внутри секций отбрасываются при перезаписи."""
    cols = {key: [] for key, _, _ in BOARD_COLUMNS}
    preamble_lines: list[str] = []
    cur = None
    seen_header = False
    last_card: dict | None = None  # последняя добавленная карточка — приёмник description
    for line in text.splitlines():
        h = line.strip()
        if h.startswith("##"):
            name = h.lstrip("#").strip().lower()
            cur = _LABEL_TO_COL.get(name)  # None для незнакомых секций
            last_card = None  # новая секция сбрасывает receiver
            if cur is not None:
                seen_header = True
            elif not seen_header:
                preamble_lines.append(line)
            continue
        # Description строка — '  > текст', сразу после карточки
        if cur is not None and last_card is not None:
            dm = _DESC_LINE_RE.match(line)
            if dm:
                desc_line = dm.group(1)
                if last_card.get("description") is None:
                    last_card["description"] = desc_line
                else:
                    last_card["description"] += "\n" + desc_line
                continue
            # Иная строка — конец description блока
            last_card = None
        m = _CARD_RE.match(line)
        if m and cur is not None:
            cid, cardtext = _extract_id_and_text(m.group(2))
            if cardtext:
                card: dict = {"id": cid, "text": cardtext}
                cols[cur].append(card)
                last_card = card
        elif cur is not None:
            # Нет checkbox-совпадения — пробуем plain '- текст' (агентский стиль)
            pm = _PLAIN_CARD_RE.match(line)
            if pm:
                cid, cardtext = _extract_id_and_text(pm.group(1))
                if cardtext:
                    # Plain-карточки всегда в текущую колонку (агент сам выбрал секцию)
                    card = {"id": cid, "text": cardtext}
                    cols[cur].append(card)
                    last_card = card
        elif not seen_header:
            preamble_lines.append(line)
    return "\n".join(preamble_lines).rstrip(), cols


def _serialize_tasks(preamble: str, cols: dict, project_name: str) -> str:
    if not preamble.strip():
        preamble = f"# Tasks — {project_name}"
    out = [preamble, ""]
    for key, label, status in BOARD_COLUMNS:
        out.append(f"## {label}")
        for card in cols[key]:
            out.append(f"- [{status}] {card['text']} <!--ops:{card['id']}-->")
            desc = card.get("description")
            if desc:
                for desc_line in desc.splitlines():
                    out.append(f"  > {desc_line}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _load_board(cwd: str):
    tp = _tasks_path(cwd)
    raw = tp.read_text(encoding="utf-8") if tp.exists() else ""
    preamble, cols = _parse_tasks(raw)
    return raw, preamble, cols


def _save_board(cwd: str, name: str, preamble: str, cols: dict) -> None:
    _tasks_path(cwd).write_text(_serialize_tasks(preamble, cols, name), encoding="utf-8")


# ─────────────────────────── Error scanner (incidents) ───────────────────────────
#
# Сканер падений (логи + тесты) → карточки в Failed-секции TASKS.md.
# Карточка-инцидент = обычная карточка с маркером ID вида "err-<hash6>".
# Метаданные (source, seen, first, last, excerpt) хранятся в description ('  > ' строки)
# в виде key=value — это переживает round-trip парсера и видно агенту в plain-md.
#
# Дедуп: хеш по (source_type, normalized_message, file?, line?). Если карточка с таким
# err-<hash> уже есть в Failed/Review/InProgress — обновляем seen+last в description,
# новую НЕ создаём (иначе один зависший воркер плодит 1000 карточек за ночь).

# Python traceback: "Traceback (most recent call last):" ... последняя строка с типом
_PY_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):\n((?:.+\n)+?)([A-Z][\w.]*(?:Error|Exception|Warning|Exit)):\s*(.+)",
    re.MULTILINE,
)
# Generic ERROR/CRITICAL: строка лога вида "... ERROR ... msg" / "... CRITICAL ... msg"
_GENERIC_ERR_RE = re.compile(
    r"^.*\b(ERROR|CRITICAL|FATAL)\b[:\s]+(.+?)$", re.MULTILINE,
)
# pytest: "FAILED tests/test_x.py::test_y - AssertionError: msg"
_PYTEST_FAILED_RE = re.compile(
    r"^FAILED\s+([\w./\-]+)::([\w\[\]\-]+)(?:\s+-\s+(.+))?$", re.MULTILINE,
)
# Стандартная строка необработанного исключения: "UNHANDLED exc_class=<Type> path=<route>"
_UNHANDLED_RE = re.compile(r"\bUNHANDLED\s+exc_class=(\S+)\s+path=(\S+)", re.MULTILINE)
# Шумовые сообщения которые часто встречаются в логах но не являются ошибками
_LOG_NOISE_SUBSTRINGS = (
    "deprecat",         # DeprecationWarning
    "GET /api/health",  # health-checks
    "200 OK",
)


def _hash6(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:6]


def _norm_msg(msg: str) -> str:
    """Нормализация сообщения для хеша: убираем числа, временные id, пути.
    Цель — '<id=42>' и '<id=99>' дают один хеш, а '<KeyError>' и '<ValueError>' разные."""
    s = msg.lower()
    s = re.sub(r"0x[0-9a-f]+", "0xN", s)            # адреса
    s = re.sub(r"\b\d{4,}\b", "N", s)               # длинные числа (PID/timestamp)
    s = re.sub(r"/[\w/.\-]+", "/PATH", s)           # пути
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


def _parse_log_errors(log_text: str, source: str = "log") -> list[dict]:
    """Извлекает ошибки из лог-текста. Возвращает list[{source, type, message, excerpt, hash}].
    Дедуп ВНУТРИ списка: одинаковые ошибки в одном прогоне → одна запись (seen считается выше)."""
    out: list[dict] = []
    seen_hashes: set[str] = set()
    traceback_exc_types: set[str] = set()

    # Сначала Python tracebacks (более структурированные)
    for m in _PY_TRACEBACK_RE.finditer(log_text):
        trace_body = m.group(1)
        exc_type = m.group(2)
        traceback_exc_types.add(exc_type)
        exc_msg = m.group(3).strip()
        excerpt_lines = trace_body.strip().split("\n")[-3:] + [f"{exc_type}: {exc_msg}"]
        excerpt = "\n".join(ln.strip()[:200] for ln in excerpt_lines)
        h = _hash6(f"{source}|{exc_type}|{_norm_msg(exc_msg)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append({
            "source": source, "type": exc_type, "message": exc_msg,
            "excerpt": excerpt, "hash": h,
        })

    # Generic ERROR/CRITICAL — фильтруем дубли python-трейсов (они уже в out)
    for m in _GENERIC_ERR_RE.finditer(log_text):
        level = m.group(1)
        msg = m.group(2).strip()
        if any(noise in msg.lower() for noise in _LOG_NOISE_SUBSTRINGS):
            continue
        # Если строка содержит "Traceback" — уже учтено выше
        if "Traceback" in msg:
            continue
        # "UNHANDLED exc_class=..." обрабатывает отдельный проход ниже — не дублируем
        if "UNHANDLED exc_class=" in msg:
            continue
        h = _hash6(f"{source}|{level}|{_norm_msg(msg)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append({
            "source": source, "type": level, "message": msg[:300],
            "excerpt": msg[:300], "hash": h,
        })

    # UNHANDLED стандартная строка: "UNHANDLED exc_class=<Type> path=<route>"
    for m in _UNHANDLED_RE.finditer(log_text):
        exc_class = m.group(1)
        path = m.group(2)
        # Если для этого типа уже есть карточка из трейсбека (богаче) — не дублируем.
        # UNHANDLED-проход нужен прежде всего когда полного трейсбека нет (systemd OnFailure).
        if exc_class in traceback_exc_types:
            continue
        h = _hash6(f"{source}|UNHANDLED|{_norm_msg(exc_class + ' ' + path)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        matched_line = m.group(0)
        out.append({
            "source": source, "type": exc_class,
            "message": f"unhandled at {path}",
            "excerpt": matched_line, "hash": h,
        })

    return out


def _parse_pytest_failures(pytest_output: str) -> list[dict]:
    """Извлекает FAILED-строки из pytest-output."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _PYTEST_FAILED_RE.finditer(pytest_output):
        file_ = m.group(1)
        test = m.group(2)
        reason = (m.group(3) or "").strip()
        h = _hash6(f"test|{file_}|{test}|{_norm_msg(reason)}")
        if h in seen:
            continue
        seen.add(h)
        out.append({
            "source": "test", "type": "FAILED", "message": f"{test} — {reason}" if reason else test,
            "excerpt": f"{file_}::{test}\n{reason}".strip(), "hash": h,
            "file": file_, "test": test,
        })
    return out


# Маркер ID для err-карточек: 'err-<hash6>'. Описание — k=v строки.
# heal_attempted — флаг предохранитель: инцидент уже пробовали починить (не повторять).
# heal_skip — помечает инцидент как benign/игнорируемый: heal не запускать никогда.
_ERR_DESC_RE = re.compile(r"^(source|seen|first|last|excerpt|heal_attempted|heal_skip)=(.*)$")


def _parse_incident_desc(desc: str | None) -> dict:
    """Парсит description err-карточки в dict. Неизвестные строки игнорируем."""
    out: dict = {}
    if not desc:
        return out
    for line in desc.splitlines():
        m = _ERR_DESC_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _format_incident_desc(meta: dict) -> str:
    """Сериализует metadata err-карточки в description-строки.
    Excerpt идёт ПОСЛЕДНИМ — может быть многострочным, но для нас это одна логическая
    запись (хранится как одна строка с \\n заменёнными на ' / ' для компактности).
    heal_attempted — предохранитель от повторного запуска починки.
    heal_skip — помечает инцидент как benign: heal не запускается никогда."""
    lines: list[str] = []
    for key in ("source", "seen", "first", "last"):
        if key in meta:
            lines.append(f"{key}={meta[key]}")
    if meta.get("heal_attempted"):
        lines.append("heal_attempted=true")
    if meta.get("heal_skip"):
        lines.append(f"heal_skip={meta['heal_skip']}")
    excerpt = meta.get("excerpt", "")
    if excerpt:
        # Многострочный excerpt сворачиваем в одну строку. splitlines() ловит ВСЕ
        # разделители (вкл. U+2028/U+2029/\x85) — иначе они порвали бы формат доски.
        compact = " / ".join(excerpt.splitlines())[:400]
        lines.append(f"excerpt={compact}")
    return "\n".join(lines)


def _is_incident_card(card: dict) -> bool:
    """Карточка-инцидент = id начинается с 'err-'."""
    return card.get("id", "").startswith("err-")


def _incident_title(err: dict) -> str:
    """Короткий заголовок карточки: '[ERR] AttributeError: msg' / '[TEST] test_name — reason'."""
    msg = err["message"][:80]
    if err["source"] == "test":
        return f"[TEST] {msg}"
    if err["source"] == "log":
        return f"[ERR] {err['type']}: {msg}" if err.get("type") else f"[ERR] {msg}"
    return f"[{err['source'].upper()}] {msg}"


async def _run_log_cmd(log_cmd: str, timeout: float = 10.0, raise_on_timeout: bool = False) -> str:
    """Запускает log_cmd, возвращает stdout (+ stderr).
    UI-controlled cmd from topics.json → exec (not shell) to prevent injection.
    raise_on_timeout=True: при таймауте убивает процесс и re-raise asyncio.TimeoutError
    (вместо возврата ""). Используется HTTP-маршрутом для возврата 504.
    raise_on_timeout=False (default): проглатывает TimeoutError и возвращает "" —
    сохраняет поведение сканера (_scan_project_errors)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(log_cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            if raise_on_timeout:
                raise
            return ""
        return stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        raise
    except Exception:
        return ""


async def _scan_project_errors(project: dict) -> list[dict]:
    """Сканирует один проект: только log_cmd → list[errors]. БЕЗ записи на диск.
    Тесты запускаются ТОЛЬКО через кнопку «Прогнать тесты» (api_project_test), не здесь.

    Spec-012 Ф0: high-water-mark fingerprint.
    Состояние: data/scan_state.json  {cwd: {"last_line": "<sha1>", "last_scan_ts": <float>}}.
    Логика:
      - Нет fingerprint (первый скан): парсим только ПОСЛЕДНИЕ 50 строк и сохраняем fingerprint
        без немедленного создания карточек по всему хвосту (избегаем flood из старых ошибок).
      - Есть fingerprint: ищем последнее вхождение строки с sha1==fingerprint, парсим всё ПОСЛЕ.
        Если fingerprint не найден (лог ротирован/вышел из окна): fallback = последние 500 строк
        (дедуп downstream защищает от дублей).
      - После парсинга: обновляем last_line = sha1(последней непустой строки), last_scan_ts = now.
    Ключ в state: cwd (стабильный абсолютный путь проекта).
    """
    errors: list[dict] = []
    log_cmd = project.get("log_cmd")

    if log_cmd:
        log_text = await _run_log_cmd(log_cmd)
        if log_text:
            all_lines = log_text.splitlines()
            cwd_key = project.get("cwd", "")
            now_ts = time.time()
            _FP_BLOCK = 6  # блок из последних N строк как «отпечаток» позиции — устойчив
                           # к повторяющимся ОДИНОЧНЫМ строкам (heartbeat / "200 OK"), на
                           # которых single-line fingerprint пропускал новые ошибки между
                           # двумя одинаковыми строками. Блок ловит реальную позицию конца.

            state = _scan_state_load()
            last_block = state.get(cwd_key, {}).get("block")  # list[sha1] последних N строк прошлого скана
            line_hashes = [hashlib.sha1(ln.encode("utf-8", "replace")).hexdigest() for ln in all_lines]

            if not last_block:
                # Первый скан (нет состояния): парсим только хвост 50 строк, чтобы не
                # залить доску историческими ошибками. Блок-отпечаток сохраним ниже.
                if all_lines:
                    errors.extend(_parse_log_errors("\n".join(all_lines[-50:]), source="log"))
            else:
                # ПЕРВОЕ вхождение блока (forward) — всё ПОСЛЕ него считаем новым.
                # Forward-bias безопаснее last-occurrence: при повторе блока скорее
                # перепарсим старое (дедуп + dismissed погасят), чем пропустим новое.
                bl = len(last_block)
                end_idx = None
                for end in range(bl, len(line_hashes) + 1):
                    if line_hashes[end - bl:end] == last_block:
                        end_idx = end
                        break
                # Блок не найден (ротация / снос state) → fallback 500 (дедуп/dismissed страхуют).
                new_lines = all_lines[end_idx:] if end_idx is not None else all_lines[-500:]
                if new_lines:
                    errors.extend(_parse_log_errors("\n".join(new_lines), source="log"))

            # ВСЕГДА сохраняем блок конца текущего вывода (даже whitespace-only — иначе
            # застрянем в режиме «первый скан» и будем терять строки за 50-хвостом).
            state[cwd_key] = {"block": line_hashes[-_FP_BLOCK:], "last_scan_ts": now_ts}
            _scan_state_save(state)

    return errors


async def _ingest_errors_to_board(cwd: str, name: str, errors: list[dict]) -> tuple[int, int]:
    """Записывает/обновляет err-карточки в TASKS.md. Возвращает (added, updated).
    Под board-lock. Дедуп: карточка err-<hash> уже есть → обновляем seen/last в description."""
    if not errors:
        return (0, 0)

    lock = _get_board_lock(cwd)
    async with lock:
        raw, preamble, cols = _load_board(cwd)
        # Guard: если файл нет/не парсится — лучше не трогать
        potential = _count_potential_cards(raw)
        parsed_count = sum(len(v) for v in cols.values())
        if raw.strip() and parsed_count < potential:
            return (0, 0)  # подозрительный файл — не пишем

        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")
        added = 0
        updated = 0

        # Индекс существующих err-карточек: hash → (column, card)
        existing: dict[str, tuple[str, dict]] = {}
        for col_key, col_cards in cols.items():
            for card in col_cards:
                cid = card.get("id", "")
                if cid.startswith("err-"):
                    h = cid[4:]
                    existing[h] = (col_key, card)

        now_float = time.time()
        dismissed_snapshot = _dismissed_load()  # один раз на батч, не на каждую ошибку
        for err in errors:
            h = err["hash"]
            if h in existing:
                # Update seen+last, не двигаем колонку (юзер мог уже перенести)
                col_key, card = existing[h]
                meta = _parse_incident_desc(card.get("description"))
                try:
                    seen_n = int(meta.get("seen", "1")) + 1
                except ValueError:
                    seen_n = 2
                meta["seen"] = str(seen_n)
                meta["last"] = now_iso
                # first / source / excerpt — оставляем из первой встречи
                card["description"] = _format_incident_desc(meta)
                updated += 1
            else:
                # Spec-012 Ф0 Task B: не воскрешать dismissed инциденты в TTL-окне
                _dts = dismissed_snapshot.get(h)
                if _dts is not None and (now_float - _dts) < _DISMISS_TTL:
                    continue
                # Новая карточка в Failed
                meta = {
                    "source": err["source"],
                    "seen": "1",
                    "first": now_iso,
                    "last": now_iso,
                    "excerpt": err.get("excerpt", ""),
                }
                cols["failed"].append({
                    "id": f"err-{h}",
                    "text": _incident_title(err),
                    "description": _format_incident_desc(meta),
                })
                added += 1

        if added or updated:
            _save_board(cwd, name, preamble, cols)
        return (added, updated)


_REPORT_DEBOUNCE: "dict[str, float]" = {}   # hash → ts последнего in-process репорта
_REPORT_DEBOUNCE_SEC = 10.0                  # одинаковый инцидент чаще раза в N сек не пишем


async def _report_incident(ctx: dict, exc_class: str, where: str, project_id: str = "claude-ops-bot") -> None:
    """Spec-012 Ф1/Ф3: ПРЯМОЙ (in-process) репорт одного инцидента → карточка,
    минуя лог-сканер и его задержку. Hash идентичен тому, что даёт `_parse_log_errors`
    на строке `UNHANDLED exc_class=.. path=..` (source="log") → дедуп: кто первый
    (этот путь или сканер), тот создаёт карточку; второй бампит seen. Резолвит проект
    сам (вся работа — в фоне, чтобы не тормозить ответ). Глотает все исключения —
    наблюдаемость не должна ронять запрос. Переиспользуется push-эндпоинтом (Ф3)."""
    try:
        h = _hash6(f"log|UNHANDLED|{_norm_msg(exc_class + ' ' + where)}")
        # Дебаунс: эндпоинт, падающий на КАЖДОМ запросе, не должен устроить I/O-шторм
        # записей в TASKS.md. Один и тот же hash пишем не чаще раза в N сек (карточка
        # уже создана; редкие пропущенные seen++ доберёт фоновый сканер). До board-lock.
        # Ключ включает project_id — иначе одинаковая ошибка в РАЗНЫХ проектах (path
        # нормализуется в /PATH → общий hash) глушила бы друг друга кросс-проектно.
        dkey = f"{project_id}\x00{h}"
        now = time.time()
        last = _REPORT_DEBOUNCE.get(dkey)
        if last is not None and (now - last) < _REPORT_DEBOUNCE_SEC:
            return
        _REPORT_DEBOUNCE[dkey] = now
        if len(_REPORT_DEBOUNCE) > 256:   # не растим словарь
            for k in [k for k, v in _REPORT_DEBOUNCE.items() if now - v > _REPORT_DEBOUNCE_SEC]:
                _REPORT_DEBOUNCE.pop(k, None)
        proj = _find_project_by_id(ctx, project_id)
        if not proj:
            return
        err = {
            "source": "log",
            "type": exc_class,
            "message": f"unhandled at {where}",
            "excerpt": f"UNHANDLED exc_class={exc_class} path={where}",
            "hash": h,
        }
        await _ingest_errors_to_board(proj["cwd"], proj["name"], [err])
    except Exception:
        pass


async def _scan_and_ingest(project: dict, ctx: dict | None = None) -> dict:
    """Полный цикл: сканируем проект, заливаем в доску, опц. TG-нотификация.
    Возвращает {ok, added, updated, scanned}."""
    try:
        errors = await _scan_project_errors(project)
    except Exception as e:
        return {"ok": False, "error": str(e), "scanned": 0, "added": 0, "updated": 0}

    try:
        added, updated = await _ingest_errors_to_board(project["cwd"], project["name"], errors)
    except Exception as e:
        return {"ok": False, "error": str(e), "scanned": len(errors), "added": 0, "updated": 0}

    # TG-нотификация про НОВЫЕ инциденты (не дедуп-updates)
    if added > 0 and ctx and project.get("notify_on_error"):
        try:
            ptb_app = ctx.get("ptb_app")
            tg_thread_str = project.get("tg_thread", "")
            if ptb_app and ":" in tg_thread_str:
                chat_s, thread_s = tg_thread_str.split(":", 1)
                chat_id = int(chat_s)
                thread_id = int(thread_s) if thread_s.isdigit() else None
                msg = f"🚨 <b>{added}</b> новых инцидентов в <b>{project['name']}</b> — см. доску."
                await ptb_app.bot.send_message(
                    chat_id, msg, message_thread_id=thread_id, parse_mode="HTML",
                )
        except Exception as e:
            print(f"[scan_and_ingest] tg notify failed for {project['name']}: {e}")

    return {"ok": True, "scanned": len(errors), "added": added, "updated": updated}


async def api_project_scan_errors(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/scan-errors — ручной запуск сканера для одного проекта."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if not project.get("log_cmd"):
        return web.json_response({
            "ok": False, "error": "log_cmd не настроен в topics.json",
        }, status=400)
    res = await _scan_and_ingest(project, ctx)
    return web.json_response(res)


async def api_project_incidents(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/incidents — счётчик активных инцидентов (для бейджа в сайдбаре).
    Активные = err-карточки в Failed/Review/InProgress (не в Done)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        _, _, cols = _load_board(project["cwd"])
    except Exception:
        return web.json_response({"count": 0, "by_column": {}})
    by_col = {}
    total = 0
    for key, col_cards in cols.items():
        n = sum(1 for c in col_cards if _is_incident_card(c))
        if n:
            by_col[key] = n
            total += n
    return web.json_response({"count": total, "by_column": by_col})


async def api_project_incident(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/incident — Spec-012 Ф3: опциональный push инцидента.

    Auth-exempt от cookie (auth_middleware пропускает этот маршрут), но делает
    двойной opt-in: (1) глобальный флаг incident_push_enabled=True в settings.json,
    (2) секрет CLAUDEOPS_INCIDENT_TOKEN в .claude-ops/secrets/ проекта.

    Порядок проверок (fail-safe):
    1. Глобальный флаг OFF → 404 (не раскрываем существование эндпоинта).
    2. Проект не найден → 404.
    3. Токен: секрет CLAUDEOPS_INCIDENT_TOKEN не задан (per-project opt-in) → 403.
       Токен из X-Incident-Token / тела → constant-time сравнение; мимо → 403.
    4. JSON: bad parse → 400; exc_class пустой → 400; санитизация (strip newlines, cap).
    5. Rate-limit per-project (30/мин) → 429.
    6. _report_incident fire-and-forget (дедуп = тот же hash, что у log-сканера).
    7. {"ok": True} — токен/секрет НИКОГДА не в ответе.
    """
    ctx = req.app["ctx"]
    now = time.time()

    # 1. Глобальный мастер-флаг (дешёвый, до любого I/O; выключено по умолчанию → 404)
    if _get_global_setting("incident_push_enabled", False) is not True:
        return web.json_response({"error": "not found"}, status=404)

    # 1.5. Per-IP backstop — ДО резолва проекта и чтения секрета, чтобы unauth-флуд
    # не упирался в диск (_secrets_read) на каждом запросе.
    ip = (req.headers.get("X-Forwarded-For", "").split(",")[0].strip() or req.remote or "?")
    ip_hist = [t for t in _incident_ip_history.get(ip, []) if now - t < _INCIDENT_PUSH_WINDOW]
    if len(ip_hist) >= _INCIDENT_IP_MAX:
        return web.json_response({"error": "too many requests"}, status=429)
    ip_hist.append(now)
    _incident_ip_history[ip] = ip_hist
    if len(_incident_ip_history) > 4096:   # не растим словарь
        for k in [k for k, v in list(_incident_ip_history.items()) if not v or now - v[-1] > _INCIDENT_PUSH_WINDOW]:
            _incident_ip_history.pop(k, None)

    # 2. Проект
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "not found"}, status=404)

    # 3. Тело парсим ОДИН раз (битый JSON → 400, а не маскируется под 403 token-mismatch)
    try:
        body = await req.json()
        if not isinstance(body, dict):
            raise ValueError("not a dict")
    except Exception:
        return web.json_response({"error": "bad request: invalid JSON"}, status=400)

    # 4. Токен — per-project opt-in (заголовок предпочтительно, иначе из тела)
    expected_token = _secrets_read(project["cwd"]).get("CLAUDEOPS_INCIDENT_TOKEN") or ""
    if not expected_token:
        return web.json_response({"error": "forbidden"}, status=403)   # push не включён для проекта
    body_token = body.get("token", "")
    presented_token = req.headers.get("X-Incident-Token", "") or (body_token if isinstance(body_token, str) else "")
    if not _hmac.compare_digest(str(presented_token), str(expected_token)):
        return web.json_response({"error": "forbidden"}, status=403)

    # 5. Санитизация: splitlines() ловит ВСЕ разделители строк (вкл. U+2028/U+2029/\x85) —
    # иначе токен-холдер мог бы инжектить '## Section' / '- [ ] card' в TASKS.md.
    def _sanitize(s, maxlen: int) -> str:
        return " ".join(str(s).splitlines()).strip()[:maxlen]

    exc_class = _sanitize(body.get("exc_class", ""), 120)
    where = _sanitize(body.get("where") or body.get("path") or "(push)", 200)
    if not exc_class:
        return web.json_response({"error": "bad request: exc_class required"}, status=400)

    # 6. Per-project rate-limit
    history = [t for t in _incident_push_history.get(pid, []) if now - t < _INCIDENT_PUSH_WINDOW]
    if len(history) >= _INCIDENT_PUSH_MAX:
        return web.json_response({"error": "too many requests"}, status=429)
    history.append(now)
    _incident_push_history[pid] = history

    # 7. Репорт — fire-and-forget. Дедуп по hash общий с лог-сканером (one error = one card).
    _spawn_bg(_report_incident(ctx, exc_class, where, project_id=pid))

    # 8. Ответ — токен/секрет НИКОГДА не раскрывается
    return web.json_response({"ok": True})


async def api_project_self_heal_toggle(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/self-heal {enabled: bool} — включить/выключить самолечение.

    Сохраняет флаг self_heal в topics.json для ВСЕХ записей с тем же cwd.
    Auth: требует сессионный cookie (стандартный middleware).
    ПРЕДОХРАНИТЕЛЬ: не включает ни для одного проекта по умолчанию — только по явному запросу.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if project.get("is_free"):
        return web.json_response({"error": "самолечение недоступно для свободных чатов"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    enabled = bool(body.get("enabled", False))

    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            b["self_heal"] = enabled
            changed += 1

    if changed:
        save_topics = ctx.get("save_topics")
        if callable(save_topics):
            save_topics()

    return web.json_response({"ok": True, "self_heal": enabled, "topics_updated": changed})


async def api_project_notify_toggle(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/notify-on-error {enabled: bool} — TG-уведомления о новых ошибках.

    При включении: сканер шлёт пинг в TG-топик проекта при детекте новых инцидентов
    («упало»). Независимо от самолечения. Флаг notify_on_error в topics.json для всех
    записей с тем же cwd. Auth: стандартный middleware.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if project.get("is_free"):
        return web.json_response({"error": "уведомления недоступны для свободных чатов"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    enabled = bool(body.get("enabled", False))

    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            b["notify_on_error"] = enabled
            changed += 1

    if changed:
        save_topics = ctx.get("save_topics")
        if callable(save_topics):
            save_topics()

    return web.json_response({"ok": True, "notify_on_error": enabled, "topics_updated": changed})


# Фоновая задача: сканер всех проектов каждые SCAN_INTERVAL_SEC секунд.
# Spec-012 Ф0: дефолт снижен до 60с (инкрементальный парс — дёшево; env override сохранён).
_SCAN_INTERVAL_SEC = int(os.environ.get("ERROR_SCAN_INTERVAL", "60"))  # 1 мин (was 5 мин)

# ─────────────────────────── Scan state (Spec 012 Ф0) ────────────────────────
#
# Высокая отметка воды: per-project fingerprint последней обработанной строки лога.
# Файл: data/scan_state.json  {<cwd>: {"last_line": "<sha1>", "last_scan_ts": <float>}}
# Отклонённые инциденты: data/dismissed_incidents.json  {<hash6>: <dismissed_ts>}
# Оба файла живут в data/ (gitignored). Все хелперы глотают ВСЕ исключения — ни один
# не может сломать сканер.

_SCAN_STATE_PATH: "Path | None" = None      # задаётся в _scan_state_init(ctx)
_DISMISSED_PATH: "Path | None" = None       # задаётся в _scan_state_init(ctx)
_DISMISS_TTL = 24 * 3600                    # 24 ч — deleted/done карточка не воскресает

# ─────────────────────────── Card Queue (sequential per-project) ───────────────────────────
# data/card_queue.json: {session_key: [card_id, ...]} — FIFO очередь ожидающих карточек.
# Одна карточка на проект за раз; остальные ждут в очереди.

_QUEUE_PATH: "Path | None" = None           # задаётся в _scan_state_init(ctx)

_QUEUE_DRAIN_INTERVAL_SEC = 3               # интервал backstop-дренажного цикла

# In-memory canonical очередь: {session_key: [card_id, ...]}. Единственный источник
# истины в рамках процесса. Все мутации меняют _QUEUE СИНХРОННО + сразу флашат на диск.
# Это устраняет RMW-гонку (read-modify-write через await): _drain_queue делает несколько
# мутаций через await, и concurrent enqueue/remove не теряются — мутация атомарна в рамках
# одного event-loop turn (нет await между чтением и записью _QUEUE).
_QUEUE: "dict[str, list[str]]" = {}


def _scan_state_init(ctx: dict) -> None:
    """Вызывается из start() — задаёт пути к файлам состояния. Загружает очередь в _QUEUE."""
    global _SCAN_STATE_PATH, _DISMISSED_PATH, _QUEUE_PATH
    _SCAN_STATE_PATH = ctx["DATA"] / "scan_state.json"
    _DISMISSED_PATH = ctx["DATA"] / "dismissed_incidents.json"
    _QUEUE_PATH = ctx["DATA"] / "card_queue.json"
    # Загружаем persisted-очередь в in-memory canonical dict (restart-resume).
    # Чистим _QUEUE сначала — изоляция тестов и повторного init.
    _QUEUE.clear()
    try:
        if _QUEUE_PATH is not None and _QUEUE_PATH.exists():
            data = json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        _QUEUE[k] = [c for c in v if isinstance(c, str)]
    except Exception:
        pass


def _queue_flush() -> None:
    """Флашит in-memory _QUEUE на диск. Глотает ВСЕ исключения.
    _QUEUE_PATH is None → только память, не падаем (тесты без init)."""
    try:
        if _QUEUE_PATH is None:
            return
        _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _QUEUE_PATH.write_text(json.dumps(_QUEUE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _queue_enqueue(session_key: str, card_id: str) -> bool:
    """Добавляет card_id в хвост очереди (dedup). Возвращает True если реально добавил,
    False если уже был (дедуп). Мутация _QUEUE синхронна → флаш."""
    try:
        lst = _QUEUE.setdefault(session_key, [])
        if card_id in lst:
            return False
        lst.append(card_id)
        _queue_flush()
        return True
    except Exception:
        return False


def _queue_remove(session_key: str, card_id: str) -> None:
    """Удаляет card_id из очереди для session_key (нет — тихо). Мутация синхронна → флаш."""
    try:
        lst = _QUEUE.get(session_key)
        if lst is not None and card_id in lst:
            lst.remove(card_id)
            _queue_flush()
    except Exception:
        pass


def _queue_for(session_key: str) -> list:
    """Возвращает список card_id в очереди для session_key (FIFO) — копия из _QUEUE."""
    try:
        return list(_QUEUE.get(session_key, []))
    except Exception:
        return []


def _scan_state_load() -> dict:
    """Загружает {cwd: {last_line, last_scan_ts}}. Ошибки/отсутствие → {}."""
    try:
        if _SCAN_STATE_PATH is None or not _SCAN_STATE_PATH.exists():
            return {}
        data = json.loads(_SCAN_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _scan_state_save(state: dict) -> None:
    """Сохраняет state на диск. Глотает ВСЕ исключения."""
    try:
        if _SCAN_STATE_PATH is None:
            return
        _SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SCAN_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _dismissed_load() -> dict:
    """Загружает {hash6: dismissed_ts}. Ошибки/отсутствие → {}."""
    try:
        if _DISMISSED_PATH is None or not _DISMISSED_PATH.exists():
            return {}
        data = json.loads(_DISMISSED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dismissed_save(dismissed: dict) -> None:
    """Сохраняет dismissed на диск. Глотает ВСЕ исключения."""
    try:
        if _DISMISSED_PATH is None:
            return
        _DISMISSED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DISMISSED_PATH.write_text(json.dumps(dismissed), encoding="utf-8")
    except Exception:
        pass


def _dismissed_add(h: str) -> None:
    """Записывает hash как dismissed(now). Прунит устаревшие (>TTL). Глотает ВСЕ исключения."""
    try:
        now = time.time()
        dismissed = _dismissed_load()
        dismissed[h] = now
        # Прунинг: удаляем записи старше TTL
        dismissed = {k: v for k, v in dismissed.items() if now - v < _DISMISS_TTL}
        _dismissed_save(dismissed)
    except Exception:
        pass


def _dismissed_is_active(h: str, now: float) -> bool:
    """True если hash был dismissed менее _DISMISS_TTL секунд назад."""
    try:
        dismissed = _dismissed_load()
        ts = dismissed.get(h)
        if ts is None:
            return False
        return (now - ts) < _DISMISS_TTL
    except Exception:
        return False

# ─────────────────────────── Самолечение (Spec 010) ───────────────────────────
#
# ПРЕДОХРАНИТЕЛИ (незыблемо):
# 1. OFF по умолчанию — флаг self_heal в topics.json или SELF_HEAL_ENABLED env.
# 2. НИКОГДА не auto-apply — агент доходит только до Review; Merge — руками Игоря.
# 3. Лимит 1 попытка/инцидент — heal_attempted=true пишется ДО запуска.
# 4. Лимит конкурентности — макс 2 авто-починки одновременно.
# 5. Только git+clean worktree — не-git/dirty проекты пропускаются.
# 6. Всё видно — Timeline kind:"self_heal" + TG-пинг Игорю.

_SELF_HEAL_MAX_CONCURRENT = int(os.environ.get("SELF_HEAL_MAX_CONCURRENT", "2"))
_self_heal_active_count = 0   # глобальный счётчик активных починок

# ── Ф2: Safety-layer — дебаунс / benign-фильтр / рейт-лимит ──────────────────
# Эти константы ADD новые предохранители ПОВЕРХ spec-010 gates, не заменяют их.

# B. Дебаунс: heal только если seen >= N (инцидент повторился → не транзиент)
_HEAL_MIN_SEEN = int(os.environ.get("SELF_HEAL_MIN_SEEN", "2"))

# C. Benign-классы: эти исключения НИКОГДА не лечим (benign-disconnect + transient)
_HEAL_BENIGN_DEFAULT: "tuple[str, ...]" = (
    "ConnectionResetError",
    "ClientConnectionResetError",
    "ConnectionAbortedError",
    "CancelledError",
    "TimeoutError",
)

# D. Per-project rate-limit: не более N запусков за окно (сек)
_HEAL_MAX_PER_WINDOW = int(os.environ.get("SELF_HEAL_MAX_PER_WINDOW", "3"))
_HEAL_WINDOW_SEC = int(os.environ.get("SELF_HEAL_WINDOW_SEC", "3600"))

# D. История heal-запусков: {session_key → [timestamp, ...]}
_heal_history: "dict[str, list[float]]" = {}


def _heal_rate_ok(key: str, now: float) -> bool:
    """Возвращает True, если heal-запуск разрешён (не превышен лимит окна).
    Побочный эффект: очищает устаревшие записи из _heal_history[key]."""
    history = _heal_history.get(key, [])
    # Убираем записи старше окна
    cutoff = now - _HEAL_WINDOW_SEC
    history = [t for t in history if t > cutoff]
    _heal_history[key] = history
    return len(history) < _HEAL_MAX_PER_WINDOW


def _heal_record(key: str, now: float) -> None:
    """Записывает timestamp heal-запуска; очищает устаревшие."""
    history = _heal_history.get(key, [])
    cutoff = now - _HEAL_WINDOW_SEC
    history = [t for t in history if t > cutoff]
    history.append(now)
    _heal_history[key] = history


def _heal_decision(
    card: dict,
    proj: dict,
    active_count: int,
    max_conc: int,
    running_busy: bool,
    rate_ok: bool,
    now: float,
) -> "tuple[str, str]":
    """Чистая функция: принимает решение по одной карточке-инциденту.

    Возвращает (action, reason) где action ∈ {'heal', 'skip', 'stop', 'benign'}.
    - 'heal'   → запустить _self_heal_card; вызывающий должен вызвать _heal_record.
    - 'skip'   → пропустить карточку, продолжить перебор.
    - 'benign' → пометить heal_skip=benign, пропустить.
    - 'stop'   → прервать перебор карточек (ресурсный лимит достигнут).

    Порядок проверок: cheapest/safest first (spec-012 Ф2, раздел E).
    Не изменяет внешнего состояния — только чтение."""
    if not _is_incident_card(card):
        return "skip", "not_incident"

    meta = _parse_incident_desc(card.get("description"))

    # Предохранитель №3 (spec-010): уже пытались починить
    if meta.get("heal_attempted") == "true":
        return "skip", "heal_attempted"

    # C. benign/heal_skip: уже помечен как benign
    if meta.get("heal_skip"):
        return "skip", f"heal_skip:{meta['heal_skip']}"

    # C. Benign-фильтр: проверяем title + excerpt на benign-классы
    ignore_list: "tuple[str, ...]" = _HEAL_BENIGN_DEFAULT
    extra = proj.get("heal_ignore")
    if extra and isinstance(extra, list):
        ignore_list = ignore_list + tuple(str(x) for x in extra)

    card_text = card.get("text", "")
    excerpt = meta.get("excerpt", "")
    combined = (card_text + " " + excerpt).lower()   # case-insensitive: "connectionreseterror" тоже ловим
    for substr in ignore_list:
        if substr.lower() in combined:
            return "benign", substr

    # B. Дебаунс: seen < MIN_SEEN → слишком молодой. Битый seen → трактуем как молодой (safe).
    try:
        seen = int(meta.get("seen", "1"))
    except (ValueError, TypeError):
        seen = 1
    if seen < _HEAL_MIN_SEEN:
        return "skip", f"too_young:seen={seen}"

    # Предохранитель №4 (spec-010): лимит конкурентности
    if active_count >= max_conc:
        return "stop", "concurrency_limit"

    # Предохранитель №2 (spec-010): running lock
    if running_busy:
        return "stop", "project_busy"

    # D. Рейт-лимит
    if not rate_ok:
        return "stop", "rate_limit"

    return "heal", "ok"


# ─────────────────────── глобальные настройки (data/settings.json) ───────────────────────
#
# Глобальные knob'ы кокпита, переопределяют env-дефолты в рантайме (hot-reload по mtime).
# Per-project настройки живут в topics.json (model/self_heal/notify_on_error/log_cmd/
# test_cmd/git_enabled). Глобальные — отдельный файл, т.к. не привязаны к проекту.
# Инициализация: start() вызывает _settings_init(ctx).

_SETTINGS_PATH: "Path | None" = None
_SETTINGS_CACHE: dict = {}
_SETTINGS_MTIME: float = 0.0

# Ключ → (тип, min, max) для валидации POST. None-границы = без проверки диапазона.
_GLOBAL_SETTINGS_SPEC = {
    "self_heal_enabled": ("bool", None, None),       # master-kill: False → самолечение выключено везде
    "self_heal_max_concurrent": ("int", 1, 10),
    "scan_interval_sec": ("int", 30, 3600),
    "default_model": ("model", None, None),          # "" → дефолт ctx
    "watchdog_stall_sec": ("int", 30, 7200),
    "watchdog_max_sec": ("int", 60, 14400),
    # Spec-012 Ф3: глобальный мастер-флаг push-эндпоинта. OFF по умолчанию —
    # оператор должен явно включить. Без этого флага POST /incident → 404.
    "incident_push_enabled": ("bool", None, None),
}


def _settings_init(ctx: dict) -> None:
    """Вызывается из start() — задаёт путь к data/settings.json."""
    global _SETTINGS_PATH
    _SETTINGS_PATH = ctx["DATA"] / "settings.json"


def _load_global_settings() -> dict:
    """Читает settings.json с mtime-гейтом. Битый файл → прошлый кэш."""
    global _SETTINGS_CACHE, _SETTINGS_MTIME
    if _SETTINGS_PATH is None:
        return {}
    try:
        mtime = _SETTINGS_PATH.stat().st_mtime
    except FileNotFoundError:
        _SETTINGS_CACHE = {}
        return {}
    except Exception:
        return _SETTINGS_CACHE if isinstance(_SETTINGS_CACHE, dict) else {}
    if mtime != _SETTINGS_MTIME:
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _SETTINGS_CACHE = data
                _SETTINGS_MTIME = mtime
        except Exception:
            pass  # битый/частичный файл при гонке — оставляем прошлый кэш
    return _SETTINGS_CACHE if isinstance(_SETTINGS_CACHE, dict) else {}


def _get_global_setting(key: str, fallback=None):
    """Эффективное значение глобальной настройки: из settings.json или fallback.
    Хранимое None/отсутствие → fallback."""
    val = _load_global_settings().get(key)
    return fallback if val is None else val


def _save_global_settings(data: dict) -> None:
    """Атомарно пишет settings.json и форсит перечитывание кэша."""
    global _SETTINGS_MTIME
    if _SETTINGS_PATH is None:
        return
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_name(_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_SETTINGS_PATH)
    _SETTINGS_MTIME = 0.0


def _effective_default_model(ctx: dict) -> str:
    """Дефолт-модель для новых проектов: глобальная настройка или ctx['DEFAULT_MODEL']."""
    return _get_global_setting("default_model", None) or ctx.get("DEFAULT_MODEL", "sonnet")


def _git_enabled(project: dict) -> bool:
    """git_enabled per-project (topics.json). По умолчанию True (git включён).
    False → кокпит НЕ использует git: прогон карточек legacy, git-sync 409,
    health не флажит отсутствие .git. Существующий .git физически не трогаем."""
    return project.get("git_enabled", True) is not False


def _self_heal_enabled(project: dict) -> bool:
    """Флаг самолечения: per-project self_heal ИЛИ глобальный env SELF_HEAL_ENABLED.
    Глобальный master-kill (settings.json self_heal_enabled=False) перекрывает всё.
    По умолчанию ВСЕГДА False — предохранитель №1."""
    if _get_global_setting("self_heal_enabled", True) is False:
        return False  # глобальный master-kill
    if project.get("self_heal"):
        return True
    return os.environ.get("SELF_HEAL_ENABLED", "").lower() in ("1", "true", "yes")


# ─────────────────────── API: настройки (глобальные + per-project) ───────────────────────

_PROJECT_SETTING_FIELDS = ("git_enabled", "model", "self_heal", "notify_on_error", "log_cmd", "test_cmd")


def _validate_global_settings(partial: dict) -> "tuple[dict, str | None]":
    """Валидирует частичный апдейт по _GLOBAL_SETTINGS_SPEC.
    None/"" → сброс ключа к дефолту. Возвращает (clean, None) или ({}, ошибка)."""
    clean: dict = {}
    for key, val in partial.items():
        spec = _GLOBAL_SETTINGS_SPEC.get(key)
        if spec is None:
            return {}, f"неизвестный ключ: {key}"
        typ, lo, hi = spec
        if val is None or val == "":
            clean[key] = None
            continue
        if typ == "bool":
            if not isinstance(val, bool):
                return {}, f"{key}: ожидался bool"
            clean[key] = val
        elif typ == "int":
            try:
                iv = int(val)
            except (TypeError, ValueError):
                return {}, f"{key}: ожидалось целое"
            if (lo is not None and iv < lo) or (hi is not None and iv > hi):
                return {}, f"{key}: вне диапазона [{lo}, {hi}]"
            clean[key] = iv
        elif typ == "model":
            sv = str(val).strip().lower()
            if sv not in _ALLOWED_MODELS:
                return {}, f"{key}: модель не из {sorted(_ALLOWED_MODELS)}"
            clean[key] = sv
    return clean, None


async def api_settings_get(req: web.Request) -> web.Response:
    """GET /api/settings — глобальные настройки: сохранённые + эффективные значения + спека."""
    ctx = req.app["ctx"]
    stored = dict(_load_global_settings())
    effective = {
        "self_heal_enabled": _get_global_setting("self_heal_enabled", True),
        "self_heal_max_concurrent": int(_get_global_setting("self_heal_max_concurrent", _SELF_HEAL_MAX_CONCURRENT)),
        "scan_interval_sec": int(_get_global_setting("scan_interval_sec", _SCAN_INTERVAL_SEC)),
        "default_model": _get_global_setting("default_model", ctx.get("DEFAULT_MODEL", "sonnet")),
        "watchdog_stall_sec": int(_get_global_setting("watchdog_stall_sec", int(os.environ.get("STALL_SECONDS", "300")))),
        "watchdog_max_sec": int(_get_global_setting("watchdog_max_sec", int(os.environ.get("MAX_SECONDS", "1800")))),
    }
    spec = {k: {"type": v[0], "min": v[1], "max": v[2]} for k, v in _GLOBAL_SETTINGS_SPEC.items()}
    return web.json_response({"stored": stored, "effective": effective, "spec": spec})


async def api_settings_post(req: web.Request) -> web.Response:
    """POST /api/settings — частичный апдейт глобальных настроек (валидируется)."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "ожидался объект"}, status=400)
    clean, err = _validate_global_settings(body)
    if err:
        return web.json_response({"error": err}, status=400)
    current = dict(_load_global_settings())
    for k, v in clean.items():
        if v is None:
            current.pop(k, None)   # сброс к дефолту
        else:
            current[k] = v
    _save_global_settings(current)
    return web.json_response({"ok": True, "stored": current})


def _project_settings_view(project: dict) -> dict:
    return {
        "git_enabled": _git_enabled(project),
        "model": project.get("model"),
        "self_heal": bool(project.get("self_heal", False)),
        "notify_on_error": bool(project.get("notify_on_error", False)),
        "log_cmd": project.get("log_cmd") or "",
        "test_cmd": project.get("test_cmd") or "",
    }


async def api_project_settings_get(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/settings — per-project настройки."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    return web.json_response(_project_settings_view(project))


async def api_project_settings_post(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/settings — частичный апдейт per-project настроек в topics.json.

    Пишет во ВСЕ записи topics с этим cwd (как rename). git_enabled и пр. подхватываются
    hot-reload'ом. Возвращает обновлённый срез настроек."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "ожидался объект"}, status=400)

    updates: dict = {}
    for k, v in body.items():
        if k not in _PROJECT_SETTING_FIELDS:
            return web.json_response({"error": f"неизвестный ключ: {k}"}, status=400)
        if k in ("git_enabled", "self_heal", "notify_on_error"):
            if not isinstance(v, bool):
                return web.json_response({"error": f"{k}: ожидался bool"}, status=400)
            updates[k] = v
        elif k == "model":
            sv = str(v).strip().lower()
            if sv not in _ALLOWED_MODELS:
                return web.json_response({"error": f"model: не из {sorted(_ALLOWED_MODELS)}"}, status=400)
            updates[k] = sv
        else:  # log_cmd / test_cmd — строки; пусто → сброс ключа
            updates[k] = str(v) if v else None

    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            for k, v in updates.items():
                if v is None:
                    b.pop(k, None)
                else:
                    b[k] = v
            changed += 1
    save_topics = ctx.get("save_topics")
    if callable(save_topics):
        save_topics()

    project = _find_project_by_id(ctx, req.match_info["id"]) or project
    return web.json_response({"ok": True, "topics_updated": changed, "settings": _project_settings_view(project)})


async def _send_tg_ping(ctx: dict, project: dict, msg: str) -> None:
    """Отправляет HTML-сообщение Игорю в TG-топик проекта. Некритичен."""
    try:
        ptb_app = ctx.get("ptb_app")
        tg_thread_str = project.get("tg_thread", "")
        if ptb_app and tg_thread_str and ":" in str(tg_thread_str):
            chat_s, thread_s = str(tg_thread_str).split(":", 1)
            chat_id = int(chat_s)
            thread_id = int(thread_s) if thread_s.isdigit() else None
            await ptb_app.bot.send_message(
                chat_id, msg, message_thread_id=thread_id, parse_mode="HTML",
            )
    except Exception as e:
        print(f"[self_heal] TG-пинг не удался: {e}")


async def _sync_forum_topic_name(ctx: dict, session_key: str, name: str) -> None:
    """editForumTopic: синкает имя TG-топика с именем проекта (после rename).
    Некритичен. Для синтетических ключей (топик не существует) — тихо игнорим."""
    try:
        ptb_app = ctx.get("ptb_app")
        if not (ptb_app and session_key and ":" in str(session_key)):
            return
        chat_s, thread_s = str(session_key).split(":", 1)
        if not thread_s.isdigit() or int(thread_s) == 0:
            return
        await ptb_app.bot.edit_forum_topic(
            chat_id=int(chat_s), message_thread_id=int(thread_s), name=name,
        )
    except Exception as e:
        print(f"[rename] edit_forum_topic не удался (возможно синтетический ключ): {e}")


async def _notify_new_incidents(ctx: dict, project: dict, n_added: int) -> None:
    """TG-пинг «упало»: при детекте новых инцидентов, если включён notify_on_error.
    Перечисляет до 3 инцидентов из Failed. Некритичен."""
    try:
        _, _, cols = _load_board(project["cwd"])
    except Exception:
        return
    texts = [c["text"] for c in cols.get("failed", []) if _is_incident_card(c)]
    if not texts:
        return
    head = "\n".join(f"• {t[:100]}" for t in texts[:3])
    more = f"\n…и ещё {len(texts) - 3}" if len(texts) > 3 else ""
    msg = (
        f"❌ <b>{project['name']}</b>: {n_added} новых ошибок\n{head}{more}\n"
        f"<i>Таб «Доска» → Failed.</i>"
    )
    await _send_tg_ping(ctx, project, msg)


async def _self_heal_card(ctx: dict, project: dict, incident_card: dict) -> None:
    """Петля починки одного инцидента. Запускается как asyncio.create_task.

    ПРЕДОХРАНИТЕЛИ:
    - heal_attempted ставится ДО запуска (предотв. зацикливание при краше).
    - Агент доходит ТОЛЬКО до Review. НИКОГДА не auto-apply.
    - Счётчик активных починок управляется снаружи (в scanner loop).
    """
    global _self_heal_active_count
    cwd = project["cwd"]
    name = project["name"]
    session_key = project["tg_thread"]
    card_id = incident_card["id"]
    card_text = incident_card["text"]
    card_desc = incident_card.get("description", "")

    # ПРЕДОХРАНИТЕЛЬ №3: пометить heal_attempted ДО запуска (атомарно под board-lock)
    lock = _get_board_lock(cwd)
    try:
        async with lock:
            _, preamble, cols = _load_board(cwd)
            # Ищем карточку в любой колонке
            found_card: dict | None = None
            for col_cards in cols.values():
                for c in col_cards:
                    if c["id"] == card_id:
                        found_card = c
                        break
                if found_card:
                    break
            if found_card is None:
                # Карточка исчезла (удалена пользователем?) — пропускаем
                _self_heal_active_count = max(0, _self_heal_active_count - 1)
                return
            meta = _parse_incident_desc(found_card.get("description"))
            meta["heal_attempted"] = "true"
            found_card["description"] = _format_incident_desc(meta)
            _save_board(cwd, name, preamble, cols)
    except Exception as e:
        print(f"[self_heal] ошибка при пометке heal_attempted для {card_id}: {e}")
        _self_heal_active_count = max(0, _self_heal_active_count - 1)
        return

    # Timeline: старт
    _bus_publish(session_key, {
        "kind": "self_heal",
        "phase": "start",
        "card_id": card_id,
        "project": name,
    })

    # TG-пинг: начинаем починку
    await _send_tg_ping(
        ctx, project,
        f"🔧 <b>Самолечение</b>: начинаю починку инцидента <code>{card_id}</code> "
        f"в <b>{name}</b>.\n<i>{card_text[:120]}</i>",
    )

    # Формируем промпт чинильщику
    excerpt_part = ""
    if card_desc:
        meta = _parse_incident_desc(card_desc)
        exc = meta.get("excerpt", "")
        if exc:
            excerpt_part = f"\n\nТрейс/детали:\n{exc[:1000]}"
    heal_prompt = (
        f"На проекте инцидент: {card_text}.{excerpt_part}\n\n"
        f"Найди причину и почини. Не трогай несвязанный код. "
        f"После правки тесты должны проходить."
    )

    # Создаём виртуальную карточку для _run_card
    heal_card: dict = {
        "id": card_id,
        "text": heal_prompt,
        "description": None,
    }

    # ПРЕДОХРАНИТЕЛЬ №5: проверяем git+clean (worktree-режим), уважая git_enabled
    run_mode = await _card_run_mode(cwd, git_enabled=_git_enabled(project))
    if run_mode != "worktree":
        print(f"[self_heal] {name}/{card_id}: не-git или dirty — пропускаем")
        _bus_publish(session_key, {
            "kind": "self_heal", "phase": "skipped",
            "reason": "not_git_or_dirty", "card_id": card_id, "project": name,
        })
        _self_heal_active_count = max(0, _self_heal_active_count - 1)
        return

    # Настраиваем worktree (C2-изоляция)
    wt_info = await _card_worktree_setup(cwd, card_id)
    if wt_info is None:
        print(f"[self_heal] {name}/{card_id}: worktree setup failed — пропускаем")
        _self_heal_active_count = max(0, _self_heal_active_count - 1)
        return

    # Захватываем running-слот СИНХРОННО (до первого await внутри _run_card)
    # Это имитирует что C2 занят — предотвращает двойные прогоны из TG
    if ctx["running"].get(session_key) is not None:
        print(f"[self_heal] {name}/{card_id}: проект занят — пропускаем")
        _bus_publish(session_key, {
            "kind": "self_heal", "phase": "skipped",
            "reason": "project_busy", "card_id": card_id, "project": name,
        })
        _self_heal_active_count = max(0, _self_heal_active_count - 1)
        return
    ctx["running"][session_key] = True

    # Перемещаем инцидент в in_progress под board-lock (как обычный C2-запуск)
    try:
        async with lock:
            _, preamble, cols = _load_board(cwd)
            moved = _pop_card(cols, card_id)
            if moved is None:
                moved = heal_card
            cols["in_progress"].append(moved)
            _save_board(cwd, name, preamble, cols)
    except Exception as e:
        print(f"[self_heal] ошибка при перемещении в in_progress: {e}")
        ctx["running"].pop(session_key, None)
        _self_heal_active_count = max(0, _self_heal_active_count - 1)
        return

    # Запускаем агента через существующий _run_card (ПЕРЕИСПОЛЬЗУЕМ, не дублируем SDK-цикл)
    # _run_card снимет running-слот в finally, перенесёт карточку в Review/Failed
    webapp_app_stub = None  # webapp_app используется только для ctx (не нужен здесь)
    try:
        await _run_card(
            ctx, webapp_app_stub, project, heal_card, session_key,
            run_mode="worktree", wt_info=wt_info,
        )
    except Exception as e:
        print(f"[self_heal] _run_card упал: {e}")
        # running-слот уже снят в _run_card.finally
        _self_heal_active_count = max(0, _self_heal_active_count - 1)
        return

    # Timeline: агент завершил прогон
    _bus_publish(session_key, {
        "kind": "self_heal",
        "phase": "fixed",
        "card_id": card_id,
        "project": name,
    })

    # Прогоняем quality gate в worktree
    wt_path = wt_info["wt_path"]
    project_secrets = _secrets_read(cwd)
    gate = await _run_quality_gate(wt_path, env=project_secrets)
    verdict = gate.get("verdict", "unknown")

    # Записываем вердикт в meta-сайдкар
    DATA: Path = ctx["DATA"]
    try:
        run_meta = _read_run_meta(DATA, card_id) or {}
        run_meta["gate"] = {"verdict": verdict, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_run_meta(DATA, card_id, run_meta)
    except Exception as e:
        print(f"[self_heal] ошибка записи gate в meta: {e}")

    # Обновляем описание карточки с пометкой авто-починки
    heal_badge = "🔧 авто-починка · гейт ✓" if verdict == "safe" else "🔧 авто-починка · гейт ✗"
    try:
        async with lock:
            _, preamble, cols = _load_board(cwd)
            # Карточка уже в review или failed (после _run_card)
            for col_cards in cols.values():
                for c in col_cards:
                    if c["id"] == card_id:
                        existing_meta = _parse_incident_desc(c.get("description"))
                        existing_meta["heal_attempted"] = "true"
                        base_desc = _format_incident_desc(existing_meta)
                        c["description"] = base_desc + f"\nheal_badge={heal_badge}"
                        break
            _save_board(cwd, name, preamble, cols)
    except Exception as e:
        print(f"[self_heal] ошибка при обновлении пометки на карточке: {e}")

    # Если risky — переносим в Failed
    if verdict == "risky":
        try:
            async with lock:
                _, preamble, cols = _load_board(cwd)
                card_obj = _pop_card(cols, card_id)
                if card_obj is not None:
                    cols["failed"].append(card_obj)
                    _save_board(cwd, name, preamble, cols)
        except Exception as e:
            print(f"[self_heal] ошибка при перемещении risky в failed: {e}")

    # Timeline: gate результат
    gate_phase = "gate_ok" if verdict == "safe" else ("gate_fail" if verdict == "risky" else "gate_unknown")
    _bus_publish(session_key, {
        "kind": "self_heal",
        "phase": gate_phase,
        "verdict": verdict,
        "card_id": card_id,
        "project": name,
    })

    # TG-пинг: результат
    if verdict == "safe":
        tg_msg = (
            f"✅ <b>Самолечение</b>: фикс готов для <b>{name}</b> · <code>{card_id}</code>.\n"
            f"Карточка в Review. Тесты прошли. Проверь и нажми «Применить»."
        )
    elif verdict == "risky":
        tg_msg = (
            f"⚠️ <b>Самолечение</b>: попытка починки <b>{name}</b> · <code>{card_id}</code> "
            f"не прошла гейт.\nКарточка в Failed. Посмотри diff вручную."
        )
    else:
        tg_msg = (
            f"🔧 <b>Самолечение</b>: прогон завершён для <b>{name}</b> · <code>{card_id}</code>.\n"
            f"Гейт: нет тестов (unknown). Карточка в Review. Проверь diff."
        )
    await _send_tg_ping(ctx, project, tg_msg)

    _self_heal_active_count = max(0, _self_heal_active_count - 1)
    print(f"[self_heal] {name}/{card_id}: done, gate={verdict}")


async def _error_scanner_loop(ctx: dict):
    """Фоновая задача: периодически сканирует все проекты с log_cmd.

    Самолечение (если включено) оценивает ВСЕ незалеченные инциденты из Failed
    на каждом скане — не только те, что добавлены в текущем скане (gate A).
    Safety-слой Ф2 (gates B–D) применяется ПОВЕРХ предохранителей spec-010.
    """
    global _self_heal_active_count
    # Первый прогон через 30с после старта (дать боту устаканиться)
    await asyncio.sleep(30)
    while True:
        try:
            projects = _collect_projects(ctx)
            for proj in projects:
                if proj.get("is_free"):
                    continue
                if not proj.get("log_cmd"):
                    continue
                res = await _scan_and_ingest(proj, ctx)
                if res.get("added") or res.get("updated"):
                    print(f"[scanner] {proj['name']}: +{res['added']} new, "
                          f"~{res['updated']} updated (из {res['scanned']} событий)")

                # Уведомление «упало»: новые инциденты + включён notify_on_error
                if res.get("added", 0) and proj.get("notify_on_error"):
                    await _notify_new_incidents(ctx, proj, res.get("added", 0))

                # ── ШАГ 3: Самолечение ─────────────────────────────────────────
                # ПРЕДОХРАНИТЕЛЬ №1 (spec-010): только если self_heal явно включён
                if not _self_heal_enabled(proj):
                    continue

                # Gate A: убрана проверка res.added > 0 — оцениваем ВСЕ инциденты
                # на каждом скане (молодые ранее были «слишком молоды», сейчас grown).

                session_key = proj["tg_thread"]
                _max_conc = int(_get_global_setting("self_heal_max_concurrent", _SELF_HEAL_MAX_CONCURRENT))
                now = time.time()

                # Загружаем доску один раз для всего перебора карточек
                try:
                    _, _, cols = _load_board(proj["cwd"])
                except Exception:
                    continue

                rate_limit_logged = False  # не спамим Timeline per-card, только раз на проект

                for card in cols.get("failed", []):
                    running_busy = ctx["running"].get(session_key) is not None
                    rate_ok = _heal_rate_ok(session_key, now)

                    action, reason = _heal_decision(
                        card, proj,
                        active_count=_self_heal_active_count,
                        max_conc=_max_conc,
                        running_busy=running_busy,
                        rate_ok=rate_ok,
                        now=now,
                    )

                    if action == "skip":
                        # Тихие пропуски (heal_attempted, not_incident) — не логируем
                        if reason.startswith("too_young"):
                            print(f"[self_heal] {proj['name']}/{card['id']}: дебаунс ({reason}), ждём")
                        continue

                    if action == "benign":
                        # C. Помечаем heal_skip=benign один раз под board-lock
                        print(f"[self_heal] {proj['name']}/{card['id']}: benign ({reason}), пометка heal_skip")
                        try:
                            lock = _get_board_lock(proj["cwd"])
                            async with lock:
                                _, preamble2, cols2 = _load_board(proj["cwd"])
                                for col_cards in cols2.values():
                                    for c in col_cards:
                                        if c["id"] == card["id"]:
                                            m2 = _parse_incident_desc(c.get("description"))
                                            m2["heal_skip"] = "benign"
                                            c["description"] = _format_incident_desc(m2)
                                            break
                                _save_board(proj["cwd"], proj["name"], preamble2, cols2)
                        except Exception as ex:
                            print(f"[self_heal] ошибка при пометке heal_skip: {ex}")
                        continue

                    if action == "stop":
                        # Ресурсный лимит — прерываем перебор карточек этого проекта
                        if reason == "rate_limit" and not rate_limit_logged:
                            rate_limit_logged = True
                            print(f"[self_heal] {proj['name']}: рейт-лимит ({_HEAL_MAX_PER_WINDOW}/"
                                  f"{_HEAL_WINDOW_SEC}s), пропускаем скан")
                            _bus_publish(session_key, {
                                "kind": "self_heal",
                                "phase": "skipped",
                                "reason": "rate_limit",
                                "project": proj["name"],
                            })
                        elif reason == "concurrency_limit":
                            print(f"[self_heal] лимит конкурентности ({_max_conc}) достигнут, "
                                  f"пропускаем {proj['name']}")
                        elif reason == "project_busy":
                            print(f"[self_heal] проект {proj['name']} занят, пропускаем")
                        break

                    # action == "heal"
                    _heal_record(session_key, now)
                    _self_heal_active_count += 1
                    _spawn_bg(_self_heal_card(ctx, proj, card))
                    print(f"[self_heal] запущена починка {proj['name']}/{card['id']}")

        except Exception as e:
            print(f"[scanner] loop error: {e}")
        await asyncio.sleep(int(_get_global_setting("scan_interval_sec", _SCAN_INTERVAL_SEC)))


def _board_payload(cwd: str) -> dict:
    tp, dp = _tasks_path(cwd), _done_path(cwd)
    _, _, cols = _load_board(cwd)
    columns = [{"key": k, "label": l, "cards": cols[k]} for k, l, _ in BOARD_COLUMNS]
    done_count = 0
    if dp.exists():
        done_count = sum(1 for ln in dp.read_text(encoding="utf-8", errors="replace").splitlines()
                         if _CARD_RE.match(ln))
    return {"columns": columns, "done_count": done_count, "exists": tp.exists()}


async def api_project_tasks(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd, name = project["cwd"], project["name"]
    tp = _tasks_path(cwd)
    # Под локом: добавляем ops-маркеры к карточкам без них (только если файл изменился).
    # Lock сериализует cockpit-операции; агент пишет напрямую — при конфликте пропускаем запись.
    async with _get_board_lock(cwd):
        raw, preamble, cols = _load_board(cwd)
        if tp.exists():
            canon = _serialize_tasks(preamble, cols, name)
            if canon != raw:
                # Перечитываем: если агент успел записать между _load_board и здесь — пропускаем.
                try:
                    current = tp.read_text(encoding="utf-8")
                except OSError:
                    current = ""
                if current == raw:
                    # Guard: не пишем если после парсинга карточек стало меньше.
                    # Это значит агент написал что-то что парсер не распознал —
                    # перезапись уничтожит данные. Лучше потерять маркер, чем карточку.
                    raw_card_count = _count_potential_cards(raw)
                    parsed_card_count = sum(len(v) for v in cols.values())
                    if parsed_card_count < raw_card_count:
                        print(
                            f"[api_project_tasks] WARNING: пропускаем запись {tp} — "
                            f"парсер нашёл {parsed_card_count} карточек из {raw_card_count} "
                            f"потенциальных (агент записал нераспознанный формат?)"
                        )
                    else:
                        tp.write_text(canon, encoding="utf-8")
    # F: добавляем очередь карточек в ответ
    payload = _board_payload(cwd)
    payload["queued"] = _queue_for(project["tg_thread"])
    return web.json_response(payload)


async def api_create_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    column = body.get("column", "backlog")
    description = body.get("description") or None
    if description is not None:
        description = str(description).strip() or None
    cwd, name = project["cwd"], project["name"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        if column not in cols:
            column = "backlog"
        new_card: dict = {"id": _new_card_id(), "text": text}
        if description:
            new_card["description"] = description
        cols[column].insert(0, new_card)
        _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload(cwd))


def _pop_card(cols: dict, card_id: str):
    for k in cols:
        for i, c in enumerate(cols[k]):
            if c["id"] == card_id:
                return cols[k].pop(i)
    return None


# ─────────────────────────── F1: авто-запуск карточки ───────────────────────────

async def _git_diff_card(cwd: str) -> tuple[str, str]:
    """Возвращает (diff_full, diff_stat) через asyncio subprocess. Пустые строки при ошибке."""
    async def _run(*args):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
        except Exception:
            return ""
    diff_full, diff_stat = await asyncio.gather(
        _run("diff"),
        _run("diff", "--stat"),
    )
    return diff_full, diff_stat


# ─────────────────────────── C2: worktree helpers ───────────────────────────

async def _card_run_mode(cwd: str, git_enabled: bool = True) -> str:
    """Определяет режим прогона карточки: 'worktree' или 'legacy'.
    worktree = git включён И git-репо И дерево чистое. Иначе — legacy (прогон в cwd).
    git_enabled=False (настройка проекта) → всегда legacy, git вообще не трогаем."""
    if not git_enabled:
        return "legacy"
    info = await _git_info(cwd)
    if info is None:
        return "legacy"
    # git status --porcelain: пустой вывод = чистое дерево
    status = await _git_cmd(cwd, "status", "--porcelain")
    if status is None or status.strip():
        return "legacy"
    return "worktree"


async def _card_worktree_setup(cwd: str, card_id: str) -> "dict | None":
    """Создаёт worktree <cwd>/.worktrees/card-<id> на ветке card-<id>.
    Если уже существует — сначала чистит. Возвращает {wt_path, base_branch} или None при ошибке."""
    try:
        base_branch = await _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if not base_branch:
            return None
        wt_path = str(Path(cwd) / ".worktrees" / f"card-{card_id}")
        # Чистим если существует (повторный прогон)
        if Path(wt_path).exists():
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "worktree", "remove", "--force", wt_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        # Удаляем ветку если осталась (404-safe)
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "branch", "-D", f"card-{card_id}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
        # Создаём новый worktree
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "worktree", "add", wt_path, "-b", f"card-{card_id}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        if proc.returncode != 0:
            print(f"[worktree_setup] git worktree add failed: {stderr.decode(errors='replace').strip()}")
            return None
        return {"wt_path": wt_path, "base_branch": base_branch}
    except Exception as e:
        print(f"[worktree_setup] ошибка: {e}")
        return None


async def _commit_in_worktree(wt_path: str, card_id: str, prompt: str) -> bool:
    """Авто-коммит в worktree. Возвращает True если был коммит (были изменения)."""
    try:
        # Проверяем наличие изменений
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if not stdout.decode().strip():
            return False  # нет изменений
        # git add -A
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "add", "-A",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
        # git commit
        short_prompt = (prompt[:60] + "…") if len(prompt) > 60 else prompt
        commit_msg = f"card {card_id}: {short_prompt}"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "commit", "-m", commit_msg,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15.0)
        return proc.returncode == 0
    except Exception as e:
        print(f"[commit_in_worktree] ошибка: {e}")
        return False


async def _diff_from_worktree(wt_path: str, base_branch: str) -> tuple[str, str]:
    """Возвращает (diff_full, diff_stat) из worktree vs base_branch."""
    async def _run(*args):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", wt_path, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
        except Exception:
            return ""
    diff_full, diff_stat = await asyncio.gather(
        _run("diff", f"{base_branch}...HEAD"),
        _run("diff", "--stat", f"{base_branch}...HEAD"),
    )
    return diff_full, diff_stat


def _write_run_meta(data_dir: Path, card_id: str, meta: dict) -> None:
    """Записывает машиночитаемый JSON-сайдкар DATA/runs/<card_id>.json с метаданными прогона."""
    try:
        runs_dir = data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        (runs_dir / f"{card_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[_write_run_meta] ошибка записи {card_id}.json: {e}")


def _read_run_meta(data_dir: Path, card_id: str) -> "dict | None":
    """Читает JSON-метаданные прогона. None если не существует или повреждён."""
    try:
        p = data_dir / "runs" / f"{card_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ─────────────────────────── AppCtx TypedDict ───────────────────────────
# Аннотация полей ctx, используемых в _run_card и хелперах.
# Рантайм — тот же dict, TypedDict только для проверки типов (mypy/pyright).

class AppCtx(TypedDict, total=False):
    topics: dict
    sessions: dict
    running: dict
    costs: dict
    rate_limits: dict
    DATA: Path
    HERE: Path
    DEFAULT_MODEL: str
    DEFAULT_CWD: str
    VAULT_PROJECTS: Path
    password: str
    _auth_token: str
    port: int
    GROUP_CHAT_ID: int
    save_sessions: object   # callable
    save_topics: object     # callable
    resolve_project: object  # callable
    run_engine: object      # async generator factory
    ptb_app: object
    MODELS: dict
    REGISTRY: dict


# ─────────────────────────── _run_card helpers ───────────────────────────

def _write_sidecar(
    data_dir: Path,
    card_id: str,
    name: str,
    prompt: str,
    answer_text: str,
    ok: bool,
    exc_info: str | None,
    diff_stat: str,
    diff_full: str,
    run_mode: str = "legacy",
    wt_branch: str | None = None,
    base_branch: str | None = None,
    wt_path: str | None = None,
    has_changes: bool = False,
) -> None:
    """Записывает сайдкар результата карточки в DATA/runs/<card_id>.md
    и машиночитаемый JSON в DATA/runs/<card_id>.json."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    outcome = "ok" if ok else "fail"
    try:
        runs_dir = data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        sidecar_lines = [
            f"# Результат карточки {card_id}",
            "",
            f"**Проект:** {name}",
            f"**Время:** {ts}",
            f"**Исход:** {outcome}",
            f"**Режим:** {run_mode}",
            "",
            "## Задача",
            "",
            prompt,
            "",
            "## Ответ агента",
            "",
            answer_text,
        ]
        if exc_info:
            sidecar_lines += ["", "## Ошибка", "", f"```\n{exc_info}\n```"]
        if diff_stat:
            sidecar_lines += ["", "## Git diff --stat", "", f"```\n{diff_stat}\n```"]
        if diff_full:
            sidecar_lines += ["", "## Git diff (полный)", "", f"```diff\n{diff_full}\n```"]
        (runs_dir / f"{card_id}.md").write_text("\n".join(sidecar_lines), encoding="utf-8")
        # Машиночитаемый JSON-сайдкар для apply/discard/фронта
        meta = {
            "card_id": card_id,
            "ts": ts,
            "outcome": outcome,
            "mode": run_mode,
            "branch": wt_branch,
            "base_branch": base_branch,
            "wt_path": wt_path,
            "has_changes": has_changes,
            "applied": False,
            "discarded": False,
        }
        (runs_dir / f"{card_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[_run_card] ошибка записи сайдкара {card_id}: {e}")


async def _move_card_after_run(
    ctx: AppCtx,
    cwd: str,
    name: str,
    card: dict,
    card_id: str,
    ok: bool,
) -> None:
    """Переносит карточку в Review (ok) или Failed (err) под board-lock."""
    try:
        target_col = "review" if ok else "failed"
        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            moved = _pop_card(cols, card_id)
            if moved is None:
                moved = card
            cols[target_col].append(moved)
            _save_board(cwd, name, preamble, cols)
    except Exception as e:
        print(f"[_run_card] ошибка переноса карточки {card_id}: {e}")


async def _notify_tg(ctx: AppCtx, session_key: str, prompt: str, ok: bool) -> None:
    """Отправляет пинг в TG-топик о завершении карточки. Некритичен — ошибки логируются."""
    try:
        ptb = ctx.get("ptb_app")
        if ptb is None:
            return
        parts = session_key.split(":", 1)
        chat_id = int(parts[0])
        thread_id = int(parts[1]) if len(parts) > 1 and parts[1] not in ("0", "") else None
        icon = "✅" if ok else "❌"
        short_text = (prompt[:60] + "…") if len(prompt) > 60 else prompt
        target_label = "Review" if ok else "Failed"
        await ptb.bot.send_message(
            chat_id,
            f"{icon} Карточка «{short_text}» → {target_label}",
            message_thread_id=thread_id,
        )
    except Exception as e:
        print(f"[_run_card] TG-пинг не удался: {e}")


async def _run_card(
    ctx: AppCtx,
    webapp_app,
    project: dict,
    card: dict,
    session_key: str,
    run_mode: str = "legacy",
    wt_info: "dict | None" = None,
) -> None:
    """Фоновая задача F1: оркестратор — выполняет карточку через run_engine, пишет сайдкар, переносит карточку.

    run_mode: 'worktree' | 'legacy'. wt_info: {wt_path, base_branch} или None.
    """
    run_engine = ctx.get("run_engine")
    cwd = project["cwd"]
    name = project["name"]
    model = project.get("model", ctx.get("DEFAULT_MODEL", "sonnet"))
    prompt = card["text"]
    # Если есть description — добавляем его к промпту для агента
    card_desc = card.get("description")
    if card_desc:
        prompt = f"{prompt}\n\n{card_desc}"
    card_id = card["id"]
    # Карточка доски: кокпит САМ перенесёт её в Review при успехе (_move_card_after_run).
    # Агенту сообщаем lifecycle, чтобы он завершил чисто и дал резюме человеку на ревью.
    # Доску (TASKS.md) агент вручную НЕ правит — иначе ломается канонизация/ops-маркеры.
    prompt = (
        f"{prompt}\n\n[Это карточка доски «{card_id}» проекта «{name}». Выполни задачу. "
        f"Когда закончишь — карточка автоматически уйдёт в Review человеку на проверку: "
        f"заверши работу и закончи КРАТКИМ резюме сделанного (оно попадёт в Review). "
        f"TASKS.md вручную не правь — перенос делает кокпит.]"
    )
    DATA: Path = ctx["DATA"]

    # В worktree-режиме агент работает в wt_path, иначе — в cwd
    effective_cwd = wt_info["wt_path"] if (run_mode == "worktree" and wt_info) else cwd

    answer_parts: list[str] = []
    exc_info: str | None = None
    ok = False
    has_changes = False

    try:
        try:
            if run_engine is None:
                raise RuntimeError("run_engine недоступен в ctx (старый запуск без F1)")

            # Публикуем старт прогона в шину (подписчики activity-stream увидят вживую)
            _bus_publish(session_key, {
                "kind": "run_start",
                "source": "card",
                "prompt": prompt,
                "run_id": card_id,
            })

            resume_sid = ctx["sessions"].get(session_key)
            # Секреты проекта — только из cwd основного проекта (не worktree), изоляция по cwd
            project_secrets = _secrets_read(cwd)
            async for event in run_engine(
                project_name=name,
                cwd=effective_cwd,
                prompt=prompt,
                session_key=session_key,
                model=model,
                resume_session_id=resume_sid,
                env=project_secrets,
            ):
                etype = event["type"]
                if etype == "text":
                    answer_parts.append(event["text"])
                    _bus_publish(session_key, {"kind": "text", "text": event["text"], "run_id": card_id})
                elif etype == "tool":
                    inp = event.get("input") or {}
                    tool_data = _format_tool(event.get("name", "?"), inp if isinstance(inp, dict) else {})
                    _bus_publish(session_key, {
                        "kind": "tool",
                        "run_id": card_id,
                        "tool": tool_data,
                    })
                elif etype == "result":
                    if event.get("session_id"):
                        ctx["sessions"][session_key] = event["session_id"]
                        ctx["save_sessions"]()
                        _inherit_label_from_free_chat(ctx, session_key, event["session_id"])
                elif etype == "error":
                    raise event["exc"]

            ok = True

        except Exception as e:
            exc_info = f"{type(e).__name__}: {e}\n\n{_tb.format_exc()}"

        # Worktree: авто-коммит + diff из ветки; legacy: diff из cwd
        if run_mode == "worktree" and wt_info:
            wt_path = wt_info["wt_path"]
            base_branch = wt_info["base_branch"]
            has_changes = await _commit_in_worktree(wt_path, card_id, prompt)
            if has_changes:
                diff_full, diff_stat = await _diff_from_worktree(wt_path, base_branch)
            else:
                diff_full, diff_stat = "", ""
            wt_branch = f"card-{card_id}"
            wt_path_val = wt_path
        else:
            # legacy: git diff из рабочего дерева
            diff_full, diff_stat = await _git_diff_card(cwd)
            has_changes = bool(diff_full or diff_stat)
            wt_path_val = None
            base_branch = None
            wt_branch = None

        # сайдкар DATA/runs/<card_id>.md + JSON meta
        answer_text = "\n".join(answer_parts).strip() or "(агент завершил без текстового ответа)"
        _write_sidecar(
            DATA, card_id, name, prompt, answer_text, ok, exc_info, diff_stat, diff_full,
            run_mode=run_mode,
            wt_branch=wt_branch,
            base_branch=base_branch,
            wt_path=wt_path_val,
            has_changes=has_changes,
        )

        # перенос карточки (перезагружаем доску — могла измениться пока агент работал)
        await _move_card_after_run(ctx, cwd, name, card, card_id, ok)

        # TG-пинг (некритичен)
        await _notify_tg(ctx, session_key, prompt, ok)

    finally:
        # Публикуем завершение прогона в шину (ПЕРЕД снятием замка)
        _bus_publish(session_key, {
            "kind": "run_end",
            "outcome": "ok" if ok else "fail",
            "run_id": card_id,
        })
        # замок снимается ГАРАНТИРОВАННО, даже если запись сайдкара/перенос упали
        ctx["running"].pop(session_key, None)

    # D: после снятия замка — дренируем очередь (следующая карточка, если есть)
    try:
        _aiohttp_app = ctx.get("_aiohttp_app")
        if _aiohttp_app is not None:
            await _drain_queue(ctx, _aiohttp_app, project)
    except Exception as _dq_exc:
        print(f"[_run_card] _drain_queue error: {_dq_exc}")


# ─────────────────────────── Card Queue: _start_card_run / _drain_queue ───────────────────────────


async def _start_card_run(ctx: AppCtx, app, project: dict, card_id: str) -> dict:
    """Reusable, race-safe: резервирует lock СИНХРОННО, переносит карточку в in_progress,
    запускает _run_card в фоне. Возвращает {"started": bool, ...}.

    Гарантия race-safety: проверка И установка ctx["running"][session_key] происходят
    без единого await между ними — это единственный guard против двойного старта.
    """
    session_key = project["tg_thread"]
    cwd = project["cwd"]
    name = project["name"]

    # run_engine отсутствует — деградация
    if ctx.get("run_engine") is None:
        return {"started": False, "reason": "no_engine"}

    # ── СИНХРОННАЯ проверка+резервация (НЕТ await между check и set) ──
    if ctx["running"].get(session_key) is not None:
        return {"started": False, "reason": "busy"}
    ctx["running"][session_key] = True
    # ── конец критической секции ──

    # Перенос карточки под board-lock
    card = None
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        card = _pop_card(cols, card_id)
        if card is None:
            ctx["running"].pop(session_key, None)
            return {"started": False, "reason": "not_found"}
        cols["in_progress"].append(card)
        _save_board(cwd, name, preamble, cols)

    # C2: режим + worktree
    run_mode = await _card_run_mode(cwd, git_enabled=_git_enabled(project))
    wt_info: dict | None = None
    if run_mode == "worktree":
        wt_info = await _card_worktree_setup(cwd, card_id)
        if wt_info is None:
            run_mode = "legacy"

    _spawn_bg(_run_card(ctx, app, project, card, session_key, run_mode=run_mode, wt_info=wt_info))
    return {"started": True, "card_id": card_id}


async def _drain_queue(ctx: AppCtx, app, project: dict) -> "str | None":
    """Пытается запустить следующую карточку из очереди.
    Если проект занят — возвращает None. Пропускает устаревшие/отсутствующие карточки.
    Возвращает card_id если запуск состоялся, иначе None.
    """
    session_key = project["tg_thread"]
    cwd = project["cwd"]

    # Быстрый non-await check: занят → ничего не делаем
    if ctx["running"].get(session_key) is not None:
        return None

    # Runnable columns (карточка должна быть в одной из них для запуска)
    _RUNNABLE = {"backlog", "review", "failed"}

    q = _queue_for(session_key)
    for card_id in q:
        # Загружаем доску
        try:
            _, _, cols = _load_board(cwd)
        except Exception:
            return None

        # Orphan-guard: если в in_progress кто-то висит (в т.ч. orphan после рестарта,
        # когда running-lock потерян но карточка осталась в колонке) — не стартуем вторую.
        if cols.get("in_progress"):
            return None

        # Проверяем, что карточка ещё существует в runnable-колонке
        found_runnable = any(
            c["id"] == card_id
            for col_key, col_cards in cols.items()
            if col_key in _RUNNABLE
            for c in col_cards
        )
        if not found_runnable:
            # Устаревшая или перемещённая запись — убираем из очереди, пробуем следующую
            _queue_remove(session_key, card_id)
            continue

        # Пытаемся запустить
        result = await _start_card_run(ctx, app, project, card_id)
        if result["started"]:
            _queue_remove(session_key, card_id)
            return card_id
        elif result.get("reason") == "busy":
            # Гонка — оставляем в очереди, попробуем позже
            return None
        else:
            # not_found или no_engine — убираем устаревшую и пробуем следующую
            # (stale-первая не должна блокировать валидную)
            _queue_remove(session_key, card_id)
            continue

    return None


async def api_move_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    to = body.get("to", "")
    cwd, name = project["cwd"], project["name"]

    # ── F1: авто-запуск при переносе в in_progress ──
    if to == "in_progress":
        session_key = project["tg_thread"]
        run_engine = ctx.get("run_engine")

        # Деградация: если движок недоступен (старый запуск) — работаем как обычный перенос
        if run_engine is None:
            print("[api_move_task] run_engine не в ctx — деградируем к ручному переносу")
            async with _get_board_lock(cwd):
                _, preamble, cols = _load_board(cwd)
                card = _pop_card(cols, card_id)
                if card is None:
                    return web.json_response({"error": "card not found"}, status=404)
                cols["in_progress"].append(card)
                _save_board(cwd, name, preamble, cols)
            return web.json_response(_board_payload(cwd))

        # Используем _start_card_run (race-safe: lock резервируется синхронно внутри)
        result = await _start_card_run(ctx, req.app, project, card_id)
        if result["started"]:
            return web.json_response(_board_payload(cwd))
        elif result.get("reason") == "busy":
            # Проект занят — ставим карточку в очередь вместо 409.
            # "enqueued":True сигнализирует постановку; board["queued"] — актуальный список
            # очереди (не затираем его флагом).
            _queue_enqueue(session_key, card_id)
            board = _board_payload(cwd)
            board["queued"] = _queue_for(session_key)
            return web.json_response({**board, "ok": True, "enqueued": True})
        else:
            # not_found или no_engine
            reason = result.get("reason", "unknown")
            if reason == "not_found":
                return web.json_response({"error": "card not found"}, status=404)
            return web.json_response({"error": reason}, status=400)

    # ── Обычный перенос (backlog / review / failed / done) ──
    session_key = project["tg_thread"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        card = _pop_card(cols, card_id)
        if card is None:
            return web.json_response({"error": "card not found"}, status=404)

        if to == "done":
            # Spec-012 Ф0 Task B: err-карточка перенесена в Done → записываем как dismissed
            if card_id.startswith("err-"):
                _dismissed_add(card_id[4:])
            dp = _done_path(cwd)
            header = dp.read_text(encoding="utf-8") if dp.exists() else f"# Done — {name}\n"
            if not header.strip():
                header = f"# Done — {name}\n"
            stamp = time.strftime("%Y-%m-%d")
            new = header.rstrip() + f"\n- [x] {card['text']} · {stamp}\n"
            dp.write_text(new, encoding="utf-8")
            _save_board(cwd, name, preamble, cols)
        elif to in cols:
            cols[to].append(card)
            _save_board(cwd, name, preamble, cols)
        else:
            cols["backlog"].append(card)
            _save_board(cwd, name, preamble, cols)
            return web.json_response({"error": "unknown column"}, status=400)
    # F: карточка вручную перемещена из очереди — убираем из очереди
    _queue_remove(session_key, card_id)
    return web.json_response(_board_payload(cwd))


async def api_delete_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    cwd, name = project["cwd"], project["name"]
    session_key = project["tg_thread"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        if _pop_card(cols, card_id) is None:
            return web.json_response({"error": "card not found"}, status=404)
        # Spec-012 Ф0 Task B: err-карточка удалена → записываем как dismissed
        if card_id.startswith("err-"):
            _dismissed_add(card_id[4:])
        _save_board(cwd, name, preamble, cols)
    # F: карточка удалена — убираем из очереди
    _queue_remove(session_key, card_id)
    return web.json_response(_board_payload(cwd))


async def api_run_batch(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/cards/run-batch — ставит несколько карточек в очередь.
    Тело: {"card_ids": ["id1", "id2", ...]}.
    Ответ: {"ok": True, "queued": <N поставлено>, "started": <card_id или null>}.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    raw_ids = body.get("card_ids")
    if not isinstance(raw_ids, list):
        return web.json_response({"error": "card_ids must be a list"}, status=400)

    session_key = project["tg_thread"]
    cwd = project["cwd"]

    # Runnable columns — карточка должна быть в одной из них
    _RUNNABLE = {"backlog", "review", "failed"}

    # Загружаем доску один раз
    try:
        _, _, cols = _load_board(cwd)
    except Exception:
        cols = {key: [] for key, _, _ in BOARD_COLUMNS}

    # Собираем set всех runnable card_id
    runnable_ids: set = set()
    for col_key, col_cards in cols.items():
        if col_key in _RUNNABLE:
            for c in col_cards:
                runnable_ids.add(c["id"])

    enqueued = 0
    for raw_id in raw_ids:
        if not isinstance(raw_id, str):
            continue
        if not _valid_card_id(raw_id):
            continue
        if raw_id not in runnable_ids:
            continue
        # _queue_enqueue → True только если реально добавил (дедуп → False) — не переоцениваем
        if _queue_enqueue(session_key, raw_id):
            enqueued += 1

    # Сразу дренируем — первая карточка стартует если проект свободен
    _aiohttp_app = ctx.get("_aiohttp_app") or req.app
    started_id = await _drain_queue(ctx, _aiohttp_app, project)

    return web.json_response({"ok": True, "queued": enqueued, "started": started_id})


async def _queue_drain_loop(ctx: dict) -> None:
    """E: Backstop-цикл: каждые _QUEUE_DRAIN_INTERVAL_SEC проверяет все проекты с очередью
    и дренирует их. Обрабатывает: рестарт (очередь пережила его), TG-пересечение
    (TG-прогон освободил проект — drain не вызван через _run_card).
    """
    await asyncio.sleep(10)  # дать боту устаканиться
    while True:
        try:
            _aiohttp_app = ctx.get("_aiohttp_app")
            if _aiohttp_app is not None:
                projects = _collect_projects(ctx)
                for proj in projects:
                    # per-project try/except: сбой в одном проекте не валит дренаж остальных
                    try:
                        if proj.get("is_free"):
                            continue
                        session_key = proj["tg_thread"]
                        if not _queue_for(session_key):
                            continue
                        await _drain_queue(ctx, _aiohttp_app, proj)
                    except Exception as pe:
                        print(f"[queue_drain_loop] project {proj.get('name')} error: {pe}")
        except Exception as e:
            print(f"[queue_drain_loop] error: {e}")
        await asyncio.sleep(_QUEUE_DRAIN_INTERVAL_SEC)


async def api_update_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    # description: если передан ключ — обновляем (None = удалить, строка = установить)
    update_description = "description" in body
    description = body.get("description")
    if description is not None:
        description = str(description).strip() or None
    cwd, name = project["cwd"], project["name"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        found = False
        for col_cards in cols.values():
            for card in col_cards:
                if card["id"] == card_id:
                    card["text"] = text
                    if update_description:
                        if description:
                            card["description"] = description
                        else:
                            card.pop("description", None)
                    found = True
                    break
            if found:
                break
        if not found:
            return web.json_response({"error": "card not found"}, status=404)
        _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload(cwd))


async def api_tasks_done(req: web.Request) -> web.Response:
    """Содержимое архива DONE.md — грузится только по запросу (сессии его не читают)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    dp = _done_path(project["cwd"])
    content = dp.read_text(encoding="utf-8", errors="replace") if dp.exists() else ""
    return web.json_response({"content": content, "exists": dp.exists()})


# ─────────────────────────── activity-stream SSE ───────────────────────────
#
# GET /api/projects/{id}/activity-stream  — поток событий проекта (session-specific)
# GET /api/activity-stream                — глобальный поток всех сессий
# Клиент держит соединение; при разрыве finally гарантирует отписку.

async def _sse_stream(req: web.Request, q: "asyncio.Queue[dict]", unsubscribe) -> web.StreamResponse:
    """Общий SSE-цикл: читает из очереди q, пишет в StreamResponse.
    unsubscribe — callable(q) для ГАРАНТИРОВАННОЙ отписки в finally."""
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=25.0)
                payload = json.dumps(event, ensure_ascii=False)
                await resp.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                # Heartbeat — держим соединение живым через туннель (Cloudflare / nginx).
                # Клиент мог отвалиться — тогда write упадёт ConnectionResetError; это норма,
                # а НЕ инцидент (раньше heartbeat-write был вне защиты → утекал в error_middleware).
                try:
                    await resp.write(b": ping\n\n")
                except (ConnectionResetError, ConnectionAbortedError):
                    break
            except (ConnectionResetError, ConnectionAbortedError):
                break
            except asyncio.CancelledError:
                break
            except Exception:
                break
    finally:
        unsubscribe(q)
    return resp


async def api_project_activity_stream(req: web.Request) -> web.StreamResponse:
    """GET /api/projects/{id}/activity-stream — поток событий шины для конкретного проекта."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = project["tg_thread"]
    q = _bus_subscribe(session_key)
    return await _sse_stream(req, q, lambda q: _bus_unsubscribe(session_key, q))


async def api_activity_stream_all(req: web.Request) -> web.StreamResponse:
    """GET /api/activity-stream — единый поток ВСЕХ событий шины (unread-индикаторы в сайдбаре)."""
    q = _bus_subscribe_global()
    return await _sse_stream(req, q, _bus_unsubscribe_global)


# ─────────────────────────── timeline read endpoint ───────────────────────────
#
# GET /api/projects/{id}/timeline?limit=N&before=<ts>
# Читает DATA/timeline/<slug>.jsonl (+ .jsonl.1 если нужна история).
# Отдаёт массив событий в хронологическом порядке (новые внизу).
# Пагинация: before=<ts> — только события со ts < before.
# Битые строки JSONL → skip (graceful).

_TIMELINE_DEFAULT_LIMIT = 200
_TIMELINE_MAX_LIMIT = 500


def _timeline_read_events(session_key: str, limit: int, before: float | None) -> list[dict]:
    """Читает события из JSONL (текущий файл + .1 для старой истории).
    Возвращает список событий в хронологическом порядке, ≤ limit штук,
    при before — только со ts < before."""
    path = _timeline_path(session_key)
    if path is None or not isinstance(path, Path):
        return []

    # Собираем строки из обоих файлов: сначала .1 (старые), потом текущий
    files: list[Path] = []
    backup = path.with_suffix(".jsonl.1")
    if backup.exists():
        files.append(backup)
    if path.exists():
        files.append(path)

    events: list[dict] = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue  # graceful: битая строка → skip
                    if not isinstance(obj, dict):
                        continue
                    ts = obj.get("ts")
                    if before is not None and isinstance(ts, (int, float)) and ts >= before:
                        continue
                    events.append(obj)
        except Exception:
            continue

    # Сортируем хронологически по ts (новые внизу)
    events.sort(key=lambda e: e.get("ts", 0))
    # Берём последние limit
    return events[-limit:]


async def api_project_timeline(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/timeline?limit=N&before=<ts> — история событий проекта."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    try:
        limit = int(req.rel_url.query.get("limit", _TIMELINE_DEFAULT_LIMIT))
        limit = max(1, min(limit, _TIMELINE_MAX_LIMIT))
    except (ValueError, TypeError):
        limit = _TIMELINE_DEFAULT_LIMIT

    before: float | None = None
    before_str = req.rel_url.query.get("before")
    if before_str:
        try:
            before = float(before_str)
        except (ValueError, TypeError):
            pass

    session_key = project["tg_thread"]
    events = _timeline_read_events(str(session_key), limit, before)
    return web.json_response({"events": events})


# ─────────────────────────── свободные чаты (без привязки к проекту) ───────────────────────────
#
# Free-чат — виртуальный «проект» с cwd=$HOME, без git, без TG-привязки.
# Каждый клик «новый свободный» создаёт отдельную вкладку со своим session_id.
# Хранятся в data/free_chats.json: {free-<uuid>: {label, cwd, model, created_at}}.

_FREE_DEFAULT_CWD = str(Path.home())


async def api_free_create(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        body = {}
    cwd = (body.get("cwd") or _FREE_DEFAULT_CWD).rstrip("/")
    model = (body.get("model") or _effective_default_model(ctx)).strip().lower()
    if model not in _ALLOWED_MODELS:
        model = _effective_default_model(ctx)

    # Лейбл — пользовательский или авто «Свободный HH:MM»
    label = (body.get("label") or "").strip()
    if not label:
        label = f"Свободный {time.strftime('%H:%M')}"

    fid = f"free-{_uuid.uuid4().hex[:8]}"
    free = _load_free_chats(ctx)
    free[fid] = {
        "label": label,
        "cwd": cwd,
        "model": model,
        "created_at": time.time(),
    }
    _save_free_chats(ctx, free)
    return web.json_response({"id": fid, **free[fid]})


async def api_free_rename(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    fid = req.match_info["id"]
    free = _load_free_chats(ctx)
    if fid not in free:
        return web.json_response({"error": "free chat not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    label = (body.get("label") or "").strip()
    if not label:
        return web.json_response({"error": "label is empty"}, status=400)
    if len(label) > 100:
        label = label[:100]
    free[fid]["label"] = label
    _save_free_chats(ctx, free)

    # Если у вкладки уже есть активная Claude-сессия — прокидываем тот же label на неё,
    # чтобы переименование вкладки автоматически переименовало и сессию в SessionSelector.
    active_sid = ctx["sessions"].get(fid)
    if active_sid:
        labels = _load_session_labels(ctx)
        labels[active_sid] = label
        _save_session_labels(ctx, labels)

    return web.json_response({"ok": True, "id": fid, "label": label})


async def api_free_delete(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    fid = req.match_info["id"]
    free = _load_free_chats(ctx)
    if fid not in free:
        return web.json_response({"error": "free chat not found"}, status=404)

    # Нельзя удалить если в нём идёт работа — клиент должен сначала остановить
    if ctx["running"].get(fid) is not None:
        return web.json_response({"error": "chat is busy, stop it first"}, status=409)

    free.pop(fid)
    _save_free_chats(ctx, free)
    # Подчищаем session_id если был
    if ctx["sessions"].pop(fid, None) is not None:
        save = ctx.get("save_sessions")
        if callable(save):
            save()
    return web.json_response({"ok": True})


# ─────────────────────────── лимиты подписки Claude Code ───────────────────────────
#
# GET /api/usage  → текущий снимок лимитов подписки (5ч окно, недельные, opus/sonnet, overage).
# Источник истины для ПРОЦЕНТОВ — официальный oauth-эндпоинт https://api.anthropic.com/api/oauth/usage
# (тот же, что бьёт `/usage` в самом Claude Code). Пассивный RateLimitEvent SDK даёт только
# status+resets_at, БЕЗ utilization на этой подписке (проверено 2026-05-30) — потому % раньше не было.
# Токен берём из ~/.claude/.credentials.json (SDK его сам рефрешит). Кэш 60с — фронт поллит каждые 30с.

_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
_usage_cache: dict = {"data": None, "ts": 0.0}
_usage_lock: asyncio.Lock | None = None  # lazy — created inside the running event loop
_USAGE_TTL = 60.0


def _get_usage_lock() -> asyncio.Lock:
    """Returns the module-level usage lock, creating it lazily inside the running loop."""
    global _usage_lock
    if _usage_lock is None:
        _usage_lock = asyncio.Lock()
    return _usage_lock


def _read_oauth_token() -> str | None:
    try:
        with open(_CREDS_PATH) as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def _iso_to_unix(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def _norm_window(d):
    """oauth-окно {utilization:0-100, resets_at:ISO} → формат фронта {utilization:0-1, resets_at:unix}."""
    if not isinstance(d, dict):
        return None
    util = d.get("utilization")
    return {
        "status": "allowed",
        "resets_at": _iso_to_unix(d.get("resets_at")),
        "utilization": (util / 100.0) if isinstance(util, (int, float)) else None,
        "ts": time.time(),
    }


async def _fetch_oauth_usage():
    token = _read_oauth_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(_OAUTH_USAGE_URL, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        return None


async def api_usage(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    now = time.time()
    async with _get_usage_lock():
        cached = _usage_cache["data"]
        if cached is None or (now - _usage_cache["ts"]) > _USAGE_TTL:
            raw = await _fetch_oauth_usage()
            if raw is not None:
                limits = {}
                for k in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
                    nv = _norm_window(raw.get(k))
                    if nv:
                        limits[k] = nv
                eu = raw.get("extra_usage")
                if isinstance(eu, dict) and eu.get("is_enabled") and eu.get("utilization") is not None:
                    limits["overage"] = {
                        "status": "allowed",
                        "resets_at": None,
                        "utilization": eu["utilization"] / 100.0,
                        "ts": now,
                    }
                _usage_cache["data"] = limits
                _usage_cache["ts"] = now
                cached = limits
        # oauth недоступен (нет токена / 401 / сеть) → фоллбэк на пассивный снимок SDK
        if not cached:
            cached = ctx.get("rate_limits") or {}
    return web.json_response({"limits": cached, "now": time.time()})


# ─────────────────────────── смена модели проекта ───────────────────────────
#
# POST /api/projects/{id}/model  {model: "opus"|"sonnet"|"haiku"}
# Обновляет model во ВСЕХ topics с тем же cwd (один проект может иметь несколько TG-топиков),
# persist через save_topics() из ctx. Применится со следующего запроса (текущая сессия не трогается).

_ALLOWED_MODELS: set[str] = {"opus", "sonnet", "haiku"}


async def api_project_set_model(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    model = (body.get("model") or "").strip().lower()
    if model not in _ALLOWED_MODELS:
        return web.json_response(
            {"error": f"model must be one of: {', '.join(sorted(_ALLOWED_MODELS))}"},
            status=400,
        )

    # Free-чат: модель хранится в free_chats.json по его id
    if project.get("is_free"):
        free = _load_free_chats(ctx)
        if project["id"] in free:
            free[project["id"]]["model"] = model
            _save_free_chats(ctx, free)
        return web.json_response({"ok": True, "model": model, "topics_updated": 1})

    # Обычный проект — обновляем все topics с тем же cwd
    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            b["model"] = model
            changed += 1

    if changed:
        save_topics = ctx.get("save_topics")
        if callable(save_topics):
            save_topics()

    return web.json_response({"ok": True, "model": model, "topics_updated": changed})


# ─────────────────────────── git sync (commit + push) ───────────────────────────
#
# POST /api/projects/{id}/git/sync  {message?: str}
# Если есть локальные правки → git add -A + git commit -m <msg>. Затем git push.
# Дефолтное сообщение: "wip: YYYY-MM-DD HH:MM" (если поле message пустое).
# Возвращает {ok, committed, pushed, log}; на ошибке status 500 + {error, log}.

async def api_project_upload(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/upload — multipart файл → data/inbox/ → {path, name, size}."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    DATA: Path = ctx["DATA"]
    inbox = DATA / "inbox"
    inbox.mkdir(exist_ok=True)

    try:
        reader = await req.multipart()
    except Exception:
        return web.json_response({"error": "ожидается multipart/form-data"}, status=400)

    field = await reader.next()
    if field is None:
        return web.json_response({"error": "нет поля file"}, status=400)

    filename = field.filename or "upload"
    safe_name = re.sub(r'[^\w.\-]', '_', filename)
    ts = int(time.time() * 1000)
    dest = inbox / f"web_{ts}_{safe_name}"

    MAX_UPLOAD = 20 * 1024 * 1024
    size = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = await field.read_chunk(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    return web.json_response({"error": "файл слишком большой (макс 20 МБ)"}, status=413)
                fh.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"path": str(dest), "name": filename, "size": size})


async def api_project_git_sync(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if not _git_enabled(project):
        return web.json_response({"error": "git отключён для этого проекта (настройки)"}, status=409)
    cwd = project["cwd"]

    try:
        body = await req.json()
    except Exception:
        body = {}
    msg = (body.get("message") or "").strip() or f"wip: {time.strftime('%Y-%m-%d %H:%M')}"

    async def _git(*args) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace")

    log_parts: list[str] = []
    committed = False
    pushed = False

    # 1. Проверяем статус
    rc, status = await _git("status", "--porcelain")
    if rc != 0:
        return web.json_response({"error": "git status failed", "log": status}, status=500)

    # 2. Если есть dirty — стейджим и коммитим
    if status.strip():
        rc, out = await _git("add", "-A")
        log_parts.append(f"$ git add -A\n{out}".rstrip())
        if rc != 0:
            return web.json_response({"error": "git add failed", "log": "\n\n".join(log_parts)}, status=500)

        rc, out = await _git("commit", "-m", msg)
        log_parts.append(f"$ git commit -m {msg!r}\n{out}".rstrip())
        if rc != 0:
            return web.json_response({"error": "git commit failed", "log": "\n\n".join(log_parts)}, status=500)
        committed = True

    # 3. Push (даже если коммита не было — могли быть локальные коммиты не отправлены)
    rc, out = await _git("push")
    log_parts.append(f"$ git push\n{out}".rstrip())
    if rc != 0:
        return web.json_response({"error": "git push failed", "log": "\n\n".join(log_parts)}, status=500)
    pushed = True

    return web.json_response({
        "ok": True,
        "committed": committed,
        "pushed": pushed,
        "message": msg if committed else None,
        "log": "\n\n".join(log_parts),
    })


# ─────────────────────────── запуск тестов проекта ───────────────────────────
#
# POST /api/projects/{id}/test → автодетект тест-команды, прогон, вывод в кокпит.
# Детект по убыванию специфичности: pytest-конфиг/tests/ → npm test → make test.

def _detect_test_cmd(cwd: str):
    """Возвращает (cmd:list[str], human:str) или None если не нашли как тестировать."""
    p = Path(cwd)
    # Python / pytest
    has_pytest_cfg = any((p / n).exists() for n in
                         ("pytest.ini", "tox.ini", "setup.cfg")) \
        or (p / "tests").is_dir() or (p / "test").is_dir()
    if (p / "pyproject.toml").exists():
        try:
            if "pytest" in (p / "pyproject.toml").read_text(errors="replace"):
                has_pytest_cfg = True
        except Exception:
            pass
    if has_pytest_cfg:
        if (p / "venv" / "bin" / "pytest").exists():
            return (["venv/bin/pytest", "-q"], "venv/bin/pytest -q")
        if (p / "venv" / "bin" / "python").exists():
            return (["venv/bin/python", "-m", "pytest", "-q"], "venv/bin/python -m pytest -q")
        return (["python3", "-m", "pytest", "-q"], "python3 -m pytest -q")
    # Node
    pkg = p / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(errors="replace"))
            if (data.get("scripts") or {}).get("test"):
                return (["npm", "test", "--silent"], "npm test")
        except Exception:
            pass
    # Make
    mk = p / "Makefile"
    if mk.exists():
        try:
            if re.search(r"^test:", mk.read_text(errors="replace"), re.M):
                return (["make", "test"], "make test")
        except Exception:
            pass
    return None


async def api_project_test(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd = project["cwd"]
    detected = _detect_test_cmd(cwd)
    if detected is None:
        return web.json_response({
            "detected": False, "ok": False, "cmd": None, "exit_code": None,
            "output": "Не нашёл как запускать тесты: нет pytest-конфига/tests/, "
                      "скрипта test в package.json или цели test в Makefile.",
        })
    cmd, human = detected
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
    except Exception as e:
        return web.json_response({"error": f"запуск не удался: {e}", "cmd": human}, status=500)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        rc = proc.returncode or 0
        timed_out = False
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        out, rc, timed_out = b"", -1, True
    text = out.decode(errors="replace")
    if len(text) > 20000:
        text = "…(начало обрезано)\n" + text[-20000:]
    if timed_out:
        text = (text + "\n⏱ прервано по таймауту 300с").strip()
    return web.json_response({
        "detected": True, "ok": (rc == 0 and not timed_out),
        "cmd": human, "exit_code": rc, "timed_out": timed_out, "output": text,
    })


# ─────────────────────────── quality gate ───────────────────────────────────
#
# _run_quality_gate(wt_path, env) — прогоняет тесты В worktree-карточки.
# Переиспользует _detect_test_cmd. Таймаут 300с. Вердикт: safe/risky/unknown.

_GATE_MAX_OUTPUT = 20_000  # символов


async def _run_quality_gate(wt_path: str, env: "dict | None" = None) -> dict:
    """Прогоняет тесты в worktree-карточки. Возвращает:
    {verdict:"safe|risky|unknown", tests:{detected, ok, cmd, exit_code, output, timed_out}}.
    Вердикт: тесты прошли→safe, упали/таймаут→risky, не найдены→unknown.
    """
    detected = _detect_test_cmd(wt_path)
    if detected is None:
        return {
            "verdict": "unknown",
            "tests": {
                "detected": False,
                "ok": False,
                "cmd": None,
                "exit_code": None,
                "output": "Тест-команда не обнаружена (нет pytest-конфига/tests/, npm test, make test).",
                "timed_out": False,
            },
            "lint": None,
        }

    cmd, human = detected
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=wt_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=run_env,
        )
    except Exception as e:
        return {
            "verdict": "risky",
            "tests": {
                "detected": True,
                "ok": False,
                "cmd": human,
                "exit_code": -1,
                "output": f"Не удалось запустить тесты: {e}",
                "timed_out": False,
            },
            "lint": None,
        }

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        rc = proc.returncode or 0
        timed_out = False
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        out, rc, timed_out = b"", -1, True

    text = out.decode(errors="replace")
    if len(text) > _GATE_MAX_OUTPUT:
        text = "…(начало обрезано)\n" + text[-_GATE_MAX_OUTPUT:]
    if timed_out:
        text = (text + "\n⏱ прервано по таймауту 300с").strip()

    ok = (rc == 0 and not timed_out)
    verdict = "safe" if ok else "risky"

    return {
        "verdict": verdict,
        "tests": {
            "detected": True,
            "ok": ok,
            "cmd": human,
            "exit_code": rc,
            "output": text,
            "timed_out": timed_out,
        },
        "lint": None,  # линт — out of scope (spec-009, п.2 дизайн-решений)
    }


async def api_card_check(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/tasks/{card}/check — прогнать quality gate в worktree карточки.
    Возвращает вердикт safe/risky/unknown. Записывает gate:{verdict,ts} в meta-сайдкар.
    Legacy или нет worktree → {verdict:"unknown", reason:"legacy"}.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)

    DATA: Path = ctx["DATA"]
    meta = _read_run_meta(DATA, card_id)

    # Legacy или нет worktree-мета → unknown без прогона
    if not meta or meta.get("mode") != "worktree" or not meta.get("wt_path"):
        return web.json_response({
            "verdict": "unknown",
            "reason": "legacy",
            "tests": None,
            "lint": None,
        })

    wt_path = meta["wt_path"]
    if not Path(wt_path).exists():
        return web.json_response({"error": "worktree не найден на диске"}, status=404)

    # Подмешать секреты проекта (тесты могут требовать ключи)
    cwd = project["cwd"]
    project_secrets = _secrets_read(cwd)

    result = await _run_quality_gate(wt_path, env=project_secrets or None)

    # Записать результат гейта в meta-сайдкар
    gate_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["gate"] = {"verdict": result["verdict"], "ts": gate_ts}
    _write_run_meta(DATA, card_id, meta)

    # Публикуем событие в Timeline (наблюдаемость)
    # session_key для события: берём из topics по cwd проекта (совпадает с apply/discard)
    try:
        topics: dict = ctx.get("topics", {})
        session_key: str = next(
            (k for k, v in topics.items() if isinstance(v, dict) and v.get("cwd") == cwd),
            f"0:{project['id']}",
        )
        _bus_publish(session_key, {
            "kind": "gate",
            "verdict": result["verdict"],
            "run_id": card_id,
        })
    except Exception:
        pass  # bus-событие не должно ломать ответ

    return web.json_response(result)


# ─────────────────────────── файловый проводник ───────────────────────────

# Директории и имена файлов, скрытые из листинга
_FS_EXCLUDE_DIRS: set[str] = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", ".worktrees", ".mypy_cache", ".pytest_cache",
}

# Файлы/паттерны, скрытые из листинга и чтения.
# Правило: имя начинается с ".env" — НО ".env.example" разрешён.
def _is_secret_name(name: str) -> bool:
    """True если имя файла считается секретным (не должно отображаться/читаться)."""
    if name.startswith(".env") and name != ".env.example":
        return True
    return False


def _read_file_content(target: Path, root: Path, rel: str) -> web.Response:
    """Общий хелпер для чтения файла: size/binary/text проверки + ответ JSON.
    root используется для нормализации rel_norm в ответе.
    Не проверяет секретность и traversal — это обязанность вызывающего."""
    if not target.exists() or not target.is_file():
        return web.json_response({"error": "not a file"}, status=404)

    try:
        size = target.stat().st_size
    except Exception:
        return web.json_response({"error": "stat failed"}, status=500)

    _MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 МБ
    if size > _MAX_FILE_SIZE:
        return web.json_response({"error": "файл слишком большой", "size": size})

    try:
        with open(target, "rb") as f:
            head = f.read(8192)
        if b"\x00" in head:
            return web.json_response({"error": "бинарный файл", "size": size})
    except Exception:
        return web.json_response({"error": "read failed"}, status=500)

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return web.json_response({"error": f"read error: {e}"}, status=500)

    lang = target.suffix.lstrip(".") if target.suffix else ""
    try:
        rel_norm = str(target.relative_to(root))
    except ValueError:
        rel_norm = rel

    return web.json_response({"path": rel_norm, "content": content, "lang": lang, "size": size})


def _resolve_safe(cwd: str, rel: str):
    """Возвращает (resolved_path, cwd_resolved) или поднимает ValueError при traversal."""
    cwd_resolved = Path(cwd).resolve()
    # Убираем ведущий / если есть — rel должен быть относительным
    rel_clean = rel.lstrip("/")
    target = (cwd_resolved / rel_clean).resolve()
    if not str(target).startswith(str(cwd_resolved) + "/") and target != cwd_resolved:
        raise ValueError("path traversal detected")
    return target, cwd_resolved


async def api_project_files(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/files?path=<rel> — листинг директории."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    rel = req.rel_url.query.get("path", "")

    try:
        target, cwd_resolved = _resolve_safe(project["cwd"], rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    if not target.exists() or not target.is_dir():
        return web.json_response({"error": "not a directory"}, status=404)

    # Нормализуем rel для ответа (относительно cwd)
    try:
        rel_norm = str(target.relative_to(cwd_resolved))
        if rel_norm == ".":
            rel_norm = ""
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Не пускаем навигацию ВНУТРЬ исключённых директорий (.git/venv/node_modules…) напрямую
    if any(part in _FS_EXCLUDE_DIRS for part in target.relative_to(cwd_resolved).parts):
        return web.json_response({"error": "directory hidden"}, status=404)

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for item in items:
            name = item.name
            # Исключаем скрытые директории и секреты
            if item.is_dir() and name in _FS_EXCLUDE_DIRS:
                continue
            if item.is_file() and _is_secret_name(name):
                continue
            # Также скрываем секреты в папках
            if item.is_dir() and _is_secret_name(name):
                continue
            if item.is_symlink():
                # Разрешаем симлинк и проверяем, не выходит ли он за пределы cwd
                try:
                    linked = item.resolve()
                    if not str(linked).startswith(str(cwd_resolved)):
                        continue  # симлинк наружу — скрываем
                except Exception:
                    continue
            entry_type = "dir" if item.is_dir() else "file"
            size = 0
            if item.is_file():
                try:
                    size = item.stat().st_size
                except Exception:
                    size = 0
            entries.append({"name": name, "type": entry_type, "size": size})
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)

    return web.json_response({"path": rel_norm, "entries": entries})


async def api_project_file(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/file?path=<rel> — содержимое файла."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    rel = req.rel_url.query.get("path", "")
    if not rel:
        return web.json_response({"error": "path required"}, status=400)

    try:
        target, cwd_resolved = _resolve_safe(project["cwd"], rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Проверяем имя на секреты (сохранён anти-traversal _resolve_safe)
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)

    # Запрещаем читать внутри исключённых директорий (.git/venv/node_modules…)
    try:
        rel_parts = target.relative_to(cwd_resolved).parts
        if any(part in _FS_EXCLUDE_DIRS for part in rel_parts):
            return web.json_response({"error": "access denied"}, status=403)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    return _read_file_content(target, cwd_resolved, rel)


# ── Глобальный файловый браузер (от $HOME) ────────────────────────────────────
# Не привязан к проекту — листинг/чтение от /home/igor/ с теми же правилами безопасности.

_GLOBAL_FS_EXCLUDE: set[str] = {
    "node_modules", "venv", ".venv", "__pycache__",
    "dist", ".worktrees", ".mypy_cache", ".pytest_cache",
}


def _resolve_global_safe(home: Path, rel: str):
    """Как _resolve_safe, но root = $HOME. Поднимает ValueError при traversal."""
    rel_clean = rel.lstrip("/")
    target = (home / rel_clean).resolve()
    if not str(target).startswith(str(home) + "/") and target != home:
        raise ValueError("path traversal detected")
    return target


async def api_global_files(req: web.Request) -> web.Response:
    """GET /api/global/files?path=<rel> — листинг от $HOME."""
    home = Path.home()
    rel = req.rel_url.query.get("path", "")
    try:
        target = _resolve_global_safe(home, rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    if not target.exists() or not target.is_dir():
        return web.json_response({"error": "not a directory"}, status=404)

    try:
        rel_norm = str(target.relative_to(home))
        if rel_norm == ".":
            rel_norm = ""
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for item in items:
            name = item.name
            if item.is_dir() and name in _GLOBAL_FS_EXCLUDE:
                continue
            if item.is_file() and _is_secret_name(name):
                continue
            entry_type = "dir" if item.is_dir() else "file"
            size = 0
            if item.is_file():
                try:
                    size = item.stat().st_size
                except Exception:
                    size = 0
            entries.append({"name": name, "type": entry_type, "size": size})
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)

    return web.json_response({"path": rel_norm, "entries": entries})


async def api_global_file(req: web.Request) -> web.Response:
    """GET /api/global/file?path=<rel> — содержимое файла от $HOME."""
    home = Path.home()
    rel = req.rel_url.query.get("path", "")
    if not rel:
        return web.json_response({"error": "path required"}, status=400)

    try:
        target = _resolve_global_safe(home, rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Секреты проверяем ДО чтения (сохранён anти-traversal _resolve_global_safe)
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)

    return _read_file_content(target, home, rel)


async def api_global_file_write(req: web.Request) -> web.Response:
    """POST /api/global/file?path=<rel> — записать содержимое файла."""
    home = Path.home()
    rel = req.rel_url.query.get("path", "")
    if not rel:
        return web.json_response({"error": "path required"}, status=400)
    try:
        target = _resolve_global_safe(home, rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)
    if not target.exists() or not target.is_file():
        return web.json_response({"error": "not a file"}, status=404)
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    content = data.get("content", "")
    try:
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        return web.json_response({"error": f"write error: {e}"}, status=500)
    return web.json_response({"ok": True, "path": rel})


async def api_card_run(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/tasks/{card}/run — сайдкар из DATA/runs/<card>.md (404-safe).
    Также возвращает meta (mode, has_changes, applied, discarded) из JSON-сайдкара."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    DATA: Path = ctx["DATA"]
    sidecar = DATA / "runs" / f"{card_id}.md"
    meta = _read_run_meta(DATA, card_id)
    if sidecar.exists():
        content = sidecar.read_text(encoding="utf-8", errors="replace")
        return web.json_response({"content": content, "exists": True, "meta": meta})
    return web.json_response({"content": "", "exists": False, "meta": meta})


async def api_card_apply(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/tasks/{card}/apply — применить worktree-ветку (merge --no-ff) в основное дерево."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)

    DATA: Path = ctx["DATA"]
    meta = _read_run_meta(DATA, card_id)

    if not meta or meta.get("mode") != "worktree" or not meta.get("wt_path") or not meta.get("branch"):
        return web.json_response(
            {"error": "карточка выполнена в рабочем дереве (legacy-режим) или нет мета — гейт недоступен"},
            status=400,
        )

    if meta.get("applied"):
        return web.json_response({"error": "карточка уже применена"}, status=400)
    if meta.get("discarded"):
        return web.json_response({"error": "карточка уже отменена"}, status=400)

    wt_path = meta["wt_path"]
    branch = meta["branch"]
    base_branch = meta.get("base_branch", "main")
    cwd = project["cwd"]
    name = project["name"]

    # Проверяем что worktree физически существует
    if not Path(wt_path).exists():
        return web.json_response({"error": "worktree не найден на диске — возможно удалён после рестарта"}, status=400)

    try:
        # Убедимся что HEAD на base_branch
        current_branch = await _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if current_branch != base_branch:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "checkout", base_branch,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode != 0:
                return web.json_response(
                    {"error": f"не удалось переключиться на {base_branch}: {err.decode(errors='replace').strip()}"},
                    status=500,
                )

        # Merge --no-ff
        prompt_short = meta.get("card_id", card_id)
        merge_msg = f"Применить карточку {card_id}"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "merge", "--no-ff", branch, "-m", merge_msg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode != 0:
            # Merge conflict — abort и вернуть 409
            abort_proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "merge", "--abort",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(abort_proc.communicate(), timeout=10.0)
            err_detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            return web.json_response(
                {"error": "merge conflict", "detail": err_detail},
                status=409,
            )

        # Успешный merge: удалить worktree + ветку
        rm_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "worktree", "remove", "--force", wt_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(rm_proc.communicate(), timeout=10.0)
        br_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "branch", "-d", branch,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(br_proc.communicate(), timeout=5.0)

        # Обновить JSON-мета
        meta["applied"] = True
        _write_run_meta(DATA, card_id, meta)

        # Перенести карточку Review → Done
        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            card = _pop_card(cols, card_id)
            dp = _done_path(cwd)
            header = dp.read_text(encoding="utf-8") if dp.exists() else f"# Done — {name}\n"
            if not header.strip():
                header = f"# Done — {name}\n"
            stamp = time.strftime("%Y-%m-%d")
            card_text = card["text"] if card else card_id
            new_done = header.rstrip() + f"\n- [x] {card_text} · {stamp}\n"
            dp.write_text(new_done, encoding="utf-8")
            _save_board(cwd, name, preamble, cols)

        return web.json_response({"ok": True, "applied": True, "card_id": card_id})

    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout при merge"}, status=500)
    except Exception as e:
        return web.json_response({"error": f"внутренняя ошибка: {e}"}, status=500)


async def api_card_discard(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/tasks/{card}/discard — отменить worktree-карточку (ветка удаляется)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)

    DATA: Path = ctx["DATA"]
    meta = _read_run_meta(DATA, card_id)

    if not meta or meta.get("mode") != "worktree" or not meta.get("wt_path") or not meta.get("branch"):
        return web.json_response(
            {"error": "карточка выполнена в рабочем дереве (legacy-режим) или нет мета — гейт недоступен"},
            status=400,
        )

    if meta.get("applied"):
        return web.json_response({"error": "карточка уже применена"}, status=400)
    if meta.get("discarded"):
        return web.json_response({"error": "карточка уже отменена"}, status=400)

    wt_path = meta["wt_path"]
    branch = meta["branch"]
    cwd = project["cwd"]
    name = project["name"]

    try:
        # Удалить worktree (если существует)
        if Path(wt_path).exists():
            rm_proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "worktree", "remove", "--force", wt_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm_proc.communicate(), timeout=10.0)

        # Удалить ветку (404-safe)
        br_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "branch", "-D", branch,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(br_proc.communicate(), timeout=5.0)

        # Обновить JSON-мета
        meta["discarded"] = True
        _write_run_meta(DATA, card_id, meta)

        # Перенести карточку Review → Backlog
        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            card = _pop_card(cols, card_id)
            if card is None:
                card = {"id": card_id, "text": card_id}
            cols["backlog"].append(card)
            _save_board(cwd, name, preamble, cols)

        return web.json_response({"ok": True, "discarded": True, "card_id": card_id})

    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout при discard"}, status=500)
    except Exception as e:
        return web.json_response({"error": f"внутренняя ошибка: {e}"}, status=500)


# ─────────────────────────── C2: сессии проекта ───────────────────────────

def _sdk_sessions_dir(cwd: str) -> Path:
    """Папка SDK с .jsonl-сессиями для данного cwd."""
    return Path.home() / ".claude" / "projects" / cwd.replace("/", "-")


def _migrate_cwd_keyed_state(old_cwd: str, new_cwd: str, ctx: dict) -> list[str]:
    """Переносит cwd-привязанное состояние при переименовании проекта.

    SDK-история диалогов (~/.claude/projects/<slug>/) и Timeline-лента
    (DATA/timeline/<slug>.jsonl) ключуются по slug = cwd.replace('/','-').
    При смене cwd их надо физически перенести — иначе кокпит читает пустой
    новый slug, и сессии/лента «исчезают» (хотя файлы целы под старым slug).
    Best-effort: ошибка миграции НЕ откатывает уже выполненный move папки —
    возвращаем список предупреждений для ответа API.
    """
    warnings: list[str] = []

    # 1. SDK-каталог истории диалогов: ~/.claude/projects/<slug>
    try:
        old_sdk = _sdk_sessions_dir(old_cwd)
        new_sdk = _sdk_sessions_dir(new_cwd)
        if old_sdk.exists() and old_sdk != new_sdk:
            if new_sdk.exists():
                # Каталог назначения занят — переносим файлы по одному, без клоббера
                for f in old_sdk.iterdir():
                    dest = new_sdk / f.name
                    if not dest.exists():
                        shutil.move(str(f), str(dest))
            else:
                new_sdk.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_sdk), str(new_sdk))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"sdk-сессии: {e}")

    # 2. Timeline: DATA/timeline/<slug>.jsonl (+ .jsonl.1 backup)
    try:
        data_dir = ctx.get("DATA")
        if data_dir is not None:
            tdir = Path(data_dir) / "timeline"
            old_slug = old_cwd.replace("/", "-")
            new_slug = new_cwd.replace("/", "-")
            if old_slug != new_slug:
                for suffix in (".jsonl", ".jsonl.1"):
                    src = tdir / f"{old_slug}{suffix}"
                    if src.exists():
                        dst = tdir / f"{new_slug}{suffix}"
                        if not dst.exists():
                            shutil.move(str(src), str(dst))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"timeline: {e}")

    return warnings


def _session_preview(jsonl_path: Path) -> str:
    """Извлечь первое человекочитаемое сообщение из jsonl-файла сессии (~70 симв.)."""
    try:
        lines_read = 0
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines_read += 1
                if lines_read > 80:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Вариант 1: операция enqueue с content-строкой
                if obj.get("operation") == "enqueue":
                    content = obj.get("content")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                        return (text[:70] + "…") if len(text) > 70 else text
                # Вариант 2: message с role=user
                msg = obj.get("message", {})
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                        return (text[:70] + "…") if len(text) > 70 else text
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = (block.get("text") or "").strip()
                                if text:
                                    return (text[:70] + "…") if len(text) > 70 else text
    except Exception:
        pass
    return "(без названия)"


async def api_project_sessions(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/sessions — список сессий SDK для проекта."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    tg_thread = project["tg_thread"]
    active_sid = ctx["sessions"].get(tg_thread)
    sdk_dir = _sdk_sessions_dir(project["cwd"])

    if not sdk_dir.is_dir():
        return web.json_response({"sessions": []})

    labels = _load_session_labels(ctx)

    sessions = []
    try:
        for f in sdk_dir.glob("*.jsonl"):
            sid = f.stem
            try:
                mtime = f.stat().st_mtime
            except Exception:
                mtime = 0
            last_used = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            preview = _session_preview(f)
            sessions.append({
                "session_id": sid,
                "last_used": last_used,
                "preview": preview,
                "is_active": sid == active_sid,
                "label": labels.get(sid) or None,
            })
    except Exception:
        pass

    sessions.sort(key=lambda s: s["last_used"], reverse=True)
    if len(sessions) > 30:
        sessions = sessions[:30]

    return web.json_response({"sessions": sessions})


async def api_project_session_label(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/sessions/{sid}/label  {label}
    Ручной лейбл ЛЮБОЙ сессии (наш слой поверх SDK). Пустой label → снять лейбл.
    Хранилище глобальное по session_id (data/session_labels.json), id проекта — только маршрут."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    sid = os.path.basename(req.match_info["sid"])  # анти-traversal: только basename
    if not sid:
        return web.json_response({"error": "bad session id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    label = (body.get("label") or "").strip()
    if len(label) > 100:
        label = label[:100]
    labels = _load_session_labels(ctx)
    if label:
        labels[sid] = label
    else:
        labels.pop(sid, None)
    _save_session_labels(ctx, labels)
    return web.json_response({"ok": True, "session_id": sid, "label": label or None})


async def api_project_set_session(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/session — переключить или сбросить сессию."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    tg_thread = project["tg_thread"]

    # Замок: нельзя менять сессию пока проект занят
    if ctx["running"].get(tg_thread) is not None:
        return web.json_response(
            {"error": "проект занят, смена сессии недоступна"},
            status=409,
        )

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    action = body.get("action")

    if action == "new":
        ctx["sessions"].pop(tg_thread, None)
        ctx["save_sessions"]()
        return web.json_response({"active": None})

    elif action == "resume":
        session_id = body.get("session_id", "")
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        # Санитизация: только basename (без / и ..) — против выхода на чужой .jsonl
        if session_id != Path(session_id).name or session_id in ("", ".", ".."):
            return web.json_response({"error": "invalid session_id"}, status=400)
        # Валидируем — файл должен существовать
        sdk_dir = _sdk_sessions_dir(project["cwd"])
        candidate = sdk_dir / f"{session_id}.jsonl"
        if not candidate.is_file():
            return web.json_response({"error": "session not found"}, status=400)
        ctx["sessions"][tg_thread] = session_id
        ctx["save_sessions"]()
        return web.json_response({"active": session_id})

    else:
        return web.json_response({"error": "action must be 'new' or 'resume'"}, status=400)


def _session_history(jsonl_path: Path, limit: int = 100) -> list[dict]:
    """Парсит SDK-транскрипт сессии → лента [{role, text, tools}].
    user(str)=реплика человека; user(list)=tool_result, пропуск.
    assistant(list)=блоки text/tool_use. Прочие type — мусор."""
    msgs: list[dict] = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type")
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                if t == "user":
                    c = m.get("content")
                    if isinstance(c, str) and c.strip():
                        msgs.append({"role": "user", "text": c.strip(), "tools": []})
                    # content-list у user = tool_result → пропускаем (не реплика человека)
                elif t == "assistant":
                    c = m.get("content")
                    if not isinstance(c, list):
                        continue
                    text_parts, tools = [], []
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and (b.get("text") or "").strip():
                            text_parts.append(b["text"])
                        elif b.get("type") == "tool_use":
                            inp = b.get("input") or {}
                            tool_name = b.get("name", "?")
                            tools.append(_format_tool(tool_name, inp if isinstance(inp, dict) else {}))
                    if text_parts or tools:
                        msgs.append({"role": "assistant", "text": "\n".join(text_parts), "tools": tools})
    except Exception:
        pass
    return msgs[-limit:] if len(msgs) > limit else msgs


def _session_context_tokens(jsonl_path: Path) -> int:
    """Реальный размер контекста сессии = prompt-токены последнего assistant-хода
    (input + cache_read + cache_creation). Совпадает с get_context_usage().totalTokens.
    0 если транскрипта/usage нет."""
    last = 0
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "assistant":
                    continue
                u = (o.get("message") or {}).get("usage") or {}
                pt = (u.get("input_tokens", 0)
                      + u.get("cache_read_input_tokens", 0)
                      + u.get("cache_creation_input_tokens", 0))
                if pt:
                    last = pt
    except Exception:
        pass
    return last


async def api_project_session_history(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/session-history?session_id=<опц.> — лента активной (или указанной) сессии."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    sid = req.rel_url.query.get("session_id", "") or ctx["sessions"].get(project["tg_thread"])
    if not sid:
        return web.json_response({"messages": [], "session_id": None})
    # Санитизация (basename-only)
    if sid != Path(sid).name or sid in (".", ".."):
        return web.json_response({"error": "invalid session_id"}, status=400)

    jsonl = _sdk_sessions_dir(project["cwd"]) / f"{sid}.jsonl"
    if not jsonl.is_file():
        return web.json_response({"messages": [], "session_id": sid})

    return web.json_response({
        "messages": _session_history(jsonl),
        "session_id": sid,
        "context_tokens": _session_context_tokens(jsonl),
    })


# ─────────────────────────── C1: SSE-чат ───────────────────────────
#
# POST /api/projects/{id}/chat  body: {"prompt": str}
# Ответ: text/event-stream с событиями:
#   data: {"type":"text","text":"..."}
#   data: {"type":"tool","name":"...","input":"..."}
#   data: {"type":"result"}
#   data: {"type":"error","error":"..."}
#   data: {"type":"done"}
#
# Замок ОБЩИЙ с TG и F1-карточками (session_key = project["tg_thread"]).
# Disconnect-устойчивость: если клиент закрыл вкладку (ConnectionResetError при write),
# генератор run_engine продолжает работу до конца, session_id сохраняется, замок снимается.

async def api_project_chat(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]

    # Проверяем run_engine заранее (деградация: старый запуск без F1/C1)
    run_engine = ctx.get("run_engine")
    if run_engine is None:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"},
        )
        await resp.prepare(req)
        payload = json.dumps({"type": "error", "error": "run_engine недоступен"}, ensure_ascii=False)
        await resp.write(f"data: {payload}\n\n".encode())
        return resp

    # Парсим тело запроса
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return web.json_response({"error": "empty prompt"}, status=400)

    # Резолвим проект
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    cwd = project["cwd"]
    name = project["name"]
    model = project.get("model", ctx.get("DEFAULT_MODEL", "sonnet"))
    session_key = project["tg_thread"]  # ОБЩИЙ ключ с TG и F1

    # Проверка замка (СИНХРОННО — до первого await, против гонки)
    if ctx["running"].get(session_key) is not None:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"},
        )
        await resp.prepare(req)
        payload = json.dumps(
            {"type": "error", "error": "проект занят (TG/карточка/чат)"},
            ensure_ascii=False,
        )
        await resp.write(f"data: {payload}\n\n".encode())
        return resp

    # Резервируем слот СИНХРОННО до первого await
    ctx["running"][session_key] = True

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)

    client_gone = False

    async def _send(payload_dict: dict):
        nonlocal client_gone
        if client_gone:
            return
        try:
            line = f"data: {json.dumps(payload_dict, ensure_ascii=False)}\n\n"
            await resp.write(line.encode())
        except (ConnectionResetError, ConnectionAbortedError, Exception) as exc:
            # Клиент закрыл вкладку — помечаем, но НЕ прерываем генератор
            # (задача доигрывает в фоне, session_id сохранится)
            client_gone = True
            print(f"[api_project_chat] клиент отключился ({type(exc).__name__}), задача продолжается в фоне")

    try:
        resume_sid = ctx["sessions"].get(session_key)
        # Секреты проекта подмешиваются в env агента (значения — только в процессе, не в API)
        project_secrets = _secrets_read(cwd)
        async for event in run_engine(
            project_name=name,
            cwd=cwd,
            prompt=prompt,
            session_key=session_key,
            model=model,
            resume_session_id=resume_sid,
            env=project_secrets,
        ):
            etype = event.get("type")
            if etype == "text":
                await _send({"type": "text", "text": event["text"]})
            elif etype == "tool":
                inp = event.get("input") or {}
                tool_data = _format_tool(event["name"], inp if isinstance(inp, dict) else {})
                await _send({"type": "tool", **tool_data})
            elif etype == "result":
                sid = event.get("session_id")
                if sid:
                    ctx["sessions"][session_key] = sid
                    ctx["save_sessions"]()
                    _inherit_label_from_free_chat(ctx, session_key, sid)
                await _send({"type": "result", "context_tokens": event.get("context_tokens", 0)})
            elif etype == "error":
                exc = event.get("exc")
                await _send({"type": "error", "error": str(exc) if exc else "unknown error"})
            elif etype == "rate_limit":
                rl_type = event.get("rate_limit_type")
                if rl_type:
                    ctx["rate_limits"][rl_type] = {
                        "status": event.get("status"),
                        "resets_at": event.get("resets_at"),
                        "utilization": event.get("utilization"),
                        "ts": time.time(),
                    }
                await _send({"type": "rate_limit", "status": event.get("status", "")})
            # прочие типы — игнорируем

        await _send({"type": "done"})

    finally:
        # Замок снимается ГАРАНТИРОВАННО (даже если генератор бросил исключение)
        ctx["running"].pop(session_key, None)

    return resp


# ─────────────────────────── Стоп-эндпоинт (chat/stop) ───────────────────────

async def api_project_chat_stop(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/chat/stop — прерывает текущий прогон агента.
    Кладёт вызов client.interrupt() на реальный SDK-клиент из ctx["running"].
    Возвращает {ok, stopped}; stopped=false если нечего прерывать."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    session_key = project["tg_thread"]
    client = ctx["running"].get(session_key)

    if client is not None and hasattr(client, "interrupt"):
        try:
            await client.interrupt()
        except Exception:
            pass
        return web.json_response({"ok": True, "stopped": True})

    return web.json_response({"ok": True, "stopped": False})


async def api_project_running(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/running — есть ли активный прогон агента в этом проекте."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = project["tg_thread"]
    return web.json_response({"running": ctx["running"].get(session_key) is not None})


# ─────────────────────────── Контекст сессии (session-context) ─────────────

_CTX_TOOL_READ  = {"Read", "Glob", "Grep"}
_CTX_TOOL_EDIT  = {"Edit", "Write", "NotebookEdit"}
_CTX_TOOL_BASH  = {"Bash"}
_CTX_LIST_LIMIT = 200


def _session_context(jsonl_path: Path) -> dict:
    """Парсит SDK-транскрипт: извлекает read/edited/commands из tool_use блоков ассистента.
    Дедуп по значению, первое вхождение wins. Лимит ~200 на категорию."""
    read: list[str]     = []
    edited: list[str]   = []
    commands: list[str] = []
    seen_read: set[str]     = set()
    seen_edited: set[str]   = set()
    seen_commands: set[str] = set()

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "assistant":
                    continue
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                c = m.get("content")
                if not isinstance(c, list):
                    continue
                for block in c:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp  = block.get("input") or {}
                    if not isinstance(inp, dict):
                        continue

                    if name in _CTX_TOOL_READ:
                        # Read → file_path; Glob/Grep → pattern or path
                        val = (inp.get("file_path") or inp.get("path") or inp.get("pattern") or "").strip()
                        if val and val not in seen_read and len(read) < _CTX_LIST_LIMIT:
                            seen_read.add(val)
                            read.append(val)

                    elif name in _CTX_TOOL_EDIT:
                        val = (inp.get("file_path") or "").strip()
                        if val and val not in seen_edited and len(edited) < _CTX_LIST_LIMIT:
                            seen_edited.add(val)
                            edited.append(val)

                    elif name in _CTX_TOOL_BASH:
                        raw = (inp.get("command") or "").strip()
                        val = (raw[:80] + "…") if len(raw) > 80 else raw
                        if val and val not in seen_commands and len(commands) < _CTX_LIST_LIMIT:
                            seen_commands.add(val)
                            commands.append(val)
    except Exception:
        pass

    return {"read": read, "edited": edited, "commands": commands}


async def api_project_session_context(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/session-context?session_id=<опц.>
    Возвращает {read, edited, commands, session_id} для активной (или указанной) сессии."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    sid = req.rel_url.query.get("session_id", "") or ctx["sessions"].get(project["tg_thread"])
    if not sid:
        return web.json_response({"read": [], "edited": [], "commands": [], "session_id": None})

    # Санитизация basename-only (как в session-history)
    if sid != Path(sid).name or sid in (".", ".."):
        return web.json_response({"error": "invalid session_id"}, status=400)

    jsonl = _sdk_sessions_dir(project["cwd"]) / f"{sid}.jsonl"
    if not jsonl.is_file():
        return web.json_response({"read": [], "edited": [], "commands": [], "session_id": sid})

    data = _session_context(jsonl)
    data["session_id"] = sid
    return web.json_response(data)


# ─────────────────────────── Память проекта (memory) ─────────────────────────

_MEMORY_MAX_SIZE = 256 * 1024  # 256 КБ

# Валидный slug для файла памяти: строчные буквы/цифры, дефис, 2-62 символа итого.
# MEMORY.md разрешён отдельно (индекс).
_MEMORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.md$")


def _project_memory_dir(cwd: str) -> Path:
    """Новое место памяти проекта: <cwd>/.claude-ops/memory/ — коммитится в репо."""
    return Path(cwd) / ".claude-ops" / "memory"


def _valid_memory_name(name: str) -> bool:
    """True если name — валидный slug.md без path-компонент."""
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name == "MEMORY.md":
        return True
    return bool(_MEMORY_NAME_RE.match(name))


def _memory_read_all(cwd: str) -> tuple[list[dict], bool]:
    """Читает все *.md из нового места (.claude-ops/memory/).
    Если нового нет, а старое (sdk) есть — АВТО-МИГРАЦИЯ в новое место
    (копируем файлы), затем читаем новое. Так удаление/запись (работающие
    только с новым местом) перестают давать 404 на legacy-памяти.
    Возвращает (files, from_legacy). files = [{name, content}], MEMORY.md первым."""
    new_dir = _project_memory_dir(cwd)
    if new_dir.is_dir():
        return _read_memory_dir(new_dir), False
    # Авто-миграция старого места ~/.claude/projects/<cwd>/memory/ → .claude-ops/memory/
    old_dir = _sdk_sessions_dir(cwd) / "memory"
    if old_dir.is_dir():
        migrated = False
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            for f in old_dir.glob("*.md"):
                dest = new_dir / f.name
                if not dest.exists():
                    dest.write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            migrated = True
        except Exception as e:
            print(f"[memory] авто-миграция legacy→new не удалась для {cwd}: {e}")
        if migrated and new_dir.is_dir():
            return _read_memory_dir(new_dir), False
        # миграция не удалась — читаем старое как было (legacy)
        return _read_memory_dir(old_dir), True
    return [], False


def _read_memory_dir(mem_dir: Path) -> list[dict]:
    """Вспомогательный: читает *.md из указанной директории памяти."""
    files: list[dict] = []
    try:
        md_files = sorted(
            mem_dir.glob("*.md"),
            key=lambda p: (0 if p.name == "MEMORY.md" else 1, p.name),
        )
        for f in md_files:
            try:
                size = f.stat().st_size
                if size > _MEMORY_MAX_SIZE:
                    content = f"[файл слишком большой: {size} байт]"
                else:
                    content = f.read_text(encoding="utf-8", errors="replace")
                files.append({"name": f.name, "content": content})
            except Exception:
                pass
    except Exception:
        pass
    return files


def _memory_write(cwd: str, name: str, content: str) -> None:
    """Атомарно записывает файл памяти, создаёт директорию если нет.
    Затем перестраивает MEMORY.md-индекс."""
    if not _valid_memory_name(name):
        raise ValueError(f"invalid memory file name: {name!r}")
    if len(content.encode("utf-8")) > _MEMORY_MAX_SIZE:
        raise ValueError("content exceeds _MEMORY_MAX_SIZE")
    mem_dir = _project_memory_dir(cwd)
    mem_dir.mkdir(parents=True, exist_ok=True)
    target = mem_dir / name
    # Атомарная запись через tmp
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    if name != "MEMORY.md":
        _memory_reindex(cwd)


def _memory_delete(cwd: str, name: str) -> bool:
    """Удаляет файл памяти. Возвращает True если удалён, False если не существовал."""
    if not _valid_memory_name(name):
        raise ValueError(f"invalid memory file name: {name!r}")
    if name == "MEMORY.md":
        raise ValueError("cannot delete MEMORY.md directly")
    target = _project_memory_dir(cwd) / name
    if not target.exists():
        return False
    target.unlink()
    _memory_reindex(cwd)
    return True


def _memory_reindex(cwd: str) -> None:
    """Перестраивает MEMORY.md как индекс всех записей в .claude-ops/memory/.
    Формат строки: - [Заголовок](file.md) — хук (из frontmatter или первой строки)."""
    mem_dir = _project_memory_dir(cwd)
    if not mem_dir.is_dir():
        return
    entries = sorted(
        (p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"),
        key=lambda p: p.name,
    )
    lines = ["# Память проекта\n", "\n"]
    for entry in entries:
        try:
            raw = entry.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw = ""
        title, hook = _memory_parse_entry(entry.name, raw)
        line = f"- [{title}]({entry.name})"
        if hook:
            line += f" — {hook}"
        lines.append(line + "\n")
    index_path = mem_dir / "MEMORY.md"
    index_path.write_text("".join(lines), encoding="utf-8")


def _memory_parse_entry(filename: str, content: str) -> tuple[str, str]:
    """Извлекает (заголовок, хук) из файла записи памяти.
    Заголовок — из frontmatter 'title' или первая строка с #/текстом.
    Хук — первое непустое предложение тела после frontmatter."""
    lines = content.splitlines()
    idx = 0
    fm: dict[str, str] = {}
    # Парсим YAML frontmatter (--- ... ---)
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines) and lines[idx].strip() != "---":
            kv = lines[idx].split(":", 1)
            if len(kv) == 2:
                fm[kv[0].strip()] = kv[1].strip()
            idx += 1
        idx += 1  # пропустить закрывающий ---

    # Заголовок: из frontmatter или из первой строки тела
    title = fm.get("title", "")
    if not title:
        for line in lines[idx:]:
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
            if line:
                title = line[:60]
                break
    if not title:
        title = filename[:-3]  # убрать .md

    # Хук: первое непустое предложение тела
    hook = fm.get("hook", "")
    if not hook:
        for line in lines[idx:]:
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith("---"):
                hook = line[:100]
                break

    return title, hook


async def api_project_memory(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/memory
    Возвращает {files:[{name, content}], exists}.
    Новое место: <cwd>/.claude-ops/memory/. Fallback: старое ~/.claude/projects/.
    MEMORY.md — первым в списке (индекс)."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    files, _legacy = _memory_read_all(project["cwd"])
    if not files:
        return web.json_response({"files": [], "exists": False})
    return web.json_response({"files": files, "exists": True})


async def api_project_memory_write(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/memory/{name}
    Создаёт или обновляет запись памяти. Обновляет MEMORY.md-индекс.
    Возвращает обновлённый список {files, exists}."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    name = req.match_info["name"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    if not _valid_memory_name(name):
        return web.json_response({"error": "invalid file name"}, status=400)

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    content = body.get("content", "")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be string"}, status=400)
    if len(content.encode("utf-8")) > _MEMORY_MAX_SIZE:
        return web.json_response({"error": "content too large"}, status=400)

    try:
        _memory_write(project["cwd"], name, content)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"write failed: {e}"}, status=500)

    files, _ = _memory_read_all(project["cwd"])
    return web.json_response({"files": files, "exists": True})


async def api_project_memory_delete(req: web.Request) -> web.Response:
    """DELETE /api/projects/{id}/memory/{name}
    Удаляет запись памяти. Обновляет MEMORY.md-индекс.
    Возвращает обновлённый список {files, exists}."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    name = req.match_info["name"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    if not _valid_memory_name(name):
        return web.json_response({"error": "invalid file name"}, status=400)

    if name == "MEMORY.md":
        return web.json_response({"error": "cannot delete MEMORY.md"}, status=400)

    try:
        deleted = _memory_delete(project["cwd"], name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"delete failed: {e}"}, status=500)

    if not deleted:
        return web.json_response({"error": "not found"}, status=404)

    files, _ = _memory_read_all(project["cwd"])
    exists = bool(files)
    return web.json_response({"files": files, "exists": exists})


# ─────────────────────────── Секреты проекта (secrets) ──────────────────────────────────────
#
# Хранилище: <cwd>/.claude-ops/secrets/secrets.env (chmod 600, не в git)
# Формат:    KEY=VALUE построчно, # — комментарии, пустые строки — игнор
# Безопасность:
#   - Имена ключей: ^[A-Z_][A-Z0-9_]*$ (env-совместимые, anti-injection)
#   - Значения НИКОГДА не возвращаются через API — только список имён
#   - .claude-ops/secrets/ добавляется в .gitignore при первой записи
#   - chmod 600 на secrets.env при каждой записи
#   - Изоляция по cwd: секреты одного проекта не видны другому

_SECRETS_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRETS_MAX_VALUE_SIZE = 8 * 1024   # 8 КБ на значение
_SECRETS_MAX_KEYS = 100              # максимум ключей в одном проекте


def _project_secrets_path(cwd: str) -> Path:
    """Путь к файлу секретов: <cwd>/.claude-ops/secrets/secrets.env."""
    return Path(cwd) / ".claude-ops" / "secrets" / "secrets.env"


def _secrets_read(cwd: str) -> dict:
    """Читает KEY=VALUE из secrets.env. Нет файла → {}.
    Комментарии (#) и пустые строки игнорируются."""
    path = _project_secrets_path(cwd)
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                if _SECRETS_KEY_RE.match(k):
                    result[k] = v
    except Exception:
        pass
    return result


def _secrets_ensure_gitignore(cwd: str) -> None:
    """Гарантирует что .claude-ops/secrets/ есть в .gitignore проекта.
    Дописывает строку если нет."""
    gitignore = Path(cwd) / ".gitignore"
    line = ".claude-ops/secrets/"
    try:
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            if line in content:
                return
            # Дописываем в конец
            if not content.endswith("\n"):
                content += "\n"
            content += f"{line}\n"
        else:
            content = f"{line}\n"
        gitignore.write_text(content, encoding="utf-8")
    except Exception:
        pass


def _secrets_write(cwd: str, data: dict) -> None:
    """Атомарно записывает secrets.env (tmp+replace), chmod 600, mkdir.
    Гарантирует .claude-ops/secrets/ в .gitignore."""
    path = _project_secrets_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# Секреты проекта — НЕ коммитить, не передавать третьим лицам\n"]
    for k, v in sorted(data.items()):
        lines.append(f"{k}={v}\n")

    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

    # chmod 600 на итоговый файл (на случай если replace не сохранил права на некоторых ФС)
    try:
        path.chmod(0o600)
    except Exception:
        pass

    _secrets_ensure_gitignore(cwd)


def _secrets_set(cwd: str, key: str, value: str) -> None:
    """Устанавливает (добавляет/обновляет) одну пару KEY=VALUE."""
    if not _SECRETS_KEY_RE.match(key):
        raise ValueError(f"invalid key name: {key!r}")
    if len(value.encode("utf-8")) > _SECRETS_MAX_VALUE_SIZE:
        raise ValueError("value too large (max 8KB)")
    data = _secrets_read(cwd)
    if key not in data and len(data) >= _SECRETS_MAX_KEYS:
        raise ValueError(f"too many keys (max {_SECRETS_MAX_KEYS})")
    data[key] = value
    _secrets_write(cwd, data)


def _secrets_delete(cwd: str, key: str) -> bool:
    """Удаляет ключ. Возвращает True если удалён, False если не существовал."""
    if not _SECRETS_KEY_RE.match(key):
        raise ValueError(f"invalid key name: {key!r}")
    data = _secrets_read(cwd)
    if key not in data:
        return False
    del data[key]
    _secrets_write(cwd, data)
    return True


# ─────────────────────────── API секретов (CRUD) ─────────────────────────────


async def api_project_secrets(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/secrets — список ИМЁН ключей (без значений).
    ⚠️ Значения секретов никогда не возвращаются клиенту."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    data = _secrets_read(project["cwd"])
    return web.json_response({"keys": sorted(data.keys()), "exists": bool(data)})


async def api_project_secrets_set(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/secrets/{key} — задать секрет.
    Body: {value: str}. Возвращает обновлённый список имён (без значений)."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    key = req.match_info["key"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Anti-traversal: имя ключа не должно содержать path-компонент
    if "/" in key or "\\" in key or ".." in key:
        return web.json_response({"error": "invalid key name"}, status=400)
    if not _SECRETS_KEY_RE.match(key):
        return web.json_response({"error": "invalid key name (must match ^[A-Z_][A-Z0-9_]*$)"}, status=400)

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request body"}, status=400)

    value = body.get("value", "")
    if not isinstance(value, str):
        return web.json_response({"error": "value must be string"}, status=400)

    try:
        _secrets_set(project["cwd"], key, value)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"write failed: {e}"}, status=500)

    data = _secrets_read(project["cwd"])
    return web.json_response({"keys": sorted(data.keys()), "exists": bool(data)})


async def api_project_secrets_delete(req: web.Request) -> web.Response:
    """DELETE /api/projects/{id}/secrets/{key} — удалить секрет."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    key = req.match_info["key"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Anti-traversal
    if "/" in key or "\\" in key or ".." in key:
        return web.json_response({"error": "invalid key name"}, status=400)
    if not _SECRETS_KEY_RE.match(key):
        return web.json_response({"error": "invalid key name"}, status=400)

    try:
        deleted = _secrets_delete(project["cwd"], key)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"delete failed: {e}"}, status=500)

    if not deleted:
        return web.json_response({"error": "key not found"}, status=404)

    data = _secrets_read(project["cwd"])
    return web.json_response({"keys": sorted(data.keys()), "exists": bool(data)})


# ─────────────────────────── новый проект: шаблоны + инициализация ───────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$")

_NEW_PROJECT_PROMPT = """\
🚀 Новый проект инициализируется. Папка: {cwd}.

В корне уже лежат стартовые шаблоны: CLAUDE.md, TASKS.md, README.md, .gitignore. Это каркас — нужно его адаптировать.

ШАГ 1 — расспроси меня (одним сообщением, текстом, в конце ответа):
- Что за проект, цель (1-2 фразы)?
- Стек/язык/инфра?
- Есть ли уже наработки/файлы/код где-то ещё? Точные пути если знаешь.
- Какие первые 3-5 задач?

ШАГ 2 (после моих ответов):
- Если указал существующие папки → просканируй их (Read нескольких файлов), краткая сводка.
- Перепиши секции «Что это» / «Стек» / «Команды» в CLAUDE.md под мои ответы. Раздел «Правила работы в кокпите» — НЕ ТРОГАЙ, он общий для всех проектов.
- Замени стартовые карточки в TASKS.md → ## Backlog (положи мои реальные 3-5 задач, удали стартовые «Заполнить …» если уже сделал).
- Заполни README.md.
- При необходимости создай `specs/`, `tests/`, и т.д. — по обстановке.
- если веб-сервис/бот → добавь глобальный error handler (FastAPI/aiohttp middleware, PTB add_error_handler, CLI try/except в main → logger.error). Иначе кокпит не видит рантайм-ошибки. Логируй стандартной строкой `UNHANDLED exc_class=<Type> path=<route>`.

ШАГ 3 — имя проекта:
- Предложи мне короткий kebab-case slug (например `my-cool-bot`). Спроси «переименовать сейчас?»
- Если ОК — попроси меня нажать кнопку ✏️ в шапке проекта (она вызовет API rename — папка переедет, topics.json обновится без рестарта).

ШАГ 4 — git init (без коммита/пуша, без моего явного ОК).

Не вали скриптом, веди диалог по шагам. 3-5 точечных вопросов за раз, потом жди ответа.\
"""

def _build_audit_prompt(ctx: dict, project_name: str) -> str:
    """Audit-промт: преамбула + baseline-чек-лист из templates/reference/audit-prompt.md.
    Сам baseline лежит файлом — Игорь правит его без правки кода."""
    here: Path = ctx["HERE"]
    base = (here / "templates" / "reference" / "audit-prompt.md").read_text(encoding="utf-8")
    return (
        f"🩺 Аудит проекта **{project_name}**.\n\n"
        f"Пройдись по этому чек-листу (baseline ниже). Для КАЖДОЙ найденной проблемы создай "
        f"новую карточку в `## Backlog` файла `TASKS.md` (формат: `- [ ] текст` строго внутри секции; "
        f"маркер `ops:ID` добавится автоматически — не вписывай руками).\n\n"
        f"В конце — короткое резюме в чате: «N проблем найдено, M карточек создано».\n\n"
        f"---\n\n{base}"
    )


def _render_template(template_name: str, vars: dict, here: Path) -> str:
    """Читает templates/<template_name>, заменяет {{var}} → значение из vars."""
    tpl_path = here / "templates" / template_name
    try:
        text = tpl_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"шаблон не найден: {tpl_path}")
    for key, val in vars.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


async def api_new_project(req: web.Request) -> web.Response:
    """POST /api/projects/new — создаёт новую папку проекта со стартовыми шаблонами и
    запускает инициализацию через run_engine (как F1-карточка)."""
    ctx = req.app["ctx"]
    run_engine = ctx.get("run_engine")

    # Парсим тело (name опционален)
    try:
        body = await req.json()
    except Exception:
        body = {}
    name = (body.get("name") or "").strip() or None

    # Создаём папку ~/projects/untitled-<ts>/
    projects_dir = Path.home() / "projects"
    projects_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    slug = f"untitled-{ts}"
    cwd = projects_dir / slug
    try:
        cwd.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return web.json_response({"error": f"папка уже существует: {cwd}"}, status=409)

    display_name = name or slug
    here: Path = ctx["HERE"]
    tpl_vars = {
        "name": display_name,
        "date": time.strftime("%Y-%m-%d"),
        "slug": slug,
    }

    # Пишем шаблоны
    try:
        (cwd / "CLAUDE.md").write_text(_render_template("CLAUDE.md.tpl", tpl_vars, here), encoding="utf-8")
        (cwd / "README.md").write_text(_render_template("README.md.tpl", tpl_vars, here), encoding="utf-8")
        (cwd / ".gitignore").write_text(_render_template(".gitignore.tpl", tpl_vars, here), encoding="utf-8")

        # TASKS.md: рендерим шаблон, затем парсим и добавляем стартовую карточку в In Progress
        tasks_raw = _render_template("TASKS.md.tpl", tpl_vars, here)
        preamble, cols = _parse_tasks(tasks_raw)
        init_card = {"id": _new_card_id(), "text": "🚀 Инициализировать проект"}
        cols["in_progress"].append(init_card)
        (cwd / "TASKS.md").write_text(_serialize_tasks(preamble, cols, display_name), encoding="utf-8")
    except Exception as e:
        # Откат: удаляем папку если не смогли записать файлы
        shutil.rmtree(str(cwd), ignore_errors=True)
        return web.json_response({"error": f"ошибка записи шаблонов: {e}"}, status=500)

    # Регистрируем в topics.json. Пытаемся создать РЕАЛЬНЫЙ forum-топик в TG —
    # бот админ супергруппы с manage_topics, поэтому проект сразу доступен в
    # Telegram (чат + авто-запуск карточек). Имя проекта формируется ПОЗЖЕ
    # онбордингом, поэтому топик создаём с плейсхолдером, а при rename имя
    # топика синкается через editForumTopic (_sync_forum_topic_name).
    # Если создать топик не удалось (нет прав/ошибка API) — фоллбэк на
    # синтетический ключ chat:ts (как раньше), проект всё равно создаётся.
    group_chat_id = ctx.get("GROUP_CHAT_ID") or 0
    thread_id = None
    ptb_app = ctx.get("ptb_app")
    if ptb_app and group_chat_id:
        try:
            topic = await ptb_app.bot.create_forum_topic(
                chat_id=group_chat_id,
                name=(display_name if name else "🆕 Новый проект"),
            )
            thread_id = topic.message_thread_id
            print(f"[new_project] forum-топик создан: thread={thread_id}")
        except Exception as e:
            print(f"[new_project] create_forum_topic не удался ({e}) — синтетический ключ")
    session_key = f"{group_chat_id}:{thread_id if thread_id is not None else ts}"
    ctx["topics"][session_key] = {
        "project": display_name,
        "cwd": str(cwd),
        "model": _effective_default_model(ctx),
    }
    save_topics = ctx.get("save_topics")
    if callable(save_topics):
        save_topics()

    pid = _project_id(str(cwd))
    project = _find_project_by_id(ctx, pid)
    if project is None:
        # Формируем минимальный объект на случай если dupe по cwd вытеснил нашу запись
        project = {
            "id": pid,
            "name": display_name,
            "cwd": str(cwd),
            "model": _effective_default_model(ctx),
            "tg_thread": session_key,
            "is_free": False,
        }

    # Если run_engine недоступен — возвращаем без запуска (деградация)
    if run_engine is None:
        return web.json_response({"id": pid, "cwd": str(cwd), "name": display_name, "started": False})

    # Проверяем замок (теоретически free slot — только что создали)
    if ctx["running"].get(session_key) is not None:
        return web.json_response({"id": pid, "cwd": str(cwd), "name": display_name, "started": False})

    # Резервируем слот СИНХРОННО (защита от гонки — та же что в api_move_task)
    ctx["running"][session_key] = True

    # Подменяем текст карточки на онбординг-промпт ДО запуска задачи
    # (run_engine получает card["text"] как prompt)
    init_card["text"] = _NEW_PROJECT_PROMPT.format(cwd=str(cwd))
    _spawn_bg(_run_card(ctx, req.app, project, init_card, session_key))

    return web.json_response({
        "id": pid,
        "cwd": str(cwd),
        "name": display_name,
        "session_key": session_key,
        "started": True,
    })


async def api_project_rename(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/rename  {slug: str}
    Переименовывает папку проекта и обновляет все записи topics.json с тем же cwd."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    slug = (body.get("slug") or "").strip()
    if not slug:
        return web.json_response({"error": "slug is required"}, status=400)
    if not _SLUG_RE.match(slug):
        return web.json_response(
            {"error": "slug must match ^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$ (kebab-case)"},
            status=400,
        )

    session_key = project["tg_thread"]
    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "проект занят, нельзя переименовать"}, status=409)

    old_cwd = Path(project["cwd"])
    new_cwd = old_cwd.parent / slug

    if new_cwd.exists():
        return web.json_response({"error": f"папка уже занята: {new_cwd}"}, status=409)

    try:
        shutil.move(str(old_cwd), str(new_cwd))
    except Exception as e:
        return web.json_response({"error": f"ошибка переименования: {e}"}, status=500)

    # Обновляем все записи topics с тем же старым cwd
    old_cwd_str = str(old_cwd)
    for b in ctx["topics"].values():
        if b.get("cwd") == old_cwd_str:
            b["cwd"] = str(new_cwd)
            b["project"] = slug

    save_topics = ctx.get("save_topics")
    if callable(save_topics):
        save_topics()

    # Переносим cwd-привязанное состояние (SDK-история диалогов + Timeline),
    # иначе после смены cwd сессии и лента «исчезают» — файлы остаются под старым slug.
    migrate_warnings = _migrate_cwd_keyed_state(old_cwd_str, str(new_cwd), ctx)

    # Синкаем имя forum-топика в TG (если у проекта реальный топик)
    await _sync_forum_topic_name(ctx, session_key, slug)

    resp_body = {
        "ok": True,
        "new_id": new_cwd.name,
        "new_cwd": str(new_cwd),
        "new_name": slug,
    }
    if migrate_warnings:
        resp_body["warnings"] = migrate_warnings
    return web.json_response(resp_body)


_DETECT_ERROR_HANDLER_SUBSTRINGS = (
    "@app.exception_handler",
    "add_error_handler",
    "error_middleware",
    "app.add_middleware",
    "@exception_handler",
    "setup_exception_handlers",
    "UNHANDLED exc_class=",   # проект принял стандартную лог-строку кокпита
)
_DETECT_EH_CONFORMANCE_RE = re.compile(r"(?im)^\s*-?\s*error handler:\s*(.+)$")
_DETECT_EH_EXCLUDE_DIRS = {"venv", ".venv", "node_modules", ".git", "dist", "build", "__pycache__", ".worktrees"}


def _detect_error_handler(cwd: Path, claude_md_text: str) -> bool:
    """Быстрый (bounded) детектор наличия глобального error handler в проекте.

    (a) Self-declaration: ## ClaudeOps conformance + строка 'error handler: <не пустое/нет>'
    (b) Code heuristic: обходим *.py (до 60 файлов / 3 MB), ищем substring-маркеры.
    Возвращает True при первом совпадении. try/except → False при любой ошибке."""
    try:
        # (a) Self-declaration — ТОЛЬКО в секции ## ClaudeOps conformance
        # (иначе строка 'error handler:' из любого другого раздела даст ложный плюс)
        if "## ClaudeOps conformance" in claude_md_text:
            section = claude_md_text.split("## ClaudeOps conformance", 1)[1].split("\n## ", 1)[0]
            m = _DETECT_EH_CONFORMANCE_RE.search(section)
            if m:
                val = m.group(1).strip().lower()
                if val not in {"нет", "no", "-", "—", ""}:
                    return True

        # (b) Code heuristic — bounded scan; os.walk прунит шумные директории,
        # НЕ спускаясь в venv/node_modules (rglob их всё равно обходит).
        files_checked = 0
        bytes_read = 0
        _MAX_FILES = 60
        _MAX_BYTES = 3 * 1024 * 1024  # 3 MB
        for root, dirs, names in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in _DETECT_EH_EXCLUDE_DIRS]
            for name in names:
                if not name.endswith(".py"):
                    continue
                if files_checked >= _MAX_FILES or bytes_read >= _MAX_BYTES:
                    return False
                try:
                    text = Path(root, name).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                files_checked += 1
                bytes_read += len(text)
                for substr in _DETECT_ERROR_HANDLER_SUBSTRINGS:
                    if substr in text:
                        return True
    except Exception:
        return False
    return False


async def api_project_health(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/health — быстрая проверка структуры проекта без агента."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    cwd = Path(project["cwd"])

    def _check(key: str, label: str, condition: bool, hint: str | None, optional: bool = False) -> dict:
        return {"key": key, "label": label, "ok": condition, "hint": hint if not condition else None, "optional": optional}

    items: list[dict] = []

    # 1. CLAUDE.md существует
    claude_md = cwd / "CLAUDE.md"
    has_claude_md = claude_md.is_file()
    items.append(_check("claude_md", "CLAUDE.md", has_claude_md, "Создай CLAUDE.md с описанием проекта"))

    # 2. CLAUDE.md содержит раздел «Правила работы в кокпите»
    cockpit_rules = False
    claude_md_text = ""
    if has_claude_md:
        try:
            claude_md_text = claude_md.read_text(encoding="utf-8", errors="replace")
            cockpit_rules = "Правила работы в кокпите" in claude_md_text
        except Exception:
            pass
    items.append(_check(
        "claude_md_cockpit_rules", "Раздел «Правила кокпита»",
        cockpit_rules, "Запусти аудит или ✏️ обнови вручную",
    ))

    # 3. TASKS.md существует с преамбулой
    tasks_md = cwd / "TASKS.md"
    has_tasks = False
    if tasks_md.is_file():
        try:
            tasks_content = tasks_md.read_text(encoding="utf-8", errors="replace")
            # Достаточно наличия любого ops-маркера ИЛИ формата `- [ ] текст <!--ops:`
            has_tasks = "<!--ops:" in tasks_content or "Формат карточки" in tasks_content
        except Exception:
            pass
    items.append(_check("tasks_md", "TASKS.md с преамбулой", has_tasks, "Создай TASKS.md с форматом колонок"))

    # 4. README.md существует (любой регистр)
    has_readme = any((cwd / name).is_file() for name in _README_CANDIDATES)
    items.append(_check("readme", "README.md", has_readme, "Запусти аудит"))

    # 5. .gitignore существует и содержит .env
    gitignore = cwd / ".gitignore"
    has_gitignore_env = False
    if gitignore.is_file():
        try:
            has_gitignore_env = ".env" in gitignore.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    items.append(_check("gitignore", ".gitignore с .env", has_gitignore_env, "Добавь .env в .gitignore"))

    # 6. git init (папка .git существует) — если git отключён настройкой, не требуем
    if not _git_enabled(project):
        items.append(_check("git_init", "git (отключён в настройках)", True, None))
    else:
        has_git = (cwd / ".git").exists()
        items.append(_check("git_init", "git init", has_git, "Запусти git init в папке проекта"))

    # ── Capability checks ──────────────────────────────────────────────────────
    # cap_log_cmd: кокпит получает логи и рантайм-ошибки только если задан log_cmd
    items.append(_check(
        "cap_log_cmd", "log_cmd задан (логи в кокпит)",
        bool(project.get("log_cmd")),
        "Задай log_cmd — иначе кокпит не видит логи и рантайм-ошибки",
    ))
    # cap_error_handler: глобальный error handler в коде или задекларирован в CLAUDE.md
    items.append(_check(
        "cap_error_handler", "Глобальный error handler",
        _detect_error_handler(cwd, claude_md_text),
        "Сервису/боту добавь глобальный error handler (пишет ошибки в лог) "
        "ИЛИ задекларируй в CLAUDE.md (## ClaudeOps conformance)",
    ))
    # cap_test_cmd: опциональный — не влияет на score
    items.append(_check(
        "cap_test_cmd", "test_cmd задан (опц., по кнопке)",
        bool(project.get("test_cmd")),
        "Опц.: задай test_cmd для кнопки «Прогнать тесты» и quality gate",
        optional=True,
    ))

    score = sum(1 for i in items if i["ok"] and not i.get("optional"))
    total = sum(1 for i in items if not i.get("optional"))
    if total == 0:
        color = "green"
    elif score == total:
        color = "green"
    elif score >= total / 2:
        color = "yellow"
    else:
        color = "red"

    return web.json_response({"items": items, "score": score, "total": total, "color": color})


async def api_project_audit(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/audit — создаёт карточку аудита и запускает её через run_engine."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    run_engine = ctx.get("run_engine")
    session_key = project["tg_thread"]
    cwd = project["cwd"]
    name = project["name"]

    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "проект занят"}, status=409)

    # Создаём карточку аудита в In Progress
    audit_card = {"id": _new_card_id(), "text": f"🩺 Аудит проекта «{name}»"}
    audit_prompt = _build_audit_prompt(ctx, name)

    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        cols["in_progress"].append(audit_card)
        _save_board(cwd, name, preamble, cols)

    if run_engine is None:
        return web.json_response({"ok": True, "card_id": audit_card["id"], "started": False})

    # Резервируем слот СИНХРОННО
    ctx["running"][session_key] = True

    # Подменяем текст карточки на полный промпт перед запуском
    audit_card["text"] = audit_prompt
    _spawn_bg(_run_card(ctx, req.app, project, audit_card, session_key))

    return web.json_response({"ok": True, "card_id": audit_card["id"], "started": True})


_UPGRADE_PROMPT_TPL = """🔧 Подтянуть проект «{name}» до стандарта кокпита.

ВАЖНО: НЕ переписывай существующее содержимое CLAUDE.md/TASKS.md/README.md/.gitignore — только ДОПОЛНЯЙ недостающее. Если файла нет — создай из шаблона.

Эталоны лежат в `{tpl_dir}`:
- `CLAUDE.md.tpl` — образец структуры, **обязательно** содержит секцию «Правила работы в кокпите» — её скопируй в CLAUDE.md проекта (если ещё нет), переменные `{{{{name}}}}` замени на актуальное имя.
- `TASKS.md.tpl` — преамбула формата карточек. Если в текущем TASKS.md нет преамбулы с фразой «Формат карточки» — добавь её ПЕРЕД первой `##` колонкой.
- `README.md.tpl` — если README отсутствует, создай минимальный.
- `.gitignore.tpl` — если в текущем нет `.env` — добавь раздел Secrets.

Шаги:
1. Прочитай `CLAUDE.md`, `TASKS.md`, `README.md`, `.gitignore` (если есть) в текущем cwd.
2. Прочитай шаблоны в `{tpl_dir}/*.tpl`.
3. Для каждого недостающего блока — добавь его, сохранив весь существующий контент.
4. НЕ ТРОГАЙ карточки в TASKS.md — только преамбулу выше первой `##`.
5. В конце — короткое резюме в чате: «Добавил/обновил: A, B, C; не трогал: X, Y».
"""


async def api_project_upgrade(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/upgrade — карточка «🔧 Подтянуть до стандарта»: дополняет CLAUDE.md/TASKS.md/README/.gitignore по шаблонам, существующее не переписывает."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    run_engine = ctx.get("run_engine")
    session_key = project["tg_thread"]
    cwd = project["cwd"]
    name = project["name"]

    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "проект занят"}, status=409)

    card = {"id": _new_card_id(), "text": f"🔧 Подтянуть «{name}» до стандарта"}
    here: Path = ctx.get("HERE") or Path(__file__).resolve().parent
    tpl_dir = str(here / "templates")
    prompt = _UPGRADE_PROMPT_TPL.format(name=name, tpl_dir=tpl_dir)

    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        cols["in_progress"].append(card)
        _save_board(cwd, name, preamble, cols)

    if run_engine is None:
        return web.json_response({"ok": True, "card_id": card["id"], "started": False})

    ctx["running"][session_key] = True
    card["text"] = prompt
    _spawn_bg(_run_card(ctx, req.app, project, card, session_key))
    return web.json_response({"ok": True, "card_id": card["id"], "started": True})


# ─────────────────────────── статика (SPA) ───────────────────────────

PLACEHOLDER_HTML = (
    "Фронтенд ещё не собран: cd web && npm install && npm run build"
)


async def spa_handler(req: web.Request) -> web.Response:
    """Отдаёт статику из web/dist. SPA-роутинг — fallback на index.html."""
    dist: Path = req.app["ctx"]["HERE"] / "web" / "dist"
    index = dist / "index.html"

    # Если dist вообще нет — заглушка
    if not dist.exists() or not index.exists():
        return web.Response(text=PLACEHOLDER_HTML, content_type="text/plain")

    # Нормализуем путь
    rel = req.path.lstrip("/") or "index.html"
    target = (dist / rel).resolve()

    # Защита от выхода за пределы dist
    try:
        target.relative_to(dist.resolve())
    except ValueError:
        # path traversal → отдаём index (безопасно)
        return web.FileResponse(index)

    if target.is_file():
        return web.FileResponse(target)

    # SPA fallback
    return web.FileResponse(index)


# ─────────────────────────── точка входа ───────────────────────────

async def start(ptb_app, ctx: dict) -> None:
    """Поднимает aiohttp-сервер кокпита в том же процессе/loop, что и бот. НЕ блокирует."""
    # Гарантируем, что error_middleware (logging.exception) реально пишет в журнал,
    # даже если корневой логгер ещё не настроен (иначе ERROR уходит в lastResort).
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING,
                             format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = ctx["port"]
    try:
        # Деривируем токен один раз при старте (scrypt медленный — не на каждый запрос)
        ctx["_auth_token"] = _derive_token(ctx["password"])

        # Timeline: инициализируем персистентность шины (DATA/timeline/)
        _timeline_init(ctx)
        _settings_init(ctx)
        # Spec-012 Ф0: инициализируем пути к файлам scan_state + dismissed_incidents
        _scan_state_init(ctx)

        app = web.Application(middlewares=[error_middleware, auth_middleware], client_max_size=20 * 1024 * 1024)
        app["ctx"] = ctx

        # F1: сохраняем ссылку на PTB-приложение для пинга в TG из _run_card
        app["ptb_app"] = ptb_app
        # Также кладём в ctx для доступа из _run_card через ctx["ptb_app"]
        ctx["ptb_app"] = ptb_app
        # Card Queue: сохраняем aiohttp-приложение в ctx для _drain_queue из _run_card и loop
        ctx["_aiohttp_app"] = app

        # API-роуты
        app.router.add_get("/api/health", api_health)
        app.router.add_post("/api/login", api_login)
        app.router.add_post("/api/logout", api_logout)
        app.router.add_get("/api/me", api_me)
        app.router.add_get("/api/projects", api_projects)
        app.router.add_get("/api/settings", api_settings_get)
        app.router.add_post("/api/settings", api_settings_post)
        app.router.add_get("/api/projects/{id}/settings", api_project_settings_get)
        app.router.add_post("/api/projects/{id}/settings", api_project_settings_post)
        # «+ Новый проект» — создаёт untitled-<ts>/, добавляет в topics.json, спавнит онбординг
        app.router.add_post("/api/projects/new", api_new_project)
        app.router.add_get("/api/projects/{id}/claude-md", api_project_claude_md)
        app.router.add_post("/api/projects/{id}/claude-md", api_project_claude_md_write)
        app.router.add_get("/api/projects/{id}/readme", api_project_readme)
        app.router.add_post("/api/projects/{id}/readme", api_project_readme_write)
        app.router.add_get("/api/projects/{id}/specs", api_project_specs)
        app.router.add_get("/api/projects/{id}/specs/{name}", api_project_spec_content)
        app.router.add_get("/api/projects/{id}/logs", api_project_logs)
        app.router.add_get("/api/projects/{id}/activity", api_project_activity)
        # Доска задач (TASKS.md / DONE.md)
        app.router.add_get("/api/projects/{id}/tasks", api_project_tasks)
        app.router.add_post("/api/projects/{id}/tasks", api_create_task)
        app.router.add_get("/api/projects/{id}/tasks/done", api_tasks_done)
        app.router.add_post("/api/projects/{id}/tasks/{card}/move", api_move_task)
        app.router.add_delete("/api/projects/{id}/tasks/{card}", api_delete_task)
        app.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", api_update_task)
        # Card Queue: batch-запуск нескольких карточек
        app.router.add_post("/api/projects/{id}/cards/run-batch", api_run_batch)
        # F1: сайдкар результата карточки
        app.router.add_get("/api/projects/{id}/tasks/{card}/run", api_card_run)
        # C2-gate: применить / отменить worktree-карточку; quality gate (check)
        app.router.add_post("/api/projects/{id}/tasks/{card}/apply", api_card_apply)
        app.router.add_post("/api/projects/{id}/tasks/{card}/discard", api_card_discard)
        app.router.add_post("/api/projects/{id}/tasks/{card}/check", api_card_check)
        # C1: SSE-чат по проекту
        app.router.add_post("/api/projects/{id}/chat", api_project_chat)
        # C1-stop: прерывание текущего прогона агента
        app.router.add_post("/api/projects/{id}/chat/stop", api_project_chat_stop)
        app.router.add_get("/api/projects/{id}/running", api_project_running)
        # Скиллы агента: глобальные (~/.claude/skills/) + проектные (<cwd>/.claude/skills/)
        app.router.add_get("/api/projects/{id}/skills", api_project_skills)
        # Сканер инцидентов: ручной запуск + счётчик активных err-карточек
        app.router.add_post("/api/projects/{id}/scan-errors", api_project_scan_errors)
        app.router.add_get("/api/projects/{id}/incidents", api_project_incidents)
        app.router.add_post("/api/projects/{id}/incident", api_project_incident)
        # Самолечение (Spec 010): тумблер включения per-project
        app.router.add_post("/api/projects/{id}/self-heal", api_project_self_heal_toggle)
        app.router.add_post("/api/projects/{id}/notify-on-error", api_project_notify_toggle)
        # Activity-stream: живой поток событий шины (карточки, внешние прогоны)
        app.router.add_get("/api/projects/{id}/activity-stream", api_project_activity_stream)
        # Timeline: история событий проекта (JSONL-лог шины) + пагинация
        app.router.add_get("/api/projects/{id}/timeline", api_project_timeline)
        # Глобальный поток всех событий (для unread-индикаторов в сайдбаре)
        app.router.add_get("/api/activity-stream", api_activity_stream_all)
        # Git sync — commit (если dirty) + push одной кнопкой
        app.router.add_post("/api/projects/{id}/git/sync", api_project_git_sync)
        # Запуск тестов проекта (автодетект pytest/npm/make)
        app.router.add_post("/api/projects/{id}/test", api_project_test)
        app.router.add_post("/api/projects/{id}/upload", api_project_upload)
        # Смена модели проекта (применяется со следующего запроса)
        app.router.add_post("/api/projects/{id}/model", api_project_set_model)
        # Лимиты подписки (5ч + недельные) — для значка в полосе вкладок
        app.router.add_get("/api/usage", api_usage)
        # Шаблоны промтов (глобальные, data/prompts.json)
        app.router.add_get("/api/prompts", api_prompts_list)
        app.router.add_post("/api/prompts", api_prompt_create)
        app.router.add_delete("/api/prompts/{id}", api_prompt_delete)
        app.router.add_route("PATCH", "/api/prompts/{id}", api_prompt_update)
        # Свободные чаты (без привязки к проекту, cwd=$HOME)
        app.router.add_post("/api/free", api_free_create)
        app.router.add_post("/api/free/{id}/rename", api_free_rename)
        app.router.add_delete("/api/free/{id}", api_free_delete)
        # C2: управление сессиями проекта
        app.router.add_get("/api/projects/{id}/sessions", api_project_sessions)
        app.router.add_post("/api/projects/{id}/sessions/{sid}/label", api_project_session_label)
        app.router.add_post("/api/projects/{id}/session", api_project_set_session)
        app.router.add_get("/api/projects/{id}/session-history", api_project_session_history)
        # Файловый проводник (read-only)
        app.router.add_get("/api/projects/{id}/files", api_project_files)
        app.router.add_get("/api/projects/{id}/file", api_project_file)
        # Глобальный файловый браузер (от $HOME, без привязки к проекту)
        app.router.add_get("/api/global/files", api_global_files)
        app.router.add_get("/api/global/file", api_global_file)
        app.router.add_post("/api/global/file", api_global_file_write)
        # Контекст сессии (read: Фича A)
        app.router.add_get("/api/projects/{id}/session-context", api_project_session_context)
        # Память проекта (read+write: Фича B)
        app.router.add_get("/api/projects/{id}/memory", api_project_memory)
        app.router.add_post("/api/projects/{id}/memory/{name}", api_project_memory_write)
        app.router.add_delete("/api/projects/{id}/memory/{name}", api_project_memory_delete)
        # Секреты проекта (Spec 007): только имена в API, значения — только агенту через env
        app.router.add_get("/api/projects/{id}/secrets", api_project_secrets)
        app.router.add_post("/api/projects/{id}/secrets/{key}", api_project_secrets_set)
        app.router.add_delete("/api/projects/{id}/secrets/{key}", api_project_secrets_delete)
        # Переименование папки проекта (kebab-case slug)
        app.router.add_post("/api/projects/{id}/rename", api_project_rename)
        # Быстрая проверка структуры проекта без агента
        app.router.add_get("/api/projects/{id}/health", api_project_health)
        # Аудит проекта: создаёт карточку + запускает run_engine
        app.router.add_post("/api/projects/{id}/audit", api_project_audit)
        # «🔧 Подтянуть до стандарта» — дополняет существующие файлы шаблонами без перезаписи
        app.router.add_post("/api/projects/{id}/upgrade", api_project_upgrade)

        # Статика — всё остальное (SPA)
        app.router.add_route("*", "/{path_info:.*}", spa_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"[webapp] слушаю 0.0.0.0:{port}")

        # Фоновый сканер инцидентов: log_cmd → карточки в Failed
        _spawn_bg(_error_scanner_loop(ctx))
        print(f"[webapp] сканер инцидентов запущен (интервал {_SCAN_INTERVAL_SEC}с)")
        # Card Queue: backstop-дренажный цикл (restart-resume + TG-interleave)
        _spawn_bg(_queue_drain_loop(ctx))
        print(f"[webapp] queue drain loop запущен (интервал {_QUEUE_DRAIN_INTERVAL_SEC}с)")
    except Exception as e:
        print(f"[webapp] ОШИБКА при запуске: {e}")

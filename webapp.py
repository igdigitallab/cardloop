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
import os
import re
import secrets
import shlex
import time
import traceback as _tb
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, TypedDict

import aiohttp
from aiohttp import web


# ─────────────────────────── activity bus ───────────────────────────
#
# Лёгкая in-process шина событий: dict[session_key -> set[asyncio.Queue]].
# Всё в одном event loop → обычные set/dict, без asyncio.Lock.
# Очередь maxsize=100: переполнена → drop (put_nowait в try/except), продюсер не блокируется.

_bus: dict[str, set[asyncio.Queue]] = {}
# Глобальные подписчики — получают ВСЕ события всех сессий, с инжектированным session_key.
# Используется для общего activity-stream приложения (unread-индикаторы в сайдбаре).
_bus_global: set[asyncio.Queue] = set()


def _bus_subscribe(session_key: str) -> "asyncio.Queue[dict]":
    """Создаёт очередь и регистрирует подписчика на session_key."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
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
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
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
async def auth_middleware(request: web.Request, handler):
    """Защита /api/* — пропускает /api/health и /api/login без cookie."""
    path = request.path
    # Незащищённые эндпоинты
    if path in ("/api/health", "/api/login"):
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

    return {"branch": branch, "dirty": dirty, "unpushed": unpushed}


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

async def api_prompts_list(req: web.Request):
    ctx = req.app["ctx"]
    return web.json_response({"prompts": _load_prompts(ctx)})

async def api_prompt_create(req: web.Request):
    ctx = req.app["ctx"]
    data = await req.json()
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

async def api_prompt_delete(req: web.Request):
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    prompts = [p for p in _load_prompts(ctx) if p.get("id") != pid]
    _save_prompts(ctx, prompts)
    return web.json_response({"ok": True})

async def api_prompt_update(req: web.Request):
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    data = await req.json()
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


def _collect_projects(ctx: dict) -> list[dict]:
    """Дедуп по cwd, собирает список проектов из ctx["topics"].
    Добавляет free-чаты как virtual projects (id=free-<uuid>, tg_thread=сам id)."""
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
        })
    out.sort(key=lambda x: x["name"].lower())

    # Free chats — отдельная секция, сортировка по времени создания
    free = _load_free_chats(ctx)
    free_items = sorted(free.items(), key=lambda kv: kv[1].get("created_at", 0))
    for fid, b in free_items:
        out.append({
            "id": fid,
            "name": b.get("label", fid),
            "cwd": b.get("cwd", "/home/igor"),
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

async def api_health(req: web.Request):
    return web.json_response({"ok": True})


async def api_login(req: web.Request):
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


async def api_logout(req: web.Request):
    resp = web.json_response({"ok": True})
    resp.del_cookie("cops_auth", path="/")
    return resp


async def api_me(req: web.Request):
    return web.json_response({"authed": True})


async def api_projects(req: web.Request):
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


async def api_project_claude_md(req: web.Request):
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


async def api_project_readme(req: web.Request):
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


async def api_project_claude_md_write(req: web.Request):
    """POST /api/projects/{id}/claude-md — перезаписать CLAUDE.md."""
    return await _write_doc(req, lambda cwd: cwd / "CLAUDE.md")


async def api_project_readme_write(req: web.Request):
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


async def api_project_specs(req: web.Request):
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


async def api_project_spec_content(req: web.Request):
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


async def api_project_logs(req: web.Request):
    """GET /api/projects/{id}/logs — runtime logs via log_cmd from topics.json."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    log_cmd: str | None = project.get("log_cmd") or None
    if not log_cmd:
        return web.json_response({"lines": [], "configured": False, "cmd": None})

    try:
        # UI-controlled cmd from topics.json → exec (not shell) to prevent injection
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(log_cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return web.json_response({"error": "log_cmd timed out"}, status=504)

        raw = stdout.decode("utf-8", errors="replace")
        lines = raw.splitlines()
        # last 300 lines, newest first
        tail = lines[-300:] if len(lines) > 300 else lines
        tail.reverse()
        return web.json_response({"lines": tail, "configured": True, "cmd": log_cmd})
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


async def api_project_skills(req: web.Request):
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


async def api_project_activity(req: web.Request):
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


_CARD_ID_RE = re.compile(r"^[a-f0-9-]{4,20}$")


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

    # Сначала Python tracebacks (более структурированные)
    for m in _PY_TRACEBACK_RE.finditer(log_text):
        trace_body = m.group(1)
        exc_type = m.group(2)
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
        h = _hash6(f"{source}|{level}|{_norm_msg(msg)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append({
            "source": source, "type": level, "message": msg[:300],
            "excerpt": msg[:300], "hash": h,
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
_ERR_DESC_RE = re.compile(r"^(source|seen|first|last|excerpt)=(.*)$")


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
    запись (хранится как одна строка с \\n заменёнными на ' / ' для компактности)."""
    lines: list[str] = []
    for key in ("source", "seen", "first", "last"):
        if key in meta:
            lines.append(f"{key}={meta[key]}")
    excerpt = meta.get("excerpt", "")
    if excerpt:
        # Многострочный excerpt сворачиваем в одну строку для description
        compact = excerpt.replace("\n", " / ")[:400]
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


async def _run_log_cmd(log_cmd: str, timeout: float = 10.0) -> str:
    """Запускает log_cmd, возвращает stdout (+ stderr).
    UI-controlled cmd from topics.json → exec (not shell) to prevent injection."""
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
            return ""
        return stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


async def _run_test_cmd(test_cmd: str, cwd: str, timeout: float = 120.0) -> str:
    """Запускает test_cmd в cwd проекта.
    UI-controlled cmd from topics.json → exec (not shell) to prevent injection."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(test_cmd),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return ""
        return stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


async def _scan_project_errors(project: dict) -> list[dict]:
    """Сканирует один проект: log_cmd + test_cmd → list[errors]. БЕЗ записи на диск."""
    errors: list[dict] = []
    log_cmd = project.get("log_cmd")
    test_cmd = project.get("test_cmd")

    if log_cmd:
        log_text = await _run_log_cmd(log_cmd)
        if log_text:
            # Берём только последние ~500 строк — старые ошибки уже учтены прошлыми сканами
            tail = "\n".join(log_text.splitlines()[-500:])
            errors.extend(_parse_log_errors(tail, source="log"))

    if test_cmd:
        test_text = await _run_test_cmd(test_cmd, project["cwd"])
        if test_text:
            errors.extend(_parse_pytest_failures(test_text))
            # Generic ERROR строки из test-output тоже учитываем
            errors.extend(_parse_log_errors(test_text, source="test"))

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


async def api_project_scan_errors(req: web.Request):
    """POST /api/projects/{id}/scan-errors — ручной запуск сканера для одного проекта."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if not project.get("log_cmd") and not project.get("test_cmd"):
        return web.json_response({
            "ok": False, "error": "ни log_cmd, ни test_cmd не настроены в topics.json",
        }, status=400)
    res = await _scan_and_ingest(project, ctx)
    return web.json_response(res)


async def api_project_incidents(req: web.Request):
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


# Фоновая задача: сканер всех проектов каждые SCAN_INTERVAL_SEC секунд.
_SCAN_INTERVAL_SEC = int(os.environ.get("ERROR_SCAN_INTERVAL", "300"))  # 5 мин


async def _error_scanner_loop(ctx: dict):
    """Фоновая задача: периодически сканирует все проекты с log_cmd/test_cmd."""
    # Первый прогон через 30с после старта (дать боту устаканиться)
    await asyncio.sleep(30)
    while True:
        try:
            projects = _collect_projects(ctx)
            for proj in projects:
                if proj.get("is_free"):
                    continue
                if not (proj.get("log_cmd") or proj.get("test_cmd")):
                    continue
                res = await _scan_and_ingest(proj, ctx)
                if res.get("added") or res.get("updated"):
                    print(f"[scanner] {proj['name']}: +{res['added']} new, "
                          f"~{res['updated']} updated (из {res['scanned']} событий)")
        except Exception as e:
            print(f"[scanner] loop error: {e}")
        await asyncio.sleep(_SCAN_INTERVAL_SEC)


def _board_payload(cwd: str) -> dict:
    tp, dp = _tasks_path(cwd), _done_path(cwd)
    _, _, cols = _load_board(cwd)
    columns = [{"key": k, "label": l, "cards": cols[k]} for k, l, _ in BOARD_COLUMNS]
    done_count = 0
    if dp.exists():
        done_count = sum(1 for ln in dp.read_text(encoding="utf-8", errors="replace").splitlines()
                         if _CARD_RE.match(ln))
    return {"columns": columns, "done_count": done_count, "exists": tp.exists()}


async def api_project_tasks(req: web.Request):
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
    return web.json_response(_board_payload(cwd))


async def api_create_task(req: web.Request):
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
    run_for_glasses: object  # callable
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
) -> None:
    """Записывает сайдкар результата карточки в DATA/runs/<card_id>.md."""
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


async def _run_card(ctx: AppCtx, webapp_app, project: dict, card: dict, session_key: str) -> None:
    """Фоновая задача F1: оркестратор — выполняет карточку через run_engine, пишет сайдкар, переносит карточку."""
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
    DATA: Path = ctx["DATA"]

    answer_parts: list[str] = []
    exc_info: str | None = None
    ok = False

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
            async for event in run_engine(
                project_name=name,
                cwd=cwd,
                prompt=prompt,
                session_key=session_key,
                model=model,
                resume_session_id=resume_sid,
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

        # git diff после выполнения
        diff_full, diff_stat = await _git_diff_card(cwd)

        # сайдкар DATA/runs/<card_id>.md
        answer_text = "\n".join(answer_parts).strip() or "(агент завершил без текстового ответа)"
        _write_sidecar(DATA, card_id, name, prompt, answer_text, ok, exc_info, diff_stat, diff_full)

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


_NEW_PROJECT_PROMPT = """🚀 Это новый проект, инициализируется с нуля. Cwd — текущая папка.

Сейчас ничего не делай — СПРОСИ Игоря текстом одним сообщением (в конце ответа):

1. Что за проект, цель?
2. Есть ли уже наработки/код/файлы где-то ещё? (другие папки $HOME, чаты, архивы — точные пути если знает)
3. Какие первые 3-5 задач?
4. Как лучше назвать проект (короткий слаг)?

Дальше — по моим ответам:
- Если указал существующие папки → просканируй (ls/Read), краткая сводка.
- Когда определимся с именем — попроси меня переименовать папку untitled-… вручную (сам не сможешь — kокпит держит её как cwd) ИЛИ просто работай в текущей.
- Затем создай: CLAUDE.md (описание + правила канбана Backlog/In Progress/Review/Failed + как формулировать задачи), TASKS.md (реальные карточки в Backlog), README.md, .gitignore (типовой по стэку), `git init` без коммита.

Веди диалог по шагам, не вали скриптом. Не задавай 10 вопросов разом — 3-5 точечных за раз."""


async def api_move_task(req: web.Request):
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

        # Проверка замка — занят ли проект (TG или другая карточка)
        if ctx["running"].get(session_key) is not None:
            return web.json_response(
                {"error": "проект занят (TG или другая карточка)"},
                status=409,
            )

        # Резервируем слот СИНХРОННО до первого await (против гонки)
        ctx["running"][session_key] = True

        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            card = _pop_card(cols, card_id)
            if card is None:
                ctx["running"].pop(session_key, None)
                return web.json_response({"error": "card not found"}, status=404)
            cols["in_progress"].append(card)
            _save_board(cwd, name, preamble, cols)

        # Запускаем фоновую задачу (не ждём завершения)
        asyncio.create_task(_run_card(ctx, req.app, project, card, session_key))

        return web.json_response(_board_payload(cwd))

    # ── Обычный перенос (backlog / review / failed / done) ──
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        card = _pop_card(cols, card_id)
        if card is None:
            return web.json_response({"error": "card not found"}, status=404)

        if to == "done":
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
    return web.json_response(_board_payload(cwd))


async def api_delete_task(req: web.Request):
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    cwd, name = project["cwd"], project["name"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        if _pop_card(cols, card_id) is None:
            return web.json_response({"error": "card not found"}, status=404)
        _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload(cwd))


async def api_update_task(req: web.Request):
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


async def api_tasks_done(req: web.Request):
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
                # Heartbeat — держим соединение живым через туннель (Cloudflare / nginx)
                await resp.write(b": ping\n\n")
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


# ─────────────────────────── свободные чаты (без привязки к проекту) ───────────────────────────
#
# Free-чат — виртуальный «проект» с cwd=$HOME, без git, без TG-привязки.
# Каждый клик «новый свободный» создаёт отдельную вкладку со своим session_id.
# Хранятся в data/free_chats.json: {free-<uuid>: {label, cwd, model, created_at}}.

import uuid as _uuid

_FREE_DEFAULT_CWD = "/home/igor"


async def api_free_create(req: web.Request):
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        body = {}
    cwd = (body.get("cwd") or _FREE_DEFAULT_CWD).rstrip("/")
    model = (body.get("model") or ctx.get("DEFAULT_MODEL", "sonnet")).strip().lower()
    if model not in _ALLOWED_MODELS:
        model = ctx.get("DEFAULT_MODEL", "sonnet")

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


async def api_free_rename(req: web.Request):
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


async def api_free_delete(req: web.Request):
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
_usage_lock = asyncio.Lock()
_USAGE_TTL = 60.0


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


async def api_usage(req: web.Request):
    ctx = req.app["ctx"]
    now = time.time()
    async with _usage_lock:
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


async def api_project_set_model(req: web.Request):
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

async def api_project_upload(req: web.Request):
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


async def api_project_git_sync(req: web.Request):
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
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


async def api_project_test(req: web.Request):
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


async def api_project_files(req: web.Request):
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


async def api_global_files(req: web.Request):
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


async def api_global_file_write(req: web.Request):
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
    data = await req.json()
    content = data.get("content", "")
    try:
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        return web.json_response({"error": f"write error: {e}"}, status=500)
    return web.json_response({"ok": True, "path": rel})


async def api_card_run(req: web.Request):
    """GET /api/projects/{id}/tasks/{card}/run — сайдкар из DATA/runs/<card>.md (404-safe)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    DATA: Path = ctx["DATA"]
    sidecar = DATA / "runs" / f"{card_id}.md"
    if sidecar.exists():
        content = sidecar.read_text(encoding="utf-8", errors="replace")
        return web.json_response({"content": content, "exists": True})
    return web.json_response({"content": "", "exists": False})


# ─────────────────────────── C2: сессии проекта ───────────────────────────

def _sdk_sessions_dir(cwd: str) -> Path:
    """Папка SDK с .jsonl-сессиями для данного cwd."""
    return Path.home() / ".claude" / "projects" / cwd.replace("/", "-")


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


async def api_project_sessions(req: web.Request):
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
            import datetime
            last_used = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat()
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


async def api_project_session_label(req: web.Request):
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


async def api_project_set_session(req: web.Request):
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


async def api_project_session_history(req: web.Request):
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

async def api_project_chat(req: web.Request):
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
        async for event in run_engine(
            project_name=name,
            cwd=cwd,
            prompt=prompt,
            session_key=session_key,
            model=model,
            resume_session_id=resume_sid,
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

async def api_project_chat_stop(req: web.Request):
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


async def api_project_running(req: web.Request):
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


async def api_project_session_context(req: web.Request):
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


async def api_project_memory(req: web.Request):
    """GET /api/projects/{id}/memory
    Возвращает {files:[{name, content}], exists} из ~/.claude/projects/<cwd>/memory/*.md.
    MEMORY.md — первым в списке (индекс)."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    mem_dir = _sdk_sessions_dir(project["cwd"]) / "memory"
    if not mem_dir.is_dir():
        return web.json_response({"files": [], "exists": False})

    files: list[dict] = []
    try:
        md_files = sorted(mem_dir.glob("*.md"), key=lambda p: p.name)
        # MEMORY.md первым
        md_files_sorted = sorted(
            md_files,
            key=lambda p: (0 if p.name == "MEMORY.md" else 1, p.name),
        )
        for f in md_files_sorted:
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

    return web.json_response({"files": files, "exists": True})


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


async def api_new_project(req: web.Request):
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
        import shutil
        shutil.rmtree(str(cwd), ignore_errors=True)
        return web.json_response({"error": f"ошибка записи шаблонов: {e}"}, status=500)

    # Регистрируем в topics.json — session_key из GROUP_CHAT_ID (как неизвестный топик 0)
    group_chat_id = ctx.get("GROUP_CHAT_ID") or 0
    session_key = f"{group_chat_id}:{ts}"
    ctx["topics"][session_key] = {
        "project": display_name,
        "cwd": str(cwd),
        "model": ctx.get("DEFAULT_MODEL", "sonnet"),
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
            "model": ctx.get("DEFAULT_MODEL", "sonnet"),
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
    asyncio.create_task(
        _run_card(ctx, req.app, project, init_card, session_key)
    )

    return web.json_response({
        "id": pid,
        "cwd": str(cwd),
        "name": display_name,
        "session_key": session_key,
        "started": True,
    })


async def api_project_rename(req: web.Request):
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
        import shutil as _shutil
        _shutil.move(str(old_cwd), str(new_cwd))
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

    return web.json_response({
        "ok": True,
        "new_id": new_cwd.name,
        "new_cwd": str(new_cwd),
        "new_name": slug,
    })


async def api_project_health(req: web.Request):
    """GET /api/projects/{id}/health — быстрая проверка структуры проекта без агента."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    cwd = Path(project["cwd"])

    def _check(key: str, label: str, condition: bool, hint: str | None) -> dict:
        return {"key": key, "label": label, "ok": condition, "hint": hint if not condition else None}

    items: list[dict] = []

    # 1. CLAUDE.md существует
    claude_md = cwd / "CLAUDE.md"
    has_claude_md = claude_md.is_file()
    items.append(_check("claude_md", "CLAUDE.md", has_claude_md, "Создай CLAUDE.md с описанием проекта"))

    # 2. CLAUDE.md содержит раздел «Правила работы в кокпите»
    cockpit_rules = False
    if has_claude_md:
        try:
            cockpit_rules = "Правила работы в кокпите" in claude_md.read_text(encoding="utf-8", errors="replace")
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

    # 6. git init (папка .git существует)
    has_git = (cwd / ".git").exists()
    items.append(_check("git_init", "git init", has_git, "Запусти git init в папке проекта"))

    score = sum(1 for item in items if item["ok"])
    total = len(items)
    if score == total:
        color = "green"
    elif score >= total / 2:
        color = "yellow"
    else:
        color = "red"

    return web.json_response({"items": items, "score": score, "total": total, "color": color})


async def api_project_audit(req: web.Request):
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
    asyncio.create_task(_run_card(ctx, req.app, project, audit_card, session_key))

    return web.json_response({"ok": True, "card_id": audit_card["id"], "started": True})


_UPGRADE_PROMPT_TPL = """🔧 Подтянуть проект «{name}» до стандарта кокпита.

ВАЖНО: НЕ переписывай существующее содержимое CLAUDE.md/TASKS.md/README.md/.gitignore — только ДОПОЛНЯЙ недостающее. Если файла нет — создай из шаблона.

Эталоны лежат в `/home/igor/claude-ops-bot/templates/`:
- `CLAUDE.md.tpl` — образец структуры, **обязательно** содержит секцию «Правила работы в кокпите» — её скопируй в CLAUDE.md проекта (если ещё нет), переменные `{{{{name}}}}` замени на актуальное имя.
- `TASKS.md.tpl` — преамбула формата карточек. Если в текущем TASKS.md нет преамбулы с фразой «Формат карточки» — добавь её ПЕРЕД первой `##` колонкой.
- `README.md.tpl` — если README отсутствует, создай минимальный.
- `.gitignore.tpl` — если в текущем нет `.env` — добавь раздел Secrets.

Шаги:
1. Прочитай `CLAUDE.md`, `TASKS.md`, `README.md`, `.gitignore` (если есть) в текущем cwd.
2. Прочитай шаблоны в `/home/igor/claude-ops-bot/templates/*.tpl`.
3. Для каждого недостающего блока — добавь его, сохранив весь существующий контент.
4. НЕ ТРОГАЙ карточки в TASKS.md — только преамбулу выше первой `##`.
5. В конце — короткое резюме в чате: «Добавил/обновил: A, B, C; не трогал: X, Y».
"""


async def api_project_upgrade(req: web.Request):
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
    prompt = _UPGRADE_PROMPT_TPL.format(name=name)

    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        cols["in_progress"].append(card)
        _save_board(cwd, name, preamble, cols)

    if run_engine is None:
        return web.json_response({"ok": True, "card_id": card["id"], "started": False})

    ctx["running"][session_key] = True
    card["text"] = prompt
    asyncio.create_task(_run_card(ctx, req.app, project, card, session_key))
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
    port = ctx["port"]
    try:
        # Деривируем токен один раз при старте (scrypt медленный — не на каждый запрос)
        ctx["_auth_token"] = _derive_token(ctx["password"])

        app = web.Application(middlewares=[auth_middleware], client_max_size=20 * 1024 * 1024)
        app["ctx"] = ctx

        # F1: сохраняем ссылку на PTB-приложение для пинга в TG из _run_card
        app["ptb_app"] = ptb_app
        # Также кладём в ctx для доступа из _run_card через ctx["ptb_app"]
        ctx["ptb_app"] = ptb_app

        # API-роуты
        app.router.add_get("/api/health", api_health)
        app.router.add_post("/api/login", api_login)
        app.router.add_post("/api/logout", api_logout)
        app.router.add_get("/api/me", api_me)
        app.router.add_get("/api/projects", api_projects)
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
        # F1: сайдкар результата карточки
        app.router.add_get("/api/projects/{id}/tasks/{card}/run", api_card_run)
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
        # Activity-stream: живой поток событий шины (карточки, внешние прогоны)
        app.router.add_get("/api/projects/{id}/activity-stream", api_project_activity_stream)
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
        # Память проекта (read: Фича B)
        app.router.add_get("/api/projects/{id}/memory", api_project_memory)
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

        # Фоновый сканер инцидентов: log_cmd/test_cmd → карточки в Failed
        asyncio.create_task(_error_scanner_loop(ctx))
        print(f"[webapp] сканер инцидентов запущен (интервал {_SCAN_INTERVAL_SEC}с)")
    except Exception as e:
        print(f"[webapp] ОШИБКА при запуске: {e}")

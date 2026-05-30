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
import time
import traceback as _tb
from datetime import datetime
from pathlib import Path

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


# ─────────────────────────── auth ───────────────────────────

def _make_token(password: str) -> str:
    """Хэш для cookie cops_auth."""
    return hashlib.sha256((password + "cops").encode()).hexdigest()


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
        expected = _make_token(password)
        token = request.cookies.get("cops_auth", "")
        if token != expected:
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
    try:
        body = await req.json()
        password = body.get("password", "")
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if password != ctx["password"]:
        return web.json_response({"error": "bad password"}, status=401)
    token = _make_token(password)
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        "cops_auth", token,
        httponly=True,
        path="/",
        max_age=2592000,  # 30 дней
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

    async def enrich(p: dict) -> dict:
        # Для свободных чатов git-проверка бессмысленна (cwd обычно $HOME, не репо проекта)
        if p.get("is_free"):
            return {**p, "health": {"git": None}}
        try:
            git = await _git_info(p["cwd"])
        except Exception:
            git = None
        return {**p, "health": {"git": git}}

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

_CARD_RE = re.compile(r"^\s*[-*]\s*\[(.)\]\s*(.*)$")
_MARKER_RE = re.compile(r"\s*<!--\s*ops:([0-9a-fA-F]+)\s*-->\s*$")


def _tasks_path(cwd: str) -> Path:
    return Path(cwd) / "TASKS.md"


def _done_path(cwd: str) -> Path:
    return Path(cwd) / "DONE.md"


def _new_card_id() -> str:
    return secrets.token_hex(3)


def _parse_tasks(text: str):
    """(preamble, cols) — preamble = всё до первого распознанного '## <Колонка>'.
    Строки-некарточки внутри секций отбрасываются при перезаписи (файл наш, канонизируем)."""
    cols = {key: [] for key, _, _ in BOARD_COLUMNS}
    preamble_lines: list[str] = []
    cur = None
    seen_header = False
    for line in text.splitlines():
        h = line.strip()
        if h.startswith("##"):
            name = h.lstrip("#").strip().lower()
            cur = _LABEL_TO_COL.get(name)  # None для незнакомых секций
            if cur is not None:
                seen_header = True
            elif not seen_header:
                preamble_lines.append(line)
            continue
        m = _CARD_RE.match(line)
        if m and cur is not None:
            rest = m.group(2)
            mk = _MARKER_RE.search(rest)
            if mk:
                cid, cardtext = mk.group(1), rest[: mk.start()].rstrip()
            else:
                cid, cardtext = _new_card_id(), rest.rstrip()
            if cardtext:
                cols[cur].append({"id": cid, "text": cardtext})
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
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _load_board(cwd: str):
    tp = _tasks_path(cwd)
    raw = tp.read_text(encoding="utf-8") if tp.exists() else ""
    preamble, cols = _parse_tasks(raw)
    return raw, preamble, cols


def _save_board(cwd: str, name: str, preamble: str, cols: dict) -> None:
    _tasks_path(cwd).write_text(_serialize_tasks(preamble, cols, name), encoding="utf-8")


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
    # Нормализация: дочиняем недостающие ops-маркеры, если файл уже есть и изменился
    raw, preamble, cols = _load_board(cwd)
    if _tasks_path(cwd).exists():
        canon = _serialize_tasks(preamble, cols, name)
        if canon != raw:
            _tasks_path(cwd).write_text(canon, encoding="utf-8")
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
    cwd, name = project["cwd"], project["name"]
    _, preamble, cols = _load_board(cwd)
    if column not in cols:
        column = "backlog"
    cols[column].insert(0, {"id": _new_card_id(), "text": text})
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


async def _run_card(ctx: dict, webapp_app, project: dict, card: dict, session_key: str) -> None:
    """Фоновая задача F1: выполняет карточку через run_engine, пишет сайдкар, переносит карточку."""
    run_engine = ctx.get("run_engine")
    cwd = project["cwd"]
    name = project["name"]
    model = project.get("model", ctx.get("DEFAULT_MODEL", "sonnet"))
    prompt = card["text"]
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

        # сайдкар DATA/runs/<card_id>.md (защищён — его падение НЕ должно держать замок)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        answer_text = "\n".join(answer_parts).strip() or "(агент завершил без текстового ответа)"
        outcome = "ok" if ok else "fail"
        try:
            runs_dir = DATA / "runs"
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

        # перенос карточки (перезагружаем доску — могла измениться пока агент работал)
        try:
            _, preamble, cols = _load_board(cwd)
            moved = _pop_card(cols, card_id)
            if moved is None:
                moved = card  # карточки уже нет в in_progress, но добавим в целевую колонку
            target_col = "review" if ok else "failed"
            cols[target_col].append(moved)
            _save_board(cwd, name, preamble, cols)
        except Exception as e:
            print(f"[_run_card] ошибка переноса карточки {card_id}: {e}")

        # TG-пинг (некритичен — оборачиваем в try/except)
        try:
            ptb = ctx.get("ptb_app")
            if ptb is not None:
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

    finally:
        # Публикуем завершение прогона в шину (ПЕРЕД снятием замка)
        _bus_publish(session_key, {
            "kind": "run_end",
            "outcome": "ok" if ok else "fail",
            "run_id": card_id,
        })
        # замок снимается ГАРАНТИРОВАННО, даже если запись сайдкара/перенос упали
        ctx["running"].pop(session_key, None)


async def api_move_task(req: web.Request):
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
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

        _, preamble, cols = _load_board(cwd)
        card = _pop_card(cols, card_id)
        if card is None:
            # карточка не найдена — снимаем резерв и возвращаем 404
            ctx["running"].pop(session_key, None)
            return web.json_response({"error": "card not found"}, status=404)

        cols["in_progress"].append(card)
        _save_board(cwd, name, preamble, cols)

        # Запускаем фоновую задачу (не ждём завершения)
        asyncio.create_task(_run_card(ctx, req.app, project, card, session_key))

        return web.json_response(_board_payload(cwd))

    # ── Обычный перенос (backlog / review / failed / done) ──
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
        # неизвестная колонка — вернуть карточку на место (в backlog) и 400
        cols["backlog"].append(card)
        _save_board(cwd, name, preamble, cols)
        return web.json_response({"error": "unknown column"}, status=400)
    return web.json_response(_board_payload(cwd))


async def api_delete_task(req: web.Request):
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd, name = project["cwd"], project["name"]
    _, preamble, cols = _load_board(cwd)
    if _pop_card(cols, req.match_info["card"]) is None:
        return web.json_response({"error": "card not found"}, status=404)
    _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload(cwd))


async def api_update_task(req: web.Request):
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
    cwd, name = project["cwd"], project["name"]
    _, preamble, cols = _load_board(cwd)
    card_id = req.match_info["card"]
    found = False
    for col_cards in cols.values():
        for card in col_cards:
            if card["id"] == card_id:
                card["text"] = text
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
# GET /api/projects/{id}/activity-stream
# Живой поток событий шины для данного проекта (session_key = tg_thread).
# Клиент держит соединение; при разрыве finally гарантирует отписку.

async def api_project_activity_stream(req: web.Request):
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    session_key = project["tg_thread"]

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)

    q = _bus_subscribe(session_key)
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
        # ГАРАНТИРОВАННАЯ отписка — иначе утечка очередей
        _bus_unsubscribe(session_key, q)

    return resp


# ─────────────────────────── GLOBAL activity-stream SSE ───────────────────────────
#
# GET /api/activity-stream  — единый поток ВСЕХ событий шины (для unread-индикаторов в сайдбаре).
# Каждое событие приходит с инжектированным session_key, фронт мапит его на проект.

async def api_activity_stream_all(req: web.Request):
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)

    q = _bus_subscribe_global()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=25.0)
                payload = json.dumps(event, ensure_ascii=False)
                await resp.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
            except (ConnectionResetError, ConnectionAbortedError):
                break
            except asyncio.CancelledError:
                break
            except Exception:
                break
    finally:
        _bus_unsubscribe_global(q)

    return resp


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


async def api_project_file(req: web.Request):
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

    # Проверяем имя на секреты
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)

    # Запрещаем читать внутри исключённых директорий (.git/venv/node_modules…)
    try:
        rel_parts = target.relative_to(cwd_resolved).parts
        if any(part in _FS_EXCLUDE_DIRS for part in rel_parts):
            return web.json_response({"error": "access denied"}, status=403)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    if not target.exists() or not target.is_file():
        return web.json_response({"error": "not a file"}, status=404)

    # Размер
    try:
        size = target.stat().st_size
    except Exception:
        return web.json_response({"error": "stat failed"}, status=500)

    MAX_SIZE = 1 * 1024 * 1024  # 1 МБ
    if size > MAX_SIZE:
        return web.json_response({"error": "файл слишком большой", "size": size})

    # Бинарный файл: проверяем первые 8 КБ на нулевые байты
    try:
        with open(target, "rb") as f:
            head = f.read(8192)
        if b"\x00" in head:
            return web.json_response({"error": "бинарный файл", "size": size})
    except Exception:
        return web.json_response({"error": "read failed"}, status=500)

    # Читаем текст
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return web.json_response({"error": f"read error: {e}"}, status=500)

    # Расширение без точки
    lang = target.suffix.lstrip(".") if target.suffix else ""

    # rel для ответа (относительно cwd)
    try:
        rel_norm = str(target.relative_to(cwd_resolved))
    except ValueError:
        rel_norm = rel

    return web.json_response({"path": rel_norm, "content": content, "lang": lang, "size": size})


async def api_card_run(req: web.Request):
    """GET /api/projects/{id}/tasks/{card}/run — сайдкар из DATA/runs/<card>.md (404-safe)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
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
        app.router.add_get("/api/projects/{id}/claude-md", api_project_claude_md)
        app.router.add_get("/api/projects/{id}/readme", api_project_readme)
        app.router.add_get("/api/projects/{id}/specs", api_project_specs)
        app.router.add_get("/api/projects/{id}/specs/{name}", api_project_spec_content)
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
        # Activity-stream: живой поток событий шины (карточки, внешние прогоны)
        app.router.add_get("/api/projects/{id}/activity-stream", api_project_activity_stream)
        # Глобальный поток всех событий (для unread-индикаторов в сайдбаре)
        app.router.add_get("/api/activity-stream", api_activity_stream_all)
        # Git sync — commit (если dirty) + push одной кнопкой
        app.router.add_post("/api/projects/{id}/git/sync", api_project_git_sync)
        app.router.add_post("/api/projects/{id}/upload", api_project_upload)
        # Смена модели проекта (применяется со следующего запроса)
        app.router.add_post("/api/projects/{id}/model", api_project_set_model)
        # Лимиты подписки (5ч + недельные) — для значка в полосе вкладок
        app.router.add_get("/api/usage", api_usage)
        # Свободные чаты (без привязки к проекту, cwd=$HOME)
        app.router.add_post("/api/free", api_free_create)
        app.router.add_post("/api/free/{id}/rename", api_free_rename)
        app.router.add_delete("/api/free/{id}", api_free_delete)
        # C2: управление сессиями проекта
        app.router.add_get("/api/projects/{id}/sessions", api_project_sessions)
        app.router.add_post("/api/projects/{id}/session", api_project_set_session)
        app.router.add_get("/api/projects/{id}/session-history", api_project_session_history)
        # Файловый проводник (read-only)
        app.router.add_get("/api/projects/{id}/files", api_project_files)
        app.router.add_get("/api/projects/{id}/file", api_project_file)
        # Контекст сессии (read: Фича A)
        app.router.add_get("/api/projects/{id}/session-context", api_project_session_context)
        # Память проекта (read: Фича B)
        app.router.add_get("/api/projects/{id}/memory", api_project_memory)

        # Статика — всё остальное (SPA)
        app.router.add_route("*", "/{path_info:.*}", spa_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"[webapp] слушаю 0.0.0.0:{port}")
    except Exception as e:
        print(f"[webapp] ОШИБКА при запуске: {e}")

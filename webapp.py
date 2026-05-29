"""
webapp.py — браузерный кокпит Claude-Ops-Bot.

Поднимается в том же процессе/loop, что и PTB-бот.
Все объекты состояния передаются через ctx — мутации видны боту.
НЕ импортирует bot.py напрямую (повторный импорт создаст второй экземпляр).
"""

import asyncio
import glob
import hashlib
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import web


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


def _collect_projects(ctx: dict) -> list[dict]:
    """Дедуп по cwd, собирает список проектов из ctx["topics"]."""
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
        })
    out.sort(key=lambda x: x["name"].lower())
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


async def api_project_specs(req: web.Request):
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    specs_dir = _find_vault_specs_dir(ctx, project["name"], project["cwd"])
    specs = []
    if specs_dir is not None:
        try:
            for f in sorted(specs_dir.glob("specs/*.md")):
                specs.append({"name": f.name, "path": str(f)})
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

    specs_dir = _find_vault_specs_dir(ctx, project["name"], project["cwd"])
    if specs_dir is None:
        return web.json_response({"error": "specs dir not found"}, status=404)

    spec_path = specs_dir / "specs" / spec_name
    try:
        # Нормализуем и проверяем, что файл внутри specs_dir
        resolved = spec_path.resolve()
        expected_parent = (specs_dir / "specs").resolve()
        if not str(resolved).startswith(str(expected_parent)):
            return web.json_response({"error": "path traversal denied"}, status=400)
        content = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except Exception as e:
        return web.json_response({"error": f"read error: {e}"}, status=500)

    return web.json_response({"name": spec_name, "content": content})


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
    cols[column].append({"id": _new_card_id(), "text": text})
    _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload(cwd))


def _pop_card(cols: dict, card_id: str):
    for k in cols:
        for i, c in enumerate(cols[k]):
            if c["id"] == card_id:
                return cols[k].pop(i)
    return None


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


async def api_tasks_done(req: web.Request):
    """Содержимое архива DONE.md — грузится только по запросу (сессии его не читают)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    dp = _done_path(project["cwd"])
    content = dp.read_text(encoding="utf-8", errors="replace") if dp.exists() else ""
    return web.json_response({"content": content, "exists": dp.exists()})


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
        app = web.Application(middlewares=[auth_middleware], client_max_size=4 * 1024 * 1024)
        app["ctx"] = ctx

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

        # Статика — всё остальное (SPA)
        app.router.add_route("*", "/{path_info:.*}", spa_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"[webapp] слушаю 0.0.0.0:{port}")
    except Exception as e:
        print(f"[webapp] ОШИБКА при запуске: {e}")

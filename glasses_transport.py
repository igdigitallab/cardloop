"""
glasses_transport.py — HTTP-транспорт для очков Even G2.

Вынесен из bot.py в отдельный модуль для изоляции и переиспользования.
Контекст (состояние) получает через ctx-dict, как webapp.py — двойного импорта нет.

⚠️ СТАТУС: заглушен 2026-05-28. Включить обратно:
  1. Раскомментить GLASSES_TOKEN в .env
  2. Восстановить CF tunnel ingress на pve + CNAME в Cloudflare
  3. Рестарт бота
Подробнее: ~/vault/01-Projects/even-g2/specs/claude-glasses.md
"""

import traceback

from aiohttp import web


# ─────────────────────────── auth ───────────────────────────

def _check_auth(req: web.Request, token: str) -> bool:
    if not token:
        return False  # без токена — не пускаем
    return req.headers.get("Authorization", "") == f"Bearer {token}"


# ─────────────────────────── HTTP handlers ───────────────────────────

async def http_health(_req: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "claude-ops-bot"})


async def http_projects(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    if not _check_auth(req, ctx["GLASSES_TOKEN"]):
        return web.json_response({"error": "unauthorized"}, status=401)
    seen, out = set(), []
    for b in ctx["topics"].values():
        if b["cwd"] in seen:
            continue
        seen.add(b["cwd"])
        out.append({"project": b["project"], "cwd": b["cwd"]})
    out.sort(key=lambda x: x["project"].lower())
    return web.json_response({"projects": out})


async def http_run(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    if not _check_auth(req, ctx["GLASSES_TOKEN"]):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await req.json()
        project = (body.get("project") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        if not project or not prompt:
            raise ValueError("project и prompt обязательны")
    except Exception as e:
        return web.json_response({"error": f"bad request: {e}"}, status=400)
    try:
        result = await ctx["run_for_glasses"](project, prompt)
        return web.json_response(result)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    except RuntimeError as e:
        return web.json_response({"error": str(e)}, status=409)
    except Exception as e:
        traceback.print_exc()
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)


async def http_reset(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    if not _check_auth(req, ctx["GLASSES_TOKEN"]):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await req.json()
        project = (body.get("project") or "").strip()
    except Exception as e:
        return web.json_response({"error": f"bad request: {e}"}, status=400)
    r = ctx["resolve_project"](project)
    if not r:
        return web.json_response({"error": f"unknown project: {project}"}, status=404)
    key = f"glasses:{r[0]}"
    ctx["sessions"].pop(key, None)
    ctx["save_sessions"]()
    return web.json_response({"ok": True, "project": r[0]})


# ─────────────────────────── CORS middleware ───────────────────────────

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """CORS для очков: WebView плагина (origin file://localhost) делает preflight.
    Без этого WebKit роняет request с 'Load failed'."""
    if request.method == "OPTIONS":
        return web.Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "86400",
        })
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ─────────────────────────── запуск ───────────────────────────

async def start(ctx: dict) -> None:
    """Поднимает aiohttp-сервер для очков. Если GLASSES_TOKEN пуст — отключён (no-op).

    ctx должен содержать:
      GLASSES_TOKEN, GLASSES_PORT, topics, sessions, save_sessions,
      resolve_project, run_for_glasses.
    """
    token = ctx.get("GLASSES_TOKEN", "")
    if not token:
        print("[glasses-http] GLASSES_TOKEN не задан — HTTP-сервер для очков ВЫКЛЮЧЕН")
        return
    port = ctx["GLASSES_PORT"]
    http_app = web.Application(middlewares=[cors_middleware], client_max_size=10 * 1024 * 1024)
    http_app["ctx"] = ctx
    http_app.router.add_get("/healthz", http_health)
    http_app.router.add_get("/projects", http_projects)
    http_app.router.add_post("/run", http_run)
    http_app.router.add_post("/reset", http_reset)
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[glasses-http] слушаю 0.0.0.0:{port}")

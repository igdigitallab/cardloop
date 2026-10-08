#!/usr/bin/env python3
"""gen_route_index — generate docs/API-routes.md, the complete HTTP route index.

Why this exists: docs/API.md is a hand-written guide to the main flows and
covers only about half of the registered routes. This tool walks the REAL
aiohttp router, so the index cannot drift from the code:

    venv/bin/python tools/gen_route_index.py            # rewrite docs/API-routes.md
    venv/bin/python tools/gen_route_index.py --check    # exit 1 if the file is stale
    venv/bin/python tools/gen_route_index.py --stdout   # print instead of writing

How the app is built (no server, no sockets, no data/ access):
  * `webapp.start()` registers every core route inline, so its route block
    (from `app = web.Application(...)` to the SPA catch-all) is extracted from
    the SOURCE with `ast` and executed against a real `web.Application`.
    Only a whitelist of statement shapes is accepted in that block; anything
    else (a loop, an `if`, a computed path) aborts with a message instead of
    producing a silently incomplete index.
  * Feature packages (`from features.<pkg> import register` inside `start()`)
    are discovered from the same source and their `routes.add_routes(app)` is
    called directly, bypassing the enable gate and the background-loop start
    of `register()`. They are marked in the table because a disabled feature
    does not serve its routes.
  * "Auth" is not hard-coded: each route is run through the real
    `webapp.auth_middleware` with a cookie-less request, and the answer
    (handler reached vs. 401) is what the table says.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "API-routes.md"

_METHOD_RANK = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
_DESC_MAX = 150

# Statement shapes allowed in the route block of webapp.start(). Anything else is
# refused so a new registration style cannot make the index silently incomplete.
_ALLOWED_ASSIGN_TARGETS = ("app", "app['ctx']", "ctx['_aiohttp_app']")  # ast.unparse quoting


def _die(msg: str) -> "None":
    sys.exit(f"gen_route_index: {msg}")


def _find_route_block(tree: ast.Module) -> "tuple[list[ast.stmt], list[str]]":
    """Return (statements to execute, feature packages) from webapp.start()."""
    start = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "start"), None)
    if start is None:
        _die("webapp.start() not found")
    for node in ast.walk(start):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                    and ast.unparse(stmt.value.func) == "web.Application"):
                begin = i
                break
        else:
            continue
        end = None
        for j in range(begin, len(body)):
            if "spa_handler" in ast.unparse(body[j]):
                end = j
                break
        if end is None:
            _die("the SPA catch-all (spa_handler) closing the route block was not found")
        keep: list[ast.stmt] = []
        features: list[str] = []
        for stmt in body[begin:end + 1]:
            text = ast.unparse(stmt)
            if isinstance(stmt, ast.ImportFrom) and (stmt.module or "").startswith("features."):
                features.append((stmt.module or "").split(".")[1])
                continue
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id.startswith("_register_")):
                continue
            if isinstance(stmt, ast.Assign) and ast.unparse(stmt.targets[0]) in _ALLOWED_ASSIGN_TARGETS:
                keep.append(stmt)
                continue
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and re.match(r"^app\.router\.add_(get|post|put|delete|patch|route)\(", text)):
                keep.append(stmt)
                continue
            _die(f"unsupported statement in the route block of webapp.start() "
                 f"(line {stmt.lineno}): {text[:100]!r}. Extend tools/gen_route_index.py "
                 f"or extract the registration into a function.")
        return keep, features
    _die("`app = web.Application(...)` not found in webapp.start()")
    raise AssertionError  # unreachable


def build_app():
    """Build the full route table without starting anything. Returns (app, feature_of)."""
    sys.path.insert(0, str(ROOT))
    import importlib

    import webapp
    from aiohttp import web

    source = Path(webapp.__file__).read_text(encoding="utf-8")
    keep, features = _find_route_block(ast.parse(source))
    fn = ast.FunctionDef(
        name="_build_routes",
        args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="ctx")], kwonlyargs=[],
                           kw_defaults=[], defaults=[]),
        body=keep + [ast.Return(value=ast.Name(id="app", ctx=ast.Load()))],
        decorator_list=[], returns=None, type_params=[],
    )
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = dict(vars(webapp))
    exec(compile(module, str(webapp.__file__), "exec"), scope)  # noqa: S102 - our own source
    # The ctx is a throwaway: auth_middleware only reads these two keys, and only for
    # routes that are not exempt.
    ctx = {"password": "x", "_auth_token": "route-index-token"}
    app = scope["_build_routes"](ctx)
    assert isinstance(app, web.Application)
    for pkg in features:
        mod = importlib.import_module(f"features.{pkg}.routes")
        mod.add_routes(app)
    return app, webapp, features


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]*\}", "x", template)


async def _needs_cookie(webapp, app, method: str, template: str) -> bool:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    sentinel = web.Response(text="reached")

    async def handler(_req):
        return sentinel

    req = make_mocked_request(method if method != "*" else "GET", _concrete(template), app=app)
    resp = await webapp.auth_middleware(req, handler)
    return resp is not sentinel


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def _guide_descriptions() -> "dict[tuple[str, str], str]":
    """(method, normalised path) -> description, from the hand-written docs/API.md tables.

    Fallback for handlers without a docstring, so the index still says what a route does
    wherever the guide already does.
    """
    out: dict[tuple[str, str], str] = {}
    guide = ROOT / "docs" / "API.md"
    if not guide.exists():
        return out
    for raw in guide.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", raw.strip().strip("|"))]
        if len(cells) < 3:
            continue
        m = re.fullmatch(r"`([A-Z]+)`", cells[0])
        p = re.fullmatch(r"`(/[^`]*)`", cells[1])
        if m and p and cells[2]:
            out.setdefault((m.group(1), _norm(p.group(1))), cells[2].replace("\\|", "|"))
    return out


def _tidy(line: str) -> str:
    """One docstring line -> one safe table cell: truncated, pipes escaped, code spans kept
    balanced, and raw ``<id>`` / ``*`` outside code spans neutralised (they would render as
    an HTML tag or emphasis)."""
    if line[:1].islower():
        line = line[0].upper() + line[1:]
    if len(line) > _DESC_MAX:
        line = line[:_DESC_MAX - 1].rstrip() + "…"
    if line.count("`") % 2:
        line += "`"
    parts = line.split("`")
    for i in range(0, len(parts), 2):  # even index = outside a code span
        parts[i] = parts[i].replace("<", "&lt;").replace(">", "&gt;").replace("*", "\\*")
    return "`".join(parts).replace("|", "\\|")


def _describe(handler) -> str:
    doc = (getattr(handler, "__doc__", None) or "").strip()
    if not doc:
        return ""
    line = doc.splitlines()[0].strip()
    # Many docstrings open with "GET /api/x — ..."; the table already has both columns.
    line = re.sub(r"^(GET|POST|PUT|PATCH|DELETE|WS)\s+/\S*\s*[—–:-]*\s*", "", line)
    return _tidy(line)


def collect(app, webapp) -> "tuple[list[dict], dict]":
    rows: list[dict] = []
    auth_probes: list = []
    guide = _guide_descriptions()
    summary = {"head": 0, "catch_all": [], "options": 0}
    for route in app.router.routes():
        resource = route.resource
        template = resource.canonical if resource is not None else ""
        method = route.method
        if method == "HEAD":
            summary["head"] += 1
            continue
        if method == "OPTIONS":
            summary["options"] += 1
            continue
        if method == "*":
            summary["catch_all"].append(template)
            continue
        handler = route.handler
        module = getattr(handler, "__module__", "") or ""
        feature = module.split(".")[1] if module.startswith("features.") else ""
        rows.append({
            "method": method,
            "path": template,
            "handler": getattr(handler, "__name__", repr(handler)),
            "desc": _describe(handler) or _tidy(guide.get((method, _norm(template)), "")),
            "feature": feature,
            "cookie": None,
        })
        auth_probes.append((method, template))

    async def _probe_all():
        return [await _needs_cookie(webapp, app, m, t) for m, t in auth_probes]

    # A private loop, never installed as the thread's current loop: asyncio.run() would
    # reset the current loop to None, which breaks later get_event_loop() callers when
    # this runs inside a test process.
    loop = asyncio.new_event_loop()
    try:
        answers = loop.run_until_complete(_probe_all())
    finally:
        loop.close()
    for row, needs in zip(rows, answers):
        row["cookie"] = needs
    rows.sort(key=lambda r: (r["path"], _METHOD_RANK.get(r["method"], 9), r["method"]))
    return rows, summary


def render(rows: "list[dict]", summary: dict, features: "list[str]", webapp) -> str:
    n_cookie = sum(1 for r in rows if r["cookie"])
    n_open = len(rows) - n_cookie
    n_paths = len({r["path"] for r in rows})
    lines: list[str] = []
    w = lines.append
    w("<!-- GENERATED FILE - do not edit by hand. -->")
    w("<!-- Regenerate: venv/bin/python tools/gen_route_index.py -->")
    w("")
    w("> Complete HTTP route index, generated from the live aiohttp router. "
      "Guide to the main flows → [API.md](API.md).")
    w("")
    w("# Cardloop HTTP route index")
    w("")
    w("**This file is generated** by `tools/gen_route_index.py` from the routes the cockpit "
      "actually registers; `tests/test_route_index.py` fails when it is stale. "
      "After adding, removing or renaming a route, regenerate it:")
    w("")
    w("```bash")
    w("venv/bin/python tools/gen_route_index.py")
    w("```")
    w("")
    w(f"{len(rows)} routes on {n_paths} paths ({n_cookie} need the session cookie, "
      f"{n_open} do not). Methods and paths are exact; the description is the first line of the "
      "handler's docstring, or the matching [API.md](API.md) row when the handler has no docstring "
      "(a dash means neither has one). Request and response shapes for "
      "the main flows are in [API.md](API.md); for the rest, the handler named in the "
      "table is the reference.")
    w("")
    w("## Authentication")
    w("")
    doc = (webapp.auth_middleware.__doc__ or "").strip()
    w("The `Auth` column is computed by running every route through the real "
      "`auth_middleware` with a cookie-less request. `cookie` means the request is answered "
      "`401` unless it carries a valid `cops_auth` cookie (obtained from `POST /api/login`); "
      "`none` means the middleware lets it through. The middleware's own contract:")
    w("")
    w("```text")
    for ln in doc.splitlines():
        w(ln.strip())
    w("```")
    w("")
    open_routes = [r for r in rows if not r["cookie"]]
    w("Answered without the cookie: "
      + ", ".join(f"`{r['method']} {r['path']}`" for r in open_routes)
      + ". Every other route in this table returns `401` without it.")
    w("")
    if features:
        w("## Feature routes")
        w("")
        w("Routes marked `feature: <name>` belong to an optional feature package under "
          "`features/` and exist only while that feature is enabled (its `register()` gate: "
          "Modules panel or env flag). Core routes are always present.")
        w("")
    w("## Routes")
    w("")
    w("| Method | Path | Auth | Handler | Description |")
    w("|--------|------|------|---------|-------------|")
    for r in rows:
        auth = "cookie" if r["cookie"] else "none"
        handler = f"`{r['handler']}`"
        if r["feature"]:
            handler += f" (feature: {r['feature']})"
        w(f"| `{r['method']}` | `{r['path']}` | {auth} | {handler} | {r['desc'] or '—'} |")
    w("")
    w("## Not listed one by one")
    w("")
    w(f"- **HEAD**: aiohttp registers a HEAD twin for every GET route ({summary['head']} of them). "
      "It has the same path, auth and handler as its GET.")
    if summary["options"]:
        w(f"- **OPTIONS**: {summary['options']} auto-registered routes.")
    for t in summary["catch_all"]:
        w(f"- **`{t}` (any method)**: the SPA fallback. It serves the built web UI "
          "(`web/dist`) for every path that no route above matched. It is outside `/api/`, so the "
          "auth middleware does not guard it.")
    w("")
    return "\n".join(lines)


def generate() -> str:
    # Importing webapp prints startup banners (e.g. "[second_opinion] MCP tool enabled");
    # keep them off stdout so `--stdout` output stays clean.
    with contextlib.redirect_stdout(sys.stderr):
        app, webapp, features = build_app()
    rows, summary = collect(app, webapp)
    return render(rows, summary, features, webapp)


def main(argv: "list[str]") -> int:
    text = generate()
    if "--stdout" in argv:
        sys.stdout.write(text)
        return 0
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale. Regenerate: "
                  "venv/bin/python tools/gen_route_index.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

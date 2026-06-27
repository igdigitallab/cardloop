"""
Card 931573: global (home) agent-rules CLAUDE.md — view + edit.

GET/POST /api/global/claude-md. The target path is resolved via the GLOBAL_CLAUDE_MD
env override so the test never touches the real $HOME/CLAUDE.md.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _global_claude_md_path


def _make_ctx(tmp_path):
    password = "testpass"
    ctx = {
        "topics": {}, "sessions": {}, "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ctx


def _make_app(ctx):
    from aiohttp import web
    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/global/claude-md", _webapp.api_global_claude_md)
    app.router.add_post("/api/global/claude-md", _webapp.api_global_claude_md_write)
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def test_path_default_is_home(monkeypatch):
    monkeypatch.delenv("GLOBAL_CLAUDE_MD", raising=False)
    assert _global_claude_md_path() == Path.home() / "CLAUDE.md"


def test_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GLOBAL_CLAUDE_MD", str(tmp_path / "x" / "CLAUDE.md"))
    assert _global_claude_md_path() == tmp_path / "x" / "CLAUDE.md"


async def test_get_missing(aiohttp_client, tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    monkeypatch.setenv("GLOBAL_CLAUDE_MD", str(target))
    ctx = _make_ctx(tmp_path)
    client = await aiohttp_client(_make_app(ctx))
    r = await client.get("/api/global/claude-md", headers=_auth(ctx))
    assert r.status == 200
    body = await r.json()
    assert body["exists"] is False
    assert body["content"] == ""
    assert body["path"] == str(target)


async def test_write_then_read(aiohttp_client, tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    monkeypatch.setenv("GLOBAL_CLAUDE_MD", str(target))
    ctx = _make_ctx(tmp_path)
    client = await aiohttp_client(_make_app(ctx))

    w = await client.post("/api/global/claude-md", headers=_auth(ctx),
                          json={"content": "# Rules\nhello"})
    assert w.status == 200
    assert target.read_text(encoding="utf-8") == "# Rules\nhello"

    r = await client.get("/api/global/claude-md", headers=_auth(ctx))
    body = await r.json()
    assert body["exists"] is True
    assert body["content"] == "# Rules\nhello"


async def test_write_creates_missing_parent(aiohttp_client, tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "CLAUDE.md"
    monkeypatch.setenv("GLOBAL_CLAUDE_MD", str(target))
    ctx = _make_ctx(tmp_path)
    client = await aiohttp_client(_make_app(ctx))
    w = await client.post("/api/global/claude-md", headers=_auth(ctx),
                          json={"content": "x"})
    assert w.status == 200
    assert target.read_text(encoding="utf-8") == "x"


async def test_write_rejects_non_string(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBAL_CLAUDE_MD", str(tmp_path / "CLAUDE.md"))
    ctx = _make_ctx(tmp_path)
    client = await aiohttp_client(_make_app(ctx))
    r = await client.post("/api/global/claude-md", headers=_auth(ctx),
                          json={"content": 123})
    assert r.status == 400


async def test_requires_auth(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBAL_CLAUDE_MD", str(tmp_path / "CLAUDE.md"))
    ctx = _make_ctx(tmp_path)
    client = await aiohttp_client(_make_app(ctx))
    r = await client.get("/api/global/claude-md")
    assert r.status == 401

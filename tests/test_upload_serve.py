"""
Upload + inline-serve tests for the chat composer attachments.

Covers:
- POST /api/projects/{id}/upload → stores file in DATA/inbox, returns {path, url, name, size}
- GET  /api/projects/{id}/upload/{filename} → serves an uploaded image back for inline preview
- traversal guard + unsupported-type (415) on the serve route
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token

# 1x1 transparent PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.fixture
def upload_ctx(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {"1001:42": {"project": "myproject", "cwd": str(pdir), "model": "sonnet"}},
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def upload_app(upload_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = upload_ctx
    app.router.add_post("/api/projects/{id}/upload", _webapp.api_project_upload)
    app.router.add_get("/api/projects/{id}/upload/{filename}", _webapp.api_project_upload_file)
    return app


def _h(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _form():
    from aiohttp import FormData

    form = FormData()
    form.add_field("file", _PNG, filename="shot.png", content_type="image/png")
    return form


async def test_upload_returns_servable_url(aiohttp_client, upload_app, upload_ctx):
    client = await aiohttp_client(upload_app)
    resp = await client.post(
        "/api/projects/myproject/upload",
        data=_form(),
        headers=_h(upload_ctx),
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "shot.png"
    assert data["size"] == len(_PNG)
    # url must be the servable path, path must be the absolute filesystem path
    assert data["url"].startswith("/api/projects/myproject/upload/")
    assert data["path"].endswith(data["url"].rsplit("/", 1)[-1])


async def test_uploaded_image_served_back(aiohttp_client, upload_app, upload_ctx):
    client = await aiohttp_client(upload_app)
    up = await client.post(
        "/api/projects/myproject/upload",
        data=_form(),
        headers=_h(upload_ctx),
    )
    url = (await up.json())["url"]
    resp = await client.get(url, headers=_h(upload_ctx))
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert await resp.read() == _PNG


async def test_serve_rejects_traversal(aiohttp_client, upload_app, upload_ctx):
    client = await aiohttp_client(upload_app)
    resp = await client.get(
        "/api/projects/myproject/upload/..%2f..%2fwebapp.py", headers=_h(upload_ctx)
    )
    assert resp.status == 400


async def test_serve_rejects_unsupported_type(aiohttp_client, upload_app, upload_ctx):
    client = await aiohttp_client(upload_app)
    resp = await client.get(
        "/api/projects/myproject/upload/notes.txt", headers=_h(upload_ctx)
    )
    assert resp.status == 415

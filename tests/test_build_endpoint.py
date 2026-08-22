"""GET /api/build — which frontend bundle the server currently serves.

The cockpit is a long-lived PWA: an open app keeps running the JS it booted with, and workbox
answers navigations from its precache, so the first reload after a deploy still returns the old
shell. The client polls this endpoint, compares the filename with the <script> tag it booted
from, and self-updates — so the endpoint must stay unauthenticated (it is also read from the
login screen and the native shell's own origin) and must follow a rebuild.
"""
import json
from pathlib import Path

import pytest
from aiohttp import web

import webapp as _webapp


def _index_html(bundle: str) -> str:
    return (
        '<!doctype html><html><head>'
        f'<script type="module" crossorigin src="./assets/{bundle}"></script>'
        '<link rel="stylesheet" href="./assets/index-Xyz.css">'
        '</head><body><div id="root"></div></body></html>'
    )


@pytest.fixture
def build_app(tmp_path: Path):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(_index_html("index-AAAA1111.js"), encoding="utf-8")
    app = web.Application()
    app["ctx"] = {"HERE": tmp_path}
    app.router.add_get("/api/build", _webapp.api_build)
    # A fresh module-level cache per test — the endpoint memoises on index.html's mtime.
    _webapp._build_cache["mtime"] = None
    _webapp._build_cache["bundle"] = None
    return app, dist


async def test_build_reports_the_served_bundle(aiohttp_client, build_app):
    app, _dist = build_app
    client = await aiohttp_client(app)
    res = await client.get("/api/build")
    assert res.status == 200
    assert (await res.json())["bundle"] == "index-AAAA1111.js"
    assert res.headers["Cache-Control"] == "no-store", \
        "a cached answer would defeat the whole point of the check"


async def test_build_follows_a_rebuild(aiohttp_client, build_app):
    """The mtime-keyed cache must not pin the old filename after a deploy."""
    app, dist = build_app
    client = await aiohttp_client(app)
    assert (await (await client.get("/api/build")).json())["bundle"] == "index-AAAA1111.js"

    index = dist / "index.html"
    index.write_text(_index_html("index-BBBB2222.js"), encoding="utf-8")
    import os
    st = index.stat()
    os.utime(index, (st.st_atime + 10, st.st_mtime + 10))  # a rebuild bumps mtime

    assert (await (await client.get("/api/build")).json())["bundle"] == "index-BBBB2222.js"


async def test_build_survives_an_unbuilt_frontend(aiohttp_client, tmp_path):
    """No web/dist yet (fresh clone) — report null, never a 500."""
    app = web.Application()
    app["ctx"] = {"HERE": tmp_path}
    app.router.add_get("/api/build", _webapp.api_build)
    _webapp._build_cache["mtime"] = None
    client = await aiohttp_client(app)
    res = await client.get("/api/build")
    assert res.status == 200 and (await res.json())["bundle"] is None


def test_build_is_exempt_from_cookie_auth():
    """Read from the login screen and from the native shell's origin — must not need a cookie."""
    src = Path(_webapp.__file__).read_text(encoding="utf-8")
    assert '"/api/health", "/api/login", "/api/build"' in src, \
        "the auth middleware exempt list must carry /api/build"

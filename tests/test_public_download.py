"""GET /dl/{name} — the one unauthenticated file route.

Everything else the cockpit serves needs the session cookie, which makes a link useless in a
phone's default browser (the case that forced this: an APK download landing on a 401 page).
The exposure has to stay narrow, so these tests pin the boundary, not just the happy path.
"""
from pathlib import Path

import pytest
from aiohttp import web

import webapp as _webapp


@pytest.fixture
def dl_app(tmp_path: Path):
    data = tmp_path / "data"
    (data / "public").mkdir(parents=True)
    (data / "public" / "cardloop-1.3-ab12cd34.apk").write_bytes(b"PK\x03\x04 apk bytes")
    (data / "secret.txt").write_text("session tokens live out here", encoding="utf-8")
    app = web.Application()
    app["ctx"] = {"DATA": data}
    app.router.add_get("/dl/{name}", _webapp.public_download)
    return app, data


async def test_serves_a_published_file_as_a_download(aiohttp_client, dl_app):
    app, _ = dl_app
    client = await aiohttp_client(app)
    res = await client.get("/dl/cardloop-1.3-ab12cd34.apk")
    assert res.status == 200
    assert await res.read() == b"PK\x03\x04 apk bytes"
    assert 'attachment; filename="cardloop-1.3-ab12cd34.apk"' in res.headers["Content-Disposition"], \
        "a browser must save the file, not try to render it"


async def test_unknown_name_is_404(aiohttp_client, dl_app):
    app, _ = dl_app
    client = await aiohttp_client(app)
    assert (await (await aiohttp_client(app)).get("/dl/nope.apk")).status == 404
    assert client is not None


@pytest.mark.parametrize("name", [
    "..%2Fsecret.txt",          # encoded traversal
    "....//secret.txt",
    ".env",                     # a dotfile name the regex must reject (leading dot)
])
async def test_traversal_and_dotfiles_are_refused(aiohttp_client, dl_app, name):
    app, _ = dl_app
    client = await aiohttp_client(app)
    res = await client.get(f"/dl/{name}")
    assert res.status == 404, f"{name} must not resolve"


async def test_symlink_out_of_public_is_refused(aiohttp_client, dl_app):
    """A symlink planted in data/public must not turn the route into read-anything."""
    app, data = dl_app
    link = data / "public" / "leak.txt"
    link.symlink_to(data / "secret.txt")
    client = await aiohttp_client(app)
    res = await client.get("/dl/leak.txt")
    assert res.status == 404
    assert b"session tokens" not in await res.read()


async def test_route_sits_outside_the_api_guard():
    """/dl/ is not under /api/, which is the only prefix auth_middleware checks — assert that
    the route was not accidentally moved under /api/ in a later refactor."""
    src = Path(_webapp.__file__).read_text(encoding="utf-8")
    assert 'add_get("/dl/{name}", public_download)' in src

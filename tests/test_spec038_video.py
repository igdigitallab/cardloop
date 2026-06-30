"""
Tests for spec-038 video extension: media route serves video files with the
correct Content-Type, rejects traversal, and the cockpit-img helper accepts
video extensions.

Pre-existing failures in test_run_card/test_c2_gate/test_context_rotation
(KeyError:'id') are unrelated to this spec and are excluded here.
"""
import sys
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _derive_token, _MEDIA_CONTENT_TYPES


# ─────────────────────────── unit: _MEDIA_CONTENT_TYPES ─────────────────────


def test_media_content_types_includes_video_extensions():
    """The content-type map must include all five video extensions."""
    expected = {
        "mp4":  "video/mp4",
        "webm": "video/webm",
        "mov":  "video/quicktime",
        "ogg":  "video/ogg",
        "ogv":  "video/ogg",
    }
    for ext, mime in expected.items():
        assert _MEDIA_CONTENT_TYPES.get(ext) == mime, (
            f"Expected _MEDIA_CONTENT_TYPES['{ext}'] == '{mime}', "
            f"got {_MEDIA_CONTENT_TYPES.get(ext)!r}"
        )


def test_media_content_types_still_includes_image_extensions():
    """Existing image extensions must be untouched."""
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        assert ext in _MEDIA_CONTENT_TYPES, f"image extension '{ext}' missing"


# ─────────────────────────── fixtures ───────────────────────────────────────


@pytest.fixture
def media_ctx(tmp_path):
    """Minimal ctx with a single project and pre-created media dir."""
    project_dir = tmp_path / "vidproj"
    project_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    password = "testpass"
    ctx = {
        "topics": {
            "1:1": {
                "project": "vidproj",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
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
def media_app(media_ctx):
    from aiohttp import web

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = media_ctx
    app.router.add_get(
        "/api/projects/{id}/media/{filename}",
        _webapp.api_project_media,
    )
    return app


def _auth(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


def _place_file(ctx, filename: str, content: bytes = b"fake-video-data") -> None:
    """Write a file into the media dir for the 'vidproj' project."""
    media_dir = ctx["DATA"] / "chat-media" / "vidproj"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / filename).write_bytes(content)


# ─────────────────────────── route: video content-types ─────────────────────


@pytest.mark.parametrize("ext,expected_ct", [
    ("mp4",  "video/mp4"),
    ("webm", "video/webm"),
    ("mov",  "video/quicktime"),
    ("ogg",  "video/ogg"),
    ("ogv",  "video/ogg"),
])
async def test_media_route_serves_video_with_correct_content_type(
    aiohttp_client, media_app, media_ctx, ext, expected_ct
):
    """GET /media/<file.ext> for video extensions returns 200 with correct Content-Type."""
    filename = f"1000000000_clip.{ext}"
    _place_file(media_ctx, filename)

    client = await aiohttp_client(media_app)
    resp = await client.get(
        f"/api/projects/vidproj/media/{filename}",
        headers=_auth(media_ctx),
    )
    assert resp.status == 200, f"Expected 200 for .{ext}, got {resp.status}"
    ct = resp.headers.get("Content-Type", "")
    assert ct == expected_ct, f"Expected Content-Type '{expected_ct}' for .{ext}, got '{ct}'"


# ─────────────────────────── route: traversal guard ─────────────────────────


@pytest.mark.parametrize("bad_filename", [
    "../etc/passwd",
    "..%2Fetc%2Fpasswd",
    "subdir/clip.mp4",
    "clip\\mp4",
])
async def test_media_route_rejects_traversal_for_video(
    aiohttp_client, media_app, media_ctx, bad_filename
):
    """Path-traversal filenames are rejected (400 or 404) — same guard as images."""
    client = await aiohttp_client(media_app)
    resp = await client.get(
        f"/api/projects/vidproj/media/{bad_filename}",
        headers=_auth(media_ctx),
        allow_redirects=False,
    )
    assert resp.status in (400, 404), (
        f"Expected 400 or 404 for traversal attempt '{bad_filename}', got {resp.status}"
    )


# ──────────── route: arbitrary file → octet-stream download (card 2efd6a) ─────


async def test_media_route_serves_arbitrary_file_as_download(
    aiohttp_client, media_app, media_ctx
):
    """An extension not in the inline-media map (pdf/zip/avi/…) is now served as a download:
    HTTP 200, Content-Type application/octet-stream, Content-Disposition: attachment.
    (Replaces the old 415 behaviour — agents drop arbitrary files via cockpit-file.)"""
    for filename in ("1000000000_report.pdf", "1000000000_bundle.zip", "1000000000_clip.avi"):
        _place_file(media_ctx, filename, content=b"fake-bytes")
        client = await aiohttp_client(media_app)
        resp = await client.get(
            f"/api/projects/vidproj/media/{filename}",
            headers=_auth(media_ctx),
        )
        assert resp.status == 200, f"Expected 200 for {filename}, got {resp.status}"
        assert resp.headers.get("Content-Type") == "application/octet-stream"
        cd = resp.headers.get("Content-Disposition", "")
        assert cd.startswith("attachment;"), f"Expected attachment disposition, got {cd!r}"
        assert filename in cd


async def test_media_route_image_stays_inline_no_attachment(
    aiohttp_client, media_app, media_ctx
):
    """A known image type must still be served inline (no attachment disposition)."""
    filename = "1000000000_shot.png"
    _place_file(media_ctx, filename, content=b"\x89PNG\r\n")
    client = await aiohttp_client(media_app)
    resp = await client.get(
        f"/api/projects/vidproj/media/{filename}",
        headers=_auth(media_ctx),
    )
    assert resp.status == 200
    assert resp.headers.get("Content-Type") == "image/png"
    assert "Content-Disposition" not in resp.headers


# ─────────────────────────── route: unknown project → 404 ────────────────────


async def test_media_route_unknown_project_returns_404(
    aiohttp_client, media_app, media_ctx
):
    """Unknown project id must return 404."""
    client = await aiohttp_client(media_app)
    resp = await client.get(
        "/api/projects/nosuchproject/media/clip.mp4",
        headers=_auth(media_ctx),
    )
    assert resp.status == 404


# ─────────────────────────── cockpit-img helper (shell) ──────────────────────


def _run_cockpit_img(tmp_path: Path, filename: str, caption: str = "test") -> tuple[int, str, str]:
    """Run the cockpit-img shell script and return (returncode, stdout, stderr)."""
    import subprocess
    script = ROOT / "tools" / "cockpit-img"
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "COPS_MEDIA_DIR": str(media_dir),
        "COPS_PROJECT_ID": "vidproj",
    }
    src = tmp_path / filename
    src.write_bytes(b"\x00" * 100)
    result = subprocess.run(
        [str(script), str(src), caption],
        capture_output=True, text=True, env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@pytest.mark.parametrize("ext", ["mp4", "webm", "mov", "ogg", "ogv"])
def test_cockpit_img_accepts_video_extension(tmp_path, ext):
    """cockpit-img must accept all supported video extensions and print markdown."""
    rc, out, err = _run_cockpit_img(tmp_path, f"clip.{ext}", "myvideo")
    assert rc == 0, f"cockpit-img exited {rc} for .{ext}: {err}"
    assert out.startswith("![myvideo]("), f"Unexpected output: {out!r}"
    assert f"/media/" in out and f".{ext}" in out


@pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "webp", "gif"])
def test_cockpit_img_still_accepts_image_extension(tmp_path, ext):
    """Image extensions must still work after the video extension was added."""
    rc, out, err = _run_cockpit_img(tmp_path, f"shot.{ext}", "screenshot")
    assert rc == 0, f"cockpit-img exited {rc} for .{ext}: {err}"
    assert out.startswith("![screenshot]("), f"Unexpected output: {out!r}"


def test_cockpit_img_rejects_unsupported_extension(tmp_path):
    """cockpit-img must reject unlisted extensions (e.g. .avi) with exit code 1."""
    rc, _out, err = _run_cockpit_img(tmp_path, "clip.avi", "bad")
    assert rc == 1, f"Expected exit 1 for .avi, got {rc}"
    assert "unsupported" in err.lower()


# ─────────────────────────── cockpit-file helper (card 2efd6a) ───────────────


def _run_cockpit_file(tmp_path: Path, filename: str) -> tuple[int, str, str]:
    """Run the cockpit-file shell script and return (returncode, stdout, stderr)."""
    import subprocess
    script = ROOT / "tools" / "cockpit-file"
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "COPS_MEDIA_DIR": str(media_dir),
        "COPS_PROJECT_ID": "vidproj",
    }
    src = tmp_path / filename
    src.write_bytes(b"\x00" * 100)
    result = subprocess.run(
        [str(script), str(src)],
        capture_output=True, text=True, env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


@pytest.mark.parametrize("filename", ["report.pdf", "bundle.zip", "data.csv", "notes.docx", "clip.avi"])
def test_cockpit_file_accepts_arbitrary_extension(tmp_path, filename):
    """cockpit-file must accept ANY extension and print one 'attached file:' URL line."""
    rc, out, err = _run_cockpit_file(tmp_path, filename)
    assert rc == 0, f"cockpit-file exited {rc} for {filename}: {err}"
    assert out.startswith("attached file: /api/projects/vidproj/media/"), f"Unexpected output: {out!r}"


def test_cockpit_file_accepts_no_extension(tmp_path):
    """A file with no extension (e.g. 'Makefile') is still handed over as a download."""
    rc, out, err = _run_cockpit_file(tmp_path, "Makefile")
    assert rc == 0, f"cockpit-file exited {rc}: {err}"
    assert out.startswith("attached file: /api/projects/vidproj/media/")


def test_cockpit_file_copies_into_media_dir(tmp_path):
    """The file must land in COPS_MEDIA_DIR with a <ts>_ prefixed name matching the printed URL."""
    rc, out, _err = _run_cockpit_file(tmp_path, "report.pdf")
    assert rc == 0
    media_dir = tmp_path / "media"
    copied = list(media_dir.glob("*_report.pdf"))
    assert len(copied) == 1, f"expected one copied file, got {list(media_dir.iterdir())}"
    assert copied[0].name in out

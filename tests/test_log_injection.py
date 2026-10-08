"""CodeQL py/log-injection: a client-chosen request path must not split a log entry.

aiohttp's ``request.path`` is percent-decoded, so ``%0A`` reaches the handler as a real
newline. The unhandled-exception line is parsed by the incident scanner and turned into a
card, so a forged line is more than cosmetic.
"""
import logging
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp  # noqa: E402


def test_log_safe_escapes_line_breaks_and_leaves_ordinary_text_alone():
    assert webapp._log_safe("/api/projects/abc/tasks") == "/api/projects/abc/tasks"
    assert webapp._log_safe("/проект/файл é") == "/проект/файл é"  # non-ASCII letters survive
    assert webapp._log_safe("a\nb\rc\r\nd") == "a\\nb\\rc\\r\\nd"
    assert webapp._log_safe("x y z\x85w\x0bv\x1cu") == "x\\u2028y\\u2029z\\x85w\\x0bv\\x1cu"
    assert len(webapp._log_safe("a\nb\rc d").splitlines()) == 1


async def test_unhandled_exception_log_line_cannot_be_forged(aiohttp_client, caplog, monkeypatch):
    reported = []

    async def fake_report(ctx, exc_class, where, project_id="cardloop"):
        reported.append(where)

    monkeypatch.setattr(webapp, "_report_incident", fake_report)

    async def boom(_req):
        raise ValueError("kaboom")

    app = web.Application(middlewares=[webapp.error_middleware])
    app["ctx"] = {}
    app.router.add_get("/boom/{item}", boom)  # {item} matches a decoded newline
    client = await aiohttp_client(app)

    with caplog.at_level(logging.ERROR):
        resp = await client.get("/boom/x%0AERROR%20root%20UNHANDLED%20exc_class=Forged%20path=pwned")
    assert resp.status == 500

    unhandled = [r for r in caplog.records if "UNHANDLED" in r.getMessage()]
    assert len(unhandled) == 1
    msg = unhandled[0].getMessage()
    assert "\n" not in msg and "\r" not in msg, msg
    assert msg.startswith("UNHANDLED exc_class=ValueError path=/boom/x%0AERROR%20root%20UNHANDLED")

    # The scanner sees exactly one UNHANDLED token, for the real exception class - the
    # forged text stays inside the (encoded) path value.
    found = webapp._UNHANDLED_RE.findall(caplog.text)
    assert [c for c, _ in found] == ["ValueError"]

    import asyncio
    await asyncio.sleep(0)  # let the fire-and-forget report run
    assert reported and "\n" not in reported[0] and " " not in reported[0]


def test_ws_origin_refusal_log_cannot_be_forged(caplog):
    req = make_mocked_request(
        "GET", "/api/terminal/ws%0AERROR%20root%20forged",
        headers={"Origin": "https://evil.example", "Host": "cockpit.example"},
        app=web.Application(),
    )
    req.app["ctx"] = {}
    with caplog.at_level(logging.WARNING):
        resp = webapp._ws_origin_refusal(req)
    assert resp is not None and resp.status == 403
    refused = [r for r in caplog.records if "[ws-origin] refused" in r.getMessage()]
    assert len(refused) == 1
    assert "\n" not in refused[0].getMessage()
    assert "\\n" in refused[0].getMessage()  # escaped, not dropped


@pytest.mark.parametrize("raw", ["/api/x", "/api/projects/ab12/tasks"])
def test_ordinary_paths_keep_the_scanner_dedup_hash(raw):
    """_report_incident and the log scanner must keep hashing the same text."""
    line = f"UNHANDLED exc_class=ValueError path={webapp._log_safe(raw)} request_id=ab12"
    errs = webapp._parse_log_errors(line, source="log")
    assert len(errs) == 1 and errs[0]["message"] == f"unhandled at {raw}"

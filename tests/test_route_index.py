"""docs/API-routes.md is generated from the live router and must not drift.

The index is the reference documentation of the HTTP interface (OpenSSF
`documentation_interface`). It is only worth anything if it cannot lag behind the
code, so this test regenerates it in memory and compares it byte for byte.
"""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
INDEX = ROOT / "docs" / "API-routes.md"
STALE_HINT = ("docs/API-routes.md is out of date with the registered routes. "
              "Regenerate it with:  venv/bin/python tools/gen_route_index.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("gen_route_index", ROOT / "tools" / "gen_route_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def test_route_index_is_up_to_date(tool):
    assert INDEX.exists(), STALE_HINT
    assert tool.generate() == INDEX.read_text(encoding="utf-8"), STALE_HINT


def test_a_new_route_changes_the_index(tool, monkeypatch):
    """Mutation guard: the generator must notice a route nobody documented."""
    from aiohttp import web

    real_build = tool.build_app

    def build_with_extra_route():
        app, wa, features = real_build()

        async def api_not_documented(_req):
            """A dummy route that exists only in this test."""
            return web.json_response({})

        app.router.add_get("/api/zz-not-documented", api_not_documented)
        return app, wa, features

    monkeypatch.setattr(tool, "build_app", build_with_extra_route)
    text = tool.generate()
    assert "/api/zz-not-documented" in text
    assert text != INDEX.read_text(encoding="utf-8")


def test_a_deleted_row_is_detected(tool):
    """Mutation guard on the file side: removing a table row must fail the comparison."""
    current = INDEX.read_text(encoding="utf-8")
    rows = [ln for ln in current.splitlines() if ln.startswith("| `")]
    assert len(rows) > 150, "the table should carry every route"
    mutated = current.replace(rows[0] + "\n", "", 1)
    assert mutated != current
    assert tool.generate() != mutated


def test_index_covers_every_core_route_registered_in_start(tool):
    """The count of `app.router.add_*` calls in webapp.start() (+ feature routes) is the
    floor of the index: a registration style the generator skips would show up here."""
    src = (ROOT / "webapp.py").read_text(encoding="utf-8")
    start = src[src.index("async def start(ctx"):]
    block = start[: start.index("spa_handler)")]
    calls = len(re.findall(r"app\.router\.add_(?:get|post|put|delete|patch|route)\(", block))
    rows = [ln for ln in INDEX.read_text(encoding="utf-8").splitlines() if ln.startswith("| `")]
    assert calls > 150
    assert len(rows) >= calls

"""
spec-095 P7a: backend leftovers found by the earlier phases, each pinned through the REAL route.

  * the live `result` frame names the window the adapter engine reported (Grok 256K), not Claude's;
  * the project-settings POST accepts null for the one field whose GET value can be null and is
    inherit-able (`context_pack_enabled`), and stays strict everywhere else;
  * the session picker's rename shows for Grok and Codex rows (the cockpit label store is applied
    in their list branches, a manual label wins over the CLI's title, clearing falls back to it).

The Claude/Codex payloads of these endpoints were also diffed byte-for-byte against the previous
commit (every request identical except the three deliberate changes above).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import webapp as _webapp  # noqa: E402
from test_grok_history import a, put_session, q, summ  # noqa: E402,F401
from test_grok_p3p4_wiring import SID1, SID2, SID3, home, cwd  # noqa: E402,F401 - fixtures by name
from test_grok_wiring import (  # noqa: E402,F401 - fixtures used by name
    CHAT_ID, PROJECT_ID, SESSION_KEY, SHAPES, _allow, _auth, _chat_record, _seed_chat, _sse_events,
    codex_on, engines, fake_ctx, grok_on, isolate,
)

CW = _webapp.CONTEXT_WINDOW
URL = f"/api/projects/{PROJECT_ID}"


@pytest.fixture
def app(fake_ctx):
    from aiohttp import web

    ap = web.Application(middlewares=[_webapp.auth_middleware])
    ap["ctx"] = fake_ctx
    ap.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    ap.router.add_get("/api/projects/{id}/settings", _webapp.api_project_settings_get)
    ap.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    ap.router.add_get("/api/projects/{id}/sessions", _webapp.api_project_sessions)
    ap.router.add_post("/api/projects/{id}/sessions/{sid}/label", _webapp.api_project_session_label)
    ap.router.add_get("/api/projects/{id}/files", _webapp.api_project_files)
    ap.router.add_get("/api/projects/{id}/file", _webapp.api_project_file)
    return ap


@pytest.fixture
def quiet_run(monkeypatch):
    monkeypatch.setattr(_webapp, "_build_agents_kwargs", lambda *a, **k: {})
    monkeypatch.setattr(_webapp, "_secrets_read", lambda *a, **k: {})


# ═════════════════ the live meter's window ════════════════════════════════════

ENGINE_KEY = {"claude": "run_engine", "codex": "run_codex_engine", "grok": "run_grok_engine"}


def _engine_reporting(provider, extra):
    own_key = SHAPES[provider][1]

    async def engine(**kwargs):
        yield {"type": "text", "text": "answer"}
        event = {"type": "result", "context_tokens": 7, **extra}
        event[own_key] = "NEW-ID"
        yield event
    return engine


async def _result_frame(client, ctx, provider, extra):
    ctx[ENGINE_KEY[provider]] = _engine_reporting(provider, extra)
    resp = await client.post(f"{URL}/chat", json={"prompt": "hello", "chat_id": CHAT_ID},
                             headers=_auth(ctx))
    assert resp.status == 200, await resp.text()
    frames = [e for e in await _sse_events(resp) if e.get("type") == "result"]
    assert len(frames) == 1, frames
    return frames[0]


def _on(provider, ctx, grok_on_fixture=None):
    if provider == "grok":
        _allow(ctx)
    _seed_chat(ctx, provider=provider)


GOOD = [256000, 258400, 1, 1_000_000]
BAD = [None, 0, -1, -256000, True, False, "256000", 256000.0, [], {}, "x"]


@pytest.mark.parametrize("provider,fixtures", [("codex", ("codex_on",)), ("grok", ("grok_on",))])
async def test_an_adapter_turn_reports_the_window_its_engine_reported(
    aiohttp_client, fake_ctx, app, quiet_run, request, provider, fixtures
):
    for name in fixtures:
        request.getfixturevalue(name)
    client = await aiohttp_client(app)
    _on(provider, fake_ctx)
    for window in GOOD:
        frame = await _result_frame(client, fake_ctx, provider, {"context_window": window})
        assert frame["context_window"] == window, window
        assert frame["provider"] == provider


@pytest.mark.parametrize("provider,fixtures", [("codex", ("codex_on",)), ("grok", ("grok_on",))])
async def test_an_adapter_turn_without_a_usable_window_keeps_the_configured_one(
    aiohttp_client, fake_ctx, app, quiet_run, request, provider, fixtures
):
    for name in fixtures:
        request.getfixturevalue(name)
    client = await aiohttp_client(app)
    _on(provider, fake_ctx)
    frame = await _result_frame(client, fake_ctx, provider, {})
    assert frame["context_window"] == CW                         # key absent
    for window in BAD:
        frame = await _result_frame(client, fake_ctx, provider, {"context_window": window})
        assert frame["context_window"] == CW, repr(window)


async def test_a_claude_turn_never_takes_the_window_from_its_event(aiohttp_client, fake_ctx, app, quiet_run):
    client = await aiohttp_client(app)
    _seed_chat(fake_ctx, provider="claude")
    for extra in ({}, {"context_window": 4242}, {"context_window": 256000}, {"context_window": None}):
        frame = await _result_frame(client, fake_ctx, "claude", extra)
        assert frame["context_window"] == CW, extra
        assert frame["provider"] == "claude"


async def test_the_rest_of_the_result_frame_is_untouched_by_the_window_choice(
    aiohttp_client, fake_ctx, app, quiet_run, grok_on
):
    client = await aiohttp_client(app)
    _on("grok", fake_ctx)
    plain = await _result_frame(client, fake_ctx, "grok", {})
    windowed = await _result_frame(client, fake_ctx, "grok", {"context_window": 256000})
    assert {k: v for k, v in plain.items() if k != "context_window"} == \
           {k: v for k, v in windowed.items() if k != "context_window"}
    assert plain["context_tokens"] == 7 and plain["grok_session_id"] == "NEW-ID"
    assert plain["context_warn_at"] == _webapp.CONTEXT_WARN_AT


# ═════════════════ settings: null = reset to inherit ══════════════════════════


async def _post(client, ctx, body):
    resp = await client.post(f"{URL}/settings", json=body, headers=_auth(ctx))
    return resp.status, await resp.json()


async def _get(client, ctx):
    resp = await client.get(f"{URL}/settings", headers=_auth(ctx))
    assert resp.status == 200
    return await resp.json()


def _topic(ctx) -> dict:
    return ctx["topics"][SESSION_KEY]


async def test_null_resets_context_pack_enabled_to_inherit(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    status, _ = await _post(client, fake_ctx, {"context_pack_enabled": False})
    assert status == 200 and _topic(fake_ctx)["context_pack_enabled"] is False
    assert (await _get(client, fake_ctx))["context_pack_enabled"] is False
    status, body = await _post(client, fake_ctx, {"context_pack_enabled": None})
    assert status == 200 and body["ok"] is True
    assert "context_pack_enabled" not in _topic(fake_ctx)          # stored as a reset, not as null
    assert body["settings"]["context_pack_enabled"] is None
    assert (await _get(client, fake_ctx))["context_pack_enabled"] is None


async def test_null_on_a_project_that_never_chose_is_a_no_op_not_an_error(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    assert "context_pack_enabled" not in _topic(fake_ctx)
    status, body = await _post(client, fake_ctx, {"context_pack_enabled": None})
    assert status == 200 and "context_pack_enabled" not in _topic(fake_ctx)


async def test_the_whole_get_record_posts_back_unchanged(aiohttp_client, fake_ctx, app):
    # what the settings tab does on Save: GET (null for an unchosen context pack), POST it all back
    client = await aiohttp_client(app)
    record = await _get(client, fake_ctx)
    assert record["context_pack_enabled"] is None
    status, body = await _post(client, fake_ctx, record)
    assert status == 200, body
    assert (await _get(client, fake_ctx)) == record


async def test_a_bool_still_sets_and_other_values_are_still_refused(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    for value in (True, False):
        status, _ = await _post(client, fake_ctx, {"context_pack_enabled": value})
        assert status == 200 and _topic(fake_ctx)["context_pack_enabled"] is value
    for bad in ("false", "true", "", 0, 1, [], {}):
        status, body = await _post(client, fake_ctx, {"context_pack_enabled": bad})
        assert status == 400 and body["error"] == "context_pack_enabled: expected bool", bad
    assert _topic(fake_ctx)["context_pack_enabled"] is False        # a refused value left it alone


async def test_the_other_bools_have_no_inherit_state_and_stay_strict_about_null(aiohttp_client, fake_ctx, app):
    client = await aiohttp_client(app)
    for key in ("git_enabled", "notify_on_error"):
        status, body = await _post(client, fake_ctx, {key: None})
        assert status == 400 and body["error"] == f"{key}: expected bool", key
        assert key not in _topic(fake_ctx)


async def test_a_refused_field_in_the_same_body_applies_nothing_not_even_the_null_reset(
    aiohttp_client, fake_ctx, app
):
    client = await aiohttp_client(app)
    await _post(client, fake_ctx, {"context_pack_enabled": False})
    status, _ = await _post(client, fake_ctx, {"context_pack_enabled": None, "git_enabled": None})
    assert status == 400
    assert _topic(fake_ctx)["context_pack_enabled"] is False
    status, _ = await _post(client, fake_ctx, {"context_pack_enabled": None, "test_cmd": "pytest"})
    assert status == 200 and _topic(fake_ctx)["test_cmd"] == "pytest"
    assert "context_pack_enabled" not in _topic(fake_ctx)


async def test_a_reset_context_pack_inherits_the_global_default_at_run_time(fake_ctx):
    _topic(fake_ctx)["context_pack_enabled"] = False
    assert _webapp._project_context_pack_enabled(_topic(fake_ctx)) is False
    _topic(fake_ctx).pop("context_pack_enabled")
    assert _webapp._project_context_pack_enabled(_topic(fake_ctx)) is True


# ═════════════════ session rename ═════════════════════════════════════════════


async def _rename(client, ctx, sid, label):
    resp = await client.post(f"{URL}/sessions/{sid}/label", json={"label": label}, headers=_auth(ctx))
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _rows(client, ctx) -> dict:
    resp = await client.get(f"{URL}/sessions", headers=_auth(ctx))
    body = await resp.json()
    return {r["session_id"]: r for r in body["sessions"]}


async def test_a_renamed_grok_session_shows_its_new_name(aiohttp_client, fake_ctx, app, home, cwd, grok_on):
    put_session(home, cwd, SID1, chat=[q("first message of an untitled session")], summary=summ())
    put_session(home, cwd, SID2, chat=[q("x")], summary=summ("2026-10-02T11:00:00Z",
                                                            session_summary="Title from the CLI"))
    _seed_chat(fake_ctx, provider="grok", grok_session_id=SID1)
    client = await aiohttp_client(app)
    rows = await _rows(client, fake_ctx)
    assert rows[SID1]["label"] is None and rows[SID2]["label"] == "Title from the CLI"
    await _rename(client, fake_ctx, SID1, "  My renamed one  ")
    await _rename(client, fake_ctx, SID2, "Mine, not the CLI's")
    rows = await _rows(client, fake_ctx)
    assert rows[SID1]["label"] == "My renamed one"                  # untitled session gets a name
    assert rows[SID2]["label"] == "Mine, not the CLI's"             # a manual name wins over the title
    assert rows[SID2]["preview"] == "Title from the CLI"            # the preview is not renamed
    assert rows[SID1]["provider"] == "grok" and rows[SID1]["is_active"] is True


async def test_clearing_a_grok_rename_falls_back_to_the_cli_title(aiohttp_client, fake_ctx, app, home, cwd, grok_on):
    put_session(home, cwd, SID2, chat=[q("x")], summary=summ(session_summary="Title from the CLI"))
    _seed_chat(fake_ctx, provider="grok")
    client = await aiohttp_client(app)
    await _rename(client, fake_ctx, SID2, "Mine")
    assert (await _rows(client, fake_ctx))[SID2]["label"] == "Mine"
    body = await _rename(client, fake_ctx, SID2, "")
    assert body["label"] is None
    assert (await _rows(client, fake_ctx))[SID2]["label"] == "Title from the CLI"


async def test_a_rename_only_touches_its_own_session_and_survives_a_reload(
    aiohttp_client, fake_ctx, app, home, cwd, grok_on
):
    put_session(home, cwd, SID1, chat=[q("one")], summary=summ())
    put_session(home, cwd, SID2, chat=[q("two")], summary=summ("2026-10-02T11:00:00Z"))
    _seed_chat(fake_ctx, provider="grok")
    client = await aiohttp_client(app)
    await _rename(client, fake_ctx, SID1, "Only this one")
    rows = await _rows(client, fake_ctx)
    assert rows[SID1]["label"] == "Only this one" and rows[SID2]["label"] is None
    stored = json.loads((fake_ctx["DATA"] / "session_labels.json").read_text())
    assert stored == {SID1: "Only this one"}
    assert (await _rows(client, fake_ctx)) == rows                    # same answer on the next read


async def test_a_label_of_another_provider_s_session_does_not_leak_into_a_grok_list(
    aiohttp_client, fake_ctx, app, home, cwd, grok_on
):
    put_session(home, cwd, SID1, chat=[q("one")], summary=summ())
    _seed_chat(fake_ctx, provider="grok")
    (fake_ctx["DATA"] / "session_labels.json").write_text(json.dumps({"some-claude-session": "Claude's name"}))
    client = await aiohttp_client(app)
    assert (await _rows(client, fake_ctx))[SID1]["label"] is None


async def test_a_grok_list_survives_a_corrupt_label_store(aiohttp_client, fake_ctx, app, home, cwd, grok_on):
    put_session(home, cwd, SID1, chat=[q("one")], summary=summ(session_summary="CLI title"))
    _seed_chat(fake_ctx, provider="grok")
    (fake_ctx["DATA"] / "session_labels.json").write_text("{not json")
    client = await aiohttp_client(app)
    assert (await _rows(client, fake_ctx))[SID1]["label"] == "CLI title"


async def test_a_renamed_codex_thread_shows_its_new_name_too(aiohttp_client, fake_ctx, app, codex_on, monkeypatch):
    async def list_threads(**kw):
        return [{"id": "T-ACTIVE", "recencyAt": 1_800_000_000, "preview": "p1", "name": "from codex",
                 "turns": [1, 2]},
                {"id": "T-OTHER", "recencyAt": 1_700_000_000, "preview": "p2", "name": None, "turns": []}]
    monkeypatch.setattr(_webapp._codex, "list_threads", list_threads)
    _seed_chat(fake_ctx, provider="codex", codex_thread_id="T-ACTIVE")
    client = await aiohttp_client(app)
    rows = await _rows(client, fake_ctx)
    assert rows["T-ACTIVE"]["label"] == "from codex" and rows["T-OTHER"]["label"] is None
    await _rename(client, fake_ctx, "T-ACTIVE", "Mine")
    await _rename(client, fake_ctx, "T-OTHER", "Also mine")
    rows = await _rows(client, fake_ctx)
    assert rows["T-ACTIVE"]["label"] == "Mine" and rows["T-OTHER"]["label"] == "Also mine"
    await _rename(client, fake_ctx, "T-ACTIVE", "")
    assert (await _rows(client, fake_ctx))["T-ACTIVE"]["label"] == "from codex"


async def test_claude_rename_still_works_as_before(aiohttp_client, fake_ctx, app, tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "sess-1.jsonl").write_text('{"type":"user","message":{"role":"user","content":"hello there"}}\n')
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: sdk)
    _seed_chat(fake_ctx, provider="claude", session_id="sess-1")
    fake_ctx["sessions"][SESSION_KEY] = "sess-1"
    client = await aiohttp_client(app)
    assert (await _rows(client, fake_ctx))["sess-1"]["label"] is None
    await _rename(client, fake_ctx, "sess-1", "Claude's name")
    assert (await _rows(client, fake_ctx))["sess-1"]["label"] == "Claude's name"


# ═════════════════ the Grok home beside the data dir, inside a checkout ═════════════════

async def test_a_projects_file_listing_and_reader_never_serve_the_grok_home(aiohttp_client, fake_ctx, app, cwd):
    """The default GROK_HOME is `<data>-grok-home`, a sibling of data/ — inside the cockpit's own checkout,
    which is a project too. Its auth.json is a login token."""
    home_dir = Path(cwd) / "data-grok-home"
    home_dir.mkdir()
    (home_dir / "auth.json").write_text('{"key": "TOKEN"}')
    (Path(cwd) / "README.md").write_text("hello")
    c = await aiohttp_client(app)
    h = _auth(fake_ctx)
    listing = await c.get(f"{URL}/files", headers=h)
    names = {e["name"] for e in (await listing.json())["entries"]}
    assert "README.md" in names and "data-grok-home" not in names
    r = await c.get(f"{URL}/files?path=data-grok-home", headers=h)
    assert r.status == 404
    r = await c.get(f"{URL}/file?path=data-grok-home/auth.json", headers=h)
    assert r.status == 403 and "TOKEN" not in await r.text()

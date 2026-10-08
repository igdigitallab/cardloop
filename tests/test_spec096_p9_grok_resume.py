"""spec-096 P9 item D: a Grok session is resumed only if the cockpit vouches for it in THIS cwd.

GROK_HOME is writable by the model's shell, so a turn can drop a session directory under another
project's group. The history readers already hide such a directory (`grok_history.vouched`), but the
Resume action and the run sites asked only `session_exists` - and the engine then recorded a
binding for whatever id it resumed, which vouches for it permanently. One resume by id laundered
the planted session. Resuming now requires the witness BEFORE anything is recorded.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import grok_engine
import grok_history
import grok_sends
import providers
import webapp as _webapp

from test_grok_history import a, put_session, q, summ
from test_grok_p3p4_wiring import (  # noqa: F401 - fixtures used by name
    PROJECT_ID, SESSION_KEY, SID1, SID2, SID3, _auth, _chat_record, _seed_chat, app, cwd, home,
)
from test_grok_wiring import engines, fake_ctx, grok_on, isolate  # noqa: F401


def _binding_file(ctx, sid):
    return ctx["DATA"] / grok_engine.SESSION_BINDINGS_DIR / sid


# ─────────────────────────── grok_history.resumable ───────────────────────────


def test_resumable_needs_the_session_on_disk_AND_the_cockpits_witness(home, tmp_path):
    data = tmp_path / "data"
    cwd = "/scratch/projA"
    put_session(home, cwd, SID1, chat=[q("x"), a("y")], summary=summ(), bound=False)
    assert grok_history.session_exists(SID1, cwd, grok_home=home) is True
    assert grok_history.resumable(SID1, cwd, grok_home=home, data_dir=data) is False     # planted: no witness
    assert grok_engine.record_session_binding(data, SID1, cwd)
    assert grok_history.resumable(SID1, cwd, grok_home=home, data_dir=data) is True      # the engine saw it born here
    assert grok_history.resumable(SID1, "/scratch/projB", grok_home=home, data_dir=data) is False
    assert grok_history.resumable(SID2, cwd, grok_home=home, data_dir=data) is False     # not on disk at all
    assert grok_history.resumable("../etc", cwd, grok_home=home, data_dir=data) is False  # hostile id: False, no raise
    # a witness for a session whose directory is gone (a wiped GROK_HOME) is not a session to resume
    assert grok_engine.record_session_binding(data, SID2, cwd)
    assert grok_history.resumable(SID2, cwd, grok_home=home, data_dir=data) is False


def test_a_session_that_predates_the_binding_is_resumable_through_the_send_ledger(home, tmp_path):
    data = tmp_path / "data"
    cwd = "/scratch/projA"
    put_session(home, cwd, SID1, chat=[q("old")], summary=summ(), bound=False)
    assert grok_history.resumable(SID1, cwd, grok_home=home, data_dir=data) is False
    assert grok_sends.record(data, SID1, "old")
    assert grok_history.resumable(SID1, cwd, grok_home=home, data_dir=data) is True


# ─────────────────────────── the Resume action ───────────────────────────


async def test_resuming_a_planted_session_is_refused_and_records_no_binding(
    aiohttp_client, fake_ctx, app, home, cwd, grok_on
):
    put_session(home, cwd, SID2, chat=[q("planted by a shell in another project")], summary=summ(), bound=False)
    _seed_chat(fake_ctx, provider="grok", grok_session_id=SID1)
    client = await aiohttp_client(app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/session",
                          json={"action": "resume", "session_id": SID2}, headers=_auth(fake_ctx))
    assert r.status == 400 and await r.json() == {"error": "session not found"}
    assert _chat_record(fake_ctx)["grok_session_id"] == SID1            # the chat did not move
    assert not _binding_file(fake_ctx, SID2).exists()                    # and nothing vouches for the plant


async def test_resuming_a_session_the_cockpit_vouches_for_still_works(
    aiohttp_client, fake_ctx, app, home, cwd, grok_on
):
    put_session(home, cwd, SID2, chat=[q("an old conversation")], summary=summ())           # bound=True
    put_session(home, cwd, SID3, chat=[q("older, before the bindings")], summary=summ(), bound=False)
    assert grok_sends.record(fake_ctx["DATA"], SID3, "older, before the bindings")           # the ledger vouches
    _seed_chat(fake_ctx, provider="grok", grok_session_id=SID1)
    client = await aiohttp_client(app)
    url = f"/api/projects/{PROJECT_ID}/session"
    for sid in (SID2, SID3):
        r = await client.post(url, json={"action": "resume", "session_id": sid}, headers=_auth(fake_ctx))
        assert await r.json() == {"active": sid, "provider": "grok"}
        assert _chat_record(fake_ctx)["grok_session_id"] == sid


# ─────────────────────────── the run sites ───────────────────────────


async def test_a_stored_id_nobody_vouches_for_is_dropped_loudly_before_the_engine_can_bind_it(
    fake_ctx, home, cwd, grok_on, capsys
):
    spec = providers.get("grok")
    put_session(home, cwd, SID2, chat=[q("planted")], summary=summ(), bound=False)
    put_session(home, cwd, SID1, chat=[q("mine")], summary=summ())
    got = await _webapp._live_resume_id(spec, fake_ctx, SESSION_KEY, cwd, SID2)
    assert got is None
    assert "no longer exists" in capsys.readouterr().out
    assert await _webapp._live_resume_id(spec, fake_ctx, SESSION_KEY, cwd, SID1) == SID1
    # a session vouched for in ANOTHER directory does not follow the chat into this one
    other = str(Path(cwd).parent / "elsewhere")
    assert await _webapp._live_resume_id(spec, fake_ctx, SESSION_KEY, other, SID1) is None
